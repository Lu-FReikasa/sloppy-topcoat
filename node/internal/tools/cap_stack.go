package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"topcoat/node/internal/providers/capability"
)

// Stacks: a compose project looked at and restarted as one thing.
//
//   - compose.logs is read-only: the logs of every container of a project, merged by time, each
//     line marked with its service.
//   - compose.restart changes state (like docker.restart, so only capability.execute runs it,
//     after the owner's click on that one proposal). It restarts the RUNNING containers of one
//     project, one after the other, those another container depends on first. It needs no new
//     Docker endpoint: it is a series of the same single "restart a container" call the write
//     proxy already allows, and every container must be on the node's restart list (a stack with
//     one container off the list is refused as a whole, before anything is touched).
const (
	labelComposeDependsOn = "com.docker.compose.depends_on"
	// maxStackContainers bounds both capabilities: a project bigger than this is not restarted or
	// read in one call.
	maxStackContainers = 20
	// DefaultStackLogLines / MaxStackLogLines are per container.
	defaultStackLogLines = 50
	maxStackLogLines     = 200
)

// stackMembers are the containers (all states) of one compose project, by container name.
func stackMembers(ctx context.Context, d DockerAPI, project string) ([]apiContainerSummary, error) {
	list, err := listContainers(ctx, d, true)
	if err != nil {
		return nil, err
	}
	var out []apiContainerSummary
	for _, c := range list {
		if c.Labels[labelComposeProject] == project {
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool { return trimName(out[i].Names) < trimName(out[j].Names) })
	return out, nil
}

// dependencyOrder puts the containers in the order a restart should follow: a service that
// another one depends on (compose's depends_on label, "db:service_started:false,cache:…") comes
// first. Ties and cycles fall back to the container name, so the order is always the same.
func dependencyOrder(cs []apiContainerSummary) []apiContainerSummary {
	byService := map[string][]int{}
	for i, c := range cs {
		if s := c.Labels[labelComposeService]; s != "" {
			byService[s] = append(byService[s], i)
		}
	}
	// need[i] = the indexes container i waits for
	need := make([]map[int]bool, len(cs))
	for i, c := range cs {
		need[i] = map[int]bool{}
		for _, dep := range strings.Split(c.Labels[labelComposeDependsOn], ",") {
			name, _, _ := strings.Cut(strings.TrimSpace(dep), ":")
			for _, j := range byService[name] {
				if j != i {
					need[i][j] = true
				}
			}
		}
	}
	done := make([]bool, len(cs))
	var out []apiContainerSummary
	for len(out) < len(cs) {
		picked := -1
		for i := range cs { // cs is sorted by name: the first ready one is the alphabetical one
			if done[i] {
				continue
			}
			ready := true
			for j := range need[i] {
				if !done[j] {
					ready = false
					break
				}
			}
			if ready {
				picked = i
				break
			}
		}
		if picked < 0 { // a cycle: take the first one left, the order is still reproducible
			for i := range cs {
				if !done[i] {
					picked = i
					break
				}
			}
		}
		done[picked] = true
		out = append(out, cs[picked])
	}
	return out
}

func checkProject(p string) error {
	if !projectName.MatchString(p) {
		return inputErr("project must be a compose project name (letters, digits, _ . -)")
	}
	return nil
}

func composeLogs(o Options) capability.Capability {
	return fn{
		name: "compose.logs",
		desc: "Logs of a whole compose project (stack): the last lines of every container in it, merged by time with the service in front of each line, newest last. Use it to see what happened across the services of one stack, e.g. which one failed first. Optional filters: one service, a text the line must contain, a time range. Secrets are masked; long output keeps the newest part.",
		schema: `{"type":"object","additionalProperties":false,"required":["project"],"properties":{` +
			`"project":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.\\-]{0,127}$","description":"compose project name (compose.projects lists them)"},` +
			`"service":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.\\-]{0,127}$","description":"only this service of the project"},` +
			`"contains":{"type":"string","maxLength":100,"description":"only lines that contain this text (case-insensitive), e.g. error"},` +
			`"lines":{"type":"integer","minimum":1,"maximum":200,"description":"last N lines of each container (default 50)"},` +
			`"since":{"type":"string","maxLength":40,"description":"how far back: a duration (15m, 2h, 3d), today, a date or an RFC 3339 time"},` +
			`"until":{"type":"string","maxLength":40,"description":"as since; end of the range (default now)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Project  string `json:"project"`
				Service  string `json:"service"`
				Contains string `json:"contains"`
				Lines    int    `json:"lines"`
				Since    string `json:"since"`
				Until    string `json:"until"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkProject(in.Project); err != nil {
				return nil, err
			}
			if in.Service != "" {
				if err := checkProject(in.Service); err != nil {
					return nil, inputErr("service must be a compose service name (letters, digits, _ . -)")
				}
			}
			lines := in.Lines
			if lines == 0 {
				lines = defaultStackLogLines
			}
			if lines < 0 || lines > maxStackLogLines {
				return nil, inputErr("lines must be 1..%d", maxStackLogLines)
			}
			now := o.now()
			since, hasSince, err := parseWhen(in.Since, now)
			if err != nil {
				return nil, err
			}
			until, hasUntil, err := parseWhen(in.Until, now)
			if err != nil {
				return nil, err
			}
			if hasSince && hasUntil && !until.After(since) {
				return nil, inputErr("until must be after since")
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			members, err := stackMembers(ctx, d, in.Project)
			if err != nil {
				return nil, err
			}
			if in.Service != "" {
				keep := members[:0:0]
				for _, c := range members {
					if c.Labels[labelComposeService] == in.Service {
						keep = append(keep, c)
					}
				}
				members = keep
			}
			if len(members) == 0 {
				return nil, inputErr("no container of compose project %q here (compose.projects lists the projects)", in.Project)
			}
			truncatedStack := len(members) > maxStackContainers
			if truncatedStack {
				members = members[:maxStackContainers]
			}
			type line struct {
				at      string
				service string
				text    string
			}
			perContainer := make([][]line, len(members))
			errs := make([]string, len(members))
			needle := strings.ToLower(in.Contains)
			parallel(len(members), 4, func(i int) {
				c := members[i]
				ins, err := inspectContainer(ctx, d, c.ID) // also tells whether the stream is framed
				if err != nil {
					errs[i] = err.Error()
					return
				}
				q := url.Values{"stdout": {"1"}, "stderr": {"1"}, "timestamps": {"1"}, "tail": {strconv.Itoa(lines)}}
				if hasSince {
					q.Set("since", strconv.FormatInt(since.Unix(), 10))
				}
				if hasUntil {
					q.Set("until", strconv.FormatInt(until.Unix(), 10))
				}
				body, _, err := d.Get(ctx, "/containers/"+c.ID+"/logs", q)
				if err != nil {
					errs[i] = err.Error()
					return
				}
				svc := c.Labels[labelComposeService]
				if svc == "" {
					svc = trimName(c.Names)
				}
				text := strings.ReplaceAll(demuxLogs(body, ins.Config.Tty), "\r\n", "\n")
				for _, l := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
					if l == "" {
						continue
					}
					at, rest, _ := strings.Cut(l, " ")
					if needle != "" && !strings.Contains(strings.ToLower(rest), needle) {
						continue
					}
					perContainer[i] = append(perContainer[i], line{at: at, service: svc, text: rest})
				}
			})
			var all []line
			var failed []map[string]string
			for i, ls := range perContainer {
				if errs[i] != "" {
					failed = append(failed, map[string]string{"container": trimName(members[i].Names), "error": errs[i]})
				}
				all = append(all, ls...)
			}
			// RFC 3339 timestamps of one zone sort as text; the service breaks ties
			sort.SliceStable(all, func(i, j int) bool {
				if all[i].at != all[j].at {
					return all[i].at < all[j].at
				}
				return all[i].service < all[j].service
			})
			var b strings.Builder
			for _, l := range all {
				b.WriteString(l.at + " [" + l.service + "] " + l.text + "\n")
			}
			head := map[string]any{"project": in.Project, "containers": len(members), "lines_asked": lines}
			if truncatedStack {
				head["stack_truncated"] = true
			}
			if len(failed) > 0 {
				head["unreadable"] = failed
			}
			return logOutput(head, b.String(), false), nil
		},
	}
}

func composeRestart(o Options) capability.Capability {
	return writeFn{fn{
		name: "compose.restart",
		desc: "Restart a whole compose project (stack): every container of it that is running is restarted, one after the other, the ones others depend on first (depends_on). Stopped containers stay stopped. The stack's services are unreachable while they restart. Only projects whose running containers are ALL on the node's restart list may be restarted; otherwise nothing is touched.",
		schema: `{"type":"object","additionalProperties":false,"required":["project"],"properties":{` +
			`"project":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.\\-]{0,127}$","description":"compose project name"},` +
			`"timeout_seconds":{"type":"integer","minimum":1,"maximum":60,"description":"seconds each container gets to stop before it is killed (default 10)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Project        string `json:"project"`
				TimeoutSeconds int    `json:"timeout_seconds"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkProject(in.Project); err != nil {
				return nil, err
			}
			if in.TimeoutSeconds < 0 || in.TimeoutSeconds > 60 {
				return nil, inputErr("timeout_seconds must be 1..60")
			}
			if o.DockerWrite == nil {
				return nil, errNoDockerWrite
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			members, err := stackMembers(ctx, d, in.Project)
			if err != nil {
				return nil, err
			}
			if len(members) == 0 {
				return nil, inputErr("no container of compose project %q here (compose.projects lists the projects)", in.Project)
			}
			// restart only what runs: "docker restart" would also START a stopped container, and a
			// stack with a service the operator stopped on purpose must keep it stopped
			var running []apiContainerSummary
			var left []string
			for _, c := range members {
				if c.State == "running" || c.State == "restarting" {
					running = append(running, c)
				} else {
					left = append(left, trimName(c.Names)+" ("+c.State+")")
				}
			}
			if len(running) == 0 {
				return nil, inputErr("no container of project %q is running: this capability restarts, it does not start", in.Project)
			}
			if len(running) > maxStackContainers {
				return nil, inputErr("project %q has %d running containers: more than %d are not restarted in one step", in.Project, len(running), maxStackContainers)
			}
			// all or nothing at the gate: one container off the list refuses the whole stack
			var off []string
			for _, c := range running {
				if !allowedByList(trimName(c.Names), o.RestartContainers) {
					off = append(off, trimName(c.Names))
				}
			}
			if len(off) > 0 {
				return nil, inputErr("containers %s of project %q are not on this node's list of containers that may be restarted: nothing was restarted", strings.Join(off, ", "), in.Project)
			}
			t := in.TimeoutSeconds
			if t == 0 {
				t = 10
			}
			ordered := dependencyOrder(running)
			var done []map[string]any
			for _, c := range ordered {
				name := trimName(c.Names)
				started := time.Now()
				if err := o.DockerWrite.Restart(ctx, name, t); err != nil {
					// a half-restarted stack is a state the person must know about: name both parts
					return nil, fmt.Errorf("restarted %s; then restarting %s failed: %w; the rest of the stack was not touched", nameList(done), name, err)
				}
				done = append(done, map[string]any{"container": name, "service": c.Labels[labelComposeService], "restarted": true, "took_ms": time.Since(started).Milliseconds()})
			}
			// the state afterwards, read through the read-only path
			for _, r := range done {
				if ins, err := inspectContainer(ctx, d, r["container"].(string)); err == nil {
					r["state"], r["running"], r["restart_count"] = ins.State.Status, ins.State.Running, ins.RestartCount
					if ins.State.Health != nil {
						r["health"] = ins.State.Health.Status
					}
				}
			}
			out := map[string]any{"project": in.Project, "restarted": true, "containers": done}
			if len(left) > 0 {
				out["left_stopped"] = left
			}
			return out, nil
		},
	}}
}

func nameList(done []map[string]any) string {
	if len(done) == 0 {
		return "nothing"
	}
	names := make([]string, len(done))
	for i, r := range done {
		names[i] = r["container"].(string)
	}
	return strings.Join(names, ", ")
}
