package tools

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// serviceProps are the properties `systemctl show` is asked for: the state of a unit and why it
// is in it, nothing that holds an environment or a command line with secrets.
var serviceProps = []string{
	"Id", "Description", "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
	"MainPID", "ExecMainStatus", "ExecMainCode", "NRestarts", "MemoryCurrent",
	"ActiveEnterTimestamp", "InactiveEnterTimestamp", "FragmentPath",
}

func journalLogs(o Options) capability.Capability {
	return fn{
		name: "journal.logs",
		desc: "Journal lines of one systemd unit (journalctl -u), newest last: the last N lines (at most 500) within an optional time range and from a minimum priority. Secrets are masked.",
		schema: `{"type":"object","additionalProperties":false,"required":["unit"],"properties":{` +
			`"unit":{"type":"string","maxLength":256,"pattern":"^[A-Za-z0-9:_.@\\\\][A-Za-z0-9:_.@\\\\-]*$","description":"systemd unit, e.g. docker.service"},` +
			`"lines":{"type":"integer","minimum":1,"maximum":500,"description":"last N lines (default 100)"},` +
			`"since":{"type":"string","maxLength":40,"description":"how far back: a duration (15m, 2h, 3d, 1 hour ago), today, yesterday, a date or date and time (UTC unless it names a zone) or an RFC 3339 time"},` +
			`"until":{"type":"string","maxLength":40,"description":"as since; end of the range (default now)"},` +
			`"priority":{"type":"integer","minimum":0,"maximum":7,"description":"only this priority or more severe (0 emerg … 3 err … 6 info, 7 debug)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Unit     string `json:"unit"`
				Lines    int    `json:"lines"`
				Since    string `json:"since"`
				Until    string `json:"until"`
				Priority *int   `json:"priority"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if err := checkUnit(in.Unit); err != nil {
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
			if in.Priority != nil && (*in.Priority < 0 || *in.Priority > 7) {
				return nil, inputErr("priority must be 0..7")
			}
			args := []string{"--no-pager", "-q", "-o", "short-iso", "-u", in.Unit, "-n", strconv.Itoa(lines)}
			const layout = "2006-01-02 15:04:05 UTC" // systemd.time accepts a time zone suffix
			if hasSince {
				args = append(args, "--since", since.UTC().Format(layout))
			}
			if hasUntil {
				args = append(args, "--until", until.UTC().Format(layout))
			}
			if in.Priority != nil {
				args = append(args, "-p", strconv.Itoa(*in.Priority))
			}
			out, err := o.Commander.Run(ctx, "journalctl", args...)
			if err != nil {
				return nil, err
			}
			return logOutput(map[string]any{"unit": in.Unit, "lines_asked": lines}, string(out), false), nil
		},
	}
}

func serviceStatus(o Options) capability.Capability {
	return fn{
		name: "service.status",
		desc: "State of one systemd unit (systemctl show): load/active/sub state, result of the last run, main PID and exit status, restart count, memory, since when.",
		schema: `{"type":"object","additionalProperties":false,"required":["unit"],"properties":{` +
			`"unit":{"type":"string","maxLength":256,"pattern":"^[A-Za-z0-9:_.@\\\\][A-Za-z0-9:_.@\\\\-]*$","description":"systemd unit, e.g. docker.service"}}}`,
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
			out, err := o.Commander.Run(ctx, "systemctl", "show", "--no-pager", "--property="+strings.Join(serviceProps, ","), in.Unit)
			if err != nil {
				return nil, err
			}
			props := map[string]string{}
			for _, l := range strings.Split(string(out), "\n") {
				if k, v, ok := strings.Cut(l, "="); ok && v != "" && v != "[not set]" {
					props[k] = v
				}
			}
			// `systemctl show` answers for a unit that does not exist too (ActiveState=inactive,
			// exit status 0); only LoadState tells the difference.
			if props["LoadState"] == "not-found" {
				return nil, inputErr("unit %s not found (LoadState=not-found): no such systemd unit on this host", in.Unit)
			}
			return map[string]any{"unit": in.Unit, "properties": props}, nil
		},
	}
}

func serviceFailed(o Options) capability.Capability {
	return fn{
		name:   "service.failed",
		desc:   "The systemd units that are in the failed state right now (systemctl list-units --failed).",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			units, err := failedUnits(ctx, o)
			if err != nil {
				return nil, err
			}
			return map[string]any{"failed": units, "count": len(units)}, nil
		},
	}
}

// failedUnits lists the units in the failed state.
func failedUnits(ctx context.Context, o Options) ([]map[string]string, error) {
	out, err := o.Commander.Run(ctx, "systemctl", "list-units", "--failed", "--no-legend", "--plain", "--no-pager")
	if err != nil {
		return nil, err
	}
	units := []map[string]string{}
	for _, l := range strings.Split(string(out), "\n") {
		f := strings.Fields(l)
		if len(f) < 4 {
			continue
		}
		units = append(units, map[string]string{"unit": f[0], "load": f[1], "active": f[2], "sub": f[3], "description": strings.Join(f[4:], " ")})
	}
	return units, nil
}
