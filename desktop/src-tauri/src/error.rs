//! The one error shape every command answers the webview with.
//!
//! `kind` is stable and the front end branches on it; `message` is for people. A message is built
//! from the app's own words and, for an error of the Memini API, from the API's `message`, which is
//! data: it is masked ([`crate::secret::redact`]), stripped of control characters and cut to
//! [`MAX_TEXT`] characters before it leaves Rust. A token never goes into a message.

use serde::Serialize;

use crate::secret::redact;

/// The longest text of an error that reaches the webview, in characters.
pub const MAX_TEXT: usize = 500;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Input from the user that is not valid.
    Invalid,
    /// No such saved connection.
    NotFound,
    /// The connection has no token in the keychain.
    NoToken,
    /// The operating system's keychain refused, or there is none.
    Keychain,
    /// The app's own settings file could not be read or written.
    Storage,
    /// The clipboard could not be read.
    Clipboard,
    /// The server cannot be reached, or did not answer in time.
    Unreachable,
    /// The server refused the token (401) or what it may do (403).
    Denied,
    /// Another error in the API's error shape.
    Api,
    /// An answer that is not the Memini API's.
    Unexpected,
    /// The `memini-server` program was not found.
    LauncherMissing,
    /// The program could not be run, failed without an answer, or did not finish in time.
    LauncherFailed,
    /// The program's output is not the JSON object of the contract.
    LauncherOutput,
    /// The program answered with a `schema` this app does not know.
    LauncherSchema,
    /// Another launcher action is still running.
    Busy,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AppError {
    pub kind: ErrorKind,
    pub message: String,
    /// The HTTP status, for an answer of a server.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,
    /// The API's stable error code, such as `insufficient_scope`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    /// The API's request id, to find the request in the server's log.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}

impl AppError {
    pub fn new(kind: ErrorKind, message: impl AsRef<str>) -> Self {
        Self {
            kind,
            message: clean(message.as_ref()),
            status: None,
            code: None,
            request_id: None,
        }
    }

    pub fn invalid(message: impl AsRef<str>) -> Self {
        Self::new(ErrorKind::Invalid, message)
    }

    pub fn with_status(mut self, status: u16) -> Self {
        self.status = Some(status);
        self
    }

    pub fn with_code(mut self, code: &str) -> Self {
        self.code = Some(clean(code));
        self
    }

    pub fn with_request_id(mut self, request_id: &str) -> Self {
        self.request_id = Some(clean(request_id));
        self
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for AppError {}

pub type AppResult<T> = Result<T, AppError>;

/// `text` as it may be shown: tokens masked, control characters replaced by spaces, at most
/// [`MAX_TEXT`] characters.
pub fn clean(text: &str) -> String {
    let masked = redact(text);
    let mut out = String::with_capacity(masked.len().min(MAX_TEXT * 4));
    for (count, character) in masked.chars().enumerate() {
        if count == MAX_TEXT {
            out.push('…');
            break;
        }
        out.push(if character.is_control() {
            ' '
        } else {
            character
        });
    }
    out.trim().to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret::fake_token;

    #[test]
    fn a_message_never_holds_a_token() {
        let token = fake_token('f');
        let error = AppError::new(ErrorKind::Api, format!("bad token {token}"))
            .with_code(&token)
            .with_request_id(&token);
        let sent = serde_json::to_string(&error).expect("serializable");
        assert!(!sent.contains(&token));
        assert_eq!(error.message, "bad token [redacted]");
    }

    #[test]
    fn a_message_is_one_line_of_bounded_length() {
        let cleaned = clean(&format!(
            "first\nsecond\u{1b}[31m{}",
            "x".repeat(2 * MAX_TEXT)
        ));
        assert!(cleaned.starts_with("first second [31m"));
        assert_eq!(cleaned.chars().count(), MAX_TEXT + 1);
        assert!(cleaned.ends_with('…'));
    }

    #[test]
    fn the_wire_shape_is_stable() {
        let error = AppError::new(ErrorKind::Denied, "The server refused the token.")
            .with_status(401)
            .with_code("invalid_token");
        let sent = serde_json::to_value(&error).expect("serializable");
        assert_eq!(
            sent,
            serde_json::json!({
                "kind": "denied",
                "message": "The server refused the token.",
                "status": 401,
                "code": "invalid_token"
            })
        );
    }
}
