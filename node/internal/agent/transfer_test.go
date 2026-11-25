package agent

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/fake"
)

const (
	xferToken = "tttttttttttttttttttttttttttttttttttttttttt1" // 43 characters, not a real secret
	xferID    = "3c1f0a52-8f4e-4d2b-9a07-5b6c1e2d4f90"
)

func xferConfig(actions string) string {
	return `{"roles":["gpu"],"runner":{"type":"fake"},"allowed_actions":[` + actions + `],` +
		`"heartbeat_interval_seconds":10,"idle_ttl_seconds":0,` +
		`"watcher":{"grace_seconds":0,"cooldown_seconds":0,"rules":[{"process":"game.exe"}]},"capabilities":[]}`
}

var xferAllowed = xferConfig(`"models.list","models.send","models.receive"`)

type xferNode struct {
	h   *harness
	hc  *hubConn
	dir string
}

// newXferNode starts one node with a fake runner whose models live in a real directory.
func newXferNode(t *testing.T, cfg string, freeBytes int64) *xferNode {
	t.Helper()
	hub := newFakeHub(t)
	dir := t.TempDir()
	h := newHarness(t, hub.srv.URL, t.TempDir(), fake.DefaultProfile(), func(o *Options) {
		o.Addresses = func() []string { return []string{"127.0.0.1"} }
		o.DiskFree = func(string) (diskfree.Usage, error) {
			return diskfree.Usage{TotalBytes: 1 << 40, FreeBytes: freeBytes}, nil
		}
	})
	h.world.ModelsDir = dir
	h.start(t)
	hc := hub.accept(t)
	hc.handshake(t, cfg)
	return &xferNode{h: h, hc: hc, dir: dir}
}

func writeModel(t *testing.T, dir, name string, size int) (data []byte, sum string) {
	t.Helper()
	data = make([]byte, size)
	for i := range data {
		data[i] = byte(i*13 + i/997)
	}
	if err := os.WriteFile(filepath.Join(dir, name+".gguf"), data, 0o600); err != nil {
		t.Fatal(err)
	}
	s := sha256.Sum256(data)
	return data, hex.EncodeToString(s[:])
}

func sendArgs(model string) string {
	return fmt.Sprintf(`{"transfer_id":%q,"model":%q,"token":%q,"peer_addresses":["127.0.0.1"]}`, xferID, model, xferToken)
}

func receiveArgs(model string, size int, sum, cert string, endpoints ...string) string {
	eps := `"` + strings.Join(endpoints, `","`) + `"`
	return fmt.Sprintf(`{"transfer_id":%q,"model":%q,"size":%d,"sha256":%q,"token":%q,"cert_sha256":%q,"endpoints":[%s]}`,
		xferID, model, size, sum, xferToken, cert, eps)
}

// waitTransfer waits for a transfer_progress event of the given role and status.
func waitTransfer(t *testing.T, hc *hubConn, role, status string) map[string]any {
	t.Helper()
	for {
		d := hc.waitEvent(t, protocol.EventTransferProgress)
		if d["role"] == role && d["status"] == status {
			return d
		}
	}
}

func TestModelTransferBetweenTwoAgents(t *testing.T) {
	src := newXferNode(t, xferAllowed, 100<<30)
	dst := newXferNode(t, xferAllowed, 100<<30)
	data, sum := writeModel(t, src.dir, "shared-model", 3<<20+17)

	res := src.hc.action(t, "models.send", sendArgs("shared-model"))
	if !res.OK || res.Result.(map[string]any)["started"] != true {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	ready := src.hc.waitEvent(t, protocol.EventTransferReady)
	if ready["transfer_id"] != xferID || ready["model"] != "shared-model" || ready["sha256"] != sum ||
		ready["size"] != float64(len(data)) || len(ready["cert_sha256"].(string)) != 64 {
		t.Fatalf("%v", ready)
	}
	port := int(ready["port"].(float64))
	if port < 47000 || port > 47099 {
		t.Fatalf("port %d outside the default range", port)
	}
	if strings.Contains(src.h.logs.String(), xferToken) || strings.Contains(dst.h.logs.String(), xferToken) {
		t.Fatal("the token is in the log")
	}

	// A second send while the socket is open is busy.
	if res := src.hc.action(t, "models.send", sendArgs("shared-model")); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("second send: %+v", res)
	}

	ep := fmt.Sprintf("127.0.0.1:%d", port)
	res = dst.hc.action(t, "models.receive", receiveArgs("copied-model", len(data), sum, ready["cert_sha256"].(string), ep))
	if !res.OK || res.Result.(map[string]any)["model"] != "copied-model" {
		t.Fatalf("%+v %+v", res, res.Error)
	}
	if d := waitTransfer(t, dst.hc, "receive", "done"); d["transferred_bytes"] != float64(len(data)) || d["total_bytes"] != float64(len(data)) {
		t.Fatalf("%v", d)
	}
	waitTransfer(t, src.hc, "send", "done")
	inv := dst.hc.waitType(t, protocol.TypeInventory)
	if !strings.Contains(string(inv.Body), "copied-model") {
		t.Fatal("inventory of the target lacks the model")
	}
	got, err := os.ReadFile(filepath.Join(dst.dir, "copied-model.gguf"))
	if err != nil || !bytes.Equal(got, data) {
		t.Fatalf("copied file differs (%v)", err)
	}
	if _, err := os.Stat(filepath.Join(dst.dir, "copied-model.gguf.part")); !os.IsNotExist(err) {
		t.Fatal(".part left behind")
	}
	eventually(t, "socket closed", func() bool {
		c, err := net.DialTimeout("tcp", ep, 200*time.Millisecond)
		if err == nil {
			c.Close()
			return false
		}
		return true
	})
	// The source is free again, and the name is taken on the target.
	eventually(t, "source idle", func() bool {
		src.h.agent.mu.Lock()
		defer src.h.agent.mu.Unlock()
		return src.h.agent.xfer == nil
	})
	res = dst.hc.action(t, "models.receive", receiveArgs("copied-model", len(data), sum, strings.Repeat("b", 64), ep))
	if res.OK || res.Error.Code != protocol.ErrInvalidArgs {
		t.Fatalf("duplicate: %+v", res)
	}
}

func TestModelTransferFailures(t *testing.T) {
	src := newXferNode(t, xferAllowed, 100<<30)
	dst := newXferNode(t, xferAllowed, 100<<30)
	data, sum := writeModel(t, src.dir, "m", 200000)
	_ = data

	res := src.hc.action(t, "models.send", sendArgs("m"))
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	ready := src.hc.waitEvent(t, protocol.EventTransferReady)
	ep := fmt.Sprintf("127.0.0.1:%d", int(ready["port"].(float64)))
	cert := ready["cert_sha256"].(string)

	// Wrong fingerprint: no route (the socket stays open for the right target).
	if res := dst.hc.action(t, "models.receive", receiveArgs("m1", 200000, sum, strings.Repeat("0", 64), ep)); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if d := waitTransfer(t, dst.hc, "receive", "failed"); d["error_code"] != "no_route" {
		t.Fatalf("%v", d)
	}
	// A refusal by the source (wrong token) is an internal error with a clear message.
	args := strings.Replace(receiveArgs("m3", 200000, sum, cert, ep), xferToken, strings.Repeat("x", 43), 1)
	if res := dst.hc.action(t, "models.receive", args); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if d := waitTransfer(t, dst.hc, "receive", "failed"); d["error_code"] != "internal" || !strings.Contains(d["error"].(string), "401") {
		t.Fatalf("%v", d)
	}
	// A hash that differs from the one the source announces (last: the source counts this
	// request as delivered and closes).
	if res := dst.hc.action(t, "models.receive", receiveArgs("m2", 200000, strings.Repeat("a", 64), cert, ep)); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	if d := waitTransfer(t, dst.hc, "receive", "failed"); d["error_code"] != "hash_mismatch" {
		t.Fatalf("%v", d)
	}
	if _, err := os.Stat(filepath.Join(dst.dir, "m2.gguf")); !os.IsNotExist(err) {
		t.Fatal("a file exists after a hash mismatch")
	}
	// Nothing was leaked into the events.
	if strings.Contains(src.h.logs.String()+dst.h.logs.String(), xferToken) {
		t.Fatal("the token is in the log")
	}
}

func TestModelTransferRefusals(t *testing.T) {
	disk := newXferNode(t, xferAllowed, 500<<20) // 500 MiB free
	writeModel(t, disk.dir, "have", 1000)
	endpoint := "127.0.0.1:47000"
	cert := strings.Repeat("c", 64)
	sum := strings.Repeat("a", 64)

	// Not enough room: size + 1 GiB.
	if res := disk.hc.action(t, "models.receive", receiveArgs("big", 10<<20, sum, cert, endpoint)); res.OK || res.Error.Code != protocol.ErrInsufficientDisk {
		t.Fatalf("disk: %+v", res)
	}
	for name, args := range map[string]string{
		"exists":    receiveArgs("have", 10, sum, cert, endpoint),
		"bad name":  receiveArgs("../x", 10, sum, cert, endpoint),
		"bad sha":   receiveArgs("n", 10, "xyz", cert, endpoint),
		"bad size":  receiveArgs("n", 0, sum, cert, endpoint),
		"bad cert":  receiveArgs("n", 10, sum, "q", endpoint),
		"bad host":  receiveArgs("n", 10, sum, cert, "example.invalid:47000"),
		"bad port":  receiveArgs("n", 10, sum, cert, "127.0.0.1:70000"),
		"no ports":  receiveArgs("n", 10, sum, cert, "127.0.0.1"),
		"no ep":     `{"transfer_id":"` + xferID + `","model":"n","size":10,"sha256":"` + sum + `","token":"` + xferToken + `","cert_sha256":"` + cert + `","endpoints":[]}`,
		"extra":     strings.Replace(receiveArgs("n", 10, sum, cert, endpoint), `"size"`, `"path":"/etc","size"`, 1),
		"bad token": strings.Replace(receiveArgs("n", 10, sum, cert, endpoint), xferToken, "short", 1),
		"bad uuid":  strings.Replace(receiveArgs("n", 10, sum, cert, endpoint), xferID, "nope", 1),
	} {
		if res := disk.hc.action(t, "models.receive", args); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
			t.Fatalf("receive %s: %+v", name, res)
		}
	}
	for name, args := range map[string]string{
		"unknown":   sendArgs("nope"),
		"path":      sendArgs("../have"),
		"bad token": strings.Replace(sendArgs("have"), xferToken, "short", 1),
		"bad peer":  strings.Replace(sendArgs("have"), "127.0.0.1", "not-an-ip", 1),
		"extra":     strings.Replace(sendArgs("have"), `"model"`, `"file":"/etc/passwd","model"`, 1),
		"no peers":  `{"transfer_id":"` + xferID + `","model":"have","token":"` + xferToken + `"}`,
	} {
		if res := disk.hc.action(t, "models.send", args); res.OK || res.Error.Code != protocol.ErrInvalidArgs {
			t.Fatalf("send %s: %+v", name, res)
		}
	}
}

func TestModelTransferNotAllowed(t *testing.T) {
	n := newXferNode(t, xferConfig(`"models.list"`), 100<<30)
	writeModel(t, n.dir, "have", 1000)
	for _, c := range []struct{ name, args string }{
		{"models.send", sendArgs("have")},
		{"models.receive", receiveArgs("x", 10, strings.Repeat("a", 64), strings.Repeat("c", 64), "127.0.0.1:47000")},
	} {
		if res := n.hc.action(t, c.name, c.args); res.OK || res.Error.Code != protocol.ErrNotAllowed {
			t.Fatalf("%s: %+v", c.name, res)
		}
	}
}

func TestModelTransferBusyWhileGamingAndAbortedByGame(t *testing.T) {
	n := newXferNode(t, xferAllowed, 100<<30)
	writeModel(t, n.dir, "have", 100000)

	if res := n.hc.action(t, "models.send", sendArgs("have")); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	ready := n.hc.waitEvent(t, protocol.EventTransferReady)
	ep := fmt.Sprintf("127.0.0.1:%d", int(ready["port"].(float64)))

	n.h.watcher.Start("game.exe")
	n.hc.waitEvent(t, protocol.EventGameDetected)
	d := waitTransfer(t, n.hc, "send", "failed")
	if d["error_code"] != "canceled" {
		t.Fatalf("%v", d)
	}
	eventually(t, "socket closed", func() bool {
		c, err := net.DialTimeout("tcp", ep, 200*time.Millisecond)
		if err == nil {
			c.Close()
			return false
		}
		return true
	})
	if res := n.hc.action(t, "models.send", sendArgs("have")); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("send while gaming: %+v", res)
	}
	args := receiveArgs("new", 10, strings.Repeat("a", 64), strings.Repeat("c", 64), "127.0.0.1:47000")
	if res := n.hc.action(t, "models.receive", args); res.OK || res.Error.Code != protocol.ErrBusy {
		t.Fatalf("receive while gaming: %+v", res)
	}
}

func TestModelTransferTTLReportsTimeout(t *testing.T) {
	cfg := strings.Replace(xferAllowed, `"capabilities":[]`, `"capabilities":[],"transfer":{"port_min":47100,"port_max":47110,"ttl_seconds":30}`, 1)
	n := newXferNode(t, cfg, 100<<30)
	writeModel(t, n.dir, "have", 1000)
	if got := n.h.agent.transferSettings(); got.TTL != 30*time.Second || got.PortMin != 47100 {
		t.Fatalf("%+v", got)
	}
	if res := n.hc.action(t, "models.send", sendArgs("have")); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	ready := n.hc.waitEvent(t, protocol.EventTransferReady)
	if p := int(ready["port"].(float64)); p < 47100 || p > 47110 {
		t.Fatalf("port %d outside the configured range", p)
	}
	// Cancel through the agent (the 30 s TTL itself is covered by the transfer package).
	n.h.agent.abortTransfer("test")
	if d := waitTransfer(t, n.hc, "send", "failed"); d["error_code"] != "canceled" {
		t.Fatalf("%v", d)
	}
}

func TestInventoryReportsAddresses(t *testing.T) {
	n := newXferNode(t, xferAllowed, 100<<30)
	res := n.hc.action(t, "models.list", `{}`)
	if !res.OK {
		t.Fatalf("%+v", res.Error)
	}
	inv := n.hc.waitType(t, protocol.TypeInventory)
	if !strings.Contains(string(inv.Body), `"addresses":["127.0.0.1"]`) {
		t.Fatalf("%s", inv.Body)
	}
}
