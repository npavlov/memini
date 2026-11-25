//! Repositories and git access of the server on this computer (scenario S17): the requests the
//! webview may make, the checks of what the user typed, the launcher's argument vectors and the
//! parsing of its answers.
//!
//! Two of these arguments come from the user, an exception to the rule that the webview never
//! passes one: the folder of a repository and the hosts of git access. Each is checked here
//! before it becomes one element of the argument vector, and the launcher is never run through a
//! shell. The folder must be an absolute path that exists, resolved, of a git repository; a host
//! must be a host name. An ssh key or an https token never is an argument: Rust reads it from a
//! file or the clipboard and the launcher reads it on its standard input. It never goes back to
//! the webview.

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::error::{clean, AppError, AppResult};
use crate::launcher::{self, Invocation, PROGRAM};

/// The most hosts one git access names.
const MAX_HOSTS: usize = 10;
/// The most bytes of an ssh key or an https token, as the launcher reads them.
pub const MAX_SECRET: usize = 16384;

/// What the webview asks of the folders of this computer that the server may read.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum RepoRequest {
    List,
    Add { path: String },
    Remove { name: String },
}

/// Where the secret of a git access comes from. The secret itself never crosses the webview.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "from", rename_all = "snake_case")]
pub enum SecretSource {
    Clipboard,
    File { path: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AccessKind {
    Ssh,
    Https,
}

/// What the webview asks of the server's git access.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum GitRequest {
    Status,
    SshKey {
        hosts: Vec<String>,
        source: SecretSource,
    },
    HttpsToken {
        hosts: Vec<String>,
        #[serde(default)]
        username: Option<String>,
        source: SecretSource,
    },
    Remove {
        kind: AccessKind,
    },
}

/// A secret the launcher reads on its standard input. Never serialized, never printed; its
/// bytes are overwritten when it is dropped.
pub struct Secret(Vec<u8>);

impl Secret {
    pub fn expose(&self) -> &[u8] {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Secret([redacted])")
    }
}

impl Drop for Secret {
    fn drop(&mut self) {
        self.0.iter_mut().for_each(|byte| *byte = 0);
    }
}

/// An ssh private key: the text between and with the markers of one, without a passphrase,
/// which the launcher's ssh-keygen checks.
pub fn ssh_key(text: &str) -> AppResult<Secret> {
    let key = text.trim();
    if key.len() > MAX_SECRET || !key.starts_with("-----BEGIN") || !key.contains("PRIVATE KEY-----")
    {
        return Err(AppError::invalid(
            "That is not an ssh private key: it starts with -----BEGIN and names a PRIVATE KEY.",
        ));
    }
    Ok(Secret(format!("{key}\n").into_bytes()))
}

/// An https token: one line of printable characters without spaces.
pub fn https_token(text: &str) -> AppResult<Secret> {
    let token = text.trim();
    if token.is_empty() || token.len() > 4096 || !token.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(AppError::invalid(
            "That is not a token: one line of printable characters without spaces.",
        ));
    }
    Ok(Secret(token.as_bytes().to_vec()))
}

/// The folder of a git repository the user named: an absolute path that exists, resolved, of a
/// working copy with its own `.git` folder or of a bare repository.
pub fn check_folder(text: &str) -> AppResult<PathBuf> {
    let text = text.trim();
    if text.chars().any(char::is_control) {
        return Err(AppError::invalid(
            "A folder's path has no control characters.",
        ));
    }
    let given = Path::new(text);
    if !given.is_absolute() {
        return Err(AppError::invalid(
            "Enter the full path of the repository's folder, starting from the root of the disk.",
        ));
    }
    let folder = given
        .canonicalize()
        .map_err(|_| AppError::invalid("There is no folder at that path."))?;
    if !folder.is_dir() {
        return Err(AppError::invalid("That path is a file, not a folder."));
    }
    let git = folder.join(".git");
    if git.is_file() {
        return Err(AppError::invalid(
            "That folder is a linked worktree or a submodule. Add the repository it belongs to.",
        ));
    }
    let bare = folder.join("HEAD").is_file() && folder.join("objects").is_dir();
    if !git.is_dir() && !bare {
        return Err(AppError::invalid(
            "That folder is not a git repository: it has no .git folder.",
        ));
    }
    if folder.to_str().is_none() {
        return Err(AppError::invalid(
            "The folder's path is not valid Unicode; move the repository to a folder whose name is.",
        ));
    }
    Ok(folder)
}

/// A host name or an IPv4 address, lowercased: letters, digits and hyphens in labels of at most
/// 63, at most 253 in all. Never one that starts with `-`, which a program would take for an
/// option.
pub fn check_host(text: &str) -> AppResult<String> {
    let host = text.trim().to_ascii_lowercase();
    let label_ok = |label: &str| {
        (1..=63).contains(&label.len())
            && label
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
            && !label.starts_with('-')
            && !label.ends_with('-')
    };
    if host.is_empty() || host.len() > 253 || !host.split('.').all(label_ok) {
        return Err(AppError::invalid(format!(
            "“{}” is not a host name, such as git.example.org.",
            clean(text.trim())
        )));
    }
    Ok(host)
}

pub fn check_hosts(hosts: &[String]) -> AppResult<Vec<String>> {
    if hosts.is_empty() || hosts.len() > MAX_HOSTS {
        return Err(AppError::invalid(format!(
            "Name the host, or up to {MAX_HOSTS} hosts, of the git server."
        )));
    }
    let mut checked: Vec<String> = Vec::new();
    for host in hosts {
        let host = check_host(host)?;
        if !checked.contains(&host) {
            checked.push(host);
        }
    }
    Ok(checked)
}

/// The user name sent with an https token: printable ASCII without spaces.
pub fn check_username(text: &str) -> AppResult<String> {
    let name = text.trim();
    if name.is_empty() || name.len() > 200 || !name.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(AppError::invalid(
            "The user name is printable characters without spaces.",
        ));
    }
    Ok(name.to_owned())
}

/// The name of a folder the launcher gave: a handle.
pub fn check_name(text: &str) -> AppResult<String> {
    let ok = (1..=63).contains(&text.len())
        && text
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        && text
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_');
    if ok {
        Ok(text.to_owned())
    } else {
        Err(AppError::invalid(
            "That is not the name of a folder of the server.",
        ))
    }
}

fn invocation(arguments: Vec<String>, minutes: u64) -> Invocation {
    let words = format!(
        "{PROGRAM} {}",
        arguments
            .iter()
            .take(2)
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join(" ")
    );
    Invocation {
        arguments,
        words,
        time_limit: Duration::from_secs(minutes * 60),
    }
}

/// The run of `repos` for a checked request: `list`, `add FOLDER` or `remove NAME`. Adding and
/// removing recreate the service, which waits for it to be healthy.
pub fn repos_invocation(request: &CheckedRepo) -> Invocation {
    let mut arguments = vec!["repos".to_owned()];
    match request {
        CheckedRepo::List => arguments.push("list".to_owned()),
        CheckedRepo::Add(folder) => {
            arguments.push("add".to_owned());
            arguments.push(folder.to_string_lossy().into_owned());
        }
        CheckedRepo::Remove(name) => {
            arguments.push("remove".to_owned());
            arguments.push(name.clone());
        }
    }
    arguments.push("--json".to_owned());
    invocation(arguments, 10)
}

/// A request of the webview after its checks.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckedRepo {
    List,
    Add(PathBuf),
    Remove(String),
}

impl CheckedRepo {
    pub fn of(request: &RepoRequest) -> AppResult<Self> {
        Ok(match request {
            RepoRequest::List => Self::List,
            RepoRequest::Add { path } => Self::Add(check_folder(path)?),
            RepoRequest::Remove { name } => Self::Remove(check_name(name)?),
        })
    }
}

/// The run of `git` for a checked request. The secret of `ssh-key` and `https-token` goes on the
/// standard input, never here.
pub fn git_invocation(request: &CheckedGit) -> Invocation {
    let mut arguments = vec!["git".to_owned()];
    let hosts_of = |hosts: &[String], arguments: &mut Vec<String>| {
        for host in hosts {
            // One element: `--host=` keeps a value from ever being read as an option.
            arguments.push(format!("--host={host}"));
        }
    };
    match request {
        CheckedGit::Status => arguments.push("status".to_owned()),
        CheckedGit::SshKey { hosts } => {
            arguments.push("ssh-key".to_owned());
            hosts_of(hosts, &mut arguments);
        }
        CheckedGit::HttpsToken { hosts, username } => {
            arguments.push("https-token".to_owned());
            hosts_of(hosts, &mut arguments);
            if let Some(name) = username {
                arguments.push(format!("--username={name}"));
            }
        }
        CheckedGit::Remove(kind) => {
            arguments.push("remove".to_owned());
            arguments.push(
                match kind {
                    AccessKind::Ssh => "ssh",
                    AccessKind::Https => "https",
                }
                .to_owned(),
            );
        }
    }
    arguments.push("--json".to_owned());
    invocation(arguments, 10)
}

/// A git request after its checks, without its secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckedGit {
    Status,
    SshKey {
        hosts: Vec<String>,
    },
    HttpsToken {
        hosts: Vec<String>,
        username: Option<String>,
    },
    Remove(AccessKind),
}

impl CheckedGit {
    /// The checked request and where its secret comes from, if it has one.
    pub fn of(request: &GitRequest) -> AppResult<(Self, Option<SecretSource>)> {
        Ok(match request {
            GitRequest::Status => (Self::Status, None),
            GitRequest::Remove { kind } => (Self::Remove(*kind), None),
            GitRequest::SshKey { hosts, source } => (
                Self::SshKey {
                    hosts: check_hosts(hosts)?,
                },
                Some(source.clone()),
            ),
            GitRequest::HttpsToken {
                hosts,
                username,
                source,
            } => (
                Self::HttpsToken {
                    hosts: check_hosts(hosts)?,
                    username: username
                        .as_deref()
                        .filter(|name| !name.trim().is_empty())
                        .map(check_username)
                        .transpose()?,
                },
                Some(source.clone()),
            ),
        })
    }
}

/// A folder of this computer the server reads, as `repos --json` describes it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LocalRepo {
    pub name: String,
    /// The folder on this computer.
    pub path: String,
    /// Where the service sees it: the location of a git source.
    pub location: String,
    /// The branch checked out in it; `None` for a detached HEAD.
    pub branch: Option<String>,
    /// Whether the folder is still there.
    pub present: bool,
}

/// The answer of `repos … --json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReposAnswer {
    pub ok: bool,
    pub error: Option<String>,
    #[serde(default)]
    pub repository: Option<LocalRepo>,
    #[serde(default)]
    pub warning: Option<String>,
    #[serde(default)]
    pub repositories: Vec<LocalRepo>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KeyPrint {
    #[serde(rename = "type")]
    pub kind: String,
    pub fingerprint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HostKeys {
    pub host: String,
    pub keys: Vec<KeyPrint>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SshAccess {
    pub key: Option<KeyPrint>,
    pub hosts: Vec<HostKeys>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HttpsAccess {
    pub hosts: Vec<String>,
    pub username: String,
}

/// The answer of `git … --json`: what identifies the server's git access, never a secret.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GitAnswer {
    pub ok: bool,
    pub error: Option<String>,
    #[serde(default)]
    pub ssh: Option<SshAccess>,
    #[serde(default)]
    pub https: Option<HttpsAccess>,
}

fn clean_repo(repo: &mut LocalRepo) {
    repo.name = clean(&repo.name);
    repo.path = clean(&repo.path);
    repo.location = clean(&repo.location);
    repo.branch = repo.branch.take().map(|text| clean(&text));
}

pub fn parse_repos(output: &str) -> AppResult<ReposAnswer> {
    let mut answer: ReposAnswer = launcher::parse_object(output)?;
    answer.error = answer.error.map(|text| clean(&text));
    answer.warning = answer.warning.map(|text| clean(&text));
    if let Some(repo) = answer.repository.as_mut() {
        clean_repo(repo);
    }
    answer.repositories.iter_mut().for_each(clean_repo);
    Ok(answer)
}

fn clean_print(print: &mut KeyPrint) {
    print.kind = clean(&print.kind);
    print.fingerprint = clean(&print.fingerprint);
}

pub fn parse_git(output: &str) -> AppResult<GitAnswer> {
    let mut answer: GitAnswer = launcher::parse_object(output)?;
    answer.error = answer.error.map(|text| clean(&text));
    if let Some(ssh) = answer.ssh.as_mut() {
        if let Some(key) = ssh.key.as_mut() {
            clean_print(key);
        }
        for host in &mut ssh.hosts {
            host.host = clean(&host.host);
            host.keys.iter_mut().for_each(clean_print);
        }
    }
    if let Some(https) = answer.https.as_mut() {
        https.hosts = https.hosts.iter().map(|host| clean(host)).collect();
        https.username = clean(&https.username);
    }
    Ok(answer)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let directory =
            std::env::temp_dir().join(format!("memini-local-git-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).expect("created");
        directory
    }

    #[test]
    fn a_folder_is_an_existing_absolute_git_repository_resolved() {
        let root = scratch("folders");
        let work = root.join("work");
        std::fs::create_dir_all(work.join(".git")).expect("created");
        let bare = root.join("bare.git");
        std::fs::create_dir_all(bare.join("objects")).expect("created");
        std::fs::write(bare.join("HEAD"), "ref: refs/heads/main\n").expect("written");
        let linked = root.join("linked");
        std::fs::create_dir_all(&linked).expect("created");
        std::fs::write(linked.join(".git"), "gitdir: /elsewhere\n").expect("written");
        let plain = root.join("plain");
        std::fs::create_dir_all(&plain).expect("created");

        let resolved = work.canonicalize().expect("exists");
        let dotted = root.join("work").join("..").join("work");
        assert_eq!(
            check_folder(dotted.to_str().expect("unicode")).expect("a repository"),
            resolved
        );
        assert!(check_folder(bare.to_str().expect("unicode")).is_ok());
        for refused in [
            "relative/work".to_owned(),
            root.join("missing").to_string_lossy().into_owned(),
            plain.to_string_lossy().into_owned(),
            linked.to_string_lossy().into_owned(),
            format!("{}\n", work.to_string_lossy()).replace('\n', "\u{7}"),
        ] {
            assert!(check_folder(&refused).is_err(), "{refused}");
        }
    }

    #[test]
    fn a_host_is_a_host_name_and_never_an_option() {
        assert_eq!(
            check_host(" Git.Example.org ").expect("a host"),
            "git.example.org"
        );
        assert_eq!(check_host("10.0.0.7").expect("an address"), "10.0.0.7");
        for refused in [
            "",
            "-oProxyCommand=touch",
            "git.example.org:22",
            "user@git.example.org",
            "git example.org",
            "a..b",
            "bad-.example.org",
        ] {
            assert!(check_host(refused).is_err(), "{refused}");
        }
        assert_eq!(
            check_hosts(&["a.example.org".to_owned(), "A.example.org".to_owned()]).expect("hosts"),
            ["a.example.org"]
        );
        assert!(check_hosts(&[]).is_err());
    }

    #[test]
    fn every_argument_is_one_element_and_no_secret_is_one() {
        let folder = PathBuf::from("/srv/with space/app");
        assert_eq!(
            repos_invocation(&CheckedRepo::Add(folder)).arguments,
            ["repos", "add", "/srv/with space/app", "--json"]
        );
        assert_eq!(
            repos_invocation(&CheckedRepo::Remove("app".to_owned())).arguments,
            ["repos", "remove", "app", "--json"]
        );
        let token = git_invocation(&CheckedGit::HttpsToken {
            hosts: vec!["git.example.org".to_owned()],
            username: Some("oauth2".to_owned()),
        });
        assert_eq!(
            token.arguments,
            [
                "git",
                "https-token",
                "--host=git.example.org",
                "--username=oauth2",
                "--json"
            ]
        );
        assert_eq!(token.words, "memini-server git https-token");
        assert_eq!(
            git_invocation(&CheckedGit::Remove(AccessKind::Ssh)).arguments,
            ["git", "remove", "ssh", "--json"]
        );
    }

    #[test]
    fn a_request_of_the_webview_is_checked_before_it_runs() {
        let request: GitRequest = serde_json::from_str(
            r#"{"action": "ssh_key", "hosts": ["git.example.org"], "source": {"from": "clipboard"}}"#,
        )
        .expect("known");
        let (checked, source) = CheckedGit::of(&request).expect("checked");
        assert_eq!(
            checked,
            CheckedGit::SshKey {
                hosts: vec!["git.example.org".to_owned()]
            }
        );
        assert_eq!(source, Some(SecretSource::Clipboard));
        let bad: GitRequest = serde_json::from_str(
            r#"{"action": "https_token", "hosts": ["-x"], "source": {"from": "clipboard"}}"#,
        )
        .expect("known");
        assert!(CheckedGit::of(&bad).is_err());
        let blank: GitRequest = serde_json::from_str(
            r#"{"action": "https_token", "hosts": ["a.example.org"], "username": " ", "source": {"from": "file", "path": "/t"}}"#,
        )
        .expect("known");
        assert_eq!(
            CheckedGit::of(&blank).expect("checked").0,
            CheckedGit::HttpsToken {
                hosts: vec!["a.example.org".to_owned()],
                username: None
            }
        );
        assert!(CheckedRepo::of(&RepoRequest::Remove {
            name: "../x".to_owned()
        })
        .is_err());
    }

    #[test]
    fn a_secret_is_checked_and_never_printed() {
        // Not a key: its markers around a word, split so that no secret scanner takes the source
        // for one.
        let fake = [
            "  -----BEGIN OPENSSH PRIVATE",
            " KEY-----\nabc\n-----END OPENSSH PRIVATE",
            " KEY-----  ",
        ]
        .concat();
        let key = ssh_key(&fake).expect("a key");
        assert!(key.expose().ends_with(b"KEY-----\n"));
        assert_eq!(format!("{key:?}"), "Secret([redacted])");
        assert!(ssh_key("ssh-ed25519 AAAA public").is_err());
        assert_eq!(
            https_token("synthetic-token\n").expect("a token").expose(),
            b"synthetic-token"
        );
        assert!(https_token("two words").is_err());
        assert!(https_token("").is_err());
    }

    #[test]
    fn the_answers_are_read_and_their_text_cleaned() {
        let repos = parse_repos(
            r#"{"schema": 1, "ok": true, "error": null, "warning": null,
                "repository": {"name": "app", "path": "/srv/app", "location": "/srv/local/app",
                               "branch": "main\u001b[31m", "present": true},
                "repositories": []}"#,
        )
        .expect("the contract");
        let repo = repos.repository.expect("added");
        assert_eq!(repo.location, "/srv/local/app");
        assert!(!repo.branch.expect("a branch").contains('\u{1b}'));
        let refused = parse_repos(r#"{"schema": 1, "ok": false, "error": "no folder"}"#)
            .expect("the contract");
        assert!(!refused.ok && refused.repositories.is_empty());

        let git = parse_git(
            r#"{"schema": 1, "ok": true, "error": null,
                "ssh": {"key": {"type": "ED25519", "fingerprint": "SHA256:k"},
                        "hosts": [{"host": "git.example.org",
                                   "keys": [{"type": "ED25519", "fingerprint": "SHA256:h"}]}]},
                "https": null}"#,
        )
        .expect("the contract");
        let ssh = git.ssh.expect("ssh access");
        assert_eq!(ssh.hosts[0].keys[0].fingerprint, "SHA256:h");
        assert_eq!(git.https, None);
        let serialized = serde_json::to_string(&ssh).expect("serializable");
        assert!(serialized.contains("\"type\":\"ED25519\""));
    }
}
