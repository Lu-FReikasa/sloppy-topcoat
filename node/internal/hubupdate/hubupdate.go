// Package hubupdate is the roll-out step of the production host (NS-01, phase 5): one named
// capability, hub.update, that wraps deploy/update-hub.sh. The script keeps its own safety net
// (revision check, database dump, health check, automatic way back); this package only makes sure
// the image it is asked to roll out is exactly the one that was built and tested, and that the
// call is the admin's click and nothing else (the action hub.manage, never an assistant's).
package hubupdate

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"topcoat/node/internal/buildsvc"
	"topcoat/node/internal/providers/capability"
)

// Name is the capability the hub calls.
const Name = "hub.update"

var (
	imageRe  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}$`)
	digestRe = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

// Runner runs one fixed command (the same interface as the build service's).
type Runner = buildsvc.Runner

// Updater is the hub.update capability.
type Updater struct {
	// Script is the path of deploy/update-hub.sh on this host, set by the operator, never by a call.
	Script string
	Runner Runner
	// Timeout bounds the whole script (it waits for the health check itself).
	Timeout time.Duration
}

func (u *Updater) Name() string { return Name }

func (u *Updater) InputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","properties":{` +
		`"image":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,199}$"},` +
		`"digest":{"type":"string","pattern":"^sha256:[0-9a-f]{64}$"},` +
		`"dry_run":{"type":"boolean"}},"required":["image","digest"],"additionalProperties":false}`)
}

func (u *Updater) Info() capability.Info {
	return capability.Info{
		Description: "Roll the hub out to an image that is already on this host: checks the image's digest, then runs the update script (revision check, dump, health check, way back).",
		Manage:      true,
	}
}

type input struct {
	Image  string `json:"image"`
	Digest string `json:"digest"`
	DryRun bool   `json:"dry_run"`
}

// Validate checks an input without running anything.
func Validate(raw json.RawMessage) (input, error) {
	var in input
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		return in, fmt.Errorf("the input is not valid: %w", err)
	}
	if !imageRe.MatchString(in.Image) || strings.HasPrefix(in.Image, "-") || strings.Contains(in.Image, "..") {
		return in, errors.New("image is not a local image name")
	}
	if !digestRe.MatchString(in.Digest) {
		return in, errors.New("digest must be sha256:<64 hex digits>")
	}
	return in, nil
}

// Invoke runs the script for an image whose digest is the one named.
func (u *Updater) Invoke(ctx context.Context, raw json.RawMessage) (json.RawMessage, error) {
	in, err := Validate(raw)
	if err != nil {
		return nil, err
	}
	if u.Script == "" || u.Runner == nil {
		return nil, errors.New("this node has no update script configured")
	}
	limit := u.Timeout
	if limit <= 0 {
		limit = 15 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	idOut, err := u.Runner.Run(ctx, "/", nil, "docker", "image", "inspect", "--format", "{{.Id}}", in.Image)
	if err != nil {
		return nil, fmt.Errorf("the image %s is not on this host: %w", in.Image, err)
	}
	if got := strings.TrimSpace(idOut); got != in.Digest {
		return nil, fmt.Errorf("the image on this host is %s, not the %s that was built and tested", got, in.Digest)
	}
	args := []string{u.Script}
	if in.DryRun {
		args = append(args, "--dry-run")
	}
	args = append(args, in.Image)
	out, runErr := u.Runner.Run(ctx, "/", nil, "bash", args...)
	tail := buildsvc.Mask(out)
	if len(tail) > 8000 {
		tail = tail[len(tail)-8000:]
	}
	res := map[string]any{"ok": runErr == nil, "dry_run": in.DryRun, "image": in.Image, "output": tail}
	if runErr != nil {
		res["error"] = firstLine(runErr.Error())
	}
	return json.Marshal(res)
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
