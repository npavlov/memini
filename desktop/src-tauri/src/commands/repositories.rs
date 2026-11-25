//! Add repositories without a terminal (scenario S17).
//!
//! Two halves. The launcher gives the server on this computer the folders of repositories it
//! may read and the git access it reaches private remotes with (`memini-server repos` and
//! `memini-server git`); the API then makes a workspace and a git source, and syncs it. The
//! checks of what the user typed, and the rule for secrets, are in [`crate::local_git`].

use reqwest::Method;
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::{AppHandle, State};
use tauri_plugin_clipboard_manager::ClipboardExt;

use super::{authorized, blocking, decode_text, home_directory, AppState, BusyGuard};
use crate::api::interpret;
use crate::error::{AppError, AppResult, ErrorKind};
use crate::launcher;
use crate::local_git::{
    self, CheckedGit, CheckedRepo, GitAnswer, GitRequest, RepoRequest, ReposAnswer, Secret,
    SecretSource, MAX_SECRET,
};

/// `memini-server repos list|add|remove --json`. Adding and removing recreate the service, so
/// they wait for any other launcher action and keep the others waiting.
#[tauri::command]
pub async fn launcher_repos(
    state: State<'_, AppState>,
    request: RepoRequest,
) -> AppResult<ReposAnswer> {
    let checked = blocking(move || CheckedRepo::of(&request)).await?;
    let inner = state.shared();
    let _busy = match checked {
        CheckedRepo::List => None,
        _ => Some(BusyGuard::begin(&inner.launcher_busy)?),
    };
    let shared = std::sync::Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    let invocation = local_git::repos_invocation(&checked);
    local_git::parse_repos(&launcher::run_invocation(&program, &invocation, None, &search).await?)
}

/// `memini-server git status|ssh-key|https-token|remove --json`. The key or token is read here,
/// from the clipboard (which is then cleared) or from a file, and goes to the launcher on its
/// standard input: never to the webview, never into an argument.
#[tauri::command]
pub async fn launcher_git(
    app: AppHandle,
    state: State<'_, AppState>,
    request: GitRequest,
) -> AppResult<GitAnswer> {
    let (checked, source) = CheckedGit::of(&request)?;
    let secret = match source {
        None => None,
        Some(source) => {
            let ssh = matches!(checked, CheckedGit::SshKey { .. });
            Some(blocking(move || read_secret(&app, &source, ssh)).await?)
        }
    };
    let inner = state.shared();
    let _busy = match checked {
        CheckedGit::Status => None,
        _ => Some(BusyGuard::begin(&inner.launcher_busy)?),
    };
    let shared = std::sync::Arc::clone(&inner);
    let (program, search) = blocking(move || shared.launcher()).await?;
    let invocation = local_git::git_invocation(&checked);
    let input = secret.as_ref().map(Secret::expose);
    local_git::parse_git(&launcher::run_invocation(&program, &invocation, input, &search).await?)
}

/// The key (`ssh`) or the token from where the user said. Errors never hold the content.
fn read_secret(app: &AppHandle, source: &SecretSource, ssh: bool) -> AppResult<Secret> {
    let text = match source {
        SecretSource::Clipboard => {
            let text = app.clipboard().read_text().map_err(|_| {
                AppError::new(
                    ErrorKind::Clipboard,
                    "The clipboard holds no text, or cannot be read.",
                )
            })?;
            let _ = app.clipboard().clear();
            text
        }
        SecretSource::File { path } => read_secret_file(path)?,
    };
    if ssh {
        local_git::ssh_key(&text)
    } else {
        local_git::https_token(&text)
    }
}

/// The text of a key or token file. A leading `~` is the home directory.
fn read_secret_file(text: &str) -> AppResult<String> {
    let text = text.trim();
    let path = match (
        text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")),
        home_directory(),
    ) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => std::path::PathBuf::from(text),
    };
    if !path.is_absolute() {
        return Err(AppError::invalid("Enter the full path of the file."));
    }
    let unreadable = || AppError::invalid("The file cannot be read. Check the path.");
    let metadata = std::fs::metadata(&path).map_err(|_| unreadable())?;
    if !metadata.is_file() || metadata.len() > MAX_SECRET as u64 {
        return Err(AppError::invalid(
            "That is not a key or token file: it is a folder, or too large.",
        ));
    }
    let bytes = std::fs::read(&path).map_err(|_| unreadable())?;
    decode_text(&bytes).ok_or_else(|| AppError::invalid("That file does not hold text."))
}

/// `POST /v1/workspaces`: a new workspace. Needs the admin scope.
#[tauri::command]
pub async fn api_workspace_create(
    state: State<'_, AppState>,
    id: String,
    name: String,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let body = json!({ "name": name });
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::POST,
            &["v1", "workspaces"],
            Some(&body),
        )
        .await
}

/// A git source as the user describes it. The server checks the location against what it
/// allows (an https or ssh remote, or a folder under its local roots).
#[derive(Debug, Clone, Deserialize)]
pub struct NewGitSource {
    pub workspace: String,
    pub name: String,
    pub location: String,
    pub refs: Vec<String>,
    #[serde(default)]
    pub schedule_seconds: Option<u32>,
}

/// `POST /v1/workspaces/{workspace}/sources` with a git source. Needs the admin scope.
#[tauri::command]
pub async fn api_source_create(
    state: State<'_, AppState>,
    id: String,
    source: NewGitSource,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let body = json!({
        "kind": "git",
        "name": source.name,
        "location": source.location,
        "refs": source.refs,
        "schedule_seconds": source.schedule_seconds,
    });
    let segments = ["v1", "workspaces", source.workspace.as_str(), "sources"];
    inner
        .http
        .call(&target, Some(&token), Method::POST, &segments, Some(&body))
        .await
}

/// Which source of which workspace.
#[derive(Debug, Clone, Deserialize)]
pub struct SourceRef {
    pub workspace: String,
    pub source: String,
}

/// `GET /v1/workspaces/{workspace}/sources/{source}/streams`: each tracked ref with its lag and
/// newest run.
#[tauri::command]
pub async fn api_source_streams(
    state: State<'_, AppState>,
    id: String,
    source: SourceRef,
) -> AppResult<Value> {
    source_call(&state, &id, &source, Method::GET, Some("streams")).await
}

/// `POST /v1/workspaces/{workspace}/sources/{source}/sync`: sync every stream now.
#[tauri::command]
pub async fn api_source_sync(
    state: State<'_, AppState>,
    id: String,
    source: SourceRef,
) -> AppResult<Value> {
    source_call(&state, &id, &source, Method::POST, Some("sync")).await
}

/// `DELETE /v1/workspaces/{workspace}/sources/{source}`: the source with its records and mirror.
/// The webview asks the user first.
#[tauri::command]
pub async fn api_source_delete(
    state: State<'_, AppState>,
    id: String,
    source: SourceRef,
) -> AppResult<Value> {
    source_call(&state, &id, &source, Method::DELETE, None).await
}

async fn source_call(
    state: &State<'_, AppState>,
    id: &str,
    source: &SourceRef,
    method: Method,
    tail: Option<&str>,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, id).await?;
    let mut segments = vec![
        "v1",
        "workspaces",
        source.workspace.as_str(),
        "sources",
        source.source.as_str(),
    ];
    segments.extend(tail);
    let raw = inner
        .http
        .exchange(&target, Some(&token), method, &segments, None)
        .await?;
    // A deletion answers 204 without a body.
    Ok(interpret(&raw, &target.place())?.unwrap_or(Value::Null))
}
