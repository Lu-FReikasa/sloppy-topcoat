// Package protocol defines the node <-> hub wire messages (proto/v1) as Go types.
//
// The JSON Schemas in proto/v1 are the source of truth; tests validate every
// message this package produces against them.
package protocol

import (
	"encoding/json"
	"fmt"
	"time"

	"topcoat/node/internal/version"
)

// Message types.
const (
	TypeHello        = "hello"
	TypeConfig       = "config"
	TypeInventory    = "inventory"
	TypeHeartbeat    = "heartbeat"
	TypeAction       = "action"
	TypeActionResult = "action_result"
	TypeEvent        = "event"
	TypeQueuedEvents = "queued_events"
	TypeError        = "error"
	// Model gateway (E1-05).
	TypeProxyRequest = "proxy_request"
	TypeProxyChunk   = "proxy_chunk"
	TypeProxyEnd     = "proxy_end"
	TypeProxyStatus  = "proxy_status"
	TypeProxyCancel  = "proxy_cancel"
)

// Node states reported in heartbeats.
const (
	StateAvailable  = "available"
	StateBusyGaming = "busy_gaming"
	StateDegraded   = "degraded"
	// StateYielded: models were unloaded because other programs need the memory (E1-06).
	StateYielded = "yielded"
	// StatePaused: the node froze its models for the person using the machine (E2-05 B, D24) and
	// takes no new work until they are continued.
	StatePaused = "paused"
)

// Pause reasons (event paused, proxy_status).
const (
	PauseInput   = "input"
	PauseCPULoad = "cpu_load"
	PauseBattery = "battery"
)

// Proxy states (proxy_status).
const (
	ProxyPaused  = "paused"
	ProxyRunning = "running"
)

// Event kinds.
const (
	EventModelLoaded     = "model_loaded"
	EventModelUnloaded   = "model_unloaded"
	EventModelLoadFailed = "model_load_failed"
	EventModelLoadRetry  = "model_load_retry"
	// EventModelLoadCancelled: a load in progress was stopped on purpose (unload, unload all,
	// a long pause); its server process is killed.
	EventModelLoadCancelled = "model_load_cancelled" //nolint:misspell // wire name fixed by the protocol
	EventGameDetected       = "game_detected"
	EventGameEnded          = "game_ended"
	EventDownloadProgress   = "download_progress"
	EventStateChanged       = "state_changed"
	EventBenchmarkStarted   = "benchmark_started"
	EventBenchmarkResult    = "benchmark_result"
	EventBenchmarkError     = "benchmark_error"
	EventBenchmarkDone      = "benchmark_finished"
	EventRuntimeInstalled   = "runtime_installed"
	EventAutoReloadPaused   = "auto_reload_paused"
	EventPaused             = "paused"
	EventResumed            = "resumed"
	EventSleeping           = "sleeping"
	// Update channel (E2-06, D50).
	EventUpdateStaging = "update_staging"
	EventUpdateStaged  = "update_staged"
	EventUpdateApplied = "update_applied"
	EventUpdateFailed  = "update_failed"
	// Direct model transfer between nodes (E2-07, D51).
	EventTransferReady    = "transfer_ready"
	EventTransferProgress = "transfer_progress"
	EventError            = "error"
)

// Error codes (common.schema.json#/$defs/error_code).
const (
	ErrNotAllowed  = "not_allowed"
	ErrInvalidArgs = "invalid_args"
	// ErrPrivateData: a web.search / web.fetch input held private data and was not sent (E3-04).
	ErrPrivateData    = "private_data"
	ErrNotImplemented = "not_implemented"
	ErrRunnerError    = "runner_error"
	ErrBusy           = "busy"
	ErrTimeout        = "timeout"
	ErrCanceled       = "canceled"
	// ErrInterrupted ends a proxied answer whose model the node unloaded (or lost) meanwhile:
	// what was said stays valid and the chat can continue it (E2-05 B).
	ErrInterrupted     = "interrupted"
	ErrNodeOffline     = "node_offline"
	ErrUpgradeRequired = "upgrade_required"
	ErrInvalidMessage  = "invalid_message"
	ErrInternal        = "internal"
	ErrNotLoaded       = "not_loaded"
	// ErrCapabilityFailed is a capability that ran and failed (Docker not reachable, unit unknown).
	ErrCapabilityFailed = "capability_failed"
	// ErrInsufficientMemory: a load would not fit into the free RAM (D27).
	ErrInsufficientMemory = "insufficient_memory"
	// ErrInsufficientDisk: a download would not fit onto the model drive (D27).
	ErrInsufficientDisk = "insufficient_disk"
)

// Envelope is the frame around every message.
type Envelope struct {
	V    int             `json:"v"`
	Type string          `json:"type"`
	ID   string          `json:"id"`
	TS   string          `json:"ts"`
	Re   string          `json:"re,omitempty"`
	Body json.RawMessage `json:"body"`
}

// New builds an envelope with a fresh UUIDv4 id and the given timestamp.
func New(msgType string, body any, now time.Time) (Envelope, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return Envelope{}, fmt.Errorf("marshal %s body: %w", msgType, err)
	}
	return Envelope{
		V:    version.ProtocolVersion,
		Type: msgType,
		ID:   NewUUID(),
		TS:   FormatTS(now),
		Body: raw,
	}, nil
}

// tsLayout is RFC 3339 in UTC with millisecond precision.
const tsLayout = "2006-01-02T15:04:05.000Z07:00"

// FormatTS formats a timestamp as RFC 3339 in UTC (millisecond precision).
func FormatTS(t time.Time) string {
	return t.UTC().Format(tsLayout)
}

// DecodeBody unmarshals the envelope body into v.
func (e Envelope) DecodeBody(v any) error {
	if err := json.Unmarshal(e.Body, v); err != nil {
		return fmt.Errorf("decode %s body: %w", e.Type, err)
	}
	return nil
}

// Hello is sent by the node right after connecting.
type Hello struct {
	NodeID          string  `json:"node_id"`
	AgentVersion    string  `json:"agent_version"`
	ProtocolVersion int     `json:"protocol_version"`
	ConfigHash      *string `json:"config_hash"`
}

// ConfigBody is sent by the hub after hello.
type ConfigBody struct {
	ConfigHash string          `json:"config_hash"`
	Config     json.RawMessage `json:"config"`
}

// CPU describes the node CPU.
type CPU struct {
	Model            string `json:"model"`
	Cores            int    `json:"cores"`
	Threads          int    `json:"threads,omitempty"`
	PerformanceCores int    `json:"performance_cores,omitempty"`
	EfficiencyCores  int    `json:"efficiency_cores,omitempty"`
}

// GPU describes one GPU in the inventory.
type GPU struct {
	Index       int    `json:"index"`
	Name        string `json:"name"`
	Vendor      string `json:"vendor"`
	VRAMTotalMB int64  `json:"vram_total_mb"`
	Integrated  bool   `json:"integrated,omitempty"`
	SharedTotal int64  `json:"shared_total_mb,omitempty"`
}

// GPUUsage is the per-GPU part of a heartbeat.
type GPUUsage struct {
	Index        int   `json:"index"`
	VRAMUsedMB   int64 `json:"vram_used_mb"`
	SharedUsedMB int64 `json:"shared_used_mb,omitempty"`
	// GPUUtil: utilization of the whole GPU in percent (all programs), when known.
	GPUUtil *float64 `json:"gpu_util,omitempty"`
	// ForeignVRAMUsedMB / ForeignGPUUtil: other programs, when known (E1-06).
	ForeignVRAMUsedMB *int64   `json:"foreign_vram_used_mb,omitempty"`
	ForeignGPUUtil    *float64 `json:"foreign_gpu_util,omitempty"`
	// ForeignSharedMaxMB: the largest shared GPU memory use of one other
	// program (an application that did not fit into VRAM), when known.
	ForeignSharedMaxMB *int64 `json:"foreign_shared_max_mb,omitempty"`
}

// Model is a model available on the node.
type Model struct {
	Name      string `json:"name"`
	SizeBytes int64  `json:"size_bytes,omitempty"`
	Format    string `json:"format,omitempty"`
	// Own: true = the file is in the node's own <data dir>/models (the hub may move or delete it),
	// false = in a configured model_dirs entry (read-only for the hub); nil = not reported.
	Own *bool `json:"own,omitempty"`
	// GGUF: what the file states about itself in its header (R8); nil = not read or not a GGUF file.
	GGUF *ModelGGUF `json:"gguf,omitempty"`
	// Projector is the file name of the vision projector loaded with the model (--mmproj, D125);
	// empty = none, the model cannot see.
	Projector string `json:"projector,omitempty"`
}

// LoadedModel is a model currently loaded by the runner.
type LoadedModel struct {
	Name       string `json:"name"`
	VRAMUsedMB int64  `json:"vram_used_mb,omitempty"`
	// ContextLength is the context of ONE slot: what a single request may use.
	ContextLength int `json:"context_length,omitempty"`
	// NParallel is the number of slots the server runs (absent = 1).
	NParallel int    `json:"n_parallel,omitempty"`
	PID       int    `json:"pid,omitempty"`
	Port      int    `json:"port,omitempty"`
	Backend   string `json:"backend,omitempty"`
	RAMUsedMB int64  `json:"ram_used_mb,omitempty"`
}

// Hardware is the hardware part of the inventory.
type Hardware struct {
	CPU        CPU   `json:"cpu"`
	RAMTotalMB int64 `json:"ram_total_mb"`
	GPUs       []GPU `json:"gpus"`
	// DiskTotalMB / DiskFreeMB are the space of the drive that holds the model directory
	// (nil = not measured).
	DiskTotalMB *int64 `json:"disk_total_mb,omitempty"`
	DiskFreeMB  *int64 `json:"disk_free_mb,omitempty"`
	// Network lists the physical interfaces (wired first), for Wake-on-LAN (E2-05 E).
	Network []NetIface `json:"network,omitempty"`
	// Addresses are the unicast IPs of this node (no loopback, no link-local): the candidates
	// for a direct model transfer (E2-07).
	Addresses []string `json:"addresses,omitempty"`
}

// NetIface is one physical network interface.
type NetIface struct {
	Name string `json:"name"`
	MAC  string `json:"mac"`
	Kind string `json:"kind"`
}

// RunnerInfo is the runner part of the inventory.
type RunnerInfo struct {
	Type      string `json:"type"`
	Available bool   `json:"available"`
	Detail    string `json:"detail,omitempty"`
	Backend   string `json:"backend,omitempty"`
	Release   string `json:"llamacpp_release,omitempty"`
	Threads   int    `json:"threads,omitempty"`
}

// Inventory is sent after config and whenever the model inventory is refreshed.
type Inventory struct {
	Hardware Hardware      `json:"hardware"`
	Runner   RunnerInfo    `json:"runner"`
	Models   []Model       `json:"models"`
	Loaded   []LoadedModel `json:"loaded"`
	// Capabilities are the capabilities this node build offers (E2-04); which of them are
	// switched on is config.capabilities.
	Capabilities []CapabilityInfo `json:"capabilities,omitempty"`
	// Update is the state of the update channel on this node (E2-06, D50).
	Update *UpdateInfo `json:"update,omitempty"`
}

// Updater states in Inventory.Update.
const (
	UpdaterOK      = "ok"
	UpdaterStale   = "stale"
	UpdaterMissing = "missing"
)

// UpdateInfo describes what the node can do with the update channel.
type UpdateInfo struct {
	// Capable is true when the build carries at least one update key.
	Capable bool `json:"capable"`
	// Updater is ok, stale or missing: the heartbeat of the privileged updater service.
	Updater string `json:"updater"`
	// UpdaterVersion is the version the running updater reports in its heartbeat (0.2.6).
	UpdaterVersion string   `json:"updater_version,omitempty"`
	Channel        string   `json:"channel,omitempty"`
	KeyIDs         []string `json:"key_ids,omitempty"`
}

// CapabilityInfo describes one capability of the node build.
type CapabilityInfo struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	ReadOnly    bool   `json:"read_only"`
	// Scratch marks a capability that changes only the scratch area of a dev node (D52): it runs
	// without a confirmation although it is not read-only. Never true for anything else.
	Scratch bool `json:"scratch,omitempty"`
	// Manage marks a capability the person runs from the hub's page (workshop.manage, D76): the
	// hub never offers it to an assistant.
	Manage      bool            `json:"manage,omitempty"`
	InputSchema json.RawMessage `json:"input_schema"`
}

// Heartbeat is sent periodically.
type Heartbeat struct {
	State     string `json:"state"`
	RAMUsedMB int64  `json:"ram_used_mb"`
	// CPUUtil is the utilization of all logical processors together in percent since the previous
	// heartbeat; absent where it cannot be measured (and on the first beat, which has no delta).
	CPUUtil *float64      `json:"cpu_util,omitempty"`
	GPUs    []GPUUsage    `json:"gpus"`
	Loaded  []LoadedModel `json:"loaded"`
	// Loading lists models being loaded right now (E1-05).
	Loading []string `json:"loading,omitempty"`
}

// ProxyRequest forwards one HTTP request to a loaded model (hub -> node, E1-05).
// Body is the raw request body (base64 in JSON).
type ProxyRequest struct {
	StreamID string            `json:"stream_id"`
	Model    string            `json:"model"`
	Method   string            `json:"method"`
	Path     string            `json:"path"`
	Headers  map[string]string `json:"headers"`
	Body     []byte            `json:"body"`
}

// ProxyChunk is part of a proxied response (node -> hub); the first carries
// Status and ContentType.
type ProxyChunk struct {
	StreamID    string `json:"stream_id"`
	Seq         int    `json:"seq"`
	Data        []byte `json:"data"`
	Status      int    `json:"status,omitempty"`
	ContentType string `json:"content_type,omitempty"`
}

// ProxyEnd ends a proxied response (node -> hub).
type ProxyEnd struct {
	StreamID string `json:"stream_id"`
	Status   int    `json:"status"`
	Error    *Error `json:"error,omitempty"`
}

// ProxyStatus tells the hub that a request in flight stands still (paused) or goes on
// (running) because the node froze its model for local use (node -> hub).
type ProxyStatus struct {
	StreamID string   `json:"stream_id"`
	State    string   `json:"state"`
	Reasons  []string `json:"reasons,omitempty"`
}

// ProxyCancel stops a proxied stream (hub -> node).
type ProxyCancel struct {
	StreamID string `json:"stream_id"`
}

// Action is sent by the hub.
type Action struct {
	Name string                     `json:"name"`
	Args map[string]json.RawMessage `json:"args"`
}

// Error is the error object used in action results and hub error messages.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

// ActionResult answers an action (envelope re = action id).
type ActionResult struct {
	OK     bool   `json:"ok"`
	Result any    `json:"result,omitempty"`
	Error  *Error `json:"error,omitempty"`
}

// Event reports a state change on the node.
type Event struct {
	Kind string         `json:"kind"`
	Data map[string]any `json:"data"`
}

// QueuedEvents carries events collected while the hub was unreachable.
type QueuedEvents struct {
	Events []Envelope `json:"events"`
}

// ModelGGUF are the facts a GGUF file states in its header: the hub derives the kind of a model
// from them (an embedding model names a pooling, a chat model carries a chat template).
type ModelGGUF struct {
	Architecture    string `json:"architecture,omitempty"`
	ContextLength   int    `json:"context_length,omitempty"`
	EmbeddingLength int    `json:"embedding_length,omitempty"`
	// PoolingType: 0 none, 1 mean, 2 cls, 3 last token, 4 rank (a reranker).
	PoolingType  int  `json:"pooling_type,omitempty"`
	ChatTemplate bool `json:"chat_template,omitempty"`
}
