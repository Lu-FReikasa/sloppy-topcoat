package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
)

// Keycloak, read-only: the identity view's source (E3-12). A confidential client with a service
// account whose roles are the read-only ones of realm-management (view-users, query-users,
// query-groups, view-clients, query-clients) gets an access token
//
//	POST {url}/realms/{realm}/protocol/openid-connect/token
//	     grant_type=client_credentials&client_id=…&client_secret=…   ->  access_token, expires_in
//
// and the capabilities GET from {url}/admin/realms/{realm}/…: users, groups (+ members and role
// mappings), clients (+ roles) and, for one user, the effective client roles. The paths and fields
// are the Keycloak Admin REST API's as the maintainer's documentation lists them; nothing here was
// run against a real Keycloak (UNVERIFIED on real hardware): the tests answer from a fake with the
// same shapes. Nothing here can change anything: only GET, and the service account has no
// manage-* role.
const (
	kcPage        = 100
	kcMaxUsers    = 300
	kcMaxGroups   = 150
	kcMaxMembers  = 300
	kcMaxClients  = 100
	kcMaxDepth    = 6
	kcParallel    = 6
	kcEffectiveBy = 50 // clients asked for one user's effective roles
)

// kcBuiltin are the clients every realm has; they are listed but marked, so the page can hide them.
var kcBuiltin = map[string]bool{
	"account": true, "account-console": true, "admin-cli": true, "broker": true,
	"realm-management": true, "security-admin-console": true,
}

type keycloakClient struct {
	cfg  *KeycloakConfig
	http func() (*http.Client, error)

	mu      sync.Mutex
	token   string
	expires time.Time
}

func newKeycloakClient(cfg *KeycloakConfig) *keycloakClient {
	return &keycloakClient{cfg: cfg, http: cfg.client}
}

func (c *keycloakClient) base() string { return strings.TrimRight(c.cfg.URL, "/") }

func (c *keycloakClient) authorize(ctx context.Context) error {
	client, err := c.http()
	if err != nil {
		return err
	}
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {c.cfg.ClientID}, "client_secret": {c.cfg.ClientSecret}}
	u := c.base() + "/realms/" + url.PathEscape(c.cfg.Realm) + "/protocol/openid-connect/token"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, strings.NewReader(form.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	raw, code, err := fetch(client, req, "Keycloak")
	if err != nil {
		return err
	}
	if code != http.StatusOK {
		return fmt.Errorf("Keycloak refused the client credentials (HTTP %d): is the client confidential with service accounts enabled, and the secret current?", code) //nolint:staticcheck // product name
	}
	var res struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if json.Unmarshal(raw, &res) != nil || res.AccessToken == "" {
		return fmt.Errorf("Keycloak's token answer has no access_token") //nolint:staticcheck // product name
	}
	c.token = res.AccessToken
	c.expires = time.Now().Add(time.Duration(res.ExpiresIn)*time.Second - 20*time.Second)
	return nil
}

// bearer returns a valid access token, logging in when there is none (or when asked to start over
// because Keycloak said the token is gone). The lock covers the token only, not the requests, so
// the parallel reads of one snapshot really are parallel.
func (c *keycloakClient) bearer(ctx context.Context, renew bool) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if renew || c.token == "" || time.Now().After(c.expires) {
		c.token = ""
		if err := c.authorize(ctx); err != nil {
			return "", err
		}
	}
	return c.token, nil
}

// get is a GET of an admin path of the realm; it logs in when there is no valid token and once
// more when Keycloak says the token is gone.
func (c *keycloakClient) get(ctx context.Context, path string, q url.Values) (json.RawMessage, error) {
	for attempt := 0; attempt < 2; attempt++ {
		token, err := c.bearer(ctx, attempt > 0)
		if err != nil {
			return nil, err
		}
		client, err := c.http()
		if err != nil {
			return nil, err
		}
		u := c.base() + "/admin/realms/" + url.PathEscape(c.cfg.Realm) + path
		if len(q) > 0 {
			u += "?" + q.Encode()
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "Bearer "+token)
		raw, code, err := fetch(client, req, "Keycloak")
		if err != nil {
			return nil, err
		}
		switch code {
		case http.StatusOK:
			return raw, nil
		case http.StatusUnauthorized:
			continue // log in again, once
		case http.StatusForbidden:
			return nil, fmt.Errorf("Keycloak refused %s (HTTP 403): the service account lacks a realm-management role (view-users, query-users, query-groups, view-clients or query-clients)", cut(path, 60)) //nolint:staticcheck // product name
		case http.StatusNotFound:
			return nil, fmt.Errorf("Keycloak does not know %s (HTTP 404): wrong realm, or a url that needs /auth in front?", cut(path, 60)) //nolint:staticcheck // product name
		}
		return nil, fmt.Errorf("Keycloak answered HTTP %d on %s", code, cut(path, 60)) //nolint:staticcheck // product name
	}
	return nil, fmt.Errorf("Keycloak keeps refusing the access token") //nolint:staticcheck // product name
}

// list reads a paginated listing (first/max) up to limit entries; truncated says there were more.
func (c *keycloakClient) list(ctx context.Context, path string, q url.Values, limit int) (items []map[string]any, truncated bool, err error) {
	for first := 0; len(items) < limit; first += kcPage {
		qq := url.Values{}
		for k, v := range q {
			qq[k] = v
		}
		qq.Set("first", strconv.Itoa(first))
		qq.Set("max", strconv.Itoa(kcPage))
		raw, err := c.get(ctx, path, qq)
		if err != nil {
			return nil, false, err
		}
		var page []map[string]any
		if json.Unmarshal(raw, &page) != nil {
			return nil, false, fmt.Errorf("Keycloak's answer on %s is not a list", cut(path, 60)) //nolint:staticcheck // product name
		}
		items = append(items, page...)
		if len(page) < kcPage {
			return items, false, nil
		}
	}
	if len(items) > limit {
		return items[:limit], true, nil
	}
	// exactly limit entries: there may be more; one more page tells
	raw, err := c.get(ctx, path, url.Values{"first": {strconv.Itoa(len(items))}, "max": {"1"}})
	if err != nil {
		return items, false, nil //nolint:nilerr // the list itself worked; "maybe more" is then unknown
	}
	var more []map[string]any
	return items, json.Unmarshal(raw, &more) == nil && len(more) > 0, nil
}

func str(m map[string]any, k string) string {
	s, _ := m[k].(string)
	return s
}

func kcBool(m map[string]any, k string, def bool) bool {
	if b, ok := m[k].(bool); ok {
		return b
	}
	return def
}

// kcGroup is one group of the tree, flattened with its path.
type kcGroup struct {
	ID, Name, Path string
}

// groupTree flattens the groups: the top level, then each group's children (embedded
// `subGroups`, or one call for `subGroupCount` when they are not embedded), at most maxGroups.
func (c *keycloakClient) groupTree(ctx context.Context) (out []kcGroup, truncated bool, err error) {
	top, trunc, err := c.list(ctx, "/groups", url.Values{"briefRepresentation": {"false"}}, kcMaxGroups)
	if err != nil {
		return nil, false, err
	}
	truncated = trunc
	var walk func(items []map[string]any, depth int) error
	walk = func(items []map[string]any, depth int) error {
		for _, g := range items {
			if len(out) >= kcMaxGroups {
				truncated = true
				return nil
			}
			id, name := str(g, "id"), str(g, "name")
			path := str(g, "path")
			if path == "" {
				path = "/" + name
			}
			out = append(out, kcGroup{ID: id, Name: name, Path: path})
			if depth >= kcMaxDepth {
				continue
			}
			var kids []map[string]any
			if raw, ok := g["subGroups"].([]any); ok && len(raw) > 0 {
				for _, r := range raw {
					if m, ok := r.(map[string]any); ok {
						kids = append(kids, m)
					}
				}
			} else if n, _ := g["subGroupCount"].(float64); n > 0 && id != "" {
				k, _, err := c.list(ctx, "/groups/"+url.PathEscape(id)+"/children", nil, kcMaxGroups)
				if err != nil {
					return err
				}
				kids = k
			}
			if err := walk(kids, depth+1); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(top, 0); err != nil {
		return nil, false, err
	}
	return out, truncated, nil
}

// roleNames reads the names of a list of role representations.
func roleNames(v any) []string {
	var out []string
	if list, ok := v.([]any); ok {
		for _, r := range list {
			if m, ok := r.(map[string]any); ok {
				if n := str(m, "name"); n != "" {
					out = append(out, n)
				}
			}
		}
	}
	sort.Strings(out)
	return out
}

func userRow(u map[string]any) map[string]any {
	name := strings.TrimSpace(str(u, "firstName") + " " + str(u, "lastName"))
	source := "local"
	if str(u, "federationLink") != "" {
		source = "directory"
	}
	return map[string]any{"id": str(u, "id"), "username": str(u, "username"), "name": name, "enabled": kcBool(u, "enabled", true), "source": source}
}

type kcInput struct {
	User string `json:"user"`
}

func keycloakSnapshot(o Options) capability.Capability {
	return fn{
		name:   "keycloak.snapshot",
		desc:   "Identities from Keycloak in one call, for the hub's identity view: users (user name, display name, enabled, whether they come from a directory such as AD), groups with their members and their client and realm role mappings, and clients (the apps) with their roles. Three sections that each say on their own whether they could be read. No e-mail addresses, attributes or credentials. Read-only.",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			kc, err := o.keycloakClient()
			if err != nil {
				return nil, err
			}
			out := map[string]any{"realm": kc.cfg.Realm, "taken_at": o.now().UTC().Format(time.RFC3339)}

			out["apps"] = kc.clientsSection(ctx)
			out["users"] = kc.usersSection(ctx)
			out["groups"] = kc.groupsSection(ctx)
			cleaned, masked := finishOutput(out, o.Connectors.secrets())
			if m, ok := cleaned.(map[string]any); ok {
				m["masked"] = masked
			}
			return cleaned, nil
		},
	}
}

func (c *keycloakClient) clientsSection(ctx context.Context) section {
	items, trunc, err := c.list(ctx, "/clients", nil, kcMaxClients)
	if err != nil {
		return failed(err)
	}
	rows := make([]map[string]any, len(items))
	parallel(len(items), kcParallel, func(i int) {
		cl := items[i]
		cid := str(cl, "clientId")
		row := map[string]any{
			"id": str(cl, "id"), "client_id": cid, "name": str(cl, "name"), "enabled": kcBool(cl, "enabled", true),
			"builtin": kcBuiltin[cid], "protocol": str(cl, "protocol"),
			"url": cleanMonitorURL(firstNonEmpty(str(cl, "baseUrl"), str(cl, "rootUrl"))),
		}
		if !kcBuiltin[cid] && str(cl, "id") != "" {
			if raw, err := c.get(ctx, "/clients/"+url.PathEscape(str(cl, "id"))+"/roles", url.Values{"max": {"100"}}); err == nil {
				var roles []map[string]any
				if json.Unmarshal(raw, &roles) == nil {
					var names []string
					for _, r := range roles {
						if n := str(r, "name"); n != "" {
							names = append(names, n)
						}
					}
					sort.Strings(names)
					row["roles"] = names
				}
			} else {
				row["roles_error"] = cut(err.Error(), 160)
			}
		}
		rows[i] = row
	})
	sort.Slice(rows, func(i, j int) bool { return str(rows[i], "client_id") < str(rows[j], "client_id") })
	return section{OK: true, Data: rows, Total: len(rows), Truncated: trunc}
}

func firstNonEmpty(ss ...string) string {
	for _, s := range ss {
		if s != "" {
			return s
		}
	}
	return ""
}

func (c *keycloakClient) usersSection(ctx context.Context) section {
	items, trunc, err := c.list(ctx, "/users", url.Values{"briefRepresentation": {"true"}}, kcMaxUsers)
	if err != nil {
		return failed(err)
	}
	rows := make([]map[string]any, 0, len(items))
	for _, u := range items {
		rows = append(rows, userRow(u))
	}
	sort.Slice(rows, func(i, j int) bool { return str(rows[i], "username") < str(rows[j], "username") })
	return section{OK: true, Data: rows, Total: len(rows), Truncated: trunc}
}

func (c *keycloakClient) groupsSection(ctx context.Context) section {
	groups, trunc, err := c.groupTree(ctx)
	if err != nil {
		return failed(err)
	}
	rows := make([]map[string]any, len(groups))
	parallel(len(groups), kcParallel, func(i int) {
		g := groups[i]
		row := map[string]any{"id": g.ID, "path": g.Path, "name": g.Name, "members": []string{}, "members_ok": true}
		members, mtrunc, err := c.list(ctx, "/groups/"+url.PathEscape(g.ID)+"/members", url.Values{"briefRepresentation": {"true"}}, kcMaxMembers)
		if err != nil {
			row["members_ok"], row["members_error"] = false, cut(err.Error(), 160)
		} else {
			ids := make([]string, 0, len(members))
			for _, m := range members {
				if id := str(m, "id"); id != "" {
					ids = append(ids, id)
				}
			}
			sort.Strings(ids)
			row["members"] = ids
			if mtrunc {
				row["members_truncated"] = true
			}
		}
		if raw, err := c.get(ctx, "/groups/"+url.PathEscape(g.ID)+"/role-mappings", nil); err == nil {
			var rm struct {
				RealmMappings  any `json:"realmMappings"`
				ClientMappings map[string]struct {
					Client   string `json:"client"`
					Mappings any    `json:"mappings"`
				} `json:"clientMappings"`
			}
			if json.Unmarshal(raw, &rm) == nil {
				row["realm_roles"] = roleNames(rm.RealmMappings)
				cr := map[string][]string{}
				for key, cm := range rm.ClientMappings {
					name := cm.Client
					if name == "" {
						name = key
					}
					if r := roleNames(cm.Mappings); len(r) > 0 {
						cr[name] = r
					}
				}
				row["client_roles"] = cr
			}
		} else {
			row["roles_error"] = cut(err.Error(), 160)
		}
		rows[i] = row
	})
	sort.Slice(rows, func(i, j int) bool { return str(rows[i], "path") < str(rows[j], "path") })
	return section{OK: true, Data: rows, Total: len(rows), Truncated: trunc}
}

func keycloakUser(o Options) capability.Capability {
	return fn{
		name: "keycloak.user",
		desc: "One person in Keycloak: their groups and the client (app) roles they effectively have — what Keycloak itself computes from direct mappings, groups and composite roles. Give the user name exactly. The authoritative answer to \"why can this person use that app\", where the snapshot only follows group role mappings.",
		schema: `{"type":"object","additionalProperties":false,"required":["user"],"properties":{` +
			`"user":{"type":"string","minLength":1,"maxLength":100,"description":"the exact user name"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in kcInput
			if err := decodeInput(raw, &in); err != nil {
				return nil, err
			}
			in.User = strings.TrimSpace(in.User)
			if in.User == "" || len(in.User) > 100 {
				return nil, inputErr("user must be a user name of 1..100 characters")
			}
			kc, err := o.keycloakClient()
			if err != nil {
				return nil, err
			}
			found, _, err := kc.list(ctx, "/users", url.Values{"username": {in.User}, "exact": {"true"}, "briefRepresentation": {"true"}}, 2)
			if err != nil {
				return nil, err
			}
			if len(found) == 0 {
				return nil, inputErr("no user %q in the realm", in.User)
			}
			u := found[0]
			id := str(u, "id")
			out := map[string]any{"user": userRow(u)}
			groups, _, err := kc.list(ctx, "/users/"+url.PathEscape(id)+"/groups", nil, kcMaxGroups)
			if err != nil {
				return nil, err
			}
			var paths []string
			for _, g := range groups {
				paths = append(paths, firstNonEmpty(str(g, "path"), "/"+str(g, "name")))
			}
			sort.Strings(paths)
			out["groups"] = paths
			clients, _, err := kc.list(ctx, "/clients", nil, kcEffectiveBy)
			if err != nil {
				return nil, err
			}
			apps := make([]map[string]any, len(clients))
			parallel(len(clients), kcParallel, func(i int) {
				cid := str(clients[i], "clientId")
				if kcBuiltin[cid] || str(clients[i], "id") == "" {
					return
				}
				raw, err := kc.get(ctx, "/users/"+url.PathEscape(id)+"/role-mappings/clients/"+url.PathEscape(str(clients[i], "id"))+"/composite", nil)
				if err != nil {
					apps[i] = map[string]any{"client_id": cid, "error": cut(err.Error(), 160)}
					return
				}
				var roles []map[string]any
				if json.Unmarshal(raw, &roles) != nil || len(roles) == 0 {
					return
				}
				apps[i] = map[string]any{"client_id": cid, "name": str(clients[i], "name"), "roles": roleNames(toAny(roles))}
			})
			var withRoles []map[string]any
			for _, a := range apps {
				if a != nil {
					withRoles = append(withRoles, a)
				}
			}
			out["apps"] = withRoles
			cleaned, masked := finishOutput(out, o.Connectors.secrets())
			if m, ok := cleaned.(map[string]any); ok {
				m["masked"] = masked
			}
			return cleaned, nil
		},
	}
}

func toAny(rs []map[string]any) []any {
	out := make([]any, len(rs))
	for i, r := range rs {
		out[i] = r
	}
	return out
}

func (o Options) keycloakClient() (*keycloakClient, error) {
	if o.Connectors == nil || o.Connectors.Keycloak == nil || o.keycloak == nil {
		return nil, errNoConnector("keycloak")
	}
	return o.keycloak, nil
}

func keycloakCaps(o Options) []capability.Capability {
	return []capability.Capability{keycloakSnapshot(o), keycloakUser(o)}
}
