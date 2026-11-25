//! The `memini-server` launcher: finding it, running it, and reading its JSON.
//!
//! The app does not reimplement the launcher. It runs the one program, with one of the fixed
//! argument vectors of [`Action`], directly: never through a shell, and never with an argument
//! that comes from the webview, the settings or a server. The program answers one JSON object
//! per command, with a `schema` number; a schema this app does not know is refused.
//!
//! A program that is not installed is a normal state: [`find`] answers `None` and the Server
//! screen explains how to install it.
//!
//! An application started from the desktop does not get the `PATH` of a terminal (on macOS it is
//! `/usr/bin:/bin:/usr/sbin:/sbin`), so the directories package managers and Docker install
//! into ([`well_known_directories`]) are searched after `PATH`, and added to the `PATH` of the
//! program, which looks for `docker` there itself.

use std::env;
use std::ffi::{OsStr, OsString};
use std::net::{Ipv4Addr, TcpListener};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{clean, AppError, AppResult, ErrorKind};
use crate::secret::Token;

/// The one program this app runs.
pub const PROGRAM: &str = "memini-server";

/// The `schema` of the launcher's JSON this app understands.
pub const KNOWN_SCHEMA: u64 = 1;

/// The file names the program can have: a script on macOS and Linux, a shim on Windows.
#[cfg(windows)]
pub const PROGRAM_FILE_NAMES: &[&str] = &[
    "memini-server.exe",
    "memini-server.cmd",
    "memini-server.bat",
];
#[cfg(not(windows))]
pub const PROGRAM_FILE_NAMES: &[&str] = &["memini-server"];

/// What the app may ask the launcher to do, and nothing else.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Status,
    Start,
    Stop,
    McpOn,
    McpOff,
    /// The first admin token of the managed server.
    Bootstrap,
    /// Another admin token although an active one exists.
    BootstrapForce,
    /// The first start of a server that is not installed yet, on the port the app chose.
    FirstStart {
        port: u16,
        /// Run the search model in Ollama on this computer from the start, not in a container.
        host_model: bool,
    },
    /// Removes the server with its data; the app asks the user first.
    Uninstall,
    /// How many cores the search model may use.
    IndexingStatus,
    /// Sets how many cores the search model may use, and recreates a running model.
    IndexingSet(IndexingMode),
    /// Moves the search model to Ollama on this computer or back into its container.
    EmbeddingSet(Runtime),
}

/// Where the search model runs: in the server's ollama container, or in Ollama on this computer
/// (the GPU of a Mac). The launcher keeps it in the managed server's Compose files.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Runtime {
    #[default]
    Container,
    Host,
}

impl Runtime {
    const fn word(self) -> &'static str {
        match self {
            Self::Container => "container",
            Self::Host => "host",
        }
    }
}

/// How much of this computer the search model uses while it embeds (scenario S18): half the
/// cores, one or two in the background, or no limit. The launcher keeps it in the managed
/// server's Compose files.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IndexingMode {
    Half,
    Background,
    Full,
}

impl IndexingMode {
    const fn word(self) -> &'static str {
        match self {
            Self::Half => "half",
            Self::Background => "background",
            Self::Full => "full",
        }
    }
}

/// The port a first start asks for, and how many after it are tried when it is taken.
pub const DEFAULT_PORT: u16 = 6364;
pub const PORTS_TRIED: u16 = 20;

impl Action {
    /// The whole argument vector of the action. The only value in it that is not fixed is the
    /// port of a first start, a number the app chose itself.
    pub fn arguments(self) -> Vec<String> {
        let fixed: &[&str] = match self {
            Self::Status => &["status", "--json"],
            Self::Start => &["start", "--json"],
            Self::Stop => &["stop", "--json"],
            Self::McpOn => &["mcp", "on", "--json"],
            Self::McpOff => &["mcp", "off", "--json"],
            Self::Bootstrap => &["bootstrap", "--json"],
            Self::BootstrapForce => &["bootstrap", "--force", "--json"],
            Self::Uninstall => &["uninstall", "--yes", "--json"],
            Self::IndexingStatus => &["indexing", "status", "--json"],
            Self::IndexingSet(mode) => {
                return ["indexing", "set", mode.word(), "--json"]
                    .iter()
                    .map(|&word| word.to_owned())
                    .collect();
            }
            Self::EmbeddingSet(runtime) => {
                return ["embedding", runtime.word(), "--json"]
                    .iter()
                    .map(|&word| word.to_owned())
                    .collect();
            }
            Self::FirstStart { port, host_model } => {
                let runtime = if host_model {
                    Runtime::Host
                } else {
                    Runtime::Container
                };
                return vec![
                    "start".to_owned(),
                    "--http-port".to_owned(),
                    port.to_string(),
                    "--embedding".to_owned(),
                    runtime.word().to_owned(),
                    "--json".to_owned(),
                ];
            }
        };
        fixed.iter().map(|&word| word.to_owned()).collect()
    }

    /// How long the action may take. The first start downloads the application, the database
    /// and the search model.
    pub const fn time_limit(self) -> Duration {
        match self {
            Self::Status | Self::IndexingStatus => Duration::from_secs(60),
            Self::IndexingSet(_) => Duration::from_secs(5 * 60),
            // Moving to Ollama on this computer may download the model first.
            Self::EmbeddingSet(_) => Duration::from_secs(60 * 60),
            Self::Start | Self::FirstStart { .. } => Duration::from_secs(60 * 60),
            Self::Stop | Self::Uninstall => Duration::from_secs(5 * 60),
            Self::McpOn | Self::McpOff => Duration::from_secs(10 * 60),
            Self::Bootstrap | Self::BootstrapForce => Duration::from_secs(2 * 60),
        }
    }

    pub fn words(self) -> String {
        let arguments = self.arguments();
        format!("{PROGRAM} {}", arguments[..arguments.len() - 1].join(" "))
    }
}

/// The first port from [`DEFAULT_PORT`] that `is_free` accepts, of [`PORTS_TRIED`].
pub fn first_free_port(is_free: impl Fn(u16) -> bool) -> Option<u16> {
    (DEFAULT_PORT..DEFAULT_PORT.saturating_add(PORTS_TRIED)).find(|&port| is_free(port))
}

/// Whether nothing listens on `port` of this computer's loopback address, where the server
/// publishes its port. Another server, or Docker publishing one, holds it.
pub fn port_is_free(port: u16) -> bool {
    TcpListener::bind((Ipv4Addr::LOCALHOST, port)).is_ok()
}

/// The answer of `memini-server status --json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LauncherStatus {
    pub installed: bool,
    pub running: bool,
    pub api_url: Option<String>,
    pub mcp_url: Option<String>,
    pub mcp_enabled: Option<bool>,
    pub image: Option<String>,
    pub directory: Option<String>,
    pub docker: DockerStatus,
    pub services: Vec<ServiceState>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DockerStatus {
    pub available: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServiceState {
    pub name: String,
    pub state: String,
    pub health: Option<String>,
}

/// The answer of `start`, `stop`, `mcp on` and `mcp off` with `--json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LauncherResult {
    pub ok: bool,
    pub error: Option<String>,
}

/// The answer of `indexing status|set --json`: the mode and the limit it gives.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IndexingAnswer {
    pub ok: bool,
    pub error: Option<String>,
    pub indexing: Option<IndexingState>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IndexingState {
    pub mode: IndexingMode,
    /// The cores the model may use; `None` for no limit.
    pub cpus: Option<f64>,
    /// The cores of this computer.
    pub cores: u32,
    /// Where the model runs; a launcher from before the setting answers without it.
    #[serde(default)]
    pub runtime: Runtime,
}

/// The indexing answer in the launcher's output.
pub fn parse_indexing(output: &str) -> AppResult<IndexingAnswer> {
    let mut answer: IndexingAnswer = parse_object(output)?;
    answer.error = answer.error.map(|text| clean(&text));
    Ok(answer)
}

/// The status in the launcher's output.
pub fn parse_status(output: &str) -> AppResult<LauncherStatus> {
    let mut status: LauncherStatus = parse_object(output)?;
    status.api_url = status.api_url.map(|text| clean(&text));
    status.mcp_url = status.mcp_url.map(|text| clean(&text));
    status.image = status.image.map(|text| clean(&text));
    status.directory = status.directory.map(|text| clean(&text));
    status.docker.error = status.docker.error.map(|text| clean(&text));
    for service in &mut status.services {
        service.name = clean(&service.name);
        service.state = clean(&service.state);
        service.health = service.health.take().map(|text| clean(&text));
    }
    Ok(status)
}

/// The result of an action in the launcher's output.
pub fn parse_result(output: &str) -> AppResult<LauncherResult> {
    let mut result: LauncherResult = parse_object(output)?;
    result.error = result.error.map(|text| clean(&text));
    Ok(result)
}

/// The answer of `bootstrap --json`. The token is a [`Token`]: it cannot be serialized or
/// printed, so it can go to the keychain and nowhere else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BootstrapAnswer {
    pub ok: bool,
    pub error: Option<String>,
    pub token: Option<Token>,
}

/// The first admin token in the launcher's output. An answer that says it succeeded without a
/// well-formed token is not the contract.
pub fn parse_bootstrap(output: &str) -> AppResult<BootstrapAnswer> {
    #[derive(Deserialize)]
    struct Wire {
        ok: bool,
        error: Option<String>,
        token: Option<String>,
    }
    let wire: Wire = parse_object(output)?;
    let token = wire.token.as_deref().and_then(Token::parse);
    if wire.ok && token.is_none() {
        return Err(not_the_contract("answered without the token it created"));
    }
    Ok(BootstrapAnswer {
        ok: wire.ok,
        error: wire.error.map(|text| clean(&text)),
        token: if wire.ok { token } else { None },
    })
}

/// The one JSON object of the output, checked for a schema this app knows. The whole output is
/// tried first, then its last line that is not empty, should the program have printed progress
/// before its answer.
pub(crate) fn parse_object<T: for<'de> Deserialize<'de>>(output: &str) -> AppResult<T> {
    let text = output.trim();
    let value = serde_json::from_str::<Value>(text)
        .ok()
        .or_else(|| serde_json::from_str::<Value>(text.lines().next_back()?.trim()).ok());
    let Some(Value::Object(object)) = value else {
        return Err(not_the_contract("did not answer with a JSON object"));
    };
    match object.get("schema").and_then(Value::as_u64) {
        Some(KNOWN_SCHEMA) => {}
        Some(other) => {
            return Err(AppError::new(
                ErrorKind::LauncherSchema,
                format!(
                    "{PROGRAM} answered with schema {other}; this app understands schema \
                     {KNOWN_SCHEMA}. Update the app, or install the launcher version that \
                     matches it."
                ),
            ));
        }
        None => return Err(not_the_contract("answered without a schema number")),
    }
    serde_json::from_value(Value::Object(object))
        .map_err(|_| not_the_contract("answered with fields this app does not expect"))
}

fn not_the_contract(what: &str) -> AppError {
    AppError::new(
        ErrorKind::LauncherOutput,
        format!(
            "{PROGRAM} {what}. It may be older than this app needs: update {PROGRAM} and try \
             again."
        ),
    )
}

/// What the app needs of the process environment to find the program.
#[derive(Debug, Clone, Default)]
pub struct SearchPath {
    /// The directories of `PATH`, then the well-known ones.
    pub directories: Vec<PathBuf>,
}

impl SearchPath {
    pub fn from_environment() -> Self {
        let mut directories: Vec<PathBuf> = env::var_os("PATH")
            .map(|path| env::split_paths(&path).collect())
            .unwrap_or_default();
        directories.extend(well_known_directories());
        Self::of(directories)
    }

    /// Absolute directories only, each once: a relative entry would be read against whatever
    /// directory the app was started in.
    pub fn of(directories: Vec<PathBuf>) -> Self {
        let mut kept: Vec<PathBuf> = Vec::new();
        for directory in directories {
            if directory.is_absolute() && !kept.contains(&directory) {
                kept.push(directory);
            }
        }
        Self { directories: kept }
    }

    /// The value of `PATH` for the program: the same directories, in the same order.
    pub fn joined(&self) -> Option<OsString> {
        env::join_paths(&self.directories).ok()
    }
}

/// Where package managers install the launcher (Homebrew, Scoop) and where Docker installs its
/// command, for an app that was not started from a terminal. The launcher looks for `docker` on
/// its own `PATH`, so these are added to it as well.
pub fn well_known_directories() -> Vec<PathBuf> {
    let home = env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from);
    let in_home = |parts: &[&str]| {
        home.as_ref().map(|home| {
            parts
                .iter()
                .fold(home.clone(), |path, part| path.join(part))
        })
    };
    let mut directories: Vec<Option<PathBuf>> = Vec::new();
    if cfg!(target_os = "macos") {
        directories.push(Some(PathBuf::from("/opt/homebrew/bin")));
        directories.push(Some(PathBuf::from("/usr/local/bin")));
        directories.push(in_home(&[".docker", "bin"]));
        directories.push(in_home(&[".orbstack", "bin"]));
        directories.push(in_home(&[".rd", "bin"]));
        directories.push(Some(PathBuf::from(
            "/Applications/Docker.app/Contents/Resources/bin",
        )));
    } else if cfg!(windows) {
        directories.push(env::var_os("SCOOP").map(|scoop| PathBuf::from(scoop).join("shims")));
        directories.push(in_home(&["scoop", "shims"]));
        directories.push(env::var_os("ProgramFiles").map(|programs| {
            PathBuf::from(programs)
                .join("Docker")
                .join("Docker")
                .join("resources")
                .join("bin")
        }));
    } else {
        directories.push(Some(PathBuf::from("/home/linuxbrew/.linuxbrew/bin")));
        directories.push(in_home(&[".linuxbrew", "bin"]));
        directories.push(Some(PathBuf::from("/usr/local/bin")));
        directories.push(Some(PathBuf::from("/usr/bin")));
        directories.push(Some(PathBuf::from("/snap/bin")));
        directories.push(in_home(&[".docker", "bin"]));
        directories.push(in_home(&[".rd", "bin"]));
    }
    directories.into_iter().flatten().collect()
}

/// The first file named as the program in `search`, by `is_file`.
pub fn find(search: &SearchPath, is_file: impl Fn(&Path) -> bool) -> Option<PathBuf> {
    search
        .directories
        .iter()
        .flat_map(|directory| {
            PROGRAM_FILE_NAMES
                .iter()
                .map(move |name| directory.join(name))
        })
        .find(|candidate| is_file(candidate))
}

/// A path the user set for the program: absolute, and named as the program is. The app runs
/// one program; a setting cannot make it run another.
pub fn check_configured_path(text: &str) -> AppResult<PathBuf> {
    let path = PathBuf::from(text.trim());
    if !path.is_absolute() {
        return Err(AppError::invalid(format!(
            "Enter the full path of {PROGRAM}, starting from the root of the disk."
        )));
    }
    let named_as_program = path
        .file_name()
        .and_then(OsStr::to_str)
        .is_some_and(|name| {
            PROGRAM_FILE_NAMES
                .iter()
                .any(|expected| name.eq_ignore_ascii_case(expected))
        });
    if !named_as_program {
        return Err(AppError::invalid(format!(
            "The path must end in {}: this app runs that program only.",
            PROGRAM_FILE_NAMES.join(" or ")
        )));
    }
    Ok(path)
}

/// Where the program that is run comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    /// The path the user set.
    Configured,
    /// The launcher the app carries with it, next to its own executable.
    Bundled,
    /// A launcher installed on this computer, found on the search path.
    Installed,
}

/// The launcher the app carries: the file named as the program in the directory of the app's
/// own executable, which is where Tauri puts a sidecar on every platform.
pub fn bundled(executable: Option<&Path>, is_file: impl Fn(&Path) -> bool) -> Option<PathBuf> {
    let directory = executable?.parent()?;
    PROGRAM_FILE_NAMES
        .iter()
        .map(|name| directory.join(name))
        .find(|candidate| is_file(candidate))
}

/// The program to run. The path the user set comes first; without one, the launcher inside the
/// app; without that, one installed on this computer. A path that is set and has no file is
/// "not installed", not a reason to fall back: the user chose it.
pub fn locate(
    configured: Option<&str>,
    executable: Option<&Path>,
    search: &SearchPath,
) -> Option<(PathBuf, Source)> {
    if let Some(text) = configured {
        return check_configured_path(text)
            .ok()
            .filter(|path| path.is_file())
            .map(|path| (path, Source::Configured));
    }
    bundled(executable, Path::is_file)
        .map(|path| (path, Source::Bundled))
        .or_else(|| find(search, Path::is_file).map(|path| (path, Source::Installed)))
}

pub fn missing() -> AppError {
    AppError::new(
        ErrorKind::LauncherMissing,
        format!("The {PROGRAM} program was not found on this computer."),
    )
}

/// Runs the program for `action` and returns what it printed. The program's exit code is not
/// judged here: with `--json` a failure is an answer too (`"ok": false`); without any output it
/// is an error.
pub async fn run(program: &Path, action: Action, search: &SearchPath) -> AppResult<String> {
    let invocation = Invocation {
        arguments: action.arguments(),
        words: action.words(),
        time_limit: action.time_limit(),
    };
    run_invocation(program, &invocation, None, search).await
}

/// One run of the program: its whole argument vector, the words that name it in a message, and
/// how long it may take.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invocation {
    pub arguments: Vec<String>,
    pub words: String,
    pub time_limit: Duration,
}

/// Runs the program as `invocation` says. `input`, a secret the program reads on its standard
/// input, is written there and the input closed; without it the program reads nothing.
pub async fn run_invocation(
    program: &Path,
    invocation: &Invocation,
    input: Option<&[u8]>,
    search: &SearchPath,
) -> AppResult<String> {
    let mut command = tokio::process::Command::new(program);
    command
        .args(&invocation.arguments)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // The launcher's JSON commands need no token; none is handed to it.
        .env_remove("MEMINI_TOKEN")
        .env_remove("MEMINI_TOKEN_FILE")
        .kill_on_drop(true);
    if let Some(path) = search.joined() {
        command.env("PATH", path);
    }
    #[cfg(windows)]
    {
        // No console window for a program started by a windowed app.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let words = &invocation.words;
    let mut child = command.spawn().map_err(|error| {
        AppError::new(
            ErrorKind::LauncherFailed,
            // The kind only: the text of the error can hold paths.
            format!("{words} could not be started ({:?}).", error.kind()),
        )
    })?;
    if let (Some(input), Some(mut stdin)) = (input, child.stdin.take()) {
        use tokio::io::AsyncWriteExt;
        stdin.write_all(input).await.map_err(|error| {
            AppError::new(
                ErrorKind::LauncherFailed,
                format!("{words} could not be given its input ({:?}).", error.kind()),
            )
        })?;
        // Dropped: the input is closed, and the program reads to its end.
    }
    let limit = invocation.time_limit;
    // Past the limit the future is dropped, and the program with it (kill_on_drop).
    let output = tokio::time::timeout(limit, child.wait_with_output())
        .await
        .map_err(|_| {
            AppError::new(
                ErrorKind::LauncherFailed,
                format!(
                    "{words} did not finish within {} minutes and was stopped.",
                    limit.as_secs().div_ceil(60)
                ),
            )
        })?
        .map_err(|error| {
            AppError::new(
                ErrorKind::LauncherFailed,
                format!("{words} could not be waited for ({:?}).", error.kind()),
            )
        })?;
    let printed = String::from_utf8_lossy(&output.stdout).into_owned();
    if printed.trim().is_empty() {
        return Err(silent_failure(words, output.status.code(), &output.stderr));
    }
    Ok(printed)
}

/// The error for a run that printed nothing on its standard output: its exit code and the last
/// line of what it said on its standard error, which is the launcher's own message.
fn silent_failure(words: &str, code: Option<i32>, stderr: &[u8]) -> AppError {
    let said = String::from_utf8_lossy(stderr);
    let last_line = said.lines().map(str::trim).rfind(|line| !line.is_empty());
    let exit = code.map_or_else(
        || "was stopped by a signal".to_owned(),
        |code| format!("ended with exit code {code}"),
    );
    let message = match last_line {
        Some(line) => format!("{words} {exit} and no answer. It said: {line}"),
        None => format!("{words} {exit} and no answer."),
    };
    let kind = if code == Some(0) {
        ErrorKind::LauncherOutput
    } else {
        ErrorKind::LauncherFailed
    };
    AppError::new(kind, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret::fake_token;

    const STATUS: &str = r#"{
        "schema": 1,
        "installed": true,
        "running": true,
        "api_url": "http://127.0.0.1:6364",
        "mcp_url": "http://127.0.0.1:6364/mcp",
        "mcp_enabled": true,
        "image": "registry.example.org/memini:0.2.0",
        "directory": "/home/sam/.local/share/memini/server",
        "docker": {"available": true, "error": null},
        "services": [
            {"name": "db", "state": "running", "health": "healthy"},
            {"name": "memini", "state": "running", "health": "healthy"},
            {"name": "ollama", "state": "running", "health": null}
        ]
    }"#;

    #[test]
    fn the_status_of_a_running_server_is_read() {
        let status = parse_status(STATUS).expect("the contract");
        assert!(status.installed && status.running);
        assert_eq!(status.api_url.as_deref(), Some("http://127.0.0.1:6364"));
        assert_eq!(status.mcp_enabled, Some(true));
        assert_eq!(status.services.len(), 3);
        assert_eq!(status.services[2].name, "ollama");
        assert_eq!(status.services[2].health, None);
        assert!(status.docker.available);
    }

    #[test]
    fn the_status_of_a_computer_without_docker_or_a_server_is_read() {
        let status = parse_status(
            r#"{"schema": 1, "installed": false, "running": false, "api_url": null,
                "mcp_url": null, "mcp_enabled": null, "image": null, "directory": null,
                "docker": {"available": false, "error": "docker was not found"},
                "services": []}"#,
        )
        .expect("the contract");
        assert!(!status.installed && !status.running);
        assert_eq!(status.mcp_enabled, None);
        assert_eq!(status.docker.error.as_deref(), Some("docker was not found"));
        assert!(status.services.is_empty());
    }

    #[test]
    fn an_unknown_schema_is_refused_with_a_clear_message() {
        let error = parse_status(&STATUS.replace("\"schema\": 1", "\"schema\": 2"))
            .expect_err("a newer schema");
        assert_eq!(error.kind, ErrorKind::LauncherSchema);
        assert!(error.message.contains("schema 2"));
        assert!(error.message.contains("schema 1"));
        let error = parse_result(r#"{"schema": 0, "ok": true, "error": null}"#)
            .expect_err("an older schema");
        assert_eq!(error.kind, ErrorKind::LauncherSchema);
    }

    #[test]
    fn output_that_is_not_the_contract_is_refused() {
        for output in [
            "",
            "No managed server at /srv/memini",
            "[1, 2]",
            r#"{"installed": true}"#,
            r#"{"schema": "1", "ok": true, "error": null}"#,
            r#"{"schema": 1, "ok": "yes", "error": null}"#,
            r#"{"schema": 1, "installed": true}"#,
        ] {
            let status = parse_status(output).expect_err("refused");
            assert_eq!(status.kind, ErrorKind::LauncherOutput, "{output}");
            let result = parse_result(output).expect_err("refused");
            assert_eq!(result.kind, ErrorKind::LauncherOutput, "{output}");
        }
    }

    #[test]
    fn progress_lines_before_the_answer_are_skipped_and_new_fields_ignored() {
        let output =
            "Pulling the model\n42%\n{\"schema\": 1, \"ok\": true, \"error\": null, \"took\": 3}\n";
        assert_eq!(
            parse_result(output).expect("the contract"),
            LauncherResult {
                ok: true,
                error: None
            }
        );
    }

    #[test]
    fn a_failed_action_is_an_answer_and_its_text_is_data() {
        let token = fake_token('q');
        let output =
            format!(r#"{{"schema": 1, "ok": false, "error": "port taken\n\u001b[31m{token}"}}"#);
        let result = parse_result(&output).expect("the contract");
        assert!(!result.ok);
        let error = result.error.expect("a reason");
        assert!(error.starts_with("port taken"));
        assert!(!error.contains(&token));
        assert!(!error.contains('\n') && !error.contains('\u{1b}'));
    }

    #[test]
    fn every_action_has_a_fixed_argument_vector_that_asks_for_json() {
        assert_eq!(Action::Status.arguments(), ["status", "--json"]);
        assert_eq!(Action::Start.arguments(), ["start", "--json"]);
        assert_eq!(Action::Stop.arguments(), ["stop", "--json"]);
        assert_eq!(Action::McpOn.arguments(), ["mcp", "on", "--json"]);
        assert_eq!(Action::McpOff.arguments(), ["mcp", "off", "--json"]);
        assert_eq!(
            Action::Uninstall.arguments(),
            ["uninstall", "--yes", "--json"]
        );
        assert_eq!(
            Action::FirstStart {
                port: 6371,
                host_model: false
            }
            .arguments(),
            [
                "start",
                "--http-port",
                "6371",
                "--embedding",
                "container",
                "--json"
            ]
        );
        assert_eq!(
            Action::FirstStart {
                port: 6364,
                host_model: true
            }
            .arguments(),
            [
                "start",
                "--http-port",
                "6364",
                "--embedding",
                "host",
                "--json"
            ]
        );
        assert_eq!(
            Action::EmbeddingSet(Runtime::Host).arguments(),
            ["embedding", "host", "--json"]
        );
        assert_eq!(
            Action::EmbeddingSet(Runtime::Container).arguments(),
            ["embedding", "container", "--json"]
        );
        assert_eq!(Action::Uninstall.words(), "memini-server uninstall --yes");
        assert_eq!(
            Action::IndexingStatus.arguments(),
            ["indexing", "status", "--json"]
        );
        assert_eq!(
            Action::IndexingSet(IndexingMode::Background).arguments(),
            ["indexing", "set", "background", "--json"]
        );
        assert_eq!(Action::Bootstrap.arguments(), ["bootstrap", "--json"]);
        assert_eq!(
            Action::BootstrapForce.arguments(),
            ["bootstrap", "--force", "--json"]
        );
        assert_eq!(Action::McpOff.words(), "memini-server mcp off");
    }

    #[test]
    fn the_indexing_answer_gives_the_mode_and_its_limit() {
        let answer = parse_indexing(
            r#"{"schema": 1, "ok": true, "error": null,
                "indexing": {"mode": "half", "cpus": 4.0, "cores": 8}}"#,
        )
        .expect("the contract");
        let state = answer.indexing.expect("a state");
        assert_eq!(state.mode, IndexingMode::Half);
        assert_eq!(state.cpus, Some(4.0));
        assert_eq!(state.cores, 8);
        // A launcher from before the runtime setting: the model is in its container.
        assert_eq!(state.runtime, Runtime::Container);
        let host = parse_indexing(
            r#"{"schema": 1, "ok": true, "error": null,
                "indexing": {"mode": "half", "cpus": 4.0, "cores": 8, "runtime": "host"},
                "host": {"found": true, "version": "0.34.2"}}"#,
        )
        .expect("the contract");
        assert_eq!(host.indexing.expect("a state").runtime, Runtime::Host);
        let full = parse_indexing(
            r#"{"schema": 1, "ok": true, "error": null,
                "indexing": {"mode": "full", "cpus": null, "cores": 8}}"#,
        )
        .expect("the contract");
        assert_eq!(full.indexing.expect("a state").cpus, None);
        let refused =
            parse_indexing(r#"{"schema": 1, "ok": false, "error": "no managed server at /srv/x"}"#)
                .expect("the contract");
        assert!(!refused.ok && refused.indexing.is_none());
    }

    #[test]
    fn a_first_start_takes_the_first_free_port_from_the_default() {
        assert_eq!(first_free_port(|_| true), Some(DEFAULT_PORT));
        assert_eq!(
            first_free_port(|port| port > DEFAULT_PORT + 2),
            Some(DEFAULT_PORT + 3)
        );
        assert_eq!(first_free_port(|_| false), None);
        // A port that is listened on is not free.
        let taken = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("a port");
        let port = taken.local_addr().expect("its address").port();
        assert!(!port_is_free(port));
    }

    #[test]
    fn the_program_is_found_in_the_first_directory_that_has_it() {
        let search = SearchPath::of(vec![
            PathBuf::from("relative/bin"),
            absolute("first"),
            absolute("second"),
            absolute("first"),
        ]);
        assert_eq!(search.directories, [absolute("first"), absolute("second")]);
        let in_second = absolute("second").join(PROGRAM_FILE_NAMES[0]);
        assert_eq!(
            find(&search, |path| path == in_second),
            Some(in_second.clone())
        );
        assert_eq!(find(&search, |_| false), None);
        let in_first = absolute("first").join(PROGRAM_FILE_NAMES[0]);
        assert_eq!(find(&search, |_| true), Some(in_first));
    }

    #[test]
    fn a_configured_path_is_absolute_and_names_the_program() {
        let good = absolute("tools").join(PROGRAM_FILE_NAMES[0]);
        let text = good.to_str().expect("unicode");
        assert_eq!(
            check_configured_path(&format!(" {text} ")).expect("accepted"),
            good
        );
        assert!(check_configured_path(PROGRAM_FILE_NAMES[0]).is_err());
        assert!(check_configured_path("bin/memini-server").is_err());
        let other = absolute("tools").join("sh");
        assert!(check_configured_path(other.to_str().expect("unicode")).is_err());
        let renamed = absolute("tools").join("memini-server-old");
        assert!(check_configured_path(renamed.to_str().expect("unicode")).is_err());
        // A configured path that does not exist is "not installed", not a reason to search.
        assert_eq!(locate(Some(text), None, &SearchPath::default()), None);
    }

    #[test]
    fn the_launcher_inside_the_app_is_the_one_beside_its_executable() {
        let executable = absolute("app").join("memini-desktop");
        let beside = absolute("app").join(PROGRAM_FILE_NAMES[0]);
        assert_eq!(
            bundled(Some(&executable), |path| path == beside),
            Some(beside.clone())
        );
        assert_eq!(bundled(Some(&executable), |_| false), None);
        assert_eq!(bundled(None, |_| true), None);
    }

    #[test]
    fn the_first_admin_token_is_read_and_cannot_be_printed() {
        let token = fake_token('b');
        let output = format!(r#"{{"schema": 1, "ok": true, "error": null, "token": "{token}"}}"#);
        let answer = parse_bootstrap(&output).expect("the contract");
        assert!(answer.ok);
        assert_eq!(
            answer.token.as_ref().map(Token::expose),
            Some(token.as_str())
        );
        assert!(!format!("{answer:?}").contains(&token));
    }

    #[test]
    fn a_refused_bootstrap_gives_its_reason_and_no_token() {
        let output = r#"{"schema": 1, "ok": false, "error": "memini: an active admin token exists", "token": null}"#;
        let answer = parse_bootstrap(output).expect("the contract");
        assert_eq!(
            answer,
            BootstrapAnswer {
                ok: false,
                error: Some("memini: an active admin token exists".to_owned()),
                token: None,
            }
        );
    }

    #[test]
    fn a_bootstrap_that_succeeds_without_a_token_is_not_the_contract() {
        for output in [
            r#"{"schema": 1, "ok": true, "error": null, "token": null}"#,
            r#"{"schema": 1, "ok": true, "error": null, "token": "not a token"}"#,
            r#"{"schema": 1, "ok": true, "error": null}"#,
        ] {
            let error = parse_bootstrap(output).expect_err("refused");
            assert_eq!(error.kind, ErrorKind::LauncherOutput, "{output}");
        }
        let newer = r#"{"schema": 2, "ok": true, "error": null, "token": null}"#;
        assert_eq!(
            parse_bootstrap(newer).expect_err("refused").kind,
            ErrorKind::LauncherSchema
        );
    }

    #[test]
    fn a_run_without_an_answer_reports_the_exit_code_and_the_last_line() {
        let error = silent_failure(
            "memini-server status",
            Some(2),
            b"usage: memini-server\nmemini-server: error: unexpected arguments for status\n\n",
        );
        assert_eq!(error.kind, ErrorKind::LauncherFailed);
        assert_eq!(
            error.message,
            "memini-server status ended with exit code 2 and no answer. It said: memini-server: \
             error: unexpected arguments for status"
        );
        let quiet = silent_failure("memini-server stop", Some(0), b"");
        assert_eq!(quiet.kind, ErrorKind::LauncherOutput);
    }

    fn absolute(name: &str) -> PathBuf {
        if cfg!(windows) {
            PathBuf::from(format!("C:\\{name}"))
        } else {
            PathBuf::from(format!("/{name}"))
        }
    }
}
