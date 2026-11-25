package tools

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"strings"
	"sync/atomic"
	"testing"
)

// roundTrip serves canned pages by host name (no network).
type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) {
	resp, err := f(r)
	if resp != nil {
		resp.Request = r
	}
	return resp, err
}

func page(status int, ctype, body string, h ...string) *http.Response {
	resp := &http.Response{StatusCode: status, Header: http.Header{"Content-Type": {ctype}}, Body: io.NopCloser(strings.NewReader(body))}
	for i := 0; i+1 < len(h); i += 2 {
		resp.Header.Set(h[i], h[i+1])
	}
	return resp
}

// resolver maps names to addresses; everything else does not resolve.
func resolver(m map[string]string) func(context.Context, string) ([]netip.Addr, error) {
	return func(_ context.Context, host string) ([]netip.Addr, error) {
		if a, ok := m[host]; ok {
			return []netip.Addr{netip.MustParseAddr(a)}, nil
		}
		return nil, errors.New("no such host")
	}
}

func webOptions(rt roundTrip) Options {
	return Options{Web: WebOptions{
		Fetch:     true,
		Transport: rt,
		Resolve: resolver(map[string]string{
			"example.org": "93.184.216.34", "docs.example.org": "93.184.216.35",
			"intranet.example.org": "10.0.0.5", "loop.example.org": "127.0.0.1", "meta.example.org": "169.254.169.254",
		}),
	}}
}

func TestOnlyPlainPublicAddressesAreFetchable(t *testing.T) {
	for addr, want := range map[string]bool{
		"93.184.216.34": true, "8.8.8.8": true, "2606:4700:4700::1111": true,
		"10.0.0.5": false, "192.168.1.1": false, "172.16.0.1": false, "172.31.255.255": false, "127.0.0.1": false,
		"169.254.169.254": false, "100.100.9.1": false, "0.0.0.0": false, "224.0.0.1": false, "255.255.255.255": false,
		"192.0.2.1": false, "198.18.0.1": false, "240.0.0.1": false,
		"::1": false, "fe80::1": false, "fc00::1": false, "fd12:3456::1": false, "::": false, "ff02::1": false,
		"::ffff:10.0.0.1": false, "::ffff:127.0.0.1": false, "64:ff9b::a00:1": false, "2002:a00:1::": false, "2001:db8::1": false,
	} {
		if got := publicAddr(netip.MustParseAddr(addr)); got != want {
			t.Errorf("publicAddr(%s) = %v, want %v", addr, got, want)
		}
	}
}

func TestWebFetchRefusesWhatIsNotAPublicPage(t *testing.T) {
	var calls atomic.Int32
	o := webOptions(func(r *http.Request) (*http.Response, error) { calls.Add(1); return page(200, "text/plain", "x"), nil })
	for _, in := range []string{
		`{"url":"ftp://example.org/x"}`, `{"url":"file:///etc/passwd"}`, `{"url":"example.org/x"}`, `{"url":"javascript:alert(1)"}`,
		`{"url":"http://user:pw@example.org/"}`, `{"url":"http://example.org:22/"}`, `{"url":"http://example.org:6379/"}`,
		`{"url":"http://localhost/"}`, `{"url":"http://printer/"}`, `{"url":"http://nas.lan/"}`, `{"url":"http://router.local/"}`,
		`{"url":"http://127.0.0.1/"}`, `{"url":"http://[::1]/"}`, `{"url":"http://10.0.0.5/"}`, `{"url":"http://169.254.169.254/latest/meta-data/"}`,
		`{"url":"http://2130706433/"}`, `{"url":"http://0x7f.1/"}`,
		`{"url":"http://intranet.example.org/"}`, `{"url":"http://loop.example.org/"}`, `{"url":"http://meta.example.org/"}`,
		`{"url":"http://unknown.example.org/"}`, `{"url":"https://example.org/","max_chars":100}`, `{"url":"https://example.org/","extra":1}`, `{}`,
	} {
		var ie *InputError
		_, err := invoke(t, o, "web.fetch", in)
		if err == nil {
			t.Errorf("%s: accepted", in)
		} else if strings.HasPrefix(in, `{"url":"http://`) && strings.Contains(in, "example.org") && !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", in, err)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("%d requests went out for refused URLs", calls.Load())
	}
}

func TestWebFetchReadsTextFromAPage(t *testing.T) {
	const doc = `<!doctype html><html><head><title>Release &amp; notes</title><style>p{color:red}</style><script>var secret="x"</script></head>
<body><!-- a comment --><nav>menu</nav><h1>Version 2.1</h1><p>Fixes the <b>crash</b> on start&nbsp;up.</p><ul><li>one</li><li>two</li></ul>
<script>track()</script><p>token=ghp_abcdefghijklmnopqrstuvwxyz0123456789</p></body></html>`
	var gotUA string
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		gotUA = r.Header.Get("User-Agent")
		if r.URL.Host != "docs.example.org" {
			t.Errorf("host %s", r.URL.Host)
		}
		return page(200, "text/html; charset=utf-8", doc), nil
	})
	out, err := invoke(t, o, "web.fetch", `{"url":"https://docs.example.org/rel?a=1#frag"}`)
	if err != nil {
		t.Fatal(err)
	}
	text := out["text"].(string)
	for _, want := range []string{"Version 2.1", "Fixes the crash on start up.", "one", "two"} {
		if !strings.Contains(text, want) {
			t.Errorf("text lacks %q: %q", want, text)
		}
	}
	for _, bad := range []string{"<", "secret", "track()", "color:red", "a comment", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"} {
		if strings.Contains(text, bad) {
			t.Errorf("text holds %q: %q", bad, text)
		}
	}
	if out["title"] != "Release & notes" || out["url"] != "https://docs.example.org/rel?a=1" || out["truncated"] != false || out["status"] != float64(200) {
		t.Fatalf("%v", out)
	}
	if !strings.Contains(out["note"].(string), "untrusted") || !strings.Contains(gotUA, "read-only") {
		t.Fatalf("%v %q", out, gotUA)
	}
}

func TestWebFetchLimitsSizeAndKind(t *testing.T) {
	long := strings.Repeat("word ", 5000)
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		switch r.URL.Path {
		case "/long":
			return page(200, "text/plain", long), nil
		case "/huge":
			return page(200, "text/plain", strings.Repeat("a", webMaxBody+10)), nil
		case "/pic":
			return page(200, "image/png", "\x89PNG"), nil
		case "/missing":
			return page(404, "text/html", "no"), nil
		case "/json":
			return page(200, "application/json", `{"ok":true}`), nil
		}
		return page(500, "text/plain", ""), nil
	})
	out, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/long","max_chars":1000}`)
	if err != nil || out["truncated"] != true || out["chars"] != float64(1000) || len([]rune(out["text"].(string))) != 1000 {
		t.Fatalf("%v %v", out, err)
	}
	out, err = invoke(t, o, "web.fetch", `{"url":"https://example.org/huge","max_chars":20000}`)
	if err != nil || out["truncated"] != true {
		t.Fatalf("%v %v", out, err)
	}
	out, err = invoke(t, o, "web.fetch", `{"url":"https://example.org/json"}`)
	if err != nil || out["text"] != `{"ok":true}` {
		t.Fatalf("%v %v", out, err)
	}
	for path, want := range map[string]string{"/pic": "not text", "/missing": "HTTP 404"} {
		if _, err := invoke(t, o, "web.fetch", `{"url":"https://example.org`+path+`"}`); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v", path, err)
		}
	}
}

func TestWebFetchChecksEveryRedirect(t *testing.T) {
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		switch r.URL.String() {
		case "https://example.org/a":
			return page(302, "text/html", "", "Location", "https://docs.example.org/b"), nil
		case "https://docs.example.org/b":
			return page(200, "text/plain", "arrived"), nil
		case "https://example.org/to-intranet":
			return page(302, "text/html", "", "Location", "https://intranet.example.org/secret"), nil
		case "https://example.org/to-metadata":
			return page(302, "text/html", "", "Location", "http://169.254.169.254/latest/meta-data/"), nil
		case "https://example.org/to-ftp":
			return page(302, "text/html", "", "Location", "ftp://example.org/x"), nil
		case "https://example.org/loop":
			return page(302, "text/html", "", "Location", "https://example.org/loop"), nil
		}
		t.Errorf("a request went out for %s", r.URL)
		return page(500, "text/plain", ""), nil
	})
	if out, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/a"}`); err != nil || out["text"] != "arrived" || out["url"] != "https://docs.example.org/b" {
		t.Fatalf("a public redirect: %v %v", out, err)
	}
	for _, p := range []string{"to-intranet", "to-metadata", "to-ftp", "loop"} {
		if _, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/`+p+`"}`); err == nil {
			t.Errorf("redirect %s was followed", p)
		}
	}
}

// A name that is public when it is checked and private when the connection is made gets nowhere.
func TestAHostThatChangesItsAddressIsRefusedWhenConnecting(t *testing.T) {
	var lookups atomic.Int32
	w := WebOptions{Fetch: true, Resolve: func(context.Context, string) ([]netip.Addr, error) {
		if lookups.Add(1) == 1 {
			return []netip.Addr{netip.MustParseAddr("93.184.216.34")}, nil
		}
		return []netip.Addr{netip.MustParseAddr("127.0.0.1")}, nil
	}}
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("the loopback server was reached") }))
	defer srv.Close()
	o := Options{Web: w}
	if _, err := w.checkedAddrs(context.Background(), "rebind.example.org"); err != nil {
		t.Fatal(err)
	}
	if _, err := invoke(t, o, "web.fetch", `{"url":"http://rebind.example.org/"}`); err == nil || !strings.Contains(err.Error(), "public internet") {
		t.Fatalf("%v", err)
	}
}

func searxServer(t *testing.T, status int, body string, seen *[]string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if seen != nil {
			*seen = append(*seen, r.URL.String())
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

const searxBody = `{"query":"x","results":[
 {"title":"Fix  the crash\non start","url":"https://user:pw@docs.example.org/fix#top","content":"Set the flag. token=ghp_abcdefghijklmnopqrstuvwxyz0123456789","engine":"duckduckgo"},
 {"title":"ftp","url":"ftp://x/y","content":"no"},
 {"title":"Second","url":"https://example.org/two","content":"` + "SNIPPET" + `","engine":"bing"},
 {"title":"Third","url":"https://example.org/three","content":"c","engine":"bing"}]}`

func TestWebSearchReturnsTitleURLAndSnippet(t *testing.T) {
	var seen []string
	srv := searxServer(t, 200, strings.Replace(searxBody, "SNIPPET", strings.Repeat("long ", 200), 1), &seen)
	o := Options{Web: WebOptions{SearxngURL: srv.URL}}
	out, err := invoke(t, o, "web.search", `{"query":"  llama.cpp   crash on start ","limit":2,"time_range":"month","language":"en"}`)
	if err != nil {
		t.Fatal(err)
	}
	if len(seen) != 1 || !strings.HasPrefix(seen[0], "/search?") {
		t.Fatalf("%v", seen)
	}
	for _, want := range []string{"format=json", "q=llama.cpp+crash+on+start", "time_range=month", "language=en", "safesearch=1"} {
		if !strings.Contains(seen[0], want) {
			t.Errorf("request %s lacks %s", seen[0], want)
		}
	}
	res := out["results"].([]any)
	if out["count"] != float64(2) || len(res) != 2 {
		t.Fatalf("limit 2 (the ftp hit is skipped): %v", out)
	}
	first := res[0].(map[string]any)
	if first["title"] != "Fix the crash on start" || first["url"] != "https://docs.example.org/fix" || first["engine"] != "duckduckgo" {
		t.Fatalf("%v", first)
	}
	if s := first["snippet"].(string); strings.Contains(s, "ghp_") || !strings.Contains(s, "Set the flag.") {
		t.Fatalf("a token reached the output: %q", s)
	}
	if s := res[1].(map[string]any)["snippet"].(string); len([]rune(s)) > webSnippetChars {
		t.Fatalf("snippet of %d characters", len([]rune(s)))
	}
	if !strings.Contains(out["note"].(string), "untrusted") {
		t.Fatalf("%v", out)
	}
}

func TestWebSearchRefusalsAreClear(t *testing.T) {
	forbidden := searxServer(t, 403, ``, nil)
	html := searxServer(t, 200, `<html>no json</html>`, nil)
	broken := searxServer(t, 500, ``, nil)
	for _, c := range []struct {
		srv  *httptest.Server
		want string
	}{
		{forbidden, "json format"}, {html, "did not answer with JSON"}, {broken, "HTTP 500"},
	} {
		_, err := invoke(t, Options{Web: WebOptions{SearxngURL: c.srv.URL}}, "web.search", `{"query":"x"}`)
		if err == nil || !strings.Contains(err.Error(), c.want) || strings.Contains(err.Error(), c.srv.URL) {
			t.Errorf("want %q, got %v", c.want, err)
		}
	}
	o := Options{Web: WebOptions{SearxngURL: forbidden.URL}}
	for _, bad := range []string{`{}`, `{"query":""}`, `{"query":"   "}`, `{"query":"x","limit":0,"limit2":1}`, `{"query":"x","limit":11}`,
		`{"query":"x","time_range":"decade"}`, `{"query":"x","language":"de_DE!"}`, `{"query":"x","url":"http://evil"}`, `{"query":"` + strings.Repeat("a", 201) + `"}`} {
		var ie *InputError
		if _, err := invoke(t, o, "web.search", bad); !errors.As(err, &ie) {
			t.Errorf("%s: want an input error, got %v", bad, err)
		}
	}
}

func TestWebCapabilitiesAreOfferedOnlyWhereConfigured(t *testing.T) {
	has := func(o Options, name string) bool {
		_, ok := NewRegistry(o).Get(name)
		return ok
	}
	if has(Options{}, "web.search") || has(Options{}, "web.fetch") {
		t.Fatal("web capabilities offered without configuration")
	}
	if !has(Options{Web: WebOptions{SearxngURL: "http://10.0.0.x:8080"}}, "web.search") || has(Options{Web: WebOptions{SearxngURL: "http://10.0.0.x:8080"}}, "web.fetch") {
		t.Fatal("searxng_url must offer web.search only")
	}
	if !has(Options{Web: WebOptions{Fetch: true}}, "web.fetch") || has(Options{Web: WebOptions{Fetch: true}}, "web.search") {
		t.Fatal("web_fetch must offer web.fetch only")
	}
}

// --- what leaves the network (defense in depth) --------------------------------------------

func guardOptions(rt roundTrip, searx string) Options {
	o := webOptions(rt)
	o.Web.SearxngURL = searx
	o.Web.PrivateNames = []string{"alpha", "alice", "ab", "example.net"}
	o.Web.SelfNames = func() []string { return []string{"node-docker.example.lan", "node-docker", "example.lan"} }
	return o
}

func TestWebInputsWithPrivateDataAreRefusedBeforeAnythingIsSent(t *testing.T) {
	var seen []string
	srv := searxServer(t, 200, searxBody, &seen)
	var fetched int
	o := guardOptions(func(*http.Request) (*http.Response, error) {
		fetched++
		return page(200, "text/plain", "x"), nil
	}, srv.URL)
	cases := []struct{ cap, input, kind string }{
		{"web.search", `{"query":"password of alpha 10.77.0.3"}`, "ip"},
		{"web.search", `{"query":"login of alpha failed"}`, "host"},
		{"web.search", `{"query":"why does node-docker restart"}`, "host"},
		{"web.search", `{"query":"status.example.lan down"}`, "internal-name"},
		{"web.search", `{"query":"nas.home.arpa unreachable"}`, "internal-name"},
		{"web.search", `{"query":"alice reports a crash"}`, "host"},
		{"web.search", `{"query":"cert for status.example.net"}`, "host"},
		{"web.search", `{"query":"error 2001:db8::1 refused"}`, "ip"},
		{"web.search", `{"query":"fe80::1 neighbor"}`, "ip"},
		{"web.search", `{"query":"seen on AA:BB:CC:00:11:22"}`, "mac"},
		{"web.search", `{"query":"mail from admin@example.org bounced"}`, "email"},
		{"web.search", `{"query":"connect with password=hunter2hunter2"}`, "secret"},
		{"web.search", `{"query":"token ghp_abcdefghijklmnopqrstuvwxyz0123456789 rejected"}`, "secret"},
		{"web.fetch", `{"url":"http://10.0.0.5/admin"}`, "ip"},
		{"web.fetch", `{"url":"https://docs.example.org/?token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"}`, "secret"},
		{"web.fetch", `{"url":"https://alpha.example.org/x"}`, "host"},
		{"web.fetch", `{"url":"https://wiki.example.lan/page"}`, "internal-name"},
	}
	for _, c := range cases {
		_, err := invoke(t, o, c.cap, c.input)
		var pe *PrivateDataError
		if !errors.As(err, &pe) {
			t.Errorf("%s %s: want a PrivateDataError, got %v", c.cap, c.input, err)
			continue
		}
		if !strings.Contains(strings.Join(pe.Kinds, ","), c.kind) {
			t.Errorf("%s: kinds %v lack %s", c.input, pe.Kinds, c.kind)
		}
		// the refusal says what KIND was found and never repeats the value
		for _, leak := range []string{"10.77.0.3", "hunter2", "ghp_", "AA:BB", "admin@", "alpha", "alice"} {
			if strings.Contains(err.Error(), leak) {
				t.Errorf("%s: the refusal repeats %q: %v", c.input, leak, err)
			}
		}
		var ie *InputError
		if !errors.As(err, &ie) {
			t.Errorf("%s: a refusal is an input error too", c.input)
		}
	}
	if len(seen) != 0 || fetched != 0 {
		t.Fatalf("a refused input reached the network: search %v, fetch %d", seen, fetched)
	}
}

func TestPublicInputsPassTheGuard(t *testing.T) {
	var seen []string
	o := guardOptions(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path == "/search" {
			seen = append(seen, r.URL.RawQuery)
			return page(200, "application/json", searxBody), nil
		}
		return page(200, "text/plain", "ok"), nil
	}, "http://searx.example.org:8080")
	for _, q := range []string{
		"llama.cpp gemma 4 sliding window kv cache", "qxl_alloc_bo_reserved failed to allocate VRAM BO",
		"docker compose restart policy unless-stopped", "kernel 6.1 nouveau fifo error", "sandbox box timeout", // "ab" is too short to count
	} {
		if _, err := invoke(t, o, "web.search", `{"query":"`+q+`"}`); err != nil {
			t.Errorf("%q refused: %v", q, err)
		}
	}
	if len(seen) != 5 {
		t.Fatalf("%v", seen)
	}
	if _, err := invoke(t, o, "web.fetch", `{"url":"https://docs.example.org/page?id=7"}`); err != nil {
		t.Fatal(err)
	}
}

func TestNamesMatchAsWholeWordsOnly(t *testing.T) {
	for text, want := range map[string]bool{
		"the host alpha is down": true, "alpha": true, "alpha.example.org": true, "host-as": false,
		"my-alpha": false, "sandboxes": false, "a host-a_x": false, "alpha-2": false, "(alpha)": true,
	} {
		if got := containsWord(strings.ToLower(text), "alpha"); got != want {
			t.Errorf("%q: got %v want %v", text, got, want)
		}
	}
}

func TestTheNodeKnowsItsOwnNames(t *testing.T) {
	names := WebOptions{PrivateNames: []string{"X", " Corp-Box. "}, SelfNames: func() []string {
		return []string{"node-a.lab.example.net", "node-a", "lab.example.net", "ab"}
	}}.privateNames()
	got := strings.Join(names, ",")
	if got != "corp-box,node-a.lab.example.net,node-a,lab.example.net" { // "X" and "ab" are too short
		t.Fatal(got)
	}
	// the real detection does not fail and yields this machine's host name
	if h, err := os.Hostname(); err == nil && len(h) >= minNameLen {
		found := false
		for _, n := range (WebOptions{}).privateNames() {
			found = found || strings.HasPrefix(strings.ToLower(h), n) || n == strings.ToLower(h)
		}
		if !found {
			t.Fatalf("host name %q is not among %v", h, (WebOptions{}).privateNames())
		}
	}
}

func TestIPv4AddressesAreTriedFirst(t *testing.T) {
	in := []netip.Addr{netip.MustParseAddr("2001:db8::1"), netip.MustParseAddr("192.0.2.1"), netip.MustParseAddr("2001:db8::2"), netip.MustParseAddr("192.0.2.2")}
	got := ipv4First(in)
	want := []string{"192.0.2.1", "192.0.2.2", "2001:db8::1", "2001:db8::2"}
	for i, w := range want {
		if got[i].String() != w {
			t.Fatalf("%v", got)
		}
	}
}

func TestWebFetchCanReturnTheMarkupForAParser(t *testing.T) {
	const doc = "<html><head><title>Song &amp; chords</title></head><body><pre>G      D" + string(rune(10)) + "Walking down the road</pre>" +
		"<div class=\"js-store\" data-content=\"{&quot;capo&quot;:2}\"></div></body></html>"
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		return page(200, "text/html; charset=utf-8", doc), nil
	})
	out, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/song","format":"html"}`)
	if err != nil {
		t.Fatal(err)
	}
	if out["text"] != doc || out["title"] != "Song & chords" || out["truncated"] != false {
		t.Fatalf("%v", out)
	}
	if imgs, _ := out["images"].([]any); len(imgs) != 0 {
		t.Fatalf("images %v", out["images"])
	}
	// the text form still collapses the layout, as before
	out, err = invoke(t, o, "web.fetch", `{"url":"https://example.org/song"}`)
	if err != nil || strings.Contains(out["text"].(string), "G      D") {
		t.Fatalf("%v %v", out, err)
	}
	for _, in := range []string{
		`{"url":"https://example.org/song","format":"pdf"}`,
		`{"url":"https://example.org/song","max_chars":30000}`,
		`{"url":"https://example.org/song","format":"html","max_chars":700000}`,
	} {
		if _, err := invoke(t, o, "web.fetch", in); err == nil {
			t.Errorf("%s was taken", in)
		}
	}
	if _, err := invoke(t, o, "web.fetch", `{"url":"https://example.org/song","format":"html","max_chars":400000}`); err != nil {
		t.Fatal(err)
	}
}

func TestOutputsAreNotHTMLEscaped(t *testing.T) {
	raw, err := marshalOutput(map[string]any{"text": `<a href="x">&quot;</a>`})
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `{"text":"<a href=\"x\">&quot;</a>"}` {
		t.Fatalf("%s", raw)
	}
}
