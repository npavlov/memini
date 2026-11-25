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
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{clean, AppError, AppResult, ErrorKind};

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
}

impl Action {
    /// The whole argument vector of the action.
    pub const fn arguments(self) -> &'static [&'static str] {
        match self {
            Self::Status => &["status", "--json"],
            Self::Start => &["start", "--json"],
            Self::Stop => &["stop", "--json"],
            Self::McpOn => &["mcp", "on", "--json"],
            Self::McpOff => &["mcp", "off", "--json"],
        }
    }

    /// How long the action may take. The first start downloads the application, the database
    /// and the search model.
    pub const fn time_limit(self) -> Duration {
        match self {
            Self::Status => Duration::from_secs(60),
            Self::Start => Duration::from_secs(60 * 60),
            Self::Stop => Duration::from_secs(5 * 60),
            Self::McpOn | Self::McpOff => Duration::from_secs(10 * 60),
        }
    }

    fn words(self) -> String {
        let arguments = self.arguments();
        format!("{PROGRAM} {}", arguments[..arguments.len() - 1].join(" "))
    }
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

/// The one JSON object of the output, checked for a schema this app knows. The whole output is
/// tried first, then its last line that is not empty, should the program have printed progress
/// before its answer.
fn parse_object<T: for<'de> Deserialize<'de>>(output: &str) -> AppResult<T> {
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

/// The program to run: the configured path if one is set, else the first found on the search
/// path. `None` when it is not installed, or the configured file is not there.
pub fn locate(configured: Option<&str>, search: &SearchPath) -> Option<PathBuf> {
    match configured {
        Some(text) => check_configured_path(text)
            .ok()
            .filter(|path| path.is_file()),
        None => find(search, Path::is_file),
    }
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
    let mut command = tokio::process::Command::new(program);
    command
        .args(action.arguments())
        .stdin(Stdio::null())
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
    let words = action.words();
    let child = command.spawn().map_err(|error| {
        AppError::new(
            ErrorKind::LauncherFailed,
            // The kind only: the text of the error can hold paths.
            format!("{words} could not be started ({:?}).", error.kind()),
        )
    })?;
    // Past the limit the future is dropped, and the program with it (kill_on_drop).
    let output = tokio::time::timeout(action.time_limit(), child.wait_with_output())
        .await
        .map_err(|_| {
            AppError::new(
                ErrorKind::LauncherFailed,
                format!(
                    "{words} did not finish within {} minutes and was stopped.",
                    action.time_limit().as_secs() / 60
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
        return Err(silent_failure(&words, output.status.code(), &output.stderr));
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
        let actions = [
            Action::Status,
            Action::Start,
            Action::Stop,
            Action::McpOn,
            Action::McpOff,
        ];
        let vectors: Vec<&[&str]> = actions.iter().map(|action| action.arguments()).collect();
        assert_eq!(
            vectors,
            [
                &["status", "--json"][..],
                &["start", "--json"][..],
                &["stop", "--json"][..],
                &["mcp", "on", "--json"][..],
                &["mcp", "off", "--json"][..],
            ]
        );
        assert_eq!(Action::McpOff.words(), "memini-server mcp off");
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
        assert_eq!(locate(Some(text), &SearchPath::default()), None);
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
