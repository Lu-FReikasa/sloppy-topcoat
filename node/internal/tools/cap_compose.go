package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// composeFileName is what may be read out of a project's working directory next to the files
// compose itself was started with: the compose files and the .env files.
var composeFileName = regexp.MustCompile(`^(?:\.env(?:\.[A-Za-z0-9_.\-]+)?|[A-Za-z0-9_.\-]+\.env|(?:docker-)?compose(?:\.[A-Za-z0-9_\-]+)?\.ya?ml)$`)

type composeProject struct {
	Name       string
	WorkingDir string
	Files      []string
	Services   []map[string]any
	Running    int
	Total      int
}

// composeProjects groups the containers by their compose labels.
func composeProjects(ctx context.Context, d DockerAPI) ([]*composeProject, error) {
	list, err := listContainers(ctx, d, true)
	if err != nil {
		return nil, err
	}
	byName := map[string]*composeProject{}
	for _, c := range list {
		name := c.Labels[labelComposeProject]
		if name == "" {
			continue
		}
		p := byName[name]
		if p == nil {
			p = &composeProject{Name: name, WorkingDir: c.Labels[labelComposeWorkdir]}
			for _, f := range strings.Split(c.Labels[labelComposeFiles], ",") {
				if f = strings.TrimSpace(f); f != "" {
					p.Files = append(p.Files, f)
				}
			}
			byName[name] = p
		}
		p.Total++
		if c.State == "running" {
			p.Running++
		}
		p.Services = append(p.Services, map[string]any{
			"service": c.Labels[labelComposeService], "container": trimName(c.Names), "state": c.State, "status": c.Status,
		})
	}
	out := make([]*composeProject, 0, len(byName))
	for _, p := range byName {
		sort.Slice(p.Services, func(i, j int) bool {
			return fmt.Sprint(p.Services[i]["service"], p.Services[i]["container"]) < fmt.Sprint(p.Services[j]["service"], p.Services[j]["container"])
		})
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func composeProjectsCap(o Options) capability.Capability {
	return fn{
		name:   "compose.projects",
		desc:   "Compose projects running on this Docker host (from container labels): name, state of each service and — only for projects inside the directories released in config.tools.compose_roots — working directory and compose files.",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			ps, err := composeProjects(ctx, d)
			if err != nil {
				return nil, err
			}
			out := make([]map[string]any, 0, len(ps))
			for _, p := range ps {
				e := map[string]any{"project": p.Name, "running": p.Running, "containers": p.Total, "services": p.Services}
				// Paths are shown only inside the released directories: where a stack keeps its
				// files is not for a node to tell unless the operator released that place.
				if p.WorkingDir != "" && o.composeRootsOK(p.WorkingDir) {
					files := []string{}
					for _, f := range p.Files {
						if o.composeRootsOK(f) {
							files = append(files, f)
						}
					}
					e["working_dir"], e["config_files"], e["files_released"] = p.WorkingDir, files, true
				} else {
					e["files_released"] = false
				}
				out = append(out, e)
			}
			return map[string]any{"projects": out, "compose_roots_set": len(o.ComposeRoots) > 0}, nil
		},
	}
}

// within reports whether path is dir or below it.
func within(path, dir string) bool {
	rel, err := filepath.Rel(dir, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && !filepath.IsAbs(rel)
}

// errNoComposeRoots is what a node without released directories answers to compose.file.
var errNoComposeRoots = inputErr("no directories are released for reading compose files: config.tools.compose_roots is empty (the default is deny; see INSTALL-NODE)")

// composeRootsOK reports whether path lies inside one of the released directories. Nothing is
// released by default: without compose_roots no compose or .env file is ever read or listed.
func (o Options) composeRootsOK(path string) bool {
	if len(o.ComposeRoots) == 0 {
		return false
	}
	real, err := filepath.EvalSymlinks(path)
	if err != nil {
		return false
	}
	for _, r := range o.ComposeRoots {
		if rr, err := filepath.EvalSymlinks(r); err == nil && within(real, rr) {
			return true
		}
	}
	return false
}

// resolveComposeFile decides which file of a project may be read. Either it is one of the files
// the project was started with, or a compose/.env file (by base name) in its working directory.
// Nothing else is ever read, whatever the caller asks for, and a symlink cannot lead out of the
// directory.
func (o Options) resolveComposeFile(p *composeProject, file string) (string, error) {
	if file == "" {
		if len(p.Files) == 0 {
			return "", inputErr("project %q has no compose file listed; name one of its .env or compose files", p.Name)
		}
		file = p.Files[0]
	}
	for _, f := range p.Files {
		if filepath.Clean(f) == filepath.Clean(file) {
			// the labels come from the container, i.e. from whoever may create containers: they
			// name a compose file, never anything else (the node's own key, /etc/…)
			if ext := strings.ToLower(filepath.Ext(f)); ext != ".yml" && ext != ".yaml" && !composeFileName.MatchString(filepath.Base(f)) {
				return "", inputErr("only YAML compose files and .env files are read")
			}
			if !o.composeRootsOK(f) {
				return "", inputErr("that file is outside the directories this node may read (config.tools.compose_roots)")
			}
			return f, nil
		}
	}
	if file != filepath.Base(file) || !composeFileName.MatchString(file) {
		return "", inputErr("file must be one of the project's compose files or a compose / .env file name in its working directory")
	}
	if p.WorkingDir == "" {
		return "", inputErr("project %q has no working directory", p.Name)
	}
	if !o.composeRootsOK(p.WorkingDir) {
		return "", inputErr("the project directory is outside the directories this node may read (config.tools.compose_roots)")
	}
	dir, err := filepath.EvalSymlinks(p.WorkingDir)
	if err != nil {
		return "", fmt.Errorf("the project directory cannot be read: %w", err)
	}
	real, err := filepath.EvalSymlinks(filepath.Join(dir, file))
	if err != nil {
		return "", inputErr("no such file in the project directory")
	}
	if !within(real, dir) {
		return "", inputErr("that file is a link that leads out of the project directory")
	}
	return real, nil
}

func composeFileCap(o Options) capability.Capability {
	return fn{
		name: "compose.file",
		desc: "Content of one file of a compose project (a compose file or a .env file), secrets masked. Only inside the directories released in config.tools.compose_roots (none by default: the call is refused). Without `file` the project's first compose file; `available` lists the compose and .env files of its directory.",
		schema: `{"type":"object","additionalProperties":false,"required":["project"],"properties":{` +
			`"project":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.-]*$","description":"compose project name (see compose.projects)"},` +
			`"file":{"type":"string","maxLength":512,"description":"a compose file path listed by compose.projects, or a file name like .env or compose.yaml in the project directory"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Project string `json:"project"`
				File    string `json:"file"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if len(o.ComposeRoots) == 0 {
				return nil, errNoComposeRoots
			}
			if !projectName.MatchString(in.Project) {
				return nil, inputErr("project must be a compose project name")
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			ps, err := composeProjects(ctx, d)
			if err != nil {
				return nil, err
			}
			var p *composeProject
			for _, c := range ps {
				if c.Name == in.Project {
					p = c
				}
			}
			if p == nil {
				return nil, inputErr("no compose project %q on this host", in.Project)
			}
			path, err := o.resolveComposeFile(p, in.File)
			if err != nil {
				return nil, err
			}
			f, err := os.Open(path) //nolint:gosec // path comes from resolveComposeFile: a listed compose file or a whitelisted name inside the project directory
			if err != nil {
				return nil, fmt.Errorf("cannot read the file (does the node's user have access?): %w", err)
			}
			defer f.Close() //nolint:errcheck // read-only
			st, err := f.Stat()
			if err != nil || !st.Mode().IsRegular() {
				return nil, inputErr("that is not a regular file")
			}
			data, err := io.ReadAll(io.LimitReader(f, MaxFileBytes+1))
			if err != nil {
				return nil, err
			}
			text, masked := Mask(string(data))
			text, cut := headBytes(text, MaxFileBytes)
			out := map[string]any{"project": p.Name, "path": path, "content": text, "bytes": len(data), "truncated": cut, "masked": masked}
			out["available"] = o.composeFilesIn(p.WorkingDir)
			return out, nil
		},
	}
}

// composeFilesIn lists the compose and .env file names of a directory.
func (o Options) composeFilesIn(dir string) []string {
	if dir == "" || !o.composeRootsOK(dir) {
		return []string{}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return []string{}
	}
	names := []string{}
	for _, e := range entries {
		if !e.IsDir() && composeFileName.MatchString(e.Name()) {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names
}
