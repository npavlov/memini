"""The core schema (migration 0002) against a real Postgres: scoping, indexes and constraints."""

import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from memini.db import connect
from memini.ids import RECORD_ID_DOMAIN, hash_fields, record_id

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]
type Row = dict[str, object]

OCCURRED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LATER = OCCURRED + timedelta(minutes=5)

# --- Scoping: every table carries workspace_id and owner_id (requirements, section 8) ------------

SCOPING_COLUMNS = ("owner_id", "workspace_id")

# Tables that may lack a scoping column, and why. Every other table must carry both. Keep it short;
# an entry here is a table the authorization filter cannot narrow by workspace or owner.
SCOPING_EXEMPT: dict[str, frozenset[str]] = {
    # Bookkeeping of the migration runner (memini.db.migrate), not data.
    "schema_migration": frozenset({"workspace_id", "owner_id"}),
    # A principal owns rows in several workspaces and is itself the owner.
    "principal": frozenset({"workspace_id", "owner_id"}),
    # The id of a workspace is the workspace_id of the rows inside it; it does have an owner_id.
    "workspace": frozenset({"workspace_id"}),
}


async def scoping_violations(conn: Conn) -> list[str]:
    """`table.column` for each scoping column a table of the public schema lacks, unless exempt."""
    cur = await conn.execute(
        "SELECT c.relname, a.attname"
        " FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND NOT a.attisdropped"
        "  AND a.attname IN ('workspace_id', 'owner_id')"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"  # tables, partitioned tables
    )
    present: dict[str, set[str]] = {}
    for table, column in await cur.fetchall():
        columns = present.setdefault(table, set())
        if column is not None:
            columns.add(column)
    return [
        f"{table}.{column}"
        for table, columns in sorted(present.items())
        for column in SCOPING_COLUMNS
        if column not in columns and column not in SCOPING_EXEMPT.get(table, frozenset())
    ]


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    """A connection to a migrated database. Autocommit: a refused statement spoils nothing."""
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


async def test_every_table_carries_workspace_and_owner(db: Conn) -> None:
    assert await scoping_violations(db) == []

    for table, exempt in SCOPING_EXEMPT.items():
        cur = await db.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        columns = {name for (name,) in await cur.fetchall()}
        assert columns, f"{table} is exempt but does not exist"
        assert not exempt & columns, f"{table} is exempt from a column it has"


async def test_the_scoping_check_reports_a_table_without_them(db: Conn) -> None:
    await db.execute("CREATE TABLE stray (id integer)")
    await db.execute("CREATE TABLE half_scoped (id integer, workspace_id uuid)")

    assert await scoping_violations(db) == [
        "half_scoped.owner_id",
        "stray.owner_id",
        "stray.workspace_id",
    ]


# --- Indexes for the lookups the tables serve (sections 4, 6 and 8) ------------------------------

# (lookup, table, index columns in order, unique)
LOOKUPS: list[tuple[str, str, tuple[str, ...], bool]] = [
    ("token by hash, on every request", "token", ("hash",), True),
    ("principal by name", "principal", ("name",), True),
    ("workspace by name", "workspace", ("name",), True),
    ("sources of a workspace, source by name", "source", ("workspace_id", "name"), True),
    ("streams of a source, stream by key", "stream", ("source_id", "key"), True),
    ("record by id: get_thread, get_meeting", "record", ("id",), True),
    (
        "record by external id: upsert, tombstone",
        "record",
        ("source_id", "kind", "external_id"),
        True,
    ),
    ("records of a source by age: retention", "record", ("source_id", "occurred_at"), False),
    (
        "records by kind and time: list_meetings, list_messages",
        "record",
        ("workspace_id", "kind", "occurred_at"),
        False,
    ),
    ("records of a stream: snapshot reconcile", "record", ("stream_id",), False),
    ("me in a workspace", "person", ("workspace_id", "principal_id"), True),
    (
        "person by alias: resolution",
        "alias",
        ("workspace_id", "kind", "normalized_value"),
        True,
    ),
    ("aliases of a person: show, merge", "alias", ("person_id",), False),
    ("runs of a workspace by time", "run", ("workspace_id", "started_at"), False),
    ("runs of a source by time", "run", ("source_id", "started_at"), False),
    ("runs of a stream by time: last result in status", "run", ("stream_id", "started_at"), False),
]


async def _indexes(conn: Conn) -> set[tuple[str, tuple[str, ...], bool]]:
    """(table, columns, unique) of every plain index in the public schema."""
    cur = await conn.execute(
        "SELECT t.relname, ix.indisunique, array("
        "   SELECT a.attname FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)"
        "   JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum"
        "   ORDER BY k.ord)"
        " FROM pg_index ix"
        " JOIN pg_class t ON t.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " WHERE n.nspname = 'public' AND ix.indpred IS NULL AND ix.indexprs IS NULL"
    )
    return {(table, tuple(columns), unique) for table, unique, columns in await cur.fetchall()}


async def test_indexes_serve_the_lookups(db: Conn) -> None:
    indexes = await _indexes(db)

    missing = [
        f"{lookup}: {table} ({', '.join(columns)})"
        for lookup, table, columns, unique in LOOKUPS
        if (table, columns, unique) not in indexes
    ]
    assert missing == []


# --- Constraints -------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """One principal with two workspaces, a and b, each holding a few rows."""

    owner: uuid.UUID
    a: uuid.UUID
    b: uuid.UUID
    source_a: uuid.UUID  # push source in a, with stream_a
    source_a2: uuid.UUID  # git source in a, no streams
    source_b: uuid.UUID  # push source in b
    stream_a: uuid.UUID
    person_a: uuid.UUID  # "me" in a
    person_b: uuid.UUID


async def _insert(conn: Conn, table: str, row: Mapping[str, object]) -> Any:
    """Insert `row` into `table`; return its id."""
    query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING id").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in row),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in row),
    )
    cur = await conn.execute(query, list(row.values()))
    inserted = await cur.fetchone()
    assert inserted is not None
    return inserted[0]


def _handle(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def workspace_row(w: World, **columns: object) -> Row:
    return {"owner_id": w.owner, "name": _handle("ws")} | columns


def source_row(w: World, **columns: object) -> Row:
    row: Row = {"workspace_id": w.a, "owner_id": w.owner, "name": _handle("src"), "kind": "push"}
    return row | columns


def stream_row(w: World, **columns: object) -> Row:
    row: Row = {
        "workspace_id": w.a,
        "owner_id": w.owner,
        "source_id": w.source_a,
        "key": _handle("channel"),
    }
    return row | columns


def token_row(w: World, **columns: object) -> Row:
    row: Row = {
        "owner_id": w.owner,
        "name": "test agent",
        "hash": secrets.token_bytes(32),
        "prefix": "mem_abcd",
        "scopes": ["read"],
    }
    return row | columns


def record_row(
    w: World,
    *,
    source_id: uuid.UUID | None = None,
    kind: str = "thread",
    external_id: str | None = None,
    **columns: object,
) -> Row:
    """A record whose id is computed for any kind, valid or not, so a test can break one field."""
    source = w.source_a if source_id is None else source_id
    external = _handle("thread") if external_id is None else external_id
    row: Row = {
        "id": hash_fields(RECORD_ID_DOMAIN, source.bytes, kind.encode(), external.encode()),
        "workspace_id": w.a,
        "owner_id": w.owner,
        "source_id": source,
        "kind": kind,
        "external_id": external,
        "content_hash": "0" * 64,
        "occurred_at": OCCURRED,
        "payload": None if kind == "code_file" else b'{"text": "synthetic"}',
    }
    return row | columns


def person_row(w: World, **columns: object) -> Row:
    return {"workspace_id": w.a, "owner_id": w.owner, "display_name": "Person Two"} | columns


def alias_row(w: World, **columns: object) -> Row:
    value = f"{_handle('person')}@example.test"
    row: Row = {
        "workspace_id": w.a,
        "owner_id": w.owner,
        "person_id": w.person_a,
        "kind": "email",
        "value": value,
        "normalized_value": value,
    }
    return row | columns


def run_row(w: World, **columns: object) -> Row:
    row: Row = {
        "workspace_id": w.a,
        "owner_id": w.owner,
        "source_id": w.source_a,
        "stream_id": w.stream_a,
        "kind": "sync",
    }
    return row | columns


@pytest.fixture
async def world(db: Conn) -> World:
    """Seed the rows of `World`, plus one valid row of every other table built by the helpers."""
    owner = await _insert(db, "principal", {"name": "me"})
    a = await _insert(db, "workspace", {"owner_id": owner, "name": "alpha"})
    b = await _insert(db, "workspace", {"owner_id": owner, "name": "beta"})
    scoped = {"owner_id": owner}
    source_a = await _insert(
        db, "source", {**scoped, "workspace_id": a, "name": "chat", "kind": "push"}
    )
    source_a2 = await _insert(
        db, "source", {**scoped, "workspace_id": a, "name": "repo", "kind": "git"}
    )
    source_b = await _insert(
        db, "source", {**scoped, "workspace_id": b, "name": "chat", "kind": "push"}
    )
    stream_a = await _insert(
        db, "stream", {**scoped, "workspace_id": a, "source_id": source_a, "key": "general"}
    )
    person_a = await _insert(
        db,
        "person",
        {**scoped, "workspace_id": a, "display_name": "Person One", "principal_id": owner},
    )
    person_b = await _insert(
        db, "person", {**scoped, "workspace_id": b, "display_name": "Person Three"}
    )
    w = World(owner, a, b, source_a, source_a2, source_b, stream_a, person_a, person_b)

    # The helpers build valid rows: proven once here, so each case below breaks exactly one thing.
    await _insert(db, "token", token_row(w))
    await _insert(db, "token", token_row(w, workspace_id=b, scopes=["read", "ingest", "admin"]))
    await _insert(db, "record", record_row(w, stream_id=stream_a))
    await _insert(db, "record", record_row(w, source_id=source_a2, kind="code_file"))
    await _insert(db, "alias", alias_row(w))
    await _insert(db, "run", run_row(w))
    await _insert(db, "run", run_row(w, status="succeeded", started_at=OCCURRED, finished_at=LATER))
    await _insert(db, "run", run_row(w, source_id=None, stream_id=None, kind="maintenance"))
    return w


# (case, table, row builder, constraint that must refuse the row)
BAD_VALUES: list[tuple[str, str, Callable[[World], Row], str]] = [
    (
        "workspace-name",
        "workspace",
        lambda w: workspace_row(w, name="Two Words"),
        "workspace_name_check",
    ),
    ("source-name", "source", lambda w: source_row(w, name="my/source"), "source_name_check"),
    ("source-kind", "source", lambda w: source_row(w, kind="svn"), "source_kind_check"),
    (
        "source-visibility",
        "source",
        lambda w: source_row(w, visibility="public"),
        "source_visibility_check",
    ),
    (
        "source-llm-policy",
        "source",
        lambda w: source_row(w, llm_policy="remote"),
        "source_llm_policy_check",
    ),
    (
        "source-retention",
        "source",
        lambda w: source_row(w, retention_days=0),
        "source_retention_days_check",
    ),
    ("source-config", "source", lambda w: source_row(w, config=Jsonb([])), "source_config_check"),
    ("stream-key", "stream", lambda w: stream_row(w, key=""), "stream_key_check"),
    (
        "stream-schedule",
        "stream",
        lambda w: stream_row(w, schedule=timedelta(0)),
        "stream_schedule_check",
    ),
    (
        "token-scope",
        "token",
        lambda w: token_row(w, scopes=["read", "write"]),
        "token_scopes_check",
    ),
    ("token-no-scopes", "token", lambda w: token_row(w, scopes=[]), "token_scopes_check"),
    ("token-hash", "token", lambda w: token_row(w, hash=b"short"), "token_hash_check"),
    (
        "token-expiry",
        "token",
        lambda w: token_row(w, expires_at=datetime(2000, 1, 1, tzinfo=UTC)),
        "token_expires_at_check",
    ),
    ("record-kind", "record", lambda w: record_row(w, kind="email"), "record_kind_check"),
    (
        "record-id",
        "record",
        lambda w: record_row(w, id=record_id(w.source_a, "thread", "another")),
        "record_id_check",
    ),
    (
        "record-code-file-content",
        "record",
        lambda w: record_row(w, kind="code_file", payload=b"print('hello')\n"),
        "record_code_file_check",
    ),
    ("alias-kind", "alias", lambda w: alias_row(w, kind="phone"), "alias_kind_check"),
    (
        "person-name",
        "person",
        lambda w: person_row(w, display_name=""),
        "person_display_name_check",
    ),
    ("run-kind", "run", lambda w: run_row(w, kind="Sync now"), "run_kind_check"),
    (
        "run-status",
        "run",
        lambda w: run_row(w, status="done", started_at=OCCURRED, finished_at=LATER),
        "run_status_check",
    ),
    (
        "run-finished-without-time",
        "run",
        lambda w: run_row(w, status="succeeded"),
        "run_finished_at_check",
    ),
    (
        "run-running-with-finish-time",
        "run",
        lambda w: run_row(w, started_at=OCCURRED, finished_at=LATER),
        "run_finished_at_check",
    ),
    (
        "run-finished-before-start",
        "run",
        lambda w: run_row(w, status="failed", finished_at=datetime(2000, 1, 1, tzinfo=UTC)),
        "run_finished_at_check",
    ),
    (
        "run-stream-without-source",
        "run",
        lambda w: run_row(w, source_id=None),
        "run_stream_id_check",
    ),
    ("run-report", "run", lambda w: run_row(w, report=Jsonb([])), "run_report_check"),
]


@pytest.mark.parametrize(
    ("table", "build", "constraint"),
    [
        pytest.param(table, build, constraint, id=case)
        for case, table, build, constraint in BAD_VALUES
    ],
)
async def test_checks_refuse_bad_values(
    db: Conn, world: World, table: str, build: Callable[[World], Row], constraint: str
) -> None:
    with pytest.raises(errors.CheckViolation) as refused:
        await _insert(db, table, build(world))

    assert refused.value.diag.constraint_name == constraint


# (case, table, row builder, foreign key that must refuse the row)
CROSSING: list[tuple[str, str, Callable[[World], Row], str]] = [
    (
        "stream-in-b-on-a-source-of-a",
        "stream",
        lambda w: stream_row(w, workspace_id=w.b),
        "stream_workspace_id_source_id_fkey",
    ),
    (
        "record-in-b-from-a-source-of-a",
        "record",
        lambda w: record_row(w, workspace_id=w.b),
        "record_workspace_id_source_id_fkey",
    ),
    (
        "record-through-a-stream-of-another-source",
        "record",
        lambda w: record_row(w, source_id=w.source_a2, stream_id=w.stream_a),
        "record_source_id_stream_id_fkey",
    ),
    (
        "alias-in-b-for-a-person-of-a",
        "alias",
        lambda w: alias_row(w, workspace_id=w.b),
        "alias_workspace_id_person_id_fkey",
    ),
    (
        "run-in-b-on-a-source-of-a",
        "run",
        lambda w: run_row(w, workspace_id=w.b, stream_id=None),
        "run_workspace_id_source_id_fkey",
    ),
    (
        "run-on-a-stream-of-another-source",
        "run",
        lambda w: run_row(w, source_id=w.source_a2),
        "run_source_id_stream_id_fkey",
    ),
]


@pytest.mark.parametrize(
    ("table", "build", "constraint"),
    [
        pytest.param(table, build, constraint, id=case)
        for case, table, build, constraint in CROSSING
    ],
)
async def test_rows_cannot_point_into_another_workspace_or_source(
    db: Conn, world: World, table: str, build: Callable[[World], Row], constraint: str
) -> None:
    with pytest.raises(errors.ForeignKeyViolation) as refused:
        await _insert(db, table, build(world))

    assert refused.value.diag.constraint_name == constraint


async def test_at_most_one_me_per_workspace(db: Conn, world: World) -> None:
    await _insert(db, "person", person_row(world))
    await _insert(db, "person", person_row(world))
    await _insert(db, "person", person_row(world, workspace_id=world.b, principal_id=world.owner))

    with pytest.raises(errors.UniqueViolation) as refused:
        await _insert(db, "person", person_row(world, principal_id=world.owner))
    assert refused.value.diag.constraint_name == "person_workspace_id_principal_id_key"


async def test_an_alias_resolves_to_one_person_per_workspace_and_kind(
    db: Conn, world: World
) -> None:
    key = "person.one@example.test"
    await _insert(
        db, "alias", alias_row(world, value="Person.One@Example.test", normalized_value=key)
    )
    await _insert(db, "alias", alias_row(world, kind="git_author", value=key, normalized_value=key))
    await _insert(
        db,
        "alias",
        alias_row(world, workspace_id=world.b, person_id=world.person_b, normalized_value=key),
    )
    other = await _insert(db, "person", person_row(world))

    with pytest.raises(errors.UniqueViolation) as refused:
        await _insert(db, "alias", alias_row(world, person_id=other, normalized_value=key))
    assert refused.value.diag.constraint_name == "alias_workspace_id_kind_normalized_value_key"


async def test_a_token_hash_is_stored_once(db: Conn, world: World) -> None:
    digest = secrets.token_bytes(32)
    await _insert(db, "token", token_row(world, hash=digest))

    with pytest.raises(errors.UniqueViolation) as refused:
        await _insert(db, "token", token_row(world, hash=digest, workspace_id=world.b))
    assert refused.value.diag.constraint_name == "token_hash_key"


async def test_the_payload_is_stored_byte_for_byte(db: Conn, world: World) -> None:
    # Duplicate keys, key order, odd whitespace, a NUL byte and a byte that is not UTF-8: jsonb or
    # text would lose or refuse some of it.
    payload = b'{"b": 1,   "a": 2, "a": 3}\x00\xff\n'
    rid = await _insert(db, "record", record_row(world, payload=payload))

    cur = await db.execute("SELECT payload FROM record WHERE id = %s", (rid,))
    assert await cur.fetchone() == (payload,)


async def _count(conn: Conn, table: str, column: str, value: object) -> int:
    query = sql.SQL("SELECT count(*) FROM {table} WHERE {column} = %s").format(
        table=sql.Identifier(table), column=sql.Identifier(column)
    )
    cur = await conn.execute(query, (value,))
    counted = await cur.fetchone()
    assert counted is not None
    return int(counted[0])


async def test_deletes_follow_ownership(db: Conn, world: World) -> None:
    kept = await _insert(db, "record", record_row(world, stream_id=world.stream_a))

    # A stream goes with its runs; its records stay, no longer linked to it.
    await db.execute("DELETE FROM stream WHERE id = %s", (world.stream_a,))
    assert await _count(db, "run", "stream_id", world.stream_a) == 0
    cur = await db.execute("SELECT stream_id FROM record WHERE id = %s", (kept,))
    assert await cur.fetchone() == (None,)

    # A source goes with its records and runs.
    await db.execute("DELETE FROM source WHERE id = %s", (world.source_a,))
    assert await _count(db, "record", "source_id", world.source_a) == 0
    assert await _count(db, "run", "source_id", world.source_a) == 0

    # A principal that owns rows stays.
    with pytest.raises(errors.ForeignKeyViolation):
        await db.execute("DELETE FROM principal WHERE id = %s", (world.owner,))

    # A workspace goes with everything in it, and nothing else.
    await db.execute("DELETE FROM workspace WHERE id = %s", (world.a,))
    for table in ("token", "source", "stream", "record", "person", "alias", "run"):
        assert await _count(db, table, "workspace_id", world.a) == 0, table
    assert await _count(db, "source", "workspace_id", world.b) == 1
    assert await _count(db, "person", "workspace_id", world.b) == 1


async def test_a_fresh_source_is_private_local_and_kept(db: Conn, world: World) -> None:
    sid = await _insert(db, "source", source_row(world))

    cur = await db.execute(
        "SELECT visibility, llm_policy, retention_days, config FROM source WHERE id = %s", (sid,)
    )
    assert await cur.fetchone() == ("private", "local", None, {})
