package netinfo

import (
	"net"
	"reflect"
	"testing"
)

func ipnet(s string) net.Addr {
	ip, n, _ := net.ParseCIDR(s)
	n.IP = ip
	return n
}

func TestAddressesOf(t *testing.T) {
	up := net.FlagUp
	got := addressesOf([]ifaceAddrs{
		{Name: "lo", Flags: up | net.FlagLoopback, Addrs: []net.Addr{ipnet("127.0.0.1/8"), ipnet("::1/128")}},
		{Name: "eth0", Flags: up, Addrs: []net.Addr{ipnet("10.0.0.20/24"), ipnet("fe80::1/64"), ipnet("169.254.3.4/16"), ipnet("fd00::20/64")}},
		{Name: "docker0", Flags: up, Addrs: []net.Addr{ipnet("172.17.0.1/16")}},
		{Name: "tailscale0", Flags: up, Addrs: []net.Addr{ipnet("100.100.9.7/32")}},
		{Name: "eth1", Flags: 0, Addrs: []net.Addr{ipnet("10.9.9.9/24")}},
		{Name: "eth2", Flags: up, Addrs: []net.Addr{&net.IPAddr{IP: net.ParseIP("10.0.0.20")}, &net.IPAddr{IP: net.ParseIP("::ffff:10.0.0.21")}}},
	})
	want := []string{"10.0.0.20", "10.0.0.21", "100.100.9.7", "fd00::20"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
