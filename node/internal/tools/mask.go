// Package tools holds the read-only capabilities of a tool node (E2-04, D4, D15): what the
// assistant may look at on a server — containers, their logs, systemd units, the journal, disks,
// compose projects — and nothing else. There is no shell here: every capability is a fixed
// operation with a schema-checked input, and everything that leaves the machine has gone through
// the masking of this file and the size limits of limits.go.
package tools

import (
	"regexp"
	"strings"
	"unicode"
)

// Marker replaces a secret. It names the rule so a reader knows what was there, never the value.
func marker(rule string) string { return "«masked:" + rule + "»" }

// valueRules find secrets by their shape, wherever they stand (logs, compose files, command
// lines). They mirror the deterministic patterns of the hub's secret filter (hub
// memory_filter.py, D19); the two lists are kept in step by hand. The hub's "long token" rule is
// left out on purpose: in logs it would hide every container ID and image digest.
var valueRules = []struct {
	rule string
	re   *regexp.Regexp
	// group is the submatch that is the secret (0 = the whole match).
	group int
}{
	{"private_key", regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)`), 0},
	{"api_key", regexp.MustCompile(`\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}`), 0},
	{"api_key", regexp.MustCompile(`\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}`), 0},
	{"api_key", regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}`), 0},
	{"api_key", regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`), 0},
	{"api_key", regexp.MustCompile(`\bAIza[0-9A-Za-z_\-]{30,}`), 0},
	{"api_key", regexp.MustCompile(`\bxox[baprs]-[A-Za-z0-9\-]{10,}`), 0},
	{"api_key", regexp.MustCompile(`\btc_[A-Za-z0-9_\-]{20,}`), 0}, // this project's own API keys
	{"token", regexp.MustCompile(`\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}`), 0},
	{"token", regexp.MustCompile(`(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._~+/\-]{16,}=*)`), 1},
	// scheme://user:password@host — the password only
	{"url_credentials", regexp.MustCompile(`\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:([^\s@]+)@`), 1},
	// Password=…; / pwd=… in connection strings and --password=… on command lines
	{"password", regexp.MustCompile(`(?i)\b(?:password|passwd|pwd)\s*=\s*([^;&\s'"]+)`), 1},
	{"password", regexp.MustCompile(`(?i)--(?:password|passwd|token|secret|api-key|apikey)(?:=|\s+)([^\s'"]+)`), 1},
}

// keyLine matches "key = value", "key: value" and "- key=value" at the start of a line (.env files,
// compose environment sections). The whole rest of the line is the value, so a password with
// spaces goes away completely. Quoted values are left to inlinePair, which keeps the quotes and
// whatever follows them (the comma in pretty-printed JSON).
var keyLine = regexp.MustCompile(`^(\s*(?:-\s+)?(?:export\s+)?)("?)([A-Za-z0-9_.\-]+)("?)(\s*[=:]\s*)(.*)$`)

// inlinePair finds key=value, key: value and "key": "value" anywhere in a line: prefix, opening
// quote, key, closing quote, separator, value. The value is a quoted string, a backslash-escaped
// one (JSON inside a log line) or a run up to whitespace, quote, ';' or '&' (connection strings,
// URL queries).
var inlinePair = regexp.MustCompile(`(^|[^A-Za-z0-9_.\-])(\\*["']?)([A-Za-z0-9_.\-]+)(\\*["']?)(\s*[=:]\s*)` +
	`("(?:[^"\\]|\\.)*"|'[^']*'|\\+["']?[^\s"'\\;&]*|[^\s"';&]+|["'][^\s"']*)`)

// authSchemes can stand between an Authorization key and its credential; the credential is the
// secret, the scheme is useful to read.
var authSchemes = map[string]bool{"bearer": true, "basic": true, "token": true, "digest": true, "negotiate": true, "ntlm": true}

var (
	camelBoundary = regexp.MustCompile(`([a-z0-9])([A-Z])`)
	schemeToken   = regexp.MustCompile(`^\s+([^\s"';&]+)`)
)

// secretParts are key-name words that say the value is a secret when they stand alone
// (DB_PASS, apiPw, X-Auth); as substrings they would hide "passed", "author" or "n_tokens".
var secretParts = map[string]bool{
	"pass": true, "pw": true, "pwd": true, "auth": true, "authorization": true, "authorisation": true,
	"salt": true, "jwt": true, "dsn": true, "psk": true, "token": true,
}

// secretSubstrings are specific enough to count inside a longer name (OPENVPN_PASSWORD,
// WIREGUARD_PRIVATE_KEY, clientSecret); matched against the name without separators.
var secretSubstrings = []string{
	"password", "passwd", "passphrase", "secret", "credential", "privatekey", "apikey", "accesskey",
	"signingkey", "encryptionkey", "licensekey", "bearer", "cookie", "webhook", "connectionstring",
	"accesstoken", "refreshtoken", "authtoken", "apitoken", "idtoken", "bottoken",
}

func secretKey(key string) bool {
	parts := strings.FieldsFunc(strings.ToLower(camelBoundary.ReplaceAllString(key, "${1}_${2}")), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	})
	for _, p := range parts {
		if secretParts[p] {
			return true
		}
	}
	joined := strings.Join(parts, "")
	for _, w := range secretSubstrings {
		if strings.Contains(joined, w) {
			return true
		}
	}
	return false
}

// harmlessValue says a value of a secret-looking key is no secret: empty, a reference to another
// variable (compose's ${DB_PASSWORD}), null, a block scalar indicator or a value that is already
// masked. Booleans and numbers are not on this list: a password may be "true" or "1234".
func harmlessValue(v string) bool {
	v = strings.TrimSpace(v)
	v = strings.Trim(v, `"'`)
	switch strings.ToLower(v) {
	case "", "null", "~", "|", ">", "|-", ">-", "|+", ">+", "[]", "{}":
		return true
	}
	if strings.HasPrefix(v, "${") && strings.HasSuffix(v, "}") && strings.Count(v, "${") == 1 {
		return true
	}
	if strings.HasPrefix(v, "$") && !strings.ContainsAny(v[1:], " /:=") {
		return true
	}
	return strings.HasPrefix(v, "«masked:")
}

// maskInline masks the values of secret keys anywhere in one line. A pair whose key is no secret
// is not skipped as a whole: its value may hold more pairs (a JSON document inside a log field).
func maskInline(line string, n *int) string {
	var b strings.Builder
	last, pos := 0, 0
	for pos < len(line) {
		m := inlinePair.FindStringSubmatchIndex(line[pos:])
		if m == nil {
			break
		}
		for i := range m {
			if m[i] >= 0 {
				m[i] += pos
			}
		}
		if !secretKey(line[m[6]:m[7]]) {
			pos = m[11] // behind the separator
			continue
		}
		vs, ve := m[12], m[13]
		val := line[vs:ve]
		if authSchemes[strings.ToLower(val)] {
			if t := schemeToken.FindStringSubmatchIndex(line[ve:]); t != nil {
				vs, ve = ve+t[2], ve+t[3]
				val = line[vs:ve]
			}
		}
		pos = ve
		if harmlessValue(val) {
			continue
		}
		q := ""
		if val[0] == '"' || val[0] == '\'' {
			q = val[:1]
		}
		rep := q + marker("secret_key")
		if len(val) >= 2 && q != "" && val[len(val)-1] == val[0] {
			rep += q
		}
		b.WriteString(line[last:vs])
		b.WriteString(rep)
		last = ve
		*n++
	}
	if last == 0 {
		return line
	}
	b.WriteString(line[last:])
	return b.String()
}

// Mask replaces the secrets in text and returns the result with the number of replacements.
// Text that contains none comes back unchanged. The rules are deliberately on the cautious side:
// a wrongly hidden value costs a question, a leaked one costs much more. The order is: secrets
// by shape (whole text), then the value of every secret-looking key, whatever the value looks like.
func Mask(text string) (string, int) {
	n := 0
	// shape-based rules over the whole text (a PEM block spans lines)
	for _, r := range valueRules {
		text = r.re.ReplaceAllStringFunc(text, func(m string) string {
			if r.group == 0 {
				n++
				return marker(r.rule)
			}
			loc := r.re.FindStringSubmatchIndex(m)
			if len(loc) < 2*r.group+2 || loc[2*r.group] < 0 {
				return m
			}
			secret := m[loc[2*r.group]:loc[2*r.group+1]]
			if secret == "" || harmlessValue(secret) {
				return m
			}
			n++
			return m[:loc[2*r.group]] + marker(r.rule) + m[loc[2*r.group+1]:]
		})
	}
	// key-based rules, line by line
	lines := strings.Split(text, "\n")
	for i, l := range lines {
		if m := keyLine.FindStringSubmatch(l); m != nil && secretKey(m[3]) && !harmlessValue(m[6]) && !strings.HasPrefix(strings.TrimSpace(m[6]), "\"") && !strings.HasPrefix(strings.TrimSpace(m[6]), "'") {
			scheme := ""
			rest := m[6]
			if f := strings.Fields(rest); len(f) > 1 && authSchemes[strings.ToLower(f[0])] {
				scheme = f[0] + " "
				rest = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(rest), f[0]))
			}
			if !harmlessValue(rest) {
				lines[i] = m[1] + m[2] + m[3] + m[4] + m[5] + scheme + marker("secret_key")
				n++
				continue
			}
		}
		if strings.ContainsAny(l, "=:") {
			lines[i] = maskInline(l, &n)
		}
	}
	return strings.Join(lines, "\n"), n
}
