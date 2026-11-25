//go:build windows

package tools

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// UNVERIFIED on real hardware: these tests were written without a Windows machine and only
// cross-compiled (GOOS=windows go vet). They are the first thing to run on node-amd:
//
//	cd node; go test ./internal/tools -run WindowsFiles -v

func winRoot(t *testing.T) (root, outside string) {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	root, outside = filepath.Join(base, "Logs"), filepath.Join(base, "Private")
	for _, d := range []string{root, outside} {
		if err := os.MkdirAll(d, 0o750); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "Node Service.log"), []byte("ok\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outside, "diary.txt"), []byte("no\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return root, outside
}

func TestWindowsFilesCaseDifferencesAndSeparators(t *testing.T) {
	root, _ := winRoot(t)
	o := filesOpts(root)
	for _, p := range []string{
		filepath.Join(root, "Node Service.log"),
		strings.ToLower(filepath.Join(root, "Node Service.log")),
		strings.ToUpper(filepath.Join(root, "Node Service.log")),
		strings.ReplaceAll(filepath.Join(root, "Node Service.log"), `\`, "/"),
	} {
		if _, err := readFile(t, o, pathArg(p)); err != nil {
			t.Errorf("%q: %v", p, err)
		}
	}
	if _, err := readFile(t, filesOpts(strings.ToLower(root)+`\`), pathArg(filepath.Join(root, "Node Service.log"))); err != nil {
		t.Errorf("root in other case with a trailing separator: %v", err)
	}
}

func TestWindowsFilesRefuseJunctionsShortNamesStreamsDevicesAndUNC(t *testing.T) {
	root, outside := winRoot(t)
	o := filesOpts(root)
	junction := filepath.Join(root, "out")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", junction, outside).CombinedOutput(); err != nil {
		t.Fatalf("mklink /J: %v: %s", err, out)
	}
	_, err := readFile(t, o, pathArg(filepath.Join(junction, "diary.txt")))
	wantRefused(t, err, "junction out of the root")
	_, err = invoke(t, o, "files.list", pathArg(junction))
	wantRefused(t, err, "list a junction out of the root")
	if lst, err := invoke(t, o, "files.list", pathArg(root)); err != nil || strings.Contains(strings.ToLower(mustJSON(lst)), `"out"`) {
		t.Fatalf("a junction out of the root is listed: %v %v", lst, err)
	}

	long := filepath.Join(root, "Node Service.log")
	buf := make([]uint16, 300)
	utf, _ := windows.UTF16PtrFromString(long)
	if n, err := windows.GetShortPathName(utf, &buf[0], uint32(len(buf))); err == nil && n > 0 {
		short := windows.UTF16ToString(buf[:n])
		if strings.Contains(short, "~") {
			_, err = readFile(t, o, pathArg(short))
			wantRefused(t, err, "short name "+short)
		}
	} else {
		t.Log("the volume has no short names; that case is not exercised")
	}

	for name, p := range map[string]string{
		"stream":   long + ":hidden",
		"data":     long + "::$DATA",
		"device":   `\\.\C:` + strings.TrimPrefix(long, filepath.VolumeName(long)),
		"qmark":    `\\?\` + long,
		"unc":      `\\localhost\` + strings.Replace(strings.TrimPrefix(long, filepath.VolumeName(long)), `\`, `$\`, 1),
		"nul":      filepath.Join(root, "NUL"),
		"com1":     filepath.Join(root, "COM1.txt"),
		"dotdot":   filepath.Join(root, "..", "Private", "diary.txt"),
		"trailing": long + ".",
	} {
		_, err := readFile(t, o, pathArg(p))
		wantRefused(t, err, name)
	}
}

func TestWindowsFilesRefuseSecretsWhateverTheCase(t *testing.T) {
	root, _ := winRoot(t)
	for _, name := range []string{"Server.KEY", "CREDENTIALS.json", ".Env", "ID_RSA"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
		_, err := readFile(t, filesOpts(root), pathArg(strings.ToLower(filepath.Join(root, name))))
		wantRefused(t, err, name)
	}
}
