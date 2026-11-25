package logfile

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRotation(t *testing.T) {
	p := filepath.Join(t.TempDir(), "logs", "node.log")
	w, err := Open(p, 10, 2)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{"aaaaaaaa\n", "bbbbbbbb\n", "cccccccc\n", "dddddddd\n"} {
		if _, err := w.Write([]byte(s)); err != nil {
			t.Fatal(err)
		}
	}
	_ = w.Close()
	read := func(p string) string { b, _ := os.ReadFile(p); return strings.TrimSpace(string(b)) }
	if read(p) != "dddddddd" || read(p+".1") != "cccccccc" || read(p+".2") != "bbbbbbbb" {
		t.Fatalf("%q %q %q", read(p), read(p+".1"), read(p+".2"))
	}
	if _, err := os.Stat(p + ".3"); !os.IsNotExist(err) {
		t.Fatal("kept too many")
	}
	if _, err := w.Write([]byte("x")); err == nil {
		t.Fatal("write after close")
	}
}
