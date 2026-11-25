// Package inputhelper reports how long ago the person at a Windows machine last touched the
// keyboard or mouse, to a service that cannot see it itself (D24).
//
// Why: the node runs as the virtual service account NT SERVICE\topcoat-node in session 0.
// WTSQuerySessionInformation on the console session is refused for that account ("Zugriff
// verweigert"), and GetLastInputInfo only ever describes the calling process's own session. So a
// small helper of the same binary runs in the user's session (`topcoat-node input-helper`, started
// at logon by a scheduled task that needs no stored password), asks GetLastInputInfo there and
// answers the service over a named pipe.
//
// The pipe is local only (remote clients are rejected, no network port) and its ACL lets only the
// service's SID connect. The wire format is one request line and one JSON line back, and is
// plain Go so it is tested on every OS; the Windows glue (pipe, GetLastInputInfo) sits in the
// *_windows.go files and is UNVERIFIED on real hardware.
package inputhelper

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"
)

// Version of the wire format.
const Version = 1

const (
	request = "idle"
	maxLine = 256
)

// ErrNoHelper says nothing answers on the console session's pipe: the helper is not running
// (nobody is logged on, the scheduled task has not started yet, or it was never installed).
var ErrNoHelper = errors.New("no input helper is running in the console session")

// ErrAlreadyRunning is what a second helper of the same session gets.
var ErrAlreadyRunning = errors.New("an input helper is already running in this session")

// PipeName is the named pipe of the helper in a session. The session id is part of the name so
// the service asks the helper of the session that is at the console.
func PipeName(session uint32) string {
	return fmt.Sprintf(`\\.\pipe\topcoat-node-input-%d`, session)
}

// PipeSDDL is the security descriptor of the pipe: read and write for the given SID and nobody
// else (not even Administrators — the helper's own handle needs no access entry).
func PipeSDDL(serviceSID string) string {
	return "D:P(A;;GRGW;;;" + serviceSID + ")"
}

type reply struct {
	V      int    `json:"v"`
	IdleMS int64  `json:"idle_ms"`
	Error  string `json:"error,omitempty"`
}

// Serve answers requests on conn until it ends (io.EOF is a normal end). idle returns the time
// since the last input.
func Serve(conn io.ReadWriter, idle func() (time.Duration, error)) error {
	r := bufio.NewReaderSize(conn, maxLine)
	for {
		line, err := readLine(r)
		if err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
		rep := reply{V: Version}
		if line != request {
			rep.Error = "unknown request"
		} else if d, err := idle(); err != nil {
			rep.Error = err.Error()
		} else {
			rep.IdleMS = d.Milliseconds()
		}
		b, err := json.Marshal(rep)
		if err != nil {
			return err
		}
		if _, err := conn.Write(append(b, '\n')); err != nil {
			return err
		}
	}
}

// Query asks a helper for the time since the last input.
func Query(conn io.ReadWriter) (time.Duration, error) {
	if _, err := io.WriteString(conn, request+"\n"); err != nil {
		return 0, err
	}
	line, err := readLine(bufio.NewReaderSize(conn, maxLine))
	if err != nil {
		if errors.Is(err, io.EOF) {
			err = io.ErrUnexpectedEOF
		}
		return 0, err
	}
	var rep reply
	if err := json.Unmarshal([]byte(line), &rep); err != nil {
		return 0, fmt.Errorf("the input helper's answer is not understood: %w", err)
	}
	switch {
	case rep.V != Version:
		return 0, fmt.Errorf("the input helper speaks version %d, this node %d", rep.V, Version)
	case rep.Error != "":
		return 0, errors.New("input helper: " + rep.Error)
	case rep.IdleMS < 0:
		return 0, errors.New("input helper: negative idle time")
	}
	return time.Duration(rep.IdleMS) * time.Millisecond, nil
}

// readLine reads one line without its newline; a line longer than maxLine is an error, so a peer
// cannot make either side buffer without bound.
func readLine(r *bufio.Reader) (string, error) {
	b, err := r.ReadSlice('\n')
	if err != nil {
		if errors.Is(err, bufio.ErrBufferFull) {
			return "", errors.New("line too long")
		}
		return "", err
	}
	return string(b[:len(b)-1]), nil
}
