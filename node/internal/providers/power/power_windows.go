package power

import (
	"context"
	"errors"

	"golang.org/x/sys/windows"
)

// UNVERIFIED on real hardware: SetSuspendState from a service running as
// NT SERVICE\topcoat-node (it may lack SeShutdownPrivilege; the error says so).

var procSetSuspendState = windows.NewLazySystemDLL("powrprof.dll").NewProc("SetSuspendState")

type system struct{}

// NewSystem returns the Windows sleeper: SetSuspendState(hibernate=false, force=true,
// disableWakeEvents=false), so the machine sleeps (not hibernates) and Wake-on-LAN can wake it.
func NewSystem() Sleeper { return system{} }

func (system) Sleep(context.Context) error {
	if r, _, err := procSetSuspendState.Call(0, 1, 0); r == 0 {
		return errors.Join(errors.New("SetSuspendState failed (the service account needs the right to shut the machine down)"), err)
	}
	return nil
}
