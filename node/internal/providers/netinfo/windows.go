package netinfo

import (
	"net"
	"strings"
)

// Interface types of the Windows network stack (IANA ifType) that can be woken over the LAN.
const (
	ifTypeEthernet = 6  // IF_TYPE_ETHERNET_CSMACD
	ifTypeWiFi     = 71 // IF_TYPE_IEEE80211
)

// wol is what is known about the Wake-on-LAN ability of an adapter.
type wol int

const (
	wolUnknown wol = iota // the driver's settings could not be read
	wolYes                // the driver offers the standard Wake-on-Magic-Packet keyword
	wolNo                 // its settings were read and the keyword is missing
)

// windowsAdapter is one entry of GetAdaptersAddresses joined with the driver settings in the
// registry.
type windowsAdapter struct {
	Name        string // the friendly name ("Ethernet", "WLAN")
	Description string // the driver's description ("Intel(R) Wi-Fi 6 AX201 160MHz")
	IfType      uint32
	MAC         net.HardwareAddr
	// InstanceID is the PnP device instance ID, e.g. PCI\VEN_8086&DEV_51F0\…; empty if unknown.
	InstanceID string
	WoL        wol
}

// physicalBuses are the device-instance prefixes of real hardware: PCI(e) cards, USB adapters and
// SoC devices. Virtual adapters (Wi-Fi Direct, Hyper-V, VPN, Bluetooth PAN, WSL) come from
// ROOT\, SWD\, VMBUS\, BTH\ and the like.
var physicalBuses = []string{`pci\`, `usb\`, `acpi\`, `pcmcia\`, `sd\`}

func physicalDevice(instanceID string) bool {
	id := strings.ToLower(instanceID)
	for _, b := range physicalBuses {
		if strings.HasPrefix(id, b) {
			return true
		}
	}
	return false
}

// keepWindows decides whether an adapter is a physical Ethernet or Wi-Fi card that can be woken.
// What is unknown (no registry entry, unreadable settings) does not exclude an adapter; what is
// known to be wrong does.
func keepWindows(a windowsAdapter) bool {
	if a.IfType != ifTypeEthernet && a.IfType != ifTypeWiFi { // mobile broadband, tunnels, PPP …
		return false
	}
	if len(a.MAC) != 6 || a.MAC.String() == "00:00:00:00:00:00" {
		return false
	}
	if looksVirtual(a.Name) || looksVirtual(a.Description) {
		return false
	}
	if a.InstanceID != "" && !physicalDevice(a.InstanceID) {
		return false
	}
	return a.WoL != wolNo
}

func windowsInterfaces(in []windowsAdapter) []Iface {
	var out []Iface
	for _, a := range in {
		if !keepWindows(a) {
			continue
		}
		kind := Ethernet
		if a.IfType == ifTypeWiFi {
			kind = WiFi
		}
		out = append(out, Iface{Name: a.Name, MAC: a.MAC.String(), Kind: kind})
	}
	return out
}
