//go:build unix

package store

import (
	"os"
	"testing"
)

func TestRewritingTheIdentityKeepsItsOwner(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root to hand a file to another account")
	}
	d, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := d.SaveIdentity(Identity{HubURL: "https://a.example.lan", NodeID: "n"}); err != nil {
		t.Fatal(err)
	}
	// the service account owns the file (install hands the data dir over)
	if err := os.Lchown(d.file(IdentityFile), 12345, 23456); err != nil {
		t.Fatal(err)
	}
	// `sudo topcoat-node set-hub` rewrites it as root
	if err := d.SaveIdentity(Identity{HubURL: "https://b.example.lan", NodeID: "n"}); err != nil {
		t.Fatal(err)
	}
	uid, gid, ok := ownerOf(d.file(IdentityFile))
	if !ok || uid != 12345 || gid != 23456 {
		t.Fatalf("owner %d:%d, the service account must still own its node.json", uid, gid)
	}
	if id, _ := d.LoadIdentity(); id.HubURL != "https://b.example.lan" {
		t.Fatalf("%+v", id)
	}
}
