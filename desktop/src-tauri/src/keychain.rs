//! Tokens in the operating system's keychain: Keychain Services on macOS, the Credential Manager
//! on Windows, the Secret Service on Linux (GNOME Keyring, KWallet).
//!
//! One entry per connection: the service is [`SERVICE`], the account is the connection's id. The
//! webview never reads an entry. Errors say what happened in the app's own words: the keychain
//! library's error can carry the bytes of a stored secret, so its text is never passed on.

use crate::error::{AppError, AppResult, ErrorKind};
use crate::secret::Token;

/// The name the entries are stored under; the keychain's own viewer shows it.
pub const SERVICE: &str = "Memini Desktop";

/// Where tokens are kept. The app uses [`Keychain`]; tests use a store in memory.
pub trait SecretStore: Send + Sync {
    /// The token of `account`; `None` when there is none.
    fn get(&self, account: &str) -> AppResult<Option<Token>>;
    fn set(&self, account: &str, token: &Token) -> AppResult<()>;
    /// Removes the token of `account`. Removing one that is not there is not an error.
    fn delete(&self, account: &str) -> AppResult<()>;
}

/// The operating system's keychain.
#[derive(Debug, Default, Clone, Copy)]
pub struct Keychain;

impl Keychain {
    fn entry(account: &str) -> AppResult<keyring::Entry> {
        keyring::Entry::new(SERVICE, account).map_err(|error| describe(&error))
    }
}

impl SecretStore for Keychain {
    fn get(&self, account: &str) -> AppResult<Option<Token>> {
        match Self::entry(account)?.get_password() {
            Ok(text) => Token::parse(&text).map(Some).ok_or_else(|| {
                AppError::new(
                    ErrorKind::Keychain,
                    "The keychain entry of this connection is not a Memini token. Store the \
                     token again.",
                )
            }),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(describe(&error)),
        }
    }

    fn set(&self, account: &str, token: &Token) -> AppResult<()> {
        Self::entry(account)?
            .set_password(token.expose())
            .map_err(|error| describe(&error))
    }

    fn delete(&self, account: &str) -> AppResult<()> {
        match Self::entry(account)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(describe(&error)),
        }
    }
}

/// The app's own words for a keychain error. Never the library's text; see the module.
fn describe(error: &keyring::Error) -> AppError {
    let message = match error {
        keyring::Error::NoStorageAccess(_) => {
            "The keychain is locked, or access to it was refused. Unlock it and try again."
        }
        keyring::Error::NoDefaultStore => {
            "No keychain is available. On Linux the app needs a Secret Service, such as GNOME \
             Keyring or KWallet, in a running desktop session."
        }
        keyring::Error::BadEncoding(_) | keyring::Error::BadDataFormat(..) => {
            "The keychain entry of this connection cannot be read. Store the token again."
        }
        keyring::Error::Ambiguous(_) => {
            "The keychain holds more than one entry for this connection. Remove them in the \
             keychain's own viewer and store the token again."
        }
        _ => "The keychain reported an error.",
    };
    AppError::new(ErrorKind::Keychain, message)
}

/// A store in memory, for tests: nothing touches the keychain of the machine that runs them.
#[cfg(test)]
#[derive(Debug, Default)]
pub struct MemoryStore(std::sync::Mutex<std::collections::HashMap<String, Token>>);

#[cfg(test)]
impl SecretStore for MemoryStore {
    fn get(&self, account: &str) -> AppResult<Option<Token>> {
        Ok(self.0.lock().expect("not poisoned").get(account).cloned())
    }

    fn set(&self, account: &str, token: &Token) -> AppResult<()> {
        self.0
            .lock()
            .expect("not poisoned")
            .insert(account.to_owned(), token.clone());
        Ok(())
    }

    fn delete(&self, account: &str) -> AppResult<()> {
        self.0.lock().expect("not poisoned").remove(account);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::secret::fake_token;

    #[test]
    fn a_keychain_error_never_carries_the_stored_bytes() {
        let token = fake_token('k');
        let error = describe(&keyring::Error::BadEncoding(token.clone().into_bytes()));
        assert_eq!(error.kind, ErrorKind::Keychain);
        assert!(!error.message.contains(&token));
        assert!(!error.message.contains("kkk"));
    }

    #[test]
    fn the_memory_store_keeps_one_token_per_account() {
        let store = MemoryStore::default();
        let token = Token::parse(&fake_token('m')).expect("a token");
        assert!(store.get("a").expect("readable").is_none());
        store.set("a", &token).expect("stored");
        assert_eq!(store.get("a").expect("readable"), Some(token));
        assert!(store.get("b").expect("readable").is_none());
        store.delete("a").expect("removed");
        store.delete("a").expect("removing nothing is fine");
        assert!(store.get("a").expect("readable").is_none());
    }
}
