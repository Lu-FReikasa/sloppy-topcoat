package tools

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// InputError is an input a capability refuses (wrong type, unknown field, out of range). The agent
// reports it as invalid_args.
type InputError struct{ Msg string }

func (e *InputError) Error() string { return e.Msg }

func inputErr(format string, a ...any) error { return &InputError{Msg: fmt.Sprintf(format, a...)} }

// decodeInput reads the input object strictly: unknown fields are an error, so a call cannot
// smuggle in an argument a capability does not know about.
func decodeInput(raw json.RawMessage, dst any) error {
	if len(bytes.TrimSpace(raw)) == 0 || string(bytes.TrimSpace(raw)) == "null" {
		raw = json.RawMessage(`{}`)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return inputErr("invalid input: %v", err)
	}
	if dec.More() {
		return inputErr("invalid input: trailing data")
	}
	return nil
}

var (
	containerRef = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$`)
	unitName     = regexp.MustCompile(`^[A-Za-z0-9:_.@\\\-]{1,256}$`)
	projectName  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$`)
)

func checkContainer(ref string) error {
	if !containerRef.MatchString(ref) {
		return inputErr("container must be a container name or ID (letters, digits, _ . -)")
	}
	return nil
}

func checkUnit(u string) error {
	if !unitName.MatchString(u) || strings.HasPrefix(u, "-") {
		return inputErr("unit must be a systemd unit name such as nginx.service")
	}
	return nil
}

const whenHelp = `use a duration like 15m, 2h, 3d ("1 hour ago" is fine), "today", "yesterday", a date such as 2026-09-30 or 2026-09-30 07:30 (UTC unless it names a zone) or an RFC 3339 timestamp`

var (
	agoRe = regexp.MustCompile(`(?i)^(an?|\d+(?:\.\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?)(?:\s+ago)?$`)
	// time stamps models write: zone-less ones are UTC (the node's clock reading is UTC too)
	stampLayouts = []string{
		"2006-01-02 15:04:05", "2006-01-02T15:04:05", "2006-01-02 15:04", "2006-01-02T15:04", "2006-01-02",
		"2006-01-02 15:04:05Z07:00", "2006-01-02 15:04:05Z0700", "2006-01-02T15:04:05Z0700", "2006-01-02 15:04Z07:00",
	}
)

// parseWhen reads a point in time. Accepted: a duration in the past ("15m", "2h", "1h30m", "3d"),
// the same in words ("1 hour ago", "30 minutes ago", "a day ago"), "now", "today" and "yesterday"
// (midnight UTC), a date or date and time ("2026-09-30", "2026-09-30 07:30[:00]", UTC unless a
// zone is named) and an RFC 3339 timestamp. The empty string is "not given".
func parseWhen(s string, now time.Time) (time.Time, bool, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false, nil
	}
	bad := func() (time.Time, bool, error) { return time.Time{}, false, inputErr("time %q: %s", s, whenHelp) }
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t, true, nil
	}
	low := strings.ToLower(s)
	midnight := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	switch low {
	case "now":
		return now, true, nil
	case "today":
		return midnight, true, nil
	case "yesterday":
		return midnight.AddDate(0, 0, -1), true, nil
	}
	for _, layout := range stampLayouts {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true, nil
		}
	}
	if m := agoRe.FindStringSubmatch(low); m != nil {
		n := 1.0
		if m[1] != "a" && m[1] != "an" {
			n, _ = strconv.ParseFloat(m[1], 64)
		}
		unit := time.Second
		switch m[2][0] {
		case 'm':
			unit = time.Minute
		case 'h':
			unit = time.Hour
		case 'd':
			unit = 24 * time.Hour
		case 'w':
			unit = 7 * 24 * time.Hour
		}
		d := time.Duration(n * float64(unit))
		if d > 3650*24*time.Hour {
			return bad()
		}
		return now.Add(-d), true, nil
	}
	if days, ok := strings.CutSuffix(low, "d"); ok {
		n, err := strconv.ParseFloat(days, 64)
		if err != nil || n < 0 || n > 3650 {
			return bad()
		}
		return now.Add(-time.Duration(n * float64(24*time.Hour))), true, nil
	}
	dur, err := time.ParseDuration(low)
	if err != nil || dur < 0 {
		return bad()
	}
	return now.Add(-dur), true, nil
}

func clampLines(n int) (int, error) {
	switch {
	case n == 0:
		return DefaultLogLines, nil
	case n < 0 || n > MaxLogLines:
		return 0, inputErr("lines must be 1..%d", MaxLogLines)
	}
	return n, nil
}
