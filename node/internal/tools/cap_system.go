package tools

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// FSStat is the usage of one mounted file system.
type FSStat struct {
	TotalBytes, FreeBytes, AvailBytes uint64
	Inodes, InodesFree                uint64
}

func (o Options) proc() string {
	if o.ProcRoot != "" {
		return o.ProcRoot
	}
	return "/proc"
}

func (o Options) etc() string {
	if o.EtcRoot != "" {
		return o.EtcRoot
	}
	return "/etc"
}

func readText(path string) (string, error) {
	b, err := os.ReadFile(path) //nolint:gosec // fixed files under /proc and /etc
	return string(b), err
}

// meminfoMB reads /proc/meminfo into MB.
func meminfoMB(text string) map[string]int64 {
	out := map[string]int64{}
	sc := bufio.NewScanner(strings.NewReader(text))
	for sc.Scan() {
		k, rest, ok := strings.Cut(sc.Text(), ":")
		if !ok {
			continue
		}
		f := strings.Fields(rest)
		if len(f) == 0 {
			continue
		}
		if n, err := strconv.ParseInt(f[0], 10, 64); err == nil {
			if len(f) > 1 && strings.EqualFold(f[1], "kB") {
				n /= 1024
			}
			out[k] = n
		}
	}
	return out
}

// osPrettyName reads PRETTY_NAME from an os-release file.
func osPrettyName(text string) string {
	for _, l := range strings.Split(text, "\n") {
		if v, ok := strings.CutPrefix(l, "PRETTY_NAME="); ok {
			return strings.Trim(strings.TrimSpace(v), `"'`)
		}
	}
	return ""
}

func systemInfo(o Options) capability.Capability {
	return fn{
		name:   "system.info",
		desc:   "Host name, OS, kernel, uptime, load average, CPU count, RAM and swap of this machine (/proc).",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(_ context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if runtime.GOOS != "linux" && o.ProcRoot == "" {
				return nil, errors.New("system.info reads /proc and is only available on Linux nodes")
			}
			host, _ := os.Hostname()
			out := map[string]any{"hostname": host, "arch": runtime.GOARCH, "cpus": runtime.NumCPU(), "time": o.now().UTC().Format("2006-01-02T15:04:05Z")}
			if t, err := readText(filepath.Join(o.etc(), "os-release")); err == nil {
				out["os"] = osPrettyName(t)
			}
			if t, err := readText(filepath.Join(o.proc(), "sys/kernel/osrelease")); err == nil {
				out["kernel"] = strings.TrimSpace(t)
			}
			if t, err := readText(filepath.Join(o.proc(), "uptime")); err == nil {
				if f := strings.Fields(t); len(f) > 0 {
					if s, err := strconv.ParseFloat(f[0], 64); err == nil {
						out["uptime_seconds"] = int64(s)
					}
				}
			}
			if t, err := readText(filepath.Join(o.proc(), "loadavg")); err == nil {
				if f := strings.Fields(t); len(f) >= 3 {
					l := map[string]float64{}
					for i, k := range []string{"1m", "5m", "15m"} {
						l[k], _ = strconv.ParseFloat(f[i], 64)
					}
					out["load"] = l
				}
			}
			if t, err := readText(filepath.Join(o.proc(), "meminfo")); err == nil {
				m := meminfoMB(t)
				total, avail := m["MemTotal"], m["MemAvailable"]
				out["memory_mb"] = map[string]int64{"total": total, "available": avail, "used": total - avail,
					"swap_total": m["SwapTotal"], "swap_used": m["SwapTotal"] - m["SwapFree"]}
			}
			return out, nil
		},
	}
}

// pseudoFS are file system types that hold no data worth reporting.
var pseudoFS = map[string]bool{
	"proc": true, "sysfs": true, "devtmpfs": true, "devpts": true, "tmpfs": true, "cgroup": true, "cgroup2": true,
	"securityfs": true, "debugfs": true, "tracefs": true, "configfs": true, "fusectl": true, "mqueue": true,
	"hugetlbfs": true, "pstore": true, "bpf": true, "autofs": true, "binfmt_misc": true, "nsfs": true,
	"overlay": true, "squashfs": true, "ramfs": true, "efivarfs": true, "rpc_pipefs": true, "selinuxfs": true,
}

func octalUnescape(s string) string {
	if !strings.Contains(s, `\`) {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+3 < len(s) {
			if n, err := strconv.ParseUint(s[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(n))
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// fsRow is one mounted data file system.
type fsRow struct {
	Mount      string  `json:"mount"`
	Device     string  `json:"device"`
	FSType     string  `json:"fstype"`
	TotalMB    int64   `json:"total_mb"`
	UsedMB     int64   `json:"used_mb"`
	AvailMB    int64   `json:"avail_mb"`
	UsedPct    float64 `json:"used_percent"`
	InodesUsed float64 `json:"inodes_used_percent,omitempty"`
}

// filesystems reads the mount table and the usage of every data file system, fullest first.
func (o Options) filesystems() ([]fsRow, error) {
	stat := o.StatFS
	if stat == nil {
		stat = statFS
	}
	text, err := readText(filepath.Join(o.proc(), "self/mounts"))
	if err != nil {
		return nil, fmt.Errorf("disk.usage reads the mount table and is only available on Linux nodes: %w", err)
	}
	var rows []fsRow
	seen := map[string]bool{}
	for _, l := range strings.Split(text, "\n") {
		f := strings.Fields(l)
		if len(f) < 3 || pseudoFS[f[2]] {
			continue
		}
		dev, mnt := octalUnescape(f[0]), octalUnescape(f[1])
		if seen[dev+"|"+mnt] {
			continue
		}
		seen[dev+"|"+mnt] = true
		st, err := stat(mnt)
		if err != nil || st.TotalBytes == 0 {
			continue
		}
		used := st.TotalBytes - st.FreeBytes
		r := fsRow{Mount: mnt, Device: dev, FSType: f[2], TotalMB: int64(st.TotalBytes >> 20), UsedMB: int64(used >> 20), AvailMB: int64(st.AvailBytes >> 20)}
		if d := used + st.AvailBytes; d > 0 {
			r.UsedPct = float64(int(float64(used)/float64(d)*1000)) / 10
		}
		if st.Inodes > 0 {
			r.InodesUsed = float64(int(float64(st.Inodes-st.InodesFree)/float64(st.Inodes)*1000)) / 10
		}
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].UsedPct > rows[j].UsedPct })
	return rows, nil
}

func diskUsage(o Options) capability.Capability {
	return fn{
		name:   "disk.usage",
		desc:   "Usage of every mounted data file system (size, used, available, inodes), fullest first. Overlay, tmpfs and pseudo file systems are left out.",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(_ context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			rows, err := o.filesystems()
			if err != nil {
				return nil, err
			}
			return map[string]any{"filesystems": rows}, nil
		},
	}
}
