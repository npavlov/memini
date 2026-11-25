//! Ollama on this computer, where the search model of a local server runs on macOS.
//!
//! Docker on macOS has no GPU, so the model in the server's ollama container embeds on the CPU of
//! Docker's virtual machine: about 12.5 s a chunk on four cores, where Ollama on the Mac takes
//! about 0.11 s on the GPU (measured 2026-11-25). The first setup and the Server screen therefore
//! look for Ollama on the Mac, have it download the model, and set the server up to use it.
//!
//! Everything here talks to one fixed address, [`OLLAMA`], and asks for one fixed model,
//! [`MODEL`]. Nothing the webview sends becomes part of a request or of a command line. Homebrew
//! runs with two fixed argument vectors, [`BREW_INSTALL`] and [`BREW_START`].

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;

use crate::error::{clean, AppError, AppResult, ErrorKind};

/// Where Ollama listens on this computer. Its own default, on the loopback address only.
pub const OLLAMA: &str = "http://127.0.0.1:11434";

/// The search model, as the launcher names it (`MODEL` in `scripts/memini-server.py`).
pub const MODEL: &str = "qwen3-embedding:0.6b";

/// What the app asks Homebrew, and nothing else.
pub const BREW_INSTALL: &[&str] = &["install", "ollama"];
pub const BREW_START: &[&str] = &["services", "start", "ollama"];

/// Where Homebrew installs its own command, on Apple silicon and on Intel Macs.
pub const BREW_PATHS: &[&str] = &["/opt/homebrew/bin/brew", "/usr/local/bin/brew"];

const PROBE_TIMEOUT: Duration = Duration::from_secs(5);
const PULL_TIMEOUT: Duration = Duration::from_secs(60 * 60);
const BREW_TIMEOUT: Duration = Duration::from_secs(20 * 60);
/// How long Ollama may take to answer after Homebrew started it.
const START_WAIT: Duration = Duration::from_secs(30);
/// How many lines of Homebrew's output the step shows.
const TAIL_LINES: usize = 12;

/// Whether this app runs where the model belongs on the host: macOS, where Docker has no GPU.
pub const fn supported() -> bool {
    cfg!(target_os = "macos")
}

/// How far the download of the model is, from the lines `/api/pull` streams.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct PullProgress {
    pub status: String,
    pub completed: u64,
    pub total: u64,
    pub done: bool,
    pub error: Option<String>,
}

/// What Homebrew did when it installed and started Ollama.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct InstallOutcome {
    pub ok: bool,
    pub error: Option<String>,
    /// The last lines Homebrew printed, cleaned of control characters.
    pub tail: Vec<String>,
}

/// What the app knows of Ollama on this computer.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct HostModel {
    /// macOS: the model should run here. Elsewhere the rest is not asked.
    pub supported: bool,
    /// Ollama answers at [`OLLAMA`].
    pub found: bool,
    pub version: Option<String>,
    pub model: &'static str,
    pub has_model: bool,
    /// Homebrew is there to install Ollama with.
    pub brew: bool,
    /// The download of the model, while it runs and after it ended.
    pub pull: Option<PullProgress>,
    pub installing: bool,
    pub install: Option<InstallOutcome>,
}

/// What a download or an installation in progress has reported, for the window to poll.
#[derive(Debug, Default)]
pub struct Progress {
    pull: Mutex<Option<PullProgress>>,
    installing: Mutex<bool>,
    install: Mutex<Option<InstallOutcome>>,
}

impl Progress {
    fn set_pull(&self, progress: PullProgress) {
        if let Ok(mut slot) = self.pull.lock() {
            *slot = Some(progress);
        }
    }

    fn pull(&self) -> Option<PullProgress> {
        self.pull.lock().ok().and_then(|slot| slot.clone())
    }

    fn set_installing(&self, value: bool) {
        if let Ok(mut slot) = self.installing.lock() {
            *slot = value;
        }
    }

    fn installing(&self) -> bool {
        self.installing.lock().map(|slot| *slot).unwrap_or(false)
    }

    fn set_install(&self, outcome: InstallOutcome) {
        if let Ok(mut slot) = self.install.lock() {
            *slot = Some(outcome);
        }
    }

    fn install(&self) -> Option<InstallOutcome> {
        self.install.lock().ok().and_then(|slot| slot.clone())
    }
}

/// The version in the answer of `/api/version`.
pub fn parse_version(body: &str) -> Option<String> {
    let value: Value = serde_json::from_str(body).ok()?;
    value.get("version").and_then(Value::as_str).map(clean)
}

/// Whether the answer of `/api/tags` lists `model`.
pub fn lists_model(body: &str, model: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(body) else {
        return false;
    };
    value
        .get("models")
        .and_then(Value::as_array)
        .is_some_and(|models| {
            models.iter().any(|item| {
                ["name", "model"]
                    .iter()
                    .any(|key| item.get(key).and_then(Value::as_str) == Some(model))
            })
        })
}

/// One line of the stream `/api/pull` answers with, folded into the progress so far. A line
/// without a total keeps the last counts, so the bar does not jump back between layers.
pub fn fold_pull_line(progress: &PullProgress, line: &str) -> PullProgress {
    let mut next = progress.clone();
    let Ok(value) = serde_json::from_str::<Value>(line.trim()) else {
        return next;
    };
    if let Some(error) = value.get("error").and_then(Value::as_str) {
        next.error = Some(clean(error));
        next.done = true;
        return next;
    }
    if let Some(status) = value.get("status").and_then(Value::as_str) {
        next.status = clean(status);
        if status == "success" {
            next.done = true;
            next.completed = next.total;
        }
    }
    if let Some(total) = value.get("total").and_then(Value::as_u64) {
        next.total = total;
        next.completed = value.get("completed").and_then(Value::as_u64).unwrap_or(0);
    }
    next
}

/// The last `count` lines of `output` that are not empty, cleaned of control characters.
pub fn tail(output: &str, count: usize) -> Vec<String> {
    let lines: Vec<String> = output
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(clean)
        .collect();
    lines[lines.len().saturating_sub(count)..].to_vec()
}

/// Homebrew on this computer, from its fixed install places.
pub fn brew(is_file: impl Fn(&Path) -> bool) -> Option<PathBuf> {
    BREW_PATHS
        .iter()
        .map(PathBuf::from)
        .find(|path| is_file(path))
}

fn client(timeout: Duration) -> AppResult<reqwest::Client> {
    reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(PROBE_TIMEOUT)
        .timeout(timeout)
        .build()
        .map_err(|_| {
            AppError::new(
                ErrorKind::Unexpected,
                "The HTTP client could not be set up.",
            )
        })
}

async fn get(client: &reqwest::Client, path: &str) -> Option<String> {
    let response = client.get(format!("{OLLAMA}{path}")).send().await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    response.text().await.ok()
}

/// What Ollama on this computer says now, with the progress of a download or an installation.
pub async fn probe(progress: &Progress) -> AppResult<HostModel> {
    let mut answer = HostModel {
        supported: supported(),
        model: MODEL,
        brew: supported() && brew(Path::is_file).is_some(),
        pull: progress.pull(),
        installing: progress.installing(),
        install: progress.install(),
        ..HostModel::default()
    };
    if !answer.supported {
        return Ok(answer);
    }
    let client = client(PROBE_TIMEOUT)?;
    if let Some(body) = get(&client, "/api/version").await {
        answer.found = true;
        answer.version = parse_version(&body);
        answer.has_model = get(&client, "/api/tags")
            .await
            .is_some_and(|tags| lists_model(&tags, MODEL));
    }
    Ok(answer)
}

/// Has Ollama on this computer download the model, keeping the progress for the window to poll.
/// The answer is the progress at the end.
pub async fn pull(progress: &Progress) -> AppResult<PullProgress> {
    let mut state = PullProgress {
        status: "starting".to_owned(),
        ..PullProgress::default()
    };
    progress.set_pull(state.clone());
    let finish = |state: PullProgress| {
        progress.set_pull(state.clone());
        state
    };
    let client = client(PULL_TIMEOUT)?;
    let body = serde_json::json!({ "model": MODEL, "stream": true }).to_string();
    let mut response = match client
        .post(format!("{OLLAMA}/api/pull"))
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body)
        .send()
        .await
    {
        Ok(response) if response.status().is_success() => response,
        _ => {
            state.error = Some("Ollama on this Mac did not take the download.".to_owned());
            state.done = true;
            return Ok(finish(state));
        }
    };
    let mut pending = String::new();
    loop {
        match response.chunk().await {
            Ok(Some(bytes)) => {
                pending.push_str(&String::from_utf8_lossy(&bytes));
                while let Some(end) = pending.find('\n') {
                    let line: String = pending.drain(..=end).collect();
                    state = fold_pull_line(&state, &line);
                    progress.set_pull(state.clone());
                }
            }
            Ok(None) => break,
            Err(_) => {
                state.error = Some("The download from Ollama on this Mac broke off.".to_owned());
                break;
            }
        }
    }
    if !pending.trim().is_empty() {
        state = fold_pull_line(&state, &pending);
    }
    state.done = true;
    if state.error.is_none() && state.status != "success" {
        state.error = Some("Ollama on this Mac ended the download without the model.".to_owned());
    }
    Ok(finish(state))
}

/// Installs Ollama with Homebrew and starts it as a service, then waits for it to answer. Runs
/// only the two fixed argument vectors, never through a shell.
pub async fn install(progress: &Progress) -> AppResult<InstallOutcome> {
    if !supported() {
        return Err(AppError::invalid(
            "Ollama is installed this way on macOS only.",
        ));
    }
    let Some(brew) = brew(Path::is_file) else {
        return Err(AppError::invalid(
            "Homebrew was not found: install Ollama from https://ollama.com/download.",
        ));
    };
    progress.set_installing(true);
    let outcome = install_with(&brew).await;
    progress.set_installing(false);
    progress.set_install(outcome.clone());
    Ok(outcome)
}

async fn install_with(brew: &Path) -> InstallOutcome {
    let mut said = String::new();
    for arguments in [BREW_INSTALL, BREW_START] {
        let mut command = tokio::process::Command::new(brew);
        command
            .args(arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("HOMEBREW_NO_AUTO_UPDATE", "1")
            .kill_on_drop(true);
        let words = format!("brew {}", arguments.join(" "));
        let output = match command.spawn() {
            Ok(child) => tokio::time::timeout(BREW_TIMEOUT, child.wait_with_output()).await,
            Err(error) => {
                return InstallOutcome {
                    ok: false,
                    error: Some(format!(
                        "{words} could not be started ({:?}).",
                        error.kind()
                    )),
                    tail: tail(&said, TAIL_LINES),
                };
            }
        };
        let Ok(Ok(output)) = output else {
            return InstallOutcome {
                ok: false,
                error: Some(format!("{words} did not finish and was stopped.")),
                tail: tail(&said, TAIL_LINES),
            };
        };
        said.push_str(&String::from_utf8_lossy(&output.stdout));
        said.push_str(&String::from_utf8_lossy(&output.stderr));
        if !output.status.success() {
            return InstallOutcome {
                ok: false,
                error: Some(format!("{words} failed.")),
                tail: tail(&said, TAIL_LINES),
            };
        }
    }
    let answers = wait_for_ollama().await;
    InstallOutcome {
        ok: answers,
        error: (!answers).then(|| {
            "Ollama was installed but does not answer yet. Start the Ollama app, then try again."
                .to_owned()
        }),
        tail: tail(&said, TAIL_LINES),
    }
}

async fn wait_for_ollama() -> bool {
    let Ok(client) = client(PROBE_TIMEOUT) else {
        return false;
    };
    let deadline = tokio::time::Instant::now() + START_WAIT;
    while tokio::time::Instant::now() < deadline {
        if get(&client, "/api/version").await.is_some() {
            return true;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_version_and_the_model_are_read_from_ollamas_answers() {
        assert_eq!(
            parse_version(r#"{"version":"0.34.2"}"#),
            Some("0.34.2".to_owned())
        );
        assert_eq!(parse_version("not json"), None);
        let tags = r#"{"models":[{"name":"qwen3:4b-instruct"},{"name":"qwen3-embedding:0.6b","digest":"ac6d"}]}"#;
        assert!(lists_model(tags, MODEL));
        assert!(!lists_model(r#"{"models":[{"name":"qwen3:4b"}]}"#, MODEL));
        assert!(lists_model(
            r#"{"models":[{"model":"qwen3-embedding:0.6b"}]}"#,
            MODEL
        ));
        assert!(!lists_model("[]", MODEL));
    }

    #[test]
    fn the_download_progress_follows_the_stream_and_keeps_its_counts() {
        let start = PullProgress::default();
        let manifest = fold_pull_line(&start, r#"{"status":"pulling manifest"}"#);
        assert_eq!(manifest.status, "pulling manifest");
        let layer = fold_pull_line(
            &manifest,
            r#"{"status":"pulling ac6d","digest":"sha256:ac6d","total":639000000,"completed":319500000}"#,
        );
        assert_eq!((layer.completed, layer.total), (319_500_000, 639_000_000));
        let verifying = fold_pull_line(&layer, r#"{"status":"verifying sha256 digest"}"#);
        assert_eq!(verifying.total, 639_000_000);
        assert!(!verifying.done);
        let done = fold_pull_line(&verifying, r#"{"status":"success"}"#);
        assert!(done.done && done.error.is_none());
        assert_eq!(done.completed, done.total);
        let failed = fold_pull_line(
            &layer,
            r#"{"error":"pull model manifest: file does not exist\u001b[0m"}"#,
        );
        assert!(failed.done);
        assert_eq!(
            failed.error.as_deref(),
            Some("pull model manifest: file does not exist [0m")
        );
        assert_eq!(fold_pull_line(&layer, "garbage"), layer);
    }

    #[test]
    fn homebrew_runs_two_fixed_commands_and_its_tail_is_cleaned() {
        assert_eq!(BREW_INSTALL, ["install", "ollama"]);
        assert_eq!(BREW_START, ["services", "start", "ollama"]);
        assert_eq!(
            brew(|path| path == Path::new("/usr/local/bin/brew")),
            Some(PathBuf::from("/usr/local/bin/brew"))
        );
        assert_eq!(brew(|_| false), None);
        let output = "==> Downloading\n\n==> Pouring ollama\n\u{1b}[32mok\u{1b}[0m\n";
        assert_eq!(tail(output, 2), ["==> Pouring ollama", "[32mok [0m"]);
        assert_eq!(tail("", 3), Vec::<String>::new());
    }
}
