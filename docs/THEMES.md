# Themes: how the desktop is drawn and arranged (consolidation step 8)

A **theme** decides how the desktop looks and behaves as a *surface*. The desktop's **core**
(`web/src/desktop/state.ts`: which programs are open, minimised, in front, which view each shows;
the keys; the command palette; what the programs hold and what they may do) does not depend on a
theme. A theme only arranges what the core hands it.

A theme is **data plus CSS, never code**. That is a decision, not a gap: the web page holds the
person's token and the whole API with it, so code that a theme brings would be code with the rights
of the person. What a theme cannot say in the data below, it says in CSS.

## What a theme decides

```json
{
  "key": "wall",
  "name": "Wall",
  "base": "terminal",
  "layout": { "kind": "columns" },
  "bar": { "position": "left", "items": ["programs", "status", "me"] },
  "chrome": { "buttons": ["close", "minimise"], "titlebar": "top" },
  "motion": { "style": "slide", "close": "fade", "overlay": "zoom", "ms": 200 },
  "css": "--accent: hotpink;\n.dt-task { letter-spacing: 1px; }"
}
```

| field | values | meaning |
|---|---|---|
| `base` | `terminal`, `leitstand`, `schreibtisch` | a built-in theme whose style sheet this one starts from |
| `layout.kind` | `tile`, `columns`, `rows`, `focus`, `cascade`, `nest` | where the shown programs stand (`cascade` overlaps them; the one in front is on top; `nest`: see below) |
| `bar.position` | `bottom`, `top`, `left`, `right`, `hidden` | where the taskbar is; `hidden`: not drawn (Ctrl+K and the keys still work) |
| `bar.items` | `start`, `programs`, `clock`, `status`, `me` | what the taskbar holds, in this order; an item that is not listed is not there |
| `chrome.buttons` | `minimise`, `maximise`, `close` | the buttons of a program's title bar, in this order |
| `chrome.titlebar` | `top`, `hidden` | whether a program has a title bar |
| `motion.style` | `none`, `fade`, `slide`, `flicker`, `zoom` | how a program window opens |
| `motion.close` | the same; optional, `none` when left out | how a window closes or is minimised |
| `motion.overlay` | the same; optional, `none` when left out | how the overlay of the desktop client appears and goes |
| `motion.ms` | 0 to 2000 | the length of each of them |
| `css` | text, at most 40 000 characters | extra style sheet, see below |

### The `nest` layout: levels and zoom

`nest` keeps the shown programs in splits that nest to any depth: a program that opens splits the
place of the one that was in front, along its longer side; one that closes gives its room to its
neighbours. Each split is a **level**, and the view **zooms** to one of them: the framed group fills
the stage, everything else lies outside it and keeps running (nothing is mounted again, a chat or
an embedded page keeps its state). The tree and the zoom are kept per screen in the browser (the
overlay of the desktop client keeps one per screen window).

| key | does |
|---|---|
| `Alt+↓` | zoom in one level, towards the program in front (the last level is the program alone) |
| `Alt+↑` | zoom out one level (in the browser `Esc` does the same while zoomed in) |
| `Alt+0` | the overview: everything at once |

The three are also in the command palette. Bringing a program forward that lies outside the frame
(taskbar, `Alt+1`…`Alt+9`) zooms out just far enough to show it. A zoom moves the windows as a
transform for `motion.ms` (none when the system asks for less motion).

A program whose place is smaller than its minimum size (480 × 320 px) is laid out at that size and
drawn **scaled down**; it then takes no input, and a click on it zooms to the first level where it
has its full size. Classes for the style sheet: `.dt-scaled` (a program drawn scaled down) and
`.dt-unframed` (a program outside the zoomed group, off the stage). In the overlay `Esc` stays the
client's key (it hides the overlay) and nest windows are not moved or sized by hand.
On a narrow screen, which shows only the program in front, the nest is not drawn and its keys
do nothing (`Esc` minimises as in the other layouts).

The idea comes from [Trellis](https://github.com/DanFessler/trellis) by Dan Fessler, a layout engine
for web apps with nested, zoomable panels. Topcoat's `nest` is an independent implementation of that
behaviour (`web/src/desktop/nest.ts`); it uses no Trellis code, whose licence does not fit the
AGPL (#12). Not in it for now: tabs inside a window, docking by drag, free pan and pinch zoom,
floating windows inside a nest.

### Animations of one's own

The presets above are CSS animations; a theme can replace any of them by its own `@keyframes` in
`css`, on these classes, for `motion.ms` (the desktop waits that long before a window or the
overlay is really gone, so an animation must end within it and stop where it ends: `forwards`):

| class | when |
|---|---|
| `.dt-program` | a window opens (its `animation` plays when it appears) |
| `.dt-program.dt-closing` | a window closes or is minimised |
| `.dt-arriving` | the overlay has just been shown (on the root) |
| `.dt-leaving` | the overlay is about to be hidden (on the root) |

```css
@keyframes xp-pop { from { transform: scale(0.6); opacity: 0; } }
@keyframes xp-drop { to { transform: translateY(40vh) scale(0.3); opacity: 0; } }
.dt-program { animation: xp-pop var(--dt-ms) ease-out; }
.dt-program.dt-closing { animation: xp-drop var(--dt-ms) ease-in forwards; }
.dt-leaving { animation: xp-drop var(--dt-ms) ease-in forwards; }
```

`--dt-ms` holds `motion.ms`. A person who asked their system for less motion sees none of it.
The overlay's own animations need the desktop client 0.1.1 or later (older ones show and hide it
at once; Esc still plays the leaving).

What feels like a fault (the hub's lint names each one; live 2026-11-16, Wireframe theme v1 to v3):

* `infinite` on a window, its title bar, the taskbar or an entry: a selected window that wobbles
  all the time. Animate a change once (opening, closing, focus) and let it rest; only a lamp or a
  busy mark (`.dt-lamp`, `.dt-signal`) may go on.
* a fill mode (`forwards`, `both`) on `.dt-program` itself: the window keeps the last frame and with
  it `transform` and `opacity` for good. Only `.dt-program.dt-closing` and the overlay classes end
  with `forwards`.
* `opacity`, `filter` or an animation on `.dt-program:hover` or `:not(:hover)`: windows fade while the
  pointer passes over them. A button (`.dt-close:hover`) may react to the pointer, a whole window not.

The built-in themes are such documents too (`BUILTIN_THEMES` in `theme.ts`). Three of them are
also the bases a theme can start from (`terminal`, `leitstand`, `schreibtisch`); their style sheets
are `desktop.css`, `leitstand.css` and `schreibtisch.css`. The fourth, **Terminal Nest**
(`terminal-nest`), is a theme like a custom one: base `terminal`, layout `nest`, the taskbar as one
line at the top, a thin title line per program and one-pixel borders between the panes, all in its
own `css` within the limits above. Its key is kept from custom themes like the others. The terminal
is what is drawn when a person's theme does not exist.

`base` is the look a theme starts from; whatever its `css` does not restyle stays as the base draws
it, so pick the base nearest to the look you want:

| base | what it draws |
|---|---|
| `terminal` | dark and plain: one hue, monospace font, square corners, no shadows. The neutral start for a look of its own. |
| `leitstand` | a control room: dark housings with bevels, signal lamps (red, amber, green, cyan), engraved labels. |
| `schreibtisch` | a desk: programs are slips of paper on a cutting mat, each a little askew and held by a strip of tape, typewriter fonts. Only for a paper look. |

## The style sheet

Every rule of a theme's `css` is nested under the root of the desktop, so it can restyle everything
inside the desktop and nothing outside. Write it like this:

```css
/* 1. variables and the desktop itself: bare declarations, they apply to the root */
--bg: #3a6ea5;
--text: #000;
--titlebar: linear-gradient(#0058ee, #003dd7);

/* 2. rules for the parts, with the classes below */
.dt-stage { background: linear-gradient(#5a9bd8, #3a7a3a); }
.dt-titlebar { background: var(--titlebar); }
.dt-close { background: #e0422c; color: #fff; }
.dt-bar-top .dt-taskbar { border-bottom: 2px solid #fff; }
```

* The root itself is reached by bare declarations, by `:root`, by `.dt-root` and by
  `.dt-theme-<key>`; all four mean the same element. A class that stands on the root
  (`.dt-bar-top`, `.dt-solo` …) at the start of a selector means the root in that state.
* A theme's rules win over the rules of its base for the same part, so `.dt-titlebar { … }` is
  enough; no `!important` is needed.
* Every `var(--name)` must be set somewhere: as a bare declaration of the theme or by the base
  (the variables below). A variable that is set nowhere makes the whole declaration void, and the
  part keeps the look of its base.
* `@keyframes` are lifted out of the nesting and work as usual.

Refused, as a whole: `@import`, `@charset`, `@namespace`, any `url()` that is not an inline
`data:image`, `image-set()`, `image()`, `cross-fade()`, `src()`, `element()`, `expression(`,
`javascript:`, `behavior`, `-moz-binding`, anything that looks like markup. The check reads the
sheet as the browser does (comments out, backslash escapes decoded), so an escape such as `\75 rl(`
counts as `url(`. The same check runs in the hub before a theme is stored and in the web build before it is
drawn (`cssProblems`). The hub also names, before a theme is used, every class the desktop does not
draw and every variable that is set nowhere (`theme_lint`).

## The variables of the base

The base and every page inside the programs draw with these variables, so setting them restyles
the pages too (a light theme sets `--bg`, `--surface` and `--text` light and dark):

| variable | what it colours |
|---|---|
| `--bg` | the background of the desktop and of the pages |
| `--surface` | panels, cards and the bars inside the pages |
| `--text`, `--muted` | the text; secondary text |
| `--border` | lines and frames |
| `--accent`, `--accent-text` | the highlight colour (focused window, active entries, buttons); text on it |
| `--track` | the track of bars and meters |
| `--ok`, `--warn`, `--bad` | a good state, a warning, an error |
| `--on-ok`, `--on-warn`, `--on-off` | text on a badge filled with `--ok`, `--warn` or the grey of "offline" (light fills want dark text) |
| `--app-chat`, `--app-research`, `--app-dev`, `--app-admin`, `--app-project` | the colour of each application: its inbox tag, its rule, a task's badge |
| `--on-app` | text on an application colour used as a fill |
| `--radius` | the corner radius of the pages' controls |

## What a style sheet can reach

The classes of the desktop that a theme may rely on (they are the surface; everything else in a
program's body belongs to the program). A class that is not in this table is not drawn, and a rule
for it matches nothing (`proto/ui/theme-surface.json` holds the full list):

| class | what it is |
|---|---|
| `.dt-root` | the whole desktop (the root, see above); `.dt-bar-top`, `.dt-bar-bottom`, `.dt-bar-left`, `.dt-bar-right`, `.dt-bar-hidden` stand on it and say where the bar is |
| `.dt-stage` | the area that holds the windows: the desktop's background belongs here (`leitstand` and `schreibtisch` paint it, over the root's background) |
| `.dt-program` | a window; `.dt-program.dt-focused` the one in focus, `.dt-program.dt-maxi` a maximised one; with `nest`, `.dt-program.dt-scaled` one drawn scaled down and `.dt-program.dt-unframed` one outside the zoomed group |
| `.dt-titlebar` | the title bar of a window; inside it `.dt-title` (with `.dt-icon`) and `.dt-titlebar-buttons` |
| `.dt-minimise`, `.dt-maximise`, `.dt-close` | the buttons of the title bar (inside `.dt-titlebar-buttons`; plain `button` elements) |
| `.dt-program-body` | the content of a window |
| `.dt-taskbar` | the taskbar; inside it `.dt-start` (the start button, with `.dt-label`), `.dt-tasks` (the entries), `.dt-clock`, `.dt-me` (the person menu, opens `.dt-me-menu`) |
| `.dt-task` | one program's entry on the taskbar (with `.dt-icon` and `.dt-label`); `.dt-task.dt-open` its program is open, `.dt-task.dt-min` minimised, `.dt-task.dt-front` in front; `.dt-start.dt-front` the start program is in front |
| `.dt-signal`, `.dt-lamp` | the status dot of an entry (`.dt-signal-red`, `-amber`, `-green` on the entry); the lamp in a title bar |
| `.dt-palette`, `.dt-palette-backdrop`, `.dt-pal-current` | the command palette (Ctrl+K) |

Careful: `.dt-min` and `.dt-maxi` are states (a minimised taskbar entry, a maximised window), not
buttons; the buttons are `.dt-minimise`, `.dt-maximise` and `.dt-close`.

## Limits

| | limit |
|---|---|
| style sheet | 40 000 characters (the built-in ones are 7 000 to 10 000) |
| custom themes | 50 per hub |
| images | only inline `data:image/...` in `url()`; no web fonts, no files, no `@import` |
| layout | the six engines above; no per-window positions |
| code | none |

## Where themes live

* built-in themes: in the web build;
* custom themes: in the hub (`ui_themes`), created and changed by an admin with live preview on the
  page *Administration → Themes*, and chosen per person (`ui_prefs.look` holds the key). An open page
  gets a changed theme as an event; no web build is needed. The agent may only *propose* a theme
  change; the person applies it with one click (D15).
* the theme workshop (*Administration → Themes → Open in the workshop*, or from a theme result of a
  project task): a conversation with the house model that changes the theme by small patches
  (`hub/src/topcoat_hub/ui/theme_patch.py`) while the desktop draws the draft; colours and motion have
  controls of their own. Saving is the person's click; opened from a task, it files the theme there as
  the task's next iteration.

## What is not in it, on purpose

* No script, no custom component: nothing that runs. If layout logic beyond the six engines is
  ever needed, it belongs in a sandbox (a Web Worker for a pure `(windows, area) -> rectangles`
  function, an iframe without access to the page's storage for a fully drawn shell, both reaching
  the core through a small allow-listed bridge), as a separate decision.
* A theme does not decide what the programs *are*, what they may do or who may see them: that is
  the applications' rights (D69) and the person's profile.
