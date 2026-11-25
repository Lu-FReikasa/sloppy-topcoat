//go:build linux

package netinfo

import (
	"errors"
	"unsafe"

	"golang.org/x/sys/unix"
)

// ethtool_wolinfo (linux/ethtool.h) and the request that fills it.
const ethtoolGWOL = 0x00000005

type ethtoolWolinfo struct {
	cmd       uint32
	supported uint32
	wolopts   uint32
	sopass    [6]uint8
}

type ifreqData struct {
	name [unix.IFNAMSIZ]byte
	data unsafe.Pointer
	_    [24 - unsafe.Sizeof(uintptr(0))]byte // pad to the size of struct ifreq
}

func platformWoL() func(string) (bool, bool) { return ethtoolWoL }

// ethtoolWoL asks the driver which Wake-on-LAN modes the card supports (ETHTOOL_GWOL). A driver
// that does not implement the request (virtual devices) or a failure other than that is "not
// known" — except EOPNOTSUPP, which is the driver saying it cannot.
func ethtoolWoL(name string) (supported, known bool) {
	if len(name) >= unix.IFNAMSIZ {
		return false, false
	}
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM, 0)
	if err != nil {
		return false, false
	}
	defer unix.Close(fd) //nolint:errcheck // best effort
	w := ethtoolWolinfo{cmd: ethtoolGWOL}
	ifr := ifreqData{data: unsafe.Pointer(&w)} //nolint:gosec // the ethtool request struct, as documented in linux/ethtool.h
	copy(ifr.name[:], name)
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), unix.SIOCETHTOOL, uintptr(unsafe.Pointer(&ifr))) //nolint:gosec // the documented ethtool ioctl
	if errno != 0 {
		if errors.Is(errno, unix.EOPNOTSUPP) {
			return false, true
		}
		return false, false
	}
	return w.supported != 0, true
}
