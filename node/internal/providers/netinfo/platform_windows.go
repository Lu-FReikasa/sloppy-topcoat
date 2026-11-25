package netinfo

import (
	"errors"
	"net"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// UNVERIFIED on real hardware: the join of GetAdaptersAddresses with the driver settings in the
// network class key. Which drivers expose *WakeOnMagicPacket, and whether the service account may
// read the class key, is only known from the machines this runs on; an adapter whose settings
// cannot be read is kept.

const netClassKey = `SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}`

func platformWindowsAdapters() func() []windowsAdapter { return collectWindowsAdapters }

type classEntry struct {
	instanceID string
	wol        wol
}

func collectWindowsAdapters() []windowsAdapter {
	classes := readNetClass()
	var out []windowsAdapter
	for _, a := range adaptersAddresses() {
		guid, _ := utf8FromPtr(a.AdapterName)
		w := windowsAdapter{
			Name:        windows.UTF16PtrToString(a.FriendlyName),
			Description: windows.UTF16PtrToString(a.Description),
			IfType:      a.IfType,
		}
		if n := int(a.PhysicalAddressLength); n == 6 {
			w.MAC = net.HardwareAddr(append([]byte(nil), a.PhysicalAddress[:n]...))
		}
		if c, ok := classes[strings.ToLower(guid)]; ok {
			w.InstanceID, w.WoL = c.instanceID, c.wol
		}
		out = append(out, w)
	}
	return out
}

func utf8FromPtr(p *byte) (string, bool) {
	if p == nil {
		return "", false
	}
	var b []byte
	for i := uintptr(0); ; i++ {
		c := *(*byte)(unsafe.Add(unsafe.Pointer(p), i)) //nolint:gosec // NUL-terminated string from the OS
		if c == 0 {
			break
		}
		b = append(b, c)
	}
	return string(b), true
}

func adaptersAddresses() []*windows.IpAdapterAddresses {
	const flags = windows.GAA_FLAG_SKIP_UNICAST | windows.GAA_FLAG_SKIP_ANYCAST |
		windows.GAA_FLAG_SKIP_MULTICAST | windows.GAA_FLAG_SKIP_DNS_SERVER
	size := uint32(15000)
	var buf []byte
	for range 3 {
		buf = make([]byte, size)
		err := windows.GetAdaptersAddresses(windows.AF_UNSPEC, flags, 0, (*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0])), &size) //nolint:gosec // the buffer the API fills
		if err == nil {
			break
		}
		if !errors.Is(err, windows.ERROR_BUFFER_OVERFLOW) {
			return nil
		}
	}
	var out []*windows.IpAdapterAddresses
	for a := (*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0])); a != nil; a = a.Next { //nolint:gosec // as above
		out = append(out, a)
	}
	return out
}

// readNetClass maps NetCfgInstanceId (the adapter GUID, lower case) to the device instance ID and
// the Wake-on-LAN ability from the driver's keywords.
func readNetClass() map[string]classEntry {
	out := map[string]classEntry{}
	root, err := registry.OpenKey(registry.LOCAL_MACHINE, netClassKey, registry.ENUMERATE_SUB_KEYS)
	if err != nil {
		return out
	}
	defer root.Close()
	names, err := root.ReadSubKeyNames(-1)
	if err != nil {
		return out
	}
	for _, n := range names {
		k, err := registry.OpenKey(registry.LOCAL_MACHINE, netClassKey+`\`+n, registry.QUERY_VALUE|registry.ENUMERATE_SUB_KEYS)
		if err != nil {
			continue
		}
		guid, _, gerr := k.GetStringValue("NetCfgInstanceId")
		if gerr == nil {
			e := classEntry{wol: wolNo}
			e.instanceID, _, _ = k.GetStringValue("DeviceInstanceID")
			if hasValue(k, "*WakeOnMagicPacket") || hasParam(k, "*WakeOnMagicPacket") {
				e.wol = wolYes
			}
			out[strings.ToLower(guid)] = e
		}
		_ = k.Close()
	}
	return out
}

func hasValue(k registry.Key, name string) bool {
	_, _, err := k.GetStringValue(name)
	if err == nil {
		return true
	}
	_, _, err = k.GetIntegerValue(name)
	return err == nil
}

// hasParam looks for the keyword in the driver's advertised settings (Ndi\params\<keyword>).
func hasParam(k registry.Key, name string) bool {
	p, err := registry.OpenKey(k, `Ndi\params\`+name, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	_ = p.Close()
	return true
}
