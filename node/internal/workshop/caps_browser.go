package workshop

import (
	"context"
	"encoding/json"
	"regexp"
	"slices"
	"strings"

	"topcoat/node/internal/providers/capability"
	"topcoat/node/internal/tools"
)

// The browser.* capabilities (E6-02). Every one is a fixed operation with a schema-checked input;
// the model never sends JavaScript or a command line. All of them are scratch capabilities: they
// change nothing outside the throw-away container, so the hub runs them for an admin without a click.

var (
	browserRefRe  = regexp.MustCompile(`^e[0-9]{1,6}$`)
	browserPageRe = regexp.MustCompile(`^/[A-Za-z0-9._~%/#?=&-]{0,199}$`)

	auditChecks = []string{
		"page_scroll", "inner_scroll", "clipped_text", "overlap", "target_size", "font_size",
		"contrast", "focus", "reduced_motion",
	}
	animProps = []string{
		"opacity", "transform", "width", "height", "top", "left", "color", "background-color", "box-shadow",
	}
)

const (
	minViewport = 200
	maxViewport = 4000
	maxMatrix   = 40
)

func (w *Workshop) browserCaps() []capability.Capability {
	return []capability.Capability{
		w.capBrowserOpen(), w.capBrowserViewport(), w.capBrowserSnapshot(), w.capBrowserAct(),
		w.capBrowserAudit(), w.capBrowserAnimations(), w.capBrowserConsole(), w.capBrowserShot(),
		w.capBrowserClose(),
	}
}

// browserOp forwards a checked input to the helper and bounds and masks the answer.
func (w *Workshop) browserOp(ctx context.Context, op string, args any) (any, error) {
	raw, err := json.Marshal(args)
	if err != nil {
		return nil, err
	}
	out, err := w.callSession(ctx, op, raw)
	if err != nil {
		return nil, err
	}
	// a secret-looking value in the page's text never leaves the node
	if masked, n := tools.Mask(string(out)); n > 0 && json.Valid([]byte(masked)) {
		out = json.RawMessage(masked)
	}
	return out, nil
}

func inRange(name string, v, lo, hi int) error {
	if v < lo || v > hi {
		return inputErr("%s must be %d..%d", name, lo, hi)
	}
	return nil
}

func oneOf(name, v string, allowed ...string) error {
	if !slices.Contains(allowed, v) {
		return inputErr("%s must be one of %s", name, strings.Join(allowed, ", "))
	}
	return nil
}

func checkRef(name, v string) error {
	if !browserRefRe.MatchString(v) {
		return inputErr("%s must be a ref from browser.snapshot (like e12)", name)
	}
	return nil
}

// --- browser.open / browser.close ------------------------------------------------------------------

func (w *Workshop) capBrowserOpen() capability.Capability {
	return wcap{
		name: "browser.open",
		desc: "Open a headless browser on the repository's GUI: a copy of the worktree is served " +
			"(mode built = the built GUI, mode dev = the dev server) together with a faked hub, in a " +
			"throw-away container with no network. One session per node; close it when done. Then use " +
			"browser.snapshot, browser.act, browser.audit and browser.animations. With theme (a theme " +
			"document, docs/THEMES.md) the faked hub offers it and has it chosen, and the window desktop " +
			"is drawn in it; a worktree without a built GUI is then shown with the GUI of the image.",
		schema: `{"type":"object","additionalProperties":false,"required":["repo"],"properties":{` + repoProp +
			`,"mode":{"enum":["built","dev"]},"path":{"type":"string","maxLength":200},"theme":{"type":"object"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Repo  string          `json:"repo"`
				Mode  string          `json:"mode"`
				Path  string          `json:"path"`
				Theme json.RawMessage `json:"theme"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Mode == "" {
				in.Mode = "built"
			}
			if err := oneOf("mode", in.Mode, "built", "dev"); err != nil {
				return nil, err
			}
			if in.Path == "" {
				in.Path = "/"
			}
			if !browserPageRe.MatchString(in.Path) || strings.Contains(in.Path, "..") {
				return nil, inputErr("path must be a page of the GUI that starts with /")
			}
			theme, err := browserTheme(in.Theme)
			if err != nil {
				return nil, err
			}
			return w.openSession(ctx, in.Repo, in.Mode, in.Path, theme)
		},
	}
}

// browserThemeBytes bounds a theme document (the hub allows 40 000 characters of style sheet).
const browserThemeBytes = 64 << 10

var browserThemeKeyRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,39}$`)

// browserTheme checks the little the node needs of a theme: an object with a key, not too large.
// The rest is data for the page in a container without network; the hub checks a theme fully.
func browserTheme(raw json.RawMessage) ([]byte, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	if len(raw) > browserThemeBytes {
		return nil, inputErr("theme is larger than %d KiB", browserThemeBytes>>10)
	}
	var t struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(raw, &t); err != nil {
		return nil, inputErr("theme must be a theme document (an object)")
	}
	if !browserThemeKeyRe.MatchString(t.Key) {
		return nil, inputErr("theme needs a key (lower case letters, digits, dashes)")
	}
	return raw, nil
}

func (w *Workshop) capBrowserClose() capability.Capability {
	return wcap{
		name:   "browser.close",
		desc:   "End the browser session and remove its container and files.",
		schema: `{"type":"object","additionalProperties":false,"properties":{}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct{}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if err := w.closeSession(""); err != nil {
				return nil, err
			}
			return map[string]any{"closed": true}, nil
		},
	}
}

// --- the operations on an open session ---------------------------------------------------------

func (w *Workshop) capBrowserViewport() capability.Capability {
	return wcap{
		name: "browser.viewport",
		desc: "Set the size of the page: width and height in CSS pixels, optionally the pixel ratio, " +
			"orientation, color scheme (light or dark) and reduced motion. Returns the effective viewport.",
		schema: `{"type":"object","additionalProperties":false,"required":["width","height"],"properties":{` +
			`"width":{"type":"integer","minimum":200,"maximum":4000},"height":{"type":"integer","minimum":200,"maximum":4000},` +
			`"dpr":{"type":"number","minimum":1,"maximum":3},"orientation":{"enum":["portrait","landscape"]},` +
			`"scheme":{"enum":["light","dark"]},"reduced_motion":{"type":"boolean"}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Width         int      `json:"width"`
				Height        int      `json:"height"`
				DPR           *float64 `json:"dpr,omitempty"`
				Orientation   string   `json:"orientation,omitempty"`
				Scheme        string   `json:"scheme,omitempty"`
				ReducedMotion *bool    `json:"reduced_motion,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if err := inRange("width", in.Width, minViewport, maxViewport); err != nil {
				return nil, err
			}
			if err := inRange("height", in.Height, minViewport, maxViewport); err != nil {
				return nil, err
			}
			if in.DPR != nil && (*in.DPR < 1 || *in.DPR > 3) {
				return nil, inputErr("dpr must be 1..3")
			}
			if in.Orientation != "" {
				if err := oneOf("orientation", in.Orientation, "portrait", "landscape"); err != nil {
					return nil, err
				}
			}
			if in.Scheme != "" {
				if err := oneOf("scheme", in.Scheme, "light", "dark"); err != nil {
					return nil, err
				}
			}
			return w.browserOp(ctx, "viewport", in)
		},
	}
}

func (w *Workshop) capBrowserSnapshot() capability.Capability {
	return wcap{
		name: "browser.snapshot",
		desc: "The page as a compact tree of what a user can perceive and operate: role, name and state " +
			"of each element, with a ref (like e12) that browser.act takes. scope limits it to the " +
			"subtree of a ref, layout adds each element's box, max_nodes bounds the size (default 200). " +
			"The answer says how many nodes were left out.",
		schema: `{"type":"object","additionalProperties":false,"properties":{"scope":{"type":"string","maxLength":8},` +
			`"layout":{"type":"boolean"},"max_nodes":{"type":"integer","minimum":1,"maximum":500}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Scope    string `json:"scope,omitempty"`
				Layout   bool   `json:"layout,omitempty"`
				MaxNodes int    `json:"max_nodes,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Scope != "" {
				if err := checkRef("scope", in.Scope); err != nil {
					return nil, err
				}
			}
			if in.MaxNodes == 0 {
				in.MaxNodes = 200
			}
			if err := inRange("max_nodes", in.MaxNodes, 1, 500); err != nil {
				return nil, err
			}
			return w.browserOp(ctx, "snapshot", in)
		},
	}
}

func (w *Workshop) capBrowserAct() capability.Capability {
	return wcap{
		name: "browser.act",
		desc: "Operate the page: click, type (value is the text), hover, scroll (value up, down, left or " +
			"right) on the element with the given ref, or press a key (value is the key name, like Enter " +
			"or Tab; ref optional). Returns what changed: the new console lines and the part of the " +
			"tree that differs.",
		schema: `{"type":"object","additionalProperties":false,"required":["action"],"properties":{` +
			`"ref":{"type":"string","maxLength":8},"action":{"enum":["click","type","hover","scroll","key"]},` +
			`"value":{"type":"string","maxLength":500}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Ref    string `json:"ref,omitempty"`
				Action string `json:"action"`
				Value  string `json:"value,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if err := oneOf("action", in.Action, "click", "type", "hover", "scroll", "key"); err != nil {
				return nil, err
			}
			if in.Action != "key" && in.Action != "scroll" || in.Ref != "" {
				if err := checkRef("ref", in.Ref); err != nil {
					return nil, err
				}
			}
			if (in.Action == "type" || in.Action == "key") && in.Value == "" {
				return nil, inputErr("%s needs a value", in.Action)
			}
			if in.Action == "scroll" {
				if err := oneOf("value", in.Value, "up", "down", "left", "right"); err != nil {
					return nil, err
				}
			}
			return w.browserOp(ctx, "act", in)
		},
	}
}

func (w *Workshop) capBrowserAudit() capability.Capability {
	return wcap{
		name: "browser.audit",
		desc: "Check the page by measurement and return only the violations, each with the selector and " +
			"the numbers: " + strings.Join(auditChecks, ", ") + ". checks picks some (default all); " +
			"matrix is a list of sizes {w,h} to check one after the other (default: the usual phone, " +
			"tablet and desktop widths in portrait and landscape).",
		schema: `{"type":"object","additionalProperties":false,"properties":{"checks":{"type":"array","maxItems":9,` +
			`"items":{"enum":["page_scroll","inner_scroll","clipped_text","overlap","target_size","font_size","contrast","focus","reduced_motion"]}},` +
			`"matrix":{"type":"array","maxItems":40,"items":{"type":"object","additionalProperties":false,"required":["w","h"],` +
			`"properties":{"w":{"type":"integer","minimum":200,"maximum":4000},"h":{"type":"integer","minimum":200,"maximum":4000},` +
			`"dpr":{"type":"number","minimum":1,"maximum":3}}}}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Checks []string `json:"checks,omitempty"`
				Matrix []struct {
					W   int     `json:"w"`
					H   int     `json:"h"`
					DPR float64 `json:"dpr,omitempty"`
				} `json:"matrix,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			for _, c := range in.Checks {
				if err := oneOf("checks", c, auditChecks...); err != nil {
					return nil, err
				}
			}
			if len(in.Matrix) > maxMatrix {
				return nil, inputErr("matrix has at most %d sizes", maxMatrix)
			}
			for _, m := range in.Matrix {
				if err := inRange("matrix w", m.W, minViewport, maxViewport); err != nil {
					return nil, err
				}
				if err := inRange("matrix h", m.H, minViewport, maxViewport); err != nil {
					return nil, err
				}
				if m.DPR != 0 && (m.DPR < 1 || m.DPR > 3) {
					return nil, inputErr("matrix dpr must be 1..3")
				}
			}
			return w.browserOp(ctx, "audit", in)
		},
	}
}

func (w *Workshop) capBrowserAnimations() capability.Capability {
	return wcap{
		name: "browser.animations",
		desc: "Judge the animations on the page as data: the running animations (target, duration, delay, " +
			"easing) and the values of the chosen properties sampled over sample_ms milliseconds, plus " +
			"frame times and layout shifts. trigger {ref, action click or hover} starts the animation " +
			"first. Time is stepped deterministically, not waited for.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"trigger":{"type":"object","additionalProperties":false,"required":["ref","action"],"properties":{"ref":{"type":"string","maxLength":8},"action":{"enum":["click","hover"]}}},` +
			`"sample_ms":{"type":"integer","minimum":100,"maximum":5000},` +
			`"properties":{"type":"array","maxItems":8,"items":{"enum":["opacity","transform","width","height","top","left","color","background-color","box-shadow"]}}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Trigger *struct {
					Ref    string `json:"ref"`
					Action string `json:"action"`
				} `json:"trigger,omitempty"`
				SampleMS   int      `json:"sample_ms,omitempty"`
				Properties []string `json:"properties,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Trigger != nil {
				if err := checkRef("trigger.ref", in.Trigger.Ref); err != nil {
					return nil, err
				}
				if err := oneOf("trigger.action", in.Trigger.Action, "click", "hover"); err != nil {
					return nil, err
				}
			}
			if in.SampleMS == 0 {
				in.SampleMS = 1000
			}
			if err := inRange("sample_ms", in.SampleMS, 100, 5000); err != nil {
				return nil, err
			}
			for _, p := range in.Properties {
				if err := oneOf("properties", p, animProps...); err != nil {
					return nil, err
				}
			}
			return w.browserOp(ctx, "animations", in)
		},
	}
}

func (w *Workshop) capBrowserConsole() capability.Capability {
	return wcap{
		name: "browser.console",
		desc: "Console messages, uncaught page errors and failed requests since the call numbered `since` " +
			"(0 = from the start). Returns them with a running number to pass as `since` next time.",
		schema: `{"type":"object","additionalProperties":false,"properties":{"since":{"type":"integer","minimum":0}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Since int `json:"since,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Since < 0 {
				return nil, inputErr("since must not be negative")
			}
			return w.browserOp(ctx, "console", in)
		},
	}
}

func (w *Workshop) capBrowserShot() capability.Capability {
	return wcap{
		name: "browser.shot",
		desc: "A picture of the page (or of a region), scaled down, or a filmstrip of n frames on one " +
			"sheet. Only useful for a model that can read images; the structural checks (snapshot, audit, " +
			"animations) work without one. The picture is small by design: use region and scale.",
		schema: `{"type":"object","additionalProperties":false,"properties":{` +
			`"region":{"type":"object","additionalProperties":false,"required":["x","y","w","h"],"properties":{` +
			`"x":{"type":"integer","minimum":0,"maximum":8000},"y":{"type":"integer","minimum":0,"maximum":8000},` +
			`"w":{"type":"integer","minimum":1,"maximum":8000},"h":{"type":"integer","minimum":1,"maximum":8000}}},` +
			`"scale":{"type":"number","minimum":0.1,"maximum":1},"filmstrip":{"type":"integer","minimum":1,"maximum":12}}}`,
		run: func(ctx context.Context, raw json.RawMessage) (any, error) {
			var in struct {
				Region *struct {
					X int `json:"x"`
					Y int `json:"y"`
					W int `json:"w"`
					H int `json:"h"`
				} `json:"region,omitempty"`
				Scale     float64 `json:"scale,omitempty"`
				Filmstrip int     `json:"filmstrip,omitempty"`
			}
			if err := decode(raw, &in); err != nil {
				return nil, err
			}
			if in.Scale == 0 {
				in.Scale = 0.5
			}
			if in.Scale < 0.1 || in.Scale > 1 {
				return nil, inputErr("scale must be 0.1..1")
			}
			if in.Filmstrip != 0 {
				if err := inRange("filmstrip", in.Filmstrip, 1, 12); err != nil {
					return nil, err
				}
			}
			return w.browserOp(ctx, "shot", in)
		},
	}
}
