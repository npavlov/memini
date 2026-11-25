//! The Memini desktop app: a native shell that manages a Memini server.
//!
//! The Rust side owns everything that needs trust: the tokens (in the operating system's
//! keychain), every HTTP call to the Memini API, and the one external program the app runs, the
//! `memini-server` launcher. The webview shows what these return and asks for actions by name.
//!
//! - [`commands`]: the commands the webview may call.
//! - [`api`]: the HTTP client of the Memini API.
//! - [`launcher`]: finding and running `memini-server`, and reading its JSON.
//! - [`keychain`]: tokens in the operating system's keychain.
//! - [`store`]: the settings file with the saved connections; no secrets.
//! - [`tools`]: the read tools the Data screen may run, and the request a call becomes.
//! - [`secret`], [`error`]: the token type that cannot be serialized, and the error shape whose
//!   text is masked before it leaves Rust.

mod api;
mod commands;
mod error;
mod keychain;
mod launcher;
mod secret;
mod store;
mod tools;

use tauri::Manager;

/// Starts the app. Called by the binary.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_clipboard_manager::init())
        .setup(|app| {
            let directory = app.path().app_config_dir()?;
            let store = store::Store::open(directory);
            let state = commands::AppState::new(store, Box::new(keychain::Keychain))?;
            app.manage(state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::app_info,
            commands::connections_list,
            commands::connection_save,
            commands::connection_delete,
            commands::connection_activate,
            commands::connection_token_from_clipboard,
            commands::connection_token_from_file,
            commands::connection_token_clear,
            commands::connection_check,
            commands::api_status,
            commands::api_workspaces,
            commands::api_sources,
            commands::api_tools,
            commands::api_tool_call,
            commands::api_tokens_list,
            commands::api_token_create,
            commands::api_token_revoke,
            commands::mcp_probe,
            commands::launcher_info,
            commands::launcher_set_path,
            commands::launcher_status,
            commands::launcher_change,
            commands::launcher_setup_token,
        ])
        .run(tauri::generate_context!())
        .expect("the Memini desktop app could not start");
}
