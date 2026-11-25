//! The Topcoat desktop client: every program of the hub in its own, free window (positions kept
//! per machine), a global key that opens the program selector, and a tray icon. The windows show
//! the hub's own web GUI (`?solo=<key>`); the client adds windows, not a second GUI. The tiled
//! desktop with its taskbar stays the browser's: the client has no window for it.
//!
//! Only the local selector may call the commands below: the hub's pages get no IPC
//! (capabilities/default.json). When a page goes to another program, the client sees the address
//! and opens that program's window (`nav.rs`); pages outside the hub go to the system browser.
//! The client opens no port and keeps no token: the sign-in lives in the web view's own cookie
//! store, like in a browser.
//!
//! With `"mode": "overlay"` (the default) the key lays the overlay over all screens instead
//! (`overlay.rs`): the whole web desktop on a blurred backdrop, without native program windows.
//! `"mode": "windows"` keeps the selector and a native window per program.

mod nav;
mod overlay;

use std::{fs, path::PathBuf};

use nav::{is_program_key, Route};
use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    webview::NewWindowResponse,
    AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_opener::OpenerExt;
use url::Url;

const DEFAULT_KEY: &str = "ctrl+alt+space";

#[derive(Serialize, Deserialize, Clone)]
struct Config {
    hub: String,
    #[serde(default = "default_key")]
    key: String,
    /// "overlay" (the default) or "windows" (the selector and a native window per program).
    #[serde(default = "default_mode")]
    mode: String,
    /// The overlay's backdrop: "blur" (the default) or "none" (only the page's tint).
    #[serde(default = "default_effect")]
    effect: String,
    /// The global key that shows the overlay on every screen at once (AltGr+F); the overlay key
    /// shows it on the screen of the mouse. (A client.json of an older client may still name a
    /// `span`: it is read over, each screen has its own window now.)
    #[serde(default = "default_all_key")]
    all_key: String,
    /// The overlay's page starts with its frame meter shown (also TOPCOAT_DEBUG_FPS=1).
    #[serde(default)]
    fps: bool,
    /// The global key that attaches Clipper to the window in front (AltGr = ctrl+alt on Windows).
    #[serde(default = "default_clipper_key")]
    clipper_key: String,
}

fn default_clipper_key() -> String {
    "ctrl+alt+KeyC".into()
}

fn default_all_key() -> String {
    "ctrl+alt+KeyF".into()
}

impl Config {
    fn new(hub: String) -> Self {
        Config {
            hub,
            key: default_key(),
            mode: default_mode(),
            effect: default_effect(),
            all_key: default_all_key(),
            fps: false,
            clipper_key: default_clipper_key(),
        }
    }
}

fn default_effect() -> String {
    "blur".into()
}

fn default_key() -> String {
    DEFAULT_KEY.into()
}

fn default_mode() -> String {
    "overlay".into()
}

/// Does the key lay the overlay (a hub is set and the mode is not "windows")?
fn overlay_mode(app: &AppHandle) -> bool {
    load_config(app).is_some_and(|c| c.mode != "windows")
}

fn config_path(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("client.json"))
}

/// The hub's address: TOPCOAT_HUB_URL, else client.json in the app's config folder, else none.
fn load_config(app: &AppHandle) -> Option<Config> {
    let mut config = if let Ok(hub) = std::env::var("TOPCOAT_HUB_URL") {
        Config::new(hub)
    } else {
        let text = fs::read_to_string(config_path(app)?).ok()?;
        // an editor on Windows may have saved the file with a byte order mark
        serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()?
    };
    if std::env::var("TOPCOAT_DEBUG_FPS").is_ok_and(|v| v == "1") {
        config.fps = true;
    }
    Some(config)
}

fn save_config(app: &AppHandle, config: &Config) -> Result<(), String> {
    let path = config_path(app).ok_or("no config folder")?;
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let text = serde_json::to_string_pretty(config).map_err(|e| e.to_string())?;
    fs::write(path, text).map_err(|e| e.to_string())
}

/// The global key that shows the overlay, changed while the client runs (the settings of the
/// overlay's page). The new key is registered first; the old one stays when the system refuses
/// the new one (another program holds it), and only a key that works is written to client.json.
pub(crate) fn set_key(app: &AppHandle, slot: nav::KeySlot, key: &str) -> Result<(), String> {
    let new: Shortcut = key.parse().map_err(|e| format!("not a key: {e}"))?;
    let mut config = load_config(app).ok_or("no hub address set")?;
    let (current, others) = match slot {
        nav::KeySlot::Overlay => (&config.key, [&config.all_key, &config.clipper_key]),
        nav::KeySlot::All => (&config.all_key, [&config.key, &config.clipper_key]),
        nav::KeySlot::Clipper => (&config.clipper_key, [&config.key, &config.all_key]),
    };
    if others.iter().any(|o| o.parse::<Shortcut>().ok() == Some(new)) {
        return Err("that key already does another thing".into());
    }
    let old: Option<Shortcut> = current.parse().ok();
    if old == Some(new) {
        return Ok(());
    }
    let keys = app.global_shortcut();
    if let Some(o) = old {
        let _ = keys.unregister(o);
    }
    if let Err(e) = keys.register(new) {
        if let Some(o) = old {
            let _ = keys.register(o);
        }
        return Err(format!("the key could not be registered (another program may hold it): {e}"));
    }
    match slot {
        nav::KeySlot::Overlay => config.key = key.to_string(),
        nav::KeySlot::All => config.all_key = key.to_string(),
        nav::KeySlot::Clipper => config.clipper_key = key.to_string(),
    }
    save_config(app, &config)
}

/// Is `shortcut` the configured Clipper key?
fn is_clipper_key(app: &AppHandle, shortcut: &Shortcut) -> bool {
    let key = load_config(app).map(|c| c.clipper_key).unwrap_or_else(default_clipper_key);
    key.parse::<Shortcut>().ok().as_ref() == Some(shortcut)
}

/// Is `shortcut` the configured key for the overlay on every screen?
fn is_all_key(app: &AppHandle, shortcut: &Shortcut) -> bool {
    let key = load_config(app).map(|c| c.all_key).unwrap_or_else(default_all_key);
    key.parse::<Shortcut>().ok().as_ref() == Some(shortcut)
}

fn hub_url(app: &AppHandle) -> Result<Url, String> {
    let hub = load_config(app).map(|c| c.hub).ok_or("no hub address set")?;
    let parsed = Url::parse(&hub).map_err(|e| e.to_string())?;
    if parsed.scheme() != "https" && std::env::var("TOPCOAT_ALLOW_HTTP").is_err() {
        // all hub traffic is encrypted (D11); plain HTTP only with an explicit opt-in
        return Err("the hub address must start with https://".into());
    }
    Ok(parsed)
}

#[tauri::command]
fn get_hub(app: AppHandle) -> String {
    load_config(&app).map(|c| c.hub).unwrap_or_default()
}

#[tauri::command]
fn set_hub(app: AppHandle, hub: String) -> Result<(), String> {
    let parsed = Url::parse(hub.trim()).map_err(|e| e.to_string())?;
    if parsed.scheme() != "https" {
        return Err("the hub address must start with https://".into());
    }
    let mut config = load_config(&app).unwrap_or_else(|| Config::new(String::new()));
    config.hub = parsed.to_string();
    save_config(&app, &config)
}

/// Open the window of one program (or bring it forward) and put the selector away.
///
/// `async` on purpose: a synchronous command runs on the main thread, and building a web view
/// there waits for that same thread on Windows (the new window stayed white, the selector hung).
#[tauri::command]
async fn open_program(app: AppHandle, key: String) -> Result<(), String> {
    show_program(&app, &key, None, None, true)?;
    hide_selector(app);
    Ok(())
}

fn program_url(hub: &Url, key: &str) -> Url {
    let mut target = hub.clone();
    target.set_query(Some(&format!("solo={key}")));
    target.set_fragment(Some(&format!("/w/{key}")));
    target
}

/// Where a window goes on the screen, in physical pixels (a pin from the overlay).
#[derive(Clone, Copy)]
pub(crate) struct Place {
    pub pos: tauri::PhysicalPosition<i32>,
    pub size: tauri::PhysicalSize<u32>,
}

/// Open the window of program `key` or bring it forward. `to` is an address a page asked for:
/// when it names a view (`view=`), an open window goes there too. `place` puts the window there
/// (a pin); `focus` false leaves the focus where it is (the overlay stays up).
fn show_program(
    app: &AppHandle,
    key: &str,
    to: Option<Url>,
    place: Option<Place>,
    focus: bool,
) -> Result<(), String> {
    if !is_program_key(key) {
        return Err("not a program".into());
    }
    let label = format!("p-{key}");
    if let Some(w) = app.get_webview_window(&label) {
        if let Some(to) = to.filter(|u| u.query_pairs().any(|(k, _)| k == "view")) {
            let _ = w.navigate(to);
        }
        let _ = w.unminimize();
        if let Some(p) = place {
            let _ = w.set_position(p.pos);
            let _ = w.set_size(p.size);
        }
        let _ = w.show();
        if focus {
            let _ = w.set_focus();
        }
        return Ok(());
    }
    let hub = hub_url(app)?;
    let target = to.unwrap_or_else(|| program_url(&hub, key));
    let (nav_app, nav_hub, nav_own) = (app.clone(), hub.clone(), key.to_string());
    let (pop_app, pop_hub, pop_own) = (app.clone(), hub, key.to_string());
    let w = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(target))
        .title(format!("Topcoat · {key}"))
        .inner_size(1100.0, 780.0)
        .focused(focus)
        .on_navigation(move |to| follow(&nav_app, &nav_hub, &nav_own, to))
        .on_new_window(move |to, _features| {
            if follow(&pop_app, &pop_hub, &pop_own, &to) {
                NewWindowResponse::Allow // a page of the hub itself (a file), with the same sign-in
            } else {
                NewWindowResponse::Deny
            }
        })
        // the page names its program in the person's language
        .on_document_title_changed(|w, title| {
            let _ = w.set_title(&title);
        })
        .build()
        .map_err(|e| e.to_string())?;
    if let Some(p) = place {
        // after the build: the window-state plugin puts a known window back where it was
        let _ = w.set_position(p.pos);
        let _ = w.set_size(p.size);
    }
    Ok(())
}

/// What a program window does with an address its page goes to: true lets the page go there.
fn follow(app: &AppHandle, hub: &Url, own: &str, to: &Url) -> bool {
    match nav::route(hub, own, to) {
        Route::Stay => true,
        Route::Program(key) => {
            let (app, to) = (app.clone(), to.clone());
            // not inside the web view's own event: a window built there waits for itself
            tauri::async_runtime::spawn(async move {
                if let Err(e) = show_program(&app, &key, Some(to), None, true) {
                    eprintln!("the window of {key} could not open: {e}");
                }
            });
            false
        }
        Route::Outside => {
            if let Err(e) = app.opener().open_url(to.as_str(), None::<&str>) {
                eprintln!("the browser could not open {to}: {e}");
            }
            false
        }
        Route::Block => false,
    }
}

#[tauri::command]
fn hide_selector(app: AppHandle) {
    if let Some(w) = app.get_webview_window("selector") {
        let _ = w.hide();
    }
}

/// The global key: the overlay on the screen of the mouse (or on every screen with `all`), or the
/// selector (no hub yet, or the mode "windows").
fn on_key(app: &AppHandle, all: bool) {
    if overlay_mode(app) {
        hide_selector(app.clone());
        overlay::toggle(app, all);
    } else {
        toggle_selector(app);
    }
}

fn toggle_selector(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("selector") {
        if w.is_visible().unwrap_or(false) {
            let _ = w.hide();
        } else {
            let _ = w.center();
            let _ = w.show();
            let _ = w.set_focus();
            let _ = w.eval("window.topcoatSelectorShown && window.topcoatSelectorShown()");
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| on_key(app, false)))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state != ShortcutState::Pressed {
                        return;
                    }
                    if *shortcut == overlay::esc() {
                        // Esc is registered only while the overlay is up; the page closes its
                        // own palette or dialog first and asks to hide only when it has none
                        // (outside this handler: hiding unregisters a key from its own event)
                        let app = app.clone();
                        tauri::async_runtime::spawn(async move { overlay::escape(&app) });
                    } else if is_clipper_key(app, shortcut) {
                        let app = app.clone();
                        tauri::async_runtime::spawn(async move { overlay::clipper(&app) });
                    } else if is_all_key(app, shortcut) {
                        on_key(app, true);
                    } else {
                        on_key(app, false);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![get_hub, set_hub, open_program, hide_selector])
        .manage(overlay::Overlay::default())
        .setup(|app| {
            let handle = app.handle().clone();
            let key = load_config(&handle).map(|c| c.key).unwrap_or_else(default_key);
            if let Err(e) = handle.global_shortcut().register(key.as_str()) {
                eprintln!("the key {key} could not be registered: {e}");
            }
            if overlay_mode(&handle) {
                let ck = load_config(&handle).map(|c| c.clipper_key).unwrap_or_else(default_clipper_key);
                if let Err(e) = handle.global_shortcut().register(ck.as_str()) {
                    eprintln!("the Clipper key {ck} could not be registered: {e}");
                }
                let ak = load_config(&handle).map(|c| c.all_key).unwrap_or_else(default_all_key);
                if let Err(e) = handle.global_shortcut().register(ak.as_str()) {
                    eprintln!("the key {ak} (overlay on every screen) could not be registered: {e}");
                }
                // the herald waits at the screen's corner, hidden, until a task of the person ends
                let h = handle.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(e) = overlay::build_herald(&h) {
                        eprintln!("the herald could not be built: {e}");
                    }
                });
            }
            let show = MenuItem::with_id(app, "overlay", "Overlay", true, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "Programm wählen", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Beenden", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &open, &quit])?;
            let mut tray = TrayIconBuilder::new().menu(&menu).tooltip("Topcoat");
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.on_menu_event(|app, event| match event.id.as_ref() {
                "overlay" => on_key(app, true),
                "open" => toggle_selector(app),
                "quit" => app.exit(0),
                _ => {}
            })
            .build(app)?;
            if let Some(sel) = app.get_webview_window("selector") {
                let s = sel.clone();
                sel.on_window_event(move |e| {
                    if let WindowEvent::Focused(false) = e {
                        let _ = s.hide(); // the selector goes away when one clicks elsewhere
                    }
                });
                if load_config(&handle).is_none() {
                    let _ = sel.show(); // first start: ask for the hub's address
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("the Topcoat client could not start");
    app.run(|_app, event| {
        // closing the last program window keeps the client in the tray; "Beenden" ends it
        if let RunEvent::ExitRequested { api, code, .. } = event {
            if code.is_none() {
                api.prevent_exit();
            }
        }
    });
}
