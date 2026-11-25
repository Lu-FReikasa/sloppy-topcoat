package tools

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
)

// Collecting capabilities (E3-04): one call instead of six. They run fixed procedures over what the
// other capabilities read — never a shell, never an argument that picks a program or a host: the
// hosts net.check and certs.expiry look at are configured on the node (tools.net_targets,
// tools.dns_names, tools.cert_targets), so a model cannot turn them into a port scanner.

// NetTarget is a host net.check looks at.
type NetTarget struct {
	Name  string
	Host  string
	Ports []int
	Ping  bool
}

// CheckOptions are the node-configured targets of net.check and certs.expiry, plus the hooks the
// tests replace (the real ones are the network).
type CheckOptions struct {
	NetTargets  []NetTarget
	DNSNames    []string
	CertTargets []string // host or host:port (default port 443)
	// Lookup resolves a name (default: the system resolver).
	Lookup func(ctx context.Context, host string) ([]string, error)
	// Ping sends the echo requests of net.check (default: an unprivileged ICMP datagram socket).
	Ping func(ctx context.Context, host string) (PingResult, error)
}

const (
	sampleChars    = 200
	healthWindow   = "1h"
	logScanLines   = 2000 // lines read per container
	journalScan    = 3000 // journal lines read
	searchMax      = 200
	searchDefault  = 50
	certDialTime   = 5 * time.Second
	connectTimeout = 3 * time.Second
)

// errorLine is what counts as an error line in a container's log.
var errorLine = regexp.MustCompile(`(?i)\b(error|err|fatal|panic|exception|critical|crit|traceback|segfault|out of memory|oom|failed|failure)\b`)

func oneSample(s string) string {
	s = strings.TrimSpace(s)
	if r := []rune(s); len(r) > sampleChars {
		s = string(r[:sampleChars-1]) + "…"
	}
	return s
}

// logLines are the lines of one container's log within a window, or the reason there are none.
func containerLogLines(ctx context.Context, d DockerAPI, c containerInfo, since, until time.Time, hasUntil bool, tail int) ([]string, error) {
	q := url.Values{"stdout": {"1"}, "stderr": {"1"}, "timestamps": {"1"}, "tail": {strconv.Itoa(tail)}, "since": {strconv.FormatInt(since.Unix(), 10)}}
	if hasUntil {
		q.Set("until", strconv.FormatInt(until.Unix(), 10))
	}
	body, _, err := d.Get(ctx, "/containers/"+c.Name+"/logs", q)
	if err != nil {
		return nil, err
	}
	tty := false
	if ins, err := inspectContainer(ctx, d, c.Name); err == nil {
		tty = ins.Config.Tty
	}
	text := strings.ReplaceAll(demuxLogs(body, tty), "\r\n", "\n")
	return strings.Split(strings.TrimRight(text, "\n"), "\n"), nil
}

type errorGroup struct {
	Source string `json:"source"`
	Count  int    `json:"count"`
	// AtLeast is set when the lines were read with a limit that was reached: the true count is
	// higher (the journal read stops at journalErrorLines lines).
	AtLeast bool `json:"at_least,omitempty"`
	// Top are the most frequent distinct messages (numbers, addresses and ids ignored): 500 lines
	// are usually 2-3 messages, and the model should name the cause instead of guessing.
	Top  []errorKind `json:"top,omitempty"`
	Last []string    `json:"last"`
}

// errorKind is one distinct error message of a group, with how often it occurred.
type errorKind struct {
	Count   int    `json:"count"`
	Message string `json:"message"` // the latest line of this kind
}

const (
	topKinds          = 3
	journalErrorLines = 500
)

var (
	varHex    = regexp.MustCompile(`(?i)\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b`)
	varNumber = regexp.MustCompile(`\d+`)
	varSpace  = regexp.MustCompile(`\s+`)
)

// messageKind reduces a line to what makes messages "the same": no timestamps, counters, addresses.
func messageKind(l string) string {
	l = varHex.ReplaceAllString(l, "#")
	l = varNumber.ReplaceAllString(l, "#")
	return strings.ToLower(strings.TrimSpace(varSpace.ReplaceAllString(l, " ")))
}

// tally counts distinct messages of one group.
type tally struct {
	order  []string
	counts map[string]int
	latest map[string]string
}

func (t *tally) add(line string) {
	if t.counts == nil {
		t.counts, t.latest = map[string]int{}, map[string]string{}
	}
	k := messageKind(line)
	if _, ok := t.counts[k]; !ok {
		t.order = append(t.order, k)
	}
	t.counts[k]++
	t.latest[k] = line
}

// top returns the most frequent kinds, most frequent first (ties: first seen first).
func (t *tally) top() []errorKind {
	keys := append([]string(nil), t.order...)
	sort.SliceStable(keys, func(i, j int) bool { return t.counts[keys[i]] > t.counts[keys[j]] })
	if len(keys) > topKinds {
		keys = keys[:topKinds]
	}
	out := make([]errorKind, 0, len(keys))
	for _, k := range keys {
		out = append(out, errorKind{Count: t.counts[k], Message: oneSample(t.latest[k])})
	}
	return out
}

// --- health.check --------------------------------------------------------------------------

func healthCheck(o Options) capability.Capability {
	return fn{
		name: "health.check",
		desc: "One call for the general state of this machine: failed systemd units, containers that are unhealthy, restarting, killed for memory or exited with an error, file systems over a threshold, RAM, swap and load, and the error lines of the last hour per container and per unit (counts and the last few lines). Compact; start here for \"is everything ok\" before looking at single containers or units. A part that cannot be read is listed under unavailable, not an error.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"since":{"type":"string","maxLength":40,"description":"how far back the error lines go (default 1h): a duration like 30m, 2h, 1d"},` +
			`"disk_warn_percent":{"type":"integer","minimum":50,"maximum":99,"description":"file systems fuller than this are listed (default 80)"},` +
			`"error_lines":{"type":"integer","minimum":1,"maximum":5,"description":"latest error lines kept per container or unit (default 2); the three most frequent distinct messages with their counts are always given"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Since     string `json:"since"`
				DiskWarn  int    `json:"disk_warn_percent"`
				ErrorLine int    `json:"error_lines"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.DiskWarn == 0 {
				in.DiskWarn = 80
			}
			if in.DiskWarn < 50 || in.DiskWarn > 99 {
				return nil, inputErr("disk_warn_percent must be 50..99")
			}
			if in.ErrorLine == 0 {
				in.ErrorLine = 2
			}
			if in.ErrorLine < 1 || in.ErrorLine > 5 {
				return nil, inputErr("error_lines must be 1..5")
			}
			if in.Since == "" {
				in.Since = healthWindow
			}
			now := o.now()
			since, ok, err := parseWhen(in.Since, now)
			if err != nil {
				return nil, err
			}
			if !ok {
				since = now.Add(-time.Hour)
			}
			res := map[string]any{"window": in.Since}
			problems := []string{}
			var unavailable []map[string]string
			skip := func(section string, err error) {
				unavailable = append(unavailable, map[string]string{"section": section, "error": oneSample(err.Error())})
			}

			// host: load, memory, swap
			host := map[string]any{}
			if t, err := readText(o.proc() + "/loadavg"); err == nil {
				if f := strings.Fields(t); len(f) >= 3 {
					l1, _ := strconv.ParseFloat(f[0], 64)
					l5, _ := strconv.ParseFloat(f[1], 64)
					cpus := runtime.NumCPU()
					host["load"] = map[string]any{"1m": l1, "5m": l5, "cpus": cpus}
					if l1 > 1.5*float64(cpus) {
						problems = append(problems, fmt.Sprintf("load: %.1f on %d CPUs", l1, cpus))
					}
				}
			} else {
				skip("host.load", err)
			}
			if t, err := readText(o.proc() + "/meminfo"); err == nil {
				m := meminfoMB(t)
				total, avail := m["MemTotal"], m["MemAvailable"]
				if total > 0 {
					host["memory_mb"] = map[string]int64{"total": total, "available": avail}
					if pct := avail * 100 / total; pct < 10 {
						problems = append(problems, fmt.Sprintf("RAM: only %d%% (%d MB) available", pct, avail))
					}
				}
				if sw := m["SwapTotal"]; sw > 0 {
					used := sw - m["SwapFree"]
					host["swap_mb"] = map[string]int64{"total": sw, "used": used}
					if used*100/sw > 50 {
						problems = append(problems, fmt.Sprintf("swap: %d%% used (%d of %d MB)", used*100/sw, used, sw))
					}
				}
			} else {
				skip("host.memory", err)
			}
			res["host"] = host

			// file systems over the threshold (and the fullest one, so the model sees the margin)
			if rows, err := o.filesystems(); err == nil {
				var full []map[string]any
				for i, r := range rows {
					if r.UsedPct >= float64(in.DiskWarn) {
						problems = append(problems, fmt.Sprintf("disk %s: %.1f%% used", r.Mount, r.UsedPct))
					}
					if r.UsedPct >= float64(in.DiskWarn) || i == 0 {
						full = append(full, map[string]any{"mount": r.Mount, "used_percent": r.UsedPct, "avail_mb": r.AvailMB})
					}
				}
				res["disks"] = full
			} else {
				skip("disks", err)
			}

			// systemd
			if units, err := failedUnits(ctx, o); err == nil {
				res["failed_units"] = units
				for _, u := range units {
					problems = append(problems, "unit failed: "+u["unit"])
				}
			} else {
				skip("systemd", err)
			}

			// containers and their error lines
			var groups []errorGroup
			if d, err := o.docker(); err != nil {
				skip("docker", err)
			} else if cs, total, truncated, err := collectContainers(ctx, d, true, ""); err != nil {
				skip("docker", err)
			} else {
				counts := map[string]int{}
				var attention []containerBrief
				for _, c := range cs {
					counts[c.State]++
					var why []string
					switch {
					case c.OOMKilled:
						why = append(why, "killed for memory")
					case c.State == "restarting":
						why = append(why, "restarting")
					case c.State == "dead":
						why = append(why, "dead")
					case c.ExitCode != nil && *c.ExitCode != 0:
						why = append(why, fmt.Sprintf("exited with %d", *c.ExitCode))
					}
					if c.Health == "unhealthy" {
						why = append(why, "unhealthy")
					}
					if c.RestartCount >= 3 && c.State == "running" {
						why = append(why, fmt.Sprintf("restarted %d times", c.RestartCount))
					}
					if len(why) > 0 {
						problems = append(problems, fmt.Sprintf("container %s: %s", c.Name, strings.Join(why, ", ")))
						attention = append(attention, c.brief())
					}
				}
				res["containers"] = map[string]any{"total": total, "truncated": truncated, "counts": counts, "attention": attention}
				groups = append(groups, containerErrors(ctx, d, cs, since, in.ErrorLine)...)
			}
			if gs, err := journalErrors(ctx, o, since, in.ErrorLine); err == nil {
				groups = append(groups, gs...)
			} else {
				skip("journal", err)
			}
			sort.SliceStable(groups, func(i, j int) bool { return groups[i].Count > groups[j].Count })
			if len(groups) > 15 {
				groups = groups[:15]
			}
			res["errors"] = groups
			for _, g := range groups {
				if g.Count >= 5 {
					problems = append(problems, errorProblem(g, in.Since))
				}
			}
			res["problems"] = problems
			res["ok"] = len(problems) == 0 && len(unavailable) == 0
			if len(unavailable) > 0 {
				res["unavailable"] = unavailable
			}
			v, masked := finishOutput(res, nil)
			if m, ok := v.(map[string]any); ok {
				m["masked"] = masked
			}
			return v, nil
		},
	}
}

// errorProblem is the problem line of a group: the count and, when there are several, the most
// frequent message, so the answer names what the lines say instead of guessing.
func errorProblem(g errorGroup, since string) string {
	n := fmt.Sprint(g.Count)
	if g.AtLeast {
		n = "at least " + n
	}
	line := fmt.Sprintf("%s: %s error lines in the last %s", g.Source, n, since)
	if len(g.Top) > 0 {
		line += fmt.Sprintf(" (most frequent, %dx: %s)", g.Top[0].Count, oneSample(g.Top[0].Message))
	}
	return line
}

// containerErrors counts the error lines of every running container in the window.
func containerErrors(ctx context.Context, d DockerAPI, cs []containerInfo, since time.Time, keep int) []errorGroup {
	var running []containerInfo
	for _, c := range cs {
		if c.State == "running" || c.State == "restarting" {
			running = append(running, c)
		}
	}
	if len(running) > 40 {
		running = running[:40]
	}
	out := make([]errorGroup, len(running))
	parallel(len(running), 6, func(i int) {
		g := errorGroup{Source: "container " + running[i].Name}
		var tl tally
		lines, err := containerLogLines(ctx, d, running[i], since, time.Time{}, false, 400)
		if err == nil {
			for _, l := range lines {
				if errorLine.MatchString(l) {
					g.Count++
					tl.add(l)
					g.Last = append(g.Last, oneSample(l))
					if len(g.Last) > keep {
						g.Last = g.Last[1:]
					}
				}
			}
			g.Top = tl.top()
		}
		out[i] = g
	})
	var groups []errorGroup
	for _, g := range out {
		if g.Count > 0 {
			groups = append(groups, g)
		}
	}
	return groups
}

// journalErrors groups the journal's error-priority lines of the window by the program or unit
// that wrote them.
func journalErrors(ctx context.Context, o Options, since time.Time, keep int) ([]errorGroup, error) {
	args := []string{"--no-pager", "-q", "-o", "short-iso", "-p", "err", "-n", strconv.Itoa(journalErrorLines), "--since", since.UTC().Format("2006-01-02 15:04:05 UTC")}
	out, err := o.Commander.Run(ctx, "journalctl", args...)
	if err != nil {
		return nil, err
	}
	byTag := map[string]*errorGroup{}
	tallies := map[string]*tally{}
	var order []string
	lineCount := 0
	for _, l := range strings.Split(string(out), "\n") {
		f := strings.Fields(l)
		if len(f) < 4 || strings.HasPrefix(l, "-- ") {
			continue
		}
		tag := strings.TrimSuffix(f[2], ":")
		if i := strings.IndexByte(tag, '['); i > 0 {
			tag = tag[:i]
		}
		g := byTag[tag]
		if g == nil {
			g = &errorGroup{Source: "unit " + tag}
			byTag[tag] = g
			tallies[tag] = &tally{}
			order = append(order, tag)
		}
		lineCount++
		g.Count++
		msg := strings.Join(f[3:], " ")
		tallies[tag].add(msg)
		g.Last = append(g.Last, oneSample(msg))
		if len(g.Last) > keep {
			g.Last = g.Last[1:]
		}
	}
	groups := make([]errorGroup, 0, len(order))
	for _, t := range order {
		g := *byTag[t]
		g.Top = tallies[t].top()
		g.AtLeast = lineCount >= journalErrorLines
		groups = append(groups, g)
	}
	return groups, nil
}

// --- logs.search ---------------------------------------------------------------------------

func logsSearch(o Options) capability.Capability {
	return fn{
		name: "logs.search",
		desc: "Search a text (or a regular expression) in the logs of all containers and in the journal in one call, within a time range: matching lines with their source, newest last, and counts per source. Bounded: at most the last 2000 lines per container and 3000 journal lines are read (the answer says how many were scanned). Secrets are masked.",
		schema: `{"type":"object","additionalProperties":false,"required":["pattern"],"properties":{` +
			`"pattern":{"type":"string","minLength":1,"maxLength":100,"description":"the text to find (case-insensitive), or a regular expression with regex=true"},` +
			`"regex":{"type":"boolean","description":"read pattern as a regular expression (RE2 syntax; default false: plain text)"},` +
			`"since":{"type":"string","maxLength":40,"description":"how far back (default 1h): a duration (30m, 2h, 3d), today, yesterday, a date or an RFC 3339 time"},` +
			`"until":{"type":"string","maxLength":40,"description":"as since; end of the range (default now)"},` +
			`"where":{"type":"string","enum":["all","containers","journal"],"description":"what to search (default all)"},` +
			`"container":{"type":"string","maxLength":128,"description":"only containers whose name contains this text"},` +
			`"unit":{"type":"string","maxLength":256,"description":"only this systemd unit in the journal"},` +
			`"max_matches":{"type":"integer","minimum":1,"maximum":200,"description":"at most this many matching lines (default 50; the newest are kept)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Pattern    string `json:"pattern"`
				Regex      bool   `json:"regex"`
				Since      string `json:"since"`
				Until      string `json:"until"`
				Where      string `json:"where"`
				Container  string `json:"container"`
				Unit       string `json:"unit"`
				MaxMatches int    `json:"max_matches"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if strings.TrimSpace(in.Pattern) == "" || len(in.Pattern) > 100 {
				return nil, inputErr("pattern must hold 1 to 100 characters")
			}
			expr := in.Pattern
			if !in.Regex {
				expr = regexp.QuoteMeta(in.Pattern)
			}
			re, err := regexp.Compile("(?i)" + expr)
			if err != nil {
				return nil, inputErr("pattern is not a valid regular expression: %v", err)
			}
			if in.Where == "" {
				in.Where = "all"
			}
			if in.Where != "all" && in.Where != "containers" && in.Where != "journal" {
				return nil, inputErr("where must be all, containers or journal")
			}
			if in.Container != "" && !containerRef.MatchString(in.Container) {
				return nil, inputErr("container must be (part of) a container name")
			}
			if in.Unit != "" {
				if err := checkUnit(in.Unit); err != nil {
					return nil, err
				}
			}
			if in.MaxMatches == 0 {
				in.MaxMatches = searchDefault
			}
			if in.MaxMatches < 1 || in.MaxMatches > searchMax {
				return nil, inputErr("max_matches must be 1..%d", searchMax)
			}
			if in.Since == "" {
				in.Since = healthWindow
			}
			now := o.now()
			since, hasSince, err := parseWhen(in.Since, now)
			if err != nil {
				return nil, err
			}
			if !hasSince {
				since = now.Add(-time.Hour)
			}
			until, hasUntil, err := parseWhen(in.Until, now)
			if err != nil {
				return nil, err
			}
			if hasUntil && !until.After(since) {
				return nil, inputErr("until must be after since")
			}

			type match struct {
				Source string `json:"source"`
				Line   string `json:"line"`
			}
			var matches []match
			counts := map[string]int{}
			scanned := map[string]int{"containers": 0, "container_lines": 0, "journal_lines": 0}
			var unavailable []map[string]string
			var mu sync.Mutex
			if in.Where != "journal" {
				if d, err := o.docker(); err != nil {
					unavailable = append(unavailable, map[string]string{"section": "docker", "error": oneSample(err.Error())})
				} else if cs, _, _, err := collectContainers(ctx, d, true, in.Container); err != nil {
					unavailable = append(unavailable, map[string]string{"section": "docker", "error": oneSample(err.Error())})
				} else {
					parallel(len(cs), 6, func(i int) {
						lines, err := containerLogLines(ctx, d, cs[i], since, until, hasUntil, logScanLines)
						if err != nil {
							return
						}
						mu.Lock()
						defer mu.Unlock()
						scanned["containers"]++
						scanned["container_lines"] += len(lines)
						for _, l := range lines {
							if re.MatchString(l) {
								counts["container "+cs[i].Name]++
								matches = append(matches, match{"container " + cs[i].Name, l})
							}
						}
					})
				}
			}
			if in.Where != "containers" {
				args := []string{"--no-pager", "-q", "-o", "short-iso", "-n", strconv.Itoa(journalScan), "--since", since.UTC().Format("2006-01-02 15:04:05 UTC")}
				if hasUntil {
					args = append(args, "--until", until.UTC().Format("2006-01-02 15:04:05 UTC"))
				}
				if in.Unit != "" {
					args = append(args, "-u", in.Unit)
				}
				if out, err := o.Commander.Run(ctx, "journalctl", args...); err != nil {
					unavailable = append(unavailable, map[string]string{"section": "journal", "error": oneSample(err.Error())})
				} else {
					for _, l := range strings.Split(strings.TrimRight(string(out), "\n"), "\n") {
						if l == "" || strings.HasPrefix(l, "-- ") {
							continue
						}
						scanned["journal_lines"]++
						if re.MatchString(l) {
							f := strings.Fields(l)
							src := "journal"
							if len(f) >= 3 {
								tag := strings.TrimSuffix(f[2], ":")
								if i := strings.IndexByte(tag, '['); i > 0 {
									tag = tag[:i]
								}
								src = "journal " + tag
							}
							counts[src]++
							matches = append(matches, match{src, l})
						}
					}
				}
			}
			// newest last: the lines start with a time stamp; keep the newest max_matches
			sort.SliceStable(matches, func(i, j int) bool { return matches[i].Line < matches[j].Line })
			total := len(matches)
			truncated := false
			if total > in.MaxMatches {
				matches, truncated = matches[total-in.MaxMatches:], true
			}
			for i := range matches {
				matches[i].Line = oneSample(matches[i].Line)
			}
			res := map[string]any{
				"pattern": in.Pattern, "regex": in.Regex, "since": in.Since, "total_matches": total, "truncated": truncated,
				"matches": matches, "counts": counts, "scanned": scanned,
			}
			if len(unavailable) > 0 {
				res["unavailable"] = unavailable
			}
			v, masked := finishOutput(res, nil)
			if m, ok := v.(map[string]any); ok {
				m["masked"] = masked
			}
			return v, nil
		},
	}
}

// --- certs.expiry --------------------------------------------------------------------------

var hostRe = regexp.MustCompile(`^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$`)

// CheckHost reports whether s is a host name or IP address a target may name.
func CheckHost(s string) bool {
	if net.ParseIP(s) != nil {
		return true
	}
	return len(s) <= 253 && hostRe.MatchString(s)
}

func splitTarget(t string, defPort string) (host, port string, err error) {
	host, port = t, defPort
	if h, p, e := net.SplitHostPort(t); e == nil {
		host, port = h, p
	}
	n, e := strconv.Atoi(port)
	if !CheckHost(host) || e != nil || n < 1 || n > 65535 {
		return "", "", fmt.Errorf("target %q is not host or host:port", t)
	}
	return host, port, nil
}

func certsExpiry(o Options) capability.Capability {
	return fn{
		name: "certs.expiry",
		desc: "TLS certificates of the services this node is configured to check (proxy hosts and services, tools.cert_targets): subject, issuer, expiry date, days remaining and whether the chain is valid for the name, soonest expiry first. Targets cannot be chosen by the caller.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"warn_days":{"type":"integer","minimum":1,"maximum":365,"description":"certificates that expire within this many days are flagged (default 21)"},` +
			`"target":{"type":"string","maxLength":260,"description":"only this configured target (host or host:port)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				WarnDays int    `json:"warn_days"`
				Target   string `json:"target"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.WarnDays == 0 {
				in.WarnDays = 21
			}
			if in.WarnDays < 1 || in.WarnDays > 365 {
				return nil, inputErr("warn_days must be 1..365")
			}
			targets := o.Checks.CertTargets
			if in.Target != "" {
				found := false
				for _, t := range targets {
					if t == in.Target {
						found = true
					}
				}
				if !found {
					return nil, inputErr("target %q is not one this node checks", in.Target)
				}
				targets = []string{in.Target}
			}
			type certRow struct {
				Target    string   `json:"target"`
				Subject   string   `json:"subject,omitempty"`
				Issuer    string   `json:"issuer,omitempty"`
				NotAfter  string   `json:"not_after,omitempty"`
				Days      *int     `json:"days_remaining,omitempty"`
				Verify    string   `json:"verify,omitempty"`
				Names     []string `json:"names,omitempty"`
				Expiring  bool     `json:"expiring,omitempty"`
				Error     string   `json:"error,omitempty"`
				sortValue time.Time
			}
			rows := make([]certRow, len(targets))
			now := o.now()
			parallel(len(targets), 6, func(i int) {
				row := certRow{Target: targets[i]}
				host, port, err := splitTarget(targets[i], "443")
				if err != nil {
					row.Error = err.Error()
					rows[i] = row
					return
				}
				d := &tls.Dialer{NetDialer: &net.Dialer{Timeout: certDialTime}, Config: &tls.Config{
					ServerName: host, InsecureSkipVerify: true, MinVersion: tls.VersionTLS10, //nolint:gosec // reads the certificate even when it is invalid; the chain is verified below
				}}
				dctx, cancel := context.WithTimeout(ctx, certDialTime)
				defer cancel()
				conn, err := d.DialContext(dctx, "tcp", net.JoinHostPort(host, port))
				if err != nil {
					row.Error = "not reachable or no TLS: " + oneSample(errorText(err))
					rows[i] = row
					return
				}
				defer conn.Close() //nolint:errcheck // read-only
				certs := conn.(*tls.Conn).ConnectionState().PeerCertificates
				if len(certs) == 0 {
					row.Error = "the service presented no certificate"
					rows[i] = row
					return
				}
				leaf := certs[0]
				inter := x509.NewCertPool()
				for _, c := range certs[1:] {
					inter.AddCert(c)
				}
				row.Subject, row.Issuer = leaf.Subject.String(), leaf.Issuer.String()
				row.NotAfter = leaf.NotAfter.UTC().Format("2006-01-02T15:04:05Z")
				row.sortValue = leaf.NotAfter
				days := int(leaf.NotAfter.Sub(now).Hours() / 24)
				row.Days = &days
				row.Expiring = days <= in.WarnDays
				row.Names = leaf.DNSNames
				if len(row.Names) > 5 {
					row.Names = row.Names[:5]
				}
				vopts := x509.VerifyOptions{Intermediates: inter, CurrentTime: now}
				if net.ParseIP(host) == nil {
					vopts.DNSName = host
				}
				if _, err := leaf.Verify(vopts); err != nil {
					row.Verify = oneSample(err.Error())
				} else {
					row.Verify = "ok"
				}
				rows[i] = row
			})
			sort.SliceStable(rows, func(i, j int) bool {
				if (rows[i].Days == nil) != (rows[j].Days == nil) {
					return rows[i].Days == nil // unreachable first: that is a problem too
				}
				return rows[i].sortValue.Before(rows[j].sortValue)
			})
			problems := []string{}
			for _, r := range rows {
				switch {
				case r.Error != "":
					problems = append(problems, r.Target+": "+r.Error)
				case *r.Days < 0:
					problems = append(problems, fmt.Sprintf("%s: certificate expired %d days ago", r.Target, -*r.Days))
				case r.Expiring:
					problems = append(problems, fmt.Sprintf("%s: expires in %d days", r.Target, *r.Days))
				case r.Verify != "ok":
					problems = append(problems, fmt.Sprintf("%s: chain not valid (%s)", r.Target, r.Verify))
				}
			}
			v, _ := finishOutput(map[string]any{"certificates": rows, "warn_days": in.WarnDays, "problems": problems, "ok": len(problems) == 0}, nil)
			return v, nil
		},
	}
}

func errorText(err error) string {
	var ue *url.Error
	if errors.As(err, &ue) {
		err = ue.Err
	}
	return err.Error()
}

// --- net.check -----------------------------------------------------------------------------

func netCheck(o Options) capability.Capability {
	return fn{
		name: "net.check",
		desc: "Reachability from this node of the hosts it is configured to watch (tools.net_targets, tools.dns_names): DNS lookups, ping (3 echo requests, unprivileged ICMP; a ping the account may not send is reported as not checked, not as an unreachable host) and TCP connects to the configured ports, with times; problems are listed first. Targets cannot be chosen by the caller.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"target":{"type":"string","maxLength":64,"description":"only the target with this name (default: all)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Target string `json:"target"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			targets := o.Checks.NetTargets
			dns := o.Checks.DNSNames
			if in.Target != "" {
				var keep []NetTarget
				for _, t := range targets {
					if t.Name == in.Target {
						keep = append(keep, t)
					}
				}
				if len(keep) == 0 {
					return nil, inputErr("target %q is not one this node checks", in.Target)
				}
				targets, dns = keep, nil
			}
			lookup := o.Checks.Lookup
			if lookup == nil {
				lookup = func(ctx context.Context, host string) ([]string, error) {
					return net.DefaultResolver.LookupHost(ctx, host)
				}
			}
			problems := []string{}
			var mu sync.Mutex
			problem := func(s string) { mu.Lock(); problems = append(problems, s); mu.Unlock() }
			var notes []string // things that were not checked: no verdict on the target
			note := func(s string) { mu.Lock(); notes = append(notes, s); mu.Unlock() }

			dnsRows := make([]map[string]any, len(dns))
			parallel(len(dns), 6, func(i int) {
				start := time.Now()
				addrs, err := lookup(ctx, dns[i])
				row := map[string]any{"name": dns[i], "ms": time.Since(start).Milliseconds()}
				if err != nil || len(addrs) == 0 {
					row["ok"], row["error"] = false, "does not resolve"
					problem("dns " + dns[i] + ": does not resolve")
				} else {
					sort.Strings(addrs)
					if len(addrs) > 4 {
						addrs = addrs[:4]
					}
					row["ok"], row["addresses"] = true, addrs
				}
				dnsRows[i] = row
			})

			rows := make([]map[string]any, len(targets))
			parallel(len(targets), 4, func(i int) {
				t := targets[i]
				row := map[string]any{"name": t.Name, "host": t.Host}
				if t.Ping {
					row["ping"] = pingHost(ctx, o, t.Host,
						func(s string) { problem(fmt.Sprintf("%s: %s", t.Name, s)) },
						func(s string) { note(fmt.Sprintf("%s: %s", t.Name, s)) })
				}
				var ports []map[string]any
				for _, p := range t.Ports {
					start := time.Now()
					dctx, cancel := context.WithTimeout(ctx, connectTimeout)
					conn, err := (&net.Dialer{}).DialContext(dctx, "tcp", net.JoinHostPort(t.Host, strconv.Itoa(p)))
					cancel()
					pr := map[string]any{"port": p, "ms": time.Since(start).Milliseconds()}
					if err != nil {
						pr["open"], pr["error"] = false, "closed or filtered"
						problem(fmt.Sprintf("%s: port %d not reachable", t.Name, p))
					} else {
						pr["open"] = true
						_ = conn.Close()
					}
					ports = append(ports, pr)
				}
				if len(ports) > 0 {
					row["ports"] = ports
				}
				rows[i] = row
			})
			sort.Strings(problems)
			sort.Strings(notes)
			out := map[string]any{"targets": rows, "dns": dnsRows, "problems": problems, "ok": len(problems) == 0}
			if len(notes) > 0 {
				out["not_checked"] = notes
			}
			v, _ := finishOutput(out, nil)
			return v, nil
		},
	}
}

// pingHost pings one host and says in the row what was or was not possible. A ping that could not
// be *sent* (no right to open an ICMP socket) is not a problem of the target: it goes to notes, not
// problems, and the row says it was not checked.
func pingHost(ctx context.Context, o Options, host string, problem, note func(string)) map[string]any {
	ping := o.Checks.Ping
	if ping == nil {
		ping = pingHostICMP
	}
	row := map[string]any{}
	res, err := ping(ctx, host)
	switch {
	case errors.Is(err, errPingNotPermitted), errors.Is(err, errPingUnsupported):
		row["checked"], row["ok"] = false, nil
		row["error"] = "ping not possible (permissions): this node's account may not send ICMP - it says nothing about the host; allow it with net.ipv4.ping_group_range or rely on the port checks"
		if errors.Is(err, errPingUnsupported) {
			row["error"] = "ping not possible on this system - it says nothing about the host; rely on the port checks"
		}
		note(host + ": " + row["error"].(string))
		return row
	case err != nil && res.Sent == 0:
		row["ok"], row["error"] = false, oneSample(err.Error())
		problem("ping failed: " + row["error"].(string))
		return row
	}
	row["sent"], row["received"] = res.Sent, res.Received
	if res.Sent > 0 {
		row["loss_percent"] = (res.Sent - res.Received) * 100 / res.Sent
	}
	if res.Received > 0 {
		row["avg_ms"] = res.AvgMS
	}
	row["ok"] = res.Received > 0
	switch {
	case res.Sent == 0:
		row["ok"], row["error"] = false, "no packet could be sent (no route?)"
		problem("ping failed: no packet could be sent (no route?)")
	case res.Received == 0:
		problem("no ping answer")
	case res.Received < res.Sent:
		problem(fmt.Sprintf("ping lost %d of %d packets", res.Sent-res.Received, res.Sent))
	}
	return row
}
