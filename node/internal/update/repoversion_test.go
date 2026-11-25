package update

import (
	"os"
	"strings"
	"testing"
)

// The VERSION file at the root of the repository is the one source of the node's version
// (scripts/node-version.sh): it must be a release number the update channel accepts.
func TestRepositoryVersionIsAReleaseNumber(t *testing.T) {
	raw, err := os.ReadFile("../../../VERSION")
	if err != nil {
		t.Fatalf("VERSION file: %v", err)
	}
	text := strings.TrimSpace(string(raw))
	v, err := ParseVersion(text)
	if err != nil {
		t.Fatalf("VERSION %q: %v", text, err)
	}
	if v.Pre != "" {
		t.Fatalf("VERSION %q has a pre-release part; releases are plain MAJOR.MINOR.PATCH", text)
	}
	// a working-copy build (VERSION-dev.<sha>) is older than the release, so a published release
	// always replaces it
	newer, err := Newer(text, text+"-dev.1a2b3c4.dirty")
	if err != nil || !newer {
		t.Fatalf("a release must be newer than its dev builds: newer=%v err=%v", newer, err)
	}
}
