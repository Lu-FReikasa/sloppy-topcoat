package protocol_test

import (
	"fmt"
	"testing"

	"topcoat/node/internal/protocol"
)

func TestReadRootsConfig(t *testing.T) {
	base := `{"roles":["tools"],"runner":{"type":"none"},"allowed_actions":[],"heartbeat_interval_seconds":10,
		"idle_ttl_seconds":0,"watcher":{"rules":[]},"capabilities":[],"tools":%s}`
	c, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `{"read_roots":["/var/log/topcoat","C:\\ProgramData\\Topcoat\\logs"]}`)))
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Tools.ReadRoots) != 2 {
		t.Fatalf("%+v", c.Tools)
	}
	other, _ := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, `{"read_roots":["/var/log/topcoat"]}`)))
	if c.Tools.Equal(other.Tools) || !c.Tools.Equal(c.Tools) {
		t.Fatal("a changed read_roots must count as a config change")
	}
	for _, bad := range []string{
		`{"read_roots":["relative/dir"]}`, `{"read_roots":["/"]}`, `{"read_roots":["C:\\"]}`, `{"read_roots":["C:/"]}`,
		`{"read_roots":["//server/share"]}`, `{"read_roots":["\\\\server\\share"]}`, `{"read_roots":["\\\\?\\C:\\x"]}`,
		`{"read_roots":["/var/../etc"]}`, `{"read_roots":["C:\\a\\..\\b"]}`, `{"read_roots":["C:\\a:stream"]}`,
		`{"read_roots":[""]}`, `{"read_roots":"/var/log"}`,
	} {
		if _, err := protocol.ParseNodeConfig([]byte(fmt.Sprintf(base, bad))); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}
