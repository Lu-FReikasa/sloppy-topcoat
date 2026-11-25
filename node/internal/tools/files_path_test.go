package tools

import (
	"strings"
	"testing"
)

// The Windows rules are string functions, so they are tested here on Linux. What Windows itself
// does with such paths (junctions, short names, case) is UNVERIFIED on real hardware; the steps for
// the maintainer are in docs/INSTALL-NODE.md ("Reading files of a host").

func TestWindowsFormRefusesWhatCanLeaveAFolder(t *testing.T) {
	for name, p := range map[string]string{
		"unc":                `\\server\share\a.log`,
		"unc slashes":        `//server/share/a.log`,
		"device qmark":       `\\?\C:\ProgramData\a.log`,
		"device dot":         `\\.\PhysicalDrive0`,
		"device nt":          `\\?\GLOBALROOT\Device\HarddiskVolume1\a.log`,
		"stream":             `C:\ProgramData\a.log:secret`,
		"stream default":     `C:\ProgramData\a.log::$DATA`,
		"drive relative":     `C:a.log`,
		"rooted no drive":    `\ProgramData\a.log`,
		"relative":           `ProgramData\a.log`,
		"dotdot":             `C:\ProgramData\..\Users\x`,
		"dotdot slash":       `C:/ProgramData/../Users/x`,
		"dot":                `C:\ProgramData\.\a.log`,
		"con":                `C:\ProgramData\CON`,
		"con with extension": `C:\ProgramData\con.txt`,
		"nul lower":          `C:\ProgramData\nul`,
		"aux":                `C:\ProgramData\AUX.log`,
		"com1":               `C:\ProgramData\COM1`,
		"lpt9":               `C:\ProgramData\lpt9.txt`,
		"conout":             `C:\ProgramData\CONOUT$`,
		"trailing dot":       `C:\ProgramData\a.log.`,
		"trailing space":     `C:\ProgramData\a.log `,
		"folder dot":         `C:\ProgramData\logs.\a.log`,
		"wildcard":           `C:\ProgramData\*.log`,
		"question":           `C:\ProgramData\a?.log`,
		"pipe":               `C:\ProgramData\a|b`,
		"short name":         `C:\PROGRA~1\Topcoat\a.log`,
		"short name file":    `C:\ProgramData\CREDEN~1.JSO`,
		"nul byte":           "C:\\ProgramData\\a.log\x00.txt",
		"control":            "C:\\ProgramData\\a\x01.log",
		"empty":              ``,
	} {
		if err := windowsFormRefused(p); err == nil {
			t.Errorf("%s: %q is accepted", name, p)
		}
	}
	for _, ok := range []string{
		`C:\ProgramData\Topcoat\logs\node.log`,
		`c:/ProgramData/Topcoat/node.log`,
		`D:\Logs\app (1).log`,
		`C:\ProgramData\console.log`, // starts like CON but is not the device
		`C:\ProgramData\Program Files (x86)\a.txt`,
	} {
		if err := windowsFormRefused(ok); err != nil {
			t.Errorf("%q refused: %v", ok, err)
		}
	}
}

func TestPosixFormRefusesWhatCanLeaveAFolder(t *testing.T) {
	for _, p := range []string{"", "relative/a.log", "/var/log/../../etc/passwd", "/var/log/a\x00.log", "/var/log/a\n.log"} {
		if err := posixFormRefused(p); err == nil {
			t.Errorf("%q is accepted", p)
		}
	}
	for _, p := range []string{"/var/log/syslog", "/var/log/a:b.log", `/var/log/back\slash.log`, "/var/log/..hidden"} {
		if err := posixFormRefused(p); err != nil {
			t.Errorf("%q refused: %v", p, err)
		}
	}
}

func TestSegmentsBelowCompareLikeTheOS(t *testing.T) {
	// Windows: case-insensitive, both separators, no prefix confusion, trailing separator on the root
	for _, tc := range []struct {
		path, dir string
		win, want bool
	}{
		{`C:\ProgramData\Topcoat\a.log`, `C:\ProgramData`, true, true},
		{`c:\programdata\topcoat\a.log`, `C:\ProgramData`, true, true},
		{`C:/PROGRAMDATA/x`, `C:\ProgramData\`, true, true},
		{`C:\ProgramData`, `C:\ProgramData`, true, true},
		{`C:\ProgramDataEvil\a.log`, `C:\ProgramData`, true, false},
		{`D:\ProgramData\a.log`, `C:\ProgramData`, true, false},
		{`C:\Users\a.log`, `C:\ProgramData`, true, false},
		{`C:\`, `C:\ProgramData`, true, false},
		// Linux: case matters
		{"/var/log/a.log", "/var/log", false, true},
		{"/var/Log/a.log", "/var/log", false, false},
		{"/var/logs/a.log", "/var/log", false, false},
		{"/var/log/a.log", "/var/log/", false, true},
		{"/var/log", "/var/log", false, true},
		{"/var", "/var/log", false, false},
	} {
		below, ok := segmentsBelow(tc.path, tc.dir, tc.win)
		if ok != tc.want {
			t.Errorf("segmentsBelow(%q, %q, win=%v) = %v %v, want %v", tc.path, tc.dir, tc.win, below, ok, tc.want)
		}
	}
	if below, _ := segmentsBelow(`C:\ProgramData\Topcoat\logs\a.log`, `c:\programdata`, true); strings.Join(below, "/") != "Topcoat/logs/a.log" {
		t.Errorf("below = %v", below)
	}
}

func TestSecretNamesAreMatchedIgnoringCaseOnEverySegment(t *testing.T) {
	win := func(p string) bool {
		below, _ := segmentsBelow(p, `C:\ProgramData`, true)
		return secretBelow(below)
	}
	for _, p := range []string{
		`C:\ProgramData\app\SERVER.KEY`,
		`C:\ProgramData\app\Credentials\x.txt`,
		`C:\ProgramData\Microsoft\Crypto\RSA\a.txt`,
		`C:\ProgramData\Microsoft\Protect\S-1-5\a.txt`,
		`C:\ProgramData\Google\Chrome\User Data\Default\Login Data`,
		`c:\programdata\x\.ENV`,
		`C:\ProgramData\x\ID_RSA`,
	} {
		if !win(p) {
			t.Errorf("%s is not recognized as a secret", p)
		}
	}
	for _, p := range []string{`C:\ProgramData\Topcoat\logs\node.log`, `C:\ProgramData\Docker\service.log`} {
		if win(p) {
			t.Errorf("%s is refused", p)
		}
	}
	// the root itself may carry any name: only what is below it counts
	if below, ok := segmentsBelow(`C:\ProgramData\credentials-docs\a.txt`, `C:\ProgramData\credentials-docs`, true); !ok || secretBelow(below) {
		t.Error("the released folder's own name must not count")
	}
}

func TestAShortNameLeftInAResolvedPathIsRefused(t *testing.T) {
	if !resolvedShortName([]string{"Topcoat", "PROGRA~1"}) || resolvedShortName([]string{"Topcoat", "logs", "node.log"}) {
		t.Fatal("short name detection")
	}
}
