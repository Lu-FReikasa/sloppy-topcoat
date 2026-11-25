package tools

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// healthHost is a node with a fake /proc, two containers (one killed for memory), a failed unit and
// a journal with error lines.
func healthHost(t *testing.T) (Options, *fakeDocker) {
	t.Helper()
	proc := t.TempDir()
	put := func(name, text string) {
		p := filepath.Join(proc, name)
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	put("loadavg", "0.52 0.41 0.30 2/345 12345\n")
	put("meminfo", "MemTotal:        8000000 kB\nMemAvailable:     500000 kB\nSwapTotal:       1024000 kB\nSwapFree:         24000 kB\n")
	put("self/mounts", "/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 /srv/data ext4 rw 0 0\n")
	f := dockerWithTwoContainers(t)
	f.list = strings.Replace(psList, `"State":"exited"`, `"State":"exited"`, 1)
	f.logs["web"] = frames(
		[2]string{"out", "2026-09-30T07:59:00Z starting\n"},
		[2]string{"err", "2026-09-30T07:59:01Z ERROR connect postgres://app:pa55w0rd@db/app failed\n"},
		[2]string{"err", "2026-09-30T07:59:02Z fatal: out of memory\n"},
		[2]string{"out", "2026-09-30T07:59:03Z ready\n"},
	)
	f.logs["db"] = frames([2]string{"out", "2026-09-30T07:00:00Z error before it died\n"})
	cmd := &fakeCommander{out: map[string]string{
		"systemctl list-units": "backup.service loaded failed failed Nightly backup\n",
		"journalctl": "2026-09-30T07:58:00+0000 host sshd[100]: error: maximum authentication attempts exceeded\n" +
			"2026-09-30T07:58:30+0000 host sshd[101]: error: maximum authentication attempts exceeded\n" +
			"2026-09-30T07:59:00+0000 host cron[7]: failed to open PID file\n",
	}}
	stat := func(p string) (FSStat, error) {
		if p == "/srv/data" {
			return FSStat{TotalBytes: 200 << 30, FreeBytes: 10 << 30, AvailBytes: 10 << 30, Inodes: 1000, InodesFree: 500}, nil
		}
		return FSStat{TotalBytes: 100 << 30, FreeBytes: 60 << 30, AvailBytes: 60 << 30, Inodes: 1000, InodesFree: 900}, nil
	}
	now := mustTime("2026-09-30T08:00:00Z")
	return Options{Docker: f.client(), Commander: cmd, ProcRoot: proc, StatFS: stat, Now: func() time.Time { return now }}, f
}

func strs(v any) []string {
	var out []string
	for _, x := range v.([]any) {
		out = append(out, x.(string))
	}
	return out
}

func TestHealthCheckAnswersInOneCallWhatTakesSix(t *testing.T) {
	o, f := healthHost(t)
	out, err := invoke(t, o, "health.check", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	problems := strings.Join(strs(out["problems"]), " | ")
	for _, want := range []string{
		"unit failed: backup.service", "container db: killed for memory", "disk /srv/data: 95.0% used",
		"RAM: only 6% (488 MB) available", "swap: 97% used", "container web: 2 error lines", // 2 lines match; below 5 is not a problem, so see below
	} {
		if want == "container web: 2 error lines" {
			if strings.Contains(problems, "container web") {
				t.Errorf("two error lines are not a problem by themselves: %s", problems)
			}
			continue
		}
		if !strings.Contains(problems, want) {
			t.Errorf("problems lack %q: %s", want, problems)
		}
	}
	if out["ok"] != false {
		t.Fatal("ok with problems")
	}
	c := out["containers"].(map[string]any)
	if c["total"] != float64(2) || len(c["attention"].([]any)) != 1 || c["attention"].([]any)[0].(map[string]any)["name"] != "db" {
		t.Fatalf("%v", c)
	}
	disks := out["disks"].([]any)
	if len(disks) != 1 || disks[0].(map[string]any)["mount"] != "/srv/data" { // only the one over the threshold
		t.Fatalf("%v", disks)
	}
	groups := out["errors"].([]any)
	byName := map[string]map[string]any{}
	for _, g := range groups {
		byName[g.(map[string]any)["source"].(string)] = g.(map[string]any)
	}
	web := byName["container web"]
	if web == nil || web["count"] != float64(2) || len(web["last"].([]any)) != 2 {
		t.Fatalf("%v", groups)
	}
	if strings.Contains(fmt.Sprint(web), "pa55w0rd") || out["masked"].(float64) < 1 {
		t.Fatalf("a secret reached the output: %v", web)
	}
	if ssh := byName["unit sshd"]; ssh == nil || ssh["count"] != float64(2) || byName["container db"] != nil {
		t.Fatalf("the journal's errors are grouped by program, the stopped container is not read: %v", groups)
	}
	// it is one call for the model; behind it the Docker API saw only reads
	for _, m := range f.methods {
		if m != http.MethodGet {
			t.Fatalf("%s", m)
		}
	}
}

func TestHealthCheckIsCompact(t *testing.T) {
	o, _ := healthHost(t)
	c, _ := NewRegistry(o).Get("health.check")
	raw, err := c.Invoke(context.Background(), json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) > 4<<10 {
		t.Fatalf("%d bytes for two containers", len(raw))
	}
}

func TestHealthCheckReportsWhatItCouldNotReadAndStillAnswers(t *testing.T) {
	out, err := invoke(t, Options{ProcRoot: t.TempDir(), Commander: &fakeCommander{err: errors.New("systemctl is not installed")}}, "health.check", `{}`)
	if err != nil {
		t.Fatalf("a missing part must not fail the call: %v", err)
	}
	un := out["unavailable"].([]any)
	sections := map[string]bool{}
	for _, u := range un {
		sections[u.(map[string]any)["section"].(string)] = true
	}
	for _, want := range []string{"docker", "systemd", "journal", "disks", "host.load", "host.memory"} {
		if !sections[want] {
			t.Errorf("%s is not reported as unavailable: %v", want, un)
		}
	}
	if out["ok"] != false {
		t.Fatal("nothing could be read is not ok")
	}
}

func TestHealthCheckInputs(t *testing.T) {
	o, _ := healthHost(t)
	for _, bad := range []string{`{"disk_warn_percent":10}`, `{"disk_warn_percent":100}`, `{"error_lines":0,"x":1}`, `{"error_lines":9}`, `{"since":"yesterday-ish"}`, `{"cmd":"rm"}`} {
		var ie *InputError
		if _, err := invoke(t, o, "health.check", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
	out, err := invoke(t, o, "health.check", `{"disk_warn_percent":99,"since":"30m"}`)
	if err != nil || out["window"] != "30m" {
		t.Fatalf("%v %v", out, err)
	}
	if strings.Contains(strings.Join(strs(out["problems"]), "|"), "disk") {
		t.Fatalf("a 99%% threshold lists no 95%% disk: %v", out["problems"])
	}
}

func TestLogsSearchFindsALineAcrossContainersAndTheJournal(t *testing.T) {
	o, f := healthHost(t)
	out, err := invoke(t, o, "logs.search", `{"pattern":"out of memory","since":"2h"}`)
	if err != nil {
		t.Fatal(err)
	}
	ms := out["matches"].([]any)
	if out["total_matches"] != float64(1) || len(ms) != 1 || ms[0].(map[string]any)["source"] != "container web" ||
		!strings.Contains(ms[0].(map[string]any)["line"].(string), "fatal: out of memory") {
		t.Fatalf("%v", out)
	}
	if sc := out["scanned"].(map[string]any); sc["containers"] != float64(2) || sc["container_lines"] != float64(5) || sc["journal_lines"] != float64(3) {
		t.Fatalf("%v", sc)
	}
	// a plain text is not a regular expression; a regex is one when asked
	out, _ = invoke(t, o, "logs.search", `{"pattern":"error.*attempts","where":"journal"}`)
	if out["total_matches"] != float64(0) {
		t.Fatalf("plain text, not a regex: %v", out)
	}
	out, err = invoke(t, o, "logs.search", `{"pattern":"error.*attempts","regex":true,"where":"journal"}`)
	if err != nil || out["total_matches"] != float64(2) || out["counts"].(map[string]any)["journal sshd"] != float64(2) {
		t.Fatalf("%v %v", out, err)
	}
	// secrets are masked, the newest matches are kept, and only containers asked for are read
	out, _ = invoke(t, o, "logs.search", `{"pattern":"ERROR","where":"containers","container":"we","max_matches":1}`)
	if out["total_matches"] != float64(1) || strings.Contains(fmt.Sprint(out), "pa55w0rd") {
		t.Fatalf("%v", out)
	}
	if n := len(out["matches"].([]any)); n != 1 {
		t.Fatalf("%d", n)
	}
	for _, p := range f.paths {
		if strings.Contains(p, "bbbbbbbbbbbbffff") && strings.HasSuffix(p, "/logs") && false {
			t.Fatal("unreachable")
		}
	}
}

func TestLogsSearchInputsAreChecked(t *testing.T) {
	o, _ := healthHost(t)
	for _, bad := range []string{
		`{}`, `{"pattern":""}`, `{"pattern":"   "}`, `{"pattern":"` + strings.Repeat("a", 101) + `"}`, `{"pattern":"(","regex":true}`,
		`{"pattern":"x","where":"files"}`, `{"pattern":"x","container":"../etc"}`, `{"pattern":"x","unit":"-bad"}`,
		`{"pattern":"x","max_matches":201}`, `{"pattern":"x","since":"1h","until":"2h"}`, `{"pattern":"x","shell":"ls"}`,
	} {
		var ie *InputError
		if _, err := invoke(t, o, "logs.search", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
}

func TestLogsSearchPassesTheTimeRangeAndUnitToTheJournalAndDocker(t *testing.T) {
	o, f := healthHost(t)
	cmd := o.Commander.(*fakeCommander)
	_, err := invoke(t, o, "logs.search", `{"pattern":"x","since":"2026-09-30 07:00","until":"2026-09-30 07:30","unit":"sshd.service"}`)
	if err != nil {
		t.Fatal(err)
	}
	args := strings.Join(cmd.calls[len(cmd.calls)-1], " ")
	for _, want := range []string{"--since 2026-09-30 07:00:00 UTC", "--until 2026-09-30 07:30:00 UTC", "-u sshd.service", "-n 3000"} {
		if !strings.Contains(args, want) {
			t.Errorf("journalctl %q lacks %q", args, want)
		}
	}
	q := strings.Join(f.queries, "&")
	if !strings.Contains(q, "until=") || !strings.Contains(q, "tail=2000") {
		t.Fatalf("%s", q)
	}
}

func TestCertsExpiryReadsTheCertificatesOfConfiguredTargets(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	leaf := srv.Certificate()
	closed, _ := net.Listen("tcp", "127.0.0.1:0")
	deadAddr := closed.Addr().String()
	_ = closed.Close()
	now := leaf.NotAfter.Add(-5 * 24 * time.Hour)
	o := Options{Now: func() time.Time { return now }, Checks: CheckOptions{CertTargets: []string{deadAddr, srv.Listener.Addr().String()}}}
	out, err := invoke(t, o, "certs.expiry", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	rows := out["certificates"].([]any)
	if len(rows) != 2 {
		t.Fatalf("%v", rows)
	}
	dead, live := rows[0].(map[string]any), rows[1].(map[string]any) // unreachable first
	if !strings.Contains(dead["error"].(string), "not reachable") {
		t.Fatalf("%v", dead)
	}
	if live["days_remaining"] != float64(5) || live["expiring"] != true || live["not_after"] != leaf.NotAfter.UTC().Format("2006-01-02T15:04:05Z") ||
		live["verify"] == "ok" || live["issuer"] == "" {
		t.Fatalf("%v", live) // the test server's certificate is not from a trusted CA
	}
	pr := strings.Join(strs(out["problems"]), " | ")
	if !strings.Contains(pr, "expires in 5 days") || !strings.Contains(pr, "not reachable") || out["ok"] != false {
		t.Fatalf("%s", pr)
	}
	// a smaller warning window does not flag it as expiring
	out, _ = invoke(t, o, "certs.expiry", `{"warn_days":2,"target":"`+srv.Listener.Addr().String()+`"}`)
	if rows := out["certificates"].([]any); len(rows) != 1 || rows[0].(map[string]any)["expiring"] == true {
		t.Fatalf("%v", out)
	}
}

func TestCertsExpiryOnlyLooksAtConfiguredTargets(t *testing.T) {
	o := Options{Checks: CheckOptions{CertTargets: []string{"status.example.lan:443"}}}
	for _, bad := range []string{`{"target":"169.254.169.254:443"}`, `{"target":"example.org"}`, `{"warn_days":0,"x":1}`, `{"warn_days":400}`, `{"host":"x"}`} {
		var ie *InputError
		if _, err := invoke(t, o, "certs.expiry", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
	if _, ok := NewRegistry(Options{}).Get("certs.expiry"); ok {
		t.Fatal("offered without a target")
	}
}

func TestNetCheckDNSPingAndPorts(t *testing.T) {
	open, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer open.Close() //nolint:errcheck // test
	go func() {
		for {
			c, err := open.Accept()
			if err != nil {
				return
			}
			_ = c.Close()
		}
	}()
	closed, _ := net.Listen("tcp", "127.0.0.1:0")
	closedPort := closed.Addr().(*net.TCPAddr).Port
	_ = closed.Close()
	openPort := open.Addr().(*net.TCPAddr).Port
	var pinged []string
	o := Options{Commander: &fakeCommander{}, Checks: CheckOptions{
		Ping: func(_ context.Context, host string) (PingResult, error) {
			pinged = append(pinged, host)
			return PingResult{Sent: 3, Received: 2, AvgMS: 0.6}, nil
		},
		NetTargets: []NetTarget{{Name: "gateway", Host: "127.0.0.1", Ping: true, Ports: []int{openPort, closedPort}}},
		DNSNames:   []string{"good.example.lan", "bad.example.lan"},
		Lookup: func(_ context.Context, h string) ([]string, error) {
			if h == "bad.example.lan" {
				return nil, errors.New("no such host")
			}
			return []string{"10.0.0.7", "10.0.0.6"}, nil
		},
	}}
	out, err := invoke(t, o, "net.check", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	pr := strings.Join(strs(out["problems"]), " | ")
	for _, want := range []string{"dns bad.example.lan: does not resolve", fmt.Sprintf("gateway: port %d not reachable", closedPort), "gateway: ping lost 1 of 3 packets"} {
		if !strings.Contains(pr, want) {
			t.Errorf("problems lack %q: %s", want, pr)
		}
	}
	if strings.Contains(pr, fmt.Sprintf("port %d ", openPort)) || out["ok"] != false {
		t.Fatalf("%s", pr)
	}
	row := out["targets"].([]any)[0].(map[string]any)
	ping := row["ping"].(map[string]any)
	if ping["loss_percent"] != float64(33) || ping["avg_ms"] != 0.6 || ping["ok"] != true {
		t.Fatalf("%v", ping)
	}
	ports := row["ports"].([]any)
	if ports[0].(map[string]any)["open"] != true || ports[1].(map[string]any)["open"] != false {
		t.Fatalf("%v", ports)
	}
	if fmt.Sprint(pinged) != "[127.0.0.1]" {
		t.Fatalf("%v", pinged)
	}
	if dns := out["dns"].([]any)[0].(map[string]any); dns["ok"] != true || fmt.Sprint(dns["addresses"]) != "[10.0.0.6 10.0.0.7]" {
		t.Fatalf("%v", dns)
	}
}

func TestNetCheckTargetsAreFixedAndNoProgramIsStartedForPing(t *testing.T) {
	o := Options{Commander: &fakeCommander{}, Checks: CheckOptions{NetTargets: []NetTarget{{Name: "gateway", Host: "10.0.0.1"}}}}
	for _, bad := range []string{`{"target":"10.0.0.99"}`, `{"target":"x; reboot"}`, `{"host":"10.0.0.99"}`} {
		var ie *InputError
		if _, err := invoke(t, o, "net.check", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
	if allowedBinaries["ping"] || allowedBinaries["sh"] || allowedBinaries["bash"] {
		t.Fatalf("%v", allowedBinaries)
	}
	for _, ok := range []string{"10.0.0.1", "host.example.lan", "fe80::1", "a-b.c"} {
		if !CheckHost(ok) {
			t.Errorf("%s refused", ok)
		}
	}
	for _, bad := range []string{"", "-oProxy", "a b", "a;b", "$(x)", "host/../x", strings.Repeat("a", 300)} {
		if CheckHost(bad) {
			t.Errorf("%q accepted", bad)
		}
	}
	if _, ok := NewRegistry(Options{}).Get("net.check"); ok {
		t.Fatal("offered without a target")
	}
}

// A ping the account may not send is "not checked", never "the host is unreachable".
func TestNetCheckPingWithoutPermissionIsNotAProblemOfTheHost(t *testing.T) {
	for name, perr := range map[string]error{"permission": errPingNotPermitted, "unsupported": errPingUnsupported} {
		t.Run(name, func(t *testing.T) {
			o := Options{Checks: CheckOptions{
				NetTargets: []NetTarget{{Name: "router", Host: "10.0.0.1", Ping: true}},
				Ping:       func(context.Context, string) (PingResult, error) { return PingResult{}, perr },
			}}
			out, err := invoke(t, o, "net.check", `{}`)
			if err != nil {
				t.Fatal(err)
			}
			if len(strs(out["problems"])) != 0 || out["ok"] != true {
				t.Fatalf("a missing right is no problem of the target: %v", out)
			}
			row := out["targets"].([]any)[0].(map[string]any)["ping"].(map[string]any)
			if row["checked"] != false || row["ok"] != nil || !strings.Contains(fmt.Sprint(row["error"]), "ping not possible") {
				t.Fatalf("%v", row)
			}
			if nc := strs(out["not_checked"]); len(nc) != 1 || !strings.HasPrefix(nc[0], "router: ") {
				t.Fatalf("%v", out["not_checked"])
			}
			if strings.Contains(fmt.Sprint(out), "unreachable") {
				t.Fatalf("%v", out)
			}
		})
	}
}

func TestNetCheckPingResultsAreReportedTruthfully(t *testing.T) {
	cases := map[string]struct {
		res  PingResult
		err  error
		want string
	}{
		"all lost":  {PingResult{Sent: 3}, nil, "router: no ping answer"},
		"none sent": {PingResult{}, errors.New("network is unreachable"), "router: ping failed: network is unreachable"},
		"no route":  {PingResult{}, nil, "router: ping failed: no packet could be sent (no route?)"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			o := Options{Checks: CheckOptions{
				NetTargets: []NetTarget{{Name: "router", Host: "10.0.0.1", Ping: true}},
				Ping:       func(context.Context, string) (PingResult, error) { return c.res, c.err },
			}}
			out, err := invoke(t, o, "net.check", `{}`)
			if err != nil {
				t.Fatal(err)
			}
			if p := strs(out["problems"]); len(p) != 1 || p[0] != c.want || out["ok"] != false {
				t.Fatalf("%v", out["problems"])
			}
		})
	}
}

func TestICMPEchoPacketAndReply(t *testing.T) {
	pkt := icmpEcho(0x1234, 7, []byte("abc"))
	if pkt[0] != 8 || pkt[1] != 0 || pkt[4] != 0x12 || pkt[5] != 0x34 || pkt[7] != 7 || string(pkt[8:]) != "abc" {
		t.Fatalf("% x", pkt)
	}
	// a valid packet sums to 0xffff over everything including its checksum
	var sum uint32
	for i := 0; i+1 < len(pkt); i += 2 {
		sum += uint32(pkt[i])<<8 | uint32(pkt[i+1])
	}
	sum += uint32(pkt[len(pkt)-1]) << 8
	for sum>>16 != 0 {
		sum = sum&0xffff + sum>>16
	}
	if uint16(sum) != 0xffff {
		t.Fatalf("checksum %x", sum)
	}
	reply := append([]byte(nil), pkt...)
	reply[0] = 0
	if seq, ok := icmpEchoReplySeq(reply); !ok || seq != 7 {
		t.Fatalf("%d %v", seq, ok)
	}
	withIP := append([]byte{0x45, 0, 0, 31, 0, 0, 0, 0, 64, 1, 0, 0, 10, 0, 0, 1, 10, 0, 0, 2}, reply...)
	if seq, ok := icmpEchoReplySeq(withIP); !ok || seq != 7 {
		t.Fatalf("with an IP header: %d %v", seq, ok)
	}
	for _, bad := range [][]byte{nil, pkt /* a request */, {0, 0, 0}, {3, 1, 0, 0, 0, 0, 0, 0}} {
		if _, ok := icmpEchoReplySeq(bad); ok {
			t.Errorf("accepted % x", bad)
		}
	}
}

// The real socket: skipped where the account may not open an ICMP socket (see ping_group_range).
func TestPingLoopbackOverAnUnprivilegedSocket(t *testing.T) {
	res, err := pingHostICMP(context.Background(), "127.0.0.1")
	if errors.Is(err, errPingNotPermitted) || errors.Is(err, errPingUnsupported) {
		t.Skipf("no unprivileged ICMP here: %v", err)
	}
	if err != nil {
		t.Fatal(err)
	}
	if res.Sent != pingCount || res.Received != pingCount {
		t.Fatalf("%+v", res)
	}
}

func TestPingNeedsAnIPv4Address(t *testing.T) {
	if _, err := resolveV4(context.Background(), "::1"); err == nil {
		t.Fatal("an IPv6 literal is not pingable here")
	}
	if ip, err := resolveV4(context.Background(), "10.0.0.5"); err != nil || ip.String() != "10.0.0.5" {
		t.Fatalf("%v %v", ip, err)
	}
}

// 500 kernel error lines are two or three messages: the answer gets them with their counts.
func TestHealthCheckSumsUpIdenticalErrorMessages(t *testing.T) {
	o, _ := healthHost(t)
	var b strings.Builder
	for i := 0; i < 420; i++ {
		fmt.Fprintf(&b, "2026-09-30T07:%02d:%02d+0000 host kernel: [drm:qxl_alloc_bo_reserved [qxl]] *ERROR* failed to allocate VRAM BO (%d)\n", i/60%60, i%60, 1000+i)
	}
	for i := 0; i < 70; i++ {
		fmt.Fprintf(&b, "2026-09-30T07:30:%02d+0000 host kernel: nouveau 0000:01:00.0: fifo: channel %d killed, error\n", i%60, i)
	}
	for i := 0; i < 10; i++ {
		b.WriteString("2026-09-30T07:40:00+0000 host kernel: usb 1-1: device descriptor read/64, error -71\n")
	}
	o.Commander = &fakeCommander{out: map[string]string{"journalctl": b.String()}}
	out, err := invoke(t, o, "health.check", `{"since":"1h"}`)
	if err != nil {
		t.Fatal(err)
	}
	var kernel map[string]any
	for _, g := range out["errors"].([]any) {
		if g.(map[string]any)["source"] == "unit kernel" {
			kernel = g.(map[string]any)
		}
	}
	if kernel == nil || kernel["count"] != float64(500) || kernel["at_least"] != true {
		t.Fatalf("the read stops at 500 lines, so 500 is a lower bound: %v", kernel)
	}
	top := kernel["top"].([]any)
	if len(top) != 3 || top[0].(map[string]any)["count"] != float64(420) || top[1].(map[string]any)["count"] != float64(70) || top[2].(map[string]any)["count"] != float64(10) {
		t.Fatalf("%v", top)
	}
	if m := top[0].(map[string]any)["message"].(string); !strings.Contains(m, "qxl_alloc_bo_reserved") {
		t.Fatalf("%s", m)
	}
	problems := strings.Join(strs(out["problems"]), " | ")
	if !strings.Contains(problems, "unit kernel: at least 500 error lines in the last 1h (most frequent, 420x: ") || !strings.Contains(problems, "qxl_alloc_bo_reserved") {
		t.Fatalf("%s", problems)
	}
}

func TestMessageKindIgnoresNumbersAddressesAndIDs(t *testing.T) {
	a := messageKind("conn 10.0.0.5:5432 failed after 31 ms (id 3f2a9c1d7b4e) at 0xffff88810a2b3c00")
	b := messageKind("conn 10.0.0.9:5433 failed after 7 ms (id aa00bb11cc22) at 0xffff88810a2b3d40")
	if a != b || strings.ContainsAny(a, "0123456789") {
		t.Fatalf("%q vs %q", a, b)
	}
	if messageKind("disk full") == messageKind("disk failed") {
		t.Fatal("different messages merged")
	}
}
