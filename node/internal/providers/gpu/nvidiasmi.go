package gpu

import (
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// NvidiaSMI reads NVIDIA GPUs by running `nvidia-smi --query-gpu=...` (a
// fixed command line; fallback when NVML cannot be loaded).
type NvidiaSMI struct {
	// Run is injectable for tests; default executes nvidia-smi.
	Run func(ctx context.Context) ([]byte, error)
	// Exec runs nvidia-smi with other fixed arguments (Sample); injectable for tests.
	Exec func(ctx context.Context, args []string) ([]byte, error)
}

var (
	smiSampleArgs = []string{"--query-gpu=index,name,memory.total,memory.used,utilization.gpu,uuid", "--format=csv,noheader,nounits"}
	smiAppsArgs   = []string{"--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"}
)

func execNvidiaSMI(ctx context.Context, args []string) ([]byte, error) {
	path, err := exec.LookPath("nvidia-smi")
	if err != nil {
		return nil, errNoDevice
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, path, args...).Output() //nolint:gosec // fixed binary; args are package constants
}

// Sample implements Sampler with two nvidia-smi calls (GPUs with utilization,
// compute processes with their memory). Other programs' utilization is only
// known while none of our processes runs on the GPU.
//
// UNVERIFIED on real hardware.
func (n NvidiaSMI) Sample(ctx context.Context, pids []int) ([]GPU, []ProcessUsage, error) {
	run := n.Exec
	if run == nil {
		run = execNvidiaSMI
	}
	out, err := run(ctx, smiSampleArgs)
	if errors.Is(err, errNoDevice) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	gpus, util, uuids, err := parseNvidiaSMISample(out)
	if err != nil {
		return nil, nil, err
	}
	usage := make([]ProcessUsage, len(gpus))
	apps, appsErr := run(ctx, smiAppsArgs)
	var perGPU map[string]map[int]int64
	if appsErr == nil {
		perGPU, appsErr = parseNvidiaSMIApps(apps)
	}
	set := pidSet(pids)
	for i := range gpus {
		var own int64
		if appsErr == nil {
			for pid, mb := range perGPU[uuids[i]] {
				if set[pid] {
					own += mb
				}
			}
			usage[i].OwnDedicatedMB, usage[i].OwnKnown = own, true
		}
		if util[i] >= 0 {
			usage[i].UtilPercent, usage[i].UtilKnown = util[i], true
			if appsErr == nil && own == 0 {
				usage[i].ForeignUtilPercent, usage[i].ForeignUtilKnown = util[i], true
			}
		}
	}
	return gpus, usage, nil
}

// parseNvidiaSMISample parses "0, NVIDIA GeForce RTX 3070, 8192, 512, 37, GPU-uuid"
// lines; utilization is -1 when nvidia-smi reports [N/A].
func parseNvidiaSMISample(out []byte) ([]GPU, []float64, []string, error) {
	r := csv.NewReader(strings.NewReader(string(out)))
	r.TrimLeadingSpace = true
	r.FieldsPerRecord = 6
	recs, err := r.ReadAll()
	if err != nil {
		return nil, nil, nil, fmt.Errorf("nvidia-smi output: %w", err)
	}
	var gpus []GPU
	var utils []float64
	var uuids []string
	for _, rec := range recs {
		idx, err1 := strconv.Atoi(strings.TrimSpace(rec[0]))
		total, err2 := strconv.ParseInt(strings.TrimSpace(rec[2]), 10, 64)
		used, err3 := strconv.ParseInt(strings.TrimSpace(rec[3]), 10, 64)
		if err1 != nil || err2 != nil || err3 != nil {
			return nil, nil, nil, fmt.Errorf("nvidia-smi output: bad line %q", strings.Join(rec, ","))
		}
		util, err := strconv.ParseFloat(strings.TrimSpace(rec[4]), 64)
		if err != nil {
			util = -1
		}
		gpus = append(gpus, GPU{Index: idx, Name: strings.TrimSpace(rec[1]), Vendor: VendorNVIDIA, VRAMTotalMB: total, VRAMUsedMB: used})
		utils = append(utils, util)
		uuids = append(uuids, strings.TrimSpace(rec[5]))
	}
	return gpus, utils, uuids, nil
}

// parseNvidiaSMIApps parses "GPU-uuid, 1234, 5120" lines (MiB) per GPU uuid.
func parseNvidiaSMIApps(out []byte) (map[string]map[int]int64, error) {
	res := map[string]map[int]int64{}
	text := strings.TrimSpace(string(out))
	if text == "" || strings.HasPrefix(text, "No running") {
		return res, nil
	}
	r := csv.NewReader(strings.NewReader(text))
	r.TrimLeadingSpace = true
	r.FieldsPerRecord = 3
	recs, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("nvidia-smi compute apps: %w", err)
	}
	for _, rec := range recs {
		pid, err1 := strconv.Atoi(strings.TrimSpace(rec[1]))
		mb, err2 := strconv.ParseInt(strings.TrimSpace(rec[2]), 10, 64)
		if err1 != nil || err2 != nil {
			continue // e.g. [N/A] memory
		}
		uuid := strings.TrimSpace(rec[0])
		if res[uuid] == nil {
			res[uuid] = map[int]int64{}
		}
		res[uuid][pid] += mb
	}
	return res, nil
}

var smiArgs = []string{"--query-gpu=index,name,memory.total,memory.used", "--format=csv,noheader,nounits"}

func runNvidiaSMI(ctx context.Context) ([]byte, error) {
	path, err := exec.LookPath("nvidia-smi")
	if err != nil {
		return nil, errNoDevice
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, path, smiArgs...).Output() //nolint:gosec // fixed binary and arguments
}

// GPUs implements GPUProvider.
func (n NvidiaSMI) GPUs(ctx context.Context) ([]GPU, error) {
	run := n.Run
	if run == nil {
		run = runNvidiaSMI
	}
	out, err := run(ctx)
	if errors.Is(err, errNoDevice) {
		return nil, nil // no NVIDIA driver installed
	}
	if err != nil {
		return nil, err
	}
	return parseNvidiaSMI(out)
}

// parseNvidiaSMI parses "0, NVIDIA GeForce RTX 3070, 8192, 512" lines (MiB).
func parseNvidiaSMI(out []byte) ([]GPU, error) {
	r := csv.NewReader(strings.NewReader(string(out)))
	r.TrimLeadingSpace = true
	r.FieldsPerRecord = 4
	recs, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("nvidia-smi output: %w", err)
	}
	gpus := make([]GPU, 0, len(recs))
	for _, rec := range recs {
		idx, err1 := strconv.Atoi(strings.TrimSpace(rec[0]))
		total, err2 := strconv.ParseInt(strings.TrimSpace(rec[2]), 10, 64)
		used, err3 := strconv.ParseInt(strings.TrimSpace(rec[3]), 10, 64)
		if err1 != nil || err2 != nil || err3 != nil {
			return nil, fmt.Errorf("nvidia-smi output: bad line %q", strings.Join(rec, ","))
		}
		gpus = append(gpus, GPU{Index: idx, Name: strings.TrimSpace(rec[1]), Vendor: VendorNVIDIA, VRAMTotalMB: total, VRAMUsedMB: used})
	}
	return gpus, nil
}
