"""Errors of the credential store. Their text names settings, ids and kinds, never a secret."""

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from memini.credentials.model import OAuthTokens

__all__ = [
    "KEY_SETTINGS",
    "CredentialDecryptionError",
    "CredentialError",
    "CredentialKeyNotConfiguredError",
    "CredentialKeyUnavailableError",
    "CredentialKindError",
    "CredentialNotFoundError",
    "InvalidCredentialError",
    "RefreshNotSavedError",
    "RefreshUnavailableError",
    "RotationBlockedError",
]

KEY_SETTINGS: Final = "MEMINI_CREDENTIAL_KEY or MEMINI_CREDENTIAL_KEY_FILE"


class CredentialError(Exception):
    """Something the credential store refused or could not do."""


class CredentialKeyNotConfiguredError(CredentialError):
    """No ``credential_key`` is set, so credentials can be neither stored nor read."""

    def __init__(self) -> None:
        super().__init__(
            f"the credential store has no key: set {KEY_SETTINGS} to 32 random bytes in base64"
            " (e.g. from `openssl rand -base64 32`)"
        )


class CredentialKeyUnavailableError(CredentialError):
    """A credential is encrypted with a key that is neither the current key nor an old one."""

    def __init__(self, credential_id: uuid.UUID, key_id: str) -> None:
        super().__init__(
            f"credential {credential_id} is encrypted with key {key_id}, which is neither"
            f" MEMINI_CREDENTIAL_KEY nor one of MEMINI_CREDENTIAL_OLD_KEYS"
        )
        self.credential_id = credential_id
        self.key_id = key_id


class CredentialDecryptionError(CredentialError):
    """A ciphertext fails its authentication: it was altered, or belongs to another row."""

    def __init__(self, credential_id: uuid.UUID) -> None:
        super().__init__(
            f"credential {credential_id} cannot be decrypted: its ciphertext was altered or"
            " belongs to another row; set the credential again"
        )
        self.credential_id = credential_id


class CredentialNotFoundError(CredentialError, LookupError):
    """The source has no credential."""

    def __init__(self, source_id: uuid.UUID) -> None:
        super().__init__(f"source {source_id} has no credential")
        self.source_id = source_id


class CredentialKindError(CredentialError):
    """The source's credential is of another kind than the caller asked for."""

    def __init__(self, source_id: uuid.UUID, kind: str, expected: str) -> None:
        super().__init__(f"the credential of source {source_id} is {kind}, not {expected}")
        self.source_id = source_id
        self.kind = kind
        self.expected = expected


class InvalidCredentialError(CredentialError, ValueError):
    """A field of a credential was refused. The message names the field, never its value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class RefreshUnavailableError(CredentialError):
    """An OAuth credential cannot be refreshed: it has no refresh token. Authorize again."""

    def __init__(self, source_id: uuid.UUID) -> None:
        super().__init__(
            f"the OAuth credential of source {source_id} has no refresh token; authorize the"
            " source again and set its credential"
        )
        self.source_id = source_id


class RefreshNotSavedError(CredentialError):
    """The provider issued new tokens, but storing them failed.

    The stored credential is unchanged: it still holds the tokens the refresh started from.
    ``tokens`` are the new ones, in memory only. Store them with
    :func:`memini.credentials.save_refreshed_tokens` on a working connection, passing
    ``base_generation``, before anything else: a provider that rotates refresh tokens may already
    refuse the old one. ``tokens`` hide their values in ``repr``; the message holds none.
    """

    def __init__(self, source_id: uuid.UUID, base_generation: int, tokens: "OAuthTokens") -> None:
        super().__init__(
            f"the provider refreshed the OAuth credential of source {source_id}, but the new"
            f" tokens could not be stored (generation {base_generation} is still stored)"
        )
        self.source_id = source_id
        self.base_generation = base_generation
        self.tokens = tokens


class RotationBlockedError(CredentialError):
    """Credentials are encrypted with keys the keyring lacks, so a rotation could not read them.
    Nothing was re-encrypted. ``missing`` maps each such key id to its number of credentials."""

    def __init__(self, missing: Mapping[str, int]) -> None:
        listing = ", ".join(f"{key_id} ({count})" for key_id, count in sorted(missing.items()))
        super().__init__(
            f"{sum(missing.values())} credentials are encrypted with keys that are neither"
            f" MEMINI_CREDENTIAL_KEY nor one of MEMINI_CREDENTIAL_OLD_KEYS: {listing}; add the"
            " keys they were made with to MEMINI_CREDENTIAL_OLD_KEYS, or set those credentials"
            " again"
        )
        self.missing = dict(missing)
