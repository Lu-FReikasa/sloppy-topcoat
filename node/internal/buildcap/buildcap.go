// Package buildcap offers the steps of the build service to the hub as capabilities of the node
// (NS-01). Each one is marked Manage, so only the action build.manage runs it, and each forwards
// its call over the service bus to the build service process: the node itself builds nothing,
// holds no Docker access and no repository credentials.
package buildcap

import (
	"context"
	"encoding/json"
	"fmt"

	"topcoat/node/internal/buildsvc"
	"topcoat/node/internal/providers/capability"
)

// Caller is the bus as the capabilities use it.
type Caller interface {
	Call(ctx context.Context, service, action string, input json.RawMessage) (json.RawMessage, error)
}

// Service is the id of the build service on the bus.
const Service = "build"

type step struct {
	name, description string
	schema            string
	caller            Caller
}

func (s step) Name() string                 { return s.name }
func (s step) InputSchema() json.RawMessage { return json.RawMessage(s.schema) }
func (s step) Info() capability.Info {
	return capability.Info{Description: s.description, Manage: true}
}

func (s step) Invoke(ctx context.Context, input json.RawMessage) (json.RawMessage, error) {
	if err := buildsvc.Validate(s.name, input); err != nil {
		return nil, fmt.Errorf("%s: %w", s.name, err)
	}
	out, err := s.caller.Call(ctx, Service, s.name, input)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", s.name, err)
	}
	return out, nil
}

const (
	commit    = `{"type":"string","pattern":"^[0-9a-f]{40}$"}`
	version   = `{"type":"string","pattern":"^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,32})?$"}`
	buildID   = `{"type":"string","pattern":"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}`
	closedObj = `"additionalProperties":false`
)

// Register adds the build steps to a registry; their calls go to caller.
func Register(reg *capability.Registry, caller Caller) error {
	steps := []step{
		{buildsvc.ActionFetch, "Fetch a commit, branch or tag of a configured source into the build mirror and name the commit and the changes since the last release.",
			`{"type":"object","properties":{"ref":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"},"source":{"enum":["release_repo","dev_mirror"]}},"required":["ref","source"],` + closedObj + `}`, caller},
		{buildsvc.ActionNode, "Build the node for linux and windows from a pinned commit in a fresh clone and upload the files to the hub. Signs nothing.",
			`{"type":"object","properties":{"commit":` + commit + `,"version":` + version + `,"build_id":` + buildID + `},"required":["commit","version","build_id"],` + closedObj + `}`, caller},
		{buildsvc.ActionHubImage, "Build the hub image from a pinned commit in a fresh clone.",
			`{"type":"object","properties":{"commit":` + commit + `,"build_id":` + buildID + `},"required":["commit","build_id"],` + closedObj + `}`, caller},
		{buildsvc.ActionNodeImage, "Build the container image of the CPU node from a pinned commit in a fresh clone.",
			`{"type":"object","properties":{"commit":` + commit + `,"version":` + version + `,"build_id":` + buildID + `},"required":["commit","version","build_id"],` + closedObj + `}`, caller},
		{buildsvc.ActionSmokeTest, "Start the image that was built last with a database and check that it migrates and answers.",
			`{"type":"object","properties":{"image":{"type":"string","pattern":"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$"}},"required":["image"],` + closedObj + `}`, caller},
	}
	for _, s := range steps {
		if err := reg.Register(s); err != nil {
			return err
		}
	}
	return nil
}
