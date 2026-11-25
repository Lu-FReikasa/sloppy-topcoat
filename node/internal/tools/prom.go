package tools

import (
	"strconv"
	"strings"
)

// promSample is one line of the Prometheus text format: name{labels} value.
type promSample struct {
	Name   string
	Labels map[string]string
	Value  float64
}

// parseProm reads the Prometheus text exposition format (comments and malformed lines skipped).
// Only what Uptime Kuma's /metrics needs: quoted label values with \\ \" \n escapes, a float or
// NaN/Inf value, an optional timestamp that is ignored.
func parseProm(text string) []promSample {
	var out []promSample
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || line[0] == '#' {
			continue
		}
		name, rest := line, ""
		labels := map[string]string{}
		if i := strings.IndexAny(line, "{ \t"); i >= 0 {
			name, rest = line[:i], line[i:]
		}
		rest = strings.TrimLeft(rest, " \t")
		if strings.HasPrefix(rest, "{") {
			var ok bool
			labels, rest, ok = parsePromLabels(rest)
			if !ok {
				continue
			}
		}
		fields := strings.Fields(rest)
		if name == "" || len(fields) == 0 {
			continue
		}
		v, err := strconv.ParseFloat(fields[0], 64)
		if err != nil {
			continue
		}
		out = append(out, promSample{Name: name, Labels: labels, Value: v})
	}
	return out
}

// parsePromLabels parses `{a="b",c="d"} rest` and returns the labels and what follows the brace.
func parsePromLabels(s string) (map[string]string, string, bool) {
	labels := map[string]string{}
	i := 1
	for i < len(s) {
		for i < len(s) && (s[i] == ' ' || s[i] == ',') {
			i++
		}
		if i < len(s) && s[i] == '}' {
			return labels, s[i+1:], true
		}
		eq := strings.IndexByte(s[i:], '=')
		if eq < 0 || i+eq+1 >= len(s) || s[i+eq+1] != '"' {
			return nil, "", false
		}
		key := strings.TrimSpace(s[i : i+eq])
		i += eq + 2
		var b strings.Builder
		closed := false
		for i < len(s) {
			c := s[i]
			if c == '\\' && i+1 < len(s) {
				switch s[i+1] {
				case 'n':
					b.WriteByte('\n')
				default:
					b.WriteByte(s[i+1])
				}
				i += 2
				continue
			}
			if c == '"' {
				closed = true
				i++
				break
			}
			b.WriteByte(c)
			i++
		}
		if !closed {
			return nil, "", false
		}
		labels[key] = b.String()
	}
	return nil, "", false
}
