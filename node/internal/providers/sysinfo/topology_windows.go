package sysinfo

import (
	"errors"
	"unsafe"

	"golang.org/x/sys/windows"
)

var procGetLogicalProcessorInformationEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GetLogicalProcessorInformationEx")

const relationProcessorCore = 0

// windowsTopology calls GetLogicalProcessorInformationEx(RelationProcessorCore).
// UNVERIFIED on real hardware.
func windowsTopology() ([]coreInfo, error) {
	var size uint32
	r, _, err := procGetLogicalProcessorInformationEx.Call(relationProcessorCore, 0, uintptr(unsafe.Pointer(&size)))
	if r == 0 && !errors.Is(err, windows.ERROR_INSUFFICIENT_BUFFER) {
		return nil, err
	}
	buf := make([]byte, size)
	r, _, err = procGetLogicalProcessorInformationEx.Call(relationProcessorCore, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)))
	if r == 0 {
		return nil, err
	}
	return parseProcessorCores(buf[:size]), nil
}
