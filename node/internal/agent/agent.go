// Package agent is the node's core: it keeps the session with the hub, applies
// the config, reports inventory and heartbeats, executes whitelisted actions,
// reacts to games (process watcher) and unloads idle models. It keeps working
// with the cached config while the hub is unreachable and queues events on disk.
package agent

import (
	"context"
	"crypto/ed25519"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"topcoat/node/internal/backoff"
	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/gguf"
	"topcoat/node/internal/pressure"
	"topcoat/node/internal/proc"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/activity"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/netinfo"
	"topcoat/node/internal/providers/power"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/sysinfo"
	"topcoat/node/internal/providers/watcher"
	"topcoat/node/internal/store"
	"topcoat/node/internal/yield"
)

// Options configures an Agent. Zero values get sensible defaults where noted.
type Options struct {
	Identity store.Identity
	// Key signs the hub's nonce on every connect (D12, node.key).
	Key ed25519.PrivateKey
	Dir *store.Dir
	// Queue defaults to Dir's events.jsonl with DefaultQueueCap.
	Queue *store.Queue

	GPU     gpu.GPUProvider
	SysInfo sysinfo.Provider
	Watcher watcher.ProcessWatcher
	// NewRunner builds the model runner for a runner config (required).
	NewRunner func(protocol.RunnerConfig) runner.ModelRunner
	// NewCapabilities builds the capability registry of a tool node from config.tools (E2-04);
	// nil = this node offers none.
	NewCapabilities func(*protocol.ToolsConfig) *capability.Registry
	// NewImageRunner builds the image runner for config.images when it is switched on (D126);
	// nil = this node build has none.
	NewImageRunner func(*protocol.ImagesConfig) runner.ImageRunner

	// AgentVersion is reported in hello.
	AgentVersion string
	Logger       *slog.Logger
	// HTTPClient carries the hub TLS config (hubtls.ForIdentity); no timeout, the
	// WebSocket is long-lived.
	HTTPClient *http.Client

	// Timing (injectable for tests).
	Now     func() time.Time
	Backoff *backoff.Backoff
	// Sleep waits d or until ctx is done (returns ctx.Err()).
	Sleep func(ctx context.Context, d time.Duration) error
	// After is used for the game grace period.
	After             func(d time.Duration) <-chan time.Time
	HandshakeTimeout  time.Duration // default 30s
	WriteTimeout      time.Duration // default 10s
	DialTimeout       time.Duration // default 15s
	IdleCheckInterval time.Duration // default 15s
	// DiskFree measures the drive of the model directory for the inventory (default
	// diskfree.Of).
	DiskFree diskfree.Func
	// HeartbeatInterval overrides config.heartbeat_interval_seconds when > 0 (tests).
	HeartbeatInterval time.Duration
	// PressurePoll overrides config.pressure.poll_seconds when > 0 (tests).
	PressurePoll time.Duration
	// Activity tells what the person at this machine does (E2-05 B, D24). Nil: nothing is
	// known, so the node never freezes for local use.
	Activity activity.Provider
	// YieldPoll overrides config.yield.poll_seconds when > 0 (tests).
	YieldPoll time.Duration
	// Network lists the physical interfaces for the inventory (Wake-on-LAN, E2-05 E); nil = none.
	Network netinfo.Provider
	// Sleeper puts the machine to sleep (node.sleep, config.power.sleep_after_idle_seconds);
	// nil = the node cannot sleep.
	Sleeper power.Sleeper
	// SleepCheck overrides how often the idle sleep looks (tests); default 30 s.
	SleepCheck time.Duration
	// SleepDelay is the pause between the result of node.sleep and the sleep itself, so the result
	// reaches the hub first (default 3 s).
	SleepDelay time.Duration
	// Update switches on the update channel (E2-06); nil = this node does not take updates.
	Update *UpdateOptions
	// Addresses lists the unicast IPs of this machine for the inventory and for models.send
	// (E2-07); nil = none are reported (the certificate of a transfer then has no SAN).
	Addresses func() []string
}

// Agent is the node agent. Create with New, then call Run.
type Agent struct {
	opts  Options
	log   *slog.Logger
	queue *store.Queue

	mu        sync.Mutex
	cfg       *protocol.NodeConfig
	cfgHash   string
	run       runner.ModelRunner
	runnerCfg protocol.RunnerConfig
	// caps is the capability registry for toolsCfg (E2-04); capSem limits concurrent calls.
	caps     *capability.Registry
	toolsCfg *protocol.ToolsConfig
	// images is the image runner for imagesCfg (D126); nil = off.
	images    runner.ImageRunner
	imagesCfg *protocol.ImagesConfig
	capSem    chan struct{}
	gaming    map[string]watcher.Event // Event.Key -> start event
	gameStop  context.CancelFunc
	// cooling is true during the cooldown after the last game ended.
	cooling  bool
	coolStop context.CancelFunc
	// bench is the running benchmark, if any.
	bench    *benchRun
	lastUsed map[string]time.Time
	// ttl holds per-model idle TTLs from models.load ttl_seconds.
	ttl map[string]time.Duration
	// downloads holds model names being downloaded.
	downloads map[string]bool
	// baseCtx outlives hub sessions (background downloads / benchmarks).
	baseCtx context.Context
	// press decides about VRAM/RAM pressure and resident models (E1-06);
	// loading counts model loads in progress per model (reported in the
	// heartbeat, E1-05), yieldedState is the last reported "yielded" state.
	press        *pressure.Controller
	loading      map[string]int
	yieldedState bool

	// Local use comes first (E2-05 B): yc decides (only yieldLoop touches it), paused is what
	// everybody else reads, pauseReasons / pausedAt describe the current pause.
	yc           *yield.Controller
	paused       atomic.Bool
	pauseReasons []string
	pausedAt     time.Time
	dedicatedGPU bool // measured when a config is applied
	inputErrSeen string
	cpuNoteSeen  string
	// loads are the model loads in progress (one per model, see load.go).
	loads map[string]*loadJob
	// xfer is the direct model transfer in progress, if any (E2-07); at most one per node.
	xfer *transferRun
	// updating is true while a build is being staged (node.update, E2-06).
	updating atomic.Bool
	// updaterSeen is the updater version the last inventory reported ("-": no heartbeat then).
	updaterSeen atomic.Value

	stateMu sync.Mutex
	state   string
	// stateFromRunner is false while no runner is configured yet; the first
	// transition out of that start-up state is not reported as state_changed.
	stateFromRunner bool

	// emitMu serializes everything sent on the live session after the
	// handshake with queue flushing, so no event can overtake the replay.
	emitMu sync.Mutex
	live   *session

	cfgChanged chan struct{}
	bg         sync.WaitGroup
}

// New validates options and creates an Agent.
func New(opts Options) (*Agent, error) {
	if opts.Dir == nil {
		return nil, errors.New("agent: Dir is required")
	}
	if len(opts.Key) != ed25519.PrivateKeySize {
		return nil, errors.New("agent: Key (ed25519) is required")
	}
	if opts.HTTPClient == nil {
		opts.HTTPClient = &http.Client{}
	}
	if opts.NewRunner == nil || opts.GPU == nil || opts.SysInfo == nil || opts.Watcher == nil {
		return nil, errors.New("agent: providers (NewRunner, GPU, SysInfo, Watcher) are required")
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Backoff == nil {
		opts.Backoff = backoff.Default()
	}
	if opts.Sleep == nil {
		opts.Sleep = sleepCtx
	}
	if opts.After == nil {
		opts.After = time.After
	}
	if opts.HandshakeTimeout <= 0 {
		opts.HandshakeTimeout = 30 * time.Second
	}
	if opts.WriteTimeout <= 0 {
		opts.WriteTimeout = 10 * time.Second
	}
	if opts.DialTimeout <= 0 {
		opts.DialTimeout = 15 * time.Second
	}
	if opts.IdleCheckInterval <= 0 {
		opts.IdleCheckInterval = 15 * time.Second
	}
	if opts.AgentVersion == "" {
		opts.AgentVersion = "unknown"
	}
	a := &Agent{
		opts:         opts,
		log:          opts.Logger,
		queue:        opts.Queue,
		gaming:       map[string]watcher.Event{},
		lastUsed:     map[string]time.Time{},
		ttl:          map[string]time.Duration{},
		downloads:    map[string]bool{},
		baseCtx:      context.Background(),
		cfgChanged:   make(chan struct{}, 1),
		capSem:       make(chan struct{}, maxConcurrentCapabilities),
		press:        pressure.New(protocol.Pressure{}, nil),
		yc:           yield.New(protocol.Yield{}),
		dedicatedGPU: true,
	}
	if a.queue == nil {
		q, skipped, err := store.OpenQueue(opts.Dir.QueuePath(), store.DefaultQueueCap)
		if err != nil {
			return nil, err
		}
		if skipped > 0 {
			a.log.Warn("skipped unreadable lines in event queue", "count", skipped)
		}
		a.queue = q
	}
	if err := a.loadCachedConfig(); err != nil {
		a.log.Warn("ignoring cached config", "err", err)
	}
	return a, nil
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Run keeps the agent running until ctx is canceled.
func (a *Agent) Run(ctx context.Context) error {
	a.mu.Lock()
	a.baseCtx = ctx
	a.mu.Unlock()
	a.refreshState(ctx)
	a.bg.Add(5)
	if a.updateEnabled() {
		a.reportUpdateResult() // an earlier update may have ended before this start
		a.bg.Add(1)
		go func() { defer a.bg.Done(); a.updateReportLoop(ctx) }()
	}
	go func() { defer a.bg.Done(); a.sleepLoop(ctx) }()
	go func() { defer a.bg.Done(); a.watcherLoop(ctx) }()
	go func() { defer a.bg.Done(); a.idleLoop(ctx) }()
	go func() { defer a.bg.Done(); a.pressureLoop(ctx) }()
	go func() { defer a.bg.Done(); a.yieldLoop(ctx) }()
	defer a.closeRunner()
	defer a.bg.Wait()

	bo := a.opts.Backoff
	for {
		established, err := a.runSession(ctx)
		if ctx.Err() != nil {
			a.log.Info("agent stopped")
			return nil
		}
		if established {
			bo.Reset()
		}
		var d time.Duration
		var ue *unauthorizedError
		if errors.As(err, &ue) {
			d = bo.MaxDelay()
			a.log.Error("hub rejected the node identity; the node may have been removed from the hub (pair again with `topcoat-node pair`); retrying with maximum backoff",
				"status", ue.status, "retry_in", d.Round(time.Millisecond))
		} else {
			d = bo.Next()
			a.log.Warn("hub session ended", "err", err, "retry_in", d.Round(time.Millisecond))
		}
		if err := a.opts.Sleep(ctx, d); err != nil {
			a.log.Info("agent stopped")
			return nil
		}
	}
}

// --- config -----------------------------------------------------------------

func (a *Agent) loadCachedConfig() error {
	cc, ok, err := a.opts.Dir.LoadConfig()
	if err != nil || !ok {
		return err
	}
	cfg, err := protocol.ParseNodeConfig(cc.Config)
	if err != nil {
		return err
	}
	a.applyConfig(cfg, cc.ConfigHash)
	a.log.Info("using cached config", "config_hash", cc.ConfigHash)
	return nil
}

// hasDedicatedGPU reports whether the node has a GPU with its own memory. Without a
// measurement it says yes (the conservative default: RAM handling stays off).
func (a *Agent) hasDedicatedGPU() bool {
	if a.opts.GPU == nil {
		return true
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	gpus, _ := a.opts.GPU.GPUs(ctx)
	for _, g := range gpus {
		if !g.Integrated {
			return true
		}
	}
	return false // no GPU at all, or only integrated ones
}

func (a *Agent) applyConfig(cfg protocol.NodeConfig, hash string) {
	dedicated := a.hasDedicatedGPU()
	a.press.SetConfig(cfg.EffectivePressureFor(dedicated), cfg.Residents)
	proc.SetLowPriority(cfg.EffectiveYieldFor(dedicated).LowPriority)
	a.mu.Lock()
	if a.opts.NewCapabilities != nil && (a.caps == nil || !a.toolsCfg.Equal(cfg.Tools)) {
		a.caps, a.toolsCfg = a.opts.NewCapabilities(cfg.Tools), cfg.Tools
	}
	var oldImages runner.ImageRunner
	if a.opts.NewImageRunner != nil && !a.imagesCfg.Equal(cfg.Images) {
		oldImages, a.images, a.imagesCfg = a.images, nil, cfg.Images
		if cfg.Images.On() {
			a.images = a.opts.NewImageRunner(cfg.Images)
		}
	}
	a.dedicatedGPU = dedicated
	a.cfg = &cfg
	a.cfgHash = hash
	var old runner.ModelRunner
	if a.run == nil || !a.runnerCfg.Equal(cfg.Runner) {
		old = a.run
		a.run = a.opts.NewRunner(cfg.Runner)
		a.runnerCfg = cfg.Runner
		if n, ok := a.run.(runner.Notifier); ok {
			r := a.run
			n.SetNotify(func(kind string, data map[string]any) { a.onRunnerEvent(r, kind, data) })
		}
		a.log.Info("runner selected", "type", a.run.Type())
	}
	a.mu.Unlock()
	if oldImages != nil {
		a.bg.Add(1)
		go func() { defer a.bg.Done(); oldImages.Close(context.Background()) }()
	}
	if c, ok := old.(runner.Closer); ok && old != nil {
		// The previous runner's processes (other backend / release) are stopped.
		a.bg.Add(1)
		go func() { defer a.bg.Done(); c.Close(context.Background()) }()
	}
	select {
	case a.cfgChanged <- struct{}{}:
	default:
	}
}

func (a *Agent) config() (*protocol.NodeConfig, string, runner.ModelRunner) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.cfg, a.cfgHash, a.run
}

// configuredContext is the context length the config asks for when a load names none
// (resident entry, per-model setting, node default); 0 = runner default.
func (a *Agent) configuredContext(model string) int {
	cfg, _, _ := a.config()
	if cfg == nil {
		return 0
	}
	return cfg.ContextFor(model)
}

// configuredParallel is the number of llama-server slots the config asks for when a load names
// none (per-model setting, node default); 0 = runner default (1).
func (a *Agent) configuredParallel(model string) int {
	cfg, _, _ := a.config()
	if cfg == nil {
		return 0
	}
	return cfg.ParallelFor(model)
}

// configuredNoJinja is whether the config switches --jinja off for a model.
func (a *Agent) configuredNoJinja(model string) bool {
	cfg, _, _ := a.config()
	return cfg != nil && !cfg.JinjaFor(model)
}

func (a *Agent) heartbeatInterval() time.Duration {
	if a.opts.HeartbeatInterval > 0 {
		return a.opts.HeartbeatInterval
	}
	cfg, _, _ := a.config()
	if cfg == nil || cfg.HeartbeatIntervalSeconds <= 0 {
		return protocol.DefaultHeartbeatIntervalSeconds * time.Second
	}
	d := time.Duration(cfg.HeartbeatIntervalSeconds) * time.Second
	// The saver profile lets an idle node be quiet (nothing changes; whatever does change is
	// reported at once by its own heartbeat).
	if f := cfg.EffectivePower().HeartbeatFactor; f > 1 && a.isIdle(cfg) {
		d *= time.Duration(f)
	}
	return d
}

// --- state ------------------------------------------------------------------

func (a *Agent) isGaming() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.gaming) > 0 || a.cooling
}

// computeState derives the node state and returns runner availability details.
func (a *Agent) computeState(ctx context.Context) (state string, available bool, detail string) {
	_, _, r := a.config()
	available = false
	detail = "no runner configured"
	if r != nil {
		hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err := r.Health(hctx)
		cancel()
		available = err == nil
		detail = ""
		if err != nil {
			detail = err.Error()
		}
	}
	switch {
	case a.isGaming():
		state = protocol.StateBusyGaming
	case !available:
		state = protocol.StateDegraded
	case a.press.Active(a.opts.Now()):
		state = protocol.StateYielded
	case a.isPaused():
		state = protocol.StatePaused
	default:
		state = protocol.StateAvailable
	}
	return state, available, detail
}

// refreshState recomputes the state and emits state_changed on transitions.
func (a *Agent) refreshState(ctx context.Context) string {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	next, _, detail := a.computeState(ctx)
	_, _, r := a.config()
	prev, prevFromRunner := a.state, a.stateFromRunner
	a.state, a.stateFromRunner = next, r != nil
	if prevFromRunner && prev != next {
		a.log.Info("node state changed", "from", prev, "to", next, "detail", detail)
		a.emit(protocol.EventStateChanged, map[string]any{"from": prev, "to": next})
	}
	return next
}

// State returns the last computed node state.
func (a *Agent) State() string {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	return a.state
}

// --- outgoing messages --------------------------------------------------------

// emit sends an event on the live session or queues it on disk.
func (a *Agent) emit(kind string, data map[string]any) {
	if data == nil {
		data = map[string]any{}
	}
	env, err := protocol.New(protocol.TypeEvent, protocol.Event{Kind: kind, Data: data}, a.opts.Now())
	if err != nil {
		a.log.Error("build event", "err", err)
		return
	}
	a.sendLive(env, true)
}

// sendLive writes env on the live session. If there is none (or it fails) the
// envelope is queued when queue is true, otherwise dropped.
func (a *Agent) sendLive(env protocol.Envelope, queue bool) {
	a.emitMu.Lock()
	defer a.emitMu.Unlock()
	if s := a.live; s != nil && (!s.readOnly.Load() || env.Type == protocol.TypeHeartbeat) {
		if err := s.write(env); err == nil {
			return
		} else if queue {
			a.log.Debug("send failed, queueing", "type", env.Type, "err", err)
		}
	}
	if queue {
		if err := a.queue.Append(env); err != nil {
			a.log.Error("queue event", "err", err)
		}
	}
}

func (a *Agent) heartbeat(ctx context.Context) (protocol.Envelope, error) {
	state := a.refreshState(ctx)
	hb := protocol.Heartbeat{State: state, GPUs: []protocol.GPUUsage{}, Loaded: []protocol.LoadedModel{}, Loading: a.loadingNames()}
	if info, err := a.opts.SysInfo.Info(ctx); err == nil {
		hb.RAMUsedMB = info.RAMUsedMB
		if info.CPUKnown {
			v := min(max(info.CPUPercent, 0), 100)
			hb.CPUUtil = &v
		}
	}
	var loaded []runner.LoadedModel
	var pids []int
	if _, _, r := a.config(); r != nil {
		if l, err := r.Loaded(ctx); err == nil {
			loaded = l
			for _, m := range l {
				if m.PID > 0 {
					pids = append(pids, m.PID)
				}
			}
		}
	}
	gpus, usage, err := gpu.SampleOf(ctx, a.opts.GPU, pids)
	if err != nil {
		a.log.Debug("gpu provider", "err", err)
	}
	foreign := foreignUsage(gpus, usage, loaded)
	for i, g := range gpus {
		gu := protocol.GPUUsage{Index: g.Index, VRAMUsedMB: nonNeg(g.VRAMUsedMB), SharedUsedMB: nonNeg(g.SharedUsedMB)}
		if u := usageAt(usage, i); u.UtilKnown {
			v := min(max(u.UtilPercent, 0), 100)
			gu.GPUUtil = &v
		}
		if f, ok := foreign[i]; ok {
			gu.ForeignVRAMUsedMB, gu.ForeignGPUUtil = f.ForeignVRAMUsedMB, f.ForeignGPUUtil
			gu.ForeignSharedMaxMB = f.ForeignSharedMaxMB
		}
		hb.GPUs = append(hb.GPUs, gu)
	}
	for _, l := range loaded {
		hb.Loaded = append(hb.Loaded, toProtoLoaded(l))
	}
	return protocol.New(protocol.TypeHeartbeat, hb, a.opts.Now())
}

func (a *Agent) sendHeartbeat(ctx context.Context) {
	env, err := a.heartbeat(ctx)
	if err != nil {
		a.log.Error("build heartbeat", "err", err)
		return
	}
	a.sendLive(env, false)
}

func (a *Agent) loadedModels(ctx context.Context) []protocol.LoadedModel {
	out := []protocol.LoadedModel{}
	_, _, r := a.config()
	if r == nil {
		return out
	}
	loaded, err := r.Loaded(ctx)
	if err != nil {
		return out
	}
	for _, l := range loaded {
		out = append(out, toProtoLoaded(l))
	}
	return out
}

func (a *Agent) inventory(ctx context.Context) (protocol.Envelope, error) {
	inv := protocol.Inventory{
		Hardware: protocol.Hardware{GPUs: []protocol.GPU{}},
		Models:   []protocol.Model{},
		Loaded:   []protocol.LoadedModel{},
	}
	info, err := a.opts.SysInfo.Info(ctx)
	if err != nil {
		a.log.Debug("sysinfo provider", "err", err)
	}
	inv.Hardware.CPU = protocol.CPU{Model: info.CPUModel, Cores: info.Cores, Threads: info.Threads,
		PerformanceCores: info.PerformanceCores, EfficiencyCores: info.EfficiencyCores}
	if inv.Hardware.CPU.Cores < 1 {
		inv.Hardware.CPU.Cores = 1
	}
	if inv.Hardware.CPU.Model == "" {
		inv.Hardware.CPU.Model = "unknown"
	}
	inv.Hardware.RAMTotalMB = nonNeg(info.RAMTotalMB)
	if a.opts.Network != nil {
		for _, i := range a.opts.Network.Interfaces() {
			inv.Hardware.Network = append(inv.Hardware.Network, protocol.NetIface{Name: i.Name, MAC: i.MAC, Kind: i.Kind})
		}
	}
	inv.Hardware.Addresses = a.addresses()
	gpus, err := a.opts.GPU.GPUs(ctx)
	if msg := gpu.Diagnose(err); msg != "" {
		a.log.Warn(msg, "err", err, "gpus", len(gpus))
	}
	for _, g := range gpus {
		inv.Hardware.GPUs = append(inv.Hardware.GPUs, protocol.GPU{Index: g.Index, Name: g.Name, Vendor: normVendor(g.Vendor),
			VRAMTotalMB: nonNeg(g.VRAMTotalMB), Integrated: g.Integrated, SharedTotal: nonNeg(g.SharedTotalMB)})
	}
	_, available, detail := a.computeState(ctx)
	_, _, r := a.config()
	if r != nil {
		inv.Runner = protocol.RunnerInfo{Type: r.Type(), Available: available, Detail: detail}
		if d, ok := r.(runner.Describer); ok {
			ri := d.RuntimeInfo()
			inv.Runner.Backend, inv.Runner.Release, inv.Runner.Threads = protoBackend(ri.Backend), ri.Release, max(ri.Threads, 0)
		}
		if models, err := r.ListModels(ctx); err == nil {
			for _, m := range models {
				own := m.Own
				inv.Models = append(inv.Models, protocol.Model{Name: m.Name, SizeBytes: nonNeg(m.SizeBytes), Format: m.Format, Own: &own, GGUF: protoGGUF(m.GGUF), Projector: m.Projector})
			}
		}
		if dr, ok := r.(runner.DiskReporter); ok {
			measure := a.opts.DiskFree
			if measure == nil {
				measure = diskfree.Of
			}
			if u, err := measure(dr.ModelDir()); err == nil {
				total, free := nonNeg(u.TotalBytes>>20), nonNeg(u.FreeBytes>>20)
				inv.Hardware.DiskTotalMB, inv.Hardware.DiskFreeMB = &total, &free
			}
		}
		inv.Loaded = a.loadedModels(ctx)
	} else {
		inv.Runner = protocol.RunnerInfo{Type: protocol.RunnerFake, Available: false, Detail: detail}
	}
	inv.Capabilities = a.capabilityInfos()
	inv.Update = a.updateInfo()
	inv.Images = a.imagesInfo(ctx)
	return protocol.New(protocol.TypeInventory, inv, a.opts.Now())
}

func (a *Agent) sendInventory(ctx context.Context) {
	env, err := a.inventory(ctx)
	if err != nil {
		a.log.Error("build inventory", "err", err)
		return
	}
	a.sendLive(env, false)
}

func toProtoLoaded(l runner.LoadedModel) protocol.LoadedModel {
	return protocol.LoadedModel{Name: l.Name, VRAMUsedMB: nonNeg(l.VRAMUsedMB), ContextLength: max(l.ContextLength, 0), NParallel: max(l.NParallel, 0),
		PID: max(l.PID, 0), Port: max(l.Port, 0), Backend: protoBackend(l.Backend), RAMUsedMB: nonNeg(l.RAMUsedMB)}
}

// protoBackend keeps only backend names the protocol knows.
func protoBackend(b string) string {
	switch b {
	case protocol.BackendCUDA, protocol.BackendVulkan, protocol.BackendCPU:
		return b
	}
	return ""
}

// onRunnerEvent handles asynchronous runner events (from the runner r).
func (a *Agent) onRunnerEvent(r runner.ModelRunner, kind string, data map[string]any) {
	_, _, current := a.config()
	if current != r {
		return // an old runner that is shutting down
	}
	ctx := a.ctx()
	switch kind {
	case protocol.EventModelUnloaded:
		if name, ok := data["model"].(string); ok {
			a.forget(name)
		}
		a.emit(kind, data)
		a.sendHeartbeat(ctx)
	case protocol.EventRuntimeInstalled:
		a.emit(kind, data)
		a.sendHeartbeat(ctx) // carries the new state (available)
		a.sendInventory(ctx)
	default:
		a.emit(kind, data)
	}
}

// ctx returns the agent's long-lived context.
func (a *Agent) ctx() context.Context {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.baseCtx
}

// closeRunner stops the runner's processes when the agent stops.
func (a *Agent) closeRunner() {
	_, _, r := a.config()
	if c, ok := r.(runner.Closer); ok {
		c.Close(context.Background())
	}
	a.mu.Lock()
	ir := a.images
	a.mu.Unlock()
	if ir != nil {
		ir.Close(context.Background()) // an sd-server never outlives its node
	}
}

func nonNeg(v int64) int64 {
	if v < 0 {
		return 0
	}
	return v
}

func normVendor(v string) string {
	switch strings.ToLower(v) {
	case gpu.VendorNVIDIA, gpu.VendorAMD, gpu.VendorIntel:
		return strings.ToLower(v)
	default:
		return gpu.VendorOther
	}
}

// errorf builds a protocol error.
func errorf(code, format string, args ...any) *protocol.Error {
	return &protocol.Error{Code: code, Message: fmt.Sprintf(format, args...)}
}

// protoGGUF turns the header facts of a model file into their protocol form (nil stays nil).
func protoGGUF(m *gguf.Meta) *protocol.ModelGGUF {
	if m == nil {
		return nil
	}
	return &protocol.ModelGGUF{
		Architecture:    m.Architecture,
		ContextLength:   m.ContextLength,
		EmbeddingLength: m.EmbeddingLength,
		PoolingType:     m.PoolingType,
		ChatTemplate:    m.ChatTemplate,
	}
}

// imagesInfo is the inventory's view of the image runner (D126); nil when it is off.
func (a *Agent) imagesInfo(ctx context.Context) *protocol.ImagesInfo {
	a.mu.Lock()
	ir := a.images
	a.mu.Unlock()
	if ir == nil {
		return nil
	}
	ri := ir.Info()
	out := &protocol.ImagesInfo{Release: ri.Release, Backend: protoBackend(ri.Backend), Installed: ri.Installed,
		Models: []protocol.ImageModel{}, Loaded: []string{}}
	if models, err := ir.ListImageModels(ctx); err == nil {
		for _, m := range models {
			out.Models = append(out.Models, protocol.ImageModel{Name: m.Name, SizeBytes: nonNeg(m.SizeBytes), Format: m.Format})
		}
	}
	if loaded, err := ir.LoadedImages(ctx); err == nil {
		for _, l := range loaded {
			out.Loaded = append(out.Loaded, l.Name)
		}
	}
	return out
}
