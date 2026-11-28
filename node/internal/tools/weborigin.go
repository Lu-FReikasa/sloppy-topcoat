package tools

import (
	"context"
	"net/url"
	"strings"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
)

// Where a URL may come from (security audit 2026-11-27, finding 03). A model that read an injected
// page could put what it read elsewhere into a URL and fetch it; the private-data guard only sees
// addresses and secret shapes. So web.fetch and web.image read only a URL this node itself handed
// out as a hit (web.search, web.images, the pictures and final address of a page it read), or one
// the hub vouches for as the person's own: the action argument url_origin "person", which the hub
// sets outside the capability's input, so no model can set it. The hub applies the same rule per
// run before it asks (web_access.WebOrigins); this is the node's half, for a call that reaches it
// another way.

const (
	// how long a hit stays fetchable, and how many the node keeps (oldest go first)
	originTTL     = 6 * time.Hour
	originMaxURLs = 4096
)

// URLMemory holds the URLs this node handed out as hits. Safe for concurrent use.
type URLMemory struct {
	mu   sync.Mutex
	seen map[string]time.Time
	now  func() time.Time
}

// NewURLMemory returns an empty memory.
func NewURLMemory() *URLMemory { return &URLMemory{seen: map[string]time.Time{}, now: time.Now} }

// Remember keeps the URLs (in the form urlKey gives); empty and unusable ones are skipped.
func (m *URLMemory) Remember(urls ...string) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	for _, raw := range urls {
		k := urlKey(raw)
		if k == "" {
			continue
		}
		if _, has := m.seen[k]; !has && len(m.seen) >= originMaxURLs {
			m.evict(now)
		}
		m.seen[k] = now
	}
}

// evict drops the expired URLs, or else the oldest one (called with the lock held).
func (m *URLMemory) evict(now time.Time) {
	oldest, at := "", now
	for k, t := range m.seen {
		if now.Sub(t) > originTTL {
			delete(m.seen, k)
			continue
		}
		if !t.After(at) {
			oldest, at = k, t
		}
	}
	if len(m.seen) >= originMaxURLs && oldest != "" {
		delete(m.seen, oldest)
	}
}

// Has reports whether the URL was handed out as a hit within originTTL.
func (m *URLMemory) Has(raw string) bool {
	if m == nil {
		return false
	}
	k := urlKey(raw)
	if k == "" {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	t, ok := m.seen[k]
	return ok && m.now().Sub(t) <= originTTL
}

// urlKey is a URL as the origin rule compares it: scheme and host in lower case, no user part, no
// fragment, "/" for an empty path. The query stays as it is: that is where data would go.
func urlKey(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	u.Scheme = strings.ToLower(u.Scheme)
	if (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return ""
	}
	u.Host = strings.ToLower(u.Host)
	u.User, u.Fragment, u.RawFragment = nil, "", ""
	if u.Path == "" && u.RawPath == "" {
		u.Path = "/"
	}
	return u.String()
}

type personURLKey struct{}

// WithPersonURL marks the call's URL as the person's own (the hub's url_origin "person").
func WithPersonURL(ctx context.Context) context.Context {
	return context.WithValue(ctx, personURLKey{}, true)
}

func personURL(ctx context.Context) bool {
	v, _ := ctx.Value(personURLKey{}).(bool)
	return v
}

// checkOrigin refuses a URL that is neither the person's nor one of this node's hits. what is
// "page" or "picture".
func (w WebOptions) checkOrigin(ctx context.Context, what, raw string) error {
	if personURL(ctx) || w.Seen.Has(raw) {
		return nil
	}
	if what == "picture" {
		return inputErr("this picture URL is not one this node gave out: take it exactly as a web.images hit (image or thumbnail) or a page read with web.fetch (its images) gives it")
	}
	return inputErr("this URL did not come from a search: fetch only a URL exactly as a web.search hit or the person's message gives it (no URL you built or changed yourself); search for the page first if you need it")
}

// URLOriginCapabilities are the capabilities that check where their URL comes from; the hub may
// send url_origin "person" only with them.
var URLOriginCapabilities = map[string]bool{"web.fetch": true, "web.image": true}

// originFn is a read-only capability that checks where its URL comes from: the inventory says so,
// so the hub knows it may (and must) vouch for the person's URLs.
type originFn struct{ fn }

func (f originFn) Info() capability.Info {
	return capability.Info{Description: f.desc, ReadOnly: true, URLOrigin: true}
}
