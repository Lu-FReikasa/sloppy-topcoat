package tools

import (
	"context"
	"strings"
	"testing"
)

func TestTailKeepsTheNewestWholeLines(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 100; i++ {
		b.WriteString("line-")
		b.WriteString(strings.Repeat("x", 8))
		b.WriteString("\n")
	}
	out, cut := tailBytes(b.String(), 50)
	if !cut || !strings.Contains(out, "earlier bytes left out") {
		t.Fatalf("%q", out)
	}
	body := out[strings.Index(out, "\n")+1:]
	for _, l := range strings.Split(strings.TrimSpace(body), "\n") {
		if l != "line-xxxxxxxx" {
			t.Fatalf("a line was cut in half: %q", l)
		}
	}
	if same, cut := tailBytes("short", 50); same != "short" || cut {
		t.Fatal("short text must not change")
	}
}

func TestHeadCutsAtARuneBoundary(t *testing.T) {
	out, cut := headBytes("ääääääääää", 5) // each ä is two bytes
	if !cut || !strings.HasPrefix(out, "ää") || strings.ContainsRune(out, '�') {
		t.Fatalf("%q", out)
	}
}

func TestTheRealCommanderRunsOnlyWhatIsAllowed(t *testing.T) {
	c := ExecCommander{}
	for _, bin := range []string{"sh", "bash", "docker", "/bin/ls", "rm", ""} {
		if _, err := c.Run(context.Background(), bin, "-c", "true"); err == nil || !strings.Contains(err.Error(), "does not run") {
			t.Errorf("%q: %v", bin, err)
		}
	}
}
