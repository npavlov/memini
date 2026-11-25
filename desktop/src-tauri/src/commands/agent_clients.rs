//! Connect and Disconnect for the agent clients on this computer (scenario S16).
//!
//! Each runs on one click for one client. Connect makes a read token for that client on the
//! server, writes the `memini` entry into the client's own configuration and keeps the token
//! where the client can read it: in a file only the user can read, which the client's
//! environment variable is loaded from, or, when the user asks for it or the client has no other
//! way, in the client's file. Disconnect removes the entry and revokes that token. The token
//! never goes to the webview, and the settings keep its id only.

use std::path::PathBuf;
use std::sync::Arc;

use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::State;

use super::{authorized, blocking, is_uuid, AppState, BusyGuard, Inner};
use crate::clients::{self, Client, ClientView, Pass, Places, Planned, Request};
use crate::error::{AppError, AppResult, ErrorKind};
use crate::secret::Token;
use crate::store::ClientLink;

/// What the user asks Connect for.
#[derive(Debug, Clone, Deserialize)]
pub struct ConnectRequest {
    pub client: String,
    /// The saved connection whose server the client is connected to.
    pub connection: String,
    /// The workspace the client's token may read; `None`: every workspace.
    #[serde(default)]
    pub workspace: Option<String>,
    /// Write the token itself into the client's file. The user confirmed it.
    #[serde(default)]
    pub literal: bool,
    /// The layout of the client's file, for a client that has several and a file that does not
    /// tell.
    #[serde(default)]
    pub variant: Option<String>,
}

/// What became of the token the app made for a client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum Revoked {
    Done,
    /// The app has no token on record for the client: the entry was not made by it.
    Nothing,
    /// The token is still valid.
    Failed {
        message: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Disconnected {
    pub client: ClientView,
    /// Whether the file had the entry.
    pub removed: bool,
    pub revoked: Revoked,
}

fn places() -> AppResult<Places> {
    Places::from_environment().ok_or_else(|| {
        AppError::new(
            ErrorKind::Storage,
            "The home directory of this user is not known.",
        )
    })
}

fn busy(inner: &Inner) -> AppResult<BusyGuard<'_>> {
    BusyGuard::begin(&inner.clients_busy).map_err(|_| {
        AppError::new(
            ErrorKind::Busy,
            "Another client is still being changed. Wait for it to finish.",
        )
    })
}

fn link_of(inner: &Inner, client: &Client) -> AppResult<Option<ClientLink>> {
    Ok(inner.lock()?.settings.clients.get(client.id).cloned())
}

fn view_of(inner: &Inner, client: &'static Client, places: &Places) -> AppResult<ClientView> {
    let has_token = link_of(inner, client)?.is_some();
    Ok(clients::view(client, places, has_token))
}

fn list(inner: &Inner, places: &Places) -> AppResult<Vec<ClientView>> {
    clients::installed(places)
        .into_iter()
        .map(|client| view_of(inner, client, places))
        .collect()
}

fn remember(inner: &Inner, client: &Client, link: Option<ClientLink>) -> AppResult<()> {
    inner.update(|settings| {
        match link {
            Some(link) => settings.clients.insert(client.id.to_owned(), link),
            None => settings.clients.remove(client.id),
        };
        Ok(())
    })
}

/// The token made for a client, and its id.
struct Made {
    id: String,
    token: Token,
}

async fn create_token(
    inner: &Arc<Inner>,
    connection: &str,
    client: &Client,
    workspace: Option<&str>,
) -> AppResult<Made> {
    let (target, admin) = authorized(inner, connection).await?;
    let body = json!({
        "name": format!("{} (Memini desktop)", client.name),
        "scopes": ["read"],
        "workspace": workspace,
    });
    let answer = inner
        .http
        .call(
            &target,
            Some(&admin),
            Method::POST,
            &["v1", "tokens"],
            Some(&body),
        )
        .await?;
    let id = answer
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| is_uuid(id));
    let token = answer
        .get("token")
        .and_then(Value::as_str)
        .and_then(Token::parse);
    match (id, token) {
        (Some(id), Some(token)) => Ok(Made {
            id: id.to_owned(),
            token,
        }),
        _ => Err(AppError::new(
            ErrorKind::Unexpected,
            "The server answered without the new token. Check the Tokens screen for one that \
             nothing uses.",
        )),
    }
}

/// Revokes the token of a link. A token the server no longer has counts as revoked.
async fn revoke(inner: &Arc<Inner>, link: &ClientLink) -> AppResult<()> {
    let (target, admin) = authorized(inner, &link.connection_id).await?;
    let segments = ["v1", "tokens", link.token_id.as_str()];
    let answer = inner
        .http
        .call(&target, Some(&admin), Method::DELETE, &segments, None)
        .await;
    match answer {
        Err(error) if error.status != Some(404) => Err(error),
        _ => Ok(()),
    }
}

/// What Connect writes, once the file was read.
#[derive(Debug, Clone)]
struct Plan {
    client: &'static Client,
    places: Places,
    file: PathBuf,
    /// The file as it is now; `None`: there is none.
    text: Option<String>,
    url: String,
    literal: bool,
    variant: Option<String>,
}

impl Plan {
    fn planned(&self, pass: Pass<'_>) -> AppResult<Planned> {
        let request = Request {
            url: &self.url,
            pass,
            variant: self.variant.as_deref(),
        };
        clients::connect_text(self.client, self.text.as_deref(), &request)
    }

    /// Whether Connect has something to write. Fails where the write would fail, before a
    /// token is made.
    fn needed(&self) -> AppResult<bool> {
        let pass = if self.literal {
            Pass::Literal("-")
        } else {
            Pass::Reference
        };
        Ok(self.planned(pass)? != Planned::Unchanged)
    }

    /// Writes the entry, and the token file when the entry refers to the variable.
    fn write(&self, token: &Token) -> AppResult<()> {
        let pass = if self.literal {
            Pass::Literal(token.expose())
        } else {
            Pass::Reference
        };
        let Planned::Change(after) = self.planned(pass)? else {
            return Ok(());
        };
        let token_file = self.places.token_file(self.client);
        if !self.literal {
            clients::save_token(&token_file, token)?;
        }
        let written = clients::write_file(&self.file, &after, self.literal);
        if written.is_err() {
            let _ = std::fs::remove_file(&token_file);
        }
        written
    }
}

async fn connect(
    inner: &Arc<Inner>,
    places: &Places,
    request: ConnectRequest,
) -> AppResult<ClientView> {
    let client = clients::find(&request.client)?;
    let _busy = busy(inner)?;
    let target = inner.target(&request.connection)?;
    let file = client.file(places);
    let read = file.clone();
    let plan = Plan {
        client,
        places: places.clone(),
        text: blocking(move || clients::read_file(&read)).await?,
        file,
        url: target.endpoint(&["mcp"])?.to_string(),
        literal: request.literal,
        variant: request.variant,
    };
    if plan.needed()? {
        let workspace = request.workspace.as_deref();
        let made = create_token(inner, &request.connection, client, workspace).await?;
        let link = ClientLink {
            connection_id: request.connection.clone(),
            token_id: made.id,
        };
        let stored = store(inner, plan, link.clone(), made.token).await;
        if let Err(error) = stored {
            // Nothing uses the token: it must not stay valid.
            let _ = revoke(inner, &link).await;
            let _ = remember(inner, client, None);
            return Err(error);
        }
    }
    view_of(inner, client, places)
}

/// Keeps the id of the token, then writes the client's file.
async fn store(inner: &Arc<Inner>, plan: Plan, link: ClientLink, token: Token) -> AppResult<()> {
    let shared = Arc::clone(inner);
    blocking(move || {
        remember(&shared, plan.client, Some(link))?;
        plan.write(&token)
    })
    .await
}

/// Takes the entry out of the client's file, and removes the token file. Answers whether the
/// file had the entry.
fn remove_entry(client: &Client, places: &Places) -> AppResult<bool> {
    let file = client.file(places);
    let after = match clients::read_file(&file)? {
        Some(text) => clients::disconnect_text(client, &text)?,
        None => None,
    };
    if let Some(after) = &after {
        clients::write_file(&file, after, false)?;
    }
    let _ = std::fs::remove_file(places.token_file(client));
    Ok(after.is_some())
}

async fn revoke_for(inner: &Arc<Inner>, client: &Client) -> AppResult<Revoked> {
    let Some(link) = link_of(inner, client)? else {
        return Ok(Revoked::Nothing);
    };
    match revoke(inner, &link).await {
        Ok(()) => {
            remember(inner, client, None)?;
            Ok(Revoked::Done)
        }
        Err(error) if error.kind == ErrorKind::NotFound => {
            remember(inner, client, None)?;
            Ok(Revoked::Failed {
                message: "The connection this token was made with is no longer saved, so the \
                          app cannot revoke it. Revoke it on its server."
                    .to_owned(),
            })
        }
        Err(error) => Ok(Revoked::Failed {
            message: error.message,
        }),
    }
}

async fn disconnect(inner: &Arc<Inner>, places: &Places, id: &str) -> AppResult<Disconnected> {
    let client = clients::find(id)?;
    let _busy = busy(inner)?;
    let owned = places.clone();
    let removed = blocking(move || remove_entry(client, &owned)).await?;
    let revoked = revoke_for(inner, client).await?;
    Ok(Disconnected {
        client: view_of(inner, client, places)?,
        removed,
        revoked,
    })
}

/// The agent clients installed on this computer, each as its own configuration file shows it.
#[tauri::command]
pub async fn clients_list(state: State<'_, AppState>) -> AppResult<Vec<ClientView>> {
    let inner = state.shared();
    blocking(move || list(&inner, &places()?)).await
}

/// Connects one client: a read token made for it, and the `memini` entry in its file. An entry
/// that is there already is left as it is, and no token is made.
#[tauri::command]
pub async fn client_connect(
    state: State<'_, AppState>,
    request: ConnectRequest,
) -> AppResult<ClientView> {
    connect(&state.shared(), &places()?, request).await
}

/// Disconnects one client: removes the `memini` entry from its file and revokes the token the
/// app made for it.
#[tauri::command]
pub async fn client_disconnect(
    state: State<'_, AppState>,
    client: String,
) -> AppResult<Disconnected> {
    disconnect(&state.shared(), &places()?, &client).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clients::{backup_of, ClientState, TokenForm};
    use crate::keychain::MemoryStore;
    use crate::secret::fake_token;
    use crate::store::{ConnectionInput, ConnectionKind, Store};
    use crate::stub::Stub;

    const TOKEN_ID: &str = "0f3a6c1e-7b2d-4e58-9a41-c5d2e8f70b13";
    const CURSOR: &str = include_str!("../../fixtures/clients/cursor.json");
    const ZED: &str = include_str!("../../fixtures/clients/zed.jsonc");

    /// A server that answers the two token routes: with a new token when one is asked for.
    fn token_server(token: String) -> Stub {
        Stub::start(move |request| {
            let made = request.line == "POST /v1/tokens";
            let secret = if made { token.as_str() } else { "" };
            (
                200,
                format!("{{\"id\":\"{TOKEN_ID}\",\"token\":\"{secret}\"}}"),
            )
        })
    }

    struct Setup {
        inner: Arc<Inner>,
        places: Places,
        directory: PathBuf,
        server: Stub,
        connection: String,
        token: String,
    }

    impl Setup {
        /// A home directory with one client's configuration in it, and a server to connect to.
        fn new(name: &str, file: &[&str], text: &str) -> Self {
            let directory = std::env::temp_dir().join(format!(
                "memini-desktop-clients-{}-{name}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&directory);
            let home = directory.join("home");
            let path = file.iter().fold(home.clone(), |path, part| path.join(part));
            std::fs::create_dir_all(path.parent().expect("a directory")).expect("made");
            std::fs::write(&path, text).expect("written");
            let state = AppState::new(
                Store::open(directory.join("settings")),
                Box::new(MemoryStore::default()),
            )
            .expect("state");
            let inner = state.shared();
            let token = fake_token('c');
            let server = token_server(token.clone());
            let connection = Self::connection(&inner, &server.address);
            Self {
                inner,
                places: Places {
                    config: home.join("config"),
                    home,
                    windows: false,
                },
                directory,
                server,
                connection,
                token,
            }
        }

        fn connection(inner: &Inner, address: &str) -> String {
            let view = inner
                .save_connection(&ConnectionInput {
                    id: None,
                    name: "Local".to_owned(),
                    kind: ConnectionKind::Local,
                    url: address.to_owned(),
                    allow_http: false,
                })
                .expect("saved");
            let id = view.connections.last().expect("one").id.clone();
            inner
                .store_token(&id, &fake_token('a'), "test")
                .expect("stored");
            id
        }

        fn request(&self, client: &str, literal: bool) -> ConnectRequest {
            ConnectRequest {
                client: client.to_owned(),
                connection: self.connection.clone(),
                workspace: Some("harbor".to_owned()),
                literal,
                variant: None,
            }
        }

        fn connect(&self, client: &str, literal: bool) -> AppResult<ClientView> {
            let request = self.request(client, literal);
            tauri::async_runtime::block_on(connect(&self.inner, &self.places, request))
        }

        fn disconnect(&self, client: &str) -> AppResult<Disconnected> {
            tauri::async_runtime::block_on(disconnect(&self.inner, &self.places, client))
        }

        fn file(&self, client: &str) -> PathBuf {
            clients::find(client).expect("known").file(&self.places)
        }

        fn text(&self, client: &str) -> String {
            std::fs::read_to_string(self.file(client)).expect("read")
        }

        fn settings(&self) -> String {
            std::fs::read_to_string(self.directory.join("settings").join("settings.json"))
                .expect("read")
        }
    }

    impl Drop for Setup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }

    #[cfg(unix)]
    fn mode(path: &std::path::Path) -> u32 {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(path).expect("there").permissions().mode() & 0o777
    }

    #[test]
    fn connect_writes_the_entry_and_keeps_the_token_in_a_file_of_the_user() {
        let setup = Setup::new("connect", &[".cursor", "mcp.json"], CURSOR);
        let cursor = clients::find("cursor").expect("known");

        let view = setup.connect("cursor", false).expect("connected");

        let token_file = setup.places.token_file(cursor);
        assert_eq!(
            view.state,
            ClientState::Connected {
                url: Some(format!("{}/mcp", setup.server.address)),
                token: TokenForm::Reference
            }
        );
        assert_eq!(
            std::fs::read_to_string(&token_file).expect("read"),
            format!("{}\n", setup.token)
        );
        #[cfg(unix)]
        assert_eq!(mode(&token_file), 0o600);
    }

    #[test]
    fn connect_asks_the_server_for_one_read_token_named_for_the_client() {
        let setup = Setup::new("token", &[".cursor", "mcp.json"], CURSOR);

        setup.connect("cursor", false).expect("connected");
        setup.connect("cursor", false).expect("connected again");

        assert_eq!(
            setup.server.asked(),
            vec![
                "POST /v1/tokens {\"name\":\"Cursor (Memini desktop)\",\"scopes\":[\"read\"],\"workspace\":\"harbor\"}"
            ]
        );
    }

    #[test]
    fn the_file_is_copied_once_before_the_first_change() {
        let setup = Setup::new("backup", &[".cursor", "mcp.json"], CURSOR);

        setup.connect("cursor", false).expect("connected");
        setup.disconnect("cursor").expect("disconnected");
        setup.connect("cursor", false).expect("connected");

        let backup = backup_of(&setup.file("cursor"));
        assert_eq!(std::fs::read_to_string(backup).expect("read"), CURSOR);
    }

    #[test]
    fn the_token_reaches_neither_the_webview_nor_the_settings_nor_the_clients_file() {
        let setup = Setup::new("secret", &[".cursor", "mcp.json"], CURSOR);

        let view = setup.connect("cursor", false).expect("connected");

        let shown = serde_json::to_string(&view).expect("json");
        let kept = [shown, setup.settings(), setup.text("cursor")].concat();
        assert!(!kept.contains(&setup.token) && kept.contains(TOKEN_ID));
    }

    #[test]
    fn disconnect_restores_the_file_removes_the_token_file_and_revokes_the_token() {
        let setup = Setup::new("disconnect", &[".cursor", "mcp.json"], CURSOR);
        let cursor = clients::find("cursor").expect("known");
        setup.connect("cursor", false).expect("connected");

        let outcome = setup.disconnect("cursor").expect("disconnected");

        assert_eq!(
            (outcome.removed, outcome.revoked, outcome.client.has_token),
            (true, Revoked::Done, false)
        );
        assert_eq!(setup.text("cursor"), CURSOR);
        assert!(!setup.places.token_file(cursor).exists());
        assert_eq!(
            setup.server.asked().last().map(String::as_str),
            Some(format!("DELETE /v1/tokens/{TOKEN_ID} ").as_str())
        );
    }

    #[test]
    fn a_malformed_file_is_refused_before_a_token_is_made() {
        let broken = format!("{CURSOR}}}");
        let setup = Setup::new("malformed", &[".cursor", "mcp.json"], &broken);

        let connected = setup.connect("cursor", false);
        let disconnected = setup.disconnect("cursor");

        assert!(connected.is_err() && disconnected.is_err());
        assert_eq!(setup.text("cursor"), broken);
        assert!(!backup_of(&setup.file("cursor")).exists());
        assert_eq!(setup.server.asked(), Vec::<String>::new());
    }

    #[test]
    fn a_client_without_a_variable_gets_the_token_in_its_file_which_only_the_user_reads() {
        let setup = Setup::new("literal", &[".config", "zed", "settings.json"], ZED);
        let zed = clients::find("zed").expect("known");

        let view = setup.connect("zed", true).expect("connected");

        let shown = serde_json::to_string(&view).expect("json");
        assert!(setup.text("zed").contains(&setup.token) && !shown.contains(&setup.token));
        assert!(!setup.places.token_file(zed).exists());
        #[cfg(unix)]
        assert_eq!(mode(&setup.file("zed")), 0o600);
    }

    #[test]
    fn a_client_without_a_variable_is_not_connected_without_the_users_word() {
        let setup = Setup::new("refused", &[".config", "zed", "settings.json"], ZED);

        let answer = setup.connect("zed", false);

        assert!(answer.is_err());
        assert_eq!(setup.text("zed"), ZED);
        assert_eq!(setup.server.asked(), Vec::<String>::new());
    }

    #[test]
    fn disconnect_of_an_entry_made_by_hand_revokes_nothing() {
        let by_hand = CURSOR.replace("tide-tables", "memini");
        let setup = Setup::new("by-hand", &[".cursor", "mcp.json"], &by_hand);

        let outcome = setup.disconnect("cursor").expect("disconnected");

        assert_eq!((outcome.removed, outcome.revoked), (true, Revoked::Nothing));
        assert_eq!(setup.server.asked(), Vec::<String>::new());
    }

    #[test]
    fn a_token_that_could_not_be_revoked_stays_on_record() {
        let setup = Setup::new("unrevoked", &[".cursor", "mcp.json"], CURSOR);
        setup.connect("cursor", false).expect("connected");
        setup.inner.clear_token(&setup.connection).expect("cleared");

        let outcome = setup.disconnect("cursor").expect("disconnected");

        assert!(matches!(outcome.revoked, Revoked::Failed { .. }));
        assert!(outcome.removed && outcome.client.has_token);
    }

    #[test]
    fn only_installed_clients_are_listed() {
        let setup = Setup::new("list", &[".cursor", "mcp.json"], CURSOR);

        let listed = list(&setup.inner, &setup.places).expect("listed");

        let ids: Vec<&str> = listed.iter().map(|client| client.id).collect();
        assert_eq!(ids, vec!["cursor"]);
    }
}
