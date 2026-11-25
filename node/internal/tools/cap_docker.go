package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"sort"
	"strconv"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// Docker API shapes, only the fields the capabilities return.
type apiContainerSummary struct {
	ID      string            `json:"Id"`
	Names   []string          `json:"Names"`
	Image   string            `json:"Image"`
	State   string            `json:"State"`
	Status  string            `json:"Status"`
	Created int64             `json:"Created"`
	Labels  map[string]string `json:"Labels"`
	Ports   []struct {
		IP          string `json:"IP"`
		PrivatePort int    `json:"PrivatePort"`
		PublicPort  int    `json:"PublicPort"`
		Type        string `json:"Type"`
	} `json:"Ports"`
}

type apiInspect struct {
	ID      string `json:"Id"`
	Name    string `json:"Name"`
	Created string `json:"Created"`
	Path    string `json:"Path"`
	Args    []string
	Image   string
	Config  struct {
		Image  string
		Tty    bool
		Labels map[string]string
	}
	State struct {
		Status     string
		Running    bool
		Paused     bool
		Restarting bool
		OOMKilled  bool
		Dead       bool
		ExitCode   int
		Error      string
		StartedAt  string
		FinishedAt string
		Health     *struct {
			Status        string
			FailingStreak int
			Log           []struct {
				Start    string
				End      string
				ExitCode int
				Output   string
			}
		}
	}
	RestartCount int
	HostConfig   struct {
		RestartPolicy struct {
			Name              string
			MaximumRetryCount int
		}
	}
	Mounts []struct {
		Type        string
		Source      string
		Destination string
		RW          bool
	}
	NetworkSettings struct {
		Networks map[string]json.RawMessage
	}
}

const (
	labelComposeProject = "com.docker.compose.project"
	labelComposeService = "com.docker.compose.service"
	labelComposeWorkdir = "com.docker.compose.project.working_dir"
	labelComposeFiles   = "com.docker.compose.project.config_files"
	maxContainers       = 200
)

func (o Options) docker() (DockerAPI, error) {
	if o.Docker == nil {
		return nil, errNoDocker
	}
	return o.Docker, nil
}

func listContainers(ctx context.Context, d DockerAPI, all bool) ([]apiContainerSummary, error) {
	q := url.Values{}
	if all {
		q.Set("all", "1")
	}
	body, _, err := d.Get(ctx, "/containers/json", q)
	if err != nil {
		return nil, err
	}
	var list []apiContainerSummary
	if err := json.Unmarshal(body, &list); err != nil {
		return nil, fmt.Errorf("the Docker API answered something unexpected: %w", err)
	}
	return list, nil
}

func inspectContainer(ctx context.Context, d DockerAPI, ref string) (apiInspect, error) {
	var in apiInspect
	body, _, err := d.Get(ctx, "/containers/"+ref+"/json", nil)
	if err != nil {
		return in, err
	}
	if err := json.Unmarshal(body, &in); err != nil {
		return in, fmt.Errorf("the Docker API answered something unexpected: %w", err)
	}
	return in, nil
}

func short(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func trimName(names []string) string {
	if len(names) == 0 {
		return ""
	}
	return strings.TrimPrefix(names[0], "/")
}

type containerInfo struct {
	ID             string   `json:"id"`
	Name           string   `json:"name"`
	Image          string   `json:"image"`
	State          string   `json:"state"`
	Status         string   `json:"status"`
	Health         string   `json:"health,omitempty"`
	RestartCount   int      `json:"restart_count"`
	ExitCode       *int     `json:"exit_code,omitempty"`
	OOMKilled      bool     `json:"oom_killed,omitempty"`
	StartedAt      string   `json:"started_at,omitempty"`
	FinishedAt     string   `json:"finished_at,omitempty"`
	ComposeProject string   `json:"compose_project,omitempty"`
	ComposeService string   `json:"compose_service,omitempty"`
	Ports          []string `json:"ports,omitempty"`
	InspectError   string   `json:"inspect_error,omitempty"`
}

// containerBrief is one line of the compact docker.ps: what an agent needs to see which
// containers are wrong. 21 containers are about 2 KB instead of 7 KB, which matters when the
// model's whole context is 16K tokens.
type containerBrief struct {
	Name         string `json:"name"`
	State        string `json:"state"`
	Health       string `json:"health,omitempty"`
	RestartCount int    `json:"restart_count"`
	StartedAt    string `json:"started_at,omitempty"`
	// only where it says something: a container that is not running, or was killed for memory
	ExitCode     *int   `json:"exit_code,omitempty"`
	OOMKilled    bool   `json:"oom_killed,omitempty"`
	InspectError string `json:"inspect_error,omitempty"`
}

func (c containerInfo) brief() containerBrief {
	return containerBrief{Name: c.Name, State: c.State, Health: c.Health, RestartCount: c.RestartCount,
		StartedAt: c.StartedAt, ExitCode: c.ExitCode, OOMKilled: c.OOMKilled, InspectError: c.InspectError}
}

func dockerPS(o Options) capability.Capability {
	return fn{
		name: "docker.ps",
		desc: "Containers of this Docker host (Docker API, read-only). By default a compact list: name, state, health, restart count, start time (plus exit code and OOM kill for containers that are not running) and counts by state. Ask with detail=true for image, ports, compose project and service, status text and finish time of each; docker.inspect shows everything about one container.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"all":{"type":"boolean","description":"include stopped containers (default true)"},` +
			`"name":{"type":"string","maxLength":128,"description":"only containers whose name contains this text"},` +
			`"detail":{"type":"boolean","description":"full details of each container (default false: the compact list)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				All    *bool  `json:"all"`
				Name   string `json:"name"`
				Detail bool   `json:"detail"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			out, total, truncated, err := collectContainers(ctx, d, in.All == nil || *in.All, in.Name)
			if err != nil {
				return nil, err
			}
			counts := map[string]int{}
			for _, c := range out {
				counts[c.State]++
			}
			res := map[string]any{"total": total, "truncated": truncated, "counts": counts}
			if in.Detail {
				res["containers"] = out
				return res, nil
			}
			brief := make([]containerBrief, len(out))
			for i, c := range out {
				brief[i] = c.brief()
			}
			res["containers"] = brief
			res["compact"] = true // detail=true (or docker.inspect) gives image, ports, compose, status text
			return res, nil
		},
	}
}

// collectContainers lists the containers (name filter: contains, case-insensitive), at most
// maxContainers, each with its inspect data. total is the count before the cut.
func collectContainers(ctx context.Context, d DockerAPI, all bool, name string) (out []containerInfo, total int, truncated bool, err error) {
	list, err := listContainers(ctx, d, all)
	if err != nil {
		return nil, 0, false, err
	}
	needle := strings.ToLower(name)
	var keep []apiContainerSummary
	for _, c := range list {
		if needle == "" || strings.Contains(strings.ToLower(trimName(c.Names)), needle) {
			keep = append(keep, c)
		}
	}
	sort.Slice(keep, func(i, j int) bool { return trimName(keep[i].Names) < trimName(keep[j].Names) })
	total = len(keep)
	if len(keep) > maxContainers {
		keep, truncated = keep[:maxContainers], true
	}
	out = make([]containerInfo, len(keep))
	parallel(len(keep), 8, func(i int) {
		c := keep[i]
		ci := containerInfo{ID: short(c.ID), Name: trimName(c.Names), Image: c.Image, State: c.State, Status: c.Status,
			ComposeProject: c.Labels[labelComposeProject], ComposeService: c.Labels[labelComposeService]}
		seen := map[string]bool{}
		for _, p := range c.Ports {
			s := fmt.Sprintf("%d/%s", p.PrivatePort, p.Type)
			if p.PublicPort != 0 {
				s = fmt.Sprintf("%d->%s", p.PublicPort, s)
			}
			if !seen[s] {
				seen[s] = true
				ci.Ports = append(ci.Ports, s)
			}
		}
		sort.Strings(ci.Ports)
		ins, err := inspectContainer(ctx, d, c.ID)
		if err != nil {
			ci.InspectError = err.Error()
		} else {
			ci.RestartCount = ins.RestartCount
			ci.OOMKilled = ins.State.OOMKilled
			ci.StartedAt, ci.FinishedAt = zeroTime(ins.State.StartedAt), zeroTime(ins.State.FinishedAt)
			if !ins.State.Running {
				code := ins.State.ExitCode
				ci.ExitCode = &code
			}
			if ins.State.Health != nil {
				ci.Health = ins.State.Health.Status
			}
		}
		out[i] = ci
	})
	return out, total, truncated, nil
}

// zeroTime hides Docker's "never" timestamp.
func zeroTime(s string) string {
	if strings.HasPrefix(s, "0001-01-01") {
		return ""
	}
	return s
}

func dockerInspect(o Options) capability.Capability {
	return fn{
		name: "docker.inspect",
		desc: "Status details of one container: state, exit code, OOM kill, restart policy and count, health checks with their last outputs, mounts, networks. Environment variables are never returned.",
		schema: `{"type":"object","additionalProperties":false,"required":["container"],"properties":{` +
			`"container":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.-]*$","description":"container name or ID"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Container string `json:"container"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkContainer(in.Container); err != nil {
				return nil, err
			}
			d, err := o.docker()
			if err != nil {
				return nil, err
			}
			ins, err := inspectContainer(ctx, d, in.Container)
			if err != nil {
				return nil, err
			}
			masked := 0
			maskText := func(s string) string {
				out, n := Mask(s)
				masked += n
				return out
			}
			var mounts []map[string]any
			for _, m := range ins.Mounts {
				mounts = append(mounts, map[string]any{"type": m.Type, "source": m.Source, "destination": m.Destination, "rw": m.RW})
			}
			var nets []string
			for n := range ins.NetworkSettings.Networks {
				nets = append(nets, n)
			}
			sort.Strings(nets)
			cmd, _ := headBytes(maskText(strings.TrimSpace(ins.Path+" "+strings.Join(ins.Args, " "))), 1000)
			out := map[string]any{
				"id": short(ins.ID), "name": strings.TrimPrefix(ins.Name, "/"), "image": ins.Config.Image, "created": ins.Created,
				"command": cmd,
				"state": map[string]any{
					"status": ins.State.Status, "running": ins.State.Running, "paused": ins.State.Paused,
					"restarting": ins.State.Restarting, "oom_killed": ins.State.OOMKilled, "dead": ins.State.Dead,
					"exit_code": ins.State.ExitCode, "error": maskText(ins.State.Error),
					"started_at": zeroTime(ins.State.StartedAt), "finished_at": zeroTime(ins.State.FinishedAt),
				},
				"restart_count":  ins.RestartCount,
				"restart_policy": ins.HostConfig.RestartPolicy.Name,
				"mounts":         mounts,
				"networks":       nets,
			}
			if p := ins.Config.Labels[labelComposeProject]; p != "" {
				out["compose_project"], out["compose_service"] = p, ins.Config.Labels[labelComposeService]
			}
			if h := ins.State.Health; h != nil {
				var last []map[string]any
				logs := h.Log
				if len(logs) > 3 {
					logs = logs[len(logs)-3:]
				}
				for _, l := range logs {
					o, _ := headBytes(maskText(strings.TrimSpace(l.Output)), 500)
					last = append(last, map[string]any{"end": l.End, "exit_code": l.ExitCode, "output": o})
				}
				out["health"] = map[string]any{"status": h.Status, "failing_streak": h.FailingStreak, "last_checks": last}
			}
			out["masked"] = masked
			return out, nil
		},
	}
}

func dockerLogs(o Options) capability.Capability {
	return fn{
		name: "docker.logs",
		desc: "Log lines of one container (stdout and stderr, with timestamps), newest last: the last N lines (at most 500) within an optional time range. Secrets are masked; long output keeps the newest part.",
		schema: `{"type":"object","additionalProperties":false,"required":["container"],"properties":{` +
			`"container":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.-]*$","description":"container name or ID"},` +
			`"lines":{"type":"integer","minimum":1,"maximum":500,"description":"last N lines (default 100)"},` +
			`"since":{"type":"string","maxLength":40,"description":"how far back: a duration (15m, 2h, 3d, 1 hour ago), today, yesterday, a date or date and time (UTC unless it names a zone) or an RFC 3339 time"},` +
			`"until":{"type":"string","maxLength":40,"description":"as since; end of the range (default now)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Container string `json:"container"`
				Lines     int    `json:"lines"`
				Since     string `json:"since"`
				Until     string `json:"until"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkContainer(in.Container); err != nil {
				return nil, err
			}
			lines, err := clampLines(in.Lines)
			if err != nil {
				return nil, err
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
			ins, err := inspectContainer(ctx, d, in.Container) // also tells whether the stream is framed
			if err != nil {
				return nil, err
			}
			q := url.Values{"stdout": {"1"}, "stderr": {"1"}, "timestamps": {"1"}, "tail": {strconv.Itoa(lines)}}
			if hasSince {
				q.Set("since", strconv.FormatInt(since.Unix(), 10))
			}
			if hasUntil {
				q.Set("until", strconv.FormatInt(until.Unix(), 10))
			}
			body, over, err := d.Get(ctx, "/containers/"+in.Container+"/logs", q)
			if err != nil {
				return nil, err
			}
			text := demuxLogs(body, ins.Config.Tty)
			return logOutput(map[string]any{"container": strings.TrimPrefix(ins.Name, "/"), "lines_asked": lines}, text, over), nil
		},
	}
}

// logOutput masks a log, keeps its newest DefaultMaxOutputBytes and counts its lines.
func logOutput(head map[string]any, text string, sourceTruncated bool) map[string]any {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	masked, n := Mask(text)
	masked = strings.TrimRight(masked, "\n")
	out, cut := tailBytes(masked, DefaultMaxOutputBytes)
	head["log"] = out
	head["lines"] = 0
	if masked != "" {
		head["lines"] = strings.Count(masked, "\n") + 1
	}
	head["truncated"] = cut || sourceTruncated
	head["masked"] = n
	return head
}
