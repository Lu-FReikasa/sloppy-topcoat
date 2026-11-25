package service

import (
	"encoding/xml"
	"strings"
	"testing"
	"unicode/utf16"
)

func TestTheInputHelperTaskRunsInTheUsersSessionWithoutAPassword(t *testing.T) {
	x := InputHelperTaskXML(`C:\Program Files\topcoat-node\topcoat-node.exe`)
	var task struct {
		Triggers struct {
			Logon []struct{ Enabled string } `xml:"LogonTrigger"`
		}
		Principals struct {
			P []struct {
				ID       string `xml:"id,attr"`
				GroupID  string `xml:"GroupId"`
				UserID   string `xml:"UserId"`
				RunLevel string
			} `xml:"Principal"`
		}
		Actions struct {
			Context string `xml:"Context,attr"`
			Exec    []struct{ Command, Arguments string }
		}
	}
	// the declaration says UTF-16 (the file is written that way); parse the text without it
	body := x[strings.Index(x, "?>")+2:]
	if err := xml.Unmarshal([]byte(body), &task); err != nil {
		t.Fatalf("not well-formed: %v\n%s", err, x)
	}
	if len(task.Triggers.Logon) != 1 || len(task.Principals.P) != 1 || len(task.Actions.Exec) != 1 {
		t.Fatalf("%+v", task)
	}
	p := task.Principals.P[0]
	if p.GroupID != "S-1-5-32-545" || p.UserID != "" || p.RunLevel != "LeastPrivilege" {
		t.Fatalf("principal %+v", p)
	}
	if task.Actions.Context != p.ID {
		t.Fatalf("the action must run as the principal: %q vs %q", task.Actions.Context, p.ID)
	}
	e := task.Actions.Exec[0]
	if e.Command != `C:\Program Files\topcoat-node\topcoat-node.exe` || e.Arguments != "input-helper" {
		t.Fatalf("%+v", e)
	}
	// schtasks refused a LogonType next to a GroupId on the real laptop: the principal is exactly
	// these two elements (and Settings/Actions hold nothing but what Task Scheduler itself writes)
	if got := childNames(t, body, "Principal"); strings.Join(got, ",") != "GroupId,RunLevel" {
		t.Fatalf("principal children: %v", got)
	}
	if got := childNames(t, body, "Settings"); strings.Join(got, ",") != "MultipleInstancesPolicy,DisallowStartIfOnBatteries,StopIfGoingOnBatteries,AllowHardTerminate,StartWhenAvailable,AllowStartOnDemand,Enabled,Hidden,ExecutionTimeLimit,Priority" {
		t.Fatalf("settings children: %v", got)
	}
	for _, bad := range []string{"S4U", "Password", "HighestAvailable", "LogonType"} {
		if strings.Contains(x, bad) {
			t.Errorf("the task must not contain %q", bad)
		}
	}
}

func TestThePathInTheTaskIsEscaped(t *testing.T) {
	x := InputHelperTaskXML(`C:\R&D <x>\topcoat-node.exe`)
	if !strings.Contains(x, `C:\R&amp;D &lt;x&gt;\topcoat-node.exe`) {
		t.Fatal(x)
	}
}

func TestTheTaskFileIsUTF16WithABOM(t *testing.T) {
	b := utf16LEWithBOM("<a>ä€</a>")
	if b[0] != 0xFF || b[1] != 0xFE || len(b)%2 != 0 {
		t.Fatalf("% x", b)
	}
	u := make([]uint16, 0, len(b)/2)
	for i := 2; i < len(b); i += 2 {
		u = append(u, uint16(b[i])|uint16(b[i+1])<<8)
	}
	if got := string(utf16.Decode(u)); got != "<a>ä€</a>" {
		t.Fatal(got)
	}
}

// childNames lists the element names directly inside the first element called parent.
func childNames(t *testing.T, doc, parent string) []string {
	t.Helper()
	dec := xml.NewDecoder(strings.NewReader(doc))
	var names []string
	depth, inside := 0, false
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		switch e := tok.(type) {
		case xml.StartElement:
			if inside && depth == 1 {
				names = append(names, e.Name.Local)
			}
			if !inside && e.Name.Local == parent {
				inside = true
				depth = 0
			}
			if inside {
				depth++
			}
		case xml.EndElement:
			if inside {
				depth--
				if depth == 0 {
					return names
				}
			}
		}
	}
	return names
}
