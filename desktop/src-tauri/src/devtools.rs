//! A developer's switch for automated GUI checks of the real overlay (web/scripts/overlay-capture.mjs).
//!
//! `TOPCOAT_DEVTOOLS_PORT=<port>` in the client's environment starts WebView2 with the Chrome
//! DevTools protocol on that port, so a script on the same machine can look at the overlay's pages
//! and take screenshots. It is off unless the variable is set: whoever can reach the port controls
//! the pages (and the person's hub session in them). Chromium binds the port to loopback
//! (127.0.0.1) only; there is no setting in client.json or in the GUI, on purpose. The variable is
//! read once at start; a client started without it never opens the port.
//!
//! UNVERIFIED on real hardware: WebView2 reading WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS for the
//! web views Tauri builds.

const VAR: &str = "TOPCOAT_DEVTOOLS_PORT";
const WEBVIEW2_ARGS: &str = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS";

/// The port from the variable's value: a number from 1024 to 65535, nothing else.
pub fn port_of(value: &str) -> Option<u16> {
    let port: u16 = value.trim().parse().ok()?;
    (port >= 1024).then_some(port)
}

/// The browser arguments with the DevTools port added to what was there.
pub fn browser_args(existing: Option<&str>, port: u16) -> String {
    let add = format!("--remote-debugging-port={port} --remote-debugging-address=127.0.0.1");
    match existing.map(str::trim).filter(|e| !e.is_empty()) {
        Some(e) => format!("{e} {add}"),
        None => add,
    }
}

/// Turn the DevTools port on when the developer asked for it in the environment.
pub fn enable_from_env() {
    let Some(port) = std::env::var(VAR).ok().as_deref().and_then(port_of) else {
        return;
    };
    let args = browser_args(std::env::var(WEBVIEW2_ARGS).ok().as_deref(), port);
    std::env::set_var(WEBVIEW2_ARGS, args);
    eprintln!("topcoat: DevTools on 127.0.0.1:{port} ({VAR}); for GUI checks only");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_a_real_unprivileged_port_counts() {
        assert_eq!(port_of("9222"), Some(9222));
        assert_eq!(port_of(" 9333 "), Some(9333));
        for bad in ["", "0", "80", "1023", "70000", "9222; --x", "abc"] {
            assert_eq!(port_of(bad), None, "{bad}");
        }
    }

    #[test]
    fn the_port_is_added_to_existing_arguments() {
        assert_eq!(
            browser_args(None, 9222),
            "--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1"
        );
        assert_eq!(
            browser_args(Some("--disable-gpu"), 9222),
            "--disable-gpu --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1"
        );
    }
}
