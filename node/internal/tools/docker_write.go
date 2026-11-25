package tools

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
)

// DockerWriteAPI is the one write path to the Docker daemon: restart a container. It is a type of
// its own, not a method of Docker, so the read-only client can never gain it, and it talks to a
// second, separate socket-proxy instance (config.tools.docker_write_host) that allows exactly this
// endpoint (docs/INSTALL-NODE "Docker host, write access"; E3-01b).
type DockerWriteAPI interface {
	Restart(ctx context.Context, container string, timeoutSeconds int) error
}

// DockerWriter implements DockerWriteAPI over the Docker Engine API.
type DockerWriter struct{ d *Docker }

// writePaths are the endpoints a write capability may POST to: nothing but a container restart.
var writePaths = []*regexp.Regexp{
	regexp.MustCompile(`^/containers/[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}/restart$`),
}

// NewDockerWriter connects to host with the same rules as NewDocker (unix socket or loopback).
func NewDockerWriter(host string) (*DockerWriter, error) {
	d, err := NewDocker(host)
	if err != nil {
		return nil, err
	}
	return &DockerWriter{d: d}, nil
}

// Restart restarts one container (POST /containers/{id}/restart?t=N).
func (w *DockerWriter) Restart(ctx context.Context, container string, timeoutSeconds int) error {
	if err := checkContainer(container); err != nil {
		return err
	}
	path := "/containers/" + container + "/restart"
	ok := false
	for _, re := range writePaths {
		if re.MatchString(path) {
			ok = true
			break
		}
	}
	if !ok {
		return fmt.Errorf("the Docker API path %q is not one this node writes", path)
	}
	q := url.Values{}
	if timeoutSeconds > 0 {
		q.Set("t", fmt.Sprint(timeoutSeconds))
	}
	u := w.d.base + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, nil)
	if err != nil {
		return err
	}
	resp, err := w.d.client.Do(req)
	if err != nil {
		return dockerUnreachable(err)
	}
	defer resp.Body.Close() //nolint:errcheck // the body of a restart answer is not used
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
	switch resp.StatusCode {
	case http.StatusNoContent, http.StatusOK:
		return nil
	case http.StatusNotFound:
		return inputErr("no such container")
	case http.StatusForbidden:
		return errors.New("the Docker write proxy refuses this request: " + firstLine(string(body)))
	}
	return fmt.Errorf("the Docker API answered %d: %s", resp.StatusCode, firstLine(string(body)))
}
