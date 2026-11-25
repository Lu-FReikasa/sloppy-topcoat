package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
)

// The Omada controller's Open API (controller 5.12 and newer): an application with client
// credentials (role Viewer, so even a bug here cannot change the network) gets an access token
//
//	POST {url}/openapi/authorize/token?grant_type=client_credentials
//	     {"omadacId": …, "client_id": …, "client_secret": …}   ->  result.accessToken, expiresIn
//
// sent as `Authorization: AccessToken=<token>`. Answers are {"errorCode":0,"msg":…,"result":…};
// lists are result.data with totalRows. The paths below are the ones the public integrations use
// (sites, devices, clients, site audit logs, events under logs/events, alerts under logs/alerts,
// latest firmware of a device). The logs need filters.timeStart/timeEnd (ms) or the controller
// answers HTTP 400. Checked against a real controller on 2026-11-17: sites, devices, clients,
// logs/events, logs/alerts and audit-logs with a time window (firmware UNVERIFIED on real hardware).

const (
	omadaTokenExpired1 = -44112
	omadaTokenExpired2 = -44113
)

type omadaClient struct {
	cfg  *OmadaConfig
	http func() (*http.Client, error)

	mu      sync.Mutex
	token   string
	expires time.Time
	sites   []omadaSite
}

type omadaSite struct{ ID, Name string }

func newOmadaClient(cfg *OmadaConfig) *omadaClient {
	return &omadaClient{cfg: cfg, http: cfg.client}
}

func (c *omadaClient) base() string { return strings.TrimRight(c.cfg.URL, "/") }

type omadaEnvelope struct {
	ErrorCode int             `json:"errorCode"`
	Msg       string          `json:"msg"`
	Result    json.RawMessage `json:"result"`
}

func (c *omadaClient) authorize(ctx context.Context) error {
	client, err := c.http()
	if err != nil {
		return err
	}
	body, _ := json.Marshal(map[string]string{
		"omadacId": c.cfg.OmadaID, "client_id": c.cfg.ClientID, "client_secret": c.cfg.ClientSecret,
	})
	u := c.base() + "/openapi/authorize/token?grant_type=client_credentials"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	raw, code, err := fetch(client, req, "the Omada controller")
	if err != nil {
		return err
	}
	var env omadaEnvelope
	if json.Unmarshal(raw, &env) != nil || code != http.StatusOK {
		return fmt.Errorf("the Omada controller answered HTTP %d to the token request", code)
	}
	if env.ErrorCode != 0 {
		return fmt.Errorf("the Omada controller refused the client credentials (error %d: %s)", env.ErrorCode, cut(env.Msg, 120))
	}
	var res struct {
		AccessToken string `json:"accessToken"`
		ExpiresIn   int    `json:"expiresIn"`
	}
	if json.Unmarshal(env.Result, &res) != nil || res.AccessToken == "" {
		return fmt.Errorf("the Omada controller's token answer has no accessToken")
	}
	c.token = res.AccessToken
	c.expires = time.Now().Add(time.Duration(res.ExpiresIn)*time.Second - 30*time.Second)
	return nil
}

func cut(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

// get is a GET of an /openapi/v1/{omadacId}/… path; it logs in when there is no valid token and
// once more if the controller says the token is gone.
func (c *omadaClient) get(ctx context.Context, path string, q url.Values) (json.RawMessage, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for attempt := 0; attempt < 2; attempt++ {
		if c.token == "" || time.Now().After(c.expires) {
			if err := c.authorize(ctx); err != nil {
				return nil, err
			}
		}
		client, err := c.http()
		if err != nil {
			return nil, err
		}
		u := c.base() + "/openapi/v1/" + url.PathEscape(c.cfg.OmadaID) + path
		if len(q) > 0 {
			u += "?" + q.Encode()
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "AccessToken="+c.token)
		raw, code, err := fetch(client, req, "the Omada controller")
		if err != nil {
			return nil, err
		}
		var env omadaEnvelope
		if json.Unmarshal(raw, &env) != nil {
			return nil, fmt.Errorf("the Omada controller answered HTTP %d with something that is not JSON", code)
		}
		if env.ErrorCode == omadaTokenExpired1 || env.ErrorCode == omadaTokenExpired2 {
			c.token = "" // log in again, once
			continue
		}
		if env.ErrorCode != 0 {
			return nil, fmt.Errorf("the Omada controller answered error %d: %s", env.ErrorCode, cut(env.Msg, 160))
		}
		if code != http.StatusOK {
			return nil, omadaHTTPError(code, raw)
		}
		return env.Result, nil
	}
	return nil, fmt.Errorf("the Omada controller keeps refusing the access token")
}

// omadaHTTPError says what the controller said. Its web framework answers a request it cannot take
// with {"status":400,"error":"Bad Request","message":…} and no errorCode; "HTTP 400" alone left a
// model guessing other site names (live 2026-11-17, the logs were asked without a time window).
func omadaHTTPError(code int, raw []byte) error {
	var body struct {
		Error   string `json:"error"`
		Message string `json:"message"`
		Msg     string `json:"msg"`
	}
	_ = json.Unmarshal(raw, &body)
	what := strings.TrimSpace(body.Error)
	for _, m := range []string{body.Message, body.Msg} {
		if m = strings.TrimSpace(m); m != "" && m != what {
			what = strings.TrimSpace(what + ": " + m)
		}
	}
	msg := fmt.Sprintf("the Omada controller answered HTTP %d", code)
	if what != "" {
		msg += " (" + cut(what, 160) + ")"
	}
	switch code {
	case http.StatusBadRequest:
		msg += "; the request lacks a parameter the controller needs or has a wrong one: this is a fault of the tool, not of the site name"
	case http.StatusNotFound:
		msg += "; this controller version does not know the path"
	}
	return fmt.Errorf("%s", msg)
}

// list splits a result into its items and the total: {"data":[…],"totalRows":n} or a plain list.
func omadaList(result json.RawMessage) ([]map[string]any, int) {
	var page struct {
		Data      []map[string]any `json:"data"`
		TotalRows int              `json:"totalRows"`
	}
	if json.Unmarshal(result, &page) == nil && page.Data != nil {
		if page.TotalRows == 0 {
			page.TotalRows = len(page.Data)
		}
		return page.Data, page.TotalRows
	}
	var plain []map[string]any
	if json.Unmarshal(result, &plain) == nil {
		return plain, len(plain)
	}
	return nil, 0
}

// resolveSite picks the site by name or id from the controller's own list, so a path never holds
// anything the controller did not name itself. No name: the configured one, else the only one.
func (c *omadaClient) resolveSite(ctx context.Context, asked string) (omadaSite, error) {
	c.mu.Lock()
	cached := c.sites
	c.mu.Unlock()
	sites := cached
	if sites == nil {
		res, err := c.get(ctx, "/sites", url.Values{"page": {"1"}, "pageSize": {"100"}})
		if err != nil {
			return omadaSite{}, err
		}
		items, _ := omadaList(res)
		for _, it := range items {
			id, _ := it["siteId"].(string)
			name, _ := it["name"].(string)
			if id != "" {
				sites = append(sites, omadaSite{ID: id, Name: name})
			}
		}
		c.mu.Lock()
		c.sites = sites
		c.mu.Unlock()
	}
	want := strings.TrimSpace(asked)
	if want == "" {
		want = strings.TrimSpace(c.cfg.Site)
	}
	if want == "" {
		if len(sites) == 1 {
			return sites[0], nil
		}
		return omadaSite{}, inputErr("the controller has %d sites; name one of: %s", len(sites), siteNames(sites))
	}
	for _, s := range sites {
		if strings.EqualFold(s.Name, want) || s.ID == want {
			return s, nil
		}
	}
	return omadaSite{}, inputErr("no site %q on the controller; sites: %s", want, siteNames(sites))
}

func siteNames(sites []omadaSite) string {
	names := make([]string, 0, len(sites))
	for _, s := range sites {
		names = append(names, s.Name)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

// Fields whose names say they are secrets are dropped from what a controller lists (a Wi-Fi
// passphrase, a key), whatever an answer of the controller contains.
var omadaSecretKey = regexp.MustCompile(`(?i)pass|secret|token|psk|credential|private|auth|cookie|key$`)

// An event's "key" is its kind (L_C_DISCONN), not a secret: kept when it looks like one.
var omadaEventKey = regexp.MustCompile(`^[A-Z][A-Z0-9_]{2,63}$`)

func omadaSecretField(name string, v any) bool {
	if name == "key" {
		s, ok := v.(string)
		return !ok || !omadaEventKey.MatchString(s)
	}
	return omadaSecretKey.MatchString(name)
}

// scalars keeps the simple fields of a controller's record (strings cut short, numbers, booleans)
// and leaves nested structures and secret-looking names out: the answer stays small and the
// fields are the controller's own names.
func scalars(item map[string]any) map[string]any {
	out := map[string]any{}
	keys := make([]string, 0, len(item))
	for k := range item {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if omadaSecretField(k, item[k]) {
			continue
		}
		switch v := item[k].(type) {
		case string:
			if v != "" {
				out[k] = cut(v, 200)
			}
		case float64, bool:
			out[k] = v
		}
		if len(out) >= 30 {
			break
		}
	}
	return out
}

// What a call may narrow: every listing takes match; only the logs take a time window (the
// controller refuses a log request without one: HTTP 400, seen live 2026-11-17).
const (
	omadaMatchProp = `,"match":{"type":"string","maxLength":100,"description":"only entries in which some field contains this text, any case: a name, hostname, MAC (AA-BB-… or aa:bb:…), IP, AP name, SSID or an event key such as DISCONN; alternatives with |, e.g. Pixel|Luxel. The controller's whole list is searched, not only the first page"}`
	omadaTimeProps = `,"since":{"type":"string","maxLength":40,"description":"start of the time window (default 1d): a duration (30m, 6h, 2d), today, yesterday, a date or an RFC 3339 time"},` +
		`"until":{"type":"string","maxLength":40,"description":"end of the time window (default now), same forms as since"}`
	omadaScanRows  = 3000 // the most rows a match looks through
	omadaPageRows  = 1000
	omadaShowLimit = 200
)

type omadaKind int

const (
	omadaPlain omadaKind = iota // a current list (devices, clients, sites)
	omadaLog                    // a log: needs a time window
	omadaOne                    // a single record (firmware)
)

func omadaCap(o Options, name, desc, extraProps string, kind omadaKind, run func(ctx context.Context, c *omadaClient, site omadaSite, in omadaInput) (any, error)) capability.Capability {
	props := `"site":{"type":"string","maxLength":64,"description":"site name (default: the configured one, or the only one)"}`
	if kind != omadaOne {
		props += `,"limit":{"type":"integer","minimum":1,"maximum":200,"description":"at most this many entries (default 50)"}` + omadaMatchProp
	}
	if kind == omadaLog {
		props += omadaTimeProps
	}
	return fn{
		name:   name,
		desc:   desc,
		schema: `{"type":"object","additionalProperties":false,"properties":{` + props + extraProps + `}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in omadaInput
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			if in.Limit == 0 {
				in.Limit = 50
			}
			if in.Limit < 1 || in.Limit > omadaShowLimit {
				return nil, inputErr("limit must be 1..%d", omadaShowLimit)
			}
			if kind != omadaLog && (in.Since != "" || in.Until != "") {
				return nil, inputErr("%s shows the current state and has no time window; since/until belong to omada.events, omada.alerts and omada.audit_logs", name)
			}
			if kind == omadaOne && in.Match != "" {
				return nil, inputErr("%s has no match", name)
			}
			if len(in.Match) > 100 {
				return nil, inputErr("match must hold at most 100 characters")
			}
			if kind == omadaLog {
				if err := in.window(time.Now()); err != nil {
					return nil, err
				}
			}
			if in.DeviceMAC != "" && !macRe.MatchString(in.DeviceMAC) {
				return nil, inputErr("device_mac must look like AA-BB-CC-DD-EE-FF")
			}
			if o.Connectors == nil || o.Connectors.Omada == nil || o.omada == nil {
				return nil, errNoConnector("omada")
			}
			var site omadaSite
			if name != "omada.sites" { // listing the sites needs none
				var err error
				if site, err = o.omada.resolveSite(ctx, in.Site); err != nil {
					return nil, err
				}
			}
			out, err := run(ctx, o.omada, site, in)
			if err != nil {
				return nil, err
			}
			cleaned, masked := finishOutput(out, o.Connectors.secrets())
			if m, ok := cleaned.(map[string]any); ok {
				m["masked"] = masked
			}
			return cleaned, nil
		},
	}
}

type omadaInput struct {
	Site      string `json:"site"`
	Limit     int    `json:"limit"`
	DeviceMAC string `json:"device_mac"`
	Match     string `json:"match"`
	Since     string `json:"since"`
	Until     string `json:"until"`

	from, to time.Time
}

// window reads since/until; the default is the last day.
func (in *omadaInput) window(now time.Time) error {
	since, ok, err := parseWhen(in.Since, now)
	if err != nil {
		return err
	}
	if !ok {
		since = now.Add(-24 * time.Hour)
	}
	until, ok, err := parseWhen(in.Until, now)
	if err != nil {
		return err
	}
	if !ok {
		until = now
	}
	if !since.Before(until) {
		return inputErr("since must lie before until")
	}
	in.from, in.to = since, until
	return nil
}

var macRe = regexp.MustCompile(`^[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}$`)

// matcher reads match: alternatives split at |, any case; a MAC written with colons is looked for
// in the controller's own spelling (dashes) too. Nil: everything is kept.
func matcher(match string) func(map[string]any) bool {
	var needles []string
	for _, part := range strings.Split(match, "|") {
		p := strings.ToLower(strings.TrimSpace(part))
		if p == "" {
			continue
		}
		needles = append(needles, p)
		if strings.Contains(p, ":") {
			needles = append(needles, strings.ReplaceAll(p, ":", "-"))
		}
	}
	if len(needles) == 0 {
		return nil
	}
	return func(item map[string]any) bool {
		for k, v := range item {
			s, ok := v.(string)
			if !ok || omadaSecretField(k, v) {
				continue
			}
			low := strings.ToLower(s)
			for _, n := range needles {
				if strings.Contains(low, n) {
					return true
				}
			}
		}
		return false
	}
}

// rows reads a listing page by page: without a filter until `want` rows are there, with one
// until `want` rows matched or omadaScanRows were looked at. With each set, the whole list (up to
// omadaScanRows) is read and each sees every row.
func (c *omadaClient) rows(ctx context.Context, path string, q url.Values, want int, keep func(map[string]any) bool, each func(map[string]any)) (kept []map[string]any, total, scanned int, err error) {
	size := min(want, 100)
	if keep != nil || each != nil {
		size = omadaPageRows
	}
	for page := 1; ; page++ {
		pq := url.Values{}
		for k, v := range q {
			pq[k] = v
		}
		pq.Set("page", strconv.Itoa(page))
		pq.Set("pageSize", strconv.Itoa(size))
		res, err := c.get(ctx, path, pq)
		if err != nil {
			return nil, 0, 0, err
		}
		items, n := omadaList(res)
		total = n
		for _, it := range items {
			scanned++
			if each != nil {
				each(it)
			}
			if (keep == nil || keep(it)) && len(kept) < omadaShowLimit {
				kept = append(kept, it)
			}
		}
		enough := each == nil && len(kept) >= want
		if len(items) == 0 || len(items) < size || scanned >= total || enough || scanned >= omadaScanRows {
			return kept, total, scanned, nil
		}
	}
}

// eventSummary counts the whole time window of an event log, whatever match picked: by event key
// and by the kind of device an event names ([ap:…], [switch:…], [gateway:…]). Single hits cannot
// show that something is missing altogether (live 2026-11-17: 630 events, not one of them on an
// access point, so no Wi-Fi drop could ever be found there; the run searched for it 17 times).
type eventSummary struct {
	byKey map[string]int
	via   map[string]int
}

var omadaVia = regexp.MustCompile(`\[(ap|switch|gateway|client):`)

func (s *eventSummary) add(it map[string]any) {
	if k, ok := it["key"].(string); ok && omadaEventKey.MatchString(k) {
		s.byKey[k]++
	}
	content, _ := it["content"].(string)
	if !strings.Contains(content, "[client:") {
		return // a device's own event ([ap:…] was connected) says nothing about clients
	}
	s.via["client"]++
	seen := map[string]bool{}
	for _, m := range omadaVia.FindAllStringSubmatch(content, -1) {
		if m[1] != "client" && !seen[m[1]] {
			seen[m[1]] = true
			s.via[m[1]]++
		}
	}
}

func (s *eventSummary) out(scanned int) map[string]any {
	on := map[string]any{"ap": s.via["ap"], "switch": s.via["switch"], "gateway": s.via["gateway"]}
	out := map[string]any{"events": scanned, "by_key": s.byKey, "client_events": s.via["client"], "client_events_on": on}
	if s.via["client"] > 0 && s.via["ap"] == 0 {
		out["note"] = fmt.Sprintf("none of the %d client events in this window happened on an access point: Wi-Fi clients joining, leaving or roaming are not in this log, so their absence says nothing about Wi-Fi drops (what is logged is a setting of the controller)", s.via["client"])
	}
	return out
}

// Client rows hold some 40 fields; these say who, where and how well. The rest stays out so a
// list of thirty clients fits on one screen of a model's prompt (live 2026-11-17: 15.8 KB, read in
// five parts, and the run stopped before it found the phone).
var omadaClientFields = []string{
	"name", "hostName", "mac", "ip", "wireless", "ssid", "apName", "radioId", "channel", "rssi", "snr",
	"signalLevel", "rxRate", "txRate", "powerSave", "uptime", "lastSeen", "active", "switchName", "port",
	"networkName", "vid", "deviceType",
}

// readable turns the controller's millisecond time stamps into times a model can read.
func readable(m map[string]any) map[string]any {
	for _, k := range []string{"time", "lastSeen", "lastSeenTime", "timestamp"} {
		if v, ok := m[k].(float64); ok && v > 1e12 {
			m[k] = time.UnixMilli(int64(v)).Local().Format(time.RFC3339)
		}
	}
	return m
}

// pick keeps the named fields of a record (after the secret filter of scalars).
func pick(item map[string]any, fields []string) map[string]any {
	all := scalars(item)
	out := map[string]any{}
	for _, f := range fields {
		if v, ok := all[f]; ok {
			out[f] = v
		}
	}
	return out
}

func omadaCaps(o Options) []capability.Capability {
	list := func(name, desc, path string, kind omadaKind, fields []string) capability.Capability {
		return omadaCap(o, name, desc, "", kind, func(ctx context.Context, c *omadaClient, site omadaSite, in omadaInput) (any, error) {
			q := url.Values{}
			if kind == omadaLog {
				q.Set("filters.timeStart", strconv.FormatInt(in.from.UnixMilli(), 10))
				q.Set("filters.timeEnd", strconv.FormatInt(in.to.UnixMilli(), 10))
			}
			p := "/sites"
			if name != "omada.sites" {
				p += "/" + url.PathEscape(site.ID) + path
			}
			keep := matcher(in.Match)
			matched := 0
			if keep != nil {
				inner := keep
				keep = func(it map[string]any) bool {
					ok := inner(it)
					if ok {
						matched++
					}
					return ok
				}
			}
			var sum *eventSummary
			var each func(map[string]any)
			if name == "omada.events" {
				sum = &eventSummary{byKey: map[string]int{}, via: map[string]int{}}
				each = sum.add
			}
			items, total, scanned, err := c.rows(ctx, p, q, in.Limit, keep, each)
			if err != nil {
				return nil, err
			}
			shown := make([]any, 0, len(items))
			for _, it := range items {
				if len(shown) >= in.Limit {
					break
				}
				row := scalars(it)
				if fields != nil {
					row = pick(it, fields)
				}
				shown = append(shown, readable(row))
			}
			out := map[string]any{"total": total, "shown": len(shown), "items": shown}
			if site.Name != "" {
				out["site"] = site.Name
			}
			if keep != nil {
				out["match"] = in.Match
				out["matched"] = matched
				out["scanned"] = scanned
				out["truncated"] = matched > len(shown) || scanned < total
				if matched == 0 {
					out["note"] = fmt.Sprintf("no entry among %d contains %q; names in the controller may differ from what people call a device (try part of it, the MAC or the IP)", scanned, in.Match)
				}
			} else {
				out["truncated"] = total > len(shown)
			}
			if kind == omadaLog {
				out["since"] = in.from.Local().Format(time.RFC3339)
				out["until"] = in.to.Local().Format(time.RFC3339)
			}
			if sum != nil {
				out["window"] = sum.out(scanned)
			}
			return out, nil
		})
	}
	return []capability.Capability{
		list("omada.sites", "Omada controller: the sites (name and id).", "", omadaPlain, nil),
		list("omada.devices", "Omada controller: the devices of a site (access points, switches, gateways) with status, model, IP, firmware and uptime as the controller reports them. Current state only.", "/devices", omadaPlain, nil),
		list("omada.clients", "Omada controller: the clients connected NOW (name, hostname, MAC, IP, SSID, AP, signal: rssi/snr/signalLevel, rates, uptime in seconds). Find one device with match. Android and iOS use a random MAC per Wi-Fi network, so the hostname is the better key. No history: for what happened earlier use omada.events.", "/clients", omadaPlain, omadaClientFields),
		list("omada.events", "Omada controller: the site's event log in a time window (default the last day), newest first: clients going online/offline (L_C_CONN, L_C_DISCONN), roaming, network changes, device events. Narrow it with match (a client's MAC from omada.clients, an AP name, an event key). Only what the controller is set to log is there.", "/logs/events", omadaLog, nil),
		list("omada.audit_logs", "Omada controller: the site's audit log in a time window (who changed what in the controller).", "/audit-logs", omadaLog, nil),
		list("omada.alerts", "Omada controller: the site's alerts in a time window (default the last day).", "/logs/alerts", omadaLog, nil),
		omadaCap(o, "omada.firmware", "Omada controller: the latest firmware the controller knows for one device (give its MAC from omada.devices).",
			`,"device_mac":{"type":"string","maxLength":17,"description":"MAC of the device, AA-BB-CC-DD-EE-FF, from omada.devices"}`, omadaOne,
			func(ctx context.Context, c *omadaClient, site omadaSite, in omadaInput) (any, error) {
				if in.DeviceMAC == "" {
					return nil, inputErr("device_mac is required")
				}
				mac := strings.ToUpper(strings.ReplaceAll(in.DeviceMAC, ":", "-"))
				res, err := c.get(ctx, "/sites/"+url.PathEscape(site.ID)+"/devices/"+url.PathEscape(mac)+"/latest-firmware-info", nil)
				if err != nil {
					return nil, err
				}
				var m map[string]any
				if json.Unmarshal(res, &m) != nil {
					return nil, fmt.Errorf("the Omada controller's firmware answer is not understood")
				}
				return map[string]any{"site": site.Name, "device_mac": mac, "firmware": scalars(m)}, nil
			}),
	}
}
