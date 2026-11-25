package watcher

import (
	"os"
	"path/filepath"
	"strings"
)

// listProcesses reads /proc. Processes of other users whose exe link cannot
// be read are reported by comm name only (the service runs as its own user;
// name rules still match them, path rules need the path).
func listProcesses() ([]Proc, error) {
	return listProc("/proc")
}

func listProc(root string) ([]Proc, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []Proc
	for _, e := range entries {
		if !e.IsDir() || !isPID(e.Name()) {
			continue
		}
		dir := filepath.Join(root, e.Name())
		p := Proc{}
		if exe, err := os.Readlink(filepath.Join(dir, "exe")); err == nil {
			p.Path = strings.TrimSuffix(exe, " (deleted)")
			p.Name = filepath.Base(p.Path)
		} else if comm, err := os.ReadFile(filepath.Join(dir, "comm")); err == nil {
			p.Name = strings.TrimSpace(string(comm))
		} else {
			continue // process ended meanwhile
		}
		out = append(out, p)
	}
	return out, nil
}

func isPID(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
