package service

import (
	"bytes"
	"errors"
	"strings"
	"testing"
)

func TestFirewallAddArgs(t *testing.T) {
	got := strings.Join(firewallAddArgs(`C:\Program Files\topcoat-node\topcoat-node.exe`, 47000, 47099), "|")
	for _, want := range []string{"name=topcoat-node-transfer", "dir=in", "action=allow", "profile=private,domain",
		"remoteip=LocalSubnet", "protocol=TCP", "localport=47000-47099", `program=C:\Program Files\topcoat-node\topcoat-node.exe`} {
		if !strings.Contains(got, want) {
			t.Fatalf("%q missing in %s", want, got)
		}
	}
}

func TestEnsureTransferFirewallRule(t *testing.T) {
	var calls [][]string
	var buf bytes.Buffer
	ensureTransferFirewallRule(func(args ...string) ([]byte, error) {
		calls = append(calls, args)
		return nil, nil
	}, `C:\p\topcoat-node.exe`, &buf)
	if len(calls) != 2 || calls[0][2] != "delete" || calls[1][2] != "add" {
		t.Fatalf("%v", calls)
	}
	if !strings.Contains(buf.String(), "47000-47099") {
		t.Fatal(buf.String())
	}

	// A failure is reported with the manual command and is not an error.
	buf.Reset()
	ensureTransferFirewallRule(func(args ...string) ([]byte, error) {
		if args[2] == "add" {
			return []byte("access denied"), errors.New("exit status 1")
		}
		return nil, nil
	}, `C:\Program Files\x\topcoat-node.exe`, &buf)
	for _, want := range []string{"could not add the firewall rule", "access denied", `program="C:\Program Files\x\topcoat-node.exe"`} {
		if !strings.Contains(buf.String(), want) {
			t.Fatalf("%q missing in %s", want, buf.String())
		}
	}
}

func TestRemoveTransferFirewallRule(t *testing.T) {
	var buf bytes.Buffer
	removeTransferFirewallRule(func(args ...string) ([]byte, error) { return nil, nil }, &buf)
	if !strings.Contains(buf.String(), "removed firewall rule") {
		t.Fatal(buf.String())
	}
	buf.Reset()
	removeTransferFirewallRule(func(args ...string) ([]byte, error) { return nil, errors.New("none") }, &buf)
	if buf.Len() != 0 {
		t.Fatal(buf.String())
	}
}

func TestLinuxFirewallNote(t *testing.T) {
	var buf bytes.Buffer
	linuxFirewallNote(&buf)
	if !strings.Contains(buf.String(), "47000-47099") || !strings.Contains(buf.String(), "changes no firewall rules") {
		t.Fatal(buf.String())
	}
}
