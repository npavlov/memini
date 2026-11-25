//! Tokens on the Rust side, and the mask that keeps them out of every text the webview gets.
//!
//! A Memini token is `memini_` followed by 43 base64url characters. The app never sends one to the
//! webview: [`Token`] has no `Serialize`, and its `Debug` prints a mask. Every text that does go to
//! the webview as part of an error passes [`redact`] first, which masks whatever looks like a
//! token, by the rule the service uses for its own logs.

use std::fmt;

/// The public marker every Memini token starts with.
pub const TOKEN_PREFIX: &str = "memini_";

/// Characters after the prefix: the base64url encoding of 32 random bytes.
const TOKEN_BODY_LENGTH: usize = 43;

/// A run of this many token characters after the prefix is masked, as the service's logs do.
const REDACT_MIN_RUN: usize = 20;

/// What a masked token is replaced with.
pub const REDACTED: &str = "[redacted]";

/// A Memini bearer token. Never serialized, never printed.
#[derive(Clone, PartialEq, Eq)]
pub struct Token(String);

impl Token {
    /// The token in `text`, without the white space around it (a token file ends with a line
    /// break); `None` when the text does not have the format of a token.
    pub fn parse(text: &str) -> Option<Self> {
        let value = text.trim();
        is_well_formed(value).then(|| Self(value.to_owned()))
    }

    /// The token itself, for the `Authorization` header and the keychain only.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Token {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Token([redacted])")
    }
}

/// Whether `value` has the format of a token. Says nothing about whether a server knows it.
pub fn is_well_formed(value: &str) -> bool {
    value
        .strip_prefix(TOKEN_PREFIX)
        .is_some_and(|body| body.len() == TOKEN_BODY_LENGTH && body.bytes().all(is_token_byte))
}

fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_'
}

/// `text` with everything that looks like a token masked: `memini_` and 20 or more token
/// characters.
pub fn redact(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find(TOKEN_PREFIX) {
        let after = &rest[at + TOKEN_PREFIX.len()..];
        // Token characters are ASCII, so the count of bytes is a character boundary.
        let run = after
            .bytes()
            .take_while(|byte| is_token_byte(*byte))
            .count();
        if run >= REDACT_MIN_RUN {
            out.push_str(&rest[..at]);
            out.push_str(REDACTED);
            rest = &after[run..];
        } else {
            out.push_str(&rest[..at + TOKEN_PREFIX.len()]);
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

/// A token for tests, assembled at run time so that no literal of it is committed (the
/// repository's secret scan would flag one).
#[cfg(test)]
pub fn fake_token(fill: char) -> String {
    format!(
        "{TOKEN_PREFIX}{}",
        fill.to_string().repeat(TOKEN_BODY_LENGTH)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_is_the_prefix_and_43_base64url_characters() {
        assert!(is_well_formed(&fake_token('a')));
        assert!(is_well_formed(&fake_token('-')));
        assert!(!is_well_formed("memini_short"));
        assert!(!is_well_formed(&format!("{}a", fake_token('a'))));
        assert!(!is_well_formed(&fake_token('!')));
        assert!(!is_well_formed(
            &fake_token('a').replacen("memini_", "other__", 1)
        ));
        assert!(!is_well_formed(""));
    }

    #[test]
    fn parsing_drops_the_line_break_of_a_token_file() {
        let token = fake_token('b');
        let parsed = Token::parse(&format!("  {token}\r\n")).expect("a token");
        assert_eq!(parsed.expose(), token);
        assert!(Token::parse("not a token").is_none());
        assert!(Token::parse(&format!("{token} {token}")).is_none());
    }

    #[test]
    fn debug_never_prints_the_token() {
        let token = fake_token('c');
        let printed = format!("{:?}", Token::parse(&token).expect("a token"));
        assert!(!printed.contains(&token));
        assert!(!printed.contains("ccc"));
    }

    #[test]
    fn redact_masks_every_token_and_keeps_the_rest() {
        let token = fake_token('d');
        let text = format!("refused {token}, again {token}.");
        assert_eq!(redact(&text), "refused [redacted], again [redacted].");
    }

    #[test]
    fn redact_masks_a_truncated_token_as_the_logs_do() {
        let partial = format!("{TOKEN_PREFIX}{}", "e".repeat(REDACT_MIN_RUN));
        assert_eq!(redact(&format!("x {partial} y")), "x [redacted] y");
    }

    #[test]
    fn redact_keeps_names_that_only_start_like_a_token() {
        let text = "memini_db_pool and memini_ alone, and memini_é";
        assert_eq!(redact(text), text);
    }
}
