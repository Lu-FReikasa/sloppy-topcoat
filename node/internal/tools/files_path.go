package tools

import (
	"path"
	"regexp"
	"strings"
)

// Path rules of files.list and files.read (HOST-01, D89). Everything in this file is a pure
// function of strings, so the Windows rules are tested on Linux too; whether Windows resolves a
// short name or a junction the way these rules expect is UNVERIFIED on real hardware.

// secretNameGlobs mark a file or folder name as a secret. A path with a segment below the released
// root that matches one of them (case-insensitive) is refused even inside a root: not listed, not
// read, not reachable through a link. The list cannot be shortened by configuration.
var secretNameGlobs = []string{
	// keys and key stores
	"*.key", "*.pem", "*.pfx", "*.p12", "*.ppk", "*.jks", "*.keystore", "*.kdbx", "*.gpg", "*.ovpn",
	"id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
	// environment and credential files
	".env", ".env.*", "*.env", "*credential*", "*secret*", "*password*", "*passwd*", "*passphrase*",
	"*.token", ".netrc", "_netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
	// the node's own connectors file (credentials of Uptime Kuma, Omada, Keycloak)
	"connectors*",
	// the node's own configuration and identity (the data folder may be released for its logs)
	"config.json", "node.json",
	// account databases
	"shadow", "shadow-", "gshadow", "gshadow-", "ntds.dit", "sam",
	// browser profile files that hold logins, cookies and keys
	"logins.json", "key3.db", "key4.db", "cert9.db", "cookies", "cookies-journal", "cookies.sqlite",
	"login data", "login data for account", "web data", "local state",
}

// secretDirNames are folder names (exact, case-insensitive) that are closed as a whole: key and
// cloud-credential folders, browser profiles and the places Windows keeps its encrypted secrets.
var secretDirNames = map[string]bool{
	".ssh": true, ".gnupg": true, ".aws": true, ".azure": true, ".kube": true, ".docker": true, "gcloud": true,
	"wireguard": true, "keychains": true,
	// browser profiles
	"user data": true, "google-chrome": true, "chromium": true, ".mozilla": true, "mozilla": true,
	"firefox": true, "bravesoftware": true, "brave-browser": true, "opera software": true, "vivaldi": true,
	// Windows: DPAPI master keys, machine keys, the recycle bin and the volume information
	"protect": true, "crypto": true, "$recycle.bin": true, "system volume information": true,
}

// SecretName reports whether one path segment marks a secret.
func SecretName(segment string) bool {
	s := strings.ToLower(segment)
	if secretDirNames[s] {
		return true
	}
	for _, g := range secretNameGlobs {
		if ok, _ := path.Match(g, s); ok {
			return true
		}
	}
	return false
}

// secretBelow reports whether a segment below the root marks a secret.
func secretBelow(segs []string) bool {
	for _, s := range segs {
		if SecretName(s) {
			return true
		}
	}
	return false
}

// splitPath splits a path into segments without empty ones. On Windows both separators count.
func splitPath(p string, win bool) []string {
	return strings.FieldsFunc(p, func(r rune) bool { return r == '/' || (win && r == '\\') })
}

func sameSegment(a, b string, win bool) bool {
	if win {
		return strings.EqualFold(a, b)
	}
	return a == b
}

// segmentsBelow returns the segments of p below dir, or ok=false when p is not dir or below it.
// On Windows the comparison ignores case and takes both separators. Neither path may hold ".."
// (resolved paths do not; callers refuse it before).
func segmentsBelow(p, dir string, win bool) (below []string, ok bool) {
	ps, ds := splitPath(p, win), splitPath(dir, win)
	if len(ds) == 0 || len(ps) < len(ds) {
		return nil, false
	}
	for i := range ds {
		if !sameSegment(ps[i], ds[i], win) {
			return nil, false
		}
	}
	return ps[len(ds):], true
}

var (
	reservedWinName = regexp.MustCompile(`(?i)^(con|prn|aux|nul|conin\$|conout\$|com[0-9]|lpt[0-9])$`)
	shortWinName    = regexp.MustCompile(`^[^~]{1,8}~[0-9]+(\.[^.]{0,3})?$`)
	winBadChars     = `<>"|?*`
)

// windowsFormRefused says why a path, given in Windows form, is not accepted: not an absolute
// drive path (UNC, device paths such as \\?\ and \\.\, drive-relative and rooted paths), an alternate
// data stream, a reserved device name, characters Windows treats specially, trailing dots or
// spaces, short (8.3) names, "." and "..". Nil means the form is acceptable; it still has to
// resolve to a place inside a released folder.
func windowsFormRefused(p string) error {
	if p == "" || strings.ContainsRune(p, 0) {
		return inputErr("the path is empty or holds a NUL")
	}
	for _, r := range p {
		if r < 0x20 {
			return inputErr("the path holds a control character")
		}
	}
	norm := strings.ReplaceAll(p, "/", `\`)
	if strings.HasPrefix(norm, `\\`) {
		return inputErr("UNC and device paths are not accepted")
	}
	if len(norm) < 3 || norm[1] != ':' || norm[2] != '\\' ||
		!((norm[0] >= 'a' && norm[0] <= 'z') || (norm[0] >= 'A' && norm[0] <= 'Z')) {
		return inputErr("the path must be absolute and start with a drive letter such as C:\\")
	}
	rest := norm[3:]
	if strings.Contains(rest, ":") {
		return inputErr("alternate data streams are not accepted")
	}
	if strings.ContainsAny(rest, winBadChars) {
		return inputErr("the path holds characters Windows reserves (%s)", winBadChars)
	}
	for _, seg := range strings.Split(rest, `\`) {
		switch {
		case seg == "":
			continue // a doubled or trailing separator
		case seg == "." || seg == "..":
			return inputErr("'.' and '..' are not accepted")
		case strings.HasSuffix(seg, ".") || strings.HasSuffix(seg, " "):
			return inputErr("a name must not end with a dot or a space")
		}
		base := seg
		if i := strings.IndexByte(base, '.'); i >= 0 {
			base = base[:i]
		}
		if reservedWinName.MatchString(strings.TrimRight(base, " ")) {
			return inputErr("%q is a reserved device name", seg)
		}
		if shortWinName.MatchString(seg) {
			return inputErr("%q looks like a short (8.3) name; use the long name", seg)
		}
	}
	return nil
}

// posixFormRefused is the same for a Unix path: absolute, no NUL or control characters, no "..".
func posixFormRefused(p string) error {
	if p == "" || strings.ContainsRune(p, 0) {
		return inputErr("the path is empty or holds a NUL")
	}
	for _, r := range p {
		if r < 0x20 {
			return inputErr("the path holds a control character")
		}
	}
	if !strings.HasPrefix(p, "/") {
		return inputErr("the path must be absolute")
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == ".." {
			return inputErr("'..' is not accepted")
		}
	}
	return nil
}

// resolvedShortName reports whether a resolved Windows path still holds a short-name segment:
// the system did not expand it, so the real name (and with it the secret check) is unknown.
func resolvedShortName(segs []string) bool {
	for _, s := range segs {
		if shortWinName.MatchString(s) {
			return true
		}
	}
	return false
}
