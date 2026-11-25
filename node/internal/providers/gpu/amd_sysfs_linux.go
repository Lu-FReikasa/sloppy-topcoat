package gpu

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// AMDSysfs reads AMD GPUs on Linux via the amdgpu sysfs interface:
// /sys/class/drm/card*/device with vendor 0x1002, mem_info_vram_total/used
// and, for APUs, mem_info_gtt_total/used as shared memory.
//
// UNVERIFIED on real hardware (tested with sysfs fixtures).
type AMDSysfs struct {
	Root     string // default /sys/class/drm
	ProcRoot string // default /proc (fdinfo of our model processes)
}

// integratedVRAMLimitMB: an AMD GPU with at most this much VRAM is an APU
// whose "VRAM" is a carve-out of system memory.
const integratedVRAMLimitMB = 2048

// GPUs implements GPUProvider.
func (a AMDSysfs) GPUs(context.Context) ([]GPU, error) {
	gpus, _ := a.scan()
	return gpus, nil
}

// Sample implements Sampler: our processes' VRAM and GTT (shared) memory from
// /proc/<pid>/fdinfo (amdgpu drm-memory-* per DRM client, matched to the card
// by drm-pdev) and the utilization from gpu_busy_percent. amdgpu evicts VRAM
// to GTT under pressure, so the demotion signal works here too.
//
// UNVERIFIED on real hardware (tested with sysfs/fdinfo fixtures).
func (a AMDSysfs) Sample(_ context.Context, pids []int) ([]GPU, []ProcessUsage, error) {
	gpus, devs := a.scan()
	usage := make([]ProcessUsage, len(gpus))
	procRoot := a.ProcRoot
	if procRoot == "" {
		procRoot = "/proc"
	}
	own, ok := fdinfoUsage(procRoot, pids)
	for i, dev := range devs {
		if ok {
			m := own[pciAddress(dev)]
			usage[i].OwnDedicatedMB, usage[i].OwnKnown = m.vramKiB>>10, true
			usage[i].OwnSharedMB, usage[i].SharedKnown = m.gttKiB>>10, true
		}
		if busy, err := strconv.ParseFloat(strings.TrimSpace(readFile(filepath.Join(dev, "gpu_busy_percent"))), 64); err == nil {
			usage[i].UtilPercent, usage[i].UtilKnown = busy, true
			if ok && own[pciAddress(dev)].vramKiB == 0 && own[pciAddress(dev)].gttKiB == 0 {
				usage[i].ForeignUtilPercent, usage[i].ForeignUtilKnown = busy, true
			}
		}
	}
	return gpus, usage, nil
}

// pciAddress is the PCI address of a card's device directory (symlink target).
func pciAddress(dev string) string {
	target, err := filepath.EvalSymlinks(dev)
	if err != nil {
		return filepath.Base(dev)
	}
	return filepath.Base(target)
}

type drmMem struct{ vramKiB, gttKiB int64 }

// fdinfoUsage sums drm-memory-vram / drm-memory-gtt of the given processes
// per PCI device, counting each DRM client once. ok=false if a process's
// fdinfo could not be read.
func fdinfoUsage(procRoot string, pids []int) (map[string]drmMem, bool) {
	out := map[string]drmMem{}
	seen := map[string]bool{}
	for _, pid := range pids {
		if pid <= 0 {
			continue
		}
		files, err := filepath.Glob(filepath.Join(procRoot, strconv.Itoa(pid), "fdinfo", "*"))
		if err != nil || files == nil {
			if _, statErr := os.Stat(filepath.Join(procRoot, strconv.Itoa(pid))); statErr != nil {
				continue // process gone
			}
			return nil, false
		}
		for _, f := range files {
			info := parseFDInfo(readFile(f))
			if info.pdev == "" || info.client == "" {
				continue
			}
			key := info.pdev + "/" + info.client
			if seen[key] {
				continue
			}
			seen[key] = true
			m := out[info.pdev]
			m.vramKiB += info.mem.vramKiB
			m.gttKiB += info.mem.gttKiB
			out[info.pdev] = m
		}
	}
	return out, true
}

type fdInfo struct {
	pdev, client string
	mem          drmMem
}

// parseFDInfo reads the DRM keys of one fdinfo file. drm-resident-* (newer
// kernels) wins over drm-memory-*.
func parseFDInfo(text string) fdInfo {
	var fi fdInfo
	var resident drmMem
	var hasResident bool
	for _, line := range strings.Split(text, "\n") {
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		val = strings.TrimSpace(val)
		switch strings.TrimSpace(key) {
		case "drm-pdev":
			fi.pdev = val
		case "drm-client-id":
			fi.client = val
		case "drm-memory-vram":
			fi.mem.vramKiB = kib(val)
		case "drm-memory-gtt":
			fi.mem.gttKiB = kib(val)
		case "drm-resident-vram":
			resident.vramKiB, hasResident = kib(val), true
		case "drm-resident-gtt":
			resident.gttKiB, hasResident = kib(val), true
		}
	}
	if hasResident {
		fi.mem = resident
	}
	return fi
}

// kib parses "1234 KiB" / "12 MiB" / "4096" (bytes) into KiB.
func kib(v string) int64 {
	num, unit, _ := strings.Cut(strings.TrimSpace(v), " ")
	n, err := strconv.ParseInt(num, 10, 64)
	if err != nil {
		return 0
	}
	switch strings.TrimSpace(unit) {
	case "KiB":
		return n
	case "MiB":
		return n << 10
	case "GiB":
		return n << 20
	case "":
		return n >> 10
	}
	return 0
}

// scan lists the AMD GPUs and their device directories (index-aligned).
func (a AMDSysfs) scan() ([]GPU, []string) {
	root := a.Root
	if root == "" {
		root = "/sys/class/drm"
	}
	cards, _ := filepath.Glob(filepath.Join(root, "card[0-9]*"))
	sort.Strings(cards)
	var out []GPU
	var devs []string
	for _, c := range cards {
		if strings.Contains(filepath.Base(c), "-") {
			continue // connector entries like card0-DP-1
		}
		dev := filepath.Join(c, "device")
		if strings.TrimSpace(readFile(filepath.Join(dev, "vendor"))) != "0x1002" {
			continue
		}
		total := readBytesMB(filepath.Join(dev, "mem_info_vram_total"))
		if total <= 0 {
			continue
		}
		g := GPU{
			Index: len(out), Vendor: VendorAMD, Name: amdName(dev),
			VRAMTotalMB: total, VRAMUsedMB: readBytesMB(filepath.Join(dev, "mem_info_vram_used")),
		}
		if total <= integratedVRAMLimitMB {
			g.Integrated = true
			g.SharedTotalMB = readBytesMB(filepath.Join(dev, "mem_info_gtt_total"))
			g.SharedUsedMB = readBytesMB(filepath.Join(dev, "mem_info_gtt_used"))
		}
		out = append(out, g)
		devs = append(devs, dev)
	}
	return out, devs
}

func amdName(dev string) string {
	if n := strings.TrimSpace(readFile(filepath.Join(dev, "product_name"))); n != "" {
		return n
	}
	return "AMD GPU " + strings.TrimSpace(readFile(filepath.Join(dev, "device")))
}

func readFile(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return string(b)
}

func readBytesMB(p string) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(readFile(p)), 10, 64)
	if err != nil {
		return 0
	}
	return n >> 20
}

// NewSystem returns the GPU provider for this OS (Linux: NVML with
// nvidia-smi fallback, plus amdgpu sysfs).
func NewSystem() GPUProvider {
	return Multi{NVML{Fallback: NvidiaSMI{}}, AMDSysfs{}}
}
