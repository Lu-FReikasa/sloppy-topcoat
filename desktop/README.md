# Topcoat desktop client (spike)

A thin native shell (Tauri 2, WebView2 on Windows) around the hub's own web GUI:

- every program in its **own window**, placed freely; positions are kept per machine (window-state plugin);
- a **global key** (default `Ctrl+Alt+Space`, `key` in `client.json`) opens the **program selector** above everything;
- a **tray icon** (choose a program, quit); closing the last window keeps the client running.

The tiled desktop with its taskbar belongs to the browser; the client has no window for it. A program window loads
`https://<hub>/?solo=<key>#/w/<key>`: the web desktop shows that one program without the taskbar and without its own
window buttons (the system frame has them) and keeps its state in the window, so windows do not overwrite each other's
desktop (`web/src/desktop/state.ts`, `soloOf`, `soloStep`).

When a page opens another program (a handover, a link), the solo window goes to `?solo=<other>&view=<view>`. The client
watches every navigation of its windows (`src/nav.rs`): another program opens in its own window (or an open one comes
forward on that view), a page outside the hub opens in the system browser, other schemes go nowhere. The page itself never
calls the client.

Windows are built only in `async` commands or spawned tasks: a web view built on the main thread (a synchronous command,
a navigation callback) waits for that same thread on Windows and stays white.

## Overlay (spike, default mode)

With `"mode": "overlay"` (the default once a hub is set) the global key lays an **overlay** instead of the selector
(`src/overlay.rs`): a borderless, see-through, always-on-top window with a blur effect, so the desktop behind looks dark
and blurred. Since client 0.2 **each screen has a window of its own**, built for that screen's place, size and DPI:

- the overlay key (`key`, default `Ctrl+Alt+Space`) opens it on the screen of the mouse;
- the key for every screen (`all_key`, default `ctrl+alt+KeyF` = AltGr+F on a German keyboard) opens it on all screens;
- each window shows the hub's whole web desktop with its own taskbar (`https://<hub>/?overlay=1&screen=<id>`). The
  primary screen shows the person's desktop from the hub (like the browser); every other screen keeps a desktop of its
  own in its web view (`&own=1`). Window places and tiles are kept per screen.
- a program does not move between screens by dragging (it cannot leave its web view): ⇄ in its title bar closes it here
  and the client opens it on the next screen's page (`?client=send&module=…`).

The windows are built once and only shown or hidden. A window is built anew when its screen changed, when Windows gave it
another DPI (a screen went away while the displays slept: it is hidden then), and after the client's settings changed.
The same key, Esc, or a click into another program (Alt+Tab) hides it; Esc is a global key only while the overlay is up.
`"mode": "windows"` brings back the selector and a native window per program; the tray has both.

Until 0.1 the overlay was one window over the rectangle around all screens (`?screens=…`, still understood by the page):
with screens of different DPI and offset it had corners no one sees and stood off the screen after the displays slept.

Known limits: a game in exclusive full screen stays above the overlay; a key the page uses for itself (Esc closing the
palette) is taken by the overlay while it is up; the hidden desktop pages keep running (memory, the hub's stream: one per
screen that was shown). UNVERIFIED on real hardware: the screen of the mouse, the rebuild after sleep, ⇄.

## Push to talk (D127)

The chat's microphone key (hold it, or hold `Ctrl+Space` while the chat window is in front) works in the client's
windows as in a browser: the page records with Web Audio and talks to the hub over its own TLS connection
(`/api/voice/ws`); the client itself adds nothing and opens no port. The first press asks for the microphone with
WebView2's own prompt for the hub's address; the answer is kept in the client's web view profile. `Ctrl+Space` is not
the overlay key (`Ctrl+Alt+Space`). UNVERIFIED on real hardware: the WebView2 prompt in the overlay and in program
windows, recording while the overlay is hidden. If WebView2 refuses without asking, the next step is a
`PermissionRequested` handler that allows only the microphone, only for the hub's origin.

## Security

- The client opens **no port** and keeps **no token**: the sign-in is in the web view's cookie store, as in a browser.
- Only the local selector window may call the client's commands (`capabilities/default.json`); the hub's pages get no IPC.
- The hub address must be `https://` (D11). The web view trusts what Windows trusts: the hub's own CA must be in the
  Windows certificate store (as for Edge/Chrome).

## Configuration

`%APPDATA%\lan.example.topcoat.desktop\client.json` (written by the selector on first start):

```json
{ "hub": "https://hub.example.lan:8443", "key": "ctrl+alt+space", "all_key": "ctrl+alt+KeyF", "mode": "overlay" }
```

`TOPCOAT_HUB_URL` overrides the address.

## Build (on the Linux test host, for Windows)

```bash
. ~/.cargo/env
cd desktop/src-tauri
XWIN_ACCEPT_LICENSE=1 cargo tauri build --runner cargo-xwin --target x86_64-pc-windows-msvc
```

The portable `topcoat-desktop.exe` lands in `target/x86_64-pc-windows-msvc/release/`. The NSIS installer step fails on a
Linux host without `libayatana-appindicator3-dev` (the CLI checks it even for a Windows target).

UNVERIFIED on real hardware: the async window fix, the navigation guard (another program, outside pages) and the window
title from the page were built and the routing rules tested (`nav.rs`), not yet run on Windows. Seen on Windows
(2026-11-11, before the fix): sign-in, the tray, the selector.
