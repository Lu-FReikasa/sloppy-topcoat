package tools

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/netip"
	"strings"
	"testing"
	"time"

	"topcoat/node/internal/providers/capability"
)

// originOptions is a node with web.search, web.images and web.fetch on one memory: the search
// engine and every page are canned (no network); fetched counts the page requests.
func originOptions(fetched *[]string) Options {
	o := webOptions(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host == "searx.example.org" {
			if r.URL.Query().Get("categories") == "images" {
				return page(200, "application/json", `{"results":[{"title":"A cat","url":"https://example.org/cats","img_src":"https://example.org/cat.png","thumbnail_src":"https://example.org/cat-small.png"}]}`), nil
			}
			return page(200, "application/json", `{"results":[{"title":"Hit","url":"https://Example.org/hit?id=7#part","content":"c"}]}`), nil
		}
		*fetched = append(*fetched, r.URL.String())
		if strings.HasSuffix(r.URL.Path, ".png") {
			return page(200, "image/png", pngBytes), nil
		}
		return page(200, "text/html", `<p>Text</p><img src="/walk.png" alt="A character walking">`), nil
	})
	o.Web.SearxngURL = "http://searx.example.org"
	o.Web.Seen = NewURLMemory()
	return o
}

var plain = context.Background() // no url_origin: what a model's call looks like to the node

func TestAModelCannotFetchAURLThatCameFromNoSearch(t *testing.T) {
	var fetched []string
	o := originOptions(&fetched)
	for _, u := range []string{
		"https://example.org/hit?id=7&d=c2VjcmV0",  // a hit's address with data added
		"https://attacker.example.org/?d=c2VjcmV0", // made up
	} {
		_, err := invokeAs(plain, t, o, "web.fetch", fmt.Sprintf(`{"url":%q}`, u))
		var ie *InputError
		if !errors.As(err, &ie) || !strings.Contains(err.Error(), "did not come from a search") {
			t.Fatalf("%s: %v", u, err)
		}
	}
	if _, err := invokeAs(plain, t, o, "web.image", `{"url":"https://example.org/cat.png"}`); err == nil || !strings.Contains(err.Error(), "not one this node gave out") {
		t.Fatalf("picture: %v", err)
	}
	if len(fetched) != 0 {
		t.Fatalf("a refused URL was asked: %v", fetched)
	}
}

func TestHitsPagesAndPicturesTheNodeGaveOutCanBeFetched(t *testing.T) {
	var fetched []string
	o := originOptions(&fetched)
	if _, err := invokeAs(plain, t, o, "web.search", `{"query":"public thing"}`); err != nil {
		t.Fatal(err)
	}
	// the hit as the search gave it (host case and fragment do not matter, the query does)
	if _, err := invokeAs(plain, t, o, "web.fetch", `{"url":"https://example.org/hit?id=7"}`); err != nil {
		t.Fatal(err)
	}
	// a picture the page listed, and the picture hits of web.images
	if _, err := invokeAs(plain, t, o, "web.image", `{"url":"https://example.org/walk.png"}`); err != nil {
		t.Fatal(err)
	}
	if _, err := invokeAs(plain, t, o, "web.images", `{"query":"cat"}`); err != nil {
		t.Fatal(err)
	}
	for _, u := range []string{"https://example.org/cat.png", "https://example.org/cat-small.png"} {
		if _, err := invokeAs(plain, t, o, "web.image", fmt.Sprintf(`{"url":%q}`, u)); err != nil {
			t.Fatalf("%s: %v", u, err)
		}
	}
	if _, err := invokeAs(plain, t, o, "web.fetch", `{"url":"https://example.org/cats"}`); err != nil {
		t.Fatalf("the page of a picture hit: %v", err)
	}
	if len(fetched) != 5 {
		t.Fatalf("%v", fetched)
	}
}

func TestTheHubVouchesForThePersonsURL(t *testing.T) {
	var fetched []string
	o := originOptions(&fetched)
	if _, err := invokeAs(WithPersonURL(plain), t, o, "web.fetch", `{"url":"https://example.org/from-the-person"}`); err != nil {
		t.Fatal(err)
	}
	// vouching does not lift the other checks
	if _, err := invokeAs(WithPersonURL(plain), t, o, "web.fetch", `{"url":"https://intranet.example.org/"}`); err == nil {
		t.Fatal("a private address was read")
	}
}

func TestOnlyTheURLCapabilitiesSayTheyCheckTheOrigin(t *testing.T) {
	r := NewRegistry(originOptions(new([]string)))
	for _, name := range r.Names() {
		c, _ := r.Get(name)
		if got := capability.InfoOf(c).URLOrigin; got != URLOriginCapabilities[name] {
			t.Errorf("%s: url_origin %v", name, got)
		}
	}
	if !URLOriginCapabilities["web.fetch"] || !URLOriginCapabilities["web.image"] || len(URLOriginCapabilities) != 2 {
		t.Fatalf("%v", URLOriginCapabilities)
	}
}

func TestTheMemoryForgetsOldHitsAndStaysBounded(t *testing.T) {
	now := time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)
	m := NewURLMemory()
	m.now = func() time.Time { return now }
	m.Remember("https://example.org/a", "", "ftp://example.org/x", "not a url")
	if !m.Has("HTTPS://EXAMPLE.ORG/a#x") || m.Has("https://example.org/a?x=1") || len(m.seen) != 1 {
		t.Fatalf("%v", m.seen)
	}
	now = now.Add(originTTL + time.Minute)
	if m.Has("https://example.org/a") {
		t.Fatal("an old hit is still fetchable")
	}
	for i := range originMaxURLs + 10 {
		m.Remember(fmt.Sprintf("https://example.org/%d", i))
		now = now.Add(time.Millisecond)
	}
	if len(m.seen) > originMaxURLs || !m.Has(fmt.Sprintf("https://example.org/%d", originMaxURLs+9)) || m.Has("https://example.org/0") {
		t.Fatalf("%d kept", len(m.seen))
	}
}

func TestTheHostIsSentInTheFormTheOriginRuleCompared(t *testing.T) {
	// the rule ignores the host's case, so the node must not send the case the model chose: in the
	// Host header and the TLS name it could spell out what the run read, one bit per letter
	var fetched []string
	o := originOptions(&fetched)
	resolve := o.Web.Resolve // like DNS, the name resolves whatever its case
	o.Web.Resolve = func(ctx context.Context, host string) ([]netip.Addr, error) {
		return resolve(ctx, strings.ToLower(host))
	}
	if _, err := invokeAs(plain, t, o, "web.search", `{"query":"public thing"}`); err != nil {
		t.Fatal(err)
	}
	if _, err := invokeAs(plain, t, o, "web.fetch", `{"url":"https://eXaMPle.ORG/hit?id=7"}`); err != nil {
		t.Fatal(err)
	}
	if _, err := invokeAs(plain, t, o, "web.image", `{"url":"https://EXAMPLE.org/walk.png"}`); err != nil {
		t.Fatal(err)
	}
	want := []string{"https://example.org/hit?id=7", "https://example.org/walk.png"}
	if fmt.Sprint(fetched) != fmt.Sprint(want) {
		t.Fatalf("sent %v, want %v", fetched, want)
	}
}
