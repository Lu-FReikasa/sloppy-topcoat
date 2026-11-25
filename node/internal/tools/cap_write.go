package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// The state-changing capabilities of E3-01b. They differ from every other capability in three ways:
//   - they are NOT read-only, so capability.invoke never runs them (D15); only the separate
//     capability.execute action does, and the hub sends that only after the user confirmed that
//     single proposal;
//   - they exist only where the node's config names what may be restarted (default deny), so a
//     node without a list does not offer them at all;
//   - what they do is as small as possible: restart one container, restart one unit.

// writeFn is a capability that changes state.
type writeFn struct{ fn }

func (f writeFn) Info() capability.Info { return capability.Info{Description: f.desc, ReadOnly: false} }

// allowedByList reports whether name matches one of the glob patterns (path.Match: * ? [...]).
func allowedByList(name string, patterns []string) bool {
	for _, p := range patterns {
		if ok, err := path.Match(p, name); err == nil && ok {
			return true
		}
	}
	return false
}

func dockerRestart(o Options) capability.Capability {
	return writeFn{fn{
		name: "docker.restart",
		desc: "Restart one container (docker restart): it is stopped and started again, so its service is unreachable for a few seconds. Only containers the node's config lists may be restarted.",
		schema: `{"type":"object","additionalProperties":false,"required":["container"],"properties":{` +
			`"container":{"type":"string","maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9_.\\-]{0,127}$","description":"container name"},` +
			`"timeout_seconds":{"type":"integer","minimum":1,"maximum":60,"description":"seconds the container gets to stop before it is killed (default 10)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Container      string `json:"container"`
				TimeoutSeconds int    `json:"timeout_seconds"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkContainer(in.Container); err != nil {
				return nil, err
			}
			if in.TimeoutSeconds < 0 || in.TimeoutSeconds > 60 {
				return nil, inputErr("timeout_seconds must be 1..60")
			}
			if o.DockerWrite == nil {
				return nil, errNoDockerWrite
			}
			if !allowedByList(in.Container, o.RestartContainers) {
				return nil, inputErr("container %s is not on this node's list of containers that may be restarted", in.Container)
			}
			t := in.TimeoutSeconds
			if t == 0 {
				t = 10
			}
			if err := o.DockerWrite.Restart(ctx, in.Container, t); err != nil {
				return nil, err
			}
			out := map[string]any{"container": in.Container, "restarted": true}
			// the state afterwards, read through the read-only path, so the proposal's result says
			// what is running now and not only that the call returned
			if d, err := o.docker(); err == nil {
				if ins, err := inspectContainer(ctx, d, in.Container); err == nil {
					out["state"] = ins.State.Status
					out["running"] = ins.State.Running
					out["restart_count"] = ins.RestartCount
				}
			}
			return out, nil
		},
	}}
}

func serviceRestart(o Options) capability.Capability {
	return writeFn{fn{
		name: "service.restart",
		desc: "Restart one systemd unit (systemctl restart): the service is unreachable while it restarts. Only units the node's config lists may be restarted.",
		schema: `{"type":"object","additionalProperties":false,"required":["unit"],"properties":{` +
			`"unit":{"type":"string","maxLength":256,"pattern":"^[A-Za-z0-9:_.@\\\\][A-Za-z0-9:_.@\\\\-]*$","description":"systemd unit, e.g. nginx.service"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Unit string `json:"unit"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkUnit(in.Unit); err != nil {
				return nil, err
			}
			if !allowedByList(in.Unit, o.RestartUnits) {
				return nil, inputErr("unit %s is not on this node's list of units that may be restarted", in.Unit)
			}
			if _, err := o.Commander.Run(ctx, "systemctl", "restart", "--no-pager", in.Unit); err != nil {
				return nil, err
			}
			out := map[string]any{"unit": in.Unit, "restarted": true}
			if st, err := o.Commander.Run(ctx, "systemctl", "show", "--no-pager", "--property=ActiveState,SubState,Result", in.Unit); err == nil {
				for _, l := range strings.Split(string(st), "\n") {
					if k, v, ok := strings.Cut(l, "="); ok && v != "" {
						out[strings.ToLower(k)] = v
					}
				}
			}
			return out, nil
		},
	}}
}

var errNoDockerWrite = fmt.Errorf("no Docker write proxy is configured on this node (config.tools.docker_write_host)")
