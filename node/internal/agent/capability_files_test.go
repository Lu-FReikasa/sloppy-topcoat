package agent

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// HOST-01: files.read exists only where folders are released, and runs only when the hub's config
// lists it: a node refuses a call that is not in its whitelist, whatever the roots are.
func TestFilesReadIsOfferedOnlyWithRootsAndRunsOnlyWhenWhitelisted(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a.log"), []byte("hello\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	reg := func(roots []string) func(*Options) {
		return func(o *Options) {
			o.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry {
				return tools.NewRegistry(tools.Options{ReadRoots: roots})
			}
		}
	}
	file := strconv.Quote(filepath.Join(dir, "a.log"))
	// no roots: not offered at all
	_, _, inv := toolsSetup(t, toolsConfig(`"files.read"`, `"capability.invoke"`), reg(nil))
	if names := inventoryNames(inv); names["files.read"] || names["files.list"] {
		t.Fatalf("offered without roots: %v", names)
	}
	// roots, but not whitelisted in capabilities: refused
	_, hc, inv := toolsSetup(t, toolsConfig(`"docker.ps"`, `"capability.invoke"`), reg([]string{dir}))
	if names := inventoryNames(inv); !names["files.read"] || !names["files.list"] {
		t.Fatalf("not offered with roots: %v", names)
	}
	res := invokeCap(t, hc, "files.read", `{"path":`+file+`}`)
	if res.OK || res.Error == nil || res.Error.Code != protocol.ErrNotAllowed {
		t.Fatalf("a call that is not whitelisted ran: %+v", res)
	}
	// roots and whitelisted: runs
	_, hc, _ = toolsSetup(t, toolsConfig(`"files.read"`, `"capability.invoke"`), reg([]string{dir}))
	if res := invokeCap(t, hc, "files.read", `{"path":`+file+`}`); !res.OK {
		t.Fatalf("%+v", res.Error)
	}
}
