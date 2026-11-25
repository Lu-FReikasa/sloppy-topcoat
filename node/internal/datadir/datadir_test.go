package datadir

import (
	"errors"
	"path/filepath"
	"testing"
)

func fakeEnv(goos string, euid int, vars map[string]string, home string) env {
	return env{
		goos:   goos,
		euid:   euid,
		getenv: func(k string) string { return vars[k] },
		homeDir: func() (string, error) {
			if home == "" {
				return "", errors.New("no home")
			}
			return home, nil
		},
	}
}

func TestResolve(t *testing.T) {
	cases := []struct {
		name string
		flag string
		e    env
		want string
	}{
		{"flag wins", "/x", fakeEnv("linux", 1000, map[string]string{EnvVar: "/y"}, "/home/u"), "/x"},
		{"env wins over default", "", fakeEnv("linux", 1000, map[string]string{EnvVar: "/y"}, "/home/u"), "/y"},
		{"linux root", "", fakeEnv("linux", 0, nil, "/root"), "/var/lib/topcoat-node"},
		{"linux xdg", "", fakeEnv("linux", 1000, map[string]string{"XDG_STATE_HOME": "/s"}, "/home/u"), filepath.Join("/s", "topcoat-node")},
		{"linux home", "", fakeEnv("linux", 1000, nil, "/home/u"), filepath.Join("/home/u", ".local", "state", "topcoat-node")},
		{"windows programdata", "", fakeEnv("windows", -1, map[string]string{"ProgramData": `D:\PD`}, ""), `D:\PD\topcoat-node`},
		{"windows fallback", "", fakeEnv("windows", -1, nil, ""), `C:\ProgramData\topcoat-node`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := resolve(c.flag, c.e); got != c.want {
				t.Fatalf("got %q want %q", got, c.want)
			}
		})
	}
}
