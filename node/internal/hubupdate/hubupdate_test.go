package hubupdate

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"topcoat/node/internal/providers/capability"
)

const digest = "sha256:" + "ab12cd34ab12cd34ab12cd34ab12cd34ab12cd34ab12cd34ab12cd34ab12cd34"

type runner struct {
	calls   []string
	imageID string
	script  func() (string, error)
}

func (r *runner) Run(_ context.Context, _ string, _ []string, name string, args ...string) (string, error) {
	line := name + " " + strings.Join(args, " ")
	r.calls = append(r.calls, line)
	if strings.HasPrefix(line, "docker image inspect") {
		if r.imageID == "" {
			return "", errors.New("no such image")
		}
		return r.imageID + "\n", nil
	}
	return r.script()
}

func updater(r *runner) *Updater {
	return &Updater{Script: "/opt/topcoat/deploy/update-hub.sh", Runner: r}
}

func run(t *testing.T, u *Updater, in string) (map[string]any, error) {
	t.Helper()
	out, err := u.Invoke(context.Background(), json.RawMessage(in))
	if err != nil {
		return nil, err
	}
	var m map[string]any
	if err := json.Unmarshal(out, &m); err != nil {
		t.Fatal(err)
	}
	return m, nil
}

func TestTheScriptRunsOnlyForTheImageThatWasBuilt(t *testing.T) {
	r := &runner{imageID: digest, script: func() (string, error) { return "== done\ntoken=ghp_" + strings.Repeat("a", 30), nil }}
	got, err := run(t, updater(r), `{"image":"topcoat-hub:local-abc1234","digest":"`+digest+`"}`)
	if err != nil || got["ok"] != true {
		t.Fatalf("got %v %v", got, err)
	}
	if r.calls[1] != "bash /opt/topcoat/deploy/update-hub.sh topcoat-hub:local-abc1234" {
		t.Fatalf("calls: %v", r.calls)
	}
	if strings.Contains(got["output"].(string), "ghp_") {
		t.Fatal("the output of the script is cleaned")
	}
	// the image on the host is another one than the one that was tested
	r2 := &runner{imageID: "sha256:" + strings.Repeat("0", 64)}
	if _, err := run(t, updater(r2), `{"image":"topcoat-hub:local-abc1234","digest":"`+digest+`"}`); err == nil || !strings.Contains(err.Error(), "not the") {
		t.Fatalf("got %v", err)
	}
	if len(r2.calls) != 1 {
		t.Fatalf("the script ran for the wrong image: %v", r2.calls)
	}
	// no such image
	if _, err := run(t, updater(&runner{}), `{"image":"x:y","digest":"`+digest+`"}`); err == nil {
		t.Fatal("a missing image must fail")
	}
}

func TestADryRunChangesNothing(t *testing.T) {
	r := &runner{imageID: digest, script: func() (string, error) { return "checks ok", nil }}
	if _, err := run(t, updater(r), `{"image":"topcoat-hub:local-abc1234","digest":"`+digest+`","dry_run":true}`); err != nil {
		t.Fatal(err)
	}
	if r.calls[1] != "bash /opt/topcoat/deploy/update-hub.sh --dry-run topcoat-hub:local-abc1234" {
		t.Fatalf("calls: %v", r.calls)
	}
}

func TestAFailedScriptIsAResultNotACrash(t *testing.T) {
	r := &runner{imageID: digest, script: func() (string, error) {
		return "health check failed; previous image restored", errors.New("exit status 1\nmore")
	}}
	got, err := run(t, updater(r), `{"image":"topcoat-hub:local-abc1234","digest":"`+digest+`"}`)
	if err != nil || got["ok"] != false || got["error"] != "exit status 1" || !strings.Contains(got["output"].(string), "restored") {
		t.Fatalf("got %v %v", got, err)
	}
}

func TestEveryInputIsCheckedBeforeAnyCommandRuns(t *testing.T) {
	for _, in := range []string{
		`{"image":"x; rm -rf /","digest":"` + digest + `"}`,
		`{"image":"-v","digest":"` + digest + `"}`,
		`{"image":"../x","digest":"` + digest + `"}`,
		`{"image":"x:y","digest":"latest"}`,
		`{"image":"x:y"}`,
		`{"image":"x:y","digest":"` + digest + `","script":"/bin/sh"}`,
		`[]`,
	} {
		r := &runner{imageID: digest}
		if _, err := run(t, updater(r), in); err == nil {
			t.Errorf("%s was accepted", in)
		}
		if len(r.calls) != 0 {
			t.Errorf("%s ran %v", in, r.calls)
		}
	}
}

func TestWithoutAScriptNothingRuns(t *testing.T) {
	if _, err := run(t, &Updater{Runner: &runner{imageID: digest}}, `{"image":"x:y","digest":"`+digest+`"}`); err == nil {
		t.Fatal("a node without a script must refuse")
	}
}

func TestItIsTheAdminsOwnStepAndNothingElse(t *testing.T) {
	info := (&Updater{}).Info()
	if !info.Manage || info.ReadOnly || info.Scratch {
		t.Fatalf("%+v", info)
	}
	var _ capability.Capability = &Updater{}
}
