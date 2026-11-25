// Command topcoat-node is the sloppy-topcoat node agent.
//
//	topcoat-node install --hub URL --code CODE [--ca-fingerprint FP | --ca-file FILE] [--name NAME] [--data-dir DIR] [--force] [--no-start]
//	topcoat-node uninstall [--data-dir DIR] [--purge]
//	topcoat-node set-hub --hub URL [--data-dir DIR] [--no-restart]
//	topcoat-node pair --hub URL --code CODE [--ca-fingerprint FP | --ca-file FILE] [--name NAME] [--data-dir DIR] [--force]
//	topcoat-node input-helper
//	topcoat-node docker-restart-proxy [--listen 127.0.0.1:2376] [--docker unix:///var/run/docker.sock]
//	topcoat-node updater [--data-dir DIR] [--update-state-dir DIR] [--log-level info] [--log-file FILE]
//	topcoat-node run  [--data-dir DIR] [--fake] [--fake-profile FILE] [--fake-control FILE] [--log-level info] [--log-file FILE]
//	topcoat-node version
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"topcoat/node/internal/agent"
	"topcoat/node/internal/buildcap"
	"topcoat/node/internal/buildsvc"
	"topcoat/node/internal/datadir"
	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/hubupdate"
	"topcoat/node/internal/inputhelper"
	"topcoat/node/internal/llamacpp"
	"topcoat/node/internal/logfile"
	"topcoat/node/internal/pairing"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/activity"
	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/providers/fake"
	"topcoat/node/internal/providers/gpu"
	"topcoat/node/internal/providers/netinfo"
	"topcoat/node/internal/providers/power"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/providers/sysinfo"
	"topcoat/node/internal/providers/watcher"
	"topcoat/node/internal/restartproxy"
	"topcoat/node/internal/sdcpp"
	"topcoat/node/internal/service"
	"topcoat/node/internal/servicebus"
	"topcoat/node/internal/sethub"
	"topcoat/node/internal/store"
	"topcoat/node/internal/tools"
	"topcoat/node/internal/update"
	"topcoat/node/internal/version"
	"topcoat/node/internal/workshop"
)

func main() {
	if service.IsWindowsService() {
		// Started by the Windows service control manager: no console, log to
		// <data dir>/logs/topcoat-node.log.
		name := service.Name
		if len(os.Args) > 1 && os.Args[1] == "updater" {
			name = service.UpdaterName
		}
		os.Exit(service.RunWindowsService(name, func(stop <-chan struct{}) int {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go func() { <-stop; cancel() }()
			return run(ctx, append(os.Args[1:], "--log-file", autoLogFile), io.Discard, io.Discard)
		}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

const usage = `topcoat-node - sloppy-topcoat node agent

Usage:
  topcoat-node install --hub URL --code CODE [--ca-fingerprint FP | --ca-file FILE] [--name NAME]
                       [--role gpu|tools|dev] [--data-dir DIR] [--service-user ACCOUNT]
                       [--force] [--no-start]
      pair and register a system service (systemd unit / Windows service); needs root / admin
  topcoat-node uninstall [--data-dir DIR] [--purge]
      remove the service (--purge also deletes the data dir with the node key and models)
  topcoat-node set-hub --hub URL [--data-dir DIR] [--no-restart]
      move this node to a new hub address (same CA and node identity); checks the certificate
      against the CA this node pinned, writes node.json, restarts the service; needs root / admin
  topcoat-node pair --hub URL --code CODE [--ca-fingerprint FP | --ca-file FILE] [--name NAME]
                    [--role gpu|tools|dev] [--data-dir DIR] [--force]
  topcoat-node input-helper
      (Windows) report keyboard/mouse activity of this session to the service over a local pipe;
      started at logon by the scheduled task that install creates, not for manual use
  topcoat-node docker-restart-proxy [--listen 127.0.0.1:2376] [--docker unix:///var/run/docker.sock]
      (tool nodes, optional) the write proxy for confirmed container restarts: it forwards
      POST /containers/<name>/restart and answers 403 to everything else; loopback only
  topcoat-node updater [--data-dir DIR] [--update-state-dir DIR] [--log-level LEVEL] [--log-file FILE]
      the privileged updater service that install registers: applies updates that the node staged,
      after verifying them against the keys built into this program; not for manual use
  topcoat-node run [--data-dir DIR] [--update-state-dir DIR] [--fake] [--fake-profile FILE] [--fake-control FILE] [--log-level LEVEL] [--log-file FILE]
  topcoat-node version

Copy the pair/install command from the hub GUI (Knoten hinzufügen); it contains the
hub URL, the code and the fingerprint of the hub's CA, e.g.:
  sudo ./topcoat-node install --hub https://hub.example.lan:8443 --code ABCD-1234 \
    --ca-fingerprint sha256:<64 hex digits>

The data dir defaults to %s
(override with $%s or --data-dir).
`

func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintf(stderr, usage, datadir.Default(), datadir.EnvVar)
		return 2
	}
	switch args[0] {
	case "pair":
		return cmdPair(ctx, args[1:], stdout, stderr)
	case "install":
		return cmdInstall(ctx, args[1:], stdout, stderr)
	case "uninstall":
		return cmdUninstall(args[1:], stdout, stderr)
	case "set-hub":
		return cmdSetHub(ctx, args[1:], stdout, stderr)
	case "input-helper":
		return cmdInputHelper(args[1:], stderr)
	case "docker-restart-proxy":
		return cmdRestartProxy(ctx, args[1:], stderr)
	case "run":
		return cmdRun(ctx, args[1:], stderr)
	case "updater":
		return cmdUpdater(ctx, args[1:], stderr)
	case "service":
		return cmdService(ctx, args[1:], stderr)
	case "version", "--version", "-v":
		fmt.Fprintf(stdout, "topcoat-node %s (protocol v%d)\n", version.Version, version.ProtocolVersion)
		return 0
	case "help", "-h", "--help":
		fmt.Fprintf(stdout, usage, datadir.Default(), datadir.EnvVar)
		return 0
	default:
		fmt.Fprintf(stderr, "unknown command %q\n\n", args[0])
		fmt.Fprintf(stderr, usage, datadir.Default(), datadir.EnvVar)
		return 2
	}
}

func cmdPair(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("pair", flag.ContinueOnError)
	fs.SetOutput(stderr)
	hub := fs.String("hub", "", "hub base URL, e.g. https://hub.example.lan:8443 (required)")
	code := fs.String("code", "", "one-time pairing code from the hub GUI, e.g. ABCD-1234 (required)")
	name := fs.String("name", "", "node name (default: sanitized hostname)")
	dir := fs.String("data-dir", "", "data directory (default: "+datadir.Default()+")")
	force := fs.Bool("force", false, "pair again even if this node is already paired")
	role := fs.String("role", "", "gpu (default: runs models), tools (no models; read-only capabilities such as Docker and logs) or dev (no models; the dev workshop: repo.* capabilities)")
	var tf trustFlags
	tf.register(fs)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *hub == "" || *code == "" {
		fmt.Fprintln(stderr, "pair: --hub and --code are required")
		fs.Usage()
		return 2
	}
	if !validRole(*role) {
		fmt.Fprintln(stderr, "pair: --role must be gpu, tools or dev")
		return 2
	}
	d, err := store.Open(datadir.Resolve(*dir))
	if err != nil {
		fmt.Fprintln(stderr, "pair:", err)
		return 1
	}
	id, err := pairing.Pair(ctx, tf.apply(pairing.Options{
		HubURL:       *hub,
		Code:         *code,
		Name:         *name,
		Role:         *role,
		AgentVersion: version.Version,
		Dir:          d,
		Force:        *force,
	}))
	if err != nil {
		fmt.Fprintln(stderr, "pair:", err)
		return 1
	}
	fmt.Fprintf(stdout, "paired: node_id=%s hub=%s data_dir=%s\n", id.NodeID, id.HubURL, d.Path)
	return 0
}

// validRole accepts the roles a node can be paired for ("" = the default, gpu).
func validRole(r string) bool {
	return r == "" || r == "gpu" || r == "tools" || r == "dev" || r == "build"
}

// trustFlags are the hub trust options shared by pair and install (D11).
type trustFlags struct {
	fingerprint, caFile *string
	insecureHTTP        *bool
}

func (t *trustFlags) register(fs *flag.FlagSet) {
	t.fingerprint = fs.String("ca-fingerprint", "", "pin the hub CA, sha256:<hex> as shown in the hub GUI")
	t.caFile = fs.String("ca-file", "", "trust this CA certificate (PEM) for the hub instead of the system store")
	t.insecureHTTP = fs.Bool("insecure-http", false, "allow a plain http:// hub URL (tests and local development only)")
}

func (t *trustFlags) apply(o pairing.Options) pairing.Options {
	o.CAFingerprint, o.CAFile, o.InsecureHTTP = *t.fingerprint, *t.caFile, *t.insecureHTTP
	return o
}

func parseLevel(s string) (slog.Level, error) {
	var l slog.Level
	err := l.UnmarshalText([]byte(strings.ToUpper(s)))
	return l, err
}

// cmdInputHelper runs the helper in the user's session (D24). Its console is hidden, so what it
// has to say goes to a small log file in the user's cache directory.
func cmdInputHelper(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("input-helper", flag.ContinueOnError)
	fs.SetOutput(stderr)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logf := func(string, ...any) {}
	if dir, err := os.UserCacheDir(); err == nil {
		if lf, err := logfile.Open(filepath.Join(dir, service.Name, "input-helper.log"), 256<<10, 1); err == nil {
			defer lf.Close() //nolint:errcheck // best effort
			l := slog.New(slog.NewTextHandler(lf, nil))
			logf = func(format string, a ...any) { l.Info(fmt.Sprintf(format, a...)) }
		}
	}
	switch err := inputhelper.Run(service.AccountSID(service.Name), logf); {
	case err == nil, errors.Is(err, inputhelper.ErrAlreadyRunning):
		return 0
	default:
		logf("input helper ended: %v", err)
		fmt.Fprintln(stderr, "input-helper:", err)
		return 1
	}
}

// cmdRestartProxy runs the Docker write proxy of E3-01b: exactly one request shape goes through.
func cmdRestartProxy(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("docker-restart-proxy", flag.ContinueOnError)
	fs.SetOutput(stderr)
	listen := fs.String("listen", "127.0.0.1:2376", "loopback address to listen on")
	docker := fs.String("docker", "unix:///var/run/docker.sock", "Docker endpoint: unix:///path or tcp://127.0.0.1:port")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger := slog.New(slog.NewTextHandler(stderr, nil))
	logger.Info("docker restart proxy", "listen", *listen, "docker", *docker)
	if err := restartproxy.Serve(ctx, *listen, *docker, logger); err != nil {
		fmt.Fprintln(stderr, "docker-restart-proxy:", err)
		return 1
	}
	return 0
}

func cmdRun(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("data-dir", "", "data directory (default: "+datadir.Default()+")")
	useFake := fs.Bool("fake", false, "use fake GPU/runner/watcher/sysinfo providers (dev and tests)")
	profile := fs.String("fake-profile", "", "JSON fake profile (with --fake); default: built-in profile")
	control := fs.String("fake-control", "", "JSON file with other programs' VRAM/GPU use on the fake machine (with --fake), re-read when it changes")
	level := fs.String("log-level", "info", "log level: debug, info, warn, error")
	logFile := fs.String("log-file", "", "also write the log to this file (rotated at 10 MB)")
	stateDir := fs.String("update-state-dir", "", "the updater's own directory (heartbeat, result); default: inside the data dir")
	buildSockets := fs.String("build-sockets", "", "directory for the build service's socket (role build); empty = no build service")
	buildUser := fs.String("build-user", "topcoat-build", "the OS user the build service runs as (with --build-sockets)")
	buildWork := fs.String("build-work", "/srv/topcoat/build", "the build service's own directory (with --build-sockets)")
	hubUpdate := fs.String("hub-update-script", "", "path of deploy/update-hub.sh on this host: offers the roll-out step hub.update (production host only)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	lvl, err := parseLevel(*level)
	if err != nil {
		fmt.Fprintln(stderr, "run: invalid --log-level:", *level)
		return 2
	}
	logOut := stderr
	if *logFile != "" {
		path := *logFile
		if path == autoLogFile {
			path = filepath.Join(datadir.Resolve(*dir), "logs", "topcoat-node.log")
		}
		lf, err := logfile.Open(path, 10<<20, 3)
		if err != nil {
			fmt.Fprintln(stderr, "run: open log file:", err)
			return 1
		}
		defer lf.Close()
		logOut = io.MultiWriter(stderr, lf)
	}
	logger := slog.New(slog.NewTextHandler(logOut, &slog.HandlerOptions{Level: lvl}))
	if (*profile != "" || *control != "") && !*useFake {
		logger.Error("--fake-profile and --fake-control require --fake")
		return 2
	}

	d, err := store.Open(datadir.Resolve(*dir))
	if err != nil {
		logger.Error("open data dir", "err", err)
		return 1
	}
	id, err := d.LoadIdentity()
	if err != nil {
		logger.Error("load identity", "err", err, "data_dir", d.Path)
		return 1
	}

	key, err := d.LoadPrivateKey()
	if err != nil {
		logger.Error("load node key", "err", err, "data_dir", d.Path)
		return 1
	}
	tlsCfg, err := hubtls.ForIdentity(d, id)
	if err != nil {
		logger.Error("hub TLS setup", "err", err, "hub", id.HubURL)
		return 1
	}
	if tlsCfg == nil {
		logger.Warn("plain HTTP to the hub (--insecure-http): only for tests and local development")
	}

	opts := agent.Options{
		Identity:     id,
		Key:          key,
		Dir:          d,
		AgentVersion: version.Version,
		Logger:       logger,
		HTTPClient:   hubtls.Client(tlsCfg, 0),
	}
	if *useFake {
		p := fake.DefaultProfile()
		if *profile != "" {
			if p, err = fake.LoadProfile(*profile); err != nil {
				logger.Error("fake profile", "err", err)
				return 1
			}
		}
		world, err := fake.NewWorld(p)
		if err != nil {
			logger.Error("fake profile", "err", err)
			return 1
		}
		opts.GPU = world.GPU()
		opts.SysInfo = world.SysInfo()
		opts.Activity = world.Activity()
		opts.Network = world.Network()
		opts.Addresses = netinfo.Addresses
		opts.Sleeper = world.Sleeper()
		opts.Watcher = fake.NewWatcher()
		fr := world.Runner()
		opts.NewRunner = func(protocol.RunnerConfig) runner.ModelRunner { return fr }
		opts.NewCapabilities = func(*protocol.ToolsConfig) *capability.Registry { return fake.NewToolsRegistry() }
		if *control != "" {
			go world.WatchControl(ctx, *control, time.Second, logger)
		}
		logger.Warn("running with FAKE providers; nothing here touches real hardware")
	} else {
		opts.GPU = gpu.NewSystem()
		opts.SysInfo = sysinfo.NewSystem()
		opts.Activity = activity.NewSystem()
		opts.Network = netinfo.NewSystem()
		opts.Addresses = netinfo.Addresses
		opts.Sleeper = power.NewSystem()
		opts.Watcher = watcher.NewSystem()
		opts.NewRunner = realRunner(d.Path, opts.GPU, opts.SysInfo, logger)
		opts.NewCapabilities = realCapabilities(logger, d.Path)
		opts.NewImageRunner = realImageRunner(d.Path, opts.GPU, logger)
	}

	var bus *servicebus.Bus
	if *buildSockets != "" {
		if bus, err = buildBus(ctx, *buildSockets, *buildUser, *buildWork, logger); err != nil {
			logger.Error("build service", "err", err)
			return 1
		}
		inner := opts.NewCapabilities
		opts.NewCapabilities = func(c *protocol.ToolsConfig) *capability.Registry {
			reg := inner(c)
			if err := buildcap.Register(reg, bus); err != nil {
				logger.Warn("build steps are off", "err", err)
			}
			return reg
		}
	}

	if *hubUpdate != "" {
		inner := opts.NewCapabilities
		opts.NewCapabilities = func(c *protocol.ToolsConfig) *capability.Registry {
			reg := inner(c)
			if err := reg.Register(&hubupdate.Updater{Script: *hubUpdate, Runner: buildsvc.ExecRunner{Timeout: 20 * time.Minute}}); err != nil {
				logger.Warn("the roll-out step is off", "err", err)
			}
			return reg
		}
	}

	keys, err := update.ParseKeys(version.UpdateKeys)
	if err != nil {
		logger.Error("this build carries an invalid update key; updates are off", "err", err)
		keys = nil
	}
	opts.Update = &agent.UpdateOptions{Keys: keys, StateDir: *stateDir}

	a, err := agent.New(opts)
	if err != nil {
		logger.Error("start agent", "err", err)
		return 1
	}
	if bus != nil {
		if err := handleUploads(bus, a, *buildWork, logger); err != nil {
			logger.Error("build service", "err", err)
			return 1
		}
	}
	logger.Info("topcoat-node starting", "version", version.Version, "protocol", version.ProtocolVersion,
		"node_id", id.NodeID, "hub", id.HubURL, "data_dir", d.Path)
	if err := a.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		logger.Error("agent stopped with error", "err", err)
		return 1
	}
	return 0
}

// newUpdateHost is replaced in tests so that no service is touched.
var newUpdateHost = service.NewHost

// exitUpdated is the exit code of an updater that applied an update: non-zero on purpose, so
// that the service manager starts the program that is now installed.
const exitUpdated = 3

// cmdUpdater is the privileged updater service (E2-06, D50). It runs with more rights than the
// node, trusts only the keys compiled into this program and the bytes it reads itself.
func cmdUpdater(ctx context.Context, args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("updater", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("data-dir", "", "data directory (default: "+datadir.Default()+")")
	level := fs.String("log-level", "info", "log level: debug, info, warn, error")
	logFile := fs.String("log-file", "", "also write the log to this file (rotated at 10 MB)")
	stateDir := fs.String("update-state-dir", "", "the updater's own directory (heartbeat, result); default: inside the data dir")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	lvl, err := parseLevel(*level)
	if err != nil {
		fmt.Fprintln(stderr, "updater: invalid --log-level:", *level)
		return 2
	}
	dataDir := datadir.Resolve(*dir)
	logOut := stderr
	if *logFile != "" {
		path := *logFile
		if path == autoLogFile {
			path = filepath.Join(dataDir, "logs", "topcoat-node-updater.log")
		}
		lf, err := logfile.Open(path, 10<<20, 3)
		if err != nil {
			fmt.Fprintln(stderr, "updater: open log file:", err)
			return 1
		}
		defer lf.Close()
		logOut = io.MultiWriter(stderr, lf)
	}
	logger := slog.New(slog.NewTextHandler(logOut, &slog.HandlerOptions{Level: lvl}))
	keys, err := update.ParseKeys(version.UpdateKeys)
	if err != nil {
		logger.Error("this build carries an invalid update key", "err", err)
		return 1
	}
	if len(keys) == 0 {
		// Nothing to verify against: stay idle (and without a heartbeat) instead of restarting in
		// a loop; the node reports the updater as missing.
		logger.Warn("this build carries no update key; the updater stays idle")
		<-ctx.Done()
		return 0
	}
	u := &update.Updater{
		Keys:      keys,
		Layout:    update.NewLayout(dataDir).WithState(*stateDir),
		Host:      newUpdateHost(),
		Plat:      update.Platform{OS: runtime.GOOS, Arch: runtime.GOARCH},
		Installed: version.Version,
		Log:       logger,
	}
	logger.Info("topcoat-node updater starting", "version", version.Version, "keys", update.KeyIDs(keys), "data_dir", dataDir)
	if err := service.EnsureUpdaterRestarts(); err != nil {
		logger.Warn("the service manager may not restart the updater after an update", "err", err)
	}
	switch err := u.Run(ctx); {
	case errors.Is(err, update.ErrApplied):
		return exitUpdated
	case err == nil || errors.Is(err, context.Canceled):
		return 0
	default:
		logger.Error("updater stopped with error", "err", err)
		return 1
	}
}

// newInstaller is replaced in tests so that nothing is downloaded.
var newInstaller = llamacpp.NewInstaller

// autoLogFile makes run log to <data dir>/logs/topcoat-node.log.
const autoLogFile = "auto"

// realRunner returns the runner factory for config.runner: the node-managed
// llama-server with the backend picked from the detected GPUs (or the
// config override) and the pinned llama.cpp release. Configurations that
// cannot work on this node give an Unavailable runner (state "degraded" with
// the reason) instead of crashing.
func realRunner(dataDir string, gpus gpu.GPUProvider, sys sysinfo.Provider, logger *slog.Logger) func(protocol.RunnerConfig) runner.ModelRunner {
	return func(c protocol.RunnerConfig) runner.ModelRunner {
		if c.Type == protocol.RunnerNone {
			return runner.None{} // role tools: no llama.cpp, no GPU probing
		}
		if c.Type != protocol.RunnerLlamaServer {
			return runner.Unavailable{RunnerType: c.Type, Reason: fmt.Errorf("runner type %q is only available with --fake", c.Type)}
		}
		pins := llamacpp.Pinned()
		release := c.LlamaCppRelease
		if release == "" {
			release = pins.Default
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		detected, derr := gpus.GPUs(ctx)
		cancel()
		if msg := gpu.Diagnose(derr); msg != "" {
			logger.Warn(msg, "err", derr, "gpus", len(detected))
		}
		if len(detected) == 0 && (c.Backend == "" || c.Backend == protocol.BackendAuto) {
			logger.Warn("no GPU detected; using the CPU backend", "err", derr)
		}
		vendors := make([]string, 0, len(detected))
		for _, g := range detected {
			vendors = append(vendors, g.Vendor)
		}
		backend := llamacpp.SelectBackend(c.Backend, vendors)
		if _, err := pins.Build(release, runtime.GOOS, backend); err != nil {
			return runner.Unavailable{RunnerType: c.Type, Reason: err}
		}
		logger.Info("llama.cpp runtime selected", "release", release, "backend", backend,
			"configured_backend", c.Backend, "gpus", len(detected), "threads", c.Threads)
		var perModel map[string]runner.ModelBackend
		for m, mb := range c.ModelBackends {
			if mb.Backend != "" {
				if _, err := pins.Build(release, runtime.GOOS, mb.Backend); err != nil {
					logger.Warn("model backend not available; the model uses the node backend", "model", m, "backend", mb.Backend, "err", err)
					mb.Backend = ""
				}
			}
			if perModel == nil {
				perModel = map[string]runner.ModelBackend{}
			}
			perModel[m] = runner.ModelBackend{Backend: mb.Backend, GPULayers: mb.GPULayers, NoHost: mb.NoHost, NCPUMoe: mb.NCPUMoe}
		}
		guardMode, guardReserve := c.EffectiveGuard()
		return runner.NewLlamaServer(runner.LlamaServerOptions{
			DataDir:        dataDir,
			ModelDirs:      c.ModelDirs,
			Backend:        backend,
			Release:        release,
			Threads:        c.Threads,
			Installer:      newInstaller(filepath.Join(dataDir, "runtime")),
			GPU:            gpus,
			Logger:         logger,
			ModelBackends:  perModel,
			SysInfo:        sys,
			GuardMode:      guardMode,
			GuardReserveMB: guardReserve,
			MaxContext:     c.MaxContextLength,
		})
	}
}

// realImageRunner returns the factory of the image runner for config.images (D126): the pinned
// stable-diffusion.cpp build for this platform and GPU, installed below <data dir>/runtime-sd when
// the first image model loads.
func realImageRunner(dataDir string, gpus gpu.GPUProvider, logger *slog.Logger) func(*protocol.ImagesConfig) runner.ImageRunner {
	return func(c *protocol.ImagesConfig) runner.ImageRunner {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		detected, _ := gpus.GPUs(ctx)
		cancel()
		vendors := make([]string, 0, len(detected))
		for _, g := range detected {
			vendors = append(vendors, g.Vendor)
		}
		pins := sdcpp.Pinned()
		backend := sdcpp.SelectBackend(runtime.GOOS, c.Backend, vendors)
		if _, err := pins.Build(pins.Default, runtime.GOOS, backend); err != nil {
			logger.Warn("no stable-diffusion.cpp build for this backend; using the CPU", "backend", backend, "err", err)
			backend = llamacpp.BackendCPU
		}
		logger.Info("image runner selected", "release", pins.Default, "backend", backend)
		s := runner.NewSDServer(runner.SDServerOptions{
			DataDir:   dataDir,
			ModelDirs: c.ModelDirs,
			Backend:   backend,
			Release:   pins.Default,
			Installer: sdcpp.NewInstaller(filepath.Join(dataDir, "runtime-sd")),
			Logger:    logger,
		})
		s.EnsureDir()
		return s
	}
}

// realCapabilities returns the factory of the capability registry for config.tools (E2-04):
// Docker over the configured socket or loopback proxy, journalctl and systemctl by fixed
// argument lists, /proc for the rest. A Docker host that is configured wrongly leaves the
// docker.* and compose.* capabilities answering with the reason; the others still work.
func realCapabilities(logger *slog.Logger, dataDir string) func(*protocol.ToolsConfig) *capability.Registry {
	return func(c *protocol.ToolsConfig) *capability.Registry {
		var o tools.Options
		// a container node with the host's /proc mounted read-only (system.memory sees the host)
		o.HostProc = os.Getenv("TOPCOAT_HOST_PROC")
		host, connectorsFile := "", ""
		if c != nil {
			host, o.ComposeRoots, connectorsFile = c.DockerHost, c.ComposeRoots, c.ConnectorsFile
			o.DocsRoots, o.DocsExclude = c.DocsRoots, c.DocsExclude
			o.ReadRoots = c.ReadRoots
			o.RestartContainers, o.RestartUnits = c.RestartContainers, c.RestartUnits
			if c.ShellSocket != "" {
				o.Shell = tools.SocketShell{Path: c.ShellSocket}
			}
			o.Web = tools.WebOptions{SearxngURL: c.SearxngURL, Fetch: c.WebFetch, PrivateNames: c.PrivateNames}
			o.Checks = tools.CheckOptions{DNSNames: c.DNSNames, CertTargets: c.CertTargets}
			for _, n := range c.NetTargets {
				o.Checks.NetTargets = append(o.Checks.NetTargets, tools.NetTarget{Name: n.Name, Host: n.Host, Ports: n.Ports, Ping: n.Ping})
			}
		}
		// a Windows node offers the windows.* capabilities; they run only where an admin added them
		// to config.capabilities (the same switch as every other capability)
		if runtime.GOOS == "windows" {
			o.Windows = tools.PowerShell{}
		}
		// the credentials of the connector capabilities (Uptime Kuma, Omada) are in a file that is
		// only read here; without it, or with a section missing, those capabilities are not offered
		switch conns, err := tools.LoadConnectors(connectorsFile); {
		case err == nil:
			o.Connectors = conns
			logger.Info("connectors configured", "uptime_kuma", conns.Kuma != nil, "omada", conns.Omada != nil)
		case errors.Is(err, tools.ErrNoConnectorsFile):
		default:
			logger.Warn("connector capabilities are off", "err", err) // the message holds no secret
		}
		d, err := tools.NewDocker(host)
		if err != nil {
			logger.Warn("docker capabilities are off", "err", err)
		} else {
			o.Docker = d
		}
		// the write path to Docker is a second, separate proxy and exists only when it is configured
		if c != nil && c.DockerWriteHost != "" {
			if w, err := tools.NewDockerWriter(c.DockerWriteHost); err != nil {
				logger.Warn("docker.restart is off", "err", err)
			} else {
				o.DockerWrite = w
			}
		}
		reg := tools.NewRegistry(o)
		if c != nil && c.Workshop != nil {
			addWorkshop(reg, c.Workshop, dataDir, logger)
		}
		return reg
	}
}

// addWorkshop registers the repo.* capabilities of a dev node (E6-01 S1, D52). A workshop that
// cannot start leaves them out and says why; the other capabilities still work.
func addWorkshop(reg *capability.Registry, c *protocol.WorkshopConfig, dataDir string, logger *slog.Logger) {
	if _, err := exec.LookPath("git"); err != nil {
		logger.Warn("workshop capabilities are off: git is not installed")
		return
	}
	o := workshop.Options{Dir: c.Dir, AllowedHosts: c.AllowedHosts, MaxRepoMB: c.MaxRepoMB}
	if o.Dir == "" {
		o.Dir = filepath.Join(dataDir, "workshop")
	}
	if c.Runner != nil && c.Runner.Type == protocol.WorkshopRunnerDocker {
		r, err := workshop.NewDockerRunner(c.Runner.DockerHost)
		if err != nil {
			logger.Warn("repo.test is off", "err", err)
		} else {
			o.Runner, o.RunnerConfig = r, *c.Runner
			// the browser.* capabilities run in the same Docker, with the image the configuration
			// names (E6-02); without an image they are not offered. Until 0.2.12 nothing set this,
			// so a configured browser image was never used (2026-11-15).
			if c.Runner.Images[protocol.WorkshopBrowser] != "" {
				o.Browser = r
			}
		}
	}
	w, err := workshop.New(o)
	if err != nil {
		logger.Warn("workshop capabilities are off", "err", err)
		return
	}
	for _, cp := range w.Registry() {
		if err := reg.Register(cp); err != nil {
			logger.Warn("workshop capability not registered", "capability", cp.Name(), "err", err)
		}
	}
	logger.Info("workshop ready", "dir", o.Dir, "hosts", o.AllowedHosts, "repo_test", o.Runner != nil, "browser", o.Browser != nil)
}

// serviceDataDir is the data dir the service uses: the flag, else the
// platform default for services (/var/lib/topcoat-node, %ProgramData%\topcoat-node).
func serviceDataDir(flagValue string) string {
	if flagValue != "" {
		return flagValue
	}
	if runtime.GOOS == "windows" {
		return datadir.Default()
	}
	return "/var/lib/topcoat-node"
}

func cmdInstall(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("install", flag.ContinueOnError)
	fs.SetOutput(stderr)
	hub := fs.String("hub", "", "hub base URL, e.g. https://hub.example.lan:8443")
	code := fs.String("code", "", "one-time pairing code from the hub GUI (not needed if already paired)")
	name := fs.String("name", "", "node name (default: sanitized hostname)")
	dir := fs.String("data-dir", "", "data directory (default: "+serviceDataDir("")+")")
	force := fs.Bool("force", false, "pair again even if this data dir is already paired")
	noStart := fs.Bool("no-start", false, "register the service but do not start it")
	role := fs.String("role", "", "gpu (default: runs models), tools (no models; read-only capabilities such as Docker and logs) or dev (no models; the dev workshop: repo.* capabilities)")
	account := fs.String("service-user", "", "Linux: run the service as this existing account instead of its own (the dev node's rootless Docker belongs to its account)")
	var tf trustFlags
	tf.register(fs)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if !validRole(*role) {
		fmt.Fprintln(stderr, "install: --role must be gpu, tools or dev")
		return 2
	}
	dataDir := serviceDataDir(*dir)
	pair := func(path string) error {
		d, err := store.Open(path)
		if err != nil {
			return err
		}
		if d.HasIdentity() && !*force {
			fmt.Fprintf(stdout, "already paired (%s); keeping the existing identity\n", path)
			return nil
		}
		if *hub == "" || *code == "" {
			return errors.New("--hub and --code are required to pair this node")
		}
		id, err := pairing.Pair(ctx, tf.apply(pairing.Options{
			HubURL: *hub, Code: *code, Name: *name, Role: *role, AgentVersion: version.Version, Dir: d, Force: *force,
		}))
		if err != nil {
			return err
		}
		fmt.Fprintf(stdout, "paired: node_id=%s hub=%s\n", id.NodeID, id.HubURL)
		return nil
	}
	if err := service.Install(service.InstallOptions{DataDir: dataDir, Pair: pair, NoStart: *noStart, Account: *account, Out: stdout}); err != nil {
		fmt.Fprintln(stderr, "install:", err)
		return 1
	}
	return 0
}

func cmdUninstall(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("data-dir", "", "data directory (default: "+serviceDataDir("")+")")
	purge := fs.Bool("purge", false, "also delete the data dir (node key, downloaded models, llama.cpp builds)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if err := service.Uninstall(service.UninstallOptions{DataDir: serviceDataDir(*dir), Purge: *purge, Out: stdout}); err != nil {
		fmt.Fprintln(stderr, "uninstall:", err)
		return 1
	}
	return 0
}

// restartService is replaced in tests.
var restartService = service.Restart

func cmdSetHub(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("set-hub", flag.ContinueOnError)
	fs.SetOutput(stderr)
	hub := fs.String("hub", "", "new hub base URL, e.g. https://hub.example.lan:8443 (required)")
	dir := fs.String("data-dir", "", "data directory (default: "+serviceDataDir("")+")")
	noRestart := fs.Bool("no-restart", false, "only write node.json, do not restart the service")
	insecure := fs.Bool("insecure-http", false, "allow a plain http:// hub URL (tests and local development only)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *hub == "" {
		fmt.Fprintln(stderr, "set-hub: --hub is required")
		fs.Usage()
		return 2
	}
	d, err := store.Open(serviceDataDir(*dir))
	if err != nil {
		fmt.Fprintln(stderr, "set-hub:", err)
		return 1
	}
	res, err := sethub.Move(ctx, sethub.Options{Dir: d, HubURL: *hub, InsecureHTTP: *insecure})
	if err != nil {
		fmt.Fprintln(stderr, "set-hub:", err)
		return 1
	}
	fmt.Fprintf(stdout, "hub address changed: %s -> %s (node_id=%s)\n", res.Old.HubURL, res.New.HubURL, res.New.NodeID)
	if res.CA != "" {
		fmt.Fprintf(stdout, "the certificate of the new hub verifies against the CA this node pinned (%s)\n", res.CA)
	}
	if *noRestart {
		fmt.Fprintln(stdout, "restart the node to use the new address")
		return 0
	}
	switch err := restartService(service.RestartOptions{Out: stdout}); {
	case err == nil:
	case errors.Is(err, service.ErrNotInstalled):
		fmt.Fprintln(stdout, "no service is installed here: restart `topcoat-node run` yourself to use the new address")
	default:
		fmt.Fprintf(stderr, "set-hub: the address is saved, but the service could not be restarted: %v\nrestart it yourself to use the new address\n", err)
		return 1
	}
	return 0
}
