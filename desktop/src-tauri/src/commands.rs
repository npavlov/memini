//! The commands the webview may call. They are the app's whole surface towards its front end.
//!
//! What crosses this boundary:
//!
//! - In: the user's input (a connection's name and address, a path, a token's name and scopes)
//!   and the ids of things to act on. Never an argument for the launcher, never a URL to call.
//! - Out: saved connections without secrets, the launcher's parsed JSON, and the API's answers.
//!   A token leaves Rust in one answer only: the creation of a new token, which the API itself
//!   shows once ([`api_token_create`]).
//!
//! A connection's token is entered without the webview: it is read from the clipboard or from a
//! file by Rust and goes straight to the keychain.
//!
//! Every command must also be listed in `build.rs` and allowed in `capabilities/default.json`;
//! a command that is only registered here cannot be called.

pub mod agent_clients;
pub mod checking;
pub mod repositories;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, State};
use tauri_plugin_clipboard_manager::ClipboardExt;

use crate::api::{failure, interpret, ApiClient, Target};
use crate::error::{clean, AppError, AppResult, ErrorKind};
use crate::host_model;
use crate::keychain::SecretStore;
use crate::launcher::{self, Action, LauncherResult, LauncherStatus, SearchPath};
use crate::secret::Token;
use crate::store::{
    check_url, is_loopback, ConnectionInput, ConnectionKind, ConnectionsView, Settings, Store,
    DEFAULT_LOCAL_URL,
};
use crate::tools::{self, ToolCall, ToolDescription};

/// The largest token file that is read, in bytes. A token is 50 characters.
const MAX_TOKEN_FILE: u64 = 4096;

const TOKEN_FORMAT: &str = "A token is memini_ followed by 43 letters, digits, '-' or '_'.";

/// The app's state, shared by every command.
pub struct AppState(Arc<Inner>);

pub struct Inner {
    store: Mutex<Store>,
    secrets: Box<dyn SecretStore>,
    http: ApiClient,
    launcher_busy: AtomicBool,
    clients_busy: AtomicBool,
    /// A download of the search model or an installation of Ollama on this computer runs.
    host_busy: AtomicBool,
    host_progress: host_model::Progress,
}

impl AppState {
    pub fn new(store: Store, secrets: Box<dyn SecretStore>) -> AppResult<Self> {
        Ok(Self(Arc::new(Inner {
            store: Mutex::new(store),
            secrets,
            http: ApiClient::new()?,
            launcher_busy: AtomicBool::new(false),
            clients_busy: AtomicBool::new(false),
            host_busy: AtomicBool::new(false),
            host_progress: host_model::Progress::default(),
        })))
    }

    /// The shared state. Not named `inner`: `tauri::State` has a method of that name.
    fn shared(&self) -> Arc<Inner> {
        Arc::clone(&self.0)
    }
}

impl Inner {
    fn lock(&self) -> AppResult<MutexGuard<'_, Store>> {
        self.store
            .lock()
            .map_err(|_| AppError::new(ErrorKind::Storage, "The settings are not available."))
    }

    fn view(&self) -> AppResult<ConnectionsView> {
        Ok(self.lock()?.settings.view())
    }

    /// Changes the settings and writes them. If either fails, the settings stay as they were.
    fn update<T>(&self, change: impl FnOnce(&mut Settings) -> AppResult<T>) -> AppResult<T> {
        let mut store = self.lock()?;
        let before = store.settings.clone();
        let changed = change(&mut store.settings);
        let result = match changed {
            Ok(value) => store.save().map(|()| value),
            Err(error) => Err(error),
        };
        if result.is_err() {
            store.settings = before;
        }
        result
    }

    fn save_connection(&self, input: &ConnectionInput) -> AppResult<ConnectionsView> {
        let saved = self.update(|settings| settings.save_connection(input))?;
        if saved.drop_token {
            // The address changed: the token was stored for the old one.
            self.secrets.delete(&saved.id)?;
        }
        self.view()
    }

    fn delete_connection(&self, id: &str) -> AppResult<ConnectionsView> {
        self.lock()?.settings.find(id)?;
        // The token first: a connection is not forgotten while its token stays behind.
        self.secrets.delete(id)?;
        self.update(|settings| settings.remove_connection(id))?;
        self.view()
    }

    fn activate(&self, id: &str) -> AppResult<ConnectionsView> {
        self.update(|settings| settings.activate(id))?;
        self.view()
    }

    /// Stores `text` as the token of a connection, if it is one. `source` names where the text
    /// came from, for the message; the text itself is never part of a message.
    fn store_token(&self, id: &str, text: &str, source: &str) -> AppResult<ConnectionsView> {
        self.lock()?.settings.find(id)?;
        let token = Token::parse(text).ok_or_else(|| {
            AppError::invalid(format!(
                "The {source} does not hold a Memini token. {TOKEN_FORMAT}"
            ))
        })?;
        self.secrets.set(id, &token)?;
        self.update(|settings| settings.set_has_token(id, true))?;
        self.view()
    }

    fn clear_token(&self, id: &str) -> AppResult<ConnectionsView> {
        self.lock()?.settings.find(id)?;
        self.secrets.delete(id)?;
        self.update(|settings| settings.set_has_token(id, false))?;
        self.view()
    }

    /// Makes the server on this computer a connection that holds `token`. The local connection
    /// is created when there is none, at the address the launcher reports if that is on this
    /// computer; it becomes the active connection. The token goes to the keychain and nowhere else.
    fn adopt_local(&self, token: &Token, reported: Option<&str>) -> AppResult<ConnectionsView> {
        let id = self.update(|settings| {
            let existing = settings
                .connections
                .iter()
                .find(|connection| connection.kind == ConnectionKind::Local);
            if let Some(local) = existing {
                return Ok(local.id.clone());
            }
            let url = reported
                .filter(|url| check_url(url, ConnectionKind::Local, false).is_ok())
                .unwrap_or(DEFAULT_LOCAL_URL);
            let input = ConnectionInput {
                id: None,
                name: "This computer".to_owned(),
                kind: ConnectionKind::Local,
                url: url.to_owned(),
                allow_http: false,
            };
            settings.save_connection(&input).map(|saved| saved.id)
        })?;
        self.secrets.set(&id, token)?;
        self.update(|settings| {
            settings.set_has_token(&id, true)?;
            settings.activate(&id)
        })?;
        self.view()
    }

    /// Forgets the local connection, and its token, when it names the server at `removed`: the
    /// server the launcher has just removed. A local connection to another server stays.
    fn forget_local(&self, removed: Option<&str>) -> AppResult<ConnectionsView> {
        let found = {
            let store = self.lock()?;
            store
                .settings
                .connections
                .iter()
                .find(|connection| {
                    connection.kind == ConnectionKind::Local
                        && removed.is_some_and(|url| same_server(&connection.url, url))
                })
                .map(|connection| connection.id.clone())
        };
        match found {
            Some(id) => self.delete_connection(&id),
            None => self.view(),
        }
    }

    fn target(&self, id: &str) -> AppResult<Target> {
        Target::of(self.lock()?.settings.find(id)?)
    }

    /// The token of a connection, from the keychain; `None` when none is stored.
    fn token(&self, id: &str) -> AppResult<Option<Token>> {
        if !self.lock()?.settings.find(id)?.has_token {
            return Ok(None);
        }
        let token = self.secrets.get(id)?;
        if token.is_none() {
            // Removed in the keychain's own viewer: the settings follow.
            self.update(|settings| settings.set_has_token(id, false))?;
        }
        Ok(token)
    }

    /// The program to run and the search path it is run with.
    fn launcher(&self) -> AppResult<(PathBuf, SearchPath)> {
        let configured = self.lock()?.settings.launcher_path.clone();
        let search = SearchPath::from_environment();
        let executable = std::env::current_exe().ok();
        let (program, _) = launcher::locate(configured.as_deref(), executable.as_deref(), &search)
            .ok_or_else(launcher::missing)?;
        Ok((program, search))
    }

    fn launcher_info(&self) -> AppResult<LauncherInfo> {
        let configured = self.lock()?.settings.launcher_path.clone();
        let executable = std::env::current_exe().ok();
        let found = launcher::locate(
            configured.as_deref(),
            executable.as_deref(),
            &SearchPath::from_environment(),
        );
        Ok(LauncherInfo {
            configured_path: configured,
            resolved_path: found
                .as_ref()
                .map(|(path, _)| path.to_string_lossy().into_owned()),
            source: found.map(|(_, source)| source),
        })
    }

    fn set_launcher_path(&self, path: Option<&str>) -> AppResult<LauncherInfo> {
        let path = path.map(str::trim).filter(|text| !text.is_empty());
        let checked = match path {
            Some(text) => {
                let checked = launcher::check_configured_path(text)?;
                if !checked.is_file() {
                    return Err(AppError::invalid("There is no file at that path."));
                }
                Some(checked.to_string_lossy().into_owned())
            }
            None => None,
        };
        self.update(|settings| {
            settings.launcher_path = checked;
            Ok(())
        })?;
        self.launcher_info()
    }
}

/// Runs work that may wait (the keychain can ask the user, a file can be on a slow disk) off the
/// threads that serve the webview.
async fn blocking<T, F>(work: F) -> AppResult<T>
where
    T: Send + 'static,
    F: FnOnce() -> AppResult<T> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(work)
        .await
        .map_err(|_| {
            AppError::new(
                ErrorKind::Unexpected,
                "A background task of the app failed.",
            )
        })?
}

/// The target and the token of a connection, for a call that needs the token.
async fn authorized(inner: &Arc<Inner>, id: &str) -> AppResult<(Target, Token)> {
    let target = inner.target(id)?;
    let (shared, id) = (Arc::clone(inner), id.to_owned());
    let token = blocking(move || shared.token(&id)).await?.ok_or_else(|| {
        AppError::new(
            ErrorKind::NoToken,
            "This connection has no token. Store one on the Connection screen.",
        )
    })?;
    Ok((target, token))
}

#[derive(Debug, Clone, Serialize)]
pub struct AppInfo {
    pub version: &'static str,
    /// `macos`, `windows` or `linux`: which install commands the Server screen shows.
    pub os: &'static str,
    /// Where a local server answers unless the launcher says otherwise.
    pub default_local_url: &'static str,
}

#[tauri::command]
pub fn app_info() -> AppInfo {
    AppInfo {
        version: env!("CARGO_PKG_VERSION"),
        os: std::env::consts::OS,
        default_local_url: DEFAULT_LOCAL_URL,
    }
}

#[tauri::command]
pub fn connections_list(state: State<'_, AppState>) -> AppResult<ConnectionsView> {
    state.0.view()
}

#[tauri::command]
pub async fn connection_save(
    state: State<'_, AppState>,
    input: ConnectionInput,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || inner.save_connection(&input)).await
}

#[tauri::command]
pub async fn connection_delete(
    state: State<'_, AppState>,
    id: String,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || inner.delete_connection(&id)).await
}

#[tauri::command]
pub async fn connection_activate(
    state: State<'_, AppState>,
    id: String,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || inner.activate(&id)).await
}

/// Takes the connection's token from the clipboard, in Rust, and clears the clipboard once the
/// token is in the keychain. The webview learns only that a token is stored.
#[tauri::command]
pub async fn connection_token_from_clipboard(
    app: AppHandle,
    state: State<'_, AppState>,
    id: String,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || {
        let text = app.clipboard().read_text().map_err(|_| {
            AppError::new(
                ErrorKind::Clipboard,
                "The clipboard holds no text, or cannot be read.",
            )
        })?;
        let view = inner.store_token(&id, &text, "clipboard")?;
        let _ = app.clipboard().clear();
        Ok(view)
    })
    .await
}

/// Takes the connection's token from a file the user names, such as the token file the install
/// guide creates. The file's content never reaches the webview, whatever it is.
#[tauri::command]
pub async fn connection_token_from_file(
    state: State<'_, AppState>,
    id: String,
    path: String,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || {
        let text = read_token_file(&path, home_directory())?;
        inner.store_token(&id, &text, "file")
    })
    .await
}

#[tauri::command]
pub async fn connection_token_clear(
    state: State<'_, AppState>,
    id: String,
) -> AppResult<ConnectionsView> {
    let inner = state.shared();
    blocking(move || inner.clear_token(&id)).await
}

fn home_directory() -> Option<PathBuf> {
    let variable = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(variable).map(PathBuf::from)
}

/// The text of a token file. A leading `~` is the home directory. Errors name the problem and
/// never the content.
fn read_token_file(text: &str, home: Option<PathBuf>) -> AppResult<String> {
    let text = text.trim();
    let path = match (
        text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")),
        home,
    ) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => PathBuf::from(text),
    };
    if !path.is_absolute() {
        return Err(AppError::invalid("Enter the full path of the token file."));
    }
    let unreadable = || AppError::invalid("The token file cannot be read. Check the path.");
    let metadata = std::fs::metadata(&path).map_err(|_| unreadable())?;
    if !metadata.is_file() || metadata.len() > MAX_TOKEN_FILE {
        return Err(AppError::invalid(format!(
            "That is not a token file. {TOKEN_FORMAT}"
        )));
    }
    let bytes = std::fs::read(&path).map_err(|_| unreadable())?;
    decode_text(&bytes)
        .ok_or_else(|| AppError::invalid(format!("That is not a token file. {TOKEN_FORMAT}")))
}

/// The text of a file: UTF-8, with or without a byte order mark, or UTF-16 with one, which is
/// what Windows PowerShell 5.1 writes when it redirects a command's output.
fn decode_text(bytes: &[u8]) -> Option<String> {
    let utf16 = |rest: &[u8], unit: fn([u8; 2]) -> u16| {
        let (pairs, odd) = rest.as_chunks::<2>();
        let units: Vec<u16> = pairs.iter().map(|pair| unit(*pair)).collect();
        if odd.is_empty() {
            String::from_utf16(&units).ok()
        } else {
            None
        }
    };
    match bytes {
        [0xFF, 0xFE, rest @ ..] => utf16(rest, u16::from_le_bytes),
        [0xFE, 0xFF, rest @ ..] => utf16(rest, u16::from_be_bytes),
        [0xEF, 0xBB, 0xBF, rest @ ..] => String::from_utf8(rest.to_vec()).ok(),
        _ => String::from_utf8(bytes.to_vec()).ok(),
    }
}

/// `GET /v1/status`: sources, streams and freshness, for the workspaces the token may read.
#[tauri::command]
pub async fn api_status(state: State<'_, AppState>, id: String) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(&target, Some(&token), Method::GET, &["v1", "status"], None)
        .await
}

/// `GET /v1/workspaces`: the workspaces the token may read.
#[tauri::command]
pub async fn api_workspaces(state: State<'_, AppState>, id: String) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::GET,
            &["v1", "workspaces"],
            None,
        )
        .await
}

/// `GET /v1/workspaces/{workspace}/sources`: the sources of a workspace, for the Data screen.
#[tauri::command]
pub async fn api_sources(
    state: State<'_, AppState>,
    id: String,
    workspace: String,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments = ["v1", "workspaces", workspace.as_str(), "sources"];
    inner
        .http
        .call(&target, Some(&token), Method::GET, &segments, None)
        .await
}

/// `GET /v1/workspaces/{workspace}/catalog`: the project map of a workspace.
#[tauri::command]
pub async fn api_catalog(
    state: State<'_, AppState>,
    id: String,
    workspace: String,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments = ["v1", "workspaces", workspace.as_str(), "catalog"];
    inner
        .http
        .call(&target, Some(&token), Method::GET, &segments, None)
        .await
}

/// `GET /v1/workspaces/{workspace}/records/sensitive`: the records of a workspace marked
/// sensitive, with their reasons and no content. Needs an admin token.
#[tauri::command]
pub async fn api_sensitive_list(
    state: State<'_, AppState>,
    id: String,
    workspace: String,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments = [
        "v1",
        "workspaces",
        workspace.as_str(),
        "records",
        "sensitive",
    ];
    inner
        .http
        .call(&target, Some(&token), Method::GET, &segments, None)
        .await
}

/// What `api_sensitive_mark` changes: one record, named by its id, marked or cleared.
#[derive(Debug, Clone, Deserialize)]
pub struct SensitiveMark {
    pub workspace: String,
    pub record_id: String,
    pub sensitive: bool,
    #[serde(default)]
    pub reason: Option<String>,
}

/// The body of a mark: the record by its id, checked here first, and the reason of a mark.
fn sensitive_body(mark: &SensitiveMark) -> AppResult<Value> {
    let record = mark.record_id.trim();
    let hex = record
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if record.len() != 64 || !hex {
        return Err(AppError::invalid(
            "A record id is 64 lowercase hexadecimal characters, as search results give it.",
        ));
    }
    let mut body = json!({"record_id": record, "sensitive": mark.sensitive});
    let reason = mark
        .reason
        .as_deref()
        .map(str::trim)
        .filter(|reason| !reason.is_empty());
    if let (true, Some(reason)) = (mark.sensitive, reason) {
        body["reason"] = Value::String(reason.to_owned());
    }
    Ok(body)
}

/// `PUT /v1/workspaces/{workspace}/records/sensitive`: mark one record sensitive, or clear its
/// mark. The record is named by its id only; the server checks the rest.
#[tauri::command]
pub async fn api_sensitive_mark(
    state: State<'_, AppState>,
    id: String,
    mark: SensitiveMark,
) -> AppResult<Value> {
    let body = sensitive_body(&mark)?;
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments = [
        "v1",
        "workspaces",
        mark.workspace.as_str(),
        "records",
        "sensitive",
    ];
    inner
        .http
        .call(&target, Some(&token), Method::PUT, &segments, Some(&body))
        .await
}

/// Which project `api_project` reads.
#[derive(Debug, Clone, Deserialize)]
pub struct ProjectPlace {
    pub workspace: String,
    pub project: String,
}

/// `GET /v1/workspaces/{workspace}/projects/{project}`: a project and its links in both
/// directions, what its services use and which services of other projects use them.
#[tauri::command]
pub async fn api_project(
    state: State<'_, AppState>,
    id: String,
    place: ProjectPlace,
) -> AppResult<Value> {
    let segments = [
        "v1",
        "workspaces",
        place.workspace.as_str(),
        "projects",
        place.project.as_str(),
    ];
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(&target, Some(&token), Method::GET, &segments, None)
        .await
}

/// Which domain `api_domain` reads.
#[derive(Debug, Clone, Deserialize)]
pub struct DomainPlace {
    pub workspace: String,
    pub project: String,
    /// The domain, or a subdomain as `domain/subdomain`.
    pub domain: String,
}

/// The parts of a domain's address: one name, or two for a subdomain. Each becomes one segment
/// of the route, so an address cannot name another route.
fn domain_segments(address: &str) -> AppResult<Vec<&str>> {
    let parts: Vec<&str> = address.split('/').collect();
    let named = parts
        .iter()
        .all(|part| !part.is_empty() && *part != "." && *part != "..");
    if !named || parts.len() > 2 {
        return Err(AppError::invalid("That is not the address of a domain."));
    }
    Ok(parts)
}

/// `GET /v1/workspaces/{workspace}/projects/{project}/domains/{domain}`: a domain with its code
/// and how many files of the index each path mask matches now.
#[tauri::command]
pub async fn api_domain(
    state: State<'_, AppState>,
    id: String,
    place: DomainPlace,
) -> AppResult<Value> {
    let mut segments = vec![
        "v1",
        "workspaces",
        place.workspace.as_str(),
        "projects",
        place.project.as_str(),
        "domains",
    ];
    segments.extend(domain_segments(&place.domain)?);
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(&target, Some(&token), Method::GET, &segments, None)
        .await
}

/// What `api_project_put` writes: a project of one workspace, as one document.
#[derive(Debug, Clone, Deserialize)]
pub struct ProjectWrite {
    pub workspace: String,
    pub project: String,
    /// The project as the user edited it, with `if_version`: the version it was read at.
    pub document: Value,
}

/// Whether a project document says which version it was made from.
fn has_version(document: &Value) -> bool {
    document.get("if_version").is_some_and(Value::is_u64)
}

/// `PUT /v1/workspaces/{workspace}/projects/{project}`: writes a whole project of the catalog.
/// It is the one write of the project map, to this one route. The document must carry
/// `if_version`, so a change someone else made meanwhile is refused by the server (409
/// `version_conflict`) and never overwritten.
#[tauri::command]
pub async fn api_project_put(
    state: State<'_, AppState>,
    id: String,
    write: ProjectWrite,
) -> AppResult<Value> {
    if !has_version(&write.document) {
        return Err(AppError::invalid(
            "The project cannot be saved without the version it was read at.",
        ));
    }
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments = [
        "v1",
        "workspaces",
        write.workspace.as_str(),
        "projects",
        write.project.as_str(),
    ];
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::PUT,
            &segments,
            Some(&write.document),
        )
        .await
}

/// The read tools the server has, from its OpenAPI document. The document is public, so no token
/// is sent for it.
#[tauri::command]
pub async fn api_tools(state: State<'_, AppState>, id: String) -> AppResult<Vec<ToolDescription>> {
    let inner = state.shared();
    let target = inner.target(&id)?;
    let document = inner
        .http
        .call(&target, None, Method::GET, &["openapi.json"], None)
        .await?;
    Ok(tools::describe_tools(&document))
}

/// What a read tool answered, with the request that produced it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolAnswer {
    /// The method and the path with its query, as it was sent. It holds no token.
    pub request: String,
    pub status: u16,
    /// The JSON of a successful answer.
    pub body: Option<Value>,
    /// The server's error, when it refused the call.
    pub error: Option<AppError>,
}

/// Runs one read tool: a `GET` to the tool's route (see [`crate::tools`]). An answer of the
/// server, an error included, comes back with the request; only a call that got no answer fails.
#[tauri::command]
pub async fn api_tool_call(
    state: State<'_, AppState>,
    id: String,
    call: ToolCall,
) -> AppResult<ToolAnswer> {
    let route = tools::route_of(&call)?;
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let url = target.route(&route.segments, &route.query)?;
    let request = clean(&match url.query() {
        Some(query) => format!("GET {}?{query}", url.path()),
        None => format!("GET {}", url.path()),
    });
    let raw = inner
        .http
        .send(&target, Some(&token), Method::GET, url, None)
        .await?;
    let (body, error) = match interpret(&raw, &target.place()) {
        Ok(body) => (body, None),
        Err(error) => (None, Some(error)),
    };
    Ok(ToolAnswer {
        request,
        status: raw.status,
        body,
        error,
    })
}

/// `GET /v1/tokens`: the tokens, without secrets.
#[tauri::command]
pub async fn api_tokens_list(state: State<'_, AppState>, id: String) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(&target, Some(&token), Method::GET, &["v1", "tokens"], None)
        .await
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Read,
    Ingest,
    Admin,
    /// With read: records marked sensitive, when a read asks for them.
    Sensitive,
}

/// What the user enters to create a token. The server checks it; this is its shape only.
#[derive(Debug, Clone, Deserialize)]
pub struct NewToken {
    pub name: String,
    pub scopes: Vec<Scope>,
    #[serde(default)]
    pub workspace: Option<String>,
    #[serde(default)]
    pub expires_in_seconds: Option<u64>,
}

/// `POST /v1/tokens`. The answer holds the new token: the API shows it this once, and so does
/// the app. This is the only command whose answer carries a token to the webview.
#[tauri::command]
pub async fn api_token_create(
    state: State<'_, AppState>,
    id: String,
    request: NewToken,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let body = json!({
        "name": request.name,
        "scopes": request.scopes,
        "workspace": request.workspace,
        "expires_in_seconds": request.expires_in_seconds,
    });
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::POST,
            &["v1", "tokens"],
            Some(&body),
        )
        .await
}

/// `DELETE /v1/tokens/{token_id}`: revokes a token and answers with it.
#[tauri::command]
pub async fn api_token_revoke(
    state: State<'_, AppState>,
    id: String,
    token_id: String,
) -> AppResult<Value> {
    if !is_uuid(&token_id) {
        return Err(AppError::invalid("That is not the id of a token."));
    }
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::DELETE,
            &["v1", "tokens", &token_id],
            None,
        )
        .await
}

/// Whether `text` has the form of a UUID, as the ids of tokens have.
fn is_uuid(text: &str) -> bool {
    text.len() == 36
        && text.bytes().enumerate().all(|(at, byte)| match at {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum McpState {
    /// The endpoint answers and asks for a token.
    On,
    /// The server has no MCP endpoint at its address.
    Off,
    /// The answer does not say.
    Unknown,
}

/// What an MCP endpoint answers to a request without a token. The endpoint asks every request
/// for a token before it reads it, so 401 means it is there; 404 means it is not.
fn mcp_state(status: u16) -> McpState {
    match status {
        401 => McpState::On,
        404 => McpState::Off,
        _ => McpState::Unknown,
    }
}

/// Asks a connection's server whether its MCP endpoint is there, without a token.
#[tauri::command]
pub async fn mcp_probe(state: State<'_, AppState>, id: String) -> AppResult<McpState> {
    let inner = state.shared();
    let target = inner.target(&id)?;
    let raw = inner
        .http
        .exchange(&target, None, Method::GET, &["mcp"], None)
        .await?;
    let state = mcp_state(raw.status);
    if state == McpState::Unknown && (500..600).contains(&raw.status) {
        // A server that is down says nothing about its endpoint; report the failure instead.
        return Err(failure(&raw, &target.place()));
    }
    Ok(state)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LauncherInfo {
    /// The path the user set; `None` when the program is looked for on `PATH`.
    pub configured_path: Option<String>,
    /// The program the app would run; `None` when it is not installed.
    pub resolved_path: Option<String>,
    /// Where that program comes from: the path set here, the app itself, or this computer.
    pub source: Option<launcher::Source>,
}

#[tauri::command]
pub async fn launcher_info(state: State<'_, AppState>) -> AppResult<LauncherInfo> {
    let inner = state.shared();
    blocking(move || inner.launcher_info()).await
}

#[tauri::command]
pub async fn launcher_set_path(
    state: State<'_, AppState>,
    path: Option<String>,
) -> AppResult<LauncherInfo> {
    let inner = state.shared();
    blocking(move || inner.set_launcher_path(path.as_deref())).await
}

/// `memini-server status --json`.
#[tauri::command]
pub async fn launcher_status(state: State<'_, AppState>) -> AppResult<LauncherStatus> {
    let inner = state.shared();
    let (program, search) = blocking(move || inner.launcher()).await?;
    let output = launcher::run(&program, Action::Status, &search).await?;
    launcher::parse_status(&output)
}

/// The launcher actions that change the server. The webview names one of these; the argument
/// vector comes from [`Action`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Change {
    Start,
    Stop,
    /// Stop, then start: the launcher has no command of its own for it.
    Restart,
    McpOn,
    McpOff,
}

impl Change {
    /// The launcher actions a change stands for, in the order they run.
    const fn actions(self) -> &'static [Action] {
        match self {
            Self::Start => &[Action::Start],
            Self::Stop => &[Action::Stop],
            Self::Restart => &[Action::Stop, Action::Start],
            Self::McpOn => &[Action::McpOn],
            Self::McpOff => &[Action::McpOff],
        }
    }
}

/// Marks an action as running until it is dropped.
struct BusyGuard<'a>(&'a AtomicBool);

impl<'a> BusyGuard<'a> {
    fn begin(flag: &'a AtomicBool) -> AppResult<Self> {
        flag.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map(|_| Self(flag))
            .map_err(|_| {
                AppError::new(
                    ErrorKind::Busy,
                    "Another server action is still running. Wait for it to finish.",
                )
            })
    }
}

impl Drop for BusyGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// Two addresses of this computer that name one server: the same scheme and port, and loopback
/// hosts both (`localhost` and `127.0.0.1` are one computer).
fn same_server(first: &str, second: &str) -> bool {
    match (url::Url::parse(first), url::Url::parse(second)) {
        (Ok(first), Ok(second)) => {
            first.scheme() == second.scheme()
                && first.port_or_known_default() == second.port_or_known_default()
                && (first.host() == second.host() || (is_loopback(&first) && is_loopback(&second)))
        }
        _ => false,
    }
}

/// The start the launcher is asked for. A server that is not installed yet starts on the first
/// free port from 6364, so another server on this computer does not stop the first setup; an
/// installed one starts on the port it has. On macOS a new server runs the search model in
/// Ollama on the Mac when it answers: Docker there has no GPU (memini-qpk.28). Rust asks Ollama
/// itself; the window's word is not taken for it.
async fn start_action(
    program: &std::path::Path,
    search: &SearchPath,
    progress: &host_model::Progress,
) -> AppResult<Action> {
    let status = launcher::parse_status(&launcher::run(program, Action::Status, search).await?)?;
    if status.installed {
        return Ok(Action::Start);
    }
    let host_model = host_model::probe(progress).await?.found;
    let port = blocking(|| Ok(launcher::first_free_port(launcher::port_is_free))).await?;
    port.map(|port| Action::FirstStart { port, host_model })
        .ok_or_else(|| {
            AppError::new(
                ErrorKind::LauncherFailed,
                format!(
                    "Ports {} to {} of this computer are all taken, so the server has no port to \
                 start on. Stop what listens on one of them and set up again.",
                    launcher::DEFAULT_PORT,
                    launcher::DEFAULT_PORT + launcher::PORTS_TRIED - 1
                ),
            )
        })
}

/// `memini-server start|stop|mcp on|mcp off --json`, one at a time. A change of several actions
/// stops at the first that fails and answers with it.
#[tauri::command]
pub async fn launcher_change(
    state: State<'_, AppState>,
    change: Change,
) -> AppResult<LauncherResult> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.launcher_busy)?;
    let shared = Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    let mut result = LauncherResult {
        ok: true,
        error: None,
    };
    for &action in change.actions() {
        let action = if action == Action::Start {
            start_action(&program, &search, &inner.host_progress).await?
        } else {
            action
        };
        let output = launcher::run(&program, action, &search).await?;
        result = launcher::parse_result(&output)?;
        if !result.ok {
            break;
        }
    }
    Ok(result)
}

/// What the last step of the first setup did. It never holds the token.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SetupOutcome {
    pub ok: bool,
    /// The service's reason when it refused, such as an admin token that exists already.
    pub error: Option<String>,
    pub view: ConnectionsView,
}

/// The last step of the first setup: `memini-server bootstrap --json` creates the first admin
/// token of the server on this computer, and Rust reads it from the launcher's pipe, puts it in
/// the keychain and makes the local connection. The token is in no answer of this command, no
/// log and no command line. `force` asks for another admin token although one exists.
#[tauri::command]
pub async fn launcher_setup_token(
    state: State<'_, AppState>,
    force: bool,
) -> AppResult<SetupOutcome> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.launcher_busy)?;
    let shared = Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    let action = if force {
        Action::BootstrapForce
    } else {
        Action::Bootstrap
    };
    let answer = launcher::parse_bootstrap(&launcher::run(&program, action, &search).await?)?;
    let Some(token) = answer.token else {
        return Ok(SetupOutcome {
            ok: false,
            error: answer.error,
            view: inner.view()?,
        });
    };
    let status = launcher::parse_status(&launcher::run(&program, Action::Status, &search).await?)?;
    let shared = Arc::clone(&inner);
    let view = blocking(move || shared.adopt_local(&token, status.api_url.as_deref())).await?;
    Ok(SetupOutcome {
        ok: true,
        error: None,
        view,
    })
}

/// `memini-server indexing status --json`: how many cores the search model may use.
#[tauri::command]
pub async fn launcher_indexing(state: State<'_, AppState>) -> AppResult<launcher::IndexingAnswer> {
    let inner = state.shared();
    let (program, search) = blocking(move || inner.launcher()).await?;
    launcher::parse_indexing(&launcher::run(&program, Action::IndexingStatus, &search).await?)
}

/// `memini-server indexing set <mode> --json`. The webview names a mode of a fixed set; the
/// argument vector comes from [`Action`]. A running model is recreated, so it waits for any other
/// launcher action.
#[tauri::command]
pub async fn launcher_indexing_set(
    state: State<'_, AppState>,
    mode: launcher::IndexingMode,
) -> AppResult<launcher::IndexingAnswer> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.launcher_busy)?;
    let shared = Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    launcher::parse_indexing(&launcher::run(&program, Action::IndexingSet(mode), &search).await?)
}

/// `memini-server embedding host|container --json`: moves the search model to Ollama on this
/// computer or back into its container, and recreates a running service. The webview names one of
/// the two; the argument vector comes from [`Action`].
#[tauri::command]
pub async fn launcher_embedding_set(
    state: State<'_, AppState>,
    runtime: launcher::Runtime,
) -> AppResult<launcher::IndexingAnswer> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.launcher_busy)?;
    let shared = Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    launcher::parse_indexing(
        &launcher::run(&program, Action::EmbeddingSet(runtime), &search).await?,
    )
}

/// Ollama on this computer: whether it answers and has the search model, whether Homebrew can
/// install it, and how far a download or an installation is. The window polls it while one runs.
#[tauri::command]
pub async fn host_model_status(state: State<'_, AppState>) -> AppResult<host_model::HostModel> {
    let inner = state.shared();
    host_model::probe(&inner.host_progress).await
}

/// Has Ollama on this computer download the search model. Answers at the end; the progress is in
/// [`host_model_status`] meanwhile.
#[tauri::command]
pub async fn host_model_pull(state: State<'_, AppState>) -> AppResult<host_model::PullProgress> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.host_busy)?;
    host_model::pull(&inner.host_progress).await
}

/// Installs Ollama with Homebrew (`brew install ollama`, `brew services start ollama`) after the
/// user said so in the window. Answers with the end of what Homebrew printed.
#[tauri::command]
pub async fn host_ollama_install(
    state: State<'_, AppState>,
) -> AppResult<host_model::InstallOutcome> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.host_busy)?;
    host_model::install(&inner.host_progress).await
}

/// What the removal of the server did. The local connection to it is gone when it worked.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UninstallOutcome {
    pub ok: bool,
    pub error: Option<String>,
    pub view: ConnectionsView,
}

/// `memini-server uninstall --yes --json`: removes the server on this computer with its data,
/// then forgets the local connection to it and its token. The webview asks the user first.
#[tauri::command]
pub async fn launcher_uninstall(state: State<'_, AppState>) -> AppResult<UninstallOutcome> {
    let inner = state.shared();
    let _busy = BusyGuard::begin(&inner.launcher_busy)?;
    let shared = Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    // The address is read before: after the removal the launcher knows no server.
    let status = launcher::parse_status(&launcher::run(&program, Action::Status, &search).await?)?;
    let result =
        launcher::parse_result(&launcher::run(&program, Action::Uninstall, &search).await?)?;
    let shared = Arc::clone(&inner);
    let view = if result.ok && status.installed {
        blocking(move || shared.forget_local(status.api_url.as_deref())).await?
    } else {
        blocking(move || shared.view()).await?
    };
    Ok(UninstallOutcome {
        ok: result.ok,
        error: result.error,
        view,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_mark_names_the_record_by_its_id_and_keeps_a_reason_only_when_marking() {
        let mark = |record: &str, sensitive: bool| SensitiveMark {
            workspace: "harbour".to_owned(),
            record_id: record.to_owned(),
            sensitive,
            reason: Some("  salary  ".to_owned()),
        };
        let id = "a".repeat(64);
        assert_eq!(
            sensitive_body(&mark(&id, true)).expect("a record id"),
            json!({"record_id": id, "sensitive": true, "reason": "salary"})
        );
        assert_eq!(
            sensitive_body(&mark(&id, false)).expect("a record id"),
            json!({"record_id": id, "sensitive": false})
        );
        for bad in ["", "A".repeat(64).as_str(), "a".repeat(63).as_str(), "../x"] {
            assert!(sensitive_body(&mark(bad, true)).is_err(), "{bad}");
        }
    }

    use super::*;
    use crate::keychain::MemoryStore;
    use crate::secret::fake_token;
    use crate::store::ConnectionKind;

    struct Fixture {
        inner: Arc<Inner>,
        directory: PathBuf,
    }

    impl Fixture {
        fn new(name: &str) -> Self {
            let directory = std::env::temp_dir().join(format!(
                "memini-desktop-commands-{}-{name}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&directory);
            let state = AppState::new(
                Store::open(directory.clone()),
                Box::new(MemoryStore::default()),
            )
            .expect("state");
            Self {
                inner: state.shared(),
                directory,
            }
        }

        fn add(&self, name: &str, url: &str) -> String {
            let view = self
                .inner
                .save_connection(&ConnectionInput {
                    id: None,
                    name: name.to_owned(),
                    kind: ConnectionKind::Remote,
                    url: url.to_owned(),
                    allow_http: false,
                })
                .expect("saved");
            view.connections.last().expect("one").id.clone()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }

    #[test]
    fn a_token_goes_to_the_keychain_and_the_view_only_says_it_is_there() {
        let fixture = Fixture::new("store-token");
        let id = fixture.add("Harbour", "https://a.example.org");
        let token = fake_token('t');
        let view = fixture
            .inner
            .store_token(&id, &format!("{token}\n"), "clipboard")
            .expect("stored");
        assert!(view.connections[0].has_token);
        assert!(!serde_json::to_string(&view)
            .expect("serializable")
            .contains(&token));
        assert_eq!(
            fixture
                .inner
                .token(&id)
                .expect("readable")
                .expect("stored")
                .expose(),
            token
        );
        // The settings file holds no token either.
        let file = std::fs::read_to_string(fixture.directory.join("settings.json")).expect("file");
        assert!(!file.contains(&token));
        assert!(file.contains("\"has_token\": true"));
    }

    #[test]
    fn text_that_is_not_a_token_is_refused_without_being_echoed() {
        let fixture = Fixture::new("not-a-token");
        let id = fixture.add("Harbour", "https://a.example.org");
        let error = fixture
            .inner
            .store_token(&id, "a private note that was on the clipboard", "clipboard")
            .expect_err("refused");
        assert_eq!(error.kind, ErrorKind::Invalid);
        assert!(!error.message.contains("private note"));
        assert!(fixture.inner.token(&id).expect("readable").is_none());
    }

    #[test]
    fn changing_the_address_removes_the_token_from_the_keychain() {
        let fixture = Fixture::new("moved");
        let id = fixture.add("Harbour", "https://a.example.org");
        fixture
            .inner
            .store_token(&id, &fake_token('u'), "file")
            .expect("stored");
        let view = fixture
            .inner
            .save_connection(&ConnectionInput {
                id: Some(id.clone()),
                name: "Harbour".to_owned(),
                kind: ConnectionKind::Remote,
                url: "https://b.example.org".to_owned(),
                allow_http: false,
            })
            .expect("saved");
        assert!(!view.connections[0].has_token);
        assert!(fixture.inner.secrets.get(&id).expect("readable").is_none());
    }

    #[test]
    fn deleting_a_connection_removes_its_token() {
        let fixture = Fixture::new("deleted");
        let id = fixture.add("Harbour", "https://a.example.org");
        fixture
            .inner
            .store_token(&id, &fake_token('v'), "file")
            .expect("stored");
        let view = fixture.inner.delete_connection(&id).expect("deleted");
        assert!(view.connections.is_empty());
        assert!(fixture.inner.secrets.get(&id).expect("readable").is_none());
        assert_eq!(
            fixture
                .inner
                .store_token(&id, &fake_token('v'), "file")
                .expect_err("gone")
                .kind,
            ErrorKind::NotFound
        );
    }

    #[test]
    fn a_token_removed_outside_the_app_is_noticed() {
        let fixture = Fixture::new("removed-outside");
        let id = fixture.add("Harbour", "https://a.example.org");
        fixture
            .inner
            .store_token(&id, &fake_token('w'), "file")
            .expect("stored");
        fixture.inner.secrets.delete(&id).expect("removed");
        assert!(fixture.inner.token(&id).expect("readable").is_none());
        assert!(!fixture.inner.view().expect("view").connections[0].has_token);
    }

    #[test]
    fn a_token_file_is_read_in_the_encodings_shells_write() {
        let token = fake_token('x');
        assert_eq!(
            decode_text(format!("{token}\n").as_bytes()).as_deref(),
            Some(&*format!("{token}\n"))
        );
        let mut with_mark = vec![0xEF, 0xBB, 0xBF];
        with_mark.extend_from_slice(token.as_bytes());
        assert_eq!(decode_text(&with_mark).as_deref(), Some(token.as_str()));
        let mut utf16 = vec![0xFF, 0xFE];
        utf16.extend(token.encode_utf16().flat_map(u16::to_le_bytes));
        assert_eq!(decode_text(&utf16).as_deref(), Some(token.as_str()));
        assert_eq!(decode_text(&[0xFF, 0xFE, 0x41]), None);
        assert_eq!(decode_text(&[0xC3, 0x28]), None);
    }

    #[test]
    fn a_token_file_is_a_small_file_at_a_full_path() {
        let fixture = Fixture::new("token-file");
        std::fs::create_dir_all(&fixture.directory).expect("directory");
        let token = fake_token('y');
        let file = fixture.directory.join("read.token");
        std::fs::write(&file, format!("{token}\n")).expect("written");
        let text = read_token_file(file.to_str().expect("unicode"), None).expect("read");
        assert_eq!(Token::parse(&text).expect("a token").expose(), token);
        // A leading ~ is the home directory.
        let text = read_token_file("~/read.token", Some(fixture.directory.clone())).expect("read");
        assert_eq!(text.trim(), token);

        assert!(read_token_file("read.token", None).is_err());
        assert!(read_token_file(fixture.directory.to_str().expect("unicode"), None).is_err());
        let large = fixture.directory.join("large");
        std::fs::write(&large, "x".repeat(MAX_TOKEN_FILE as usize + 1)).expect("written");
        let error = read_token_file(large.to_str().expect("unicode"), None).expect_err("too large");
        assert!(!error.message.contains("xxx"));
    }

    #[test]
    fn the_launcher_path_must_name_the_program_and_exist() {
        let fixture = Fixture::new("launcher-path");
        std::fs::create_dir_all(&fixture.directory).expect("directory");
        let program = fixture.directory.join(launcher::PROGRAM_FILE_NAMES[0]);
        let text = program.to_str().expect("unicode").to_owned();
        assert_eq!(
            fixture
                .inner
                .set_launcher_path(Some(&text))
                .expect_err("no file yet")
                .kind,
            ErrorKind::Invalid
        );
        std::fs::write(&program, "").expect("written");
        let info = fixture
            .inner
            .set_launcher_path(Some(&text))
            .expect("accepted");
        assert_eq!(info.configured_path.as_deref(), Some(text.as_str()));
        assert_eq!(info.resolved_path.as_deref(), Some(text.as_str()));
        let other = fixture.directory.join("sh");
        std::fs::write(&other, "").expect("written");
        assert!(fixture.inner.set_launcher_path(other.to_str()).is_err());
        let cleared = fixture
            .inner
            .set_launcher_path(Some("  "))
            .expect("cleared");
        assert_eq!(cleared.configured_path, None);
    }

    #[test]
    fn two_addresses_name_one_server_by_scheme_port_and_loopback_host() {
        assert!(same_server(
            "http://127.0.0.1:6364",
            "http://127.0.0.1:6364/"
        ));
        assert!(same_server(
            "http://localhost:6364",
            "http://127.0.0.1:6364"
        ));
        assert!(!same_server(
            "http://127.0.0.1:6365",
            "http://127.0.0.1:6364"
        ));
        assert!(!same_server(
            "https://127.0.0.1:6364",
            "http://127.0.0.1:6364"
        ));
        assert!(!same_server("http://127.0.0.1:6364", "not an address"));
    }

    #[test]
    fn removing_the_server_forgets_its_local_connection_and_token_only() {
        let fixture = Fixture::new("forget-local");
        let remote = fixture.add("Harbour", "https://a.example.org");
        let token = Token::parse(&fake_token('u')).expect("a token");
        let local = fixture
            .inner
            .adopt_local(&token, Some("http://127.0.0.1:6370"))
            .expect("adopted")
            .active_id
            .expect("the local connection");

        // Another server was removed: the connection to this one stays.
        let kept = fixture
            .inner
            .forget_local(Some("http://127.0.0.1:6364"))
            .expect("a view");
        assert_eq!(kept.connections.len(), 2);
        let kept = fixture.inner.forget_local(None).expect("a view");
        assert_eq!(kept.connections.len(), 2);

        let view = fixture
            .inner
            .forget_local(Some("http://localhost:6370"))
            .expect("a view");
        assert_eq!(view.connections.len(), 1);
        assert_eq!(view.connections[0].id, remote);
        assert_eq!(fixture.inner.secrets.get(&local).expect("readable"), None);
    }

    #[test]
    fn the_first_setup_makes_the_local_connection_and_keeps_the_token_in_the_keychain() {
        let fixture = Fixture::new("adopt-local");
        let remote = fixture.add("Harbour", "https://a.example.org");
        let token = Token::parse(&fake_token('s')).expect("a token");

        let view = fixture
            .inner
            .adopt_local(&token, Some("http://127.0.0.1:7001"))
            .expect("adopted");

        let local = view.connections.last().expect("the local connection");
        assert_eq!(local.kind, ConnectionKind::Local);
        assert_eq!(local.url, "http://127.0.0.1:7001");
        assert!(local.has_token);
        assert_eq!(view.active_id.as_deref(), Some(local.id.as_str()));
        assert_ne!(local.id, remote);
        assert!(!serde_json::to_string(&view)
            .expect("serializable")
            .contains(token.expose()));
        assert_eq!(
            fixture.inner.secrets.get(&local.id).expect("readable"),
            Some(token)
        );
    }

    #[test]
    fn the_first_setup_keeps_an_existing_local_connection_and_its_address() {
        let fixture = Fixture::new("adopt-existing");
        let saved = fixture
            .inner
            .save_connection(&ConnectionInput {
                id: None,
                name: "My server".to_owned(),
                kind: ConnectionKind::Local,
                url: "http://localhost:6364".to_owned(),
                allow_http: false,
            })
            .expect("saved");
        let id = saved.connections[0].id.clone();
        let token = Token::parse(&fake_token('e')).expect("a token");

        let view = fixture
            .inner
            .adopt_local(&token, Some("http://127.0.0.1:7001"))
            .expect("adopted");

        assert_eq!(view.connections.len(), 1);
        assert_eq!(view.connections[0].name, "My server");
        assert_eq!(view.connections[0].url, "http://localhost:6364");
        assert!(view.connections[0].has_token);
        assert_eq!(
            fixture.inner.secrets.get(&id).expect("readable"),
            Some(token)
        );
    }

    #[test]
    fn the_first_setup_never_takes_an_address_that_is_not_on_this_computer() {
        let fixture = Fixture::new("adopt-elsewhere");
        let token = Token::parse(&fake_token('n')).expect("a token");

        let view = fixture
            .inner
            .adopt_local(&token, Some("https://elsewhere.example.org"))
            .expect("adopted");

        assert_eq!(view.connections[0].url, DEFAULT_LOCAL_URL);
    }

    #[test]
    fn a_restart_is_a_stop_and_then_a_start() {
        let change: Change = serde_json::from_str("\"restart\"").expect("known");
        assert_eq!(change.actions(), [Action::Stop, Action::Start]);
        assert_eq!(Change::McpOn.actions(), [Action::McpOn]);
    }

    #[test]
    fn one_launcher_action_runs_at_a_time() {
        let flag = AtomicBool::new(false);
        let first = BusyGuard::begin(&flag).expect("free");
        assert_eq!(
            BusyGuard::begin(&flag).err().map(|error| error.kind),
            Some(ErrorKind::Busy)
        );
        drop(first);
        assert!(BusyGuard::begin(&flag).is_ok());
    }

    #[test]
    fn a_project_is_written_only_with_the_version_it_was_read_at() {
        let versions = [
            has_version(&json!({"title": "Port", "if_version": 3})),
            has_version(&json!({"title": "Port", "if_version": 0})),
            has_version(&json!({"title": "Port"})),
            has_version(&json!({"title": "Port", "if_version": null})),
            has_version(&json!({"title": "Port", "if_version": "3"})),
        ];

        assert_eq!(versions, [true, true, false, false, false]);
    }

    #[test]
    fn the_address_of_a_domain_is_one_name_or_two() {
        let read = |address| domain_segments(address).ok();

        assert_eq!(read("fees"), Some(vec!["fees"]));
        assert_eq!(read("fees/berth"), Some(vec!["fees", "berth"]));
        assert_eq!(
            [
                read(""),
                read("fees/"),
                read("../tokens"),
                read("a/b/c"),
                read("a/..")
            ],
            [None, None, None, None, None]
        );
    }

    #[test]
    fn the_ids_of_tokens_are_uuids() {
        assert!(is_uuid("0b9f6a52-7c1e-4d7a-9a57-3f6f0c2d8e11"));
        assert!(!is_uuid("0b9f6a52-7c1e-4d7a-9a57-3f6f0c2d8e1"));
        assert!(!is_uuid("../workspaces"));
        assert!(!is_uuid("0b9f6a52x7c1e-4d7a-9a57-3f6f0c2d8e11"));
    }

    #[test]
    fn an_mcp_endpoint_that_asks_for_a_token_is_on() {
        assert_eq!(mcp_state(401), McpState::On);
        assert_eq!(mcp_state(404), McpState::Off);
        assert_eq!(mcp_state(200), McpState::Unknown);
        assert_eq!(mcp_state(503), McpState::Unknown);
    }

    #[test]
    fn the_webview_names_a_change_and_never_an_argument() {
        let change: Change = serde_json::from_str("\"mcp_off\"").expect("known");
        assert_eq!(change.actions()[0].arguments(), ["mcp", "off", "--json"]);
        assert!(serde_json::from_str::<Change>("\"bootstrap\"").is_err());
        assert!(serde_json::from_str::<Change>("\"cli\"").is_err());
        assert!(serde_json::from_str::<Change>("\"status; rm -rf /\"").is_err());
        assert!(serde_json::from_str::<Change>("[\"start\", \"--http-port\", \"1\"]").is_err());
    }
}
