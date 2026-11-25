package fake

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"time"
)

// Control is the --fake-control file: what other programs do on the fake
// machine, so VRAM pressure (E1-06) can be tried without a GPU, e.g.
//
//	{"gpus": [{"foreign_vram_mb": 12000, "foreign_util": 90}], "ram_foreign_mb": 0}
type Control struct {
	GPUs         []Other `json:"gpus"`
	RAMForeignMB int64   `json:"ram_foreign_mb"`
	// Activity is what the person at the fake machine does (D24): the node ducks while they
	// type, while other programs use the CPU, or on battery. Omitted = nobody is there.
	Activity *ControlActivity `json:"activity,omitempty"`
}

// ControlActivity is the "activity" part of the control file.
type ControlActivity struct {
	// InputIdleSeconds is the time since the last input; a negative value or absent = unknown.
	InputIdleSeconds *float64 `json:"input_idle_seconds,omitempty"`
	ForeignCPUPct    *float64 `json:"foreign_cpu_percent,omitempty"`
	OnBattery        *bool    `json:"on_battery,omitempty"`
}

// ApplyControl parses a control document and applies it.
func (w *World) ApplyControl(b []byte) error {
	var c Control
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return fmt.Errorf("fake control: %w", err)
	}
	for i, o := range c.GPUs {
		if err := w.SetOther(i, o); err != nil {
			return fmt.Errorf("fake control: gpus[%d]: %w", i, err)
		}
	}
	w.SetForeignRAM(c.RAMForeignMB)
	a := c.Activity
	if a == nil {
		a = &ControlActivity{}
	}
	if a.InputIdleSeconds != nil && *a.InputIdleSeconds >= 0 {
		w.SetInputIdle(time.Duration(*a.InputIdleSeconds*float64(time.Second)), true)
	} else {
		w.SetInputIdle(0, false)
	}
	if a.ForeignCPUPct != nil {
		w.SetForeignCPU(*a.ForeignCPUPct, true)
	} else {
		w.SetForeignCPU(0, false)
	}
	if a.OnBattery != nil {
		w.SetOnBattery(*a.OnBattery, true)
	} else {
		w.SetOnBattery(false, false)
	}
	return nil
}

// WatchControl re-reads the control file whenever it changes until ctx ends.
func (w *World) WatchControl(ctx context.Context, path string, every time.Duration, log *slog.Logger) {
	var last time.Time
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		if st, err := os.Stat(path); err == nil && st.ModTime() != last {
			last = st.ModTime()
			if b, err := os.ReadFile(path); err == nil {
				if err := w.ApplyControl(b); err != nil {
					log.Warn("ignoring fake control file", "path", path, "err", err)
				} else {
					log.Info("fake control applied", "path", path)
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}
