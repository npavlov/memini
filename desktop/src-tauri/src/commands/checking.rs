//! The test of a connection, step by step: whether the address answers, what version the server
//! reports, whether a token is stored, whether the server accepts it, and what the token may do
//! when the API tells. A step that fails is reported with the steps before it, never as one
//! error for the whole: a connection without a token still shows that its server answers.

use std::sync::Arc;

use reqwest::Method;
use serde::Serialize;
use serde_json::Value;
use tauri::State;

use super::{blocking, AppState, Inner, Scope};
use crate::api::{failure, Target};
use crate::error::{clean, AppError, AppResult};
use crate::secret::{Token, TOKEN_PREFIX};

/// What a test of a connection found.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ConnectionCheck {
    /// Why the address did not answer; `None`: it answered.
    pub unreachable: Option<AppError>,
    /// The version the server reports; `None` when the address answers without one, which a
    /// Memini server does not.
    pub version: Option<String>,
    /// The server's readiness; `None` when it did not say.
    pub ready: Option<bool>,
    pub token: TokenCheck,
    /// The scope the token lacks, when the server refused the test's read for that.
    pub lacking: Option<Scope>,
    /// The scopes of the token, when the API tells them: it does to a token with `admin`.
    pub scopes: Option<Vec<Scope>>,
    /// How many workspaces the token may read, when it was accepted.
    pub workspaces: Option<usize>,
    /// The server's words when the token was not accepted.
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenCheck {
    /// No token is stored for the connection.
    Missing,
    /// The server accepted the token for reading.
    Accepted,
    /// The server does not know the token, or it is revoked or expired.
    Refused,
    /// The token is valid but lacks a scope the read needs.
    LacksScope,
    /// The server answered the read in a way that says nothing about the token.
    Unknown,
    /// A token is stored, but the server did not answer, so it was not tried.
    NotChecked,
}

/// Why the address does not answer its liveness route; `None` when it answers. Any answer of a
/// server counts, also one that is not Memini's: the version tells that apart.
async fn reach(inner: &Inner, target: &Target) -> Option<AppError> {
    let answer = inner
        .http
        .exchange(target, None, Method::GET, &["healthz"], None)
        .await;
    match answer {
        Err(error) => Some(error),
        Ok(raw) if (502..=504).contains(&raw.status) => Some(failure(&raw, &target.place())),
        Ok(_) => None,
    }
}

async fn version_of(inner: &Inner, target: &Target) -> Option<String> {
    let answer = inner
        .http
        .call(target, None, Method::GET, &["version"], None)
        .await
        .ok()?;
    answer
        .get("version")
        .and_then(Value::as_str)
        .map(clean)
        .filter(|version| !version.is_empty())
}

/// Readiness answers 503 with the same report when a check fails: the body is read either way.
async fn readiness_of(inner: &Inner, target: &Target) -> Option<bool> {
    let raw = inner
        .http
        .exchange(target, None, Method::GET, &["readyz"], None)
        .await
        .ok()?;
    let report: Value = serde_json::from_slice(&raw.body).ok()?;
    let status = report.get("status").and_then(Value::as_str)?;
    Some(status == "ready")
}

/// What a refused read says about the token. The read is of the status, which needs the `read`
/// scope: the API's code for a missing scope therefore names that one.
fn refusal_of(error: &AppError) -> (TokenCheck, Option<Scope>) {
    match (error.status, error.code.as_deref()) {
        (Some(401), _) => (TokenCheck::Refused, None),
        (Some(403), Some("insufficient_scope")) => (TokenCheck::LacksScope, Some(Scope::Read)),
        (Some(403), _) => (TokenCheck::LacksScope, None),
        _ => (TokenCheck::Unknown, None),
    }
}

/// Reads the status with the token and writes what that says about the token into `check`.
async fn try_token(inner: &Inner, target: &Target, token: &Token, check: &mut ConnectionCheck) {
    let answer = inner
        .http
        .call(target, Some(token), Method::GET, &["v1", "status"], None)
        .await;
    match answer {
        Ok(status) => {
            check.token = TokenCheck::Accepted;
            check.workspaces = status
                .get("workspaces")
                .and_then(Value::as_array)
                .map(Vec::len);
        }
        Err(error) => {
            (check.token, check.lacking) = refusal_of(&error);
            check.detail = Some(error.message);
        }
    }
}

/// The scopes of `token` in the server's list of tokens. The list shows the first characters of
/// each token; when they do not point at exactly one active token, nothing is claimed.
fn scopes_in(list: &Value, token: &Token) -> Option<Vec<Scope>> {
    let own: Vec<&Value> = list
        .get("items")?
        .as_array()?
        .iter()
        .filter(|entry| {
            let prefix = entry.get("prefix").and_then(Value::as_str).unwrap_or("");
            let active = entry.get("status").and_then(Value::as_str) == Some("active");
            active && prefix.len() > TOKEN_PREFIX.len() && token.expose().starts_with(prefix)
        })
        .collect();
    let [entry] = own.as_slice() else {
        return None;
    };
    let scopes = entry.get("scopes")?.as_array()?;
    Some(
        scopes
            .iter()
            .filter_map(|scope| serde_json::from_value(scope.clone()).ok())
            .collect(),
    )
}

/// The scopes of the token, where the API tells them: in the list of tokens, which it gives to
/// a token with the `admin` scope.
async fn own_scopes(inner: &Inner, target: &Target, token: &Token) -> Option<Vec<Scope>> {
    let list = inner
        .http
        .call(target, Some(token), Method::GET, &["v1", "tokens"], None)
        .await
        .ok()?;
    scopes_in(&list, token)
}

async fn check_connection(
    inner: &Arc<Inner>,
    id: &str,
    with_scopes: bool,
) -> AppResult<ConnectionCheck> {
    let target = inner.target(id)?;
    let (shared, token_id) = (Arc::clone(inner), id.to_owned());
    let token = blocking(move || shared.token(&token_id)).await?;
    let mut check = ConnectionCheck {
        unreachable: None,
        version: None,
        ready: None,
        token: if token.is_some() {
            TokenCheck::NotChecked
        } else {
            TokenCheck::Missing
        },
        lacking: None,
        scopes: None,
        workspaces: None,
        detail: None,
    };
    check.unreachable = reach(inner, &target).await;
    if check.unreachable.is_some() {
        return Ok(check);
    }
    check.version = version_of(inner, &target).await;
    check.ready = readiness_of(inner, &target).await;
    let Some(token) = token else {
        return Ok(check);
    };
    try_token(inner, &target, &token, &mut check).await;
    let known = matches!(check.token, TokenCheck::Accepted | TokenCheck::LacksScope);
    if with_scopes && known {
        check.scopes = own_scopes(inner, &target, &token).await;
    }
    Ok(check)
}

/// Tests a connection. Only a connection that is not saved, or whose address is not valid, is
/// an error: everything the test finds, a server that does not answer included, is its answer.
/// `scopes` also asks for the token's scopes, with one more request; the status bar, which asks
/// every minute, leaves it out.
#[tauri::command]
pub async fn connection_check(
    state: State<'_, AppState>,
    id: String,
    scopes: Option<bool>,
) -> AppResult<ConnectionCheck> {
    check_connection(&state.shared(), &id, scopes.unwrap_or(false)).await
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::keychain::MemoryStore;
    use crate::secret::fake_token;
    use crate::store::{ConnectionInput, ConnectionKind, Store};
    use crate::stub::{api_error, Request, Stub};

    struct Setup {
        inner: Arc<Inner>,
        directory: PathBuf,
        id: String,
    }

    impl Setup {
        /// A saved connection to `address`, with a token stored when `token` is given.
        fn new(name: &str, address: &str, token: Option<&str>) -> Self {
            let directory = std::env::temp_dir().join(format!(
                "memini-desktop-check-{}-{name}",
                std::process::id()
            ));
            let _ = std::fs::remove_dir_all(&directory);
            let state = AppState::new(
                Store::open(directory.clone()),
                Box::new(MemoryStore::default()),
            )
            .expect("state");
            let inner = state.shared();
            let view = inner
                .save_connection(&ConnectionInput {
                    id: None,
                    name: "Harbour".to_owned(),
                    kind: ConnectionKind::Remote,
                    url: address.to_owned(),
                    allow_http: false,
                })
                .expect("saved");
            let id = view.connections.last().expect("one").id.clone();
            if let Some(token) = token {
                inner.store_token(&id, token, "test").expect("stored");
            }
            Self {
                inner,
                directory,
                id,
            }
        }

        fn check(&self, with_scopes: bool) -> ConnectionCheck {
            let checking = check_connection(&self.inner, &self.id, with_scopes);
            tauri::async_runtime::block_on(checking).expect("a report")
        }
    }

    impl Drop for Setup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }

    /// A Memini server that answers its public routes, and the status as `status` says.
    fn server(status: fn(&Request) -> (u16, String)) -> Stub {
        Stub::start(move |request| match request.line.as_str() {
            "GET /healthz" => (200, "{\"status\":\"ok\"}".to_owned()),
            "GET /version" => (200, "{\"version\":\"0.2.0\"}".to_owned()),
            "GET /readyz" => (200, "{\"status\":\"ready\"}".to_owned()),
            _ => status(request),
        })
    }

    fn accepted(request: &Request) -> (u16, String) {
        match request.line.as_str() {
            "GET /v1/status" if request.with_token => {
                (200, "{\"workspaces\":[{\"name\":\"harbor\"}]}".to_owned())
            }
            _ => (
                403,
                api_error("insufficient_scope", "The token lacks the admin scope."),
            ),
        }
    }

    fn refused(_request: &Request) -> (u16, String) {
        (
            401,
            api_error("invalid_token", "The bearer token is not valid."),
        )
    }

    fn without_read(request: &Request) -> (u16, String) {
        let own = fake_token('b');
        match request.line.as_str() {
            "GET /v1/tokens" => (
                200,
                format!(
                    "{{\"items\":[{{\"prefix\":\"{}\",\"status\":\"active\",\"scopes\":[\"admin\"]}}]}}",
                    &own[..12]
                ),
            ),
            _ => (403, api_error("insufficient_scope", "The token lacks the read scope.")),
        }
    }

    #[test]
    fn a_server_that_does_not_answer_is_reported_with_what_is_known_without_it() {
        let setup = Setup::new("silent", &Stub::silent_address(), None);

        let check = setup.check(true);

        assert!(check.unreachable.is_some());
        assert_eq!((check.version, check.token), (None, TokenCheck::Missing));
    }

    #[test]
    fn a_stored_token_is_not_tried_on_a_server_that_does_not_answer() {
        let setup = Setup::new("untried", &Stub::silent_address(), Some(&fake_token('b')));

        let check = setup.check(true);

        assert_eq!(
            (check.unreachable.is_some(), check.token),
            (true, TokenCheck::NotChecked)
        );
    }

    #[test]
    fn a_connection_without_a_token_still_reports_the_server_and_asks_nothing_with_a_token() {
        let stub = server(accepted);
        let setup = Setup::new("no-token", &stub.address, None);

        let check = setup.check(true);

        assert_eq!(
            check,
            ConnectionCheck {
                unreachable: None,
                version: Some("0.2.0".to_owned()),
                ready: Some(true),
                token: TokenCheck::Missing,
                lacking: None,
                scopes: None,
                workspaces: None,
                detail: None,
            }
        );
        assert_eq!(
            stub.asked(),
            vec!["GET /healthz ", "GET /version ", "GET /readyz "]
        );
    }

    #[test]
    fn a_token_the_server_accepts_is_reported_with_the_workspaces_it_reads() {
        let stub = server(accepted);
        let setup = Setup::new("accepted", &stub.address, Some(&fake_token('b')));

        let check = setup.check(false);

        assert_eq!(
            (check.token, check.workspaces, check.scopes),
            (TokenCheck::Accepted, Some(1), None)
        );
        assert_eq!(
            stub.asked().last().map(String::as_str),
            Some("GET /v1/status ")
        );
    }

    #[test]
    fn a_token_the_server_does_not_know_is_refused_with_the_servers_words() {
        let stub = server(refused);
        let setup = Setup::new("refused", &stub.address, Some(&fake_token('b')));

        let check = setup.check(true);

        assert_eq!(
            (
                check.token,
                check.detail.as_deref(),
                check.version.as_deref()
            ),
            (
                TokenCheck::Refused,
                Some("The bearer token is not valid."),
                Some("0.2.0")
            )
        );
    }

    #[test]
    fn a_token_without_the_read_scope_names_the_scope_and_shows_the_scopes_it_has() {
        let stub = server(without_read);
        let setup = Setup::new("scope", &stub.address, Some(&fake_token('b')));

        let check = setup.check(true);

        assert_eq!(
            (check.token, check.lacking, check.scopes),
            (
                TokenCheck::LacksScope,
                Some(Scope::Read),
                Some(vec![Scope::Admin])
            )
        );
    }

    #[test]
    fn the_scopes_are_not_asked_for_unless_the_test_wants_them() {
        let stub = server(without_read);
        let setup = Setup::new("quiet", &stub.address, Some(&fake_token('b')));

        let check = setup.check(false);

        assert_eq!(check.scopes, None);
        assert!(!stub.asked().contains(&"GET /v1/tokens ".to_owned()));
    }

    #[test]
    fn an_address_that_answers_without_a_version_is_not_taken_for_a_memini_server() {
        let stub = Stub::start(|_request| (404, "{\"detail\":\"Not Found\"}".to_owned()));
        let setup = Setup::new("other", &stub.address, None);

        let check = setup.check(true);

        assert_eq!(
            (check.unreachable, check.version, check.ready),
            (None, None, None)
        );
    }

    #[test]
    fn the_report_never_carries_the_token() {
        let token = fake_token('b');
        let stub = server(without_read);
        let setup = Setup::new("masked", &stub.address, Some(&token));

        let check = setup.check(true);

        let shown = serde_json::to_string(&check).expect("json");
        assert!(!shown.contains(&token) && !shown.contains(&token[..12]));
    }

    #[test]
    fn the_scopes_of_a_token_are_claimed_only_for_one_active_token_with_its_first_characters() {
        let token = Token::parse(&fake_token('b')).expect("a token");
        let entry = |prefix: &str, status: &str| serde_json::json!({"prefix": prefix, "status": status, "scopes": ["read", "admin"]});
        let own = &token.expose()[..12];
        let list = |entries: Vec<Value>| serde_json::json!({ "items": entries });

        let found = [
            scopes_in(
                &list(vec![entry(own, "active"), entry("memini_zzzzz", "active")]),
                &token,
            ),
            scopes_in(&list(vec![entry(own, "revoked")]), &token),
            scopes_in(
                &list(vec![entry(own, "active"), entry(own, "active")]),
                &token,
            ),
            scopes_in(&list(vec![entry("memini_", "active")]), &token),
            scopes_in(&list(vec![]), &token),
        ];

        assert_eq!(
            found,
            [
                Some(vec![Scope::Read, Scope::Admin]),
                None,
                None,
                None,
                None
            ]
        );
    }
}
