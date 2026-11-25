package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"topcoat/node/internal/agent"
	"topcoat/node/internal/buildcap"
	"topcoat/node/internal/buildsvc"
	"topcoat/node/internal/servicebus"
)

// The build service (NS-01, D80): a second process under its own OS user that builds releases.
// The node listens on a Unix socket for it, knows it by the kernel's word about its user id, and
// uploads the files it makes to the hub with the node's credentials. UNVERIFIED on real
// hardware: the systemd unit, rootless Docker and the real repositories; the pieces are tested
// with fakes (servicebus, buildsvc, buildcap, agent).

const maxUploadBytes = 512 << 20

var (
	uploadIDRe   = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	uploadNameRe = regexp.MustCompile(`^topcoat-node-(linux|windows)-(amd64|arm64)(\.exe)?$`)
)

// checkUpload decides whether the node may send this file for the service: the build id and the
// name are of the expected shape, the path lies below the service's own directory for exactly
// that build, and it is one regular file (no link out of the directory, no directory, not empty,
// not too big). It returns what to open and how long it is.
func checkUpload(work string, u buildsvc.Upload) (string, int64, error) {
	if !uploadIDRe.MatchString(u.BuildID) || !uploadNameRe.MatchString(u.Name) {
		return "", 0, errors.New("build id or file name is not valid")
	}
	want := filepath.Join(work, "builds", u.BuildID) + string(filepath.Separator)
	path := filepath.Clean(u.Path)
	if !strings.HasPrefix(path, want) {
		return "", 0, errors.New("the file is not in the directory of this build")
	}
	fi, err := os.Lstat(path)
	if err != nil {
		return "", 0, err
	}
	if !fi.Mode().IsRegular() {
		return "", 0, errors.New("only a regular file is sent")
	}
	if fi.Size() == 0 || fi.Size() > maxUploadBytes {
		return "", 0, errors.New("the file is empty or larger than the hub takes")
	}
	// a link in a directory above would lead out: the real path must be the path
	real, err := filepath.EvalSymlinks(path)
	if err != nil || real != path {
		return "", 0, errors.New("the path goes through a link")
	}
	return path, fi.Size(), nil
}

// buildBus returns the node's side of the build service, listening on the sockets below dir. The
// socket belongs to the service's OS user; the kernel names the caller.
func buildBus(ctx context.Context, dir, userName, work string, logger *slog.Logger) (*servicebus.Bus, error) {
	u, err := user.Lookup(userName)
	if err != nil {
		return nil, fmt.Errorf("build user %q: %w", userName, err)
	}
	uid, err := strconv.ParseUint(u.Uid, 10, 32)
	if err != nil {
		return nil, err
	}
	gid, err := strconv.ParseUint(u.Gid, 10, 32)
	if err != nil {
		return nil, err
	}
	bus, err := servicebus.New(dir, []servicebus.Spec{{ID: buildcap.Service, Actions: buildsvc.Actions}},
		func(peer uint32) (string, bool) { return buildcap.Service, peer == uint32(uid) })
	if err != nil {
		return nil, err
	}
	chown := func(path, _ string) error { return os.Chown(path, int(uid), int(gid)) }
	if err := bus.Listen(ctx, chown); err != nil {
		return nil, err
	}
	logger.Info("build service socket ready", "socket", bus.SocketPath(buildcap.Service), "user", userName)
	return bus, nil
}

// handleUploads lets the build service ask the node to upload a finished file to the hub.
func handleUploads(bus *servicebus.Bus, a *agent.Agent, work string, logger *slog.Logger) error {
	return bus.Handle("upload", func(ctx context.Context, _ string, input json.RawMessage) (json.RawMessage, error) {
		var u buildsvc.Upload
		if err := json.Unmarshal(input, &u); err != nil {
			return nil, errors.New("bad upload request")
		}
		path, size, err := checkUpload(work, u)
		if err != nil {
			return nil, err
		}
		f, err := os.Open(path) //nolint:gosec // checked above
		if err != nil {
			return nil, err
		}
		defer func() { _ = f.Close() }()
		if err := a.UploadArtifact(ctx, u.BuildID, u.Name, u.OS, u.Arch, f, size); err != nil {
			return nil, err
		}
		logger.Info("build artifact uploaded", "build", u.BuildID, "name", u.Name, "bytes", size)
		return json.RawMessage(`{}`), nil
	})
}

// busHost is the service's view of the node: Upload goes over the bus.
type busHost struct{ c *servicebus.Client }

func (h busHost) Upload(ctx context.Context, u buildsvc.Upload) error {
	_, err := h.c.Host(ctx, "upload", u)
	return err
}

// cmdService runs a service process of this node. Only "build" exists.
func cmdService(ctx context.Context, args []string, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "build" {
		fmt.Fprintln(stderr, "service: the only service is build:  topcoat-node service build --socket FILE --work DIR ...")
		return 2
	}
	fs := flag.NewFlagSet("service build", flag.ContinueOnError)
	fs.SetOutput(stderr)
	socket := fs.String("socket", "/run/topcoat-node/build.sock", "the node's socket for this service")
	work := fs.String("work", "", "the service's own directory (mirrors, clones, outputs) (required)")
	releaseRepo := fs.String("release-repo", "", "URL or path of the release repository")
	devMirror := fs.String("dev-mirror", "", "URL or path of the mirror of the development repository")
	keys := fs.String("update-keys", "", "the hub's public update keys, built into the node release (comma-separated base64)")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	if *work == "" || (*releaseRepo == "" && *devMirror == "") {
		fmt.Fprintln(stderr, "service build: --work and at least one of --release-repo and --dev-mirror are required")
		return 2
	}
	ctx, stop := signal.NotifyContext(ctx, os.Interrupt)
	defer stop()
	logger := slog.New(slog.NewTextHandler(stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	sources := map[string]string{}
	if *releaseRepo != "" {
		sources["release_repo"] = *releaseRepo
	}
	if *devMirror != "" {
		sources["dev_mirror"] = *devMirror
	}
	for {
		err := serveBuild(ctx, *socket, *work, sources, *keys)
		if ctx.Err() != nil {
			return 0
		}
		logger.Warn("build service disconnected; trying again", "err", err)
		select {
		case <-ctx.Done():
			return 0
		case <-time.After(5 * time.Second):
		}
	}
}

func serveBuild(ctx context.Context, socket, work string, sources map[string]string, keys string) error {
	cl, err := servicebus.Dial(ctx, socket, buildcap.Service, buildsvc.Actions)
	if err != nil {
		return err
	}
	svc, err := buildsvc.New(buildsvc.Config{
		Work: work, Sources: sources, UpdateKeys: keys,
		Runner: buildsvc.ExecRunner{}, Host: busHost{cl},
	})
	if err != nil {
		return err
	}
	return cl.Serve(ctx, svc.Handle)
}
