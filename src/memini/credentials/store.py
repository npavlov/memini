"""Reading and writing credentials, and refreshing OAuth tokens without losing one.

The functions take a source id: they serve the service itself (the service executor, the git
connector) and the CLI, which find the source first. :func:`resolve_source` finds one by workspace
and name through a :class:`~memini.auth.WriteAccess`, as a write must.

Writes (:func:`put_api_key`, :func:`put_oauth`, :func:`delete_credential`) run in a transaction of
their own, or in a savepoint when the caller has one open. :func:`refresh_oauth`,
:func:`fresh_oauth` and :func:`save_refreshed_tokens` commit by themselves and refuse a connection
inside a transaction: a refreshed token is safe only once its commit is real.

Every secret this module decrypts or is given is registered with
:func:`memini.logs.register_secret` before it is returned, so a log line that holds one by mistake
shows it masked. Nothing here logs a secret, and no error text holds one.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from psycopg import AsyncConnection, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from memini.auth import WriteAccess
from memini.credentials.crypto import Keyring, Sealed, associated_data
from memini.credentials.errors import (
    CredentialError,
    CredentialKindError,
    CredentialNotFoundError,
    RefreshNotSavedError,
    RefreshUnavailableError,
)
from memini.credentials.model import (
    ApiKeyCredential,
    CredentialInfo,
    CredentialKind,
    OAuthClient,
    OAuthCredential,
    OAuthTokens,
    check_secret,
)
from memini.logs import register_secret
from memini.workspaces import NotFoundError, get_writable_workspace

__all__ = [
    "DEFAULT_REFRESH_MARGIN",
    "DEFAULT_REFRESH_TIMEOUT",
    "Refresher",
    "SourceRef",
    "delete_credential",
    "fresh_oauth",
    "get_credential_info",
    "list_credentials",
    "put_api_key",
    "put_oauth",
    "read_api_key",
    "read_oauth",
    "refresh_oauth",
    "resolve_source",
    "save_refreshed_tokens",
]

type Conn = AsyncConnection[TupleRow]

type Refresher = Callable[[OAuthCredential], Awaitable[OAuthTokens]]
"""Calls the provider's token endpoint with the refresh token of the credential it is given and
returns the tokens the provider issued. A ``refresh_token`` of ``None`` in the result means the
provider issued none, and the old one stays (RFC 6749, section 6). It raises on any failure; it
must not store anything itself."""

DEFAULT_REFRESH_TIMEOUT: Final = 60.0
"""Seconds a refresher may take. The credential's row stays locked while it runs."""

DEFAULT_REFRESH_MARGIN: Final = timedelta(minutes=2)
""":func:`fresh_oauth` refreshes an access token that expires within this time."""

log = logging.getLogger(__name__)


# --- Rows ----------------------------------------------------------------------------------------

_COLUMNS: Final = sql.SQL("""
c.id, c.workspace_id, w.name, c.owner_id, c.source_id, s.name, c.kind, c.metadata, c.expires_at,
c.key_id, c.generation, c.created_at, c.updated_at, c.refreshed_at, c.nonce, c.ciphertext
""")

_FROM: Final = sql.SQL("""
FROM credential c
JOIN source s ON s.id = c.source_id
JOIN workspace w ON w.id = c.workspace_id
""")


@dataclass(frozen=True, slots=True)
class _Row:
    info: CredentialInfo
    sealed: Sealed

    @property
    def aad(self) -> bytes:
        return associated_data(
            credential_id=self.info.id,
            workspace_id=self.info.workspace_id,
            source_id=self.info.source_id,
            kind=self.info.kind,
        )


def _row(values: Sequence[Any]) -> _Row:
    (
        credential_id,
        workspace_id,
        workspace,
        owner_id,
        source_id,
        source,
        kind,
        metadata,
        expires_at,
        key_id,
        generation,
        created_at,
        updated_at,
        refreshed_at,
        nonce,
        ciphertext,
    ) = values
    meta = cast(Mapping[str, Any], metadata)
    info = CredentialInfo(
        id=credential_id,
        workspace_id=workspace_id,
        workspace=workspace,
        owner_id=owner_id,
        source_id=source_id,
        source=source,
        kind=kind,
        key_id=key_id,
        generation=generation,
        expires_at=expires_at,
        token_endpoint=meta.get("token_endpoint"),
        client_id=meta.get("client_id"),
        scope=meta.get("scope"),
        refreshable=bool(meta.get("refreshable", False)),
        created_at=created_at,
        updated_at=updated_at,
        refreshed_at=refreshed_at,
    )
    return _Row(info, Sealed(key_id, bytes(nonce), bytes(ciphertext)))


async def _fetch(conn: Conn, source_id: uuid.UUID, *, lock: bool = False) -> _Row | None:
    query = sql.SQL("SELECT {} {} WHERE c.source_id = %s{}").format(
        _COLUMNS, _FROM, sql.SQL(" FOR NO KEY UPDATE OF c") if lock else sql.SQL("")
    )
    cur = await conn.execute(query, (source_id,))
    values = await cur.fetchone()
    return None if values is None else _row(values)


async def _fetch_by_id(conn: Conn, credential_id: uuid.UUID) -> _Row:
    cur = await conn.execute(
        sql.SQL("SELECT {} {} WHERE c.id = %s").format(_COLUMNS, _FROM), (credential_id,)
    )
    values = await cur.fetchone()
    if values is None:  # written in this transaction
        raise RuntimeError(f"credential {credential_id} vanished")
    return _row(values)


# --- Secrets -------------------------------------------------------------------------------------


def _register(*secrets: SecretStr | None) -> None:
    for secret in secrets:
        if secret is not None:
            register_secret(secret.get_secret_value())


def _seal(keyring: Keyring, row_aad: bytes, fields: Mapping[str, SecretStr | None]) -> Sealed:
    plain = {name: value.get_secret_value() for name, value in fields.items() if value is not None}
    return keyring.seal(json.dumps(plain, sort_keys=True, separators=(",", ":")).encode(), row_aad)


def _open(keyring: Keyring, row: _Row) -> dict[str, SecretStr]:
    plaintext = keyring.open(row.sealed, row.aad, credential_id=row.info.id)
    try:
        loaded: object = json.loads(plaintext)
    except ValueError:
        loaded = None
    if not isinstance(loaded, dict):
        raise CredentialError(f"credential {row.info.id} holds a secret of an unknown form")
    fields: dict[str, SecretStr] = {}
    for name, value in cast(dict[object, object], loaded).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise CredentialError(f"credential {row.info.id} holds a secret of an unknown form")
        fields[name] = SecretStr(value)
    _register(*fields.values())
    return fields


def _required(row: _Row, fields: Mapping[str, SecretStr], name: str) -> SecretStr:
    value = fields.get(name)
    if value is None:
        raise CredentialError(f"credential {row.info.id} lacks its {name}")
    return value


def _api_key(row: _Row, keyring: Keyring) -> ApiKeyCredential:
    if row.info.kind != "api_key":
        raise CredentialKindError(row.info.source_id, row.info.kind, "api_key")
    fields = _open(keyring, row)
    return ApiKeyCredential(row.info, _required(row, fields, "api_key"))


def _oauth(row: _Row, keyring: Keyring) -> OAuthCredential:
    if row.info.kind != "oauth":
        raise CredentialKindError(row.info.source_id, row.info.kind, "oauth")
    fields = _open(keyring, row)
    tokens = OAuthTokens(
        access_token=_required(row, fields, "access_token"),
        refresh_token=fields.get("refresh_token"),
        expires_at=row.info.expires_at,
    )
    client = OAuthClient(
        token_endpoint=row.info.token_endpoint,
        client_id=row.info.client_id,
        client_secret=fields.get("client_secret"),
        scope=row.info.scope,
    )
    return OAuthCredential(row.info, tokens, client)


def _oauth_secrets(tokens: OAuthTokens, client: OAuthClient) -> dict[str, SecretStr | None]:
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "client_secret": client.client_secret,
    }


def _oauth_metadata(tokens: OAuthTokens, client: OAuthClient) -> dict[str, Any]:
    metadata: dict[str, Any] = {"refreshable": tokens.refresh_token is not None}
    for name, value in (
        ("token_endpoint", client.token_endpoint),
        ("client_id", client.client_id),
        ("scope", client.scope),
    ):
        if value is not None:
            metadata[name] = value
    return metadata


def _require_idle(conn: Conn, function: str) -> None:
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError(
            f"{function} commits by itself and needs a connection outside a transaction"
        )


# --- Sources -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A source found by :func:`resolve_source`."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    workspace: str
    name: str
    kind: str


async def resolve_source(conn: Conn, access: WriteAccess, workspace: str, name: str) -> SourceRef:
    """The source ``name`` of the workspace ``workspace``, if ``access`` may change it.

    Raises:
        NotFoundError: no such workspace or source that ``access`` may change.
    """
    found = await get_writable_workspace(conn, access, workspace)
    condition = access.where(
        workspace_id=sql.Identifier("s", "workspace_id"),
        owner_id=sql.Identifier("s", "owner_id"),
        source_id=sql.Identifier("s", "id"),
    )
    cur = await conn.execute(
        sql.SQL(
            "SELECT s.id, s.kind FROM source s WHERE s.workspace_id = %s AND s.name = %s AND {}"
        ).format(condition),
        (found.id, name),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("source")
    return SourceRef(id=row[0], workspace_id=found.id, workspace=found.name, name=name, kind=row[1])


# --- Reading -------------------------------------------------------------------------------------


async def get_credential_info(conn: Conn, *, source_id: uuid.UUID) -> CredentialInfo | None:
    """The source's credential without its secret, or ``None``. Needs no key."""
    row = await _fetch(conn, source_id)
    return None if row is None else row.info


async def list_credentials(
    conn: Conn, *, workspace_id: uuid.UUID | None = None
) -> list[CredentialInfo]:
    """Every credential, or those of one workspace, without secrets, by workspace and source name.

    Needs no key."""
    condition = sql.SQL("TRUE") if workspace_id is None else sql.SQL("c.workspace_id = %s")
    cur = await conn.execute(
        sql.SQL("SELECT {} {} WHERE {} ORDER BY w.name, s.name").format(_COLUMNS, _FROM, condition),
        () if workspace_id is None else (workspace_id,),
    )
    return [_row(values).info for values in await cur.fetchall()]


async def read_api_key(conn: Conn, keyring: Keyring, *, source_id: uuid.UUID) -> ApiKeyCredential:
    """The source's API key, decrypted.

    Raises:
        CredentialNotFoundError: the source has no credential.
        CredentialKindError: its credential is not an API key.
        CredentialKeyNotConfiguredError, CredentialKeyUnavailableError, CredentialDecryptionError:
            see :meth:`Keyring.open`.
    """
    row = await _fetch(conn, source_id)
    if row is None:
        raise CredentialNotFoundError(source_id)
    return _api_key(row, keyring)


async def read_oauth(conn: Conn, keyring: Keyring, *, source_id: uuid.UUID) -> OAuthCredential:
    """The source's OAuth tokens and client, decrypted, as stored: possibly expired. Use
    :func:`fresh_oauth` for tokens to send.

    Raises: as :func:`read_api_key`.
    """
    row = await _fetch(conn, source_id)
    if row is None:
        raise CredentialNotFoundError(source_id)
    return _oauth(row, keyring)


# --- Writing -------------------------------------------------------------------------------------


async def _put(
    conn: Conn,
    keyring: Keyring,
    source_id: uuid.UUID,
    kind: CredentialKind,
    secrets: Mapping[str, SecretStr | None],
    metadata: Mapping[str, Any],
    expires_at: datetime | None,
) -> CredentialInfo:
    keyring.current  # noqa: B018 - refuse without a key before anything is read or written
    _register(*secrets.values())
    async with conn.transaction():
        # Locking the source serializes puts of one source, so two cannot both insert, and waits
        # for a deletion of the source to finish.
        cur = await conn.execute(
            "SELECT workspace_id, owner_id FROM source WHERE id = %s FOR NO KEY UPDATE",
            (source_id,),
        )
        source = await cur.fetchone()
        if source is None:
            raise NotFoundError("source")
        workspace_id, owner_id = source
        cur = await conn.execute(
            "SELECT id FROM credential WHERE source_id = %s FOR NO KEY UPDATE", (source_id,)
        )
        existing = await cur.fetchone()
        if existing is None:
            cur = await conn.execute("SELECT uuidv7()")
            new_id = await cur.fetchone()
            if new_id is None:
                raise RuntimeError("SELECT uuidv7() returned no row")
            credential_id: uuid.UUID = new_id[0]
        else:
            credential_id = existing[0]
        aad = associated_data(
            credential_id=credential_id,
            workspace_id=workspace_id,
            source_id=source_id,
            kind=kind,
        )
        sealed = _seal(keyring, aad, secrets)
        if existing is None:
            await conn.execute(
                "INSERT INTO credential (id, workspace_id, owner_id, source_id, kind, metadata,"
                " expires_at, key_id, nonce, ciphertext)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    credential_id,
                    workspace_id,
                    owner_id,
                    source_id,
                    kind,
                    Jsonb(dict(metadata)),
                    expires_at,
                    sealed.key_id,
                    sealed.nonce,
                    sealed.ciphertext,
                ),
            )
        else:
            await conn.execute(
                "UPDATE credential SET kind = %s, metadata = %s, expires_at = %s, key_id = %s,"
                " nonce = %s, ciphertext = %s, generation = generation + 1, updated_at = now(),"
                " refreshed_at = NULL"
                " WHERE id = %s",
                (
                    kind,
                    Jsonb(dict(metadata)),
                    expires_at,
                    sealed.key_id,
                    sealed.nonce,
                    sealed.ciphertext,
                    credential_id,
                ),
            )
        info = (await _fetch_by_id(conn, credential_id)).info
    log.info(
        "stored the %s credential of source %s (generation %d)",
        kind,
        source_id,
        info.generation,
        extra={"source_id": str(source_id), "credential_id": str(info.id)},
    )
    return info


async def put_api_key(
    conn: Conn, keyring: Keyring, *, source_id: uuid.UUID, api_key: SecretStr
) -> CredentialInfo:
    """Store ``api_key`` as the source's credential, replacing the one it had.

    Raises:
        InvalidCredentialError: the key is empty, too long, or holds whitespace.
        NotFoundError: no such source.
        CredentialKeyNotConfiguredError: ``credential_key`` is not set.
    """
    check_secret("api_key", api_key)
    return await _put(conn, keyring, source_id, "api_key", {"api_key": api_key}, {}, None)


async def put_oauth(
    conn: Conn,
    keyring: Keyring,
    *,
    source_id: uuid.UUID,
    tokens: OAuthTokens,
    client: OAuthClient | None = None,
) -> CredentialInfo:
    """Store OAuth tokens and the client they were issued to as the source's credential,
    replacing the one it had. Tokens without a refresh token work until they expire.

    Raises: as :func:`put_api_key`.
    """
    client = OAuthClient() if client is None else client
    return await _put(
        conn,
        keyring,
        source_id,
        "oauth",
        _oauth_secrets(tokens, client),
        _oauth_metadata(tokens, client),
        tokens.expires_at,
    )


async def delete_credential(conn: Conn, *, source_id: uuid.UUID) -> bool:
    """Delete the source's credential; ``False`` if it had none. Needs no key."""
    async with conn.transaction():
        cur = await conn.execute("DELETE FROM credential WHERE source_id = %s", (source_id,))
        deleted = cur.rowcount > 0
    if deleted:
        log.info("deleted the credential of source %s", source_id)
    return deleted


# --- Refreshing ----------------------------------------------------------------------------------


async def _store_refreshed(
    conn: Conn, keyring: Keyring, current: OAuthCredential, issued: OAuthTokens
) -> OAuthCredential:
    """Write the merged tokens as the next generation of ``current``'s row, which is locked."""
    tokens = OAuthTokens(
        access_token=issued.access_token,
        # A provider that issues no new refresh token keeps the old one valid (RFC 6749, 6).
        refresh_token=issued.refresh_token or current.tokens.refresh_token,
        expires_at=issued.expires_at,
    )
    info = current.info
    aad = associated_data(
        credential_id=info.id,
        workspace_id=info.workspace_id,
        source_id=info.source_id,
        kind="oauth",
    )
    sealed = _seal(keyring, aad, _oauth_secrets(tokens, current.client))
    cur = await conn.execute(
        "UPDATE credential SET metadata = %s, expires_at = %s, key_id = %s, nonce = %s,"
        " ciphertext = %s, generation = generation + 1, updated_at = now(), refreshed_at = now()"
        " WHERE id = %s AND generation = %s",
        (
            Jsonb(_oauth_metadata(tokens, current.client)),
            tokens.expires_at,
            sealed.key_id,
            sealed.nonce,
            sealed.ciphertext,
            info.id,
            info.generation,
        ),
    )
    if cur.rowcount != 1:  # the row is locked by this transaction
        raise RuntimeError(f"credential {info.id} changed while it was locked")
    row = await _fetch_by_id(conn, info.id)
    return OAuthCredential(row.info, tokens, current.client)


async def refresh_oauth(
    conn: Conn,
    keyring: Keyring,
    *,
    source_id: uuid.UUID,
    refresher: Refresher,
    seen_generation: int | None = None,
    timeout: float = DEFAULT_REFRESH_TIMEOUT,
) -> OAuthCredential:
    """Refresh the source's OAuth tokens through ``refresher`` and store the result.

    One transaction locks the credential's row, calls ``refresher`` (bounded by ``timeout``),
    writes the new tokens as the next generation and commits. So:

    - The old tokens stay stored until the new ones are committed: a failure or a crash anywhere
      before the commit leaves the credential as it was, and the next refresh starts again from the
      old refresh token.
    - Two refreshes of one credential never run at once. With ``seen_generation``, the generation
      the caller read, a refresh that waited for another one finds a newer generation and returns
      the stored tokens without calling the provider, rather than spending the old refresh token a
      second time (a provider that detects reuse revokes the whole grant). Without it, the
      provider is called in any case.

    What this cannot close: a provider that invalidates a refresh token when it is used, and a
    crash after its answer but before the commit. The new tokens existed only in memory, and the
    old refresh token is spent; the provider refuses the next refresh and the source must be
    authorized again. A failure that leaves the process running (the commit failed, the database
    went away) raises :class:`RefreshNotSavedError` holding the new tokens, for
    :func:`save_refreshed_tokens`. The window is the time between the provider's answer and the
    commit: one ``UPDATE``.

    Raises:
        ValueError: ``conn`` is inside a transaction; a savepoint is not a commit.
        CredentialKeyNotConfiguredError: no ``credential_key``; raised before the provider is
            called, as the new tokens could not be stored.
        CredentialNotFoundError, CredentialKindError: no OAuth credential for the source.
        RefreshUnavailableError: the credential has no refresh token.
        TimeoutError: ``refresher`` took longer than ``timeout``; the credential is unchanged.
        RefreshNotSavedError: the provider answered, but the new tokens could not be stored.
        Whatever ``refresher`` raises; the credential is unchanged.
    """
    _require_idle(conn, "refresh_oauth")
    keyring.current  # noqa: B018 - before the provider is called, not after
    issued: OAuthTokens | None = None
    base_generation = 0
    try:
        async with conn.transaction():
            row = await _fetch(conn, source_id, lock=True)
            if row is None:
                raise CredentialNotFoundError(source_id)
            current = _oauth(row, keyring)
            if seen_generation is not None and current.info.generation != seen_generation:
                return current  # refreshed or set again since the caller read it
            if current.tokens.refresh_token is None:
                raise RefreshUnavailableError(source_id)
            base_generation = current.info.generation
            async with asyncio.timeout(timeout):
                issued = await refresher(current)
            _register(issued.access_token, issued.refresh_token)
            refreshed = await _store_refreshed(conn, keyring, current, issued)
    except Exception as error:
        if issued is None:
            raise
        raise RefreshNotSavedError(source_id, base_generation, issued) from error
    log.info(
        "refreshed the OAuth credential of source %s (generation %d)",
        source_id,
        refreshed.info.generation,
        extra={"source_id": str(source_id), "credential_id": str(refreshed.info.id)},
    )
    return refreshed


async def save_refreshed_tokens(
    conn: Conn,
    keyring: Keyring,
    *,
    source_id: uuid.UUID,
    base_generation: int,
    tokens: OAuthTokens,
) -> OAuthCredential:
    """Store tokens a refresh got but could not store (:class:`RefreshNotSavedError`).

    They are written only if the credential is still at ``base_generation``, the generation the
    refresh started from. Otherwise something newer is stored (the failed commit did in fact
    commit, another refresh followed, or the credential was set again) and is returned unchanged.

    Raises:
        ValueError: ``conn`` is inside a transaction.
        CredentialNotFoundError, CredentialKindError: no OAuth credential for the source.
        CredentialKeyNotConfiguredError: no ``credential_key``.
    """
    _require_idle(conn, "save_refreshed_tokens")
    keyring.current  # noqa: B018
    _register(tokens.access_token, tokens.refresh_token)
    async with conn.transaction():
        row = await _fetch(conn, source_id, lock=True)
        if row is None:
            raise CredentialNotFoundError(source_id)
        current = _oauth(row, keyring)
        if current.info.generation != base_generation:
            return current
        saved = await _store_refreshed(conn, keyring, current, tokens)
    log.info(
        "stored the refreshed OAuth credential of source %s (generation %d)",
        source_id,
        saved.info.generation,
        extra={"source_id": str(source_id), "credential_id": str(saved.info.id)},
    )
    return saved


async def fresh_oauth(
    conn: Conn,
    keyring: Keyring,
    *,
    source_id: uuid.UUID,
    refresher: Refresher,
    margin: timedelta = DEFAULT_REFRESH_MARGIN,
    timeout: float = DEFAULT_REFRESH_TIMEOUT,
    now: Callable[[], datetime] | None = None,
) -> OAuthCredential:
    """The source's OAuth tokens, refreshed first when the access token expires within
    ``margin``.

    An access token without a known expiry is returned as it is; when the provider refuses it,
    call :func:`refresh_oauth` with the generation it came with. Concurrent callers refresh once:
    see ``seen_generation`` there.

    Raises: as :func:`refresh_oauth`.
    """
    _require_idle(conn, "fresh_oauth")
    async with conn.transaction():
        current = await read_oauth(conn, keyring, source_id=source_id)
    expires_at = current.tokens.expires_at
    moment = datetime.now(UTC) if now is None else now()
    if expires_at is None or expires_at - margin > moment:
        return current
    return await refresh_oauth(
        conn,
        keyring,
        source_id=source_id,
        refresher=refresher,
        seen_generation=current.info.generation,
        timeout=timeout,
    )
