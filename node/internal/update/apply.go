package update

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"
)

// Host is what the updater needs from the operating system. The real implementations sit in
// internal/service (UNVERIFIED on real hardware); the tests use a fake.
type Host interface {
	// StopNode stops the node service and everything running from the program file.
	StopNode(ctx context.Context) error
	// StartNode starts the node service (already running is not an error).
	StartNode(ctx context.Context) error
	// ReplaceProgram makes data the program file and keeps the old file for RestoreProgram.
	ReplaceProgram(ctx context.Context, data []byte) error
	// RestoreProgram puts the file kept by the last ReplaceProgram back; it does nothing when
	// ReplaceProgram did not keep one (ReplaceProgram first removes a leftover of an older update).
	RestoreProgram(ctx context.Context) error
}

// ErrApplied is returned by Run after a successful update: the updater itself still runs the old
// code, so the caller ends the process and the service manager starts the new one.
var ErrApplied = errors.New("update applied; the updater restarts to run the new program")

// Outcome is what one Apply call did.
type Outcome int

// Outcomes of Apply.
const (
	NothingToDo Outcome = iota
	Applied
	Failed
)

// Updater applies staged builds. It trusts only its own keys and the bytes it read itself.
type Updater struct {
	Keys      []Key
	Layout    Layout
	Host      Host
	Plat      Platform
	Installed string // the version of the running updater = the installed program

	ConfirmTimeout time.Duration // default 120 s
	Poll           time.Duration // default 5 s
	Now            func() time.Time
	Sleep          func(ctx context.Context, d time.Duration) error
	Log            *slog.Logger
}

func (u *Updater) now() time.Time {
	if u.Now != nil {
		return u.Now()
	}
	return time.Now()
}

func (u *Updater) log() *slog.Logger {
	if u.Log != nil {
		return u.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (u *Updater) sleep(ctx context.Context, d time.Duration) error {
	if u.Sleep != nil {
		return u.Sleep(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (u *Updater) confirmTimeout() time.Duration {
	if u.ConfirmTimeout > 0 {
		return u.ConfirmTimeout
	}
	return 120 * time.Second
}

func (u *Updater) poll() time.Duration {
	if u.Poll > 0 {
		return u.Poll
	}
	return 5 * time.Second
}

// Run is the updater service: it writes its heartbeat, recovers an interrupted update and applies
// staged builds until ctx ends. It returns ErrApplied after a successful update.
func (u *Updater) Run(ctx context.Context) error {
	if err := u.Layout.EnsureDir(); err != nil {
		return err
	}
	u.writeStatus()
	if out := u.recover(ctx); out == Applied {
		return ErrApplied
	}
	for {
		u.writeStatus()
		out, err := u.Apply(ctx)
		if err != nil && ctx.Err() == nil {
			u.log().Error("apply update", "err", err)
		}
		if out == Applied {
			return ErrApplied
		}
		if err := u.sleep(ctx, u.poll()); err != nil {
			return err
		}
	}
}

func (u *Updater) writeStatus() {
	st := Status{AliveAt: stamp(u.now()), Version: u.Installed, Keys: KeyIDs(u.Keys)}
	if err := writeJSON(u.Layout.status(), st); err != nil {
		u.log().Warn("write updater status", "err", err)
	}
}

func (u *Updater) writeResult(ok bool, version, reason string) {
	id := make([]byte, 8)
	_, _ = rand.Read(id)
	res := Result{ID: hex.EncodeToString(id), OK: ok, Version: version, Reason: reason, At: stamp(u.now())}
	if err := writeJSON(u.Layout.result(), res); err != nil {
		u.log().Error("write update result", "err", err)
	}
}

func (u *Updater) reject(version, reason string) (Outcome, error) {
	u.log().Warn("staged build refused", "version", version, "reason", reason)
	u.writeResult(false, version, reason)
	u.Layout.Clear()
	return Failed, errors.New(reason)
}

// Apply handles one staged build, if there is one.
func (u *Updater) Apply(ctx context.Context) (Outcome, error) {
	// Nothing is read, removed or written through a link the node account may have prepared.
	if err := u.Layout.CheckDirs(); err != nil {
		return NothingToDo, err
	}
	var ready Ready
	if err := readJSON(u.Layout.ready(), &ready); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return NothingToDo, nil
		}
		removeIfExists(u.Layout.ready())
		return NothingToDo, fmt.Errorf("read the update request: %w", err)
	}
	manifest, sigLine, binary, err := u.Layout.ReadStaged()
	if err != nil {
		return u.reject(ready.Version, "staged build unreadable: "+err.Error())
	}
	m, err := Check(u.Keys, manifest, sigLine, binary, u.Plat, u.Installed)
	if err != nil {
		return u.reject(ready.Version, "staged build refused: "+err.Error())
	}
	// The request is consumed before anything changes: a crash must not apply it twice.
	removeIfExists(u.Layout.ready())
	started := stamp(u.now())
	if err := writeJSON(u.Layout.applying(), Applying{Version: m.Version, Phase: PhaseStopping, StartedAt: started}); err != nil {
		return u.reject(m.Version, "cannot write the update marker: "+err.Error())
	}
	u.log().Info("applying update", "from", u.Installed, "to", m.Version)

	if err := u.Host.StopNode(ctx); err != nil {
		_ = u.Host.StartNode(ctx)
		return u.fail(m.Version, "could not stop the node: "+err.Error(), false)
	}
	if err := u.Host.ReplaceProgram(ctx, binary); err != nil {
		return u.fail(m.Version, "could not replace the program: "+err.Error(), true)
	}
	if err := writeJSON(u.Layout.applying(), Applying{Version: m.Version, Phase: PhaseReplaced, StartedAt: started}); err != nil {
		return u.fail(m.Version, "cannot write the update marker: "+err.Error(), true)
	}
	return u.finish(ctx, m.Version)
}

// finish starts the node and waits for its confirmation; a missing or wrong confirmation rolls the
// program file back.
func (u *Updater) finish(ctx context.Context, version string) (Outcome, error) {
	u.Layout.ClearConfirmed()
	if err := u.Host.StartNode(ctx); err != nil {
		return u.fail(version, "the node did not start: "+err.Error(), true)
	}
	if err := u.waitConfirmed(ctx, version); err != nil {
		return u.fail(version, err.Error(), true)
	}
	removeIfExists(u.Layout.applying())
	u.Layout.Clear()
	u.writeResult(true, version, "")
	u.log().Info("update confirmed", "version", version)
	return Applied, nil
}

// fail records the failure and, when the program was touched, restores the old one and starts
// the node again.
func (u *Updater) fail(version, reason string, restore bool) (Outcome, error) {
	u.log().Error("update failed", "version", version, "reason", reason, "restore", restore)
	// The context of the caller may be gone (shutdown): the way back must still run.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(context.Background()), 2*time.Minute)
	defer cancel()
	if restore {
		if err := u.Host.StopNode(ctx); err != nil {
			u.log().Error("stop the node before the rollback", "err", err)
		}
		if err := u.Host.RestoreProgram(ctx); err != nil {
			reason += "; ROLLBACK FAILED: " + err.Error()
		}
		if err := u.Host.StartNode(ctx); err != nil {
			reason += "; the old node did not start: " + err.Error()
		}
	}
	u.writeResult(false, version, reason)
	removeIfExists(u.Layout.applying())
	u.Layout.Clear()
	return Failed, errors.New(reason)
}

func (u *Updater) waitConfirmed(ctx context.Context, version string) error {
	deadline := u.now().Add(u.confirmTimeout())
	for {
		var c Confirmed
		if err := readJSON(u.Layout.confirmed(), &c); err == nil {
			if c.Version == version {
				return nil
			}
			return fmt.Errorf("the node started as version %s, expected %s", c.Version, version)
		}
		if !u.now().Before(deadline) {
			return fmt.Errorf("the node did not report back within %s", u.confirmTimeout())
		}
		if err := u.sleep(ctx, min(u.poll(), time.Second)); err != nil {
			return err
		}
	}
}

// recover finishes an update that a crash of the updater (or the machine) interrupted: the node is
// started and has to confirm the new version, else the old program comes back.
func (u *Updater) recover(ctx context.Context) Outcome {
	var a Applying
	if err := readJSON(u.Layout.applying(), &a); err != nil {
		return NothingToDo
	}
	u.log().Warn("an update was interrupted", "version", a.Version, "phase", a.Phase)
	if a.Phase != PhaseReplaced {
		// The program file was never touched: just bring the old node back.
		if err := u.Host.StartNode(ctx); err != nil {
			u.log().Error("start the node after an interrupted update", "err", err)
		}
		u.writeResult(false, a.Version, "the update was interrupted before the program was replaced")
		removeIfExists(u.Layout.applying())
		u.Layout.Clear()
		return Failed
	}
	out, err := u.finish(ctx, a.Version)
	if err != nil {
		u.log().Error("recover update", "err", err)
	}
	return out
}
