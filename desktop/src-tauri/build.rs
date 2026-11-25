//! Every command of the app gets a permission (`allow-<command>`), and the capability in
//! `capabilities/default.json` lists the ones the window may call. Without this list Tauri would
//! allow every registered command to every window.

const COMMANDS: &[&str] = &[
    "app_info",
    "connections_list",
    "connection_save",
    "connection_delete",
    "connection_activate",
    "connection_token_from_clipboard",
    "connection_token_from_file",
    "connection_token_clear",
    "connection_check",
    "api_status",
    "api_workspaces",
    "api_sources",
    "api_catalog",
    "api_domain",
    "api_project_put",
    "api_tools",
    "api_tool_call",
    "api_tokens_list",
    "api_token_create",
    "api_token_revoke",
    "mcp_probe",
    "clients_list",
    "client_connect",
    "client_disconnect",
    "launcher_info",
    "launcher_set_path",
    "launcher_status",
    "launcher_change",
    "launcher_setup_token",
];

fn main() {
    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS));
    tauri_build::try_build(attributes).expect("the Tauri build script failed");
}
