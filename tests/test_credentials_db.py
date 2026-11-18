"""The credential store against a real Postgres: nothing in clear text, rows bound to their
ciphertexts, refreshes that lose no token, and key rotation. Every key, token and name is synthetic.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Final

import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import SecretStr

from memini.credentials import (
    CredentialDecryptionError,
    CredentialKey,
    CredentialKeyNotConfiguredError,
    CredentialKeyUnavailableError,
    CredentialKindError,
    CredentialNotFoundError,
    Keyring,
    OAuthClient,
    OAuthCredential,
    OAuthTokens,
    Refresher,
    RefreshNotSavedError,
    RefreshUnavailableError,
    RotationBlockedError,
    delete_credential,
    fresh_oauth,
    get_credential_info,
    key_id,
    list_credentials,
    put_api_key,
    put_oauth,
    read_api_key,
    read_oauth,
    refresh_oauth,
    rotate_key,
    save_refreshed_tokens,
)
from memini.db import connect
from memini.logs import REDACTED
from memini.workspaces import NotFoundError

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

KEY_A: Final = bytes(range(32))
KEY_B: Final = bytes(range(64, 96))
KEY_C: Final = bytes(range(128, 160))
KEYRING_A: Final = Keyring(CredentialKey(KEY_A))
ENDPOINT: Final = "https://auth.example.test/oauth/token"


def secret(label: str) -> str:
    """A distinctive fake secret: easy to find in a scan, never a real token."""
    return f"fake-{label}-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class World:
    owner_id: uuid.UUID
    workspace_id: uuid.UUID
    sources: tuple[uuid.UUID, ...]


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection to a migrated database, in autocommit mode, as the service's pool lends."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def world(db: Conn) -> World:
    """One principal and workspace with five push sources."""
    cur = await db.execute("INSERT INTO principal (name) VALUES ('owner') RETURNING id")
    row = await cur.fetchone()
    assert row is not None
    owner_id: uuid.UUID = row[0]
    cur = await db.execute(
        "INSERT INTO workspace (owner_id, name) VALUES (%s, 'acme') RETURNING id", (owner_id,)
    )
    row = await cur.fetchone()
    assert row is not None
    workspace_id: uuid.UUID = row[0]
    sources: list[uuid.UUID] = []
    for n in range(5):
        cur = await db.execute(
            "INSERT INTO source (workspace_id, owner_id, name, kind) VALUES (%s, %s, %s, 'push')"
            " RETURNING id",
            (workspace_id, owner_id, f"source-{n}"),
        )
        row = await cur.fetchone()
        assert row is not None
        sources.append(row[0])
    return World(owner_id, workspace_id, tuple(sources))


# --- A fake token endpoint -----------------------------------------------------------------------


class ProviderRejected(Exception):
    """The fake provider refused a refresh token, as a real one answers invalid_grant."""


class SimulatedCrash(Exception):
    """Stands for the process dying: raised after the provider has answered."""


class FakeProvider:
    """A token endpoint that rotates refresh tokens.

    ``strict``: a refresh token is spent when used, so a second use is refused (rotation with reuse
    detection, as OAuth 2.1 advises). Otherwise the previous one stays valid, as with providers
    that rotate with a grace period or do not rotate at all.
    """

    def __init__(self, first_refresh: str, *, strict: bool) -> None:
        self.strict = strict
        self.valid: set[str] = {first_refresh}
        self.calls = 0
        self.issued: list[OAuthTokens] = []

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        self.calls += 1
        if refresh_token not in self.valid:
            raise ProviderRejected("invalid_grant")
        if self.strict:
            self.valid.discard(refresh_token)
        tokens = OAuthTokens(
            access_token=SecretStr(secret(f"access-{self.calls}")),
            refresh_token=SecretStr(secret(f"refresh-{self.calls}")),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert tokens.refresh_token is not None
        self.valid.add(tokens.refresh_token.get_secret_value())
        self.issued.append(tokens)
        return tokens


def refresher(provider: FakeProvider, *, crash_after_answer: bool = False) -> Refresher:
    async def refresh(credential: OAuthCredential) -> OAuthTokens:
        assert credential.tokens.refresh_token is not None
        tokens = await provider.refresh(credential.tokens.refresh_token.get_secret_value())
        if crash_after_answer:
            raise SimulatedCrash
        return tokens

    return refresh


async def put_fake_oauth(
    db: Conn, source_id: uuid.UUID, *, expires_in: timedelta = timedelta(hours=1)
) -> OAuthTokens:
    tokens = OAuthTokens(
        access_token=SecretStr(secret("access-0")),
        refresh_token=SecretStr(secret("refresh-0")),
        expires_at=datetime.now(UTC) + expires_in,
    )
    await put_oauth(
        db,
        KEYRING_A,
        source_id=source_id,
        tokens=tokens,
        client=OAuthClient(ENDPOINT, "client-1", SecretStr(secret("client-secret")), "read"),
    )
    return tokens


def value(secret_: SecretStr | None) -> str:
    assert secret_ is not None
    return secret_.get_secret_value()


# --- Put, read, delete ---------------------------------------------------------------------------


async def test_an_api_key_is_stored_read_and_replaced(db: Conn, world: World) -> None:
    source = world.sources[0]
    first, second = secret("api-key-1"), secret("api-key-2")

    info = await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(first))
    read = await read_api_key(db, KEYRING_A, source_id=source)
    replaced = await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(second))

    assert (info.kind, info.generation, info.key_id) == ("api_key", 1, key_id(KEY_A))
    assert (info.workspace, info.source, info.source_id) == ("acme", "source-0", source)
    assert read.api_key.get_secret_value() == first
    assert read.info == info
    assert replaced.id == info.id
    assert replaced.generation == 2
    assert value((await read_api_key(db, KEYRING_A, source_id=source)).api_key) == second


async def test_oauth_tokens_are_stored_with_their_client(db: Conn, world: World) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)

    read = await read_oauth(db, KEYRING_A, source_id=source)

    assert value(read.tokens.access_token) == value(tokens.access_token)
    assert value(read.tokens.refresh_token) == value(tokens.refresh_token)
    assert read.tokens.expires_at == tokens.expires_at
    assert (read.client.token_endpoint, read.client.client_id, read.client.scope) == (
        ENDPOINT,
        "client-1",
        "read",
    )
    assert read.client.client_secret is not None
    assert read.info.kind == "oauth"
    assert read.info.refreshable
    with pytest.raises(CredentialKindError):
        await read_api_key(db, KEYRING_A, source_id=source)


async def test_metadata_is_read_without_a_key(db: Conn, world: World) -> None:
    await put_api_key(db, KEYRING_A, source_id=world.sources[1], api_key=SecretStr(secret("k")))
    await put_fake_oauth(db, world.sources[0])

    listed = await list_credentials(db)
    info = await get_credential_info(db, source_id=world.sources[0])

    assert [(c.source, c.kind) for c in listed] == [("source-0", "oauth"), ("source-1", "api_key")]
    assert info is not None
    assert info.token_endpoint == ENDPOINT
    assert await get_credential_info(db, source_id=world.sources[2]) is None
    assert await list_credentials(db, workspace_id=uuid.uuid4()) == []


async def test_without_a_key_nothing_is_stored_and_the_setting_is_named(
    db: Conn, world: World
) -> None:
    await put_api_key(db, KEYRING_A, source_id=world.sources[1], api_key=SecretStr(secret("k")))
    keyless = Keyring(None)

    with pytest.raises(CredentialKeyNotConfiguredError, match="MEMINI_CREDENTIAL_KEY"):
        await put_api_key(db, keyless, source_id=world.sources[0], api_key=SecretStr("abcdefgh"))
    with pytest.raises(CredentialKeyNotConfiguredError):
        await read_api_key(db, keyless, source_id=world.sources[1])
    assert await get_credential_info(db, source_id=world.sources[0]) is None


async def test_delete_and_missing_sources(db: Conn, world: World) -> None:
    source = world.sources[0]
    await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(secret("k")))

    assert await delete_credential(db, source_id=source)
    assert not await delete_credential(db, source_id=source)
    with pytest.raises(CredentialNotFoundError):
        await read_api_key(db, KEYRING_A, source_id=source)
    with pytest.raises(NotFoundError):
        await put_api_key(db, KEYRING_A, source_id=uuid.uuid4(), api_key=SecretStr("abcdefgh"))


async def test_deleting_a_source_deletes_its_credential(db: Conn, world: World) -> None:
    source = world.sources[0]
    await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(secret("k")))

    await db.execute("DELETE FROM source WHERE id = %s", (source,))

    assert await list_credentials(db) == []


# --- Nothing in clear text -----------------------------------------------------------------------


async def _columns(db: Conn) -> list[tuple[str, str]]:
    cur = await db.execute(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c"
        " JOIN information_schema.tables t"
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
        " WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
        " ORDER BY c.table_name, c.ordinal_position"
    )
    return [(table, column) for table, column in await cur.fetchall()]


async def _holders(db: Conn, needle: str) -> list[str]:
    """``table.column`` of every column of every table that holds ``needle``, as text, as its UTF-8
    bytes (a bytea column's text is its hex), or in hex."""
    found: list[str] = []
    for table, column in await _columns(db):
        query = sql.SQL(
            "SELECT count(*) FROM {table} WHERE strpos({column}::text, %s) > 0"
            " OR strpos({column}::text, %s) > 0"
        ).format(table=sql.Identifier(table), column=sql.Identifier(column))
        cur = await db.execute(query, (needle, needle.encode().hex()))
        row = await cur.fetchone()
        if row is not None and row[0]:
            found.append(f"{table}.{column}")
    return found


async def test_the_database_holds_no_secret_in_clear_text(db: Conn, world: World) -> None:
    api_key = secret("api-key")
    await put_api_key(db, KEYRING_A, source_id=world.sources[1], api_key=SecretStr(api_key))
    tokens = await put_fake_oauth(db, world.sources[0])
    old = await read_oauth(db, KEYRING_A, source_id=world.sources[0])
    provider = FakeProvider(value(tokens.refresh_token), strict=True)
    refreshed = await refresh_oauth(
        db, KEYRING_A, source_id=world.sources[0], refresher=refresher(provider)
    )
    secrets = [
        api_key,
        value(tokens.access_token),
        value(tokens.refresh_token),
        value(old.client.client_secret),
        value(refreshed.tokens.access_token),
        value(refreshed.tokens.refresh_token),
    ]
    columns = await _columns(db)

    for needle in secrets:
        assert await _holders(db, needle) == [], "a secret is stored in clear text"
    # The key is not stored either, in any form.
    for needle in (KEY_A.hex(), CredentialKey(KEY_A).id + KEY_A.hex()[:8]):
        assert await _holders(db, needle) == []
    # The scan sees every column, the ciphertext included, and finds what is stored in clear.
    assert ("credential", "ciphertext") in columns
    assert ("credential", "metadata") in columns
    assert await _holders(db, ENDPOINT) == ["credential.metadata"]
    assert await _holders(db, "source-3") == ["source.name"]


# --- Bound to the right key and row --------------------------------------------------------------


async def test_another_key_cannot_read(db: Conn, world: World) -> None:
    source = world.sources[0]
    api_key = secret("api-key")
    await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(api_key))

    with pytest.raises(CredentialKeyUnavailableError) as unavailable:
        await read_api_key(db, Keyring(CredentialKey(KEY_B)), source_id=source)
    # The right id with the wrong key: the tag does not verify.
    await db.execute(
        "UPDATE credential SET key_id = %s WHERE source_id = %s", (key_id(KEY_B), source)
    )
    with pytest.raises(CredentialDecryptionError) as forged:
        await read_api_key(db, Keyring(CredentialKey(KEY_B)), source_id=source)

    assert key_id(KEY_A) in str(unavailable.value)
    for error in (unavailable.value, forged.value):
        assert api_key not in str(error)


async def test_a_ciphertext_copied_to_another_row_does_not_decrypt(db: Conn, world: World) -> None:
    first, second = world.sources[0], world.sources[1]
    first_key = secret("api-key-first")
    await put_api_key(db, KEYRING_A, source_id=first, api_key=SecretStr(first_key))
    await put_api_key(db, KEYRING_A, source_id=second, api_key=SecretStr(secret("second")))

    await db.execute(
        "UPDATE credential c SET key_id = f.key_id, nonce = f.nonce, ciphertext = f.ciphertext"
        " FROM credential f WHERE f.source_id = %s AND c.source_id = %s",
        (first, second),
    )

    with pytest.raises(CredentialDecryptionError) as caught:
        await read_api_key(db, KEYRING_A, source_id=second)
    assert first_key not in str(caught.value)
    assert value((await read_api_key(db, KEYRING_A, source_id=first)).api_key) == first_key


async def test_a_row_whose_kind_was_changed_does_not_decrypt(db: Conn, world: World) -> None:
    source = world.sources[0]
    await put_api_key(db, KEYRING_A, source_id=source, api_key=SecretStr(secret("k")))

    await db.execute("UPDATE credential SET kind = 'oauth' WHERE source_id = %s", (source,))

    with pytest.raises(CredentialDecryptionError):
        await read_oauth(db, KEYRING_A, source_id=source)


# --- Refresh -------------------------------------------------------------------------------------


async def test_a_refresh_stores_the_new_tokens_as_the_next_generation(
    db: Conn, world: World
) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=True)

    refreshed = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))
    stored = await read_oauth(db, KEYRING_A, source_id=source)

    issued = provider.issued[-1]
    assert refreshed.info.generation == stored.info.generation == 2
    assert value(stored.tokens.access_token) == value(issued.access_token)
    assert value(stored.tokens.refresh_token) == value(issued.refresh_token)
    assert stored.tokens.expires_at == issued.expires_at
    assert stored.info.refreshed_at is not None
    assert value(stored.client.client_secret) == value(refreshed.client.client_secret)


async def test_a_provider_that_issues_no_refresh_token_leaves_the_old_one(
    db: Conn, world: World
) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)

    async def access_only(credential: OAuthCredential) -> OAuthTokens:
        return OAuthTokens(access_token=SecretStr(secret("access-only")))

    refreshed = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=access_only)

    assert value(refreshed.tokens.refresh_token) == value(tokens.refresh_token)
    stored = await read_oauth(db, KEYRING_A, source_id=source)
    assert value(stored.tokens.refresh_token) == value(tokens.refresh_token)
    assert stored.tokens.expires_at is None


async def test_a_crash_after_the_provider_answered_loses_no_token(db: Conn, world: World) -> None:
    """The provider rotated the refresh token and the process died before the commit. The old
    pair is still stored, and the next refresh starts from it."""
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=False)

    with pytest.raises(SimulatedCrash):
        await refresh_oauth(
            db,
            KEYRING_A,
            source_id=source,
            refresher=refresher(provider, crash_after_answer=True),
        )
    kept = await read_oauth(db, KEYRING_A, source_id=source)
    again = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))
    then = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))

    assert provider.calls == 3
    assert kept.info.generation == 1
    assert value(kept.tokens.refresh_token) == value(tokens.refresh_token)
    assert again.info.generation == 2
    assert value(again.tokens.refresh_token) == value(provider.issued[1].refresh_token)
    assert then.info.generation == 3


async def test_the_window_that_remains_with_a_strict_provider(db: Conn, world: World) -> None:
    """A provider that spends a refresh token on use, and a crash between its answer and the
    commit: the new tokens are gone, the provider refuses the old one, and the source must be
    authorized again. The stored credential is the old one, unharmed; this is the documented
    limit of :func:`refresh_oauth`."""
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=True)

    with pytest.raises(SimulatedCrash):
        await refresh_oauth(
            db,
            KEYRING_A,
            source_id=source,
            refresher=refresher(provider, crash_after_answer=True),
        )
    with pytest.raises(ProviderRejected):
        await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))

    kept = await read_oauth(db, KEYRING_A, source_id=source)
    assert kept.info.generation == 1
    assert value(kept.tokens.refresh_token) == value(tokens.refresh_token)


async def test_a_lost_connection_after_the_answer_hands_back_the_tokens_to_save(
    db: Conn, world: World, migrated_database_url: str
) -> None:
    """The database connection dies while the provider answers: the commit cannot happen, the
    old pair stays, and the new tokens come back in the error to be saved on a new connection.
    Even a strict provider then accepts the next refresh."""
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=True)

    async with connect(migrated_database_url, autocommit=True) as doomed:
        pid = doomed.info.backend_pid

        async def answer_then_lose_the_database(credential: OAuthCredential) -> OAuthTokens:
            issued = await refresher(provider)(credential)
            await db.execute("SELECT pg_terminate_backend(%s)", (pid,))
            return issued

        with pytest.raises(RefreshNotSavedError) as caught:
            await refresh_oauth(
                doomed, KEYRING_A, source_id=source, refresher=answer_then_lose_the_database
            )

    error = caught.value
    kept = await read_oauth(db, KEYRING_A, source_id=source)
    saved = await save_refreshed_tokens(
        db, KEYRING_A, source_id=source, base_generation=error.base_generation, tokens=error.tokens
    )
    again = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))

    assert kept.info.generation == error.base_generation == 1
    assert value(error.tokens.refresh_token) == value(provider.issued[0].refresh_token)
    assert value(error.tokens.access_token) not in str(error)
    assert saved.info.generation == 2
    assert again.info.generation == 3
    assert provider.calls == 2


async def test_saving_late_tokens_never_overwrites_a_newer_generation(
    db: Conn, world: World
) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=False)
    newer = await refresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))
    late = OAuthTokens(SecretStr(secret("late-access")), SecretStr(secret("late-refresh")))

    kept = await save_refreshed_tokens(
        db, KEYRING_A, source_id=source, base_generation=1, tokens=late
    )

    assert kept.info.generation == newer.info.generation == 2
    assert value(kept.tokens.access_token) == value(newer.tokens.access_token)


async def test_concurrent_refreshes_spend_the_refresh_token_once(
    db: Conn, world: World, migrated_database_url: str
) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(credential: OAuthCredential) -> OAuthTokens:
        entered.set()
        await release.wait()
        return await refresher(provider)(credential)

    async with (
        connect(migrated_database_url, autocommit=True) as first,
        connect(migrated_database_url, autocommit=True) as second,
    ):
        seen = (await read_oauth(db, KEYRING_A, source_id=source)).info.generation
        one = asyncio.create_task(
            refresh_oauth(first, KEYRING_A, source_id=source, refresher=slow, seen_generation=seen)
        )
        await entered.wait()
        other = asyncio.create_task(
            refresh_oauth(second, KEYRING_A, source_id=source, refresher=slow, seen_generation=seen)
        )
        await _until_waiting_for_a_lock(db, second.info.backend_pid)
        release.set()
        results = await asyncio.gather(one, other)

    assert provider.calls == 1
    assert [r.info.generation for r in results] == [2, 2]
    assert len({value(r.tokens.access_token) for r in results}) == 1
    assert len({value(r.tokens.refresh_token) for r in results}) == 1


async def _until_waiting_for_a_lock(db: Conn, pid: int) -> None:
    for _ in range(500):
        cur = await db.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        )
        row = await cur.fetchone()
        if row is not None and row[0] == "Lock":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the second refresh never waited for the row lock")


async def test_fresh_oauth_refreshes_only_near_expiry(db: Conn, world: World) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source, expires_in=timedelta(hours=1))
    provider = FakeProvider(value(tokens.refresh_token), strict=True)

    current = await fresh_oauth(db, KEYRING_A, source_id=source, refresher=refresher(provider))
    later = await fresh_oauth(
        db,
        KEYRING_A,
        source_id=source,
        refresher=refresher(provider),
        now=lambda: datetime.now(UTC) + timedelta(minutes=59),
    )

    assert current.info.generation == 1
    assert later.info.generation == 2
    assert provider.calls == 1


async def test_refresh_refusals_call_no_provider(db: Conn, world: World) -> None:
    oauth, api_key, bare = world.sources[0], world.sources[1], world.sources[2]
    tokens = await put_fake_oauth(db, oauth)
    await put_api_key(db, KEYRING_A, source_id=api_key, api_key=SecretStr(secret("k")))
    await put_oauth(
        db, KEYRING_A, source_id=bare, tokens=OAuthTokens(SecretStr(secret("access-only")))
    )
    provider = FakeProvider(value(tokens.refresh_token), strict=True)
    refresh = refresher(provider)

    with pytest.raises(CredentialKeyNotConfiguredError):
        await refresh_oauth(db, Keyring(None), source_id=oauth, refresher=refresh)
    with pytest.raises(CredentialKindError):
        await refresh_oauth(db, KEYRING_A, source_id=api_key, refresher=refresh)
    with pytest.raises(RefreshUnavailableError, match="authorize the source again"):
        await refresh_oauth(db, KEYRING_A, source_id=bare, refresher=refresh)
    with pytest.raises(CredentialNotFoundError):
        await refresh_oauth(db, KEYRING_A, source_id=world.sources[3], refresher=refresh)
    async with db.transaction():
        with pytest.raises(ValueError, match="outside a transaction"):
            await refresh_oauth(db, KEYRING_A, source_id=oauth, refresher=refresh)

    assert provider.calls == 0


async def test_a_refresh_that_times_out_changes_nothing(db: Conn, world: World) -> None:
    source = world.sources[0]
    await put_fake_oauth(db, source)

    async def hang(credential: OAuthCredential) -> OAuthTokens:
        await asyncio.sleep(60)
        raise AssertionError("not reached")

    with pytest.raises(TimeoutError):
        await refresh_oauth(db, KEYRING_A, source_id=source, refresher=hang, timeout=0.05)

    assert (await read_oauth(db, KEYRING_A, source_id=source)).info.generation == 1


# --- Rotation ------------------------------------------------------------------------------------


async def _put_keys(db: Conn, world: World, keyring: Keyring) -> dict[uuid.UUID, str]:
    keys: dict[uuid.UUID, str] = {}
    for source in world.sources:
        keys[source] = secret("api-key")
        await put_api_key(db, keyring, source_id=source, api_key=SecretStr(keys[source]))
    return keys


async def _state(db: Conn) -> list[tuple[uuid.UUID, str, int, datetime]]:
    cur = await db.execute(
        "SELECT source_id, key_id, generation, updated_at FROM credential ORDER BY source_id"
    )
    return list(await cur.fetchall())


async def test_rotation_re_encrypts_every_row_and_the_old_key_is_no_longer_needed(
    db: Conn, world: World
) -> None:
    keys = await _put_keys(db, world, KEYRING_A)
    before = await _state(db)
    rotating = Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)])

    result = await rotate_key(db, rotating, batch_size=2)
    again = await rotate_key(db, rotating, batch_size=2)

    assert (result.rotated, result.already, result.remaining, result.unreadable) == (5, 0, 0, ())
    assert result.complete
    assert (again.rotated, again.already, again.remaining) == (0, 5, 0)
    after = await _state(db)
    assert {row[1] for row in after} == {key_id(KEY_B)}
    # The secret, its generation and its time of change stay as they were.
    assert [(s, g, u) for s, _, g, u in after] == [(s, g, u) for s, _, g, u in before]
    only_b = Keyring(CredentialKey(KEY_B))
    for source, key in keys.items():
        assert value((await read_api_key(db, only_b, source_id=source)).api_key) == key
        with pytest.raises(CredentialKeyUnavailableError):
            await read_api_key(db, KEYRING_A, source_id=source)


async def test_rotation_resumes_where_it_stopped(
    db: Conn, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _put_keys(db, world, KEYRING_A)
    rotating = Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)])
    seal = CredentialKey.seal
    calls = 0

    def failing_seal(self: CredentialKey, plaintext: bytes, aad: bytes) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SimulatedCrash
        return seal(self, plaintext, aad)

    monkeypatch.setattr(CredentialKey, "seal", failing_seal)
    with pytest.raises(SimulatedCrash):
        await rotate_key(db, rotating, batch_size=2)
    monkeypatch.setattr(CredentialKey, "seal", seal)
    halfway = [row[1] for row in await _state(db)]
    result = await rotate_key(db, rotating, batch_size=2)

    assert sorted(halfway).count(key_id(KEY_B)) == 2  # the first batch committed
    assert (result.rotated, result.already, result.remaining) == (3, 2, 0)


async def test_rotation_refuses_rows_under_a_key_it_lacks(db: Conn, world: World) -> None:
    await _put_keys(db, world, KEYRING_A)
    before = await _state(db)

    with pytest.raises(RotationBlockedError) as caught:
        await rotate_key(db, Keyring(CredentialKey(KEY_C), [CredentialKey(KEY_B)]))

    assert caught.value.missing == {key_id(KEY_A): 5}
    assert "MEMINI_CREDENTIAL_OLD_KEYS" in str(caught.value)
    assert await _state(db) == before


async def test_rotation_skips_and_reports_an_unreadable_row(db: Conn, world: World) -> None:
    await _put_keys(db, world, KEYRING_A)
    broken = world.sources[2]
    await db.execute(
        "UPDATE credential SET ciphertext = set_byte(ciphertext, 0, get_byte(ciphertext, 0) # 1)"
        " WHERE source_id = %s",
        (broken,),
    )
    info = await get_credential_info(db, source_id=broken)
    assert info is not None

    result = await rotate_key(db, Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)]))

    assert (result.rotated, result.unreadable, result.remaining) == (4, (info.id,), 1)
    assert not result.complete


async def test_a_refresh_after_the_key_changed_writes_under_the_new_key(
    db: Conn, world: World
) -> None:
    source = world.sources[0]
    tokens = await put_fake_oauth(db, source)
    provider = FakeProvider(value(tokens.refresh_token), strict=True)
    rotating = Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)])

    refreshed = await refresh_oauth(db, rotating, source_id=source, refresher=refresher(provider))

    assert refreshed.info.key_id == key_id(KEY_B)


# --- Logs ----------------------------------------------------------------------------------------


@pytest.fixture
def every_record() -> Iterator[list[logging.LogRecord]]:
    """Every log record of the process during the test, at every level, unformatted."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler, level = Keep(logging.DEBUG), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


async def test_secrets_reach_no_log_and_no_error(
    db: Conn, world: World, every_record: list[logging.LogRecord]
) -> None:
    api_key = secret("api-key")
    await put_api_key(db, KEYRING_A, source_id=world.sources[1], api_key=SecretStr(api_key))
    tokens = await put_fake_oauth(db, world.sources[0])
    provider = FakeProvider(value(tokens.refresh_token), strict=True)
    refreshed = await refresh_oauth(
        db, KEYRING_A, source_id=world.sources[0], refresher=refresher(provider)
    )
    errors: list[BaseException] = []
    for failing in (
        read_api_key(db, Keyring(CredentialKey(KEY_B)), source_id=world.sources[1]),
        read_api_key(db, KEYRING_A, source_id=world.sources[0]),
        refresh_oauth(
            db,
            KEYRING_A,
            source_id=world.sources[0],
            refresher=refresher(FakeProvider("none", strict=True)),
        ),
    ):
        try:
            await failing
        except Exception as error:
            errors.append(error)
    await rotate_key(db, Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)]))
    secrets = [
        api_key,
        value(tokens.access_token),
        value(tokens.refresh_token),
        value(refreshed.tokens.access_token),
        value(refreshed.tokens.refresh_token),
    ]

    logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in every_record)
    texts = " ".join(f"{error!s} {error!r}" for error in errors)
    assert len(errors) == 3
    assert "refreshed the OAuth credential" in logged
    for needle in secrets:
        assert needle not in logged
        assert needle not in texts


async def test_a_secret_logged_by_mistake_is_masked(
    db: Conn, world: World, log_output: StringIO
) -> None:
    await put_fake_oauth(db, world.sources[0])
    oauth = await read_oauth(db, KEYRING_A, source_id=world.sources[0])
    token = value(oauth.tokens.access_token)

    logging.getLogger("memini.test").warning("sending %s", token)

    assert token not in log_output.getvalue()
    assert REDACTED in log_output.getvalue()
