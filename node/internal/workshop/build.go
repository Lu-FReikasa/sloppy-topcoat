package workshop

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// repo.build_node and repo.build_list (E6-01 S3): cross-build topcoat-node from the worktree in a
// throw-away container and keep the unsigned binaries in the node's own output folder
// (<workshop>/out/<repo>/<version>/). The command is fixed here, like a test template; the
// repository supplies only the source. Nothing is signed, uploaded or announced: that stays with
// the maintainer (D52). The hub image is not built here: it needs a rootless image builder, which
// is decided after a spike on the server (brief E6-01-dev-workshop).
const (
	buildKey        = protocol.WorkshopBuildNode
	buildSeconds    = 900
	maxArtefacts    = 12
	maxArtefactMB   = 200
	outDirName      = "out"
	versionPatternS = `^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$`
)

var versionRe = regexp.MustCompile(versionPatternS)

// buildCmd runs the repository's own release script from node/ with the version as an argument
// (never spliced into the command line). The script is the one `make build` and the maintainer use.
var buildCmd = []string{"sh", "-c", `cd node && bash ../scripts/build-node-release.sh "$0" /work/.out`}

// artefact is one file a build produced.
type artefact struct {
	Name   string `json:"name"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

func (w *Workshop) outDir(repo, version string) string {
	return filepath.Join(w.dir, outDirName, repo, version)
}

func (w *Workshop) capBuildNode() capability.Capability {
	return wcap{
		name: "repo.build_node",
		desc: "Cross-build topcoat-node (Linux and Windows) from the worktree in a throw-away container and keep the UNSIGNED binaries in the workshop's output folder. Needs a node/ directory in the repository. Returns file names, sizes and SHA-256. Signing, uploading and rolling out stay with the maintainer.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo","version"],"properties":{` + repoProp + `,` +
			`"version":{"type":"string","minLength":1,"maxLength":40,"pattern":"` + versionPatternS + `","description":"the version string built in, e.g. 0.1.6-dev1"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo    string `json:"repo"`
				Version string `json:"version"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if !versionRe.MatchString(in.Version) {
				return nil, inputErr("the version may hold letters, digits and . _ + - (at most 40 characters)")
			}
			spec, err := w.specFor(ctx)
			if err != nil {
				return nil, err
			}
			dir, err := w.clone(in.Repo)
			if err != nil {
				return nil, err
			}
			rc := w.o.RunnerConfig
			image := rc.Images[buildKey]
			if image == "" {
				return nil, inputErr("this node has no image for builds (config.tools.workshop.runner.images[%s])", buildKey)
			}
			if st, err := os.Stat(filepath.Join(dir, "node", "cmd", "topcoat-node")); err != nil || !st.IsDir() {
				return nil, inputErr("this repository has no node/cmd/topcoat-node: repo.build_node builds Topcoat's node")
			}
			network := ""
			if rc.Network != "" && slices.Contains(rc.NetworkTemplates, buildKey) {
				network = rc.Network // Go modules are downloaded
			}
			unlock := w.lock(in.Repo)
			defer unlock()
			run := filepath.Join(w.dir, ".runs", fmt.Sprintf("%s-build-%d", in.Repo, w.o.Now().UnixNano()))
			defer os.RemoveAll(run)
			if err := w.copyWorktree(ctx, dir, run); err != nil {
				return nil, err
			}
			if err := os.MkdirAll(filepath.Join(run, ".out"), 0o755); err != nil {
				return nil, err
			}
			job := Job{
				Template: buildKey, Image: image, Cmd: append(append([]string{}, buildCmd...), in.Version),
				Dir: w.hostPath(run), Network: network,
				MemoryMB: orDefault(rc.MemoryMB, defaultTestMemoryMB), CPUMilli: orDefault(rc.CPUMilli, defaultTestCPUMilli),
				Timeout: time.Duration(orDefault(rc.TimeoutSeconds, buildSeconds)) * time.Second,
			}
			res, err := w.o.Runner.Run(ctx, job)
			if err != nil {
				return nil, fmt.Errorf("the build could not start: %w", err)
			}
			out, _ := tools.Mask(res.Output)
			if len(out) > testOutputBytes {
				out = "[… earlier output left out]\n" + out[len(out)-testOutputBytes:]
			}
			reply := map[string]any{
				"repo": in.Repo, "version": in.Version, "branch": w.currentBranch(ctx, dir),
				"exit_code": res.ExitCode, "timed_out": res.TimedOut, "built": false,
				"signed": false, "duration_ms": res.Duration.Milliseconds(), "network": network != "",
				"output": out, "base": spec.Branch,
			}
			if res.ExitCode != 0 || res.TimedOut {
				return reply, nil
			}
			files, err := w.keepArtefacts(filepath.Join(run, ".out"), w.outDir(in.Repo, in.Version))
			if err != nil {
				return nil, err
			}
			reply["built"] = len(files) > 0
			reply["artefacts"] = files
			reply["folder"] = filepath.ToSlash(filepath.Join(outDirName, in.Repo, in.Version))
			return reply, nil
		},
	}
}

// keepArtefacts moves the regular files of a finished build to the output folder of the version
// (replacing an earlier build of the same version) and describes them.
func (w *Workshop) keepArtefacts(from, to string) ([]artefact, error) {
	entries, err := os.ReadDir(from)
	if err != nil {
		return nil, err
	}
	if len(entries) > maxArtefacts {
		return nil, inputErr("the build produced %d files; at most %d are kept", len(entries), maxArtefacts)
	}
	var total int64
	for _, e := range entries {
		info, err := e.Info()
		if err != nil || !info.Mode().IsRegular() {
			return nil, inputErr("the build produced %q, which is not a plain file", e.Name())
		}
		total += info.Size()
	}
	if total > int64(maxArtefactMB)<<20 {
		return nil, inputErr("the build output is larger than %d MB", maxArtefactMB)
	}
	if err := os.RemoveAll(to); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(to, 0o755); err != nil {
		return nil, err
	}
	var files []artefact
	for _, e := range entries {
		dst := filepath.Join(to, e.Name())
		if err := copyFile(filepath.Join(from, e.Name()), dst, 0o644); err != nil {
			return nil, err
		}
		a, err := describe(dst)
		if err != nil {
			return nil, err
		}
		files = append(files, a)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Name < files[j].Name })
	return files, nil
}

func describe(p string) (artefact, error) {
	f, err := os.Open(p)
	if err != nil {
		return artefact{}, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return artefact{}, err
	}
	return artefact{Name: filepath.Base(p), Bytes: n, SHA256: hex.EncodeToString(h.Sum(nil))}, nil
}

func (w *Workshop) capBuildList() capability.Capability {
	return wcap{
		name: "repo.build_list", readOnly: true,
		desc:   "The builds kept in the workshop's output folder for a repository: version, files, sizes and SHA-256.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo string `json:"repo"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if _, err := w.repoDir(in.Repo); err != nil {
				return nil, err
			}
			base := filepath.Join(w.dir, outDirName, in.Repo)
			versions, err := os.ReadDir(base)
			if err != nil {
				if os.IsNotExist(err) {
					return map[string]any{"repo": in.Repo, "builds": []any{}}, nil
				}
				return nil, err
			}
			var builds []map[string]any
			for _, v := range versions {
				if !v.IsDir() || !versionRe.MatchString(v.Name()) {
					continue
				}
				entries, _ := os.ReadDir(filepath.Join(base, v.Name()))
				var files []artefact
				var newest time.Time
				for _, e := range entries {
					info, err := e.Info()
					if err != nil || !info.Mode().IsRegular() {
						continue
					}
					a, err := describe(filepath.Join(base, v.Name(), e.Name()))
					if err != nil {
						continue
					}
					files = append(files, a)
					if info.ModTime().After(newest) {
						newest = info.ModTime()
					}
				}
				builds = append(builds, map[string]any{
					"version": v.Name(), "files": files, "built_at": newest.UTC().Format(time.RFC3339),
					"folder": filepath.ToSlash(filepath.Join(outDirName, in.Repo, v.Name())), "signed": false,
				})
			}
			sort.Slice(builds, func(i, j int) bool { return builds[i]["built_at"].(string) > builds[j]["built_at"].(string) })
			if len(builds) > 20 {
				builds = builds[:20]
			}
			return map[string]any{"repo": in.Repo, "builds": builds, "unsigned": true, "where": strings.TrimSpace(w.dir) + "/" + outDirName + "/" + in.Repo}, nil
		},
	}
}
