package service

import (
	"fmt"
	"io"
	"strconv"
	"strings"

	"topcoat/node/internal/protocol"
)

// Direct model transfer and the firewall (E2-07, D51). The node opens one TCP port of a fixed
// range, and only while a model is being sent (models.send). Binding the port is not enough on
// Windows: the first bind of an unknown program raises the firewall prompt, and a service has
// nobody to answer it. So the installer adds one inbound rule for the program, profile Private and Domain,
// remote addresses "LocalSubnet", for the default range. A node that follows another range
// (config.transfer.port_min / port_max from the hub) needs the rule changed by hand.
//
// UNVERIFIED on real hardware: the netsh calls and their quoting are untested on Windows.

// FirewallRuleName is the Windows firewall rule of the transfer port range.
const FirewallRuleName = "topcoat-node-transfer"

// netshRunner runs netsh.exe with arguments (a fake in tests).
type netshRunner func(args ...string) ([]byte, error)

// firewallAddArgs is the netsh command that opens the transfer range for the program on
// private and domain networks, from the local subnet only.
func firewallAddArgs(program string, portMin, portMax int) []string {
	return []string{"advfirewall", "firewall", "add", "rule",
		"name=" + FirewallRuleName,
		"dir=in", "action=allow", "enable=yes",
		"program=" + program,
		"profile=private,domain",
		"remoteip=LocalSubnet",
		"protocol=TCP",
		"localport=" + strconv.Itoa(portMin) + "-" + strconv.Itoa(portMax),
	}
}

func firewallDeleteArgs() []string {
	return []string{"advfirewall", "firewall", "delete", "rule", "name=" + FirewallRuleName}
}

// ensureTransferFirewallRule replaces the rule with one for program (best effort: a failure is
// reported on w with the manual command, never returned as an install failure).
func ensureTransferFirewallRule(run netshRunner, program string, w io.Writer) {
	portMin, portMax := protocol.DefaultTransferPortMin, protocol.DefaultTransferPortMax
	_, _ = run(firewallDeleteArgs()...) // none yet is fine; an old rule may point to another program path
	add := firewallAddArgs(program, portMin, portMax)
	if out, err := run(add...); err != nil {
		fmt.Fprintf(w, "could not add the firewall rule for direct model transfer: %v: %s\n"+
			"  Models can still be received. To send models to other nodes, run in an elevated shell:\n"+
			"  netsh %s\n", err, strings.TrimSpace(string(out)), strings.Join(quoteForDisplay(add), " "))
		return
	}
	fmt.Fprintf(w, "firewall rule %s: TCP %d-%d from the local subnet, private and domain networks only (opened by the node only while a model is being sent)\n",
		FirewallRuleName, portMin, portMax)
}

// removeTransferFirewallRule deletes the rule; none is fine.
func removeTransferFirewallRule(run netshRunner, w io.Writer) {
	if _, err := run(firewallDeleteArgs()...); err == nil {
		fmt.Fprintf(w, "removed firewall rule %s\n", FirewallRuleName)
	}
}

func quoteForDisplay(args []string) []string {
	out := make([]string, len(args))
	for i, a := range args {
		if k, v, ok := strings.Cut(a, "="); ok && strings.ContainsAny(v, " \t") {
			a = k + `="` + v + `"`
		}
		out[i] = a
	}
	return out
}

// linuxFirewallNote tells what a firewall on Linux has to allow; the installer changes no rules.
func linuxFirewallNote(w io.Writer) {
	fmt.Fprintf(w, "direct model transfer: while a model is being sent the node listens on one TCP port of %d-%d (nothing listens otherwise).\n"+
		"  If a firewall is active, allow that range from your network, e.g.: ufw allow from 10.0.0.0/24 to any port %d:%d proto tcp\n"+
		"  (the installer changes no firewall rules)\n",
		protocol.DefaultTransferPortMin, protocol.DefaultTransferPortMax,
		protocol.DefaultTransferPortMin, protocol.DefaultTransferPortMax)
}
