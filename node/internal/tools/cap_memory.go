package tools

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// What the RAM of a machine is spent on (2026-11-15): a node answered "90 % in use" and nothing
// could say why (on node-cpu it was three virtual machines). system.memory splits /proc/meminfo
// into its parts, reads the ZFS ARC where there is one and names the processes that hold the most,
// with the virtual machine a QEMU process runs and the container a process belongs to. It reads
// only fixed files under /proc; no command line is returned (it may hold secrets), only the name
// of a VM taken from it.
//
// In a container the node sees only its own processes; with the host's /proc mounted read-only
// (`-v /proc:/host/proc:ro`, TOPCOAT_HOST_PROC=/host/proc) it sees the host's, without being able
// to signal any of them.

const memoryTopDefault, memoryTopMax = 10, 30

var (
	// "-name guest=vm-b,debug-threads=on" or "-name vm-b"
	qemuName = regexp.MustCompile(`^(?:guest=)?([^,]+)`)
	// a cgroup path of a Docker container: ".../docker-<id>.scope", ".../docker/<id>", or "/../<id>" as a
	// container node sees it from its own cgroup namespace (live on node-cpu 2026-11-15)
	dockerCgroup = regexp.MustCompile(`(?:docker[-/]|/)([0-9a-f]{12})[0-9a-f]{52}\b`)
)

func (o Options) memProc() string {
	if o.HostProc != "" {
		return o.HostProc
	}
	return o.proc()
}

type memProcess struct {
	PID       int    `json:"pid"`
	Name      string `json:"name"`
	RSSMB     int64  `json:"rss_mb"`
	SwapMB    int64  `json:"swap_mb,omitempty"`
	VM        string `json:"vm,omitempty"`
	Container string `json:"container,omitempty"`
}

// statusFields reads Name, VmRSS and VmSwap of /proc/<pid>/status.
func statusFields(text string) (name string, rssKB, swapKB int64) {
	for _, l := range strings.Split(text, "\n") {
		k, v, ok := strings.Cut(l, ":")
		if !ok {
			continue
		}
		v = strings.TrimSpace(v)
		switch k {
		case "Name":
			name = v
		case "VmRSS", "VmSwap":
			n, _ := strconv.ParseInt(strings.Fields(v + " 0")[0], 10, 64)
			if k == "VmRSS" {
				rssKB = n
			} else {
				swapKB = n
			}
		}
	}
	return name, rssKB, swapKB
}

// vmNameOf is the name of the virtual machine a QEMU command line runs ("" for anything else).
func vmNameOf(cmdline []byte) string {
	args := strings.Split(strings.TrimRight(string(cmdline), "\x00"), "\x00")
	if len(args) == 0 || !strings.Contains(filepath.Base(args[0]), "qemu") {
		return ""
	}
	for i, a := range args[:len(args)-1] {
		if a == "-name" {
			if m := qemuName.FindStringSubmatch(args[i+1]); m != nil {
				return m[1]
			}
		}
	}
	return ""
}

func systemMemory(o Options) capability.Capability {
	return fn{
		name: "system.memory",
		desc: "What the RAM of this machine is spent on: the parts of /proc/meminfo (programs, cache, shared memory, kernel), " +
			"swap, the ZFS ARC, and the processes that hold the most, with the virtual machine a QEMU process runs and the " +
			"container a process belongs to. Read-only.",
		schema: `{"type":"object","additionalProperties":false,"properties":{"top":{"type":"integer","minimum":1,"maximum":30}}}`,
		run: func(_ context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Top int `json:"top"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.Top <= 0 {
				in.Top = memoryTopDefault
			}
			in.Top = min(in.Top, memoryTopMax)
			if runtime.GOOS != "linux" && o.ProcRoot == "" && o.HostProc == "" {
				return nil, errors.New("system.memory reads /proc and is only available on Linux nodes")
			}
			proc := o.memProc()
			text, err := readText(filepath.Join(proc, "meminfo"))
			if err != nil {
				return nil, err
			}
			m := meminfoMB(text)
			total, avail := m["MemTotal"], m["MemAvailable"]
			out := map[string]any{
				"memory_mb": map[string]int64{
					"total": total, "available": avail, "used": total - avail,
					"programs":           m["AnonPages"],
					"cache":              m["Cached"] - m["Shmem"],
					"shared_tmpfs":       m["Shmem"],
					"kernel_reclaimable": m["SReclaimable"],
					"kernel_other":       m["SUnreclaim"] + m["KernelStack"] + m["PageTables"],
					"swap_total":         m["SwapTotal"], "swap_used": m["SwapTotal"] - m["SwapFree"],
				},
			}
			if t, err := readText(filepath.Join(proc, "spl/kstat/zfs/arcstats")); err == nil {
				for _, l := range strings.Split(t, "\n") {
					if f := strings.Fields(l); len(f) == 3 && f[0] == "size" {
						n, _ := strconv.ParseInt(f[2], 10, 64)
						out["zfs_arc_mb"] = n >> 20
					}
				}
			}
			entries, err := os.ReadDir(proc)
			if err != nil {
				return nil, err
			}
			var procs []memProcess
			byVM := map[string]int64{}
			byContainer := map[string]int64{}
			for _, e := range entries {
				pid, err := strconv.Atoi(e.Name())
				if err != nil {
					continue
				}
				dir := filepath.Join(proc, e.Name())
				st, err := readText(filepath.Join(dir, "status"))
				if err != nil {
					continue // gone, or not ours to read
				}
				name, rss, swap := statusFields(st)
				if rss == 0 && swap == 0 {
					continue // kernel threads
				}
				p := memProcess{PID: pid, Name: name, RSSMB: rss >> 10, SwapMB: swap >> 10}
				if cl, err := os.ReadFile(filepath.Join(dir, "cmdline")); err == nil { //nolint:gosec // under /proc
					p.VM = vmNameOf(cl)
				}
				if cg, err := readText(filepath.Join(dir, "cgroup")); err == nil {
					if mm := dockerCgroup.FindStringSubmatch(cg); mm != nil {
						p.Container = mm[1]
					}
				}
				if p.VM != "" {
					byVM[p.VM] += p.RSSMB
				}
				if p.Container != "" {
					byContainer[p.Container] += p.RSSMB
				}
				procs = append(procs, p)
			}
			sort.Slice(procs, func(i, j int) bool { return procs[i].RSSMB+procs[i].SwapMB > procs[j].RSSMB+procs[j].SwapMB })
			out["processes_seen"] = len(procs)
			out["top_processes"] = procs[:min(in.Top, len(procs))]
			if len(byVM) > 0 {
				out["virtual_machines_mb"] = byVM
			}
			if len(byContainer) > 0 {
				out["containers_mb"] = byContainer
			}
			if o.HostProc == "" && inContainer(proc) {
				out["note"] = "the node runs in a container and sees only its own processes; mount the host's /proc " +
					"read-only and set TOPCOAT_HOST_PROC to see all of them"
			}
			return out, nil
		},
	}
}

// inContainer says whether the processes under proc are a container's (PID 1 is not an init).
func inContainer(proc string) bool {
	comm, err := readText(filepath.Join(proc, "1", "comm"))
	if err != nil {
		return false
	}
	switch strings.TrimSpace(comm) {
	case "systemd", "init", "launchd":
		return false
	}
	return true
}
