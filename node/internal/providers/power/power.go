// Package power puts the machine to sleep (E2-05 E): the node.sleep action and the idle sleep of
// config.power.sleep_after_idle_seconds. Coming back is Wake-on-LAN (the hub sends the magic
// packet to the addresses of the inventory), so sleeping is only offered as an explicit choice.
package power

import "context"

// Sleeper suspends the machine (RAM stays powered; no hibernation).
type Sleeper interface {
	// Sleep returns once the request was accepted; the machine goes to sleep right after.
	Sleep(ctx context.Context) error
}
