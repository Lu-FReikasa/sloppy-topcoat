package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"

	"topcoat/node/internal/providers/capability"
)

// Uptime Kuma exposes its monitors as Prometheus metrics on /metrics, authenticated with an API key
// given as the password of HTTP basic auth (any user name) — or open when Kuma runs without
// authentication (then api_key is left out of the connectors file). Metric names and the status codes are
// from Uptime Kuma's server/prometheus.js and wiki; verified against the source, not against a
// running instance (UNVERIFIED on real hardware).
const (
	kumaStatus   = "monitor_status"              // 1 up, 0 down, 2 pending, 3 maintenance
	kumaResponse = "monitor_response_time"       // milliseconds of the last check
	kumaCertDays = "monitor_cert_days_remaining" // days until the certificate expires
	kumaCertOK   = "monitor_cert_is_valid"       // 1 valid, 0 invalid
)

var kumaStatusName = map[int]string{0: "down", 1: "up", 2: "pending", 3: "maintenance"}

type kumaMonitor struct {
	ID       string
	Name     string
	Type     string
	URL      string
	Host     string
	Port     string
	Status   string
	Response *float64
	CertDays *float64
	CertOK   *bool
}

func kumaMetrics(ctx context.Context, cfg *KumaConfig) ([]kumaMonitor, error) {
	client, err := cfg.client()
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(cfg.URL, "/")+"/metrics", nil)
	if err != nil {
		return nil, err
	}
	if cfg.APIKey != "" { // without a key (Kuma without its own login) no header is sent at all
		req.SetBasicAuth("", cfg.APIKey)
	}
	body, code, err := fetch(client, req, "Uptime Kuma")
	if err != nil {
		return nil, err
	}
	switch {
	case (code == http.StatusUnauthorized || code == http.StatusForbidden) && cfg.APIKey == "":
		return nil, fmt.Errorf("Uptime Kuma wants a login on /metrics (HTTP %d): set uptime_kuma.api_key in the connectors file, or release /metrics for this node in front of Kuma if Kuma runs without authentication", code) //nolint:staticcheck // product name
	case code == http.StatusUnauthorized || code == http.StatusForbidden:
		return nil, fmt.Errorf("Uptime Kuma refused the API key (HTTP %d): is the key valid, not expired and not deleted?", code) //nolint:staticcheck // product name
	case code != http.StatusOK:
		return nil, fmt.Errorf("Uptime Kuma answered HTTP %d on /metrics", code) //nolint:staticcheck // product name
	}
	byKey := map[string]*kumaMonitor{}
	var order []string
	for _, s := range parseProm(string(body)) {
		if s.Name != kumaStatus && s.Name != kumaResponse && s.Name != kumaCertDays && s.Name != kumaCertOK {
			continue
		}
		key := s.Labels["monitor_id"] + "\x00" + s.Labels["monitor_name"]
		m := byKey[key]
		if m == nil {
			m = &kumaMonitor{
				ID: s.Labels["monitor_id"], Name: s.Labels["monitor_name"], Type: s.Labels["monitor_type"],
				URL: cleanMonitorURL(s.Labels["monitor_url"]), Host: s.Labels["monitor_hostname"], Port: s.Labels["monitor_port"],
				Status: "unknown",
			}
			if m.URL == "null" {
				m.URL = ""
			}
			byKey[key] = m
			order = append(order, key)
		}
		v := s.Value
		switch s.Name {
		case kumaStatus:
			if name, ok := kumaStatusName[int(v)]; ok {
				m.Status = name
			}
		case kumaResponse:
			if v >= 0 {
				m.Response = &v
			}
		case kumaCertDays:
			m.CertDays = &v
		case kumaCertOK:
			ok := v == 1
			m.CertOK = &ok
		}
	}
	out := make([]kumaMonitor, 0, len(order))
	for _, k := range order {
		out = append(out, *byKey[k])
	}
	return out, nil
}

func kumaMonitors(o Options) capability.Capability {
	return fn{
		name: "kuma.monitors",
		desc: "Uptime Kuma: every monitor with its status (up, down, pending, maintenance), the response time of the last check and the days until its TLS certificate expires. Filter by status, part of the name or certificates that expire soon; problems come first.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"status":{"type":"string","enum":["all","down","up","pending","maintenance"],"description":"only monitors in this state (default all)"},` +
			`"name_contains":{"type":"string","maxLength":64,"description":"part of the monitor name, case-insensitive"},` +
			`"cert_days_below":{"type":"integer","minimum":1,"maximum":3650,"description":"only monitors whose certificate expires in fewer days"},` +
			`"limit":{"type":"integer","minimum":1,"maximum":200,"description":"at most this many monitors (default 100)"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Status       string `json:"status"`
				NameContains string `json:"name_contains"`
				CertDays     *int   `json:"cert_days_below"`
				Limit        int    `json:"limit"`
			}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.Status == "" {
				in.Status = "all"
			}
			switch in.Status {
			case "all", "down", "up", "pending", "maintenance":
			default:
				return nil, inputErr("status must be all, down, up, pending or maintenance")
			}
			if in.Limit == 0 {
				in.Limit = 100
			}
			if in.Limit < 1 || in.Limit > 200 {
				return nil, inputErr("limit must be 1..200")
			}
			if in.CertDays != nil && (*in.CertDays < 1 || *in.CertDays > 3650) {
				return nil, inputErr("cert_days_below must be 1..3650")
			}
			if o.Connectors == nil || o.Connectors.Kuma == nil {
				return nil, errNoConnector("uptime_kuma")
			}
			all, err := kumaMetrics(ctx, o.Connectors.Kuma)
			if err != nil {
				return nil, err
			}
			counts := map[string]int{}
			for _, m := range all {
				counts[m.Status]++
			}
			needle := strings.ToLower(in.NameContains)
			var shown []kumaMonitor
			for _, m := range all {
				if in.Status != "all" && m.Status != in.Status {
					continue
				}
				if needle != "" && !strings.Contains(strings.ToLower(m.Name), needle) {
					continue
				}
				if in.CertDays != nil && (m.CertDays == nil || *m.CertDays >= float64(*in.CertDays)) {
					continue
				}
				shown = append(shown, m)
			}
			rank := map[string]int{"down": 0, "pending": 1, "unknown": 2, "maintenance": 3, "up": 4}
			sort.SliceStable(shown, func(i, j int) bool {
				if rank[shown[i].Status] != rank[shown[j].Status] {
					return rank[shown[i].Status] < rank[shown[j].Status]
				}
				return strings.ToLower(shown[i].Name) < strings.ToLower(shown[j].Name)
			})
			truncated := len(shown) > in.Limit
			if truncated {
				shown = shown[:in.Limit]
			}
			list := make([]any, 0, len(shown))
			for _, m := range shown {
				e := map[string]any{"name": m.Name, "type": m.Type, "status": m.Status}
				if m.ID != "" {
					e["id"] = m.ID
				}
				if m.URL != "" {
					e["url"] = m.URL
				}
				if m.Host != "" && m.Host != "null" {
					e["hostname"] = m.Host
				}
				if m.Port != "" && m.Port != "null" {
					e["port"] = m.Port
				}
				if m.Response != nil {
					e["response_ms"] = *m.Response
				}
				if m.CertDays != nil {
					e["cert_days_remaining"] = *m.CertDays
				}
				if m.CertOK != nil {
					e["cert_valid"] = *m.CertOK
				}
				list = append(list, e)
			}
			out, masked := finishOutput(map[string]any{
				"monitors": list, "total": len(all), "matching": len(shown), "truncated": truncated,
				"counts": counts, "problems": counts["down"] + counts["pending"],
			}, o.Connectors.secrets())
			out.(map[string]any)["masked"] = masked
			return out, nil
		},
	}
}
