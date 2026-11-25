package tools

import (
	"fmt"
	"net/netip"
	"os"
	"regexp"
	"strings"
)

// The node's own guard for what leaves the network (defense in depth: the hub checks the same, but
// a call that reaches the node some other way — an old hub, a direct API call, a bug — must not put
// private data into a search engine either). web.search's query and web.fetch's URL are refused
// when they hold an IP address, a MAC or e-mail address, a name of the operator (this machine's
// host name and domain, the search domains of its resolver, tools.private_names), an internal-looking
// domain name (.lan, .local, .internal …) or a secret. The refusal names the KINDS found, never the
// value: it is returned to the model and logged.

// PrivateDataError is a web input refused because it holds private data.
type PrivateDataError struct {
	What  string // "query" or "URL"
	Kinds []string
}

func (e *PrivateDataError) Error() string {
	return fmt.Sprintf("the %s contains private data (%s) and would leave the network: ask again about the public thing only, without host names, addresses, user names or secrets",
		e.What, strings.Join(e.Kinds, ", "))
}

// Unwrap makes a refusal an input error too (the input is what is wrong).
func (e *PrivateDataError) Unwrap() error { return &InputError{Msg: e.Error()} }

var (
	ipv4Re     = regexp.MustCompile(`\b(?:\d{1,3}\.){3}\d{1,3}\b`)
	ipv6Re     = regexp.MustCompile(`[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*`)
	outMACRe   = regexp.MustCompile(`\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b`)
	emailRe    = regexp.MustCompile(`[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+`)
	internalRe = regexp.MustCompile(`(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:lan|local|internal|home\.arpa|localdomain|intranet|corp|home|private)\b`)
)

// minNameLen keeps a one- or two-letter name from blocking every query.
const minNameLen = 3

// outgoingKinds returns what in text must not go to the internet (empty = nothing found).
func (w WebOptions) outgoingKinds(text string) []string {
	var kinds []string
	for _, m := range ipv4Re.FindAllString(text, -1) {
		if _, err := netip.ParseAddr(m); err == nil {
			kinds = append(kinds, "ip")
			break
		}
	}
	if len(kinds) == 0 {
		for _, m := range ipv6Re.FindAllString(text, -1) {
			if _, err := netip.ParseAddr(strings.Trim(m, ":.") + tail(m)); err == nil && strings.Count(m, ":") >= 2 {
				kinds = append(kinds, "ip")
				break
			}
		}
	}
	if outMACRe.MatchString(text) {
		kinds = append(kinds, "mac")
	}
	if emailRe.MatchString(text) {
		kinds = append(kinds, "email")
	}
	low := strings.ToLower(text)
	for _, n := range w.privateNames() {
		if containsWord(low, n) {
			kinds = append(kinds, "host")
			break
		}
	}
	if internalRe.MatchString(text) {
		kinds = append(kinds, "internal-name")
	}
	if _, n := Mask(text); n > 0 {
		kinds = append(kinds, "secret")
	}
	return kinds
}

// tail restores what a trim of ":" removed at the end of an IPv6 literal such as "fe80::".
func tail(m string) string {
	if strings.HasSuffix(m, "::") {
		return "::"
	}
	return ""
}

// privateNames are the lower-case names that are the operator's: tools.private_names, this machine's
// host name (short and fully qualified), its domain and the search domains of its resolver.
func (w WebOptions) privateNames() []string {
	seen := map[string]bool{}
	var out []string
	add := func(n string) {
		n = strings.ToLower(strings.Trim(strings.TrimSpace(n), "."))
		if len(n) >= minNameLen && !seen[n] {
			seen[n] = true
			out = append(out, n)
		}
	}
	for _, n := range w.PrivateNames {
		add(n)
	}
	own := w.SelfNames
	if own == nil {
		own = selfNames
	}
	for _, n := range own() {
		add(n)
	}
	return out
}

// selfNames reads the node's own names: the host name and its parts, and /etc/resolv.conf's
// search and domain entries (the operator's own domain, in practice).
func selfNames() []string {
	var out []string
	if h, err := os.Hostname(); err == nil && h != "" {
		out = append(out, h)
		if i := strings.IndexByte(h, '.'); i > 0 {
			out = append(out, h[:i], h[i+1:])
		}
	}
	if b, err := os.ReadFile("/etc/resolv.conf"); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			f := strings.Fields(l)
			if len(f) >= 2 && (f[0] == "search" || f[0] == "domain") {
				out = append(out, f[1:]...)
			}
		}
	}
	return out
}

// containsWord reports whether name occurs in text (both lower case) as a whole word: not inside a
// longer name (host-1 is not host-10, "box" is not "sandbox").
func containsWord(text, name string) bool {
	for from := 0; ; {
		i := strings.Index(text[from:], name)
		if i < 0 {
			return false
		}
		i += from
		end := i + len(name)
		if (i == 0 || !wordByte(text[i-1])) && (end == len(text) || !wordByte(text[end])) {
			return true
		}
		from = i + 1
	}
}

func wordByte(b byte) bool {
	return b >= 'a' && b <= 'z' || b >= '0' && b <= '9' || b == '_' || b == '-'
}

// checkOutgoing refuses text that holds private data; what is "query" or "URL".
func (w WebOptions) checkOutgoing(what, text string) error {
	if kinds := w.outgoingKinds(text); len(kinds) > 0 {
		return &PrivateDataError{What: what, Kinds: kinds}
	}
	return nil
}
