//! Where an address that a program window's page goes to belongs: in that window, in the window of
//! another program, or in the system browser. No Tauri types here, so the rules are tested alone.

use url::Url;

#[derive(Debug, PartialEq, Eq)]
pub enum Route {
    /// The window's own program (its sign-in, its views): the page goes there itself.
    Stay,
    /// Another program of the hub (`?solo=<key>`): its own window opens or comes forward.
    Program(String),
    /// A page outside the hub (a source of a report): the system browser shows it.
    Outside,
    /// Anything else (`file:`, custom schemes): nothing happens.
    Block,
}

/// A program key as the hub writes it: lower-case letters, digits and dashes.
pub fn is_program_key(k: &str) -> bool {
    !k.is_empty()
        && k.len() <= 40
        && k.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// Where `to` belongs when the window of program `own` on the hub `hub` goes there.
pub fn route(hub: &Url, own: &str, to: &Url) -> Route {
    match to.scheme() {
        "http" | "https" => {}
        "about" => return Route::Stay,
        _ => return Route::Block,
    }
    if to.origin() != hub.origin() {
        return Route::Outside;
    }
    match to.query_pairs().find(|(k, _)| k == "solo") {
        Some((_, key)) if key != own && is_program_key(&key) => Route::Program(key.into_owned()),
        _ => Route::Stay,
    }
}

/// The address of the overlay's desktop on one screen: the hub's whole web desktop on a
/// see-through page. `screen` names the screen (`screen_id`); `own` says that it is not the
/// primary one: its page keeps a desktop of its own (the primary one shows the person's desktop
/// from the hub). `fps` starts the page with its frame meter shown.
pub fn overlay_url(hub: &Url, screen: &str, own: bool, fps: bool) -> Url {
    let mut target = hub.clone();
    let mut query = String::from("overlay=1");
    if !screen.is_empty() {
        query.push_str(&format!("&screen={screen}"));
        if own {
            query.push_str("&own=1");
        }
    }
    if fps {
        query.push_str("&fps=1");
    }
    target.set_query(Some(&query));
    target.set_fragment(None);
    target
}

/// A screen in physical pixels: x, y, width, height.
pub type Screen = (i32, i32, u32, u32);

/// A screen's name as the overlay uses it (a window label and the page's `screen=`): the system's
/// name in lower-case letters and digits (`\\.\DISPLAY2` is `display2`), else its place in the
/// list. It stays the same from one start to the next as long as Windows keeps its numbering.
pub fn screen_id(name: Option<&str>, index: usize) -> String {
    let id: String = name
        .unwrap_or_default()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .map(|c| c.to_ascii_lowercase())
        .take(32)
        .collect();
    if id.is_empty() { format!("screen{index}") } else { id }
}

/// The screen that holds the point (the mouse), else the nearest one; None without screens.
pub fn screen_at(point: (f64, f64), screens: &[Screen]) -> Option<usize> {
    let (px, py) = point;
    let distance = |s: &Screen| {
        let (x, y, w, h) = (s.0 as f64, s.1 as f64, s.2 as f64, s.3 as f64);
        let dx = (x - px).max(0.0).max(px - (x + w));
        let dy = (y - py).max(0.0).max(py - (y + h));
        dx * dx + dy * dy
    };
    let inside = |s: &Screen| {
        let (x, y) = (s.0 as f64, s.1 as f64);
        px >= x && px < x + s.2 as f64 && py >= y && py < y + s.3 as f64
    };
    screens.iter().position(inside).or_else(|| {
        (0..screens.len()).min_by(|&a, &b| distance(&screens[a]).total_cmp(&distance(&screens[b])))
    })
}

/// The screen after `from` in the list, round (where a program goes that is sent on).
pub fn next_screen(from: usize, count: usize) -> Option<usize> {
    (count > 1).then(|| (from + 1) % count)
}

/// A place on the overlay's page, in its CSS pixels: left, top, width, height.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    pub h: u32,
}

/// Where an address of the overlay's desktop belongs.
#[derive(Debug, PartialEq, Eq)]
pub enum OverlayRoute {
    /// The hub: the overlay has every program itself.
    Stay,
    /// A program pinned to the normal desktop (`?solo=<key>&pin=x,y,w,h`): a native window opens at
    /// that place, the overlay's page stays.
    Pin(String, Rect),
    /// A request of the page to the client itself (`?client=<verb>&...`): the page stays.
    Client(ClientCmd),
    Outside,
    Block,
}

/// What the overlay's page may ask of the client, a fixed list (the hub's pages get no IPC; this
/// is the one way back, by an address the client stops and reads). Nothing here reads anything
/// from the machine: the page can hide the overlay and change the client's own settings.
#[derive(Debug, PartialEq, Eq, Clone)]
pub enum ClientCmd {
    /// Hide the overlay (Esc, when the page had nothing of its own to close).
    Hide,
    /// A global key of the client: the one that shows the overlay on the screen of the mouse
    /// (`ctrl+alt+Space`), the one that shows it on every screen (`ctrl+alt+KeyF`, AltGr+F) or the
    /// one that attaches Clipper to the window in front (`ctrl+alt+KeyC`, AltGr+C).
    SetKey { slot: KeySlot, key: String },
    /// The herald (a small window at the screen's corner) shows itself or goes.
    HeraldShow,
    HeraldHide,
    /// The overlay opens a task (the herald's message was clicked): only an id.
    OpenTask(String),
    /// How the overlay looks: its backdrop.
    SetLook { effect: Effect },
    /// A program goes to the overlay of the next screen, on this view (a module of the hub, e.g.
    /// `page.chat`): that page opens it, this one has closed it already.
    Send(String),
}

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum KeySlot {
    Overlay,
    All,
    Clipper,
}

impl KeySlot {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "overlay" => Some(Self::Overlay),
            "all" => Some(Self::All),
            "clipper" => Some(Self::Clipper),
            _ => None,
        }
    }
}

/// A task id as the hub writes it (a UUID), nothing else.
pub fn task_id_ok(id: &str) -> bool {
    id.len() == 36
        && id.chars().enumerate().all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) { c == '-' } else { c.is_ascii_hexdigit() }
        })
}

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Effect {
    /// The screen behind blurred (Windows' blur behind the window).
    Blur,
    /// Only the page's own dark tint: no backdrop effect to compose.
    None,
}

impl Effect {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "blur" => Some(Self::Blur),
            "none" => Some(Self::None),
            _ => None,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Blur => "blur",
            Self::None => "none",
        }
    }
}

/// A module of the hub as a program's view names it (`page.chat`, `page.dev-workshop`).
pub fn module_ok(m: &str) -> bool {
    !m.is_empty()
        && m.len() <= 60
        && m.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '-' | '_'))
}

/// A key the page may set: modifiers and one key, `+` between them, at least one of ctrl, alt or
/// super (a plain letter or shift+letter would swallow typing everywhere). Whether the system
/// takes it is the global-shortcut parser's and RegisterHotKey's word.
pub fn key_ok(key: &str) -> bool {
    if key.is_empty() || key.len() > 60 {
        return false;
    }
    let parts: Vec<&str> = key.split('+').collect();
    let Some((last, mods)) = parts.split_last() else { return false };
    let lower = |p: &str| p.to_ascii_lowercase();
    let is_mod = |p: &str| matches!(lower(p).as_str(), "ctrl" | "alt" | "shift" | "super");
    let strong = mods.iter().any(|m| matches!(lower(m).as_str(), "ctrl" | "alt" | "super"));
    !last.is_empty()
        && !is_mod(last)
        && last.chars().all(|c| c.is_ascii_alphanumeric())
        && mods.iter().all(|m| is_mod(m))
        && strong
}

/// The client's part of an address of the hub (`?client=...`), if it is one the client knows.
pub fn client_cmd(to: &Url) -> Option<ClientCmd> {
    let get = |name: &str| to.query_pairs().find(|(k, _)| k == name).map(|(_, v)| v.into_owned());
    match get("client")?.as_str() {
        "hide" => Some(ClientCmd::Hide),
        "set-key" => {
            let slot = KeySlot::parse(&get("slot").unwrap_or_else(|| "overlay".into()))?;
            get("key").filter(|k| key_ok(k)).map(|key| ClientCmd::SetKey { slot, key })
        }
        "herald-show" => Some(ClientCmd::HeraldShow),
        "herald-hide" => Some(ClientCmd::HeraldHide),
        "open-task" => get("id").filter(|i| task_id_ok(i)).map(ClientCmd::OpenTask),
        "set-look" => get("effect").and_then(|e| Effect::parse(&e)).map(|effect| ClientCmd::SetLook { effect }),
        "send" => get("module").filter(|m| module_ok(m)).map(ClientCmd::Send),
        _ => None,
    }
}

/// `pin=x,y,w,h` as the web desktop writes it (`floating.ts`, `pinHref`); a window smaller than
/// 120 x 80 or larger than 16384 is not one.
pub fn pin_rect(to: &Url) -> Option<Rect> {
    let (_, raw) = to.query_pairs().find(|(k, _)| k == "pin")?;
    let n: Vec<i64> = raw.split(',').map(|p| p.trim().parse().ok()).collect::<Option<_>>()?;
    let [x, y, w, h] = n[..] else { return None };
    let ok = |v: i64, lo: i64| (lo..=16384).contains(&v);
    if !(ok(w, 120) && ok(h, 80) && ok(x, -16384) && ok(y, -16384)) {
        return None;
    }
    Some(Rect { x: x as i32, y: y as i32, w: w as u32, h: h as u32 })
}

/// Where `to` belongs when the overlay's desktop goes there.
pub fn overlay_route(hub: &Url, to: &Url) -> OverlayRoute {
    let route = route(hub, "", to);
    if route != Route::Outside && route != Route::Block && to.query_pairs().any(|(k, _)| k == "client") {
        // a request to the client never loads a page, also one the client does not know
        return client_cmd(to).map_or(OverlayRoute::Block, OverlayRoute::Client);
    }
    match route {
        Route::Program(key) => match pin_rect(to) {
            Some(rect) => OverlayRoute::Pin(key, rect),
            None => OverlayRoute::Stay,
        },
        Route::Stay => OverlayRoute::Stay,
        Route::Outside => OverlayRoute::Outside,
        Route::Block => OverlayRoute::Block,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    #[test]
    fn the_own_program_and_the_sign_in_stay_in_the_window() {
        let hub = u("https://hub.example.lan/");
        assert_eq!(route(&hub, "dev", &u("https://hub.example.lan/?solo=dev#/w/dev")), Route::Stay);
        assert_eq!(route(&hub, "dev", &u("https://hub.example.lan/login")), Route::Stay);
        assert_eq!(route(&hub, "dev", &u("about:blank")), Route::Stay);
    }

    #[test]
    fn another_program_gets_its_own_window() {
        let hub = u("https://hub.example.lan/");
        let to = u("https://hub.example.lan/?solo=research&view=%7B%7D#/w/research");
        assert_eq!(route(&hub, "chat", &to), Route::Program("research".into()));
    }

    #[test]
    fn a_key_the_hub_would_not_write_stays() {
        let hub = u("https://hub.example.lan/");
        assert_eq!(route(&hub, "chat", &u("https://hub.example.lan/?solo=../x")), Route::Stay);
        assert_eq!(route(&hub, "chat", &u("https://hub.example.lan/?solo=Dev")), Route::Stay);
    }

    #[test]
    fn other_sites_go_to_the_browser_and_other_schemes_nowhere() {
        let hub = u("https://hub.example.lan:8443/");
        assert_eq!(route(&hub, "chat", &u("https://example.org/a")), Route::Outside);
        // the same host on another port is another site
        assert_eq!(route(&hub, "chat", &u("https://hub.example.lan/?solo=dev")), Route::Outside);
        assert_eq!(route(&hub, "chat", &u("file:///C:/x.txt")), Route::Block);
    }

    #[test]
    fn the_overlay_shows_the_whole_desktop_and_keeps_every_program() {
        let hub = u("https://hub.example.lan:8443/");
        assert_eq!(overlay_url(&hub, "", false, false).as_str(), "https://hub.example.lan:8443/?overlay=1");
        assert_eq!(
            overlay_url(&hub, "display1", false, false).as_str(),
            "https://hub.example.lan:8443/?overlay=1&screen=display1"
        );
        assert_eq!(
            overlay_url(&hub, "display2", true, false).as_str(),
            "https://hub.example.lan:8443/?overlay=1&screen=display2&own=1"
        );
        assert_eq!(overlay_url(&hub, "", false, true).as_str(), "https://hub.example.lan:8443/?overlay=1&fps=1");
        let solo = u("https://hub.example.lan:8443/?solo=research#/w/research");
        assert_eq!(overlay_route(&hub, &solo), OverlayRoute::Stay);
        assert_eq!(overlay_route(&hub, &u("https://example.org/a")), OverlayRoute::Outside);
        assert_eq!(overlay_route(&hub, &u("file:///C:/x.txt")), OverlayRoute::Block);
    }

    #[test]
    fn each_screen_has_its_own_window_and_the_mouse_picks_one() {
        // primary 3840x2160 at 0,0; a 2560x1440 screen to its right, higher by 730
        let screens = [(0, 0, 3840, 2160), (3840, -730, 2560, 1440)];
        assert_eq!(screen_at((100.0, 100.0), &screens), Some(0));
        assert_eq!(screen_at((4000.0, -700.0), &screens), Some(1));
        // in a corner no one sees: the nearest screen (just below the right one, further down
        // beside the primary one)
        assert_eq!(screen_at((4000.0, 800.0), &screens), Some(1));
        assert_eq!(screen_at((4000.0, 1500.0), &screens), Some(0));
        assert_eq!(screen_at((-50.0, 2000.0), &screens), Some(0));
        assert_eq!(screen_at((0.0, 0.0), &[]), None);
        assert_eq!(screen_id(Some(r"\\.\DISPLAY2"), 1), "display2");
        assert_eq!(screen_id(None, 1), "screen1");
        assert_eq!(screen_id(Some("..."), 0), "screen0");
        assert_eq!(next_screen(0, 2), Some(1));
        assert_eq!(next_screen(1, 2), Some(0));
        assert_eq!(next_screen(0, 1), None);
    }

    #[test]
    fn a_pin_opens_the_program_natively_at_its_place() {
        let hub = u("https://hub.example.lan:8443/");
        let to = u("https://hub.example.lan:8443/?solo=wiki&view=%7B%7D&pin=10,-20,600,400#/w/wiki");
        let rect = Rect { x: 10, y: -20, w: 600, h: 400 };
        assert_eq!(overlay_route(&hub, &to), OverlayRoute::Pin("wiki".into(), rect));
        // a pin the web desktop would not write is no pin
        for bad in ["1,2,3", "a,b,c,d", "0,0,50,400", "0,0,600,99999", "0,0,600,400,1"] {
            let to = u(&format!("https://hub.example.lan:8443/?solo=wiki&pin={bad}"));
            assert_eq!(overlay_route(&hub, &to), OverlayRoute::Stay, "{bad}");
        }
        // on another site a pin is just an outside page
        let other = u("https://example.org/?solo=wiki&pin=0,0,600,400");
        assert_eq!(overlay_route(&hub, &other), OverlayRoute::Outside);
    }

    #[test]
    fn the_page_asks_the_client_by_an_address_it_stops() {
        let hub = u("https://hub.example.lan:8443/");
        let at = |q: &str| overlay_route(&hub, &u(&format!("https://hub.example.lan:8443/?{q}")));
        assert_eq!(at("client=hide"), OverlayRoute::Client(ClientCmd::Hide));
        assert_eq!(
            at("client=set-key&key=ctrl%2Balt%2BKeyC"),
            OverlayRoute::Client(ClientCmd::SetKey { slot: KeySlot::Overlay, key: "ctrl+alt+KeyC".into() })
        );
        assert_eq!(at("client=set-look&effect=none"), OverlayRoute::Client(ClientCmd::SetLook { effect: Effect::None }));
        assert_eq!(
            at("client=set-key&slot=all&key=ctrl%2Balt%2BKeyF"),
            OverlayRoute::Client(ClientCmd::SetKey { slot: KeySlot::All, key: "ctrl+alt+KeyF".into() })
        );
        assert_eq!(at("client=send&module=page.chat"), OverlayRoute::Client(ClientCmd::Send("page.chat".into())));
        // what the client does not know loads nothing either (the screens are no setting any more)
        for bad in [
            "client=shell&cmd=x",
            "client=set-key&key=x",
            "client=set-look&effect=acrylic",
            "client=set-look&span=primary",
            "client=send&module=Page%20Chat",
            "client=send",
            "client=",
        ] {
            assert_eq!(at(bad), OverlayRoute::Block, "{bad}");
        }
        // another site cannot ask: it is an outside page
        let other = u("https://example.org/?client=hide");
        assert_eq!(overlay_route(&hub, &other), OverlayRoute::Outside);
    }

    #[test]
    fn a_key_needs_ctrl_alt_or_super_and_one_plain_key() {
        for good in ["ctrl+alt+Space", "ctrl+alt+KeyC", "super+shift+F5", "alt+Digit1", "CTRL+ALT+space"] {
            assert!(key_ok(good), "{good}");
        }
        for bad in ["KeyC", "shift+KeyC", "ctrl+alt", "ctrl+alt+", "ctrl+alt+Key-C", "ctrl++Space", "meta+KeyC", ""] {
            assert!(!key_ok(bad), "{bad}");
        }
    }

    #[test]
    fn the_clipper_key_the_herald_and_a_task_are_known_requests() {
        let hub = u("https://hub.example.lan:8443/");
        let at = |q: &str| overlay_route(&hub, &u(&format!("https://hub.example.lan:8443/?{q}")));
        assert_eq!(
            at("client=set-key&slot=clipper&key=ctrl%2Balt%2BKeyC"),
            OverlayRoute::Client(ClientCmd::SetKey { slot: KeySlot::Clipper, key: "ctrl+alt+KeyC".into() })
        );
        assert_eq!(at("client=set-key&slot=boss&key=ctrl%2Balt%2BKeyC"), OverlayRoute::Block);
        assert_eq!(at("client=herald-show"), OverlayRoute::Client(ClientCmd::HeraldShow));
        assert_eq!(at("client=herald-hide"), OverlayRoute::Client(ClientCmd::HeraldHide));
        let id = "7df190c2-0748-4384-9044-5918a00a525b";
        assert_eq!(at(&format!("client=open-task&id={id}")), OverlayRoute::Client(ClientCmd::OpenTask(id.into())));
        for bad in ["client=open-task&id=../x", "client=open-task&id=7df190c2-0748-4384-9044-5918a00a525", "client=open-task"] {
            assert_eq!(at(bad), OverlayRoute::Block, "{bad}");
        }
    }
}
