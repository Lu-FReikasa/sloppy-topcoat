// Package netinfo lists the network interfaces a machine can be woken through (Wake-on-LAN):
// the hardware address of every physical interface. The hub stores them with the inventory, so
// it still knows them while the node is asleep or off (E2-05 E).
package netinfo

import (
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// Kind of an interface.
const (
	Ethernet = "ethernet"
	WiFi     = "wifi"
	Other    = "other"
)

// Iface is one physical network interface.
type Iface struct {
	Name string `json:"name"`
	// MAC is lower-case, colon separated (aa:bb:cc:dd:ee:ff).
	MAC  string `json:"mac"`
	Kind string `json:"kind"`
}

// Provider lists the physical interfaces.
type Provider interface {
	Interfaces() []Iface
}

type system struct {
	list func() ([]net.Interface, error)
	// sysfs is /sys/class/net (Linux; a physical interface has a "device" entry).
	sysfs string
	goos  string
	// win lists the Windows adapters with what the network stack knows about them (type, device,
	// Wake-on-LAN keywords). Without it Windows interfaces are only filtered by name.
	win func() []windowsAdapter
	// wol asks whether an interface can be woken (Linux: ethtool). known is false when the
	// driver does not say; such an interface is kept.
	wol func(name string) (supported, known bool)
}

// NewSystem returns the provider for this machine.
func NewSystem() Provider {
	return system{list: net.Interfaces, sysfs: "/sys/class/net", goos: runtime.GOOS,
		win: platformWindowsAdapters(), wol: platformWoL()}
}

// virtualName matches names of interfaces that are not a network card: bridges, VPNs,
// containers, virtual machines, tunnels.
var virtualHints = []string{
	"vethernet", "virtual", "vmware", "vbox", "virtualbox", "hyper-v", "bluetooth", "loopback",
	"tap", "tun", "isatap", "teredo", "wsl", "docker", "veth", "br-", "virbr", "vboxnet",
	"tailscale", "zerotier", "wg", "vpn", "pseudo",
}

func looksVirtual(name string) bool {
	n := strings.ToLower(name)
	for _, h := range virtualHints {
		if strings.HasPrefix(n, h) || strings.Contains(n, h) && len(h) > 4 {
			return true
		}
	}
	return false
}

func kindOf(name string, wireless bool) string {
	n := strings.ToLower(name)
	switch {
	case wireless || strings.Contains(n, "wi-fi") || strings.Contains(n, "wlan") || strings.HasPrefix(n, "wl"):
		return WiFi
	case strings.Contains(n, "ethernet") || strings.HasPrefix(n, "eth") || strings.HasPrefix(n, "en"):
		return Ethernet
	}
	return Other
}

func (s system) Interfaces() []Iface {
	if s.goos == "windows" && s.win != nil {
		return sortWired(dedupe(windowsInterfaces(s.win())))
	}
	ifs, err := s.list()
	if err != nil {
		return nil
	}
	var out []Iface
	for _, i := range ifs {
		if i.Flags&net.FlagLoopback != 0 || len(i.HardwareAddr) != 6 || looksVirtual(i.Name) {
			continue
		}
		mac := i.HardwareAddr.String()
		if mac == "00:00:00:00:00:00" {
			continue
		}
		wireless := false
		if s.goos == "linux" {
			// only interfaces that sit on a device (a card, USB adapter) are physical
			if _, err := os.Stat(filepath.Join(s.sysfs, i.Name, "device")); err != nil {
				continue
			}
			_, err := os.Stat(filepath.Join(s.sysfs, i.Name, "wireless"))
			wireless = err == nil
		}
		kind := kindOf(i.Name, wireless)
		if kind == Ethernet && s.wol != nil {
			// a wired card that says it cannot wake the machine is no use here; one that says
			// nothing stays (Wi-Fi is not asked: its wake-up is not an ethtool matter)
			if supported, known := s.wol(i.Name); known && !supported {
				continue
			}
		}
		out = append(out, Iface{Name: i.Name, MAC: mac, Kind: kind})
	}
	return sortWired(dedupe(out))
}

// sortWired puts wired interfaces first: they are the ones Wake-on-LAN normally works on.
func sortWired(out []Iface) []Iface {
	sort.SliceStable(out, func(a, b int) bool { return rank(out[a].Kind) < rank(out[b].Kind) })
	return out
}

// dedupe keeps the first interface of every hardware address (a virtual switch clones the MAC of
// the card it sits on).
func dedupe(in []Iface) []Iface {
	seen := map[string]bool{}
	out := in[:0:0]
	for _, i := range in {
		if seen[i.MAC] {
			continue
		}
		seen[i.MAC] = true
		out = append(out, i)
	}
	return out
}

// rank puts wired interfaces first: they are the ones Wake-on-LAN normally works on.
func rank(kind string) int {
	switch kind {
	case Ethernet:
		return 0
	case Other:
		return 1
	}
	return 2
}
