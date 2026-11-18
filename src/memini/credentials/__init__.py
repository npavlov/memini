"""The credential store: API keys and OAuth tokens of sources, encrypted at rest.

The service reaches some sources on its own (the service executor of MCP recipes; git remotes
later) and needs a credential for each. Requirements section 7: secrets come from the environment
or a secrets file, never from the database in clear text. So the database holds credentials only
encrypted, under a key that comes from the environment (``MEMINI_CREDENTIAL_KEY``), and never the
key.

- :mod:`~memini.credentials.crypto`: AES-256-GCM, the :class:`Keyring` of the current key and the
  old ones, key ids, and the associated data that binds a ciphertext to its row.
- :mod:`~memini.credentials.store`: put, read and delete a source's credential; refresh OAuth
  tokens so that a refresh token is not lost (:func:`refresh_oauth`).
- :mod:`~memini.credentials.rotation`: re-encrypt the store under a new key
  (``memini credentials rotate-key``).

For the service executor, the calls are::

    keyring = Keyring.from_settings(settings)            # once, at start
    oauth = await fresh_oauth(conn, keyring, source_id=..., refresher=...)
    oauth.tokens.access_token.get_secret_value()         # into the Authorization header only
    # the provider refused the access token before it expired:
    oauth = await refresh_oauth(conn, keyring, source_id=..., refresher=...,
                                seen_generation=oauth.info.generation)
    api_key = (await read_api_key(conn, keyring, source_id=...)).api_key

``conn`` for the refresh calls is a connection outside a transaction: they commit by themselves.

Secret values are ``SecretStr`` in memory, registered with :func:`memini.logs.register_secret` as
soon as they are decrypted or received, and never part of an error message. No HTTP route returns
one.
"""

from memini.credentials.crypto import (
    KEY_ID_LENGTH,
    NONCE_LENGTH,
    CredentialKey,
    Keyring,
    Sealed,
    associated_data,
    key_id,
)
from memini.credentials.errors import (
    KEY_SETTINGS,
    CredentialDecryptionError,
    CredentialError,
    CredentialKeyNotConfiguredError,
    CredentialKeyUnavailableError,
    CredentialKindError,
    CredentialNotFoundError,
    InvalidCredentialError,
    RefreshNotSavedError,
    RefreshUnavailableError,
    RotationBlockedError,
)
from memini.credentials.model import (
    MAX_SECRET_LENGTH,
    ApiKeyCredential,
    CredentialInfo,
    CredentialKind,
    OAuthClient,
    OAuthCredential,
    OAuthTokens,
)
from memini.credentials.rotation import DEFAULT_ROTATION_BATCH, RotationResult, rotate_key
from memini.credentials.store import (
    DEFAULT_REFRESH_MARGIN,
    DEFAULT_REFRESH_TIMEOUT,
    Refresher,
    SourceRef,
    delete_credential,
    fresh_oauth,
    get_credential_info,
    list_credentials,
    put_api_key,
    put_oauth,
    read_api_key,
    read_oauth,
    refresh_oauth,
    resolve_source,
    save_refreshed_tokens,
)

__all__ = [
    "DEFAULT_REFRESH_MARGIN",
    "DEFAULT_REFRESH_TIMEOUT",
    "DEFAULT_ROTATION_BATCH",
    "KEY_ID_LENGTH",
    "KEY_SETTINGS",
    "MAX_SECRET_LENGTH",
    "NONCE_LENGTH",
    "ApiKeyCredential",
    "CredentialDecryptionError",
    "CredentialError",
    "CredentialInfo",
    "CredentialKey",
    "CredentialKeyNotConfiguredError",
    "CredentialKeyUnavailableError",
    "CredentialKind",
    "CredentialKindError",
    "CredentialNotFoundError",
    "InvalidCredentialError",
    "Keyring",
    "OAuthClient",
    "OAuthCredential",
    "OAuthTokens",
    "RefreshNotSavedError",
    "RefreshUnavailableError",
    "Refresher",
    "RotationBlockedError",
    "RotationResult",
    "Sealed",
    "SourceRef",
    "associated_data",
    "delete_credential",
    "fresh_oauth",
    "get_credential_info",
    "key_id",
    "list_credentials",
    "put_api_key",
    "put_oauth",
    "read_api_key",
    "read_oauth",
    "refresh_oauth",
    "resolve_source",
    "rotate_key",
    "save_refreshed_tokens",
]
