package tools

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// inventory.snapshot: what runs on this machine, in one call, for the hub's inventory. It reads
// the same sources as docker.ps and the systemd capabilities (the Docker API, `systemctl`, /proc)
// and adds nothing that changes state. Every section answers on its own — `ok`, or `code` and
// `error` — so the hub can tell "nothing runs" from "could not look": a section that failed is
// never read as an empty one.

const (
	maxInventoryServices = 400
	// section codes: the section is not offered here (no Docker configured, not Linux), or it failed
	sectionNotConfigured = "not_configured"
	sectionFailed        = "error"
)

// invContainer is the part of docker.ps the inventory keeps (no status text, no log, no env).
type invContainer struct {
	Name           string   `json:"name"`
	Image          string   `json:"image"`
	State          string   `json:"state"`
	Health         string   `json:"health,omitempty"`
	RestartCount   int      `json:"restart_count"`
	StartedAt      string   `json:"started_at,omitempty"`
	ComposeProject string   `json:"compose_project,omitempty"`
	ComposeService string   `json:"compose_service,omitempty"`
	Ports          []string `json:"ports,omitempty"`
}

type invService struct {
	Unit        string `json:"unit"`
	Active      string `json:"active"`
	Sub         string `json:"sub"`
	Enabled     string `json:"enabled,omitempty"` // UnitFileState: enabled, disabled, static, ...
	Description string `json:"description,omitempty"`
}

// section is the answer of one source: data, or why there is none.
type section struct {
	OK        bool   `json:"ok"`
	Code      string `json:"code,omitempty"`
	Error     string `json:"error,omitempty"`
	Data      any    `json:"data,omitempty"`
	Total     int    `json:"total,omitempty"`
	Truncated bool   `json:"truncated,omitempty"`
}

func failed(err error) section {
	code := sectionFailed
	if errors.Is(err, errNoDocker) {
		code = sectionNotConfigured
	}
	msg, _ := Mask(err.Error())
	return section{Code: code, Error: truncateRunes(oneLine(msg), 300)}
}

func inventorySnapshot(o Options) capability.Capability {
	return fn{
		name:   "inventory.snapshot",
		desc:   "One look at what runs on this machine, for the hub's inventory: host facts (name, OS, kernel, uptime), every Docker container (image, state, health, compose project, ports) and the systemd services that are active, failed or enabled. Each section says on its own whether it could be read; a section that failed is not an empty one.",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			out := map[string]any{
				"taken_at":   o.now().UTC().Format("2006-01-02T15:04:05Z"),
				"host":       inventoryHost(o),
				"containers": inventoryContainers(ctx, o),
				"services":   inventoryServices(ctx, o),
			}
			return out, nil
		},
	}
}

func inventoryHost(o Options) section {
	if runtime.GOOS != "linux" && o.ProcRoot == "" {
		return section{Code: sectionNotConfigured, Error: "host facts are read from /proc and are only available on Linux nodes"}
	}
	host, _ := os.Hostname()
	data := map[string]any{"hostname": host, "arch": runtime.GOARCH, "cpus": runtime.NumCPU()}
	if t, err := readText(o.etc() + "/os-release"); err == nil {
		data["os"] = osPrettyName(t)
	}
	if t, err := readText(o.proc() + "/sys/kernel/osrelease"); err == nil {
		data["kernel"] = strings.TrimSpace(t)
	}
	if t, err := readText(o.proc() + "/uptime"); err == nil {
		if f := strings.Fields(t); len(f) > 0 {
			if s, err := strconv.ParseFloat(f[0], 64); err == nil {
				data["uptime_seconds"] = int64(s)
			}
		}
	}
	if t, err := readText(o.proc() + "/meminfo"); err == nil {
		data["memory_total_mb"] = meminfoMB(t)["MemTotal"]
	}
	return section{OK: true, Data: data}
}

func inventoryContainers(ctx context.Context, o Options) section {
	d, err := o.docker()
	if err != nil {
		return failed(err)
	}
	list, total, truncated, err := collectContainers(ctx, d, true, "")
	if err != nil {
		return failed(err)
	}
	rows := make([]invContainer, 0, len(list))
	for _, c := range list {
		image, _ := Mask(c.Image)
		rows = append(rows, invContainer{Name: c.Name, Image: image, State: c.State, Health: c.Health,
			RestartCount: c.RestartCount, StartedAt: c.StartedAt,
			ComposeProject: c.ComposeProject, ComposeService: c.ComposeService, Ports: c.Ports})
	}
	return section{OK: true, Data: rows, Total: total, Truncated: truncated}
}

// inventoryServices lists the systemd services that matter: active ones, failed ones, and those
// that are enabled (an enabled service that is not running is exactly what an inventory is for).
// Loaded-but-static idle units (the bulk of `--all`) are left out.
func inventoryServices(ctx context.Context, o Options) section {
	out, err := o.Commander.Run(ctx, "systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager")
	if err != nil {
		return failed(err)
	}
	enabled := map[string]string{}
	// the unit file states are a nicety: when they cannot be read the list is still right
	if files, err := o.Commander.Run(ctx, "systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"); err == nil {
		for _, l := range strings.Split(string(files), "\n") {
			if f := strings.Fields(l); len(f) >= 2 {
				enabled[f[0]] = f[1]
			}
		}
	}
	var rows []invService
	for _, l := range strings.Split(string(out), "\n") {
		f := strings.Fields(l)
		if len(f) < 4 || f[1] == "not-found" {
			continue
		}
		s := invService{Unit: f[0], Active: f[2], Sub: f[3], Enabled: enabled[f[0]], Description: truncateRunes(oneLine(strings.Join(f[4:], " ")), 120)}
		if s.Active == "inactive" && s.Enabled != "enabled" {
			continue
		}
		rows = append(rows, s)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].Unit < rows[j].Unit })
	sec := section{OK: true, Total: len(rows)}
	if len(rows) > maxInventoryServices {
		rows, sec.Truncated = rows[:maxInventoryServices], true
	}
	if rows == nil {
		rows = []invService{}
	}
	sec.Data = rows
	return sec
}
