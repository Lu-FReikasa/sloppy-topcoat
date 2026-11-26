package runner

import (
	"context"
	"path/filepath"
	"slices"
	"testing"
)

func TestProjectorMatching(t *testing.T) {
	dir := filepath.Join("m", "repo")
	cases := []struct {
		name, setting string
		files         []string
		subfolder     bool
		want          string
	}{
		{"qwen-vl-q4", "", []string{"mmproj-qwen-vl-q4.gguf", "mmproj-F16.gguf"}, false, "mmproj-qwen-vl-q4.gguf"},
		{"qwen-vl-q4", "", []string{"QWEN-VL-Q4.mmproj.gguf"}, false, "QWEN-VL-Q4.mmproj.gguf"},
		{"qwen-vl-q4", "", []string{"qwen-vl-q4-mmproj-f16.gguf"}, false, "qwen-vl-q4-mmproj-f16.gguf"},
		// a lone projector in the top of a models dir may belong to any model next to it
		{"gemma-q4", "", []string{"mmproj-F16.gguf"}, false, ""},
		// in a subfolder (a downloaded repository) it is the model's
		{"gemma-q4", "", []string{"mmproj-F16.gguf"}, true, "mmproj-F16.gguf"},
		// several there: F16 before BF16 before F32, regardless of the order on disk
		{"gemma-q4", "", []string{"mmproj-BF16.gguf", "mmproj-F32.gguf", "mmproj-F16.gguf"}, true, "mmproj-F16.gguf"},
		{"gemma-q4", "", []string{"mmproj-F32.gguf", "mmproj-BF16.gguf"}, true, "mmproj-BF16.gguf"},
		{"gemma-q4", "", []string{"mmproj-z.gguf", "mmproj-a.gguf"}, true, "mmproj-a.gguf"},
		// the setting wins; "none" switches it off; a named file that is not there gives none
		{"gemma-q4", "mmproj-F32.gguf", []string{"mmproj-F16.gguf", "mmproj-F32.gguf"}, true, "mmproj-F32.gguf"},
		{"gemma-q4", "none", []string{"mmproj-gemma-q4.gguf"}, true, ""},
		{"gemma-q4", "missing.gguf", []string{"mmproj-F16.gguf"}, true, ""},
		{"gemma-q4", "", nil, true, ""},
	}
	for _, c := range cases {
		got := projectorFor(c.name, dir, c.files, c.setting, c.subfolder)
		want := ""
		if c.want != "" {
			want = filepath.Join(dir, c.want)
		}
		if got != want {
			t.Errorf("%s %q %v sub=%v: got %q, want %q", c.name, c.setting, c.files, c.subfolder, got, want)
		}
	}
}

func TestAVisionModelIsListedAndLoadedWithItsProjector(t *testing.T) {
	fs := &fakeStarter{log: cudaLog}
	l, data := newTestServer(t, "cuda", fs, func(o *LlamaServerOptions) {
		o.GPU = &fixedGPU{}
		o.Projectors = map[string]string{"plain": "none"}
	})
	repo := filepath.Join(data, "models", "unsloth", "gemma-vl")
	writeModel(t, repo, "gemma-vl-q4.gguf", 10)
	writeModel(t, repo, "mmproj-F16.gguf", 3)
	writeModel(t, repo, "mmproj-F32.gguf", 5)
	writeModel(t, filepath.Join(data, "models"), "plain.gguf", 10)
	writeModel(t, filepath.Join(data, "models"), "mmproj-plain.gguf", 3) // switched off by the setting
	ctx := context.Background()

	models, err := l.ListModels(ctx)
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, m := range models {
		got[m.Name] = m.Projector
	}
	if len(got) != 2 || got["gemma-vl-q4"] != "mmproj-F16.gguf" || got["plain"] != "" {
		t.Fatalf("projectors are no models, and each model names its own: %v", got)
	}

	if _, err := l.Load(ctx, "gemma-vl-q4", LoadOptions{ContextLength: 4096}); err != nil {
		t.Fatal(err)
	}
	if p, ok := argOf(fs.started[0].args, "--mmproj"); !ok || p != filepath.Join(repo, "mmproj-F16.gguf") {
		t.Fatalf("--mmproj %q %v in %v", p, ok, fs.started[0].args)
	}
	if _, err := l.Load(ctx, "plain", LoadOptions{ContextLength: 4096}); err != nil {
		t.Fatal(err)
	}
	if slices.Contains(fs.started[1].args, "--mmproj") {
		t.Fatalf("a model without a projector gets none: %v", fs.started[1].args)
	}
}
