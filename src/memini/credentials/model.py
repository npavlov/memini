"""What a credential holds, in memory. Secret values are ``SecretStr``: ``repr`` and ``str`` hide
them, and they are unwrapped only where they are sent or encrypted."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr

from memini.credentials.errors import InvalidCredentialError

__all__ = [
    "MAX_SECRET_LENGTH",
    "ApiKeyCredential",
    "CredentialInfo",
    "CredentialKind",
    "OAuthClient",
    "OAuthCredential",
    "OAuthTokens",
]

CredentialKind = Literal["api_key", "oauth"]

MAX_SECRET_LENGTH: Final = 16384
"""The longest secret value accepted. Tokens are far shorter; a longer value is a mistake."""

_MAX_URL_LENGTH: Final = 2048
_MAX_TEXT_LENGTH: Final = 4096


def check_secret(field: str, secret: SecretStr) -> SecretStr:
    """``secret`` if it is one line of printable characters without spaces; a header or a form
    field could not carry anything else. The error names ``field``, never the value."""
    value = secret.get_secret_value()
    if not value:
        raise InvalidCredentialError(field, "is empty")
    if len(value) > MAX_SECRET_LENGTH:
        raise InvalidCredentialError(field, f"is longer than {MAX_SECRET_LENGTH} characters")
    if any(c.isspace() or not c.isprintable() for c in value):
        raise InvalidCredentialError(field, "holds whitespace or control characters")
    return secret


def _check_text(field: str, value: str | None) -> None:
    if value is None:
        return
    if not value or len(value) > _MAX_TEXT_LENGTH or not value.isprintable():
        raise InvalidCredentialError(
            field, f"must be printable text of 1 to {_MAX_TEXT_LENGTH} characters"
        )


def _check_endpoint(url: str | None) -> None:
    if url is None:
        return
    field = "token_endpoint"
    if len(url) > _MAX_URL_LENGTH or any(c.isspace() or not c.isprintable() for c in url):
        raise InvalidCredentialError(
            field, f"must be a URL of at most {_MAX_URL_LENGTH} characters"
        )
    try:
        parts = urlsplit(url)
        has_userinfo = parts.username is not None or parts.password is not None
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        raise InvalidCredentialError(field, "is not a valid URL") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise InvalidCredentialError(field, "must be an https:// or http:// URL with a host")
    if has_userinfo:
        # The endpoint is stored in clear: credentials in it would be too.
        raise InvalidCredentialError(
            field, "holds credentials; give the client secret as client_secret instead"
        )
    if parts.fragment:
        raise InvalidCredentialError(field, "must not have a fragment")


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """The tokens of an OAuth credential.

    ``refresh_token`` is ``None`` when the provider issued none: the credential then works until
    the access token expires and cannot be refreshed. ``expires_at`` (timezone-aware) is when the
    access token expires, ``None`` when the provider did not say.
    """

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        check_secret("access_token", self.access_token)
        if self.refresh_token is not None:
            check_secret("refresh_token", self.refresh_token)
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise InvalidCredentialError("expires_at", "must carry a time zone")


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """How an OAuth credential is refreshed: the token endpoint and the client, as registered with
    the provider. Only ``client_secret`` is secret; the rest is stored in clear."""

    token_endpoint: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    scope: str | None = None

    def __post_init__(self) -> None:
        _check_endpoint(self.token_endpoint)
        _check_text("client_id", self.client_id)
        _check_text("scope", self.scope)
        if self.client_secret is not None:
            check_secret("client_secret", self.client_secret)


@dataclass(frozen=True, slots=True)
class CredentialInfo:
    """A stored credential without its secret: safe to show, list and log."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace: str
    """The name of the workspace."""
    owner_id: uuid.UUID
    source_id: uuid.UUID
    source: str
    """The name of the source."""
    kind: CredentialKind
    key_id: str
    """The id of the key the secret is encrypted with."""
    generation: int
    """The version of the secret: one more on every set and every refresh."""
    expires_at: datetime | None
    """oauth: when the access token expires; ``None`` when unknown, and for an API key."""
    token_endpoint: str | None
    client_id: str | None
    scope: str | None
    refreshable: bool
    """oauth: whether there is a refresh token."""
    created_at: datetime
    updated_at: datetime
    """When the secret last changed: set again, or refreshed."""
    refreshed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApiKeyCredential:
    """A decrypted API key and what is stored with it."""

    info: CredentialInfo
    api_key: SecretStr


@dataclass(frozen=True, slots=True)
class OAuthCredential:
    """Decrypted OAuth tokens, the client they belong to, and what is stored with them."""

    info: CredentialInfo
    tokens: OAuthTokens
    client: OAuthClient
