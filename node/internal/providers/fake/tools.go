package fake

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// NewToolsRegistry returns the capability registry of a fake tool node (E2-04): a Docker host with
// three containers (one of them out-of-memory killed, its log holding a secret), a journal, a few
// systemd units, a /proc and a compose project with files — all canned, nothing touches the real
// machine. The integration tests and `topcoat-node run --fake` use it.
func NewToolsRegistry() *capability.Registry {
	dir, err := os.MkdirTemp("", "topcoat-fake-tools-")
	if err != nil {
		dir = os.TempDir()
	}
	write := func(name, text string) string {
		p := filepath.Join(dir, name)
		_ = os.MkdirAll(filepath.Dir(p), 0o750)
		_ = os.WriteFile(p, []byte(text), 0o600)
		return p
	}
	compose := write("site/compose.yaml", "services:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_PASSWORD: fake-db-password\n      TZ: Europe/Berlin\n")
	write("site/.env", "DB_PASSWORD=fake-db-password\nHOST=db\n")
	proc := filepath.Join(dir, "proc")
	write("proc/uptime", "93784.12 350000.00\n")
	write("proc/loadavg", "0.52 0.41 0.30 2/345 12345\n")
	write("proc/meminfo", "MemTotal:        8000000 kB\nMemAvailable:    2000000 kB\nSwapTotal:       1024000 kB\nSwapFree:         24000 kB\n")
	write("proc/sys/kernel/osrelease", "6.8.0-fake\n")
	write("proc/self/mounts", "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /srv/data ext4 rw 0 0\n")
	// a small knowledge folder: notes, and what the exclude list must keep out of every result
	write("docs/README.md", "# Home lab notes\n\nThe web stack runs on vm-docker1 (compose project site).\n\n## Backups\n\nThe nightly backup of the database runs at 03:00 and keeps seven days.\n")
	write("docs/servers/web.md", "# Web\n\n## Restart\n\nThe web container restarts itself when it runs out of memory; raise the memory limit in the compose file.\n")
	write("docs/SENSIBEL/logins.md", "# never indexed\nadmin password: fake-secret-value\n")
	write("docs/servers/nas-password.md", "# never indexed\n")
	etc := filepath.Join(dir, "etc")
	write("etc/os-release", "PRETTY_NAME=\"Fake Linux 1.0\"\n")
	return tools.NewRegistry(tools.Options{
		// a fake Uptime Kuma answers from memory (no network, no listening port)
		Connectors: &tools.Connectors{Kuma: &tools.KumaConfig{
			URL: "http://kuma.fake.invalid:3001", APIKey: fakeKumaKey, TLS: tools.TLS{Transport: fakeKuma{}},
		}},
		// a fake SearXNG and a fake public page answer from memory; the fake name resolves to a
		// public address, "intranet.example.org" to a private one
		Web: tools.WebOptions{
			SearxngURL: "http://searx.fake.invalid:8080", Fetch: true, Transport: fakeWeb{},
			Resolve: func(_ context.Context, host string) ([]netip.Addr, error) {
				switch host {
				case "docs.example.org":
					return []netip.Addr{netip.MustParseAddr("93.184.216.34")}, nil
				case "intranet.example.org":
					return []netip.Addr{netip.MustParseAddr("10.0.0.5")}, nil
				}
				return nil, fmt.Errorf("no such host")
			},
		},
		// net.check watches one gateway (a fake ping) and one name (fake lookup)
		Checks: tools.CheckOptions{
			NetTargets: []tools.NetTarget{{Name: "gateway", Host: "10.0.0.1", Ping: true}},
			DNSNames:   []string{"status.example.lan"},
			Lookup:     func(context.Context, string) ([]string, error) { return []string{"10.0.0.7"}, nil },
			Ping: func(context.Context, string) (tools.PingResult, error) {
				return tools.PingResult{Sent: 3, Received: 3, AvgMS: 0.5}, nil
			},
		},
		Docker: fakeDocker{workdir: filepath.Join(dir, "site"), compose: compose},
		// the fake's own directory is the only one released (default deny, like a real node)
		ComposeRoots: []string{dir},
		DocsRoots:    []string{filepath.Join(dir, "docs")},
		Commander:    fakeCommander{},
		ProcRoot:     proc, EtcRoot: etc,
		StatFS: func(p string) (tools.FSStat, error) {
			if p == "/srv/data" {
				return tools.FSStat{TotalBytes: 200 << 30, FreeBytes: 10 << 30, AvailBytes: 10 << 30, Inodes: 1000, InodesFree: 500}, nil
			}
			return tools.FSStat{TotalBytes: 100 << 30, FreeBytes: 60 << 30, AvailBytes: 60 << 30, Inodes: 1000, InodesFree: 900}, nil
		},
	})
}

type fakeCommander struct{}

func (fakeCommander) Run(_ context.Context, bin string, args ...string) ([]byte, error) {
	switch {
	case bin == "journalctl":
		return []byte("2026-09-30T07:00:00+0000 vm app[1]: started\n2026-09-30T07:00:05+0000 vm app[1]: error: out of memory\n"), nil
	case bin == "systemctl" && len(args) > 0 && args[0] == "show":
		return []byte("Id=app.service\nDescription=Fake app\nLoadState=loaded\nActiveState=failed\nSubState=failed\nResult=oom-kill\nNRestarts=3\n"), nil
	case bin == "systemctl" && len(args) > 0 && args[0] == "list-unit-files":
		return []byte("app.service enabled enabled\ndocker.service enabled enabled\nnginx.service enabled enabled\nssh.service disabled enabled\n"), nil
	case bin == "systemctl" && len(args) > 0 && args[0] == "list-units" && !slices.Contains(args, "--failed"):
		// the inventory's look at the services: app failed, docker running, nginx enabled but down
		return []byte("app.service loaded failed failed Fake app\ndocker.service loaded active running Docker Application Container Engine\n" +
			"nginx.service loaded inactive dead A high performance web server\ncron.service loaded active running Regular background program processing\n" +
			"ssh.service loaded inactive dead OpenBSD Secure Shell server\nsystemd-fsck@x.service loaded inactive dead File System Check\n"), nil
	case bin == "systemctl":
		return []byte("app.service loaded failed failed Fake app\n"), nil
	}
	return nil, fmt.Errorf("fake: %s is not available", bin)
}

type fakeDocker struct{ workdir, compose string }

func (f fakeDocker) Get(_ context.Context, path string, _ url.Values) ([]byte, bool, error) {
	labels := func(service string) string {
		return fmt.Sprintf(`{"com.docker.compose.project":"site","com.docker.compose.service":%q,"com.docker.compose.project.working_dir":%q,"com.docker.compose.project.config_files":%q}`, service, f.workdir, f.compose)
	}
	switch {
	case path == "/containers/json":
		return []byte(`[
		 {"Id":"1111111111111111","Names":["/site-web-1"],"Image":"nginx:1","State":"running","Status":"Up 3 days (healthy)","Labels":` + labels("web") + `},
		 {"Id":"2222222222222222","Names":["/site-db-1"],"Image":"postgres:16","State":"running","Status":"Up 3 days","Labels":` + labels("db") + `},
		 {"Id":"3333333333333333","Names":["/site-worker-1"],"Image":"worker:2","State":"exited","Status":"Exited (137) 2 hours ago","Labels":` + labels("worker") + `}]`), false, nil
	case strings.HasSuffix(path, "/logs"):
		return frame("2026-09-30T07:59:00Z worker starting\n2026-09-30T07:59:01Z connect postgres://app:demo-password@db/app failed\n2026-09-30T07:59:02Z killed"), false, nil
	case strings.HasSuffix(path, "/json"):
		ref := strings.TrimSuffix(strings.TrimPrefix(path, "/containers/"), "/json")
		name, state := "site-web-1", `{"Status":"running","Running":true,"Health":{"Status":"healthy","FailingStreak":0}}`
		switch ref {
		case "2222222222222222", "site-db-1":
			name, state = "site-db-1", `{"Status":"running","Running":true}`
		case "3333333333333333", "site-worker-1":
			name, state = "site-worker-1", `{"Status":"exited","Running":false,"OOMKilled":true,"ExitCode":137,"FinishedAt":"2026-09-30T06:00:00Z"}`
		}
		return []byte(fmt.Sprintf(`{"Id":%q,"Name":"/%s","Config":{"Image":"img","Tty":false,"Labels":%s},"State":%s,"RestartCount":7}`, ref, name, labels("x"), state)), false, nil
	}
	return nil, false, fmt.Errorf("fake docker: %s", path)
}

func frame(text string) []byte {
	h := make([]byte, 8)
	h[0] = 1
	binary.BigEndian.PutUint32(h[4:], uint32(len(text))) //nolint:gosec // a canned log line is far below 4 GiB
	return append(h, text...)
}

const fakeKumaKey = "uk1_fake_fakefakefakefake"

// fakeKuma is Uptime Kuma's /metrics for the fake tool node: one monitor up, one down.
type fakeKuma struct{}

func (fakeKuma) RoundTrip(r *http.Request) (*http.Response, error) {
	_, pw, ok := r.BasicAuth()
	status, body := http.StatusOK, `monitor_status{monitor_id="1",monitor_name="Web",monitor_type="http",monitor_url="https://web.example.lan/?token=abcdefabcdef1234",monitor_hostname="null",monitor_port="null"} 1
monitor_response_time{monitor_id="1",monitor_name="Web",monitor_type="http",monitor_url="https://web.example.lan/?token=abcdefabcdef1234",monitor_hostname="null",monitor_port="null"} 38
monitor_cert_days_remaining{monitor_id="1",monitor_name="Web",monitor_type="http",monitor_url="https://web.example.lan/?token=abcdefabcdef1234",monitor_hostname="null",monitor_port="null"} 9
monitor_status{monitor_id="2",monitor_name="NAS ping",monitor_type="ping",monitor_url="null",monitor_hostname="10.0.0.9",monitor_port="null"} 0
`
	if r.URL.Path != "/metrics" || !ok || pw != fakeKumaKey {
		status, body = http.StatusUnauthorized, ""
	}
	return &http.Response{
		StatusCode: status, Status: http.StatusText(status), Header: http.Header{"Content-Type": {"text/plain"}},
		Body: io.NopCloser(strings.NewReader(body)), Request: r,
	}, nil
}

// fakeWeb answers SearXNG's JSON API and one public page for the fake tool node.
type fakeWeb struct{}

func (fakeWeb) RoundTrip(r *http.Request) (*http.Response, error) {
	status, ctype, body := http.StatusOK, "application/json", ""
	switch {
	case r.URL.Host == "searx.fake.invalid:8080" && r.URL.Path == "/search":
		body = `{"results":[{"title":"Fixing the out-of-memory restart","url":"https://docs.example.org/oom","content":"Raise the memory limit of the container and check the swap.","engine":"fake"},{"title":"Release notes","url":"https://docs.example.org/notes","content":"Version 2.1 fixes a crash on start.","engine":"fake"}]}`
	case r.URL.Host == "docs.example.org" && r.URL.Path == "/oom":
		ctype = "text/html"
		body = "<html><head><title>Fixing the out-of-memory restart</title></head><body><h1>Out of memory</h1><p>Raise the memory limit of the container.</p></body></html>"
	default:
		status, ctype, body = http.StatusNotFound, "text/plain", "not found"
	}
	return &http.Response{
		StatusCode: status, Status: http.StatusText(status), Header: http.Header{"Content-Type": {ctype}},
		Body: io.NopCloser(strings.NewReader(body)), Request: r,
	}, nil
}
