package tools

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"topcoat/node/internal/providers/capability"
)

// fakeKeycloak answers the few Admin REST paths the capabilities read, with the shapes Keycloak
// documents. It records every request so a test can prove nothing but GET (and the token POST) was sent.
type fakeKeycloak struct {
	mu       sync.Mutex
	requests []string
	tokens   int
	failPath string // a path suffix that answers 500
	expire   bool   // the first admin call answers 401 once
	srv      *httptest.Server
}

const (
	kcSecret = "kc-secret-for-the-test-0001"
	kcClient = "topcoat-readonly"
)

func newFakeKeycloak(t *testing.T) *fakeKeycloak {
	f := &fakeKeycloak{}
	json200 := func(w http.ResponseWriter, v any) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(v)
	}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.requests = append(f.requests, r.Method+" "+r.URL.RequestURI())
		fail, expire := f.failPath, f.expire
		if r.URL.Path == "/realms/home/protocol/openid-connect/token" {
			f.tokens++
		}
		f.mu.Unlock()
		if r.URL.Path == "/realms/home/protocol/openid-connect/token" {
			_ = r.ParseForm()
			if r.Form.Get("client_id") != kcClient || r.Form.Get("client_secret") != kcSecret || r.Form.Get("grant_type") != "client_credentials" {
				http.Error(w, "bad", http.StatusUnauthorized)
				return
			}
			json200(w, map[string]any{"access_token": "tok-" + strings.Repeat("x", 8), "expires_in": 300})
			return
		}
		if r.Header.Get("Authorization") != "Bearer tok-xxxxxxxx" {
			http.Error(w, "no", http.StatusUnauthorized)
			return
		}
		if expire {
			f.mu.Lock()
			f.expire = false
			f.mu.Unlock()
			http.Error(w, "expired", http.StatusUnauthorized)
			return
		}
		if fail != "" && strings.HasSuffix(r.URL.Path, fail) {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		p := strings.TrimPrefix(r.URL.Path, "/admin/realms/home")
		first := r.URL.Query().Get("first")
		switch {
		case p == "/users" && r.URL.Query().Get("username") == "ann":
			json200(w, []any{map[string]any{"id": "u1", "username": "ann", "firstName": "Ann", "lastName": "Example", "enabled": true, "federationLink": "ldap1", "email": "ann@example.lan"}})
		case p == "/users" && r.URL.Query().Get("username") != "":
			json200(w, []any{})
		case p == "/users":
			if first != "0" {
				json200(w, []any{})
				return
			}
			json200(w, []any{
				map[string]any{"id": "u1", "username": "ann", "firstName": "Ann", "lastName": "Example", "enabled": true, "federationLink": "ldap1", "email": "ann@example.lan"},
				map[string]any{"id": "u2", "username": "bob", "enabled": false},
			})
		case p == "/groups":
			if first != "0" {
				json200(w, []any{})
				return
			}
			json200(w, []any{
				map[string]any{"id": "g1", "name": "family", "path": "/family", "subGroupCount": 1},
				map[string]any{"id": "g3", "name": "admins", "path": "/admins", "subGroups": []any{}},
			})
		case p == "/groups/g1/children":
			json200(w, []any{map[string]any{"id": "g2", "name": "kids", "path": "/family/kids"}})
		case p == "/groups/g1/members":
			json200(w, []any{map[string]any{"id": "u1"}})
		case p == "/groups/g2/members":
			json200(w, []any{map[string]any{"id": "u2"}})
		case p == "/groups/g3/members":
			json200(w, []any{})
		case p == "/groups/g1/role-mappings":
			json200(w, map[string]any{
				"realmMappings":  []any{map[string]any{"name": "household"}},
				"clientMappings": map[string]any{"homarr": map[string]any{"id": "c2", "client": "homarr", "mappings": []any{map[string]any{"name": "user"}}}},
			})
		case strings.HasSuffix(p, "/role-mappings"):
			json200(w, map[string]any{})
		case p == "/clients":
			if first != "0" {
				json200(w, []any{})
				return
			}
			json200(w, []any{
				map[string]any{"id": "c1", "clientId": "account", "enabled": true},
				map[string]any{"id": "c2", "clientId": "homarr", "name": "Homarr", "enabled": true, "baseUrl": "https://homarr.example.lan/?token=abc"},
			})
		case p == "/clients/c2/roles":
			json200(w, []any{map[string]any{"name": "user"}, map[string]any{"name": "admin"}})
		case p == "/users/u1/groups":
			json200(w, []any{map[string]any{"id": "g1", "name": "family", "path": "/family"}})
		case p == "/users/u1/role-mappings/clients/c2/composite":
			json200(w, []any{map[string]any{"name": "user"}})
		default:
			http.Error(w, "not found", http.StatusNotFound)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeKeycloak) options() Options {
	cfg := &KeycloakConfig{URL: f.srv.URL, Realm: "home", ClientID: kcClient, ClientSecret: kcSecret}
	return Options{Connectors: &Connectors{Keycloak: cfg}}
}

func TestKeycloakCapabilitiesExistOnlyWithTheConnectorsSection(t *testing.T) {
	for _, n := range []string{"keycloak.snapshot", "keycloak.user"} {
		if _, ok := NewRegistry(Options{}).Get(n); ok {
			t.Fatalf("%s offered without a keycloak section", n)
		}
	}
	f := newFakeKeycloak(t)
	r := NewRegistry(f.options())
	for _, n := range []string{"keycloak.snapshot", "keycloak.user"} {
		c, ok := r.Get(n)
		if !ok {
			t.Fatalf("%s missing", n)
		}
		if !capability.InfoOf(c).ReadOnly {
			t.Fatalf("%s must be read-only", n)
		}
	}
}

func TestKeycloakSnapshotReadsUsersGroupsAndAppsAndNothingElse(t *testing.T) {
	f := newFakeKeycloak(t)
	out, err := invoke(t, f.options(), "keycloak.snapshot", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["realm"] != "home" {
		t.Fatalf("%v", out)
	}
	users := out["users"].(map[string]any)["data"].([]any)
	if len(users) != 2 {
		t.Fatalf("%v", users)
	}
	ann := users[0].(map[string]any)
	if ann["username"] != "ann" || ann["name"] != "Ann Example" || ann["source"] != "directory" || ann["enabled"] != true {
		t.Fatalf("%v", ann)
	}
	if bob := users[1].(map[string]any); bob["source"] != "local" || bob["enabled"] != false {
		t.Fatalf("%v", bob)
	}
	if strings.Contains(mustJSON(out), "ann@example.lan") {
		t.Fatal("an e-mail address is in the output")
	}
	groups := out["groups"].(map[string]any)["data"].([]any)
	if len(groups) != 3 { // /admins, /family, /family/kids (the child was fetched, it was not embedded)
		t.Fatalf("%v", groups)
	}
	byPath := map[string]map[string]any{}
	for _, g := range groups {
		m := g.(map[string]any)
		byPath[m["path"].(string)] = m
	}
	fam := byPath["/family"]
	if mem := fam["members"].([]any); len(mem) != 1 || mem[0] != "u1" {
		t.Fatalf("%v", fam)
	}
	if cr := fam["client_roles"].(map[string]any)["homarr"].([]any); len(cr) != 1 || cr[0] != "user" {
		t.Fatalf("%v", fam)
	}
	if rr := fam["realm_roles"].([]any); len(rr) != 1 || rr[0] != "household" {
		t.Fatalf("%v", fam)
	}
	if byPath["/family/kids"]["members"].([]any)[0] != "u2" {
		t.Fatalf("%v", byPath["/family/kids"])
	}
	apps := out["apps"].(map[string]any)["data"].([]any)
	if len(apps) != 2 {
		t.Fatalf("%v", apps)
	}
	acct, homarr := apps[0].(map[string]any), apps[1].(map[string]any)
	if acct["client_id"] != "account" || acct["builtin"] != true || homarr["builtin"] != false {
		t.Fatalf("%v %v", acct, homarr)
	}
	if roles := homarr["roles"].([]any); len(roles) != 2 || roles[0] != "admin" {
		t.Fatalf("%v", homarr)
	}
	if strings.Contains(homarr["url"].(string), "abc") {
		t.Fatalf("a token in a URL query: %v", homarr["url"])
	}
	// nothing but reads: GET, and the one POST that logs in
	for _, r := range f.requests {
		if !strings.HasPrefix(r, "GET /admin/realms/home/") && !strings.HasPrefix(r, "POST /realms/home/protocol/openid-connect/token") {
			t.Fatalf("an unexpected request: %s", r)
		}
	}
	if f.tokens != 1 {
		t.Fatalf("the token was fetched %d times for one snapshot", f.tokens)
	}
}

func TestKeycloakSnapshotSectionsFailOnTheirOwnAndNeverBecomeEmpty(t *testing.T) {
	f := newFakeKeycloak(t)
	f.failPath = "/users"
	out, err := invoke(t, f.options(), "keycloak.snapshot", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	users := out["users"].(map[string]any)
	if users["ok"] == true || !strings.Contains(users["error"].(string), "HTTP 500") {
		t.Fatalf("%v", users)
	}
	if out["groups"].(map[string]any)["ok"] != true || out["apps"].(map[string]any)["ok"] != true {
		t.Fatalf("the other sections must still be read: %v", out)
	}
}

func TestKeycloakMemberOfOneGroupThatFailsIsMarkedNotDropped(t *testing.T) {
	f := newFakeKeycloak(t)
	f.failPath = "/groups/g2/members"
	out, err := invoke(t, f.options(), "keycloak.snapshot", `{}`)
	if err != nil {
		t.Fatal(err)
	}
	for _, g := range out["groups"].(map[string]any)["data"].([]any) {
		m := g.(map[string]any)
		if m["path"] == "/family/kids" && (m["members_ok"] != false || m["members_error"] == nil) {
			t.Fatalf("%v", m)
		}
	}
}

func TestKeycloakLogsInAgainOnceWhenTheTokenIsRefused(t *testing.T) {
	f := newFakeKeycloak(t)
	f.expire = true
	if _, err := invoke(t, f.options(), "keycloak.snapshot", `{}`); err != nil {
		t.Fatal(err)
	}
	if f.tokens != 2 {
		t.Fatalf("tokens %d", f.tokens)
	}
}

func TestKeycloakWrongCredentialsAreSaidInPlainWordsAndTheSecretIsNeverShown(t *testing.T) {
	f := newFakeKeycloak(t)
	o := f.options()
	o.Connectors.Keycloak.ClientSecret = "wrong-secret-value-0002"
	out, err := invoke(t, o, "keycloak.snapshot", `{}`)
	// every section fails with the same, readable reason; the capability itself still answers
	if err != nil {
		t.Fatal(err)
	}
	users := out["users"].(map[string]any)
	if users["ok"] == true || !strings.Contains(users["error"].(string), "refused the client credentials") {
		t.Fatalf("%v", users)
	}
	if s := mustJSON(out); strings.Contains(s, "wrong-secret-value-0002") || strings.Contains(s, kcSecret) {
		t.Fatalf("a secret is in the output: %s", s)
	}
}

func TestKeycloakUserShowsGroupsAndTheRolesKeycloakComputes(t *testing.T) {
	f := newFakeKeycloak(t)
	out, err := invoke(t, f.options(), "keycloak.user", `{"user":"ann"}`)
	if err != nil {
		t.Fatal(err)
	}
	if g := out["groups"].([]any); len(g) != 1 || g[0] != "/family" {
		t.Fatalf("%v", out)
	}
	apps := out["apps"].([]any)
	if len(apps) != 1 || apps[0].(map[string]any)["client_id"] != "homarr" || apps[0].(map[string]any)["roles"].([]any)[0] != "user" {
		t.Fatalf("%v", apps)
	}
	if strings.Contains(mustJSON(out), "ann@example.lan") {
		t.Fatal("an e-mail address is in the output")
	}
	if _, err := invoke(t, f.options(), "keycloak.user", `{"user":"nobody"}`); err == nil || !strings.Contains(err.Error(), "no user") {
		t.Fatalf("%v", err)
	}
	for _, bad := range []string{`{}`, `{"user":""}`, `{"user":"ann","all":true}`, `{"user":"` + strings.Repeat("a", 101) + `"}`} {
		if _, err := invoke(t, f.options(), "keycloak.user", bad); err == nil {
			t.Fatalf("accepted %s", bad)
		}
	}
}
