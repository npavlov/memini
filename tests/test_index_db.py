"""The index fingerprint in Postgres (migration 0004): recorded, checked, refused, replaced.

Acceptance check of principle 9: a changed model makes search fail with a message that names the
rebuild command; the fingerprint the settings call for passes. Every value is synthetic.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow

from memini.db import connect
from memini.embeddings import EmbeddingSpec
from memini.index import (
    IndexFingerprint,
    IndexMismatchError,
    check_index,
    ensure_index,
    read_index_state,
    set_index_fingerprint,
)
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

REMOTE = EmbeddingSpec(
    provider="openai_compatible",
    model="synthetic-embed:v1",
    dimension=768,
    digest="synthetic-served-digest",
    document_prefix="search_document: ",
)
LOCAL = EmbeddingSpec(
    provider="local",
    model="synthetic-org/synthetic-model",
    dimension=384,
    digest="sha256:" + "ab" * 32,
    pooling="mean",
    max_tokens=512,
)
BUILT = IndexFingerprint(REMOTE, chunker_version="1")


@dataclass(frozen=True)
class Scope:
    owner: uuid.UUID
    acme: uuid.UUID
    other: uuid.UUID


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
async def scope(db: Conn) -> Scope:
    owner = await ensure_principal(db, "owner")
    acme = await create_workspace(db, owner_id=owner, name="acme")
    other = await create_workspace(db, owner_id=owner, name="other")
    return Scope(owner=owner, acme=acme.id, other=other.id)


async def test_nothing_indexed_yet_is_not_a_mismatch(db: Conn, scope: Scope) -> None:
    assert await read_index_state(db, scope.acme) is None
    assert await check_index(db, scope.acme, BUILT) is None


async def test_the_first_write_records_the_fingerprint(db: Conn, scope: Scope) -> None:
    state = await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    assert state.workspace == "acme"
    assert state.fingerprint == BUILT.digest
    assert state.built_with == BUILT
    assert await check_index(db, scope.acme, BUILT) == state
    assert await ensure_index(
        db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT
    ) == (state)


async def test_a_changed_model_blocks_search_and_names_the_rebuild_command(
    db: Conn, scope: Scope
) -> None:
    """Acceptance check: the message names `memini reindex`, the workspace, and both sides."""
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)
    configured = IndexFingerprint(
        replace(REMOTE, model="synthetic-embed:v2", dimension=1024), chunker_version="2"
    )

    with pytest.raises(IndexMismatchError) as info:
        await check_index(db, scope.acme, configured)

    error = info.value
    assert error.workspace == "acme"
    assert error.changed == ("model", "dimension", "chunker version")
    assert error.command == "memini reindex --workspace acme"
    assert str(error) == (
        "the index of workspace acme is out of date: it was built with model synthetic-embed:v1"
        " (openai_compatible, dimension 768, digest synthetic-served-digest, document prefix"
        " 'search_document: '), chunker 1, but the service is configured for model"
        " synthetic-embed:v2 (openai_compatible, dimension 1024, digest synthetic-served-digest,"
        " document prefix 'search_document: '), chunker 2 (changed: model, dimension, chunker"
        " version). Search and indexing in this workspace are blocked until the index is rebuilt:"
        " run `memini reindex --workspace acme`"
    )


@pytest.mark.parametrize(
    "configured",
    [
        IndexFingerprint(replace(REMOTE, digest="another-served-digest"), "1"),
        IndexFingerprint(replace(REMOTE, document_prefix=""), "1"),
        IndexFingerprint(REMOTE, "2"),
        IndexFingerprint(LOCAL, "1"),
    ],
)
async def test_any_changed_part_blocks_search(
    db: Conn, scope: Scope, configured: IndexFingerprint
) -> None:
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    with pytest.raises(IndexMismatchError, match=r"run `memini reindex --workspace acme`"):
        await check_index(db, scope.acme, configured)


async def test_writing_to_a_stale_index_is_refused_and_changes_nothing(
    db: Conn, scope: Scope
) -> None:
    before = await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    with pytest.raises(IndexMismatchError):
        await ensure_index(
            db, workspace_id=scope.acme, owner_id=scope.owner, expected=IndexFingerprint(LOCAL, "1")
        )

    assert await read_index_state(db, scope.acme) == before


async def test_a_rebuild_records_the_new_fingerprint_and_unblocks_search(
    db: Conn, scope: Scope
) -> None:
    first = await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)
    rebuilt = IndexFingerprint(LOCAL, chunker_version="2")

    state = await set_index_fingerprint(
        db, workspace_id=scope.acme, owner_id=scope.owner, fingerprint=rebuilt
    )

    assert state.built_with == rebuilt
    assert state.created_at == first.created_at
    assert state.updated_at >= first.updated_at
    assert await check_index(db, scope.acme, rebuilt) == state
    with pytest.raises(IndexMismatchError):
        await check_index(db, scope.acme, BUILT)


async def test_each_workspace_has_its_own_index_state(db: Conn, scope: Scope) -> None:
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)
    rebuilt = IndexFingerprint(LOCAL, "1")
    await set_index_fingerprint(
        db, workspace_id=scope.other, owner_id=scope.owner, fingerprint=rebuilt
    )

    assert await check_index(db, scope.other, rebuilt) is not None
    with pytest.raises(IndexMismatchError, match="workspace acme"):
        await check_index(db, scope.acme, rebuilt)


async def test_ensure_index_holds_a_share_lock_until_the_transaction_ends(
    migrated_database_url: str, db: Conn, scope: Scope
) -> None:
    """A rebuild cannot change the fingerprint under a writer that checked it."""
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    async with connect(migrated_database_url) as writer, connect(migrated_database_url) as rebuild:
        await ensure_index(writer, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)
        await rebuild.execute("SET lock_timeout = '100ms'")
        with pytest.raises(errors.LockNotAvailable):
            await set_index_fingerprint(
                rebuild,
                workspace_id=scope.acme,
                owner_id=scope.owner,
                fingerprint=IndexFingerprint(LOCAL, "1"),
            )
        await rebuild.rollback()
        await writer.commit()


async def test_deleting_a_workspace_deletes_its_index_state(db: Conn, scope: Scope) -> None:
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    await db.execute("DELETE FROM workspace WHERE id = %s", (scope.acme,))

    cur = await db.execute("SELECT count(*) FROM index_state")
    assert await cur.fetchone() == (0,)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fingerprint", "not-hex"),
        ("embedding_key", "A" * 64),
        ("provider", "cloud"),
        ("model", ""),
        ("dimension", 0),
        ("dimension", 16001),
        ("normalization", "none"),
        ("pooling", "max"),
        ("max_tokens", 0),
        ("chunker_version", ""),
    ],
)
async def test_checks_refuse_bad_values(db: Conn, scope: Scope, column: str, value: object) -> None:
    await ensure_index(
        db, workspace_id=scope.acme, owner_id=scope.owner, expected=IndexFingerprint(LOCAL, "1")
    )

    with pytest.raises(errors.CheckViolation):
        await update_state(db, scope.acme, {column: value})


@pytest.mark.parametrize(
    ("assignments", "constraint"),
    [
        ({"pooling": None, "max_tokens": None}, "index_state_local_check"),
        ({"pooling": None}, "index_state_local_check"),
        ({"model_digest": ""}, "index_state_model_digest_check"),
        ({"provider": "openai_compatible"}, "index_state_local_check"),
    ],
)
async def test_a_local_index_is_pinned_and_pooled(
    db: Conn, scope: Scope, assignments: dict[str, object], constraint: str
) -> None:
    await ensure_index(
        db, workspace_id=scope.acme, owner_id=scope.owner, expected=IndexFingerprint(LOCAL, "1")
    )

    with pytest.raises(errors.CheckViolation) as info:
        await update_state(db, scope.acme, assignments)

    assert info.value.diag.constraint_name == constraint


async def update_state(db: Conn, workspace_id: uuid.UUID, values: dict[str, object]) -> None:
    query = sql.SQL("UPDATE index_state SET {assignments} WHERE workspace_id = %s").format(
        assignments=sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(column)) for column in values
        )
    )
    await db.execute(query, [*values.values(), workspace_id])


async def test_one_row_per_workspace(db: Conn, scope: Scope) -> None:
    await ensure_index(db, workspace_id=scope.acme, owner_id=scope.owner, expected=BUILT)

    with pytest.raises(errors.UniqueViolation):
        await db.execute(
            "INSERT INTO index_state (workspace_id, owner_id, fingerprint, embedding_key, provider,"
            " model, dimension, normalization, chunker_version)"
            " SELECT workspace_id, owner_id, fingerprint, embedding_key, provider, model,"
            " dimension, normalization, chunker_version FROM index_state WHERE workspace_id = %s",
            (scope.acme,),
        )
