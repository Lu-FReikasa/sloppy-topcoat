package agent

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"slices"
	"strings"
	"time"

	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
	"topcoat/node/internal/workshop"
)

// maxConcurrentCapabilities bounds the capability calls running at once: a tool node shares a VM
// with family services (E2-04), so the assistant asking five things at once must not load it.
const maxConcurrentCapabilities = 4

// capabilityInfos lists what this node build offers (inventory.capabilities).
func (a *Agent) capabilityInfos() []protocol.CapabilityInfo {
	a.mu.Lock()
	reg := a.caps
	a.mu.Unlock()
	if reg == nil {
		return nil
	}
	var out []protocol.CapabilityInfo
	for _, n := range reg.Names() {
		c, _ := reg.Get(n)
		info := capability.InfoOf(c)
		out = append(out, protocol.CapabilityInfo{Name: n, Description: info.Description, ReadOnly: info.ReadOnly, Scratch: info.Scratch, Manage: info.Manage, URLOrigin: info.URLOrigin, InputSchema: c.InputSchema()})
	}
	return out
}

// actCapabilityInvoke runs one capability: {"capability": "docker.ps", "input": {...}}. The
// capability must be switched on in config.capabilities and read-only — a capability that changes
// something is never run by this action (D15: it needs a confirmation of that single action).
func (a *Agent) actCapabilityInvoke(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "capability", "input", "repo", "url_origin"); perr != nil {
		return nil, perr, nil
	}
	return a.runCapability(ctx, args, runInvoke)
}

// how a capability is run: a plain call, a confirmed proposal, or the person's own management
// step from the hub's page
type runMode int

const (
	runInvoke runMode = iota
	runExecute
	runManage
	runBuild
	runHub
)

// actWorkshopManage runs one management capability of the dev workshop (D76): what the person
// started on the hub's page (a new local repository, a branch, a merge into the default branch, a
// publish). Only capabilities marked Manage run here, and they run nowhere else: an assistant's
// calls (capability.invoke / capability.execute) never reach them.
func (a *Agent) actWorkshopManage(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "capability", "input", "repo"); perr != nil {
		return nil, perr, nil
	}
	return a.runCapability(ctx, args, runManage)
}

// actBuildManage runs one step of the build service (NS-01): what the admin started on the Releases
// page. Only a capability named build.* that is marked Manage runs here, and no other action runs
// it, so an assistant can never reach it.
func (a *Agent) actBuildManage(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "capability", "input"); perr != nil {
		return nil, perr, nil
	}
	return a.runCapability(ctx, args, runBuild)
}

// actHubManage runs the roll-out of the hub (hub.update) on the production host.
func (a *Agent) actHubManage(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "capability", "input"); perr != nil {
		return nil, perr, nil
	}
	return a.runCapability(ctx, args, runHub)
}

var (
	proposalIDRe   = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	proposalHashRe = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// actCapabilityExecute runs one state-changing capability the user confirmed (E3-01b, D15):
// {"capability": "docker.restart", "input": {...}, "proposal": "<uuid>", "hash": "<sha256>"}.
// It runs only capabilities that are NOT read-only (the mirror of capability.invoke), only where
// the config allows capability.execute and switches the capability on, and only with the id and
// hash of a confirmed proposal — the hub checks them against its record, the node writes them to
// its log next to what it did, so a restart can be traced to the click that caused it.
func (a *Agent) actCapabilityExecute(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "capability", "input", "proposal", "hash", "repo"); perr != nil {
		return nil, perr, nil
	}
	id, _ := argString(args, "proposal")
	hash, _ := argString(args, "hash")
	if !proposalIDRe.MatchString(id) || !proposalHashRe.MatchString(hash) {
		return nil, errorf(protocol.ErrInvalidArgs, "a confirmed proposal (proposal id and hash) is required"), nil
	}
	a.log.Info("executing a confirmed proposal", "proposal", id, "hash", hash)
	return a.runCapability(ctx, args, runExecute)
}

// runCapability is the shared body: write says which kind of capability this action may run.
func (a *Agent) runCapability(ctx context.Context, args map[string]json.RawMessage, mode runMode) (any, *protocol.Error, func()) {
	write := mode == runExecute
	name, ok := argString(args, "capability")
	if !ok || name == "" {
		return nil, errorf(protocol.ErrInvalidArgs, "capability is required"), nil
	}
	cfg, _, _ := a.config()
	a.mu.Lock()
	reg := a.caps
	a.mu.Unlock()
	if cfg == nil || reg == nil || !slices.Contains(cfg.Capabilities, name) {
		return nil, errorf(protocol.ErrNotAllowed, "capability %s is not enabled on this node", name), nil
	}
	c, found := reg.Get(name)
	if !found {
		return nil, errorf(protocol.ErrNotAllowed, "capability %s is not offered by this node version", name), nil
	}
	info := capability.InfoOf(c)
	isBuild, isHub := strings.HasPrefix(name, "build."), strings.HasPrefix(name, "hub.")
	manages := mode == runManage || mode == runBuild || mode == runHub
	if info.Manage != manages || isBuild != (mode == runBuild) || isHub != (mode == runHub) {
		switch {
		case info.Manage && isBuild:
			return nil, errorf(protocol.ErrNotAllowed, "capability %s is a step of the build service: only build.manage runs it", name), nil
		case info.Manage && isHub:
			return nil, errorf(protocol.ErrNotAllowed, "capability %s is the roll-out of the hub: only hub.manage runs it", name), nil
		case mode == runHub:
			return nil, errorf(protocol.ErrNotAllowed, "hub.manage runs only the roll-out of the hub"), nil
		case info.Manage:
			return nil, errorf(protocol.ErrNotAllowed, "capability %s is the person's own step: only workshop.manage runs it", name), nil
		case mode == runBuild:
			return nil, errorf(protocol.ErrNotAllowed, "build.manage runs only the build service's steps"), nil
		}
		return nil, errorf(protocol.ErrNotAllowed, "workshop.manage runs only the workshop's management capabilities"), nil
	}
	switch ro := info.ReadOnly || info.Scratch; {
	case mode == runManage || mode == runBuild || mode == runHub:
	case !ro && !write:
		return nil, errorf(protocol.ErrNotAllowed, "capability %s changes state: it is not run without a confirmation", name), nil
	case ro && write: // a scratch capability is run by capability.invoke, like a read-only one
		return nil, errorf(protocol.ErrNotAllowed, "capability %s is read-only: capability.execute runs only capabilities that change state", name), nil
	}
	input := args["input"]
	// a workshop capability gets its repository (URL, branch, template, token) from the hub with
	// the call; the token is in the context only: it is not part of the input, so it is in no log
	// line, no audit row and no output
	if raw, has := args["repo"]; has {
		if !strings.HasPrefix(name, "repo.") && !strings.HasPrefix(name, "workshop.") {
			return nil, errorf(protocol.ErrInvalidArgs, "capability %s takes no repository", name), nil
		}
		var spec workshop.Spec
		if err := json.Unmarshal(raw, &spec); err != nil || (spec.URL == "" && !spec.Local) {
			return nil, errorf(protocol.ErrInvalidArgs, "repo must hold url and branch"), nil
		}
		ctx = workshop.WithSpec(ctx, spec)
	}
	// the hub vouches that a web URL is the person's own, not one a model made up (finding 03);
	// only capability.invoke takes it (the other actions refuse the key above)
	if raw, has := args["url_origin"]; has {
		var origin string
		if err := json.Unmarshal(raw, &origin); err != nil || origin != "person" {
			return nil, errorf(protocol.ErrInvalidArgs, `url_origin must be "person"`), nil
		}
		if !tools.URLOriginCapabilities[name] {
			return nil, errorf(protocol.ErrInvalidArgs, "capability %s takes no url_origin", name), nil
		}
		ctx = tools.WithPersonURL(ctx)
	}
	select {
	case a.capSem <- struct{}{}:
		defer func() { <-a.capSem }()
	case <-ctx.Done():
		return nil, errorf(protocol.ErrCanceled, "canceled"), nil
	default:
		return nil, errorf(protocol.ErrBusy, "this node is already running %d capability calls", maxConcurrentCapabilities), nil
	}
	cctx, cancel := context.WithTimeout(ctx, cfg.ToolTimeout())
	defer cancel()
	start := a.opts.Now()
	raw, err := c.Invoke(cctx, input)
	took := a.opts.Now().Sub(start)
	log := a.log.With("capability", name, "input", string(input), "ms", took.Milliseconds())
	if err != nil {
		var ie *tools.InputError
		var pe *tools.PrivateDataError
		switch {
		case errors.As(err, &pe):
			// the refusal names the kinds, and the log must not hold what was refused
			a.log.With("capability", name, "ms", took.Milliseconds()).Warn("capability refused a web input with private data", "kinds", strings.Join(pe.Kinds, ","))
			return nil, errorf(protocol.ErrPrivateData, "%s", err.Error()), nil
		case errors.As(err, &ie):
			log.Info("capability refused the input", "why", err.Error())
			return nil, errorf(protocol.ErrInvalidArgs, "%s", err.Error()), nil
		case errors.Is(cctx.Err(), context.DeadlineExceeded):
			log.Warn("capability timed out")
			return nil, errorf(protocol.ErrTimeout, "capability %s did not finish in %s", name, cfg.ToolTimeout()), nil
		case ctx.Err() != nil:
			return nil, errorf(protocol.ErrCanceled, "canceled"), nil
		}
		log.Warn("capability failed", "err", err)
		return nil, errorf(protocol.ErrCapabilityFailed, "%s", err.Error()), nil
	}
	if len(raw) > outputLimit(info) {
		log.Warn("capability output too large", "bytes", len(raw))
		return nil, errorf(protocol.ErrCapabilityFailed, "the output of %s is larger than this node sends (%d bytes)", name, len(raw)), nil
	}
	log.Info("capability done", "bytes", len(raw))
	return map[string]any{
		"capability": name, "output": raw, "bytes": len(raw),
		"duration_ms": took.Milliseconds(), "at": start.UTC().Format(time.RFC3339),
	}, nil, nil
}

// outputLimit is the largest output the node sends for a capability: its own MaxOutputBytes
// (capped at capability.MaxOutputCeiling) or else the default for every capability.
func outputLimit(info capability.Info) int {
	if info.MaxOutputBytes > 0 {
		return min(info.MaxOutputBytes, capability.MaxOutputCeiling)
	}
	return defaultOutputLimit
}

// defaultOutputLimit is the output limit of a capability that sets none.
const defaultOutputLimit = tools.HardMaxOutputBytes + 4096
