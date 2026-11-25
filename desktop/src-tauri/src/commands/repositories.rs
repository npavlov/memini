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
    /// Path globs of files to leave out, such as tests and data (scenario S18).
    #[serde(default)]
    pub exclude: Vec<String>,
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
        "exclude": source.exclude,
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

/// A change of the schedule: off (only when asked) or every so many seconds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ScheduleChange {
    Off,
    Every { seconds: u32 },
}

/// What a code base indexes and when it syncs (scenario S18). A field left out stays as it is;
/// `include` and `exclude` replace the whole list.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct SourceChanges {
    #[serde(default)]
    pub include: Option<Vec<String>>,
    #[serde(default)]
    pub exclude: Option<Vec<String>>,
    #[serde(default)]
    pub builtin_excludes: Option<bool>,
    #[serde(default)]
    pub schedule: Option<ScheduleChange>,
}

impl SourceChanges {
    /// The query of a preview: each glob a pair, an empty pair for a list cleared.
    fn preview_query(&self) -> Vec<(String, String)> {
        let mut query = Vec::new();
        for (name, globs) in [("include", &self.include), ("exclude", &self.exclude)] {
            if let Some(globs) = globs {
                if globs.is_empty() {
                    query.push((name.to_owned(), String::new()));
                }
                query.extend(globs.iter().map(|glob| (name.to_owned(), glob.clone())));
            }
        }
        if let Some(builtin) = self.builtin_excludes {
            query.push(("builtin_excludes".to_owned(), builtin.to_string()));
        }
        query
    }

    /// The body of the request: only the fields that change, an explicit null for a schedule
    /// switched off.
    fn body(&self) -> Value {
        let mut body = serde_json::Map::new();
        if let Some(include) = &self.include {
            body.insert("include".to_owned(), json!(include));
        }
        if let Some(exclude) = &self.exclude {
            body.insert("exclude".to_owned(), json!(exclude));
        }
        if let Some(builtin) = self.builtin_excludes {
            body.insert("builtin_excludes".to_owned(), json!(builtin));
        }
        match self.schedule {
            Some(ScheduleChange::Off) => {
                body.insert("schedule_seconds".to_owned(), Value::Null);
            }
            Some(ScheduleChange::Every { seconds }) => {
                body.insert("schedule_seconds".to_owned(), json!(seconds));
            }
            None => {}
        }
        Value::Object(body)
    }
}

/// `PATCH /v1/workspaces/{workspace}/sources/{source}`: what a code base indexes and when it
/// syncs. Needs the admin scope. A changed scope makes the next sync list the ref again.
#[tauri::command]
pub async fn api_source_update(
    state: State<'_, AppState>,
    id: String,
    source: SourceRef,
    changes: SourceChanges,
) -> AppResult<Value> {
    source_body_call(&state, &id, &source, Method::PATCH, None, &changes.body()).await
}

/// `GET /v1/workspaces/{workspace}/sources/{source}/preview`: what a sync would index with the
/// source's rules, or with the ones given, which are not saved. The globs go in the query, one
/// pair each; an empty list is one empty pair, which the server reads as a list cleared.
#[tauri::command]
pub async fn api_source_preview(
    state: State<'_, AppState>,
    id: String,
    source: SourceRef,
    rules: SourceChanges,
) -> AppResult<Value> {
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    let segments: Vec<String> = [
        "v1",
        "workspaces",
        source.workspace.as_str(),
        "sources",
        source.source.as_str(),
        "preview",
    ]
    .iter()
    .map(|&segment| segment.to_owned())
    .collect();
    let url = target.route(&segments, &rules.preview_query())?;
    let raw = inner
        .http
        .send(&target, Some(&token), Method::GET, url, None)
        .await?;
    Ok(interpret(&raw, &target.place())?.unwrap_or(Value::Null))
}

/// `GET /v1/runs/{run}`: one run with its report, which tells how far a sync is.
#[tauri::command]
pub async fn api_run(state: State<'_, AppState>, id: String, run: String) -> AppResult<Value> {
    if run.is_empty() || run.len() > 64 || !run.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err(AppError::invalid("That is not the id of a run."));
    }
    let inner = state.shared();
    let (target, token) = authorized(&inner, &id).await?;
    inner
        .http
        .call(
            &target,
            Some(&token),
            Method::GET,
            &["v1", "runs", run.as_str()],
            None,
        )
        .await
}

async fn source_body_call(
    state: &State<'_, AppState>,
    id: &str,
    source: &SourceRef,
    method: Method,
    tail: Option<&str>,
    body: &Value,
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
    inner
        .http
        .call(&target, Some(&token), method, &segments, Some(body))
        .await
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_change_sends_only_what_changes() {
        let changes: SourceChanges =
            serde_json::from_str(r#"{"exclude": ["**/*_test.go"]}"#).expect("a change");
        assert_eq!(changes.body(), json!({"exclude": ["**/*_test.go"]}));
    }

    #[test]
    fn a_preview_puts_each_glob_in_the_query_and_a_cleared_list_as_one_empty_pair() {
        let rules: SourceChanges =
            serde_json::from_str(r#"{"exclude": ["**/*_test.go", "**/*.json"], "include": []}"#)
                .expect("rules");
        assert_eq!(
            rules.preview_query(),
            [
                ("include".to_owned(), String::new()),
                ("exclude".to_owned(), "**/*_test.go".to_owned()),
                ("exclude".to_owned(), "**/*.json".to_owned()),
            ]
        );
        assert!(SourceChanges::default().preview_query().is_empty());
    }

    #[test]
    fn a_schedule_switched_off_is_an_explicit_null() {
        let off: SourceChanges =
            serde_json::from_str(r#"{"schedule": {"kind": "off"}}"#).expect("a change");
        assert_eq!(off.body(), json!({"schedule_seconds": null}));
        let every: SourceChanges =
            serde_json::from_str(r#"{"schedule": {"kind": "every", "seconds": 3600}}"#)
                .expect("a change");
        assert_eq!(every.body(), json!({"schedule_seconds": 3600}));
        assert_eq!(SourceChanges::default().body(), json!({}));
    }
}
