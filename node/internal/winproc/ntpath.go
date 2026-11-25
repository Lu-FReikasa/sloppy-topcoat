package winproc

import "strings"

// NTToDosPath converts an NT image path as returned by
// NtQuerySystemInformation(SystemProcessIdInformation), e.g.
// \Device\HarddiskVolume3\Games\x.exe, into the Win32 form C:\Games\x.exe
// using the drive mapping from QueryDosDevice ("C:" -> "\Device\HarddiskVolume3").
// Network paths (\Device\Mup\server\share\...) become \\server\share\....
// Unknown devices return "" (the process keeps its name-only match).
func NTToDosPath(nt string, drives map[string]string) string {
	if strings.HasPrefix(nt, `\??\`) {
		return nt[4:]
	}
	if rest, ok := cutPrefixFold(nt, `\Device\Mup\`); ok {
		return `\\` + rest
	}
	best, bestLen := "", 0
	for letter, device := range drives {
		if rest, ok := cutPrefixFold(nt, device); ok && (rest == "" || rest[0] == '\\') && len(device) > bestLen {
			best, bestLen = letter+rest, len(device)
		}
	}
	return best
}

func cutPrefixFold(s, prefix string) (string, bool) {
	if len(s) < len(prefix) || !strings.EqualFold(s[:len(prefix)], prefix) {
		return "", false
	}
	return s[len(prefix):], true
}
