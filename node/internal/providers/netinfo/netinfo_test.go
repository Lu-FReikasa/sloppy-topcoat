package netinfo

import (
	"net"
	"os"
	"path/filepath"
	"testing"
)

func iface(name, mac string, flags net.Flags) net.Interface {
	hw, _ := net.ParseMAC(mac)
	return net.Interface{Name: name, HardwareAddr: hw, Flags: flags}
}

func TestOnlyPhysicalInterfacesWiredFirst(t *testing.T) {
	sys := t.TempDir()
	for _, n := range []string{"wlp2s0", "enp3s0"} { // real cards sit on a device
		_ = os.MkdirAll(filepath.Join(sys, n, "device"), 0o755)
	}
	_ = os.MkdirAll(filepath.Join(sys, "wlp2s0", "wireless"), 0o755)
	_ = os.MkdirAll(filepath.Join(sys, "docker0"), 0o755) // a bridge: no device
	s := system{sysfs: sys, goos: "linux", list: func() ([]net.Interface, error) {
		return []net.Interface{
			iface("lo", "00:00:00:00:00:00", net.FlagLoopback),
			iface("wlp2s0", "02:00:00:00:00:02", net.FlagUp),
			iface("docker0", "02:00:00:00:00:03", net.FlagUp),
			iface("enp3s0", "02:00:00:00:00:01", net.FlagUp),
			iface("veth1234", "02:00:00:00:00:04", net.FlagUp),
			iface("tun0", "", net.FlagUp),
		}, nil
	}}
	got := s.Interfaces()
	if len(got) != 2 || got[0] != (Iface{Name: "enp3s0", MAC: "02:00:00:00:00:01", Kind: Ethernet}) ||
		got[1] != (Iface{Name: "wlp2s0", MAC: "02:00:00:00:00:02", Kind: WiFi}) {
		t.Fatalf("%+v", got)
	}
}

func TestWindowsNamesAreFilteredByName(t *testing.T) {
	s := system{goos: "windows", list: func() ([]net.Interface, error) {
		return []net.Interface{
			iface("Ethernet", "02:00:00:00:00:01", net.FlagUp),
			iface("vEthernet (WSL)", "02:00:00:00:00:05", net.FlagUp),
			iface("VMware Network Adapter VMnet1", "02:00:00:00:00:06", net.FlagUp),
			iface("Bluetooth Network Connection", "02:00:00:00:00:07", net.FlagUp),
			iface("Wi-Fi", "02:00:00:00:00:02", net.FlagUp),
		}, nil
	}}
	got := s.Interfaces()
	if len(got) != 2 || got[0].Name != "Ethernet" || got[0].Kind != Ethernet || got[1].Kind != WiFi {
		t.Fatalf("%+v", got)
	}
}

func mac(s string) net.HardwareAddr {
	hw, _ := net.ParseMAC(s)
	return hw
}

// The adapters of the real laptop that showed the problem: a Wi-Fi card, Wi-Fi Direct virtual
// adapters, a virtual LAN connection and mobile broadband.
func TestWindowsListsOnlyPhysicalEthernetAndWiFiThatCanWake(t *testing.T) {
	adapters := []windowsAdapter{
		{Name: "WLAN", Description: "Intel(R) Wi-Fi 6 AX201 160MHz", IfType: ifTypeWiFi, MAC: mac("02:00:00:00:00:02"), InstanceID: `PCI\VEN_8086&DEV_51F0`, WoL: wolYes},
		{Name: "LAN-Verbindung* 9", Description: "Microsoft Wi-Fi Direct Virtual Adapter", IfType: ifTypeWiFi, MAC: mac("02:00:00:00:00:12"), InstanceID: `SWD\WIFIDIRECT`, WoL: wolYes},
		{Name: "LAN-Verbindung* 10", Description: "Microsoft Wi-Fi Direct Virtual Adapter #2", IfType: ifTypeWiFi, MAC: mac("02:00:00:00:00:22"), WoL: wolUnknown},
		{Name: "LAN-Verbindung 2", Description: "Bluetooth Device (Personal Area Network)", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:32"), InstanceID: `BTH\MS_BTHPAN`, WoL: wolNo},
		{Name: "Mobilfunk", Description: "Mobile Broadband Adapter", IfType: 243, MAC: mac("02:00:00:00:00:42"), InstanceID: `USB\VID_1199`, WoL: wolUnknown},
		{Name: "vEthernet (WSL)", Description: "Hyper-V Virtual Ethernet Adapter", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:52"), InstanceID: `ROOT\VMS_MP`, WoL: wolYes},
		{Name: "Ethernet", Description: "Realtek PCIe GbE Family Controller", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:01"), InstanceID: `PCI\VEN_10EC&DEV_8168`, WoL: wolYes},
		{Name: "USB-LAN", Description: "USB GbE Family Controller", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:62"), InstanceID: `USB\VID_0BDA&PID_8153`, WoL: wolNo},
	}
	s := system{goos: "windows", win: func() []windowsAdapter { return adapters }}
	got := s.Interfaces()
	want := []Iface{
		{Name: "Ethernet", MAC: "02:00:00:00:00:01", Kind: Ethernet},
		{Name: "WLAN", MAC: "02:00:00:00:00:02", Kind: WiFi},
	}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("%+v", got)
	}
}

func TestWindowsKeepsWhatItCannotJudge(t *testing.T) {
	// no registry entry (settings unreadable): the type and name decide alone
	a := windowsAdapter{Name: "Ethernet", Description: "Some NIC", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:01")}
	if !keepWindows(a) {
		t.Fatal("an adapter without driver information must stay")
	}
	a.MAC = nil
	if keepWindows(a) {
		t.Fatal("an adapter without a hardware address cannot be woken")
	}
}

func TestOneEntryPerHardwareAddress(t *testing.T) {
	s := system{goos: "windows", win: func() []windowsAdapter {
		return []windowsAdapter{
			{Name: "Ethernet", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:01"), InstanceID: `PCI\VEN_1`},
			{Name: "Ethernet 2", IfType: ifTypeEthernet, MAC: mac("02:00:00:00:00:01"), InstanceID: `PCI\VEN_1`}, // a vSwitch clone
		}
	}}
	if got := s.Interfaces(); len(got) != 1 || got[0].Name != "Ethernet" {
		t.Fatalf("%+v", got)
	}
}

func TestLinuxDropsWiredCardsThatSayTheyCannotWake(t *testing.T) {
	sys := t.TempDir()
	for _, n := range []string{"enp3s0", "enp4s0", "enp5s0", "wlp2s0"} {
		_ = os.MkdirAll(filepath.Join(sys, n, "device"), 0o755)
	}
	_ = os.MkdirAll(filepath.Join(sys, "wlp2s0", "wireless"), 0o755)
	s := system{sysfs: sys, goos: "linux",
		list: func() ([]net.Interface, error) {
			return []net.Interface{
				iface("enp3s0", "02:00:00:00:00:01", net.FlagUp), // can wake
				iface("enp4s0", "02:00:00:00:00:03", net.FlagUp), // driver says no
				iface("enp5s0", "02:00:00:00:00:04", net.FlagUp), // driver does not say
				iface("wlp2s0", "02:00:00:00:00:02", net.FlagUp),
			}, nil
		},
		wol: func(name string) (bool, bool) {
			switch name {
			case "enp3s0":
				return true, true
			case "enp4s0":
				return false, true
			}
			return false, false
		}}
	var names []string
	for _, i := range s.Interfaces() {
		names = append(names, i.Name)
	}
	if len(names) != 3 || names[0] != "enp3s0" || names[1] != "enp5s0" || names[2] != "wlp2s0" {
		t.Fatalf("%v", names)
	}
}
