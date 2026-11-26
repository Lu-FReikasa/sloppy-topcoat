package runner

import (
	"path/filepath"
	"sort"
	"strings"
)

// Vision projectors (D125): a model that can see is two files, the language model and its
// projector (a GGUF file of the architecture "clip", by convention with "mmproj" in its name).
// llama-server loads both with -m <model> --mmproj <projector>. The scan does not list projectors
// as models; it finds the projector of each model here, in the model's own directory only:
//
//  1. the per-model setting runner.projectors (a file name, or "none");
//  2. by name: mmproj-<model>.gguf, <model>.mmproj.gguf or <model>-mmproj*.gguf;
//  3. the only projector of a subfolder (the layout of a downloaded repository, where the
//     projector is called e.g. mmproj-F16.gguf). With several there, F16 is preferred, then BF16,
//     then Q8_0, then the first by name. Never in the top of a models directory: there a lone
//     projector may belong to any of the models next to it.

// isProjector reports whether a file name is a vision projector.
func isProjector(file string) bool {
	return strings.EqualFold(filepath.Ext(file), ".gguf") && strings.Contains(strings.ToLower(file), "mmproj")
}

// projectorFor returns the path of the projector for the model name in its directory dir, or ""
// for none. files are the projector file names in dir; setting is the runner.projectors entry
// ("" = none set); subfolder is whether dir lies below a scanned models directory.
func projectorFor(name, dir string, files []string, setting string, subfolder bool) string {
	if setting == "none" {
		return ""
	}
	if setting != "" {
		for _, f := range files {
			if strings.EqualFold(f, setting) {
				return filepath.Join(dir, f)
			}
		}
		return "" // the named file is not there: no projector rather than a wrong one
	}
	low := strings.ToLower(name)
	for _, f := range files {
		base := strings.ToLower(strings.TrimSuffix(f, filepath.Ext(f)))
		if base == "mmproj-"+low || base == low+".mmproj" || strings.HasPrefix(base, low+"-mmproj") {
			return filepath.Join(dir, f)
		}
	}
	if !subfolder || len(files) == 0 {
		return ""
	}
	sorted := append([]string(nil), files...)
	sort.Strings(sorted)
	for _, quant := range []string{"f16", "bf16", "q8_0"} {
		for _, f := range sorted {
			base := strings.ToLower(strings.TrimSuffix(f, filepath.Ext(f)))
			// "-f16" does not match "-bf16": the separator must come right before the quant
			if strings.HasSuffix(base, "-"+quant) || strings.HasSuffix(base, "_"+quant) || strings.HasSuffix(base, "."+quant) {
				return filepath.Join(dir, f)
			}
		}
	}
	return filepath.Join(dir, sorted[0])
}

// baseOrEmpty is the file name of path, "" for "" (filepath.Base would give ".").
func baseOrEmpty(path string) string {
	if path == "" {
		return ""
	}
	return filepath.Base(path)
}
