package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"net/url"
	"path"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Runner types.
const (
	RunnerLlamaServer = "llamaserver"
	RunnerFake        = "fake"
	// RunnerNone is a node without models (role tools): nothing to download, nothing to load.
	RunnerNone = "none"
)

// DefaultHeartbeatIntervalSeconds is used when the config does not set one.
const DefaultHeartbeatIntervalSeconds = 10

// RunnerConfig selects and configures the model runner.
type RunnerConfig struct {
	Type      string   `json:"type"`
	ModelDirs []string `json:"model_dirs,omitempty"`
	// Backend is auto (default), cuda, vulkan or cpu.
	Backend string `json:"backend,omitempty"`
	// LlamaCppRelease pins the llama.cpp release (empty = the node's default pin).
	LlamaCppRelease string `json:"llamacpp_release,omitempty"`
	// Threads is the default CPU thread count (0 = runner default).
	Threads int `json:"threads,omitempty"`
	// DefaultContextLength is -c for loads that do not ask for a context (0 = runner
	// default, 8192). ModelContextLengths overrides it per model name.
	DefaultContextLength int `json:"default_context_length,omitempty"`
	// MaxContextLength lets the node grow the context per slot of a load that asks for "at least"
	// a context (models.load min_context_length), up to this cap, as far as the memory holds;
	// 0 = no growth: a load gets the context it asks for, the smallest the work needs.
	MaxContextLength    int            `json:"max_context_length,omitempty"`
	ModelContextLengths map[string]int `json:"model_context_lengths,omitempty"`
	// NParallel is the number of llama-server slots (-np) a model serves at the same time (0 =
	// runner default, 1); ModelNParallel overrides it per model name. The context length of a
	// load is the context of ONE slot: the server is started with -c = context x slots, so a
	// request never shares its window with another one. More slots cost KV memory (x slots) and
	// let long generations run side by side; 1-2 suit background and agent use.
	NParallel      int            `json:"n_parallel,omitempty"`
	ModelNParallel map[string]int `json:"model_n_parallel,omitempty"`
	// Jinja is whether llama-server uses the model's own chat template engine (--jinja, the
	// prerequisite of native tool calls: OpenAI-compatible tools / tool_calls); nil = yes.
	// ModelJinja overrides it per model name (false = --no-jinja, e.g. for a model whose template
	// the engine cannot read).
	Jinja      *bool           `json:"jinja,omitempty"`
	ModelJinja map[string]bool `json:"model_jinja,omitempty"`
	// ModelBackends sets backend and GPU offload per model (E2-05, D27): a 7B on Vulkan and a
	// 30B on the CPU side by side. Absent = the node's backend and llama.cpp's own fit.
	ModelBackends map[string]ModelBackend `json:"model_backends,omitempty"`
	// MemoryGuard checks free RAM before a load (E2-05, D27); nil = refuse with 1024 MB reserve.
	MemoryGuard *MemoryGuard `json:"memory_guard,omitempty"`
}

// ModelBackend is the per-model backend override (config.runner.model_backends).
type ModelBackend struct {
	// Backend is cuda, vulkan or cpu; empty = the node's backend.
	Backend string `json:"backend,omitempty"`
	// GPULayers is the exact number of layers on the GPU (0 = none); nil = llama.cpp fits
	// as many as the device memory holds (--fit).
	GPULayers *int `json:"gpu_layers,omitempty"`
	// NoHost passes llama-server --no-host: no pinned host buffers of the GPU backend. An
	// experiment for a partial offload on an iGPU; UNVERIFIED on real hardware.
	NoHost bool `json:"no_host,omitempty"`
	// NCPUMoe keeps the expert weights of the first N layers of a Mixture-of-Experts model in
	// the system RAM (llama-server --n-cpu-moe) while the rest sits on the GPU. For a discrete
	// GPU with less VRAM than the model needs; nil = off. Without GPULayers every other layer
	// goes to the GPU (-ngl all, no fit). UNVERIFIED on real hardware.
	NCPUMoe *int `json:"n_cpu_moe,omitempty"`
}

// Memory guard modes (config.runner.memory_guard.mode).
const (
	GuardRefuse = "refuse"
	GuardWarn   = "warn"
	GuardOff    = "off"
)

// AutoGuardReserve is what memory_guard.reserve_mb means when unset or 0: the node keeps
// max(4096 MB, 12 % of the RAM) free after a load (see memfit.AutoReserveMB); 1 GB was too little
// for a 32 GB laptop that also runs its owner's programs.
const AutoGuardReserve = 0

// MemoryGuard mirrors config.runner.memory_guard.
type MemoryGuard struct {
	// Mode is refuse (default), warn (load anyway, report the shortfall) or off.
	Mode      string `json:"mode,omitempty"`
	ReserveMB *int64 `json:"reserve_mb,omitempty"`
}

// EffectiveGuard returns the mode and the reserve with their defaults.
func (r RunnerConfig) EffectiveGuard() (mode string, reserveMB int64) {
	mode, reserveMB = GuardRefuse, AutoGuardReserve
	if g := r.MemoryGuard; g != nil {
		if g.Mode != "" {
			mode = g.Mode
		}
		if g.ReserveMB != nil {
			reserveMB = *g.ReserveMB
		}
	}
	return mode, reserveMB
}

// Context length limits for config.runner (a sanity range, not what a model supports).
const (
	MinContextLength = 256
	MaxContextLength = 1 << 20
)

// ContextFor returns the context length a load of model without an explicit one uses:
// the resident entry, else the per-model setting, else the node default; 0 means "runner
// default".
func (c *NodeConfig) ContextFor(model string) int {
	if r, ok := c.Resident(model); ok && r.ContextLength > 0 {
		return r.ContextLength
	}
	if n := c.Runner.ModelContextLengths[model]; n > 0 {
		return n
	}
	return c.Runner.DefaultContextLength
}

// absolutePath: /unix/path or C:\windows\path (the node validates configs of either OS).
func absolutePath(f string) bool {
	return strings.HasPrefix(f, "/") || (len(f) > 2 && f[1] == ':' && (f[2] == '\\' || f[2] == '/'))
}

// ValidReadRoot says whether r may be a tools.read_roots entry: an absolute path of either OS that
// is not a filesystem root, not a UNC path and not a device path (\\?\, \\.\), without NUL or
// ".." segments. The node checks it again, on the resolved path, at every call.
func ValidReadRoot(r string) error {
	if !absolutePath(r) {
		return fmt.Errorf("%q is not an absolute path", r)
	}
	if len(r) > 512 || strings.ContainsRune(r, 0) {
		return fmt.Errorf("a root is too long or holds a NUL")
	}
	if strings.HasPrefix(r, "//") || strings.HasPrefix(r, `\\`) || strings.HasPrefix(r, `/\`) || strings.HasPrefix(r, `\/`) {
		return fmt.Errorf("%q is a UNC or device path", r)
	}
	body := r
	if len(r) > 2 && r[1] == ':' {
		body = r[2:]
	}
	segments := 0
	for _, seg := range strings.FieldsFunc(body, func(c rune) bool { return c == '/' || c == '\\' }) {
		if seg == ".." || seg == "." {
			return fmt.Errorf("%q must not hold . or .. segments", r)
		}
		segments++
	}
	if segments == 0 {
		return fmt.Errorf("%q is a filesystem root; name a folder", r)
	}
	if len(r) > 2 && r[1] == ':' && strings.ContainsAny(body, ":") {
		return fmt.Errorf("%q holds a stream or drive marker", r)
	}
	return nil
}

// JinjaFor reports whether a load of model runs llama-server with --jinja: the per-model setting,
// else the node default, else yes.
func (c *NodeConfig) JinjaFor(model string) bool {
	if v, ok := c.Runner.ModelJinja[model]; ok {
		return v
	}
	if c.Runner.Jinja != nil {
		return *c.Runner.Jinja
	}
	return true
}

// MaxParallel is the highest number of slots (config.runner.n_parallel) the node accepts.
const MaxParallel = 16

// ParallelFor returns the number of slots a load of model uses: the per-model setting, else the
// node default; 0 means "runner default" (1).
func (c *NodeConfig) ParallelFor(model string) int {
	if n := c.Runner.ModelNParallel[model]; n > 0 {
		return n
	}
	return c.Runner.NParallel
}

// Backends (config.runner.backend).
const (
	BackendAuto   = "auto"
	BackendCUDA   = "cuda"
	BackendVulkan = "vulkan"
	BackendCPU    = "cpu"
)

// Equal reports whether two runner configs are the same.
func (r RunnerConfig) Equal(o RunnerConfig) bool {
	if r.Type != o.Type || r.Backend != o.Backend || r.LlamaCppRelease != o.LlamaCppRelease ||
		r.Threads != o.Threads || len(r.ModelDirs) != len(o.ModelDirs) {
		return false
	}
	for i := range r.ModelDirs {
		if r.ModelDirs[i] != o.ModelDirs[i] {
			return false
		}
	}
	return r.guardKey() == o.guardKey() && backendsKey(r.ModelBackends) == backendsKey(o.ModelBackends)
}

func (r RunnerConfig) guardKey() string {
	m, res := r.EffectiveGuard()
	return fmt.Sprintf("%s/%d", m, res)
}

func backendsKey(m map[string]ModelBackend) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		layers := "auto"
		if l := m[k].GPULayers; l != nil {
			layers = strconv.Itoa(*l)
		}
		moe := "off"
		if n := m[k].NCPUMoe; n != nil {
			moe = strconv.Itoa(*n)
		}
		fmt.Fprintf(&b, "%s=%s/%s/%t/%s;", k, m[k].Backend, layers, m[k].NoHost, moe)
	}
	return b.String()
}

// WatcherRule matches a process by executable name or by a folder prefix of
// the executable path (both case-insensitive).
type WatcherRule struct {
	Process    string `json:"process,omitempty"`
	PathPrefix string `json:"path_prefix,omitempty"`
}

// DefaultCooldownSeconds is how long the node stays busy_gaming after the last
// watched process ended when the config does not say otherwise.
const DefaultCooldownSeconds = 60

// WatcherConfig configures the game / GPU-heavy process watcher.
type WatcherConfig struct {
	GraceSeconds int `json:"grace_seconds,omitempty"`
	// CooldownSeconds nil means DefaultCooldownSeconds.
	CooldownSeconds *int          `json:"cooldown_seconds,omitempty"`
	Rules           []WatcherRule `json:"rules"`
}

// Cooldown returns the effective cooldown.
func (w WatcherConfig) Cooldown() int {
	if w.CooldownSeconds == nil {
		return DefaultCooldownSeconds
	}
	return *w.CooldownSeconds
}

// NodeConfig is the desired node configuration sent by the hub (node_config.schema.json).
type NodeConfig struct {
	Roles                    []string      `json:"roles"`
	Runner                   RunnerConfig  `json:"runner"`
	AllowedActions           []string      `json:"allowed_actions"`
	HeartbeatIntervalSeconds int           `json:"heartbeat_interval_seconds"`
	IdleTTLSeconds           int           `json:"idle_ttl_seconds"`
	Watcher                  WatcherConfig `json:"watcher"`
	Capabilities             []string      `json:"capabilities"`
	// Residents are kept loaded whenever possible (E1-06).
	Residents []Resident `json:"residents,omitempty"`
	// Pressure configures yielding memory to other programs (E1-06); nil = off.
	Pressure *PressureConfig `json:"pressure,omitempty"`
	// Yield configures stepping back for the person using the machine (E2-05 B, D24).
	Yield *YieldConfig `json:"yield,omitempty"`
	// Power is the node's energy profile (E2-05 E).
	Power *PowerConfig `json:"power,omitempty"`
	// Tools configures the read-only capabilities of a tool node (E2-04); nil = defaults.
	Tools *ToolsConfig `json:"tools,omitempty"`
	// Update selects the update channel (E2-06, D50); nil = stable.
	Update *UpdateConfig `json:"update,omitempty"`
	// Transfer configures the direct model transfer between nodes (E2-07, D51); nil = defaults.
	Transfer *TransferConfig `json:"transfer,omitempty"`
}

// Direct model transfer defaults (E2-07).
const (
	DefaultTransferPortMin = 47000
	DefaultTransferPortMax = 47099
	DefaultTransferTTL     = 600
)

// TransferConfig mirrors config.transfer; zero fields take the defaults.
type TransferConfig struct {
	PortMin    int `json:"port_min,omitempty"`
	PortMax    int `json:"port_max,omitempty"`
	TTLSeconds int `json:"ttl_seconds,omitempty"`
}

// TransferSettings is the effective transfer configuration.
type TransferSettings struct {
	PortMin, PortMax int
	TTL              time.Duration
}

// EffectiveTransfer applies the defaults.
func (c *NodeConfig) EffectiveTransfer() TransferSettings {
	t := TransferSettings{PortMin: DefaultTransferPortMin, PortMax: DefaultTransferPortMax, TTL: DefaultTransferTTL * time.Second}
	if c.Transfer != nil {
		if c.Transfer.PortMin != 0 {
			t.PortMin = c.Transfer.PortMin
		}
		if c.Transfer.PortMax != 0 {
			t.PortMax = c.Transfer.PortMax
		}
		if c.Transfer.TTLSeconds != 0 {
			t.TTL = time.Duration(c.Transfer.TTLSeconds) * time.Second
		}
	}
	return t
}

func (c *NodeConfig) validateTransfer() error {
	if c.Transfer == nil {
		return nil
	}
	t := c.EffectiveTransfer()
	for name, p := range map[string]int{"port_min": t.PortMin, "port_max": t.PortMax} {
		if p < 1024 || p > 65535 {
			return fmt.Errorf("config: transfer.%s must be 1024..65535", name)
		}
	}
	if t.PortMin > t.PortMax {
		return fmt.Errorf("config: transfer.port_min must be <= port_max")
	}
	if c.Transfer.TTLSeconds != 0 && (c.Transfer.TTLSeconds < 30 || c.Transfer.TTLSeconds > 86400) {
		return fmt.Errorf("config: transfer.ttl_seconds must be 30..86400")
	}
	return nil
}

// DefaultUpdateChannel is the channel of a node whose config does not name one.
const DefaultUpdateChannel = "stable"

var updateChannelRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// UpdateConfig mirrors config.update.
type UpdateConfig struct {
	Channel string `json:"channel,omitempty"`
}

// UpdateChannel is the channel this node follows.
func (c *NodeConfig) UpdateChannel() string {
	if c.Update != nil && c.Update.Channel != "" {
		return c.Update.Channel
	}
	return DefaultUpdateChannel
}

// ToolsConfig mirrors config.tools: where the capabilities find Docker and which directories
// compose files may be read from.
type ToolsConfig struct {
	// DockerHost is unix:///path/to.sock or tcp://127.0.0.1:port (a read-only socket proxy on the
	// loopback); empty = unix:///var/run/docker.sock.
	DockerHost string `json:"docker_host,omitempty"`
	// DockerWriteHost is a second socket proxy that allows exactly "restart a container" (E3-01b);
	// empty = docker.restart is not offered. RestartContainers and RestartUnits are glob lists of
	// what a confirmed proposal may restart; empty = nothing (default deny).
	DockerWriteHost   string   `json:"docker_write_host,omitempty"`
	RestartContainers []string `json:"restart_containers,omitempty"`
	RestartUnits      []string `json:"restart_units,omitempty"`
	// ShellSocket is the unix socket of the readshell unit (deploy/readshell): the node offers
	// shell.read only where it is set. The command runs as an OS user that can only read.
	ShellSocket string `json:"shell_socket,omitempty"`
	// ComposeRoots are the only directories compose and .env files are read from or listed; empty or
	// missing releases none (default deny).
	ComposeRoots []string `json:"compose_roots,omitempty"`
	// DocsRoots are the only directories the knowledge base reads Markdown/text files from (E2-03);
	// empty or missing releases none and docs.list / docs.read are not offered. DocsExclude adds
	// globs to the fixed exclude list (credentials, keys, *SENSIBEL* …); it never shortens it.
	DocsRoots   []string `json:"docs_roots,omitempty"`
	DocsExclude []string `json:"docs_exclude,omitempty"`
	// ReadRoots are the only directories files.list and files.read look into (HOST-01, D89): a
	// person names them per node on the hub. Empty or missing releases none and the two
	// capabilities are not offered. A root is never a filesystem root, a UNC or a device path.
	ReadRoots []string `json:"read_roots,omitempty"`
	// ConnectorsFile is the file with the credentials of the systems the connector capabilities
	// ask (Uptime Kuma, Omada; E2-04c). Only its PATH is configured here: the credentials are in
	// the file, created on the tool node by the maintainer, and never pass the hub. Empty = the
	// default place (/etc/topcoat-node/connectors.json, %ProgramData%\topcoat-node\connectors.json).
	ConnectorsFile string `json:"connectors_file,omitempty"`
	// SearxngURL is the SearXNG instance web.search asks (http or https); empty = web.search is not
	// offered. The URL is configured here and never taken from an input.
	SearxngURL string `json:"searxng_url,omitempty"`
	// WebFetch offers web.fetch: the node then reads public web pages a model names (never a
	// private address). Off by default.
	WebFetch bool `json:"web_fetch,omitempty"`
	// NetTargets, DNSNames and CertTargets are what net.check and certs.expiry look at (E3-04).
	// They are configured here and never named by a caller, so a model cannot turn the node into a
	// port scanner. Without targets the capability is not offered.
	NetTargets  []NetTarget `json:"net_targets,omitempty"`
	DNSNames    []string    `json:"dns_names,omitempty"`
	CertTargets []string    `json:"cert_targets,omitempty"`

	// PrivateNames are names (hosts, users, domains) that web.search and web.fetch refuse to send:
	// the node already refuses its own host name, its domain, its resolver's search domains, IP and
	// MAC addresses, e-mail addresses, internal-looking domains and secrets (E3-04).
	PrivateNames []string `json:"private_names,omitempty"`
	// TimeoutSeconds bounds one capability call (default 20).
	TimeoutSeconds *int `json:"timeout_seconds,omitempty"`

	// Workshop configures the repo.* capabilities of a dev node (E6-01 S1, D52); nil = they are
	// not offered.
	Workshop *WorkshopConfig `json:"workshop,omitempty"`
}

// WorkshopConfig is config.tools.workshop: where the clones live, which hosts may be cloned from
// and how tests run. The repositories themselves (URL, branch, token) are never configured here:
// the hub hands them over with each call.
type WorkshopConfig struct {
	// Dir is the absolute directory of the clones; empty = <data dir>/workshop.
	Dir string `json:"dir,omitempty"`
	// AllowedHosts are the only hosts a repository URL may name; empty = github.com.
	AllowedHosts []string `json:"allowed_hosts,omitempty"`
	// MaxRepoMB bounds the size of one clone; 0 = DefaultWorkshopRepoMB.
	MaxRepoMB int `json:"max_repo_mb,omitempty"`
	// Runner says how repo.test runs a test template; nil or type none = repo.test is not offered.
	Runner *WorkshopRunner `json:"runner,omitempty"`
}

// Workshop runner types.
const (
	WorkshopRunnerNone   = "none"
	WorkshopRunnerDocker = "docker"
)

// DefaultWorkshopRepoMB is the size limit of one clone.
const DefaultWorkshopRepoMB = 500

// WorkshopRunner runs a test template in a throw-away container (D52).
type WorkshopRunner struct {
	Type string `json:"type"`
	// DockerHost is unix:///path or tcp://127.0.0.1:port of the Docker API; empty = the default socket.
	DockerHost string `json:"docker_host,omitempty"`
	// HostDir is the workshop directory as the Docker daemon sees it, when the node itself runs in
	// a container; empty = the same path as Dir.
	HostDir string `json:"host_dir,omitempty"`
	// Images maps a test template (make-check, npm-test, go-test, pytest) to the image it runs in;
	// a template without an image is refused.
	Images map[string]string `json:"images,omitempty"`
	// Network is a Docker network for templates that must download packages; empty = no network.
	Network string `json:"network,omitempty"`
	// NetworkTemplates are the templates that may use Network.
	NetworkTemplates []string `json:"network_templates,omitempty"`
	// MemoryMB, CPUMilli (1000 = one CPU) and TimeoutSeconds bound one run; 0 = the defaults.
	MemoryMB int `json:"memory_mb,omitempty"`
	CPUMilli int `json:"cpu_milli,omitempty"`
	// TimeoutSeconds is the hard upper limit of one run (repo.test: 600 by default; the
	// background run of repo.test_start: 1800).
	TimeoutSeconds int `json:"timeout_seconds,omitempty"`
	// IdleSeconds ends a background run (repo.test_start) that printed nothing new for that long;
	// 0 = the default (180).
	IdleSeconds int `json:"idle_seconds,omitempty"`
}

// Template names a repository's test command comes from (never free text).
var WorkshopTemplates = []string{"make-check", "npm-test", "go-test", "pytest"}

// WorkshopBuildNode is the image/network key of repo.build_node (E6-01 S3). It is not a test
// template: a repository cannot pick it, only the node's configuration names its image.
const WorkshopBuildNode = "build-node"

// WorkshopBrowser is the image key of the browser.* capabilities (E6-02): the container image with the
// headless browser and its helper. Like the build image it is named only by the node's configuration.
const WorkshopBrowser = "browser"

func workshopKey(k string) bool {
	return k == WorkshopBuildNode || k == WorkshopBrowser || slices.Contains(WorkshopTemplates, k)
}

// NetTarget is a host net.check pings and connects to.
type NetTarget struct {
	Name  string `json:"name"`
	Host  string `json:"host"`
	Ports []int  `json:"ports,omitempty"`
	Ping  bool   `json:"ping,omitempty"`
}

func sameNetTargets(a, b []NetTarget) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Name != b[i].Name || a[i].Host != b[i].Host || a[i].Ping != b[i].Ping || len(a[i].Ports) != len(b[i].Ports) {
			return false
		}
		for j := range a[i].Ports {
			if a[i].Ports[j] != b[i].Ports[j] {
				return false
			}
		}
	}
	return true
}

// Equal reports whether two tools configs are the same (nil = empty).
func (t *ToolsConfig) Equal(o *ToolsConfig) bool {
	var a, b ToolsConfig
	if t != nil {
		a = *t
	}
	if o != nil {
		b = *o
	}
	if a.DockerHost != b.DockerHost || a.ShellSocket != b.ShellSocket || a.DockerWriteHost != b.DockerWriteHost ||
		!sameStrings(a.RestartContainers, b.RestartContainers) || !sameStrings(a.RestartUnits, b.RestartUnits) || a.ConnectorsFile != b.ConnectorsFile ||
		a.SearxngURL != b.SearxngURL || a.WebFetch != b.WebFetch || !sameStrings(a.PrivateNames, b.PrivateNames) ||
		!sameStrings(a.ComposeRoots, b.ComposeRoots) || !sameStrings(a.DocsRoots, b.DocsRoots) ||
		!sameStrings(a.DocsExclude, b.DocsExclude) || !sameStrings(a.ReadRoots, b.ReadRoots) || !sameStrings(a.DNSNames, b.DNSNames) ||
		!sameStrings(a.CertTargets, b.CertTargets) || !sameNetTargets(a.NetTargets, b.NetTargets) ||
		!reflect.DeepEqual(a.Workshop, b.Workshop) {
		return false
	}
	at, bt := DefaultToolTimeoutSeconds, DefaultToolTimeoutSeconds
	if a.TimeoutSeconds != nil {
		at = *a.TimeoutSeconds
	}
	if b.TimeoutSeconds != nil {
		bt = *b.TimeoutSeconds
	}
	return at == bt
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// DefaultToolTimeoutSeconds bounds one capability call.
const DefaultToolTimeoutSeconds = 20

// ToolTimeout is the effective time limit of one capability call.
func (c *NodeConfig) ToolTimeout() time.Duration {
	if c.Tools != nil && c.Tools.TimeoutSeconds != nil {
		return time.Duration(*c.Tools.TimeoutSeconds) * time.Second
	}
	return DefaultToolTimeoutSeconds * time.Second
}

// DefaultResidentPriority is used when a resident has no priority.
const DefaultResidentPriority = 50

// Resident is a model the node keeps loaded whenever possible.
type Resident struct {
	Model         string `json:"model"`
	ContextLength int    `json:"context_length,omitempty"`
	// Priority 0..100, higher = yielded last, reloaded first (nil = 50).
	Priority *int `json:"priority,omitempty"`
	// AutoReload nil = true.
	AutoReload *bool `json:"auto_reload,omitempty"`
}

// EffectivePriority returns the priority with its default.
func (r Resident) EffectivePriority() int {
	if r.Priority == nil {
		return DefaultResidentPriority
	}
	return *r.Priority
}

// Reloads reports whether the node may load the model on its own.
func (r Resident) Reloads() bool { return r.AutoReload == nil || *r.AutoReload }

// Resident returns the resident entry for model, if any.
func (c *NodeConfig) Resident(model string) (Resident, bool) {
	for _, r := range c.Residents {
		if r.Model == model {
			return r, true
		}
	}
	return Resident{}, false
}

// PressureConfig mirrors config.pressure; nil fields take the defaults
// (docs/PROTOCOL.md "VRAM pressure and resident models").
type PressureConfig struct {
	Enabled                 *bool              `json:"enabled,omitempty"`
	PollSeconds             *int               `json:"poll_seconds,omitempty"`
	SustainSeconds          *int               `json:"sustain_seconds,omitempty"`
	UnloadBelowFreeMB       *int64             `json:"unload_below_free_mb,omitempty"`
	MarginMB                *int64             `json:"margin_mb,omitempty"`
	ReloadAfterSeconds      *int               `json:"reload_after_seconds,omitempty"`
	ReloadHeadroomMB        *int64             `json:"reload_headroom_mb,omitempty"`
	ReloadMaxForeignGPUUtil *float64           `json:"reload_max_foreign_gpu_util,omitempty"`
	MinOffSeconds           *int               `json:"min_off_seconds,omitempty"`
	ReloadWhileRuleMatches  *bool              `json:"reload_while_rule_matches,omitempty"`
	ForeignSharedTriggerMB  *int64             `json:"foreign_shared_trigger_mb,omitempty"`
	RAM                     *RAMPressureConfig `json:"ram,omitempty"`
}

// RAMPressureConfig mirrors config.pressure.ram.
type RAMPressureConfig struct {
	Enabled           *bool  `json:"enabled,omitempty"`
	UnloadBelowFreeMB *int64 `json:"unload_below_free_mb,omitempty"`
	MarginMB          *int64 `json:"margin_mb,omitempty"`
	ReloadHeadroomMB  *int64 `json:"reload_headroom_mb,omitempty"`
}

// Pressure is the effective pressure configuration (defaults applied).
type Pressure struct {
	// Enabled is GPU VRAM pressure handling.
	Enabled                bool
	Poll                   time.Duration
	Sustain                time.Duration
	UnloadBelowFreeMB      int64
	MarginMB               int64
	ReloadAfter            time.Duration
	ReloadHeadroomMB       int64
	ReloadMaxForeignUtil   float64
	MinOff                 time.Duration
	ReloadWhileRuleMatches bool
	// ForeignSharedTriggerMB: a single other process with at least this much
	// shared GPU memory is demand the GPU could not meet (0 = signal off).
	ForeignSharedTriggerMB int64
	RAMEnabled             bool
	RAMUnloadBelowFreeMB   int64
	RAMMarginMB            int64
	RAMReloadHeadroomMB    int64
}

// Pressure defaults (docs/PROTOCOL.md).
const (
	DefaultPressurePollSeconds        = 2
	DefaultPressureSustainSeconds     = 3
	DefaultPressureUnloadBelowFreeMB  = 1024
	DefaultPressureMarginMB           = 1024
	DefaultPressureReloadAfterSeconds = 60
	DefaultPressureReloadHeadroomMB   = 2048
	DefaultPressureReloadMaxUtil      = 30
	DefaultPressureMinOffSeconds      = 120
	DefaultPressureForeignSharedMB    = 1024
	DefaultRAMUnloadBelowFreeMB       = 2048
	DefaultRAMMarginMB                = 1024
	DefaultRAMReloadHeadroomMB        = 1024
)

func orInt(p *int, d int) int {
	if p == nil {
		return d
	}
	return *p
}

func orInt64(p *int64, d int64) int64 {
	if p == nil {
		return d
	}
	return *p
}

func orBool(p *bool, d bool) bool {
	if p == nil {
		return d
	}
	return *p
}

// EffectivePressure applies the defaults for a node with a dedicated GPU (RAM handling is
// off unless the config turns it on).
func (c *NodeConfig) EffectivePressure() Pressure { return c.EffectivePressureFor(true) }

// EffectivePressureFor applies the defaults. Without a pressure object GPU and RAM
// handling are off; residents are still kept loaded. RAM handling that the config leaves
// open (ram.enabled absent) is on for a node without a dedicated GPU — there the models
// live in system memory (CPU backend, iGPU), and the user's own programs need it (E2-05, D27).
func (c *NodeConfig) EffectivePressureFor(dedicatedGPU bool) Pressure {
	p := c.Pressure
	present := p != nil
	if p == nil {
		p = &PressureConfig{}
	}
	util := float64(DefaultPressureReloadMaxUtil)
	if p.ReloadMaxForeignGPUUtil != nil {
		util = *p.ReloadMaxForeignGPUUtil
	}
	ram := p.RAM
	if ram == nil {
		ram = &RAMPressureConfig{}
	}
	return Pressure{
		Enabled:                present && orBool(p.Enabled, true),
		Poll:                   time.Duration(orInt(p.PollSeconds, DefaultPressurePollSeconds)) * time.Second,
		Sustain:                time.Duration(orInt(p.SustainSeconds, DefaultPressureSustainSeconds)) * time.Second,
		UnloadBelowFreeMB:      orInt64(p.UnloadBelowFreeMB, DefaultPressureUnloadBelowFreeMB),
		MarginMB:               orInt64(p.MarginMB, DefaultPressureMarginMB),
		ReloadAfter:            time.Duration(orInt(p.ReloadAfterSeconds, DefaultPressureReloadAfterSeconds)) * time.Second,
		ReloadHeadroomMB:       orInt64(p.ReloadHeadroomMB, DefaultPressureReloadHeadroomMB),
		ReloadMaxForeignUtil:   util,
		MinOff:                 time.Duration(orInt(p.MinOffSeconds, DefaultPressureMinOffSeconds)) * time.Second,
		ReloadWhileRuleMatches: orBool(p.ReloadWhileRuleMatches, false),
		ForeignSharedTriggerMB: orInt64(p.ForeignSharedTriggerMB, DefaultPressureForeignSharedMB),
		RAMEnabled:             present && orBool(ram.Enabled, !dedicatedGPU),
		RAMUnloadBelowFreeMB:   orInt64(ram.UnloadBelowFreeMB, DefaultRAMUnloadBelowFreeMB),
		RAMMarginMB:            orInt64(ram.MarginMB, DefaultRAMMarginMB),
		RAMReloadHeadroomMB:    orInt64(ram.ReloadHeadroomMB, DefaultRAMReloadHeadroomMB),
	}
}

// YieldConfig mirrors config.yield (docs/PROTOCOL.md "Yielding to local use"); nil fields take
// the defaults below.
type YieldConfig struct {
	// Enabled turns freezing on activity, CPU load and battery on or off. Absent: on for a node
	// without a dedicated GPU (the laptop that must stay usable), off with one.
	Enabled *bool `json:"enabled,omitempty"`
	// LowPriority runs llama-server and llama-bench at the lowest process priority (default on,
	// independent of Enabled).
	LowPriority *bool `json:"low_priority,omitempty"`
	PollSeconds *int  `json:"poll_seconds,omitempty"`
	// IdleSeconds: keyboard/mouse input younger than this counts as "the user is here".
	IdleSeconds *int `json:"idle_seconds,omitempty"`
	// CPUPercent: other programs using more than this share of the CPU for SustainSeconds
	// count as load; 0 switches the signal off.
	CPUPercent     *float64 `json:"cpu_percent,omitempty"`
	SustainSeconds *int     `json:"sustain_seconds,omitempty"`
	// ResumeSeconds: a CPU-load pause ends after the load stayed below the threshold this long.
	ResumeSeconds  *int  `json:"resume_seconds,omitempty"`
	PauseOnBattery *bool `json:"pause_on_battery,omitempty"`
	// UnloadAfterPauseSeconds: a pause that lasts this long unloads the models (0 = never).
	UnloadAfterPauseSeconds *int `json:"unload_after_pause_seconds,omitempty"`
}

// Yield is the effective yield configuration (defaults applied).
type Yield struct {
	Enabled          bool
	LowPriority      bool
	Poll             time.Duration
	Idle             time.Duration
	CPUPercent       float64
	Sustain          time.Duration
	Resume           time.Duration
	PauseOnBattery   bool
	UnloadAfterPause time.Duration
}

// Yield defaults (docs/PROTOCOL.md).
const (
	DefaultYieldPollSeconds             = 5
	DefaultYieldIdleSeconds             = 60
	DefaultYieldCPUPercent              = 60
	DefaultYieldSustainSeconds          = 5
	DefaultYieldResumeSeconds           = 30
	DefaultYieldUnloadAfterPauseSeconds = 900
)

// EffectiveYieldFor applies the defaults; dedicatedGPU decides the default of Enabled.
func (c *NodeConfig) EffectiveYieldFor(dedicatedGPU bool) Yield {
	y := c.Yield
	if y == nil {
		y = &YieldConfig{}
	}
	cpu := float64(DefaultYieldCPUPercent)
	if y.CPUPercent != nil {
		cpu = *y.CPUPercent
	}
	return Yield{
		Enabled:          orBool(y.Enabled, !dedicatedGPU),
		LowPriority:      orBool(y.LowPriority, true),
		Poll:             time.Duration(orInt(y.PollSeconds, DefaultYieldPollSeconds)) * time.Second,
		Idle:             time.Duration(orInt(y.IdleSeconds, DefaultYieldIdleSeconds)) * time.Second,
		CPUPercent:       cpu,
		Sustain:          time.Duration(orInt(y.SustainSeconds, DefaultYieldSustainSeconds)) * time.Second,
		Resume:           time.Duration(orInt(y.ResumeSeconds, DefaultYieldResumeSeconds)) * time.Second,
		PauseOnBattery:   orBool(y.PauseOnBattery, true),
		UnloadAfterPause: time.Duration(orInt(y.UnloadAfterPauseSeconds, DefaultYieldUnloadAfterPauseSeconds)) * time.Second,
	}
}

// Energy profiles (config.power.profile).
const (
	PowerPerformance = "performance"
	PowerBalanced    = "balanced"
	PowerSaver       = "saver"
)

// PowerConfig mirrors config.power: what the node itself costs while it has nothing to do.
type PowerConfig struct {
	// Profile: performance (nothing is slowed down), balanced (default) or saver.
	Profile string `json:"profile,omitempty"`
	// SleepAfterIdleSeconds: the node puts its machine to sleep after this long without any work
	// and without a user (0 = never, the default). Needs Wake-on-LAN to come back.
	SleepAfterIdleSeconds *int `json:"sleep_after_idle_seconds,omitempty"`
}

// Power is the effective energy configuration.
type Power struct {
	Profile string
	// IdleBackoff multiplies the polling intervals (pressure, yield) while the node has no
	// model loaded and no reason to look at the hardware.
	IdleBackoff int
	// HeartbeatFactor multiplies the heartbeat interval while the node is idle.
	HeartbeatFactor int
	SleepAfterIdle  time.Duration
}

// EffectivePower applies the profile.
func (c *NodeConfig) EffectivePower() Power {
	p := Power{Profile: PowerBalanced, IdleBackoff: 3, HeartbeatFactor: 1}
	if c.Power == nil {
		return p
	}
	switch c.Power.Profile {
	case PowerPerformance:
		p.Profile, p.IdleBackoff = PowerPerformance, 1
	case PowerSaver:
		p.Profile, p.IdleBackoff, p.HeartbeatFactor = PowerSaver, 6, 3
	}
	if c.Power.SleepAfterIdleSeconds != nil {
		p.SleepAfterIdle = time.Duration(*c.Power.SleepAfterIdleSeconds) * time.Second
	}
	return p
}

// ParseNodeConfig decodes and minimally validates a config object. Unknown
// fields are ignored (forward compatibility); the essentials are checked.
func ParseNodeConfig(raw []byte) (NodeConfig, error) {
	var c NodeConfig
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := dec.Decode(&c); err != nil {
		return NodeConfig{}, fmt.Errorf("decode config: %w", err)
	}
	if err := c.Validate(); err != nil {
		return NodeConfig{}, err
	}
	return c, nil
}

// Validate checks the fields the node relies on.
func (c *NodeConfig) Validate() error {
	switch c.Runner.Type {
	case RunnerLlamaServer, RunnerFake, RunnerNone:
	default:
		return fmt.Errorf("config: unknown runner.type %q", c.Runner.Type)
	}
	switch c.Runner.Backend {
	case "":
		c.Runner.Backend = BackendAuto
	case BackendAuto, BackendCUDA, BackendVulkan, BackendCPU:
	default:
		return fmt.Errorf("config: unknown runner.backend %q", c.Runner.Backend)
	}
	if c.Update != nil && c.Update.Channel != "" && !updateChannelRe.MatchString(c.Update.Channel) {
		return fmt.Errorf("config: update.channel %q must be lower-case letters, digits and '-'", c.Update.Channel)
	}
	if c.Tools != nil {
		if c.Tools.TimeoutSeconds != nil && (*c.Tools.TimeoutSeconds < 1 || *c.Tools.TimeoutSeconds > 300) {
			return fmt.Errorf("config: tools.timeout_seconds must be 1..300")
		}
		if f := c.Tools.ConnectorsFile; f != "" && !absolutePath(f) {
			return fmt.Errorf("config: tools.connectors_file must be an absolute path")
		}
		if u := c.Tools.SearxngURL; u != "" {
			pu, err := url.Parse(u)
			if err != nil || (pu.Scheme != "http" && pu.Scheme != "https") || pu.Host == "" || pu.User != nil {
				return fmt.Errorf("config: tools.searxng_url must be an http(s) URL without a user name or password")
			}
		}
		if err := c.Tools.validateTargets(); err != nil {
			return err
		}
		if err := c.Tools.Workshop.validate(); err != nil {
			return err
		}
		if len(c.Tools.PrivateNames) > 100 {
			return fmt.Errorf("config: tools.private_names has at most 100 entries")
		}
		for _, n := range c.Tools.PrivateNames {
			if strings.TrimSpace(n) == "" || len(n) > 100 {
				return fmt.Errorf("config: tools.private_names entries must hold 1 to 100 characters")
			}
		}
		for _, r := range c.Tools.ComposeRoots {
			if !strings.HasPrefix(r, "/") {
				return fmt.Errorf("config: tools.compose_roots must be absolute paths")
			}
		}
		for _, r := range c.Tools.DocsRoots {
			if !absolutePath(r) {
				return fmt.Errorf("config: tools.docs_roots must be absolute paths")
			}
		}
		if len(c.Tools.ReadRoots) > 50 {
			return fmt.Errorf("config: tools.read_roots has at most 50 entries")
		}
		for _, r := range c.Tools.ReadRoots {
			if err := ValidReadRoot(r); err != nil {
				return fmt.Errorf("config: tools.read_roots: %w", err)
			}
		}
		for _, g := range c.Tools.DocsExclude {
			if strings.TrimSpace(g) == "" {
				return fmt.Errorf("config: tools.docs_exclude must not hold an empty glob")
			}
			if _, err := path.Match(g, "x"); err != nil {
				return fmt.Errorf("config: tools.docs_exclude glob %q is invalid: %w", g, err)
			}
		}
	}
	if c.Runner.Threads < 0 {
		return fmt.Errorf("config: runner.threads must be >= 1")
	}
	if n := c.Runner.MaxContextLength; n != 0 && (n < MinContextLength || n > MaxContextLength) {
		return fmt.Errorf("config: runner.max_context_length must be %d..%d", MinContextLength, MaxContextLength)
	}
	if n := c.Runner.DefaultContextLength; n != 0 && (n < MinContextLength || n > MaxContextLength) {
		return fmt.Errorf("config: runner.default_context_length must be %d..%d", MinContextLength, MaxContextLength)
	}
	for m, n := range c.Runner.ModelContextLengths {
		if strings.TrimSpace(m) == "" || n < MinContextLength || n > MaxContextLength {
			return fmt.Errorf("config: runner.model_context_lengths[%q] must be %d..%d", m, MinContextLength, MaxContextLength)
		}
	}
	for m := range c.Runner.ModelJinja {
		if strings.TrimSpace(m) == "" {
			return fmt.Errorf("config: runner.model_jinja needs model names")
		}
	}
	if n := c.Runner.NParallel; n < 0 || n > MaxParallel {
		return fmt.Errorf("config: runner.n_parallel must be 1..%d", MaxParallel)
	}
	for m, n := range c.Runner.ModelNParallel {
		if strings.TrimSpace(m) == "" || n < 1 || n > MaxParallel {
			return fmt.Errorf("config: runner.model_n_parallel[%q] must be 1..%d", m, MaxParallel)
		}
	}
	for m, mb := range c.Runner.ModelBackends {
		if strings.TrimSpace(m) == "" {
			return fmt.Errorf("config: runner.model_backends has an empty model name")
		}
		switch mb.Backend {
		case "", BackendCUDA, BackendVulkan, BackendCPU:
		default:
			return fmt.Errorf("config: runner.model_backends[%q].backend %q must be cuda, vulkan or cpu", m, mb.Backend)
		}
		if mb.GPULayers != nil && (*mb.GPULayers < 0 || *mb.GPULayers > 1000) {
			return fmt.Errorf("config: runner.model_backends[%q].gpu_layers must be 0..1000", m)
		}
	}
	if g := c.Runner.MemoryGuard; g != nil {
		switch g.Mode {
		case "", GuardRefuse, GuardWarn, GuardOff:
		default:
			return fmt.Errorf("config: runner.memory_guard.mode %q must be refuse, warn or off", g.Mode)
		}
		if g.ReserveMB != nil && (*g.ReserveMB < 0 || *g.ReserveMB > 1<<20) {
			return fmt.Errorf("config: runner.memory_guard.reserve_mb must be 0..1048576")
		}
	}
	if c.Watcher.CooldownSeconds != nil && *c.Watcher.CooldownSeconds < 0 {
		return fmt.Errorf("config: watcher.cooldown_seconds must be >= 0")
	}
	if c.HeartbeatIntervalSeconds == 0 {
		c.HeartbeatIntervalSeconds = DefaultHeartbeatIntervalSeconds
	}
	if c.HeartbeatIntervalSeconds < 1 || c.HeartbeatIntervalSeconds > 300 {
		return fmt.Errorf("config: heartbeat_interval_seconds %d out of range 1..300", c.HeartbeatIntervalSeconds)
	}
	if c.IdleTTLSeconds < 0 {
		return fmt.Errorf("config: idle_ttl_seconds must be >= 0")
	}
	if c.Watcher.GraceSeconds < 0 {
		return fmt.Errorf("config: watcher.grace_seconds must be >= 0")
	}
	for i, r := range c.Watcher.Rules {
		if strings.TrimSpace(r.Process) == "" && strings.TrimSpace(r.PathPrefix) == "" {
			return fmt.Errorf("config: watcher.rules[%d] needs process or path_prefix", i)
		}
	}
	if err := c.validateYieldAndPower(); err != nil {
		return err
	}
	if err := c.validateTransfer(); err != nil {
		return err
	}
	return c.validatePressure()
}

func (c *NodeConfig) validateYieldAndPower() error {
	if y := c.Yield; y != nil {
		ints := []struct {
			name   string
			v      *int
			lo, hi int
		}{
			{"poll_seconds", y.PollSeconds, 1, 300},
			{"idle_seconds", y.IdleSeconds, 1, 86400},
			{"sustain_seconds", y.SustainSeconds, 0, 3600},
			{"resume_seconds", y.ResumeSeconds, 0, 3600},
			{"unload_after_pause_seconds", y.UnloadAfterPauseSeconds, 0, 7 * 86400},
		}
		for _, f := range ints {
			if f.v != nil && (*f.v < f.lo || *f.v > f.hi) {
				return fmt.Errorf("config: yield.%s must be %d..%d", f.name, f.lo, f.hi)
			}
		}
		if y.CPUPercent != nil && (*y.CPUPercent < 0 || *y.CPUPercent > 100) {
			return fmt.Errorf("config: yield.cpu_percent must be 0..100")
		}
	}
	if p := c.Power; p != nil {
		switch p.Profile {
		case "", PowerPerformance, PowerBalanced, PowerSaver:
		default:
			return fmt.Errorf("config: power.profile %q must be performance, balanced or saver", p.Profile)
		}
		if p.SleepAfterIdleSeconds != nil && (*p.SleepAfterIdleSeconds < 0 || *p.SleepAfterIdleSeconds > 30*86400) {
			return fmt.Errorf("config: power.sleep_after_idle_seconds must be 0..2592000")
		}
	}
	return nil
}

func (c *NodeConfig) validatePressure() error {
	seen := map[string]bool{}
	for i, r := range c.Residents {
		if strings.TrimSpace(r.Model) == "" {
			return fmt.Errorf("config: residents[%d].model is empty", i)
		}
		if seen[r.Model] {
			return fmt.Errorf("config: residents[%d]: model %q listed twice", i, r.Model)
		}
		seen[r.Model] = true
		if r.Priority != nil && (*r.Priority < 0 || *r.Priority > 100) {
			return fmt.Errorf("config: residents[%d].priority must be 0..100", i)
		}
		if r.ContextLength < 0 {
			return fmt.Errorf("config: residents[%d].context_length must be >= 1", i)
		}
	}
	p := c.Pressure
	if p == nil {
		return nil
	}
	ints := []struct {
		name   string
		v      *int
		lo, hi int
	}{
		{"poll_seconds", p.PollSeconds, 1, 60},
		{"sustain_seconds", p.SustainSeconds, 0, 600},
		{"reload_after_seconds", p.ReloadAfterSeconds, 0, 86400},
		{"min_off_seconds", p.MinOffSeconds, 0, 86400},
	}
	for _, f := range ints {
		if f.v != nil && (*f.v < f.lo || *f.v > f.hi) {
			return fmt.Errorf("config: pressure.%s must be %d..%d", f.name, f.lo, f.hi)
		}
	}
	mbs := map[string]*int64{
		"unload_below_free_mb": p.UnloadBelowFreeMB, "margin_mb": p.MarginMB, "reload_headroom_mb": p.ReloadHeadroomMB,
		"foreign_shared_trigger_mb": p.ForeignSharedTriggerMB,
	}
	if p.RAM != nil {
		mbs["ram.unload_below_free_mb"] = p.RAM.UnloadBelowFreeMB
		mbs["ram.margin_mb"] = p.RAM.MarginMB
		mbs["ram.reload_headroom_mb"] = p.RAM.ReloadHeadroomMB
	}
	for name, v := range mbs {
		if v != nil && (*v < 0 || *v > 1<<20) {
			return fmt.Errorf("config: pressure.%s must be 0..1048576", name)
		}
	}
	if u := p.ReloadMaxForeignGPUUtil; u != nil && (*u < 0 || *u > 100) {
		return fmt.Errorf("config: pressure.reload_max_foreign_gpu_util must be 0..100")
	}
	return nil
}

// ActionAllowed reports whether name is in allowed_actions.
func (c *NodeConfig) ActionAllowed(name string) bool {
	for _, a := range c.AllowedActions {
		if a == name {
			return true
		}
	}
	return false
}

var (
	targetNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,63}$`)
	targetHostRe = regexp.MustCompile(`^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$`)
)

// maxTargets bounds each list of targets.
const maxTargets = 50

func okHost(h string) bool {
	return net.ParseIP(h) != nil || (len(h) <= 253 && targetHostRe.MatchString(h))
}

func (t *ToolsConfig) validateTargets() error {
	if len(t.NetTargets) > maxTargets || len(t.DNSNames) > maxTargets || len(t.CertTargets) > maxTargets {
		return fmt.Errorf("config: tools: at most %d net_targets, dns_names and cert_targets each", maxTargets)
	}
	names := map[string]bool{}
	for _, n := range t.NetTargets {
		if !targetNameRe.MatchString(n.Name) || names[n.Name] {
			return fmt.Errorf("config: tools.net_targets: names must be unique and use letters, digits, space, . _ -")
		}
		names[n.Name] = true
		if !okHost(n.Host) {
			return fmt.Errorf("config: tools.net_targets[%s].host must be a host name or IP address", n.Name)
		}
		if len(n.Ports) > 20 {
			return fmt.Errorf("config: tools.net_targets[%s]: at most 20 ports", n.Name)
		}
		for _, p := range n.Ports {
			if p < 1 || p > 65535 {
				return fmt.Errorf("config: tools.net_targets[%s].ports must be 1..65535", n.Name)
			}
		}
	}
	for _, d := range t.DNSNames {
		if !okHost(d) {
			return fmt.Errorf("config: tools.dns_names must be host names")
		}
	}
	for _, c := range t.CertTargets {
		h, p := c, "443"
		if hh, pp, err := net.SplitHostPort(c); err == nil {
			h, p = hh, pp
		}
		if n, err := strconv.Atoi(p); err != nil || n < 1 || n > 65535 || !okHost(h) {
			return fmt.Errorf("config: tools.cert_targets must be host or host:port")
		}
	}
	return nil
}

func (w *WorkshopConfig) validate() error {
	if w == nil {
		return nil
	}
	if w.Dir != "" && !absolutePath(w.Dir) {
		return fmt.Errorf("config: tools.workshop.dir must be an absolute path")
	}
	if len(w.AllowedHosts) > 20 {
		return fmt.Errorf("config: tools.workshop.allowed_hosts has at most 20 entries")
	}
	for _, h := range w.AllowedHosts {
		if !workshopHostRe.MatchString(h) {
			return fmt.Errorf("config: tools.workshop.allowed_hosts entry %q is not a host name", h)
		}
	}
	if w.MaxRepoMB < 0 || w.MaxRepoMB > 20000 {
		return fmt.Errorf("config: tools.workshop.max_repo_mb must be 0..20000")
	}
	r := w.Runner
	if r == nil {
		return nil
	}
	switch r.Type {
	case WorkshopRunnerNone, WorkshopRunnerDocker:
	default:
		return fmt.Errorf("config: tools.workshop.runner.type must be none or docker")
	}
	if r.HostDir != "" && !absolutePath(r.HostDir) {
		return fmt.Errorf("config: tools.workshop.runner.host_dir must be an absolute path")
	}
	for tpl, img := range r.Images {
		if !workshopKey(tpl) {
			return fmt.Errorf("config: tools.workshop.runner.images: %q is not a test template", tpl)
		}
		if !workshopImageRe.MatchString(img) {
			return fmt.Errorf("config: tools.workshop.runner.images[%s] is not an image reference", tpl)
		}
	}
	for _, tpl := range r.NetworkTemplates {
		if !workshopKey(tpl) {
			return fmt.Errorf("config: tools.workshop.runner.network_templates: %q is not a test template", tpl)
		}
	}
	if r.Network != "" && !workshopNetworkRe.MatchString(r.Network) {
		return fmt.Errorf("config: tools.workshop.runner.network is not a Docker network name")
	}
	if r.MemoryMB < 0 || r.MemoryMB > 65536 || r.CPUMilli < 0 || r.CPUMilli > 64000 ||
		r.TimeoutSeconds < 0 || r.TimeoutSeconds > 3600 {
		return fmt.Errorf("config: tools.workshop.runner limits are out of range")
	}
	return nil
}

var (
	workshopHostRe    = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$`)
	workshopImageRe   = regexp.MustCompile(`^[a-z0-9][a-z0-9._/:@-]{0,255}$`)
	workshopNetworkRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)
)
