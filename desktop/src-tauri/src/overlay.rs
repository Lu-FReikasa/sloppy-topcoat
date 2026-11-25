//! The overlay: the global key lays a borderless, see-through, always-on-top window with a blur
//! effect over the screen of the mouse, so the desktop behind looks dark and blurred; the key for
//! every screen (AltGr+F) lays one over each screen.
//!
//! Each screen has a window of its own, built for that screen alone: its place, its size and its
//! DPI (2026-11-20: one window over the rectangle around all screens had corners no one sees, and
//! with two screens of different DPI Windows moved and scaled it after the displays slept, so the
//! taskbar stood off the screen). Each window shows the hub's whole web desktop with its own
//! taskbar (`?overlay=1&screen=<id>`); the page keeps the places of its windows per screen. A
//! program cannot leave its web view, so it does not move between screens by dragging: "to the
//! other screen" closes it on its page and the client opens it on the next screen's page
//! (`?client=send`).
//!
//! The windows are built once and then only shown or hidden: the toggle is instant and a desktop
//! keeps its state. A window is built anew when its screen changed (place, size), when Windows gave
//! it another DPI (a screen went away while the displays slept: it is hidden then and built anew
//! at the next key press), and after the client's settings changed. Esc hides the overlay too;
//! Esc is a global key only while the overlay is up (the hub's page gets no IPC, so the page cannot
//! ask the client to hide), and so is the overlay itself: when another program takes the focus
//! (Alt+Tab into a game), the overlay goes away and gives Esc back.
//!
//! The pin of a program window (web: `floating.ts`) sends the page to the program's solo address
//! with `pin=x,y,w,h`; the client keeps the page where it is and opens the program's native window
//! at that place instead (`lib.rs`, `show_program`), without taking the focus from the overlay.
//!
//! The backdrop is `Effect::Blur`, not acrylic: Windows 11 draws acrylic (a system backdrop) only
//! for the active window and a grey fill for all others, so the screens without focus stayed grey
//! until a click (seen 2026-11-12).
//!
//! UNVERIFIED on real hardware: one window per screen with screens of different DPI, the screen of
//! the mouse (`cursor_position` in physical pixels of the virtual screen), the rebuild after the
//! displays slept, sending a program to the next screen; not covered is a game in exclusive full
//! screen (the overlay cannot stand above it).

use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Mutex,
    },
    thread,
    time::Duration,
};

use tauri::{
    webview::NewWindowResponse,
    window::{Color, Effect, EffectsBuilder},
    AppHandle, Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Shortcut};
use tauri_plugin_opener::OpenerExt;

use crate::{hub_url, load_config, nav, save_config, set_key, show_program, Place};

const PREFIX: &str = "ov-";

/// What the client knows about its overlay windows.
#[derive(Default)]
pub struct Overlay {
    /// Windows are being built (a second key press waits for them).
    building: AtomicBool,
    /// Per window (its label): the screen it was built for.
    built: Mutex<HashMap<String, nav::Screen>>,
    /// The client's settings changed since the windows were built (their pages were told the old
    /// ones): they are built anew at the next key press.
    stale: AtomicBool,
    /// Counts the times the overlay was shown: a late fallback hide (`leave`) only hides the
    /// showing it was asked for, not one the key started since.
    shown: AtomicU64,
    /// The overlay window the person used last: Esc, Clipper and answers go there.
    last: Mutex<Option<String>>,
}

/// One screen as the overlay sees it: its window's label, its name for the page, where it lies
/// (physical pixels), its scale, and whether it is the primary one.
#[derive(Clone, Debug)]
struct Spot {
    label: String,
    id: String,
    geometry: nav::Screen,
    scale: f64,
    primary: bool,
}

/// The longest leaving a theme may play (motion.ms is at most 2000) and a margin: after it the
/// client hides the overlay itself, in case the page did not ask.
const LEAVE_FALLBACK: Duration = Duration::from_millis(2300);

/// Esc, as a global key while the overlay is up.
pub fn esc() -> Shortcut {
    Shortcut::new(None, Code::Escape)
}

fn windows(app: &AppHandle) -> Vec<WebviewWindow> {
    let mut all: Vec<_> = app
        .webview_windows()
        .into_iter()
        .filter(|(label, _)| label.starts_with(PREFIX))
        .map(|(_, w)| w)
        .collect();
    all.sort_by(|a, b| a.label().cmp(b.label()));
    all
}

fn visible(w: &WebviewWindow) -> bool {
    w.is_visible().unwrap_or(false)
}

fn shown(app: &AppHandle) -> Vec<WebviewWindow> {
    windows(app).into_iter().filter(visible).collect()
}

pub fn is_shown(app: &AppHandle) -> bool {
    windows(app).iter().any(visible)
}

/// The screens, the primary one first.
fn spots(app: &AppHandle) -> Result<Vec<Spot>, String> {
    let mut all = app.available_monitors().map_err(|e| e.to_string())?;
    if all.is_empty() {
        return Err("no screen found".into());
    }
    if let Ok(Some(primary)) = app.primary_monitor() {
        if let Some(i) = all.iter().position(|m| m.position() == primary.position()) {
            let p = all.remove(i);
            all.insert(0, p);
        }
    }
    let mut out: Vec<Spot> = Vec::new();
    for (i, m) in all.iter().enumerate() {
        let mut id = nav::screen_id(m.name().map(String::as_str), i);
        if out.iter().any(|s| s.id == id) {
            id = format!("{id}x{i}"); // two screens that read the same: keep them apart
        }
        out.push(Spot {
            label: format!("{PREFIX}{id}"),
            id,
            geometry: (m.position().x, m.position().y, m.size().width, m.size().height),
            scale: m.scale_factor(),
            primary: i == 0,
        });
    }
    Ok(out)
}

/// The screen of the mouse (the primary one when the mouse cannot be read).
fn here(app: &AppHandle) -> Result<Spot, String> {
    let all = spots(app)?;
    let geometry: Vec<nav::Screen> = all.iter().map(|s| s.geometry).collect();
    let at = app.cursor_position().ok().and_then(|p| nav::screen_at((p.x, p.y), &geometry));
    Ok(all[at.unwrap_or(0)].clone())
}

/// The overlay window that has the keyboard, else the one used last, else any that is shown.
fn front(app: &AppHandle) -> Option<WebviewWindow> {
    let all = shown(app);
    if let Some(w) = all.iter().find(|w| w.is_focused().unwrap_or(false)) {
        return Some(w.clone());
    }
    let last = app.state::<Overlay>().last.lock().ok().and_then(|l| l.clone());
    if let Some(w) = last.and_then(|l| all.iter().find(|w| w.label() == l).cloned()) {
        return Some(w);
    }
    all.into_iter().next()
}

/// The key: the overlay comes up on the screen of the mouse (`all`: on every screen), or goes
/// away when it is up (`all`: when it is up on every screen; else the missing ones come too).
pub fn toggle(app: &AppHandle, all: bool) {
    let app = app.clone();
    // windows are built off the main thread (README: a web view built there stays white)
    tauri::async_runtime::spawn(async move {
        let result = if all {
            spots(&app).and_then(|every| {
                let up = every
                    .iter()
                    .all(|s| app.get_webview_window(&s.label).is_some_and(|w| visible(&w)));
                if up {
                    leave(&app);
                    Ok(())
                } else {
                    show(&app, &every)
                }
            })
        } else if is_shown(&app) {
            leave(&app);
            Ok(())
        } else {
            here(&app).and_then(|s| show(&app, &[s]))
        };
        if let Err(e) = result {
            eprintln!("the overlay could not open: {e}");
        }
    });
}

/// The key while the overlay is up: each page plays its theme's leaving and then asks to hide
/// (`?client=hide`); a page without it (an older hub) asks at once. Should no page ask at all, the
/// client hides the overlay after the longest leaving there can be.
/// UNVERIFIED on real hardware.
fn leave(app: &AppHandle) {
    const JS: &str = "(function(c){if(c&&typeof c.leave==='function'){c.leave();}\
else{window.location.assign('?client=hide');}})(window.topcoatClient);";
    let asked_any = shown(app).iter().fold(false, |any, w| w.eval(JS).is_ok() || any);
    if !asked_any {
        return hide(app);
    }
    let asked = app.state::<Overlay>().shown.load(Ordering::SeqCst);
    let app = app.clone();
    thread::spawn(move || {
        thread::sleep(LEAVE_FALLBACK);
        if app.state::<Overlay>().shown.load(Ordering::SeqCst) == asked && is_shown(&app) {
            hide(&app);
        }
    });
}

pub fn hide(app: &AppHandle) {
    for w in windows(app) {
        let _ = w.hide();
    }
    let _ = app.global_shortcut().unregister(esc());
}

/// Show the overlay on these screens (built where needed); the screen of the mouse gets the
/// keyboard when it is one of them.
fn show(app: &AppHandle, spots_wanted: &[Spot]) -> Result<(), String> {
    let state = app.state::<Overlay>();
    if spots_wanted.is_empty() {
        return Ok(());
    }
    if state.building.swap(true, Ordering::SeqCst) {
        return Ok(()); // the first press is still building them
    }
    let built = build_all(app, spots_wanted);
    state.building.store(false, Ordering::SeqCst);
    let wins = built?;
    for (w, s) in wins.iter().zip(spots_wanted) {
        let was = visible(w);
        // placed again each time: the window stays on its own screen, so this is exact
        place(w, s.geometry);
        let _ = w.show();
        if !was {
            // the page plays its theme's arrival (a page without it shows at once)
            // UNVERIFIED on real hardware
            let _ = w.eval(
                "(function(c){if(c&&typeof c.onShow==='function')c.onShow();})(window.topcoatClient);",
            );
        }
    }
    state.shown.fetch_add(1, Ordering::SeqCst);
    let mouse = here(app).ok().map(|s| s.label);
    let focus = wins
        .iter()
        .find(|w| Some(w.label().to_string()) == mouse)
        .or_else(|| wins.first());
    if let Some(w) = focus {
        let _ = w.set_focus();
        if let Ok(mut last) = state.last.lock() {
            *last = Some(w.label().to_string());
        }
    }
    // a second screen joining a shown overlay finds Esc registered already
    if !app.global_shortcut().is_registered(esc()) {
        if let Err(e) = app.global_shortcut().register(esc()) {
            eprintln!("Esc could not be registered: {e}");
        }
    }
    Ok(())
}

/// The windows for these screens; windows of screens that are gone are dropped, and all are built
/// anew after the settings changed (only while none is shown: a shown page keeps its state).
fn build_all(app: &AppHandle, wanted: &[Spot]) -> Result<Vec<WebviewWindow>, String> {
    let state = app.state::<Overlay>();
    let current = spots(app)?;
    let stale = !is_shown(app) && state.stale.swap(false, Ordering::SeqCst);
    for w in windows(app) {
        if stale || !current.iter().any(|s| s.label == w.label()) {
            forget(app, w.label());
            let _ = w.destroy();
        }
    }
    wanted.iter().map(|s| build_if_needed(app, s)).collect()
}

fn forget(app: &AppHandle, label: &str) {
    if let Ok(mut built) = app.state::<Overlay>().built.lock() {
        built.remove(label);
    }
}

/// The window of one screen: the one there is when it was built for this screen as it is now,
/// else a new one (the desktop's state lives on the hub and in the page's storage).
fn build_if_needed(app: &AppHandle, s: &Spot) -> Result<WebviewWindow, String> {
    let same = |w: &WebviewWindow| {
        let state = app.state::<Overlay>();
        let built = state.built.lock().ok();
        built.is_some_and(|b| b.get(w.label()) == Some(&s.geometry))
    };
    if let Some(w) = app.get_webview_window(&s.label) {
        if same(&w) {
            return Ok(w);
        }
        forget(app, &s.label);
        let _ = w.destroy();
    }
    let config = load_config(app).ok_or("no hub address set")?;
    let hub = hub_url(app)?;
    let url = WebviewUrl::External(nav::overlay_url(&hub, &s.id, !s.primary, config.fps));
    let blur = nav::Effect::parse(&config.effect) != Some(nav::Effect::None);
    // no lock is held here: building waits for the main thread, whose events take the lock
    let w = guard(app, &hub, &s.label, base(app, &s.label, url, s.geometry, s.scale, blur))
        .build()
        .map_err(|e| e.to_string())?;
    place(&w, s.geometry);
    watch(app, &w);
    if let Ok(mut built) = app.state::<Overlay>().built.lock() {
        built.insert(s.label.clone(), s.geometry);
    }
    Ok(w)
}

/// The builder takes logical units; the screens' own physical pixels are exact on any DPI.
fn place(w: &WebviewWindow, (x, y, width, height): nav::Screen) {
    let _ = w.set_position(PhysicalPosition::new(x, y));
    let _ = w.set_size(PhysicalSize::new(width, height));
}

fn base<'a>(
    app: &'a AppHandle,
    label: &str,
    url: WebviewUrl,
    (x, y, width, height): nav::Screen,
    scale: f64,
    blur: bool,
) -> WebviewWindowBuilder<'a, tauri::Wry, AppHandle> {
    let pos = PhysicalPosition::new(x, y).to_logical::<f64>(scale);
    let size = PhysicalSize::new(width, height).to_logical::<f64>(scale);
    let w = WebviewWindowBuilder::new(app, label, url)
        .title("Topcoat")
        .position(pos.x, pos.y)
        .inner_size(size.width, size.height)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .resizable(false)
        .visible(false)
        // see-through from the first frame, before the page has painted anything
        .background_color(Color(0, 0, 0, 0))
        // what the page knows of its client: its settings, and where its answers go
        .initialization_script(&client_script(app));
    if !blur {
        return w; // only the page's own tint: nothing for the system to compose behind it
    }
    w.effects(
        EffectsBuilder::new()
            // blur stays when the window is not active (acrylic turns grey then)
            .effect(Effect::Blur)
            // a dark tint inside the effect
            .color(Color(8, 10, 14, 150))
            .build(),
    )
}

/// The client's settings as the overlay's page sees them (`window.topcoatClient.config`);
/// `screens` tells the page whether there is another screen to send a program to.
fn client_config(app: &AppHandle) -> serde_json::Value {
    let c = load_config(app);
    serde_json::json!({
        "version": app.package_info().version.to_string(),
        "key": c.as_ref().map(|c| c.key.clone()).unwrap_or_default(),
        "allKey": c.as_ref().map(|c| c.all_key.clone()).unwrap_or_default(),
        "clipperKey": c.as_ref().map(|c| c.clipper_key.clone()).unwrap_or_default(),
        "effect": c.as_ref().map(|c| c.effect.clone()).unwrap_or_default(),
        "screens": app.available_monitors().map(|m| m.len()).unwrap_or(1),
    })
}

/// Runs in the page before its own scripts: data only, no function of the client.
fn client_script(app: &AppHandle) -> String {
    format!(
        "window.topcoatClient=Object.assign(window.topcoatClient||{{}},{{config:{}}});",
        client_config(app)
    )
}

/// Tell the page that asked how its request went, and the settings as they are now.
fn report(app: &AppHandle, to: &str, what: &str, result: Result<(), String>) {
    let answer = serde_json::json!({
        "what": what,
        "ok": result.is_ok(),
        "error": result.err(),
    });
    let js = format!(
        "(function(c){{if(!c)return;c.config={};if(typeof c.onResult==='function')c.onResult({});}})(window.topcoatClient);",
        client_config(app),
        answer
    );
    if let Some(w) = app.get_webview_window(to) {
        let _ = w.eval(&js);
    }
}

/// Esc while the overlay is up: the page in front closes what it has open (the palette, a dialog,
/// a menu), else it asks to hide. A page without that answer (an older hub) is hidden at once.
pub fn escape(app: &AppHandle) {
    const JS: &str = "(function(c){if(c&&typeof c.escape==='function'){c.escape();}\
else{window.location.assign('?client=hide');}})(window.topcoatClient);";
    match front(app) {
        Some(w) if w.eval(JS).is_ok() => {}
        _ => hide(app),
    }
}

/// The page in front opens this (`{module, props}` as JSON): at once, or when it has started.
fn open_in(w: &WebviewWindow, intent: &str) {
    let js = format!(
        "(function(c){{if(!c)return;var o={intent};\
if(typeof c.onOpen==='function')c.onOpen(o);else c.wantOpen=o;}})(window.topcoatClient);"
    );
    let _ = w.eval(&js);
}

/// The overlay on the screen of the mouse, when none is up yet.
fn ensure_shown(app: &AppHandle) -> Result<(), String> {
    if is_shown(app) {
        return Ok(());
    }
    show(app, &[here(app)?])
}

/// What the page in window `from` asked of the client (`nav::client_cmd`).
fn on_client(app: &AppHandle, from: &str, cmd: nav::ClientCmd) {
    match cmd {
        nav::ClientCmd::Hide => hide(app),
        nav::ClientCmd::HeraldShow => {
            if let Some(h) = app.get_webview_window(HERALD) {
                place_herald(app, &h);
                let _ = h.show(); // not focusable: the person's window keeps the keyboard
                hide_herald_later(app);
            }
        }
        nav::ClientCmd::HeraldHide => {
            HERALD_SHOWN.fetch_add(1, Ordering::SeqCst); // a pending timer has nothing to do
            if let Some(h) = app.get_webview_window(HERALD) {
                let _ = h.hide();
            }
        }
        nav::ClientCmd::OpenTask(id) => {
            if let Some(h) = app.get_webview_window(HERALD) {
                let _ = h.hide();
            }
            if let Err(e) = ensure_shown(app) {
                eprintln!("the overlay could not open: {e}");
                return;
            }
            if let Some(w) = front(app) {
                open_in(&w, &format!("{{module:'page.tasks',props:{{id:'{id}'}}}}"));
            }
        }
        nav::ClientCmd::Send(module) => {
            let result = (|| {
                let all = spots(app)?;
                let i = all.iter().position(|s| s.label == from).ok_or("not an overlay window")?;
                let j = nav::next_screen(i, all.len()).ok_or("there is no other screen")?;
                let target = all[j].clone();
                if !app.get_webview_window(&target.label).is_some_and(|w| visible(&w)) {
                    show(app, std::slice::from_ref(&target))?;
                }
                let w = app.get_webview_window(&target.label).ok_or("the window is gone")?;
                // the module is checked (nav::module_ok): letters, digits, dots and dashes only
                open_in(&w, &format!("{{module:'{module}',props:{{}}}}"));
                let _ = w.set_focus();
                Ok::<(), String>(())
            })();
            if let Err(e) = &result {
                eprintln!("the program could not go to the other screen: {e}");
            }
            report(app, from, "send", result);
        }
        nav::ClientCmd::SetKey { slot, key } => {
            let result = set_key(app, slot, &key);
            if result.is_ok() {
                app.state::<Overlay>().stale.store(true, Ordering::SeqCst);
            }
            report(app, from, "key", result);
        }
        nav::ClientCmd::SetLook { effect } => {
            let result = (|| {
                let mut c = load_config(app).ok_or("no hub address set")?;
                c.effect = effect.as_str().into();
                save_config(app, &c)
            })();
            let ok = result.is_ok();
            report(app, from, "look", result);
            if ok {
                // the backdrop is fixed when a window is built: build the shown ones again
                let again: Vec<Spot> = spots(app)
                    .unwrap_or_default()
                    .into_iter()
                    .filter(|s| app.get_webview_window(&s.label).is_some_and(|w| visible(&w)))
                    .collect();
                app.state::<Overlay>().stale.store(true, Ordering::SeqCst);
                hide(app);
                if let Err(e) = show(app, &again) {
                    eprintln!("the overlay could not open again: {e}");
                }
            }
        }
    }
}

/// The desktop's page: the hub stays, everything else goes to the system browser. `label` is the
/// window, so that a request of its page is answered there.
fn guard<'a>(
    app: &AppHandle,
    hub: &url::Url,
    label: &str,
    w: WebviewWindowBuilder<'a, tauri::Wry, AppHandle>,
) -> WebviewWindowBuilder<'a, tauri::Wry, AppHandle> {
    let (nav_app, nav_hub, nav_label) = (app.clone(), hub.clone(), label.to_string());
    let (pop_app, pop_hub, pop_label) = (app.clone(), hub.clone(), label.to_string());
    w.on_navigation(move |to| follow(&nav_app, &nav_hub, &nav_label, to))
        .on_new_window(move |to, _features| {
            if follow(&pop_app, &pop_hub, &pop_label, &to) {
                NewWindowResponse::Allow // a file of the hub, with the same sign-in
            } else {
                NewWindowResponse::Deny
            }
        })
}

fn follow(app: &AppHandle, hub: &url::Url, label: &str, to: &url::Url) -> bool {
    match nav::overlay_route(hub, to) {
        nav::OverlayRoute::Stay => true,
        nav::OverlayRoute::Client(cmd) => {
            let (app, label) = (app.clone(), label.to_string());
            // not inside the web view's own event (a window built or hidden there waits for it)
            tauri::async_runtime::spawn(async move { on_client(&app, &label, cmd) });
            false
        }
        nav::OverlayRoute::Pin(key, rect) => {
            let place = place_of(app, label, rect);
            let (app, to) = (app.clone(), to.clone());
            // not inside the web view's own event: a window built there waits for itself
            tauri::async_runtime::spawn(async move {
                if let Err(e) = show_program(&app, &key, Some(to), place, false) {
                    eprintln!("the window of {key} could not be pinned: {e}");
                }
            });
            false
        }
        nav::OverlayRoute::Outside => {
            if let Err(e) = app.opener().open_url(to.as_str(), None::<&str>) {
                eprintln!("the browser could not open {to}: {e}");
            }
            hide(app); // the page in the browser would sit under the overlay
            false
        }
        nav::OverlayRoute::Block => false,
    }
}

/// A place on an overlay page (CSS pixels of its window) in screen pixels.
fn place_of(app: &AppHandle, label: &str, r: nav::Rect) -> Option<Place> {
    let w = app.get_webview_window(label)?;
    let origin = w.outer_position().ok()?;
    let scale = w.scale_factor().ok()?;
    let px = |v: f64| (v * scale).round();
    Some(Place {
        pos: PhysicalPosition::new(origin.x + px(r.x as f64) as i32, origin.y + px(r.y as f64) as i32),
        size: PhysicalSize::new(px(r.w as f64) as u32, px(r.h as f64) as u32),
    })
}

/// What a window of the overlay hears of the system. It remembers which window the person used
/// last; when no overlay window has the focus any more (another program took it), the overlay
/// goes (a short wait first: a click from one screen's window to another's passes through
/// "none"); and a window that Windows gave another DPI (a screen went away while the displays
/// slept, live 2026-11-20) is hidden and built anew at the next key press.
fn watch(app: &AppHandle, w: &WebviewWindow) {
    let (app, label) = (app.clone(), w.label().to_string());
    w.on_window_event(move |e| match e {
        WindowEvent::Focused(true) => {
            if let Ok(mut last) = app.state::<Overlay>().last.lock() {
                *last = Some(label.clone());
            }
        }
        WindowEvent::Focused(false) => {
            let app = app.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(200));
                let all = windows(&app);
                if all.iter().any(visible) && !all.iter().any(|w| w.is_focused().unwrap_or(false)) {
                    hide(&app);
                }
            });
        }
        WindowEvent::ScaleFactorChanged { .. } => {
            forget(&app, &label);
            let app = app.clone();
            // not inside the window's own event: hiding waits for it
            tauri::async_runtime::spawn(async move {
                if is_shown(&app) {
                    hide(&app);
                }
            });
        }
        _ => {}
    });
}

/// The Clipper key: the overlay comes up (when it is not) and Clipper goes to the window in front.
/// A page that is still loading takes the wish when it starts (`wantClipper`).
pub fn clipper(app: &AppHandle) {
    const JS: &str = "(function(c){if(!c)return;if(typeof c.attachClipper==='function')\
c.attachClipper();else c.wantClipper=true;})(window.topcoatClient);";
    if let Err(e) = ensure_shown(app) {
        eprintln!("the overlay could not open: {e}");
        return;
    }
    if let Some(w) = front(app) {
        let _ = w.eval(JS);
    }
}

// ---- the herald (the desktop plan, 5b) -----------------------------------------------------
// A small window at the corner of the primary screen: Clipper tells the person that a task they
// started is done. It runs the hub's page `?herald=1`, which reads the stream; it asks to be
// shown (`client=herald-show`) and hidden, and a click on it opens the task in the overlay. It
// never takes the focus (not focusable), so a game or an editor keeps the keyboard.

const HERALD: &str = "herald";
const HERALD_W: f64 = 420.0;
const HERALD_H: f64 = 300.0;

/// Each show (and each hide) counts up; a timer hides the herald only if nothing came after it.
static HERALD_SHOWN: AtomicU64 = AtomicU64::new(0);
/// The herald is gone after this, whatever the page did (2026-11-13: a page that was reloaded
/// while the herald showed never asked to hide it, and an empty, see-through window at the
/// screen's corner caught every click there). The page asks again for each message.
const HERALD_MAX: Duration = Duration::from_secs(20);

fn hide_herald_later(app: &AppHandle) {
    let mine = HERALD_SHOWN.fetch_add(1, Ordering::SeqCst) + 1;
    let app = app.clone();
    thread::spawn(move || {
        thread::sleep(HERALD_MAX);
        if HERALD_SHOWN.load(Ordering::SeqCst) == mine {
            if let Some(h) = app.get_webview_window(HERALD) {
                let _ = h.hide();
            }
        }
    });
}

fn place_herald(app: &AppHandle, h: &WebviewWindow) {
    let Ok(Some(m)) = app.primary_monitor() else { return };
    let area = m.work_area();
    let scale = m.scale_factor();
    let (w, hgt) = ((HERALD_W * scale) as i32, (HERALD_H * scale) as i32);
    let margin = (12.0 * scale) as i32;
    let x = area.position.x + area.size.width as i32 - w - margin;
    let y = area.position.y + area.size.height as i32 - hgt - margin;
    let _ = h.set_position(PhysicalPosition::new(x, y));
    let _ = h.set_size(PhysicalSize::new(w as u32, hgt as u32));
}

pub fn build_herald(app: &AppHandle) -> Result<(), String> {
    if app.get_webview_window(HERALD).is_some() {
        return Ok(());
    }
    let hub = hub_url(app)?;
    let mut url = hub.clone();
    url.set_query(Some("herald=1"));
    url.set_fragment(None);
    let w = guard(
        app,
        &hub,
        HERALD,
        WebviewWindowBuilder::new(app, HERALD, WebviewUrl::External(url))
            .title("Topcoat")
            .inner_size(HERALD_W, HERALD_H)
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .always_on_top(true)
            .skip_taskbar(true)
            .resizable(false)
            .focused(false)
            .focusable(false)
            .visible(false)
            .background_color(Color(0, 0, 0, 0))
            .initialization_script(&client_script(app)),
    )
    .build()
    .map_err(|e| e.to_string())?;
    place_herald(app, &w);
    Ok(())
}

