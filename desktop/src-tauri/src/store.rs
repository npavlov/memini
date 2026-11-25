//! The app's settings file: the saved connections, the active one, and the launcher's path.
//!
//! It holds no secret. A connection's token is in the operating system's keychain
//! ([`crate::keychain`]) under the connection's id; the file only remembers whether one was
//! stored, so that listing the connections never opens the keychain.
//!
//! Rules a connection is saved under:
//!
//! - The address is `http://` or `https://`, without a user name, password, query or fragment.
//! - A local connection is the server the launcher manages on this computer: its host is a
//!   loopback address, and there is at most one.
//! - A remote connection over plain `http://` to another computer needs `allow_http`: a token
//!   sent that way can be read on the network.
//! - A token belongs to the server it was stored for. Changing a connection's origin (scheme,
//!   host or port) drops its token, with one exception: the local connection keeps it when it
//!   moves between addresses on this computer, because the launcher's server can come back on
//!   another port.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use url::{Host, Url};

use crate::error::{AppError, AppResult, ErrorKind};

/// The version of the settings file this app writes and reads.
pub const SETTINGS_SCHEMA: u32 = 1;

/// Where a local server answers unless the launcher says otherwise.
pub const DEFAULT_LOCAL_URL: &str = "http://127.0.0.1:6364";

const MAX_NAME_LENGTH: usize = 80;
const MAX_URL_LENGTH: usize = 2000;
const SETTINGS_FILE: &str = "settings.json";
const INVALID_FILE: &str = "settings.invalid.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConnectionKind {
    /// The server the launcher manages on this computer.
    Local,
    /// A server reached by its address.
    Remote,
}

/// A saved connection, as it is stored and as the webview sees it. It holds no secret.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Connection {
    pub id: String,
    pub name: String,
    pub kind: ConnectionKind,
    /// The server's base address, without a trailing slash.
    pub url: String,
    #[serde(default)]
    pub allow_http: bool,
    /// Whether a token was stored in the keychain for this connection.
    #[serde(default)]
    pub has_token: bool,
}

/// What the user enters to create a connection (`id` is `None`) or to change one.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ConnectionInput {
    #[serde(default)]
    pub id: Option<String>,
    pub name: String,
    pub kind: ConnectionKind,
    pub url: String,
    #[serde(default)]
    pub allow_http: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Settings {
    #[serde(default)]
    pub schema: u32,
    /// A path to `memini-server` the user set; `None`: look for it on `PATH`.
    #[serde(default)]
    pub launcher_path: Option<String>,
    #[serde(default)]
    pub active_id: Option<String>,
    #[serde(default)]
    pub connections: Vec<Connection>,
}

/// What the webview gets of the settings' connections.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ConnectionsView {
    pub connections: Vec<Connection>,
    pub active_id: Option<String>,
}

/// The result of saving a connection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Saved {
    pub id: String,
    /// The connection's origin changed, so its token must be removed from the keychain.
    pub drop_token: bool,
}

impl Settings {
    pub fn view(&self) -> ConnectionsView {
        ConnectionsView {
            connections: self.connections.clone(),
            active_id: self.active_id.clone(),
        }
    }

    pub fn find(&self, id: &str) -> AppResult<&Connection> {
        self.connections
            .iter()
            .find(|connection| connection.id == id)
            .ok_or_else(no_such_connection)
    }

    fn find_mut(&mut self, id: &str) -> AppResult<&mut Connection> {
        self.connections
            .iter_mut()
            .find(|connection| connection.id == id)
            .ok_or_else(no_such_connection)
    }

    /// Creates or changes a connection. The first connection becomes the active one.
    pub fn save_connection(&mut self, input: &ConnectionInput) -> AppResult<Saved> {
        let name = check_name(&input.name)?;
        let url = check_url(&input.url, input.kind, input.allow_http)?;
        let stored_url = stored_form(&url);
        let other_local = self.connections.iter().any(|connection| {
            connection.kind == ConnectionKind::Local && Some(&connection.id) != input.id.as_ref()
        });
        if input.kind == ConnectionKind::Local && other_local {
            return Err(AppError::invalid(
                "There is already a local connection. Save a second server on this computer as a \
                 remote connection with its address.",
            ));
        }
        if let Some(id) = &input.id {
            let connection = self.find_mut(id)?;
            let stays_local =
                connection.kind == ConnectionKind::Local && input.kind == ConnectionKind::Local;
            let moved = match Url::parse(&connection.url) {
                Ok(old) if old.origin() == url.origin() => false,
                // Both ends are this computer: the same server on another port.
                Ok(old) => !(stays_local && is_loopback(&old) && is_loopback(&url)),
                Err(_) => true,
            };
            let drop_token = moved && connection.has_token;
            connection.name = name;
            connection.kind = input.kind;
            connection.url = stored_url;
            connection.allow_http = input.allow_http;
            if moved {
                connection.has_token = false;
            }
            return Ok(Saved {
                id: id.clone(),
                drop_token,
            });
        }
        let id = new_id(&self.connections);
        self.connections.push(Connection {
            id: id.clone(),
            name,
            kind: input.kind,
            url: stored_url,
            allow_http: input.allow_http,
            has_token: false,
        });
        if self.active_id.is_none() {
            self.active_id = Some(id.clone());
        }
        Ok(Saved {
            id,
            drop_token: false,
        })
    }

    /// Removes a connection. If it was the active one, the first that is left becomes active.
    pub fn remove_connection(&mut self, id: &str) -> AppResult<Connection> {
        let at = self
            .connections
            .iter()
            .position(|connection| connection.id == id)
            .ok_or_else(no_such_connection)?;
        let removed = self.connections.remove(at);
        if self.active_id.as_deref() == Some(id) {
            self.active_id = self
                .connections
                .first()
                .map(|connection| connection.id.clone());
        }
        Ok(removed)
    }

    pub fn activate(&mut self, id: &str) -> AppResult<()> {
        self.find(id)?;
        self.active_id = Some(id.to_owned());
        Ok(())
    }

    pub fn set_has_token(&mut self, id: &str, has_token: bool) -> AppResult<()> {
        self.find_mut(id)?.has_token = has_token;
        Ok(())
    }
}

fn no_such_connection() -> AppError {
    AppError::new(ErrorKind::NotFound, "There is no such saved connection.")
}

fn check_name(name: &str) -> AppResult<String> {
    let name = name.trim();
    if name.is_empty() {
        return Err(AppError::invalid("Give the connection a name."));
    }
    if name.chars().count() > MAX_NAME_LENGTH || name.chars().any(char::is_control) {
        return Err(AppError::invalid(format!(
            "A connection's name is at most {MAX_NAME_LENGTH} printable characters."
        )));
    }
    Ok(name.to_owned())
}

/// The address of a connection, checked against the rules of the module.
pub fn check_url(text: &str, kind: ConnectionKind, allow_http: bool) -> AppResult<Url> {
    const EXPECTED: &str = "Enter the server's address, such as https://memini.example.org.";
    let text = text.trim();
    if text.is_empty() || text.len() > MAX_URL_LENGTH || text.chars().any(char::is_whitespace) {
        return Err(AppError::invalid(EXPECTED));
    }
    let url = Url::parse(text).map_err(|_| AppError::invalid(EXPECTED))?;
    if !matches!(url.scheme(), "http" | "https") || url.host().is_none() {
        return Err(AppError::invalid(EXPECTED));
    }
    if !url.username().is_empty() || url.password().is_some() {
        // Not echoed: the part before the host may be a password.
        return Err(AppError::invalid(
            "The address holds a user name or a password. The token is the credential; remove \
             them from the address.",
        ));
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err(AppError::invalid(
            "The address must not have a query or a fragment.",
        ));
    }
    let loopback = is_loopback(&url);
    match kind {
        ConnectionKind::Local if !loopback => Err(AppError::invalid(
            "A local connection is a server on this computer: its address is 127.0.0.1, \
             localhost or ::1. Save another computer's server as a remote connection.",
        )),
        ConnectionKind::Remote if url.scheme() == "http" && !loopback && !allow_http => {
            Err(AppError::invalid(
                "This address uses plain HTTP, so the token could be read on the network. Use \
                 https://, or allow plain HTTP for this connection.",
            ))
        }
        _ => Ok(url),
    }
}

/// Whether the address names this computer.
pub fn is_loopback(url: &Url) -> bool {
    match url.host() {
        Some(Host::Ipv4(address)) => address.is_loopback(),
        Some(Host::Ipv6(address)) => address.is_loopback(),
        Some(Host::Domain(name)) => name.eq_ignore_ascii_case("localhost"),
        None => false,
    }
}

fn stored_form(url: &Url) -> String {
    url.as_str().trim_end_matches('/').to_owned()
}

/// An id no connection has yet. It names the keychain entry; it is not a secret.
fn new_id(existing: &[Connection]) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    (0u32..)
        .map(|attempt| format!("c{nanos:x}-{attempt:x}"))
        .find(|id| existing.iter().all(|connection| &connection.id != id))
        .unwrap_or_else(|| format!("c{nanos:x}"))
}

/// The settings and the file they are kept in.
#[derive(Debug)]
pub struct Store {
    directory: PathBuf,
    pub settings: Settings,
}

impl Store {
    /// Reads the settings from `directory`. A missing file gives empty settings. A file that
    /// cannot be understood (not JSON, or written by a newer app) is moved aside, not deleted,
    /// and the app starts with empty settings.
    pub fn open(directory: PathBuf) -> Self {
        let file = directory.join(SETTINGS_FILE);
        let settings = match fs::read(&file) {
            Ok(bytes) => match parse_settings(&bytes) {
                Some(settings) => settings,
                None => {
                    let _ = fs::rename(&file, directory.join(INVALID_FILE));
                    Settings::default()
                }
            },
            Err(_) => Settings::default(),
        };
        Self {
            directory,
            settings,
        }
    }

    /// Writes the settings: to a temporary file first, then over the old file.
    pub fn save(&mut self) -> AppResult<()> {
        self.settings.schema = SETTINGS_SCHEMA;
        let text = serde_json::to_string_pretty(&self.settings)
            .map_err(|_| storage_error("The settings could not be written."))?;
        write_file(&self.directory, &text).map_err(|error| {
            // The kind only: the text of an input/output error can hold a path.
            storage_error(format!(
                "The settings could not be saved ({:?}).",
                error.kind()
            ))
        })
    }
}

fn parse_settings(bytes: &[u8]) -> Option<Settings> {
    let settings: Settings = serde_json::from_slice(bytes).ok()?;
    (settings.schema <= SETTINGS_SCHEMA).then_some(settings)
}

fn write_file(directory: &Path, text: &str) -> std::io::Result<()> {
    fs::create_dir_all(directory)?;
    let temporary = directory.join(format!("{SETTINGS_FILE}.tmp"));
    fs::write(&temporary, text)?;
    let renamed = fs::rename(&temporary, directory.join(SETTINGS_FILE));
    if renamed.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    renamed
}

fn storage_error(message: impl AsRef<str>) -> AppError {
    AppError::new(ErrorKind::Storage, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(name: &str, kind: ConnectionKind, url: &str) -> ConnectionInput {
        ConnectionInput {
            id: None,
            name: name.to_owned(),
            kind,
            url: url.to_owned(),
            allow_http: false,
        }
    }

    fn message(result: AppResult<Url>) -> String {
        result.expect_err("refused").message
    }

    #[test]
    fn an_address_is_http_or_https_without_credentials_query_or_fragment() {
        let remote = ConnectionKind::Remote;
        assert!(check_url("https://memini.example.org", remote, false).is_ok());
        assert!(check_url(" https://memini.example.org/prefix/ ", remote, false).is_ok());
        assert!(check_url("ftp://memini.example.org", remote, false).is_err());
        assert!(check_url("memini.example.org", remote, false).is_err());
        assert!(check_url("https://memini.example.org/?a=b", remote, false).is_err());
        assert!(check_url("https://memini.example.org/#top", remote, false).is_err());
        assert!(check_url("https://memini.example .org", remote, false).is_err());
        assert!(check_url("", remote, false).is_err());
    }

    #[test]
    fn credentials_in_an_address_are_refused_and_never_echoed() {
        let text = message(check_url(
            "https://operator:hunter2@memini.example.org",
            ConnectionKind::Remote,
            false,
        ));
        assert!(!text.contains("hunter2"));
        assert!(!text.contains("operator"));
    }

    #[test]
    fn plain_http_to_another_computer_needs_the_opt_in() {
        let remote = ConnectionKind::Remote;
        assert!(check_url("http://memini.example.org:6364", remote, false).is_err());
        assert!(check_url("http://memini.example.org:6364", remote, true).is_ok());
        assert!(check_url("http://127.0.0.1:6364", remote, false).is_ok());
        assert!(check_url("http://localhost:7000", remote, false).is_ok());
        assert!(check_url("http://[::1]:6364", remote, false).is_ok());
    }

    #[test]
    fn a_local_connection_is_on_this_computer() {
        let local = ConnectionKind::Local;
        assert!(check_url(DEFAULT_LOCAL_URL, local, false).is_ok());
        assert!(check_url("http://memini.example.org:6364", local, true).is_err());
        assert!(check_url("https://127.0.0.1.example.org", local, false).is_err());
        assert!(check_url("http://127.0.0.1@memini.example.org", local, false).is_err());
    }

    #[test]
    fn the_first_connection_becomes_active_and_ids_differ() {
        let mut settings = Settings::default();
        let first = settings
            .save_connection(&input(
                "Harbour",
                ConnectionKind::Remote,
                "https://a.example.org/",
            ))
            .expect("saved");
        let second = settings
            .save_connection(&input(
                "Quay",
                ConnectionKind::Remote,
                "https://b.example.org",
            ))
            .expect("saved");
        assert_ne!(first.id, second.id);
        assert_eq!(settings.active_id.as_deref(), Some(first.id.as_str()));
        assert_eq!(settings.connections[0].url, "https://a.example.org");
        assert!(!settings.connections[0].has_token);
    }

    #[test]
    fn there_is_one_local_connection_at_most() {
        let mut settings = Settings::default();
        let local = settings
            .save_connection(&input(
                "This computer",
                ConnectionKind::Local,
                DEFAULT_LOCAL_URL,
            ))
            .expect("saved");
        let again = input("Another", ConnectionKind::Local, "http://localhost:7000");
        assert!(settings.save_connection(&again).is_err());
        // Changing the one that exists is not a second one.
        let mut change = input("Renamed", ConnectionKind::Local, DEFAULT_LOCAL_URL);
        change.id = Some(local.id);
        assert!(settings.save_connection(&change).is_ok());
    }

    #[test]
    fn changing_the_origin_drops_the_token_and_a_rename_keeps_it() {
        let mut settings = Settings::default();
        let saved = settings
            .save_connection(&input(
                "Harbour",
                ConnectionKind::Remote,
                "https://a.example.org",
            ))
            .expect("saved");
        settings.set_has_token(&saved.id, true).expect("known");

        let mut rename = input(
            "Harbour team",
            ConnectionKind::Remote,
            "https://a.example.org/",
        );
        rename.id = Some(saved.id.clone());
        assert!(!settings.save_connection(&rename).expect("saved").drop_token);
        assert!(settings.connections[0].has_token);

        let mut moved = input(
            "Harbour team",
            ConnectionKind::Remote,
            "https://b.example.org",
        );
        moved.id = Some(saved.id.clone());
        assert!(settings.save_connection(&moved).expect("saved").drop_token);
        assert!(!settings.connections[0].has_token);
    }

    #[test]
    fn the_local_connection_keeps_its_token_on_another_port_of_this_computer() {
        let mut settings = Settings::default();
        let saved = settings
            .save_connection(&input(
                "This computer",
                ConnectionKind::Local,
                DEFAULT_LOCAL_URL,
            ))
            .expect("saved");
        settings.set_has_token(&saved.id, true).expect("known");

        let mut other_port = input(
            "This computer",
            ConnectionKind::Local,
            "http://127.0.0.1:7001",
        );
        other_port.id = Some(saved.id.clone());
        let result = settings.save_connection(&other_port).expect("saved");
        assert!(!result.drop_token);
        assert!(settings.connections[0].has_token);
        assert_eq!(settings.connections[0].url, "http://127.0.0.1:7001");

        // Turned into a remote connection to another computer, it loses the token.
        let mut elsewhere = input(
            "This computer",
            ConnectionKind::Remote,
            "https://a.example.org",
        );
        elsewhere.id = Some(saved.id.clone());
        assert!(
            settings
                .save_connection(&elsewhere)
                .expect("saved")
                .drop_token
        );
        assert!(!settings.connections[0].has_token);
    }

    #[test]
    fn a_remote_connection_on_this_computer_loses_its_token_on_another_port() {
        let mut settings = Settings::default();
        let saved = settings
            .save_connection(&input(
                "Second server",
                ConnectionKind::Remote,
                "http://127.0.0.1:7000",
            ))
            .expect("saved");
        settings.set_has_token(&saved.id, true).expect("known");

        let mut other_port = input(
            "Second server",
            ConnectionKind::Remote,
            "http://127.0.0.1:7001",
        );
        other_port.id = Some(saved.id.clone());
        assert!(
            settings
                .save_connection(&other_port)
                .expect("saved")
                .drop_token
        );
    }

    #[test]
    fn removing_the_active_connection_activates_another() {
        let mut settings = Settings::default();
        let first = settings
            .save_connection(&input(
                "Harbour",
                ConnectionKind::Remote,
                "https://a.example.org",
            ))
            .expect("saved");
        let second = settings
            .save_connection(&input(
                "Quay",
                ConnectionKind::Remote,
                "https://b.example.org",
            ))
            .expect("saved");
        settings.remove_connection(&first.id).expect("removed");
        assert_eq!(settings.active_id.as_deref(), Some(second.id.as_str()));
        settings.remove_connection(&second.id).expect("removed");
        assert_eq!(settings.active_id, None);
        assert_eq!(
            settings
                .remove_connection(&second.id)
                .expect_err("gone")
                .kind,
            ErrorKind::NotFound
        );
    }

    #[test]
    fn settings_survive_a_save_and_a_newer_schema_is_not_read() {
        let directory = std::env::temp_dir().join(format!(
            "memini-desktop-test-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = fs::remove_dir_all(&directory);
        let mut store = Store::open(directory.clone());
        assert_eq!(store.settings, Settings::default());
        store
            .settings
            .save_connection(&input(
                "Harbour",
                ConnectionKind::Remote,
                "https://a.example.org",
            ))
            .expect("saved");
        store.save().expect("written");
        let reopened = Store::open(directory.clone());
        assert_eq!(reopened.settings, store.settings);
        assert_eq!(reopened.settings.schema, SETTINGS_SCHEMA);

        fs::write(
            directory.join(SETTINGS_FILE),
            br#"{"schema": 2, "connections": []}"#,
        )
        .expect("written");
        let newer = Store::open(directory.clone());
        assert_eq!(newer.settings, Settings::default());
        assert!(directory.join(INVALID_FILE).is_file());
        let _ = fs::remove_dir_all(&directory);
    }
}
