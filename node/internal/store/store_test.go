package store

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/protocol"
)

func checkMode(t *testing.T, path string, want os.FileMode) {
	t.Helper()
	if runtime.GOOS == "windows" {
		return
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != want {
		t.Fatalf("%s mode %v want %v", path, st.Mode().Perm(), want)
	}
}

func TestIdentityKeyAndConfig(t *testing.T) {
	d, err := Open(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	checkMode(t, d.Path, 0o700)
	if _, err := d.LoadIdentity(); !errors.Is(err, ErrNotPaired) {
		t.Fatalf("got %v", err)
	}
	if d.HasIdentity() {
		t.Fatal("identity reported before save")
	}
	id := Identity{HubURL: "https://hub.example.lan:8443", NodeID: "n"}
	if err := d.SaveIdentity(id); err != nil {
		t.Fatal(err)
	}
	checkMode(t, filepath.Join(d.Path, IdentityFile), 0o600)
	got, err := d.LoadIdentity()
	if err != nil || got != id {
		t.Fatalf("%+v %v", got, err)
	}
	if err := os.WriteFile(filepath.Join(d.Path, IdentityFile), []byte(`{"hub_url":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := d.LoadIdentity(); err == nil {
		t.Fatal("incomplete identity accepted")
	}

	_, priv, _ := ed25519.GenerateKey(rand.Reader)
	if err := d.SavePrivateKey(priv); err != nil {
		t.Fatal(err)
	}
	checkMode(t, filepath.Join(d.Path, KeyFile), 0o600)
	k, err := d.LoadPrivateKey()
	if err != nil || !k.Equal(priv) {
		t.Fatalf("key round trip %v", err)
	}

	if _, ok, err := d.LoadConfig(); ok || err != nil {
		t.Fatal("config before save")
	}
	if err := d.SaveConfig("h", json.RawMessage(`{"a":1}`), time.Now()); err != nil {
		t.Fatal(err)
	}
	checkMode(t, filepath.Join(d.Path, ConfigFile), 0o600)
	cc, ok, err := d.LoadConfig()
	if err != nil || !ok || cc.ConfigHash != "h" || string(cc.Config) != `{"a":1}` {
		t.Fatalf("%+v %v %v", cc, ok, err)
	}
	if _, err := Open(""); err == nil {
		t.Fatal("empty path accepted")
	}
	// No temp files left behind.
	entries, _ := os.ReadDir(d.Path)
	for _, e := range entries {
		if strings.Contains(e.Name(), ".tmp-") {
			t.Fatalf("temp file left: %s", e.Name())
		}
	}
}

func ev(i int) protocol.Envelope {
	e, _ := protocol.New(protocol.TypeEvent, protocol.Event{Kind: "game_detected", Data: map[string]any{"i": i}}, time.Now())
	return e
}

func TestQueuePersistenceAndCap(t *testing.T) {
	path := filepath.Join(t.TempDir(), QueueFile)
	q, skipped, err := OpenQueue(path, 5)
	if err != nil || skipped != 0 {
		t.Fatal(err)
	}
	var ids []string
	for i := 0; i < 7; i++ {
		e := ev(i)
		ids = append(ids, e.ID)
		if err := q.Append(e); err != nil {
			t.Fatal(err)
		}
	}
	if q.Len() != 5 || q.Dropped() != 2 {
		t.Fatalf("len %d dropped %d", q.Len(), q.Dropped())
	}
	checkMode(t, path, 0o600)
	// Reopen: oldest two dropped, order kept.
	q2, _, err := OpenQueue(path, 5)
	if err != nil {
		t.Fatal(err)
	}
	snap := q2.Snapshot()
	for i, e := range snap {
		if e.ID != ids[i+2] {
			t.Fatalf("pos %d: %s want %s", i, e.ID, ids[i+2])
		}
	}
	if err := q2.DropFirst(3); err != nil {
		t.Fatal(err)
	}
	if err := q2.DropFirst(0); err != nil {
		t.Fatal(err)
	}
	q3, _, _ := OpenQueue(path, 5)
	if q3.Len() != 2 || q3.Snapshot()[0].ID != ids[5] {
		t.Fatalf("after drop %d", q3.Len())
	}
	if err := q3.DropFirst(99); err != nil || q3.Len() != 0 {
		t.Fatal("drop all")
	}
}

func TestQueueDefaultCapAndCorruptLines(t *testing.T) {
	path := filepath.Join(t.TempDir(), QueueFile)
	var b strings.Builder
	for i := 0; i < DefaultQueueCap+10; i++ {
		raw, _ := json.Marshal(ev(i))
		b.Write(raw)
		b.WriteByte('\n')
		if i == 3 {
			b.WriteString("not json\n\n{\"type\":\"heartbeat\",\"id\":\"x\"}\n")
		}
	}
	if err := os.WriteFile(path, []byte(b.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	q, skipped, err := OpenQueue(path, 0)
	if err != nil {
		t.Fatal(err)
	}
	if skipped != 2 || q.Len() != DefaultQueueCap || q.Dropped() != 10 {
		t.Fatalf("skipped %d len %d dropped %d", skipped, q.Len(), q.Dropped())
	}
	var qe struct{ Events []protocol.Envelope }
	qe.Events = q.Snapshot()
	if len(qe.Events) > 1000 {
		t.Fatal("more than maxItems")
	}
	if _, _, err := OpenQueue(filepath.Join(t.TempDir(), "missing", "q"), 0); err != nil {
		t.Fatalf("missing file should be an empty queue: %v", err)
	}
	if err := WriteFileAtomic(filepath.Join(t.TempDir(), "nodir", "x"), []byte("x")); err == nil {
		t.Fatal("write into missing dir succeeded")
	}
}
