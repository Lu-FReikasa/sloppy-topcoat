package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"

	"topcoat/node/internal/diskfree"
	"topcoat/node/internal/hf"
	"topcoat/node/internal/protocol"
	"topcoat/node/internal/providers/runner"
	"topcoat/node/internal/transfer"
)

// Direct model transfer between nodes (E2-07, D51; brief E2-07-model-distribution,
// "Wire contract"). models.send opens a single-use TLS socket on the source for exactly one
// model; models.receive pulls it on the target. Neither action runs on a node that is gaming
// or paused, and a node has at most one transfer at a time. The token is never logged.

// Transfer error codes of transfer_progress.
const (
	xferNoRoute      = "no_route"
	xferHashMismatch = "hash_mismatch"
	xferDisk         = "insufficient_disk"
	xferTimeout      = "timeout"
	xferCanceled     = "canceled"
	xferInternal     = "internal"
)

// transferReserve is what must stay free on the target on top of the file (like downloads).
const transferReserve = int64(1) << 30

const maxTransferEndpoints = 16

var (
	tokenRe  = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	sha256Re = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// transferRun is the transfer in progress.
type transferRun struct {
	id     string
	role   string // send or receive
	cancel context.CancelCauseFunc
}

// addresses are the unicast IPs of this node.
func (a *Agent) addresses() []string {
	if a.opts.Addresses == nil {
		return nil
	}
	return a.opts.Addresses()
}

// abortTransfer cancels a running transfer (e.g. a game started).
func (a *Agent) abortTransfer(reason string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.xfer != nil {
		a.log.Warn("aborting model transfer", "transfer_id", a.xfer.id, "role", a.xfer.role, "reason", reason)
		a.xfer.cancel(fmt.Errorf("aborted: %s", reason))
	}
}

// busyForTransfer refuses a transfer on a node that is gaming or paused.
func (a *Agent) busyForTransfer() *protocol.Error {
	switch {
	case a.isGaming():
		return errorf(protocol.ErrBusy, "node is busy gaming; no model transfer now")
	case a.isPaused():
		return errorf(protocol.ErrBusy, "node is paused for local use; no model transfer now")
	}
	return nil
}

// claimTransfer registers the transfer; nil if another one is open.
func (a *Agent) claimTransfer(id, role string) (*transferRun, context.Context, *protocol.Error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.xfer != nil {
		return nil, nil, errorf(protocol.ErrBusy, "transfer %s is still open on this node", a.xfer.id)
	}
	ctx, cancel := context.WithCancelCause(a.baseCtx)
	run := &transferRun{id: id, role: role, cancel: cancel}
	a.xfer = run
	return run, ctx, nil
}

// xferEnd is how a transfer ended: the final transfer_progress event.
type xferEnd struct {
	status string // "done" or "failed"
	done   int64
	code   string
	err    error
}

// finishTransfer frees the transfer slot and then reports the end. In this order a hub that
// starts the next transfer as soon as it sees done/failed never finds this one still open.
func (a *Agent) finishTransfer(run *transferRun, total int64, end xferEnd) {
	a.releaseTransfer(run)
	a.emitTransfer(run.id, run.role, end.status, end.done, total, end.code, end.err)
}

func (a *Agent) releaseTransfer(run *transferRun) {
	run.cancel(nil)
	a.mu.Lock()
	if a.xfer == run {
		a.xfer = nil
	}
	a.mu.Unlock()
}

func (a *Agent) emitTransfer(id, role, status string, done, total int64, code string, err error) {
	d := map[string]any{"transfer_id": id, "role": role, "status": status, "transferred_bytes": done, "total_bytes": total}
	if code != "" {
		d["error_code"] = code
	}
	if err != nil {
		d["error"] = err.Error()
	}
	a.emit(protocol.EventTransferProgress, d)
}

// throttledProgress reports running progress at most every DownloadProgressInterval.
func (a *Agent) throttledProgress(id, role string, last *lastProgress) hf.Progress {
	return hf.Throttle(func(done, total int64) {
		last.set(done, total)
		if done < total {
			a.emitTransfer(id, role, "running", done, total, "", nil)
		}
	}, DownloadProgressInterval, a.opts.Now)
}

// lastProgress remembers the last reported byte counts (read after the transfer failed).
type lastProgress struct {
	mu          sync.Mutex
	done, total int64
}

func (l *lastProgress) set(done, total int64) {
	l.mu.Lock()
	l.done, l.total = done, total
	l.mu.Unlock()
}

func (l *lastProgress) get() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.done
}

// --- models.send --------------------------------------------------------------------

func (a *Agent) actModelsSend(_ context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "transfer_id", "model", "token", "peer_addresses"); perr != nil {
		return nil, perr, nil
	}
	id, ok := argString(args, "transfer_id")
	if !ok || !protocol.IsUUID(id) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.transfer_id must be a UUID"), nil
	}
	model, ok := argString(args, "model")
	if !ok || !modelNameRe.MatchString(model) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.model must be a model name (letters, digits, '.', '_' or '-')"), nil
	}
	token, ok := argString(args, "token")
	if !ok || !tokenRe.MatchString(token) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.token must be 43 base64url characters"), nil
	}
	var peers []string
	if raw, ok := args["peer_addresses"]; !ok || json.Unmarshal(raw, &peers) != nil || len(peers) > maxTransferEndpoints {
		return nil, errorf(protocol.ErrInvalidArgs, "args.peer_addresses must be a list of up to %d IP addresses", maxTransferEndpoints), nil
	}
	for _, p := range peers {
		if _, err := netip.ParseAddr(p); err != nil {
			return nil, errorf(protocol.ErrInvalidArgs, "args.peer_addresses: %q is not an IP address", p), nil
		}
	}
	if perr := a.busyForTransfer(); perr != nil {
		return nil, perr, nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	mf, ok := r.(runner.ModelFiler)
	if !ok {
		return nil, errorf(protocol.ErrNotImplemented, "runner %s cannot send model files", r.Type()), nil
	}
	path, size, _, err := mf.ModelFile(model)
	switch {
	case errors.Is(err, runner.ErrUnknownModel), errors.Is(err, runner.ErrSplitModel):
		return nil, errorf(protocol.ErrInvalidArgs, "%v", err), nil
	case err != nil:
		return nil, errorf(protocol.ErrRunnerError, "model %s: %v", model, err), nil
	case size <= 0:
		return nil, errorf(protocol.ErrInvalidArgs, "model %s is empty", model), nil
	}
	run, ctx, perr := a.claimTransfer(id, "send")
	if perr != nil {
		return nil, perr, nil
	}
	return map[string]any{"started": true}, nil, func() {
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			defer a.releaseTransfer(run) // a no-op after finishTransfer; frees the slot on a panic
			a.finishTransfer(run, size, a.runSend(ctx, run, model, path, size, token, peers))
		}()
	}
}

// runSend serves the file until the target has it; the caller reports the end. The socket is
// closed when it returns.
func (a *Agent) runSend(ctx context.Context, run *transferRun, model, path string, size int64, token string, peers []string) xferEnd {
	id := run.id
	var last lastProgress
	fail := func(err error) xferEnd {
		code := xferInternal
		if ctx.Err() != nil {
			code, err = xferCanceled, context.Cause(ctx)
		} else if errors.Is(err, transfer.ErrExpired) {
			code = xferTimeout
		}
		a.log.Warn("model send failed", "transfer_id", id, "model", model, "code", code, "err", err)
		return xferEnd{status: "failed", done: last.get(), code: code, err: err}
	}
	a.log.Info("model send: hashing", "transfer_id", id, "model", model, "bytes", size)
	sum, err := hashFile(ctx, path, size)
	if err != nil {
		return fail(err)
	}
	tc := a.transferSettings()
	progress := a.throttledProgress(id, "send", &last)
	srv, err := transfer.Start(ctx, transfer.ServerConfig{
		File: path, Size: size, SHA256: sum, Token: token, PeerAddrs: peers, Addresses: a.addresses(),
		PortMin: tc.PortMin, PortMax: tc.PortMax, TTL: tc.TTL,
		Progress: func(done, total int64) { progress(done, total) },
	})
	if err != nil {
		return fail(err)
	}
	defer srv.Close()
	addrs := a.addresses()
	if addrs == nil {
		addrs = []string{}
	}
	a.log.Info("model send: socket open", "transfer_id", id, "model", model, "port", srv.Port())
	a.emit(protocol.EventTransferReady, map[string]any{
		"transfer_id": id, "model": model, "size": size, "sha256": sum,
		"port": srv.Port(), "addresses": addrs, "cert_sha256": srv.CertSHA256(),
	})
	if err := srv.Wait(); err != nil {
		return fail(err)
	}
	a.log.Info("model sent", "transfer_id", id, "model", model)
	return xferEnd{status: "done", done: size}
}

// transferSettings is the effective config.transfer.
func (a *Agent) transferSettings() protocol.TransferSettings {
	cfg, _, _ := a.config()
	if cfg == nil {
		return (&protocol.NodeConfig{}).EffectiveTransfer()
	}
	return cfg.EffectiveTransfer()
}

// hashFile computes the SHA-256 of a file, stopping when ctx ends. A file that changed its
// size since it was listed is refused.
func hashFile(ctx context.Context, path string, size int64) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	buf := make([]byte, 1<<20)
	var n int64
	for {
		if err := ctx.Err(); err != nil {
			return "", context.Cause(ctx)
		}
		k, rerr := f.Read(buf)
		h.Write(buf[:k])
		n += int64(k)
		if errors.Is(rerr, io.EOF) {
			break
		}
		if rerr != nil {
			return "", rerr
		}
	}
	if n != size {
		return "", fmt.Errorf("model file changed while hashing (%d != %d bytes)", n, size)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// --- models.receive -----------------------------------------------------------------

func (a *Agent) actModelsReceive(ctx context.Context, args map[string]json.RawMessage) (any, *protocol.Error, func()) {
	if perr := onlyKeys(args, "transfer_id", "model", "size", "sha256", "token", "cert_sha256", "endpoints"); perr != nil {
		return nil, perr, nil
	}
	id, ok := argString(args, "transfer_id")
	if !ok || !protocol.IsUUID(id) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.transfer_id must be a UUID"), nil
	}
	name, ok := argString(args, "model")
	if !ok || !modelNameRe.MatchString(name) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.model must be a model name (letters, digits, '.', '_' or '-')"), nil
	}
	size, ok := parseInt(args["size"])
	if !ok || size < 1 {
		return nil, errorf(protocol.ErrInvalidArgs, "args.size must be an integer >= 1"), nil
	}
	sum, ok := argString(args, "sha256")
	if !ok || !sha256Re.MatchString(sum) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.sha256 must be 64 lower-case hex characters"), nil
	}
	token, ok := argString(args, "token")
	if !ok || !tokenRe.MatchString(token) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.token must be 43 base64url characters"), nil
	}
	certFP, ok := argString(args, "cert_sha256")
	if !ok || !sha256Re.MatchString(certFP) {
		return nil, errorf(protocol.ErrInvalidArgs, "args.cert_sha256 must be 64 lower-case hex characters"), nil
	}
	var endpoints []string
	if raw, ok := args["endpoints"]; !ok || json.Unmarshal(raw, &endpoints) != nil || len(endpoints) < 1 || len(endpoints) > maxTransferEndpoints {
		return nil, errorf(protocol.ErrInvalidArgs, "args.endpoints must be a list of 1..%d \"ip:port\" entries", maxTransferEndpoints), nil
	}
	for _, e := range endpoints {
		if !validEndpoint(e) {
			return nil, errorf(protocol.ErrInvalidArgs, "args.endpoints: %q is not ip:port (IPv6 as [ip]:port)", e), nil
		}
	}
	if perr := a.busyForTransfer(); perr != nil {
		return nil, perr, nil
	}
	r, perr := a.runner()
	if perr != nil {
		return nil, perr, nil
	}
	rcv, ok := r.(runner.ModelReceiver)
	dir := ""
	if ok {
		dir = rcv.ReceiveDir()
	}
	if dir == "" {
		return nil, errorf(protocol.ErrNotImplemented, "runner %s cannot receive model files", r.Type()), nil
	}
	dest := filepath.Join(dir, name+".gguf")
	if models, err := r.ListModels(ctx); err == nil {
		for _, m := range models {
			if m.Name == name {
				return nil, errorf(protocol.ErrInvalidArgs, "model %s already exists on this node", name), nil
			}
		}
	}
	if _, err := os.Stat(dest); err == nil {
		return nil, errorf(protocol.ErrInvalidArgs, "model %s already exists on this node", name), nil
	}
	if err := a.checkTransferDisk(dir, dest, size); err != nil {
		return nil, errorf(protocol.ErrInsufficientDisk, "%v", err), nil
	}
	run, rctx, perr := a.claimTransfer(id, "receive")
	if perr != nil {
		return nil, perr, nil
	}
	return map[string]any{"started": true, "model": name}, nil, func() {
		a.bg.Add(1)
		go func() {
			defer a.bg.Done()
			defer a.releaseTransfer(run) // a no-op after finishTransfer; frees the slot on a panic
			end := a.runReceive(rctx, run, name, dir, dest, size, sum, token, certFP, endpoints)
			a.finishTransfer(run, size, end)
			if end.status == "done" {
				a.sendInventory(a.jobCtx())
			}
		}()
	}
}

func validEndpoint(e string) bool {
	host, port, err := net.SplitHostPort(e)
	if err != nil {
		return false
	}
	if _, err := netip.ParseAddr(host); err != nil {
		return false
	}
	p, err := strconv.Atoi(port)
	return err == nil && p >= 1 && p <= 65535
}

// checkTransferDisk applies the download rule: the file (minus what a .part already holds)
// plus 1 GiB must fit. A drive that cannot be measured never blocks.
func (a *Agent) checkTransferDisk(dir, dest string, size int64) error {
	measure := a.opts.DiskFree
	if measure == nil {
		measure = diskfree.Of
	}
	u, err := measure(dir)
	if err != nil {
		return nil
	}
	var have int64
	if st, err := os.Stat(dest + ".part"); err == nil {
		have = st.Size()
	}
	need := max(size-have, 0)
	if u.FreeBytes < need+transferReserve {
		return fmt.Errorf("%w: %s needs %d MB, %d MB must stay free, but only %d MB are free on the drive of %s",
			runner.ErrInsufficientDisk, filepath.Base(dest), need>>20, transferReserve>>20, u.FreeBytes>>20, dir)
	}
	return nil
}

// runReceive pulls the file from the source; the caller reports the end.
func (a *Agent) runReceive(ctx context.Context, run *transferRun, name, dir, dest string, size int64, sum, token, certFP string, endpoints []string) xferEnd {
	id := run.id
	var last lastProgress
	progress := a.throttledProgress(id, "receive", &last)
	a.log.Info("model receive started", "transfer_id", id, "model", name, "bytes", size, "endpoints", len(endpoints))
	a.emitTransfer(id, "receive", "running", 0, size, "", nil)
	err := os.MkdirAll(dir, 0o750)
	if err == nil {
		err = transfer.Fetch(ctx, endpoints, certFP, token, dest, size, sum, func(done, total int64) { progress(done, total) })
	}
	if err != nil {
		code := xferInternal
		switch {
		case errors.Is(err, transfer.ErrNoRoute):
			code = xferNoRoute
		case errors.Is(err, transfer.ErrHashMismatch):
			code = xferHashMismatch
		case errors.Is(ctx.Err(), context.Canceled):
			code, err = xferCanceled, context.Cause(ctx)
		case errors.Is(err, context.DeadlineExceeded):
			code = xferTimeout
		}
		a.log.Warn("model receive failed", "transfer_id", id, "model", name, "code", code, "err", err)
		return xferEnd{status: "failed", done: last.get(), code: code, err: err}
	}
	a.log.Info("model received", "transfer_id", id, "model", name)
	return xferEnd{status: "done", done: size}
}
