package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"topcoat/node/internal/buildsvc"
)

const testBuild = "11111111-2222-4333-8444-555555555555"

func upload(work, name string) buildsvc.Upload {
	return buildsvc.Upload{BuildID: testBuild, Name: name, OS: "linux", Arch: "amd64",
		Path: filepath.Join(work, "builds", testBuild, "out", name)}
}

func writeArtifact(t *testing.T, work, name, content string) {
	t.Helper()
	dir := filepath.Join(work, "builds", testBuild, "out")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestACleanArtifactOfTheBuildMayBeUploaded(t *testing.T) {
	work := t.TempDir()
	// the temp dir may sit behind a link of its own (macOS); the check wants the real path
	work, _ = filepath.EvalSymlinks(work)
	writeArtifact(t, work, "topcoat-node-linux-amd64", "program")
	path, size, err := checkUpload(work, upload(work, "topcoat-node-linux-amd64"))
	if err != nil || size != 7 || !strings.HasSuffix(path, "topcoat-node-linux-amd64") {
		t.Fatalf("path=%q size=%d err=%v", path, size, err)
	}
}

func TestOnlyTheFilesOfTheBuildAreSent(t *testing.T) {
	work, _ := filepath.EvalSymlinks(t.TempDir())
	writeArtifact(t, work, "topcoat-node-linux-amd64", "program")
	writeArtifact(t, work, "SHA256SUMS", "x")
	secret := filepath.Join(work, "secret")
	if err := os.WriteFile(secret, []byte("topsecret"), 0o600); err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "topcoat-node-linux-amd64")
	if err := os.WriteFile(outside, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(work, "builds", testBuild, "out", "topcoat-node-windows-amd64.exe")
	if err := os.Symlink(secret, link); err != nil {
		t.Skip("no symlinks here")
	}
	dirLink := filepath.Join(work, "builds", testBuild, "linked")
	if err := os.Symlink(filepath.Dir(outside), dirLink); err != nil {
		t.Fatal(err)
	}
	empty := filepath.Join(work, "builds", testBuild, "out", "topcoat-node-linux-arm64")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	bad := map[string]buildsvc.Upload{
		"another name":     {BuildID: testBuild, Name: "SHA256SUMS", Path: filepath.Join(work, "builds", testBuild, "out", "SHA256SUMS")},
		"a name with a ..": {BuildID: testBuild, Name: "../topcoat-node-linux-amd64", Path: outside},
		"a bad build id":   {BuildID: "../x", Name: "topcoat-node-linux-amd64", Path: outside},
		"outside the work": {BuildID: testBuild, Name: "topcoat-node-linux-amd64", Path: outside},
		"another build":    {BuildID: "22222222-2222-4333-8444-555555555555", Name: "topcoat-node-linux-amd64", Path: filepath.Join(work, "builds", testBuild, "out", "topcoat-node-linux-amd64")},
		"a file link":      {BuildID: testBuild, Name: "topcoat-node-windows-amd64.exe", Path: link},
		"a directory link": {BuildID: testBuild, Name: "topcoat-node-linux-amd64", Path: filepath.Join(dirLink, "topcoat-node-linux-amd64")},
		"an empty file":    {BuildID: testBuild, Name: "topcoat-node-linux-arm64", Path: empty},
		"a missing file":   {BuildID: testBuild, Name: "topcoat-node-linux-arm64.exe", Path: filepath.Join(work, "builds", testBuild, "out", "topcoat-node-linux-arm64.exe")},
		"a dotted path":    {BuildID: testBuild, Name: "topcoat-node-linux-amd64", Path: filepath.Join(work, "builds", testBuild, "out", "..", "..", "..", "secret")},
	}
	for what, u := range bad {
		if _, _, err := checkUpload(work, u); err == nil {
			t.Errorf("%s was accepted", what)
		}
	}
}
