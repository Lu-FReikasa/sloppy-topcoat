package buildsvc

import "regexp"

var secretPatterns = []struct {
	re   *regexp.Regexp
	keep int
}{
	{regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)`), 0},
	{regexp.MustCompile(`\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}`), 0},
	{regexp.MustCompile(`\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}`), 0},
	{regexp.MustCompile(`(?i)\b(bearer|token|authorization)(\s*[:=]\s*|\s+)[A-Za-z0-9._~+/=-]{16,}`), 2},
	{regexp.MustCompile(`(?i)\b(password|passwd|secret|api[_-]?key)(\s*[:=]\s*)\S{6,}`), 2},
	{regexp.MustCompile(`topcoat-update-key-v1 [A-Za-z0-9+/=]{40,}`), 0},
	{regexp.MustCompile(`https://[^\s/:@]+:[^\s/@]+@`), 0},
}

// Mask replaces everything in a build log that looks like a key, a token or a password. A log
// leaves the machine (to the hub, to a page), so it is cleaned here first.
func Mask(s string) string {
	for _, p := range secretPatterns {
		keep := p.keep
		s = p.re.ReplaceAllStringFunc(s, func(m string) string {
			if keep == 0 {
				return "[masked]"
			}
			sub := p.re.FindStringSubmatch(m)
			return sub[1] + sub[2] + "[masked]"
		})
	}
	return s
}
