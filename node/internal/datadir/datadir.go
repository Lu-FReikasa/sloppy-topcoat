// Package datadir resolves the node's data directory.
package datadir

import (
	"os"
	"path/filepath"
	"runtime"
)

// EnvVar overrides the platform default data directory.
const EnvVar = "TOPCOAT_NODE_DATA_DIR"

const dirName = "topcoat-node"

// env abstracts the process environment for tests.
type env struct {
	goos    string
	getenv  func(string) string
	euid    int
	homeDir func() (string, error)
}

func osEnv() env {
	return env{goos: runtime.GOOS, getenv: os.Getenv, euid: os.Geteuid(), homeDir: os.UserHomeDir}
}

// Resolve returns the data dir: flagValue if set, else $TOPCOAT_NODE_DATA_DIR, else the platform default.
func Resolve(flagValue string) string {
	return resolve(flagValue, osEnv())
}

// Default returns the platform default data dir (ignoring flag and env override).
func Default() string {
	return platformDefault(osEnv())
}

func resolve(flagValue string, e env) string {
	if flagValue != "" {
		return flagValue
	}
	if v := e.getenv(EnvVar); v != "" {
		return v
	}
	return platformDefault(e)
}

func platformDefault(e env) string {
	if e.goos == "windows" {
		base := e.getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		// Built with explicit separator so the result is correct even when resolved on another OS in tests.
		return base + `\` + dirName
	}
	if e.euid == 0 {
		return "/var/lib/" + dirName
	}
	if x := e.getenv("XDG_STATE_HOME"); x != "" {
		return filepath.Join(x, dirName)
	}
	home, err := e.homeDir()
	if err != nil || home == "" {
		return filepath.Join(os.TempDir(), dirName)
	}
	return filepath.Join(home, ".local", "state", dirName)
}
