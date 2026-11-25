// no console window next to the client on Windows
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    topcoat_desktop_lib::run()
}
