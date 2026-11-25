package tools

import (
	"encoding/json"
	"fmt"
	"net/url"
	"reflect"
	"regexp"
	"strings"
)

// finishOutput is the last step of every connector capability: no value of the connectors file
// can be in the output (the credentials are replaced wherever they turn up, in any string), and
// everything else passes the same masking as logs and files. Returns the cleaned value and the
// number of replacements.
func finishOutput(v any, secrets []string) (any, int) {
	n := 0
	var walk func(any) any
	walk = func(x any) any {
		switch t := x.(type) {
		case string:
			for _, s := range secrets {
				if len(s) >= 4 && strings.Contains(t, s) {
					t = strings.ReplaceAll(t, s, marker("connector_secret"))
					n++
				}
			}
			masked, c := Mask(t)
			n += c
			return masked
		case []any:
			for i := range t {
				t[i] = walk(t[i])
			}
			return t
		case []map[string]any:
			for i := range t {
				for k := range t[i] {
					t[i][k] = walk(t[i][k])
				}
			}
			return t
		case map[string]any:
			for k := range t {
				t[k] = walk(t[k])
			}
			return t
		default:
			// structs and typed slices: through JSON, so every string in them is reached
			if k := reflect.ValueOf(x).Kind(); k == reflect.Slice || k == reflect.Struct || k == reflect.Map {
				if b, err := json.Marshal(x); err == nil {
					var generic any
					if json.Unmarshal(b, &generic) == nil {
						return walk(generic)
					}
				}
			}
			return x
		}
	}
	return walk(v), n
}

var pushPath = regexp.MustCompile(`(/api/push/)[^/?#]+`)

// cleanMonitorURL is what of a monitored URL may be shown: no user name or password, no query
// (tokens live there), no push token (Uptime Kuma's push monitors carry theirs in the path).
func cleanMonitorURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return pushPath.ReplaceAllString(strings.SplitN(raw, "?", 2)[0], "${1}"+marker("push_token"))
	}
	u.User, u.RawQuery, u.Fragment = nil, "", ""
	return pushPath.ReplaceAllString(u.String(), "${1}"+marker("push_token"))
}

func errNoConnector(section string) error {
	return fmt.Errorf("this node has no %q section in its connectors file", section)
}
