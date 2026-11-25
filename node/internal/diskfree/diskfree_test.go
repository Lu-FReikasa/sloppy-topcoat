package diskfree

import (
	"path/filepath"
	"runtime"
	"testing"
)

func TestOfMeasuresTheDriveAndFallsBackToAnExistingParent(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "windows" {
		t.Skip("not measured on this platform")
	}
	dir := t.TempDir()
	u, err := Of(dir)
	if err != nil {
		t.Fatal(err)
	}
	if u.TotalBytes <= 0 || u.FreeBytes < 0 || u.FreeBytes > u.TotalBytes {
		t.Fatalf("%+v", u)
	}
	// the model directory does not exist before the first download
	u2, err := Of(filepath.Join(dir, "models", "not", "yet"))
	if err != nil || u2.TotalBytes != u.TotalBytes {
		t.Fatalf("%+v %v", u2, err)
	}
}
