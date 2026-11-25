// No console window next to the app's window on Windows, in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    memini_desktop_lib::run();
}
