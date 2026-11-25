//! Calls to the Memini HTTP API. Every call is made here, in Rust: the webview never builds a
//! request, never sees a token and never holds an `Authorization` header.
//!
//! - The token goes to the connection's own address and nowhere else: redirects are not followed.
//! - A server on this computer is called directly; another server through the system's proxy
//!   settings, if there are any.
//! - An answer is read up to [`MAX_BODY`] bytes.
//! - An error of the API (`{"error": {"code", "message", "request_id", "details"}}`) becomes an
//!   [`AppError`] with the API's code. Its message is the server's text, which is data: it is
//!   shown as text and decides nothing.
//! - Nothing is logged, and the text of a transport error is never passed on.

use std::error::Error as _;
use std::time::Duration;

use reqwest::header::{HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_TYPE};
use reqwest::Method;
use serde_json::Value;
use url::Url;

use crate::error::{AppError, AppResult, ErrorKind};
use crate::secret::Token;
use crate::store::{check_url, is_loopback, Connection};

/// Seconds to connect to the server.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
/// Seconds for a whole call.
const TIMEOUT: Duration = Duration::from_secs(60);
/// The largest answer that is read, in bytes.
pub const MAX_BODY: usize = 8 * 1024 * 1024;

const USER_AGENT: &str = concat!("memini-desktop/", env!("CARGO_PKG_VERSION"));
const JSON: &str = "application/json";

/// Where a connection's server is.
#[derive(Debug, Clone)]
pub struct Target {
    base: Url,
    loopback: bool,
}

impl Target {
    /// The target of a saved connection. The address is checked again on every use: the settings
    /// file can be edited by hand.
    pub fn of(connection: &Connection) -> AppResult<Self> {
        let base = check_url(&connection.url, connection.kind, connection.allow_http)?;
        Ok(Self {
            loopback: is_loopback(&base),
            base,
        })
    }

    /// The address of a route. Each segment is encoded, so a value cannot reach another route.
    pub fn endpoint(&self, segments: &[&str]) -> AppResult<Url> {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|()| AppError::invalid("The connection's address cannot take a path."))?
            .pop_if_empty()
            .extend(segments);
        Ok(url)
    }

    /// The address of a route with a query. Segments and query pairs are encoded; an empty last
    /// segment gives the trailing slash of a directory route.
    pub fn route(&self, segments: &[String], query: &[(String, String)]) -> AppResult<Url> {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|()| AppError::invalid("The connection's address cannot take a path."))?
            .pop_if_empty()
            .extend(segments);
        if !query.is_empty() {
            url.query_pairs_mut().extend_pairs(query);
        }
        Ok(url)
    }

    /// Scheme, host and port, for messages. It is what the user entered, never a secret.
    pub fn place(&self) -> String {
        self.base.origin().ascii_serialization()
    }
}

/// An answer as it arrived.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Raw {
    pub status: u16,
    pub body: Vec<u8>,
}

pub struct ApiClient {
    direct: reqwest::Client,
    proxied: reqwest::Client,
}

impl ApiClient {
    pub fn new() -> AppResult<Self> {
        Ok(Self {
            direct: build_client(true)?,
            proxied: build_client(false)?,
        })
    }

    /// One call. `token` is sent as the bearer token when given; public routes take none.
    pub async fn exchange(
        &self,
        target: &Target,
        token: Option<&Token>,
        method: Method,
        segments: &[&str],
        body: Option<&Value>,
    ) -> AppResult<Raw> {
        let url = target.endpoint(segments)?;
        self.send(target, token, method, url, body).await
    }

    /// One call to an address that [`Target::endpoint`] or [`Target::route`] built for `target`.
    pub async fn send(
        &self,
        target: &Target,
        token: Option<&Token>,
        method: Method,
        url: Url,
        body: Option<&Value>,
    ) -> AppResult<Raw> {
        let client = if target.loopback {
            &self.direct
        } else {
            &self.proxied
        };
        let mut request = client.request(method, url).header(ACCEPT, JSON);
        if let Some(token) = token {
            let mut value = HeaderValue::from_str(&format!("Bearer {}", token.expose()))
                .map_err(|_| AppError::new(ErrorKind::NoToken, "The stored token is not valid."))?;
            value.set_sensitive(true);
            request = request.header(AUTHORIZATION, value);
        }
        if let Some(body) = body {
            let bytes = serde_json::to_vec(body)
                .map_err(|_| AppError::invalid("The request could not be written."))?;
            request = request.header(CONTENT_TYPE, JSON).body(bytes);
        }
        let place = target.place();
        let mut response = request
            .send()
            .await
            .map_err(|error| transport_error(&error, &place))?;
        let status = response.status().as_u16();
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| transport_error(&error, &place))?
        {
            if bytes.len() + chunk.len() > MAX_BODY {
                return Err(AppError::new(
                    ErrorKind::Unexpected,
                    format!("The server at {place} sent a longer answer than the app reads."),
                )
                .with_status(status));
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok(Raw {
            status,
            body: bytes,
        })
    }

    /// A call that must answer with a JSON object.
    pub async fn call(
        &self,
        target: &Target,
        token: Option<&Token>,
        method: Method,
        segments: &[&str],
        body: Option<&Value>,
    ) -> AppResult<Value> {
        let place = target.place();
        let raw = self.exchange(target, token, method, segments, body).await?;
        interpret(&raw, &place)?.ok_or_else(|| {
            AppError::new(
                ErrorKind::Unexpected,
                format!("The server at {place} answered without a body. {NOT_MEMINI}"),
            )
            .with_status(raw.status)
        })
    }
}

fn build_client(direct: bool) -> AppResult<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .referer(false)
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(TIMEOUT)
        .user_agent(USER_AGENT);
    if direct {
        builder = builder.no_proxy();
    }
    builder.build().map_err(|_| {
        AppError::new(
            ErrorKind::Unexpected,
            "The HTTP client could not be set up.",
        )
    })
}

const NOT_MEMINI: &str = "Is this the address of a Memini server?";

/// The app's own words for a call that got no answer. The error's text is not used: it can hold
/// more than the user entered.
fn transport_error(error: &reqwest::Error, place: &str) -> AppError {
    let message = if error.is_timeout() {
        format!("The server at {place} did not answer in time.")
    } else if mentions_certificate(error) {
        format!(
            "The certificate of the server at {place} was not accepted. The app trusts what the \
             operating system trusts."
        )
    } else if error.is_connect() {
        format!(
            "Cannot reach the server at {place}. Check the address, that the server is running, \
             and any VPN it needs."
        )
    } else {
        format!("The connection to the server at {place} failed.")
    };
    AppError::new(ErrorKind::Unreachable, message)
}

fn mentions_certificate(error: &reqwest::Error) -> bool {
    let mut source = error.source();
    while let Some(cause) = source {
        if cause
            .to_string()
            .to_ascii_lowercase()
            .contains("certificate")
        {
            return true;
        }
        source = cause.source();
    }
    false
}

/// The JSON object of a successful answer (`None` for one without a body), or the error an
/// answer that is not a success stands for.
pub fn interpret(raw: &Raw, place: &str) -> AppResult<Option<Value>> {
    let status = raw.status;
    if !(200..300).contains(&status) {
        return Err(failure(raw, place));
    }
    if status == 204 || raw.body.is_empty() {
        return Ok(None);
    }
    match serde_json::from_slice::<Value>(&raw.body) {
        Ok(value @ Value::Object(_)) => Ok(Some(value)),
        _ => Err(AppError::new(
            ErrorKind::Unexpected,
            format!(
                "The server at {place} answered HTTP {status} with something other than a JSON \
                 object. {NOT_MEMINI}"
            ),
        )
        .with_status(status)),
    }
}

/// The error for an answer that is not a success.
pub fn failure(raw: &Raw, place: &str) -> AppError {
    let status = raw.status;
    let Some(error) = error_body(&raw.body) else {
        let (kind, message) = match status {
            502..=504 => (
                ErrorKind::Unreachable,
                format!("The server at {place} is unavailable (HTTP {status})."),
            ),
            300..=399 => (
                ErrorKind::Unexpected,
                format!(
                    "The server at {place} answered with a redirect (HTTP {status}), which is \
                     not followed. Check the address."
                ),
            ),
            _ => (
                ErrorKind::Unexpected,
                format!(
                    "The server at {place} answered HTTP {status} without the API's error body. \
                     {NOT_MEMINI}"
                ),
            ),
        };
        return AppError::new(kind, message).with_status(status);
    };
    let code = text_of(error.get("code")).unwrap_or("error");
    let mut message =
        text_of(error.get("message")).map_or_else(|| format!("HTTP {status}"), str::to_owned);
    let fields = failed_fields(error.get("details"));
    if !fields.is_empty() {
        message = format!("{message} Not valid: {fields}.");
    }
    let kind = match (status, code) {
        (401 | 403, _) => ErrorKind::Denied,
        (503, "unavailable") => ErrorKind::Unreachable,
        _ => ErrorKind::Api,
    };
    let mut app_error = AppError::new(kind, message)
        .with_status(status)
        .with_code(code);
    if let Some(request_id) = text_of(error.get("request_id")) {
        app_error = app_error.with_request_id(request_id);
    }
    app_error
}

/// The `error` object of an answer in the API's error shape.
fn error_body(body: &[u8]) -> Option<serde_json::Map<String, Value>> {
    match serde_json::from_slice::<Value>(body).ok()? {
        Value::Object(mut object) => match object.remove("error")? {
            Value::Object(error) => Some(error),
            _ => None,
        },
        _ => None,
    }
}

fn text_of(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

/// `body.name (string_too_short), query.limit (less_than_equal)` from the error's `details`:
/// where a field is and how it failed, never its value.
fn failed_fields(details: Option<&Value>) -> String {
    let Some(Value::Array(details)) = details else {
        return String::new();
    };
    let mut found = Vec::new();
    for detail in details.iter().take(20) {
        let Value::Object(entry) = detail else {
            continue;
        };
        let location = match entry.get("location") {
            Some(Value::Array(parts)) => parts
                .iter()
                .filter_map(|part| match part {
                    Value::String(text) => Some(text.clone()),
                    Value::Number(number) => Some(number.to_string()),
                    _ => None,
                })
                .collect::<Vec<_>>()
                .join("."),
            _ => String::new(),
        };
        let location = if location.is_empty() {
            "?".to_owned()
        } else {
            location
        };
        let kind = text_of(entry.get("type")).unwrap_or("invalid");
        found.push(format!("{location} ({kind})"));
    }
    found.join(", ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret::fake_token;
    use crate::store::ConnectionKind;

    const PLACE: &str = "https://memini.example.org";

    fn raw(status: u16, body: &str) -> Raw {
        Raw {
            status,
            body: body.as_bytes().to_vec(),
        }
    }

    fn connection(url: &str) -> Connection {
        Connection {
            id: "c1".to_owned(),
            name: "Harbour".to_owned(),
            kind: ConnectionKind::Remote,
            url: url.to_owned(),
            allow_http: false,
            has_token: true,
        }
    }

    #[test]
    fn a_route_is_appended_to_the_address_and_its_prefix() {
        let target = Target::of(&connection("https://memini.example.org/prefix")).expect("valid");
        let url = target.endpoint(&["v1", "tokens"]).expect("a route");
        assert_eq!(url.as_str(), "https://memini.example.org/prefix/v1/tokens");
        assert_eq!(target.place(), "https://memini.example.org");
        let plain = Target::of(&connection("https://memini.example.org")).expect("valid");
        assert_eq!(
            plain.endpoint(&["version"]).expect("a route").as_str(),
            "https://memini.example.org/version"
        );
    }

    #[test]
    fn a_segment_cannot_reach_another_route() {
        let target = Target::of(&connection("https://memini.example.org")).expect("valid");
        let url = target
            .endpoint(&["v1", "tokens", "../workspaces?x=1#y"])
            .expect("a route");
        assert_eq!(
            url.as_str(),
            "https://memini.example.org/v1/tokens/..%2Fworkspaces%3Fx=1%23y"
        );
        assert_eq!(url.query(), None);
    }

    #[test]
    fn a_route_with_a_query_encodes_its_pairs_and_keeps_a_trailing_slash() {
        let target = Target::of(&connection("https://memini.example.org/prefix")).expect("valid");
        let segments = ["v1".to_owned(), "search".to_owned()];
        let query = [
            ("query".to_owned(), "archive & retention?".to_owned()),
            ("kinds".to_owned(), "thread".to_owned()),
            ("kinds".to_owned(), "meeting".to_owned()),
        ];
        let url = target.route(&segments, &query).expect("a route");
        assert_eq!(
            url.as_str(),
            "https://memini.example.org/prefix/v1/search?query=archive+%26+retention%3F&kinds=thread&kinds=meeting"
        );
        let directory = ["files".to_owned(), String::new()];
        let url = target.route(&directory, &[]).expect("a route");
        assert_eq!(url.as_str(), "https://memini.example.org/prefix/files/");
        assert_eq!(url.query(), None);
    }

    #[test]
    fn a_saved_address_is_checked_again_before_a_call() {
        let edited = connection("http://memini.example.org");
        assert!(Target::of(&edited).is_err());
        let loopback = Target::of(&connection("http://127.0.0.1:6364")).expect("valid");
        assert!(loopback.loopback);
    }

    #[test]
    fn a_success_is_a_json_object_or_nothing() {
        let value = interpret(&raw(200, r#"{"tokens": []}"#), PLACE).expect("ok");
        assert_eq!(value, Some(serde_json::json!({"tokens": []})));
        assert_eq!(interpret(&raw(204, ""), PLACE).expect("ok"), None);
        let error = interpret(&raw(200, "<html>hello</html>"), PLACE).expect_err("not the API");
        assert_eq!(error.kind, ErrorKind::Unexpected);
        assert!(!error.message.contains("<html>"));
        assert_eq!(
            interpret(&raw(200, "[1, 2]"), PLACE)
                .expect_err("not an object")
                .kind,
            ErrorKind::Unexpected
        );
    }

    #[test]
    fn an_api_error_keeps_its_code_and_request_id() {
        let body = r#"{"error": {"code": "insufficient_scope", "message": "The token lacks the admin scope.", "request_id": "req-1", "details": []}}"#;
        let error = failure(&raw(403, body), PLACE);
        assert_eq!(error.kind, ErrorKind::Denied);
        assert_eq!(error.status, Some(403));
        assert_eq!(error.code.as_deref(), Some("insufficient_scope"));
        assert_eq!(error.request_id.as_deref(), Some("req-1"));
        assert_eq!(error.message, "The token lacks the admin scope.");
    }

    #[test]
    fn a_validation_error_names_fields_and_never_values() {
        let body = r#"{"error": {"code": "validation_error", "message": "The request is not valid.", "request_id": "r", "details": [{"location": ["body", "name"], "type": "string_too_short"}, {"location": ["body", "scopes", 0], "type": "literal_error"}]}}"#;
        let error = failure(&raw(422, body), PLACE);
        assert_eq!(error.kind, ErrorKind::Api);
        assert_eq!(
            error.message,
            "The request is not valid. Not valid: body.name (string_too_short), body.scopes.0 \
             (literal_error)."
        );
    }

    #[test]
    fn a_database_that_is_away_is_unreachable_not_an_error_of_the_request() {
        let body = r#"{"error": {"code": "unavailable", "message": "The database is not available; try again later.", "request_id": "r", "details": []}}"#;
        assert_eq!(failure(&raw(503, body), PLACE).kind, ErrorKind::Unreachable);
        assert_eq!(
            failure(&raw(502, "Bad Gateway"), PLACE).kind,
            ErrorKind::Unreachable
        );
        let timeout =
            r#"{"error": {"code": "query_timeout", "message": "Too slow.", "request_id": "r"}}"#;
        assert_eq!(failure(&raw(503, timeout), PLACE).kind, ErrorKind::Api);
    }

    #[test]
    fn an_answer_that_is_not_the_api_says_so_without_its_text() {
        let redirect = failure(&raw(302, ""), PLACE);
        assert_eq!(redirect.kind, ErrorKind::Unexpected);
        assert!(redirect.message.contains("redirect"));
        let page = failure(&raw(404, "<h1>secret page</h1>"), PLACE);
        assert_eq!(page.kind, ErrorKind::Unexpected);
        assert!(!page.message.contains("secret page"));
    }

    #[test]
    fn a_token_in_a_server_message_is_masked() {
        let token = fake_token('z');
        let body = format!(
            r#"{{"error": {{"code": "bad_request", "message": "you sent {token}", "request_id": "r"}}}}"#
        );
        let error = failure(&raw(400, &body), PLACE);
        assert!(!serde_json::to_string(&error)
            .expect("serializable")
            .contains(&token));
    }
}
