package tools

import (
	"errors"
	"testing"
)

func TestParseWhen(t *testing.T) {
	now := mustTime("2026-09-30T08:00:00Z")
	for in, want := range map[string]string{
		"15m":                       "2026-09-30T07:45:00Z",
		"2h":                        "2026-09-30T06:00:00Z",
		"3d":                        "2026-09-27T08:00:00Z",
		"1.5d":                      "2026-09-28T20:00:00Z",
		"2026-09-29T10:00:00Z":      "2026-09-29T10:00:00Z",
		"1h30m":                     "2026-09-30T06:30:00Z",
		"1 hour ago":                "2026-09-30T07:00:00Z",
		"2 Hours Ago":               "2026-09-30T06:00:00Z",
		"30 min ago":                "2026-09-30T07:30:00Z",
		"an hour ago":               "2026-09-30T07:00:00Z",
		"a day ago":                 "2026-09-29T08:00:00Z",
		"2 days ago":                "2026-09-28T08:00:00Z",
		"1 week ago":                "2026-09-23T08:00:00Z",
		"45 seconds ago":            "2026-09-30T07:59:15Z",
		"now":                       "2026-09-30T08:00:00Z",
		"today":                     "2026-09-30T00:00:00Z",
		"yesterday":                 "2026-09-29T00:00:00Z",
		"2026-09-29":                "2026-09-29T00:00:00Z",
		"2026-09-29 07:30":          "2026-09-29T07:30:00Z",
		"2026-09-29 07:30:15":       "2026-09-29T07:30:15Z",
		"2026-09-29T07:30:15":       "2026-09-29T07:30:15Z",
		"2026-09-29 07:30:00+02:00": "2026-09-29T05:30:00Z",
		"2026-09-29 07:30:00+0200":  "2026-09-29T05:30:00Z",
	} {
		got, ok, err := parseWhen(in, now)
		if err != nil || !ok || !got.Equal(mustTime(want)) {
			t.Errorf("%q: %v %v %v", in, got, ok, err)
		}
	}
	if _, ok, err := parseWhen("  ", now); ok || err != nil {
		t.Error("blank is 'not given'")
	}
	for _, bad := range []string{"-5m", "xd", "99999d", "5", "1h30", "last tuesday", "5 parsecs ago", "2026-13-40"} {
		var ie *InputError
		if _, _, err := parseWhen(bad, now); !errors.As(err, &ie) {
			t.Errorf("%q accepted (%v)", bad, err)
		}
	}
}
