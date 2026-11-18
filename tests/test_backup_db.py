"""Backup and restore (docs/operations/backup.md) against a real Postgres and real git mirrors.

Acceptance of memini-fj4: a dump restored into an empty database gives the same search results. A
database is filled as the service fills it: a git source synced with the FakeEmbedder, a push
source with a thread and a document indexed at ingest, knowledge items with evidence, one
superseding another, a credential, tokens. It is dumped with ``pg_dump --format=custom`` and
restored with ``pg_restore --no-owner --single-transaction`` into a new, empty database, as
scripts/backup.sh and scripts/restore.sh do (the programs: tests/backup_helpers.py). The restored
database must then hold every row and every object, the HNSW index of the embedding key and the
triggers of the journal among them, find nothing to migrate, answer every search mode as the
original does, and accept the same tokens and credential key. Mirrors are not in a backup: a sync
after the restore clones the repository again and goes on from the pinned commit. Every value is
synthetic.
"""

import base64
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, LiteralString
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow, dict_row
from pydantic import SecretStr

from backup_helpers import PgPrograms, find_programs
from git_helpers import SourceRepo
from index_helpers import LAMP, PRUNE, WATER, WATER_EDITED, source
from ingest_helpers import DAY, T0, document, thread
from memini.auth import (
    AuthenticationError,
    Principal,
    ReadFilter,
    WriteAccess,
    authenticate,
    authorize_read,
    create_token,
    revoke_token,
)
from memini.credentials import (
    CredentialKey,
    CredentialKeyUnavailableError,
    Keyring,
    put_api_key,
    read_api_key,
)
from memini.db import Pool, connect, migrate, open_pool, pending_migrations
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, git_sync_handler, stream_pin
from memini.index import VectorIndex, current_fingerprint, read_index_state, vector_index_exists
from memini.ingest import Batch, PayloadIndexer, WindowClaim, ingest
from memini.jobs import Job, execute_job
from memini.knowledge import (
    ALL_STATUSES,
    EvidenceRef,
    ItemFilters,
    NewItem,
    SupersedesRef,
    get_item,
    list_items,
    record_item,
)
from memini.query import NotSyncedError, read_source
from memini.runs import wait_for_run_writes
from memini.search import Mode, SearchFilters, SearchQuery, search
from memini.sources import NewGitSource, NewPushSource, create_git_source, create_push_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.git,
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

type Conn = AsyncConnection[TupleRow]

MAIN: Final = "refs/heads/main"
STREAM: Final = "chat/general"
KEY: Final = bytes(range(32, 64))
"""The credential key: kept apart from the database, and so from its dump."""
OTHER_KEY: Final = bytes(range(96, 128))
API_KEY: Final = "synthetic-api-key-0123456789abcdef"

GREENHOUSE: Final = source(WATER, PRUNE, LAMP).decode()
README: Final = (
    "# Greenhouse\n\nControls for a synthetic greenhouse.\n\n"
    "## Watering\n\nThe beds get water twice a day, within the limit of forty litres.\n"
)

SEARCHES: Final[Sequence[tuple[Mode, str, SearchFilters]]] = (
    ("hybrid", "water the plant within the limit", SearchFilters()),
    ("hybrid", "move the release to Friday", SearchFilters(kinds=("thread",))),
    ("dense", "a lamp over the plants", SearchFilters()),
    ("lexical", "water limit", SearchFilters()),
    ("lexical", "release candidate", SearchFilters(sources=("chat",))),
    ("exact", "plant.soil", SearchFilters()),
    ("regex", r"def \w+\(plant", SearchFilters(repositories=("greenhouse",))),
    ("symbol", "Lamp.on", SearchFilters()),
    ("symbol", "water", SearchFilters()),
)


@dataclass(frozen=True)
class GitStream:
    """The main branch of the git source: what a sync job names."""

    owner: uuid.UUID
    workspace: uuid.UUID
    source: uuid.UUID
    stream: uuid.UUID


@dataclass(frozen=True)
class Backup:
    """A database filled as the service fills it, its archive, and the database restored from it."""

    original: str
    """URL of the database that was dumped."""
    restored: str
    """URL of the empty database the archive was restored into."""
    archive: bytes
    programs: PgPrograms
    repo: SourceRepo
    sources_dir: Path
    git: GitStream
    chat: uuid.UUID
    items: tuple[uuid.UUID, ...]
    """Oldest first; the second supersedes the first."""
    read_token: str
    revoked_token: str

    @property
    def reader(self) -> ReadFilter:
        return authorize_read(
            Principal(
                principal_id=self.git.owner,
                token_id=uuid.uuid4(),
                scopes=frozenset({"read"}),
                workspace_id=None,
            )
        )


def database_name(url: str) -> str:
    return str(conninfo_to_dict(url)["dbname"])


@pytest.fixture
def restore_target(postgres_url: str) -> Iterator[str]:
    """URL of a second new database with nothing in it, dropped after the test."""
    name = f"memini_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    try:
        yield urlunsplit(urlsplit(postgres_url)._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(postgres_url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


def git_options(mirror_dir: Path, sources_dir: Path) -> GitOptions:
    return GitOptions(
        mirror_dir=mirror_dir, local_roots=(sources_dir,), fetch_timeout=30, command_timeout=30
    )


async def sync(
    pool: Pool, mirrors: MirrorManager, embedder: FakeEmbedder, target: GitStream
) -> dict[str, Any]:
    """Run one git_sync job of ``target`` as the worker runs it; return its run row."""
    job = Job(
        id=uuid.uuid4(),
        workspace_id=target.workspace,
        owner_id=target.owner,
        source_id=target.source,
        stream_id=target.stream,
        kind=GIT_SYNC,
        args={},
        attempt=1,
        max_attempts=5,
        worker="test",
    )
    try:
        await execute_job(job, {GIT_SYNC: git_sync_handler(pool, mirrors, embedder)}, pool)
    finally:
        await wait_for_run_writes()
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM run WHERE stream_id = %s ORDER BY started_at DESC LIMIT 1",
            (target.stream,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row


@dataclass(frozen=True)
class Filled:
    git: GitStream
    chat: uuid.UUID
    items: tuple[uuid.UUID, ...]
    read_token: str
    revoked_token: str


async def fill(url: str, repo: SourceRepo, sources_dir: Path, mirror_dir: Path) -> Filled:
    """Fill the database at ``url`` through the service's own code, with a FakeEmbedder."""
    embedder = FakeEmbedder()
    head = repo.commit({"src/greenhouse.py": GREENHOUSE, "README.md": README})
    async with (
        open_pool(url, min_size=1, max_size=4, timeout=5) as pool,
        connect(url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        workspace = (await create_workspace(db, owner_id=owner, name="acme")).id
        access = WriteAccess.for_owner(owner)
        options = git_options(mirror_dir, sources_dir)

        # A git source, synced: code and a Markdown document, chunked, embedded and pinned.
        git = await create_git_source(
            db,
            access,
            "acme",
            NewGitSource(name="greenhouse", location=str(repo.path), refs=("main",)),
            options=options,
        )
        cur = await db.execute(
            "SELECT id FROM stream WHERE source_id = %s AND key = %s", (git.id, MAIN)
        )
        row = await cur.fetchone()
        assert row is not None
        main = GitStream(owner, workspace, git.id, row[0])
        run = await sync(pool, MirrorManager(options), embedder, main)
        assert run["status"] == "succeeded", run["error"]
        pin = await stream_pin(db, git.id, MAIN)
        assert pin is not None
        assert pin.commit == head

        # A push source: a thread and a document, indexed at ingest; the stream's checkpoint moves.
        chat = (await create_push_source(db, access, "acme", NewPushSource(name="chat"))).id
        result = await ingest(
            db,
            access,
            "acme",
            "chat",
            Batch(
                stream=STREAM,
                window=WindowClaim(start=T0, end=T0 + DAY, complete=True),
                records=[
                    thread("general/0001"),
                    document("notes/lamp", "The lamp stays on for twelve hours a day."),
                ],
            ),
            runs=pool,
            indexer=PayloadIndexer(embedder),
        )
        assert result.inserted == 2
        cur = await db.execute(
            "SELECT id, kind FROM record WHERE source_id = %s ORDER BY kind", (chat,)
        )
        records = {kind: rid for rid, kind in await cur.fetchall()}
        cur = await db.execute(
            "SELECT id FROM record WHERE source_id = %s AND kind = 'code_file'", (git.id,)
        )
        row = await cur.fetchone()
        assert row is not None
        code: str = row[0]

        # Knowledge: a decision, a later one that replaces it, and an open question.
        cur = await db.execute(
            "INSERT INTO person (workspace_id, owner_id, display_name) VALUES (%s, %s, %s)"
            " RETURNING id",
            (workspace, owner, "Bo Sample"),
        )
        row = await cur.fetchone()
        assert row is not None
        person: uuid.UUID = row[0]
        first = await record_item(
            db,
            access,
            "acme",
            NewItem(
                kind="decision",
                statement="Water the beds once a day.",
                decided_at=T0,
                evidence=(EvidenceRef(records["thread"], "m1"),),
                participants=(person,),
            ),
        )
        second = await record_item(
            db,
            access,
            "acme",
            NewItem(
                kind="decision",
                statement="Water the beds twice a day, within the limit.",
                decided_at=T0 + timedelta(hours=10),
                evidence=(
                    EvidenceRef(records["thread"], "m2"),
                    EvidenceRef(code, f"{head}#water"),
                ),
                participants=(person,),
                supersedes=(SupersedesRef(first.id),),
            ),
        )
        question = await record_item(
            db,
            access,
            "acme",
            NewItem(
                kind="open_question",
                statement="Should the lamp stay on at night?",
                decided_at=T0 + timedelta(hours=11),
                evidence=(EvidenceRef(records["document"]),),
            ),
        )

        # A credential, under the key, and tokens: one in use, one revoked.
        await put_api_key(
            db, Keyring(CredentialKey(KEY)), source_id=chat, api_key=SecretStr(API_KEY)
        )
        read = await create_token(db, owner_id=owner, name="agent", scopes=("read",))
        revoked = await create_token(db, owner_id=owner, name="old agent", scopes=("read",))
        await revoke_token(db, owner_id=owner, token_id=revoked.info.id)
    await wait_for_run_writes()
    return Filled(
        main,
        chat,
        (first.id, second.id, question.id),
        read.secret.get_secret_value(),
        revoked.secret.get_secret_value(),
    )


@pytest.fixture
async def backup(migrated_database_url: str, restore_target: str, tmp_path: Path) -> Backup:
    """The filled database, dumped and restored into ``restore_target``."""
    sources_dir = tmp_path / "sources"
    repo = SourceRepo(sources_dir / "greenhouse")
    filled = await fill(migrated_database_url, repo, sources_dir, tmp_path / "mirrors")
    programs = find_programs(migrated_database_url)
    archive = programs.dump(database_name(migrated_database_url))
    assert archive.startswith(b"PGDMP")  # the custom format
    programs.restore(database_name(restore_target), archive)
    return Backup(
        original=migrated_database_url,
        restored=restore_target,
        archive=archive,
        programs=programs,
        repo=repo,
        sources_dir=sources_dir,
        git=filled.git,
        chat=filled.chat,
        items=filled.items,
        read_token=filled.read_token,
        revoked_token=filled.revoked_token,
    )


@pytest.fixture
async def both(backup: Backup) -> AsyncIterator[tuple[Conn, Conn]]:
    """Connections to the original database and to the restored one, in autocommit."""
    async with (
        connect(backup.original, autocommit=True) as before,
        connect(backup.restored, autocommit=True) as after,
    ):
        yield before, after


async def scalar(conn: Conn, query: LiteralString, *params: Any) -> Any:
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def table_digests(conn: Conn) -> dict[str, tuple[int, str]]:
    """Every table of the schema: its number of rows and a digest of their text, in order."""
    cur = await conn.execute(
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace"
        " AND c.relkind IN ('r', 'p') ORDER BY 1"
    )
    digests: dict[str, tuple[int, str]] = {}
    for (table,) in await cur.fetchall():
        rows = await conn.execute(
            sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), ''))"
                " FROM {} t"
            ).format(sql.Identifier(table))
        )
        count, digest = (await rows.fetchone()) or (0, "")
        digests[table] = (count, digest)
    return digests


def fake_query(fake: FakeEmbedder) -> str:
    """A vector of the embedder's dimension, as pgvector reads it."""
    return "[" + ",".join(["0.5"] * fake.spec.dimension) + "]"


# --- The restored database -----------------------------------------------------------------------


async def test_a_dump_restored_into_an_empty_database_gives_the_same_search_results(
    backup: Backup, both: tuple[Conn, Conn], fake: FakeEmbedder
) -> None:
    before, after = both

    # Every row of every table, the journal's superseded item and its link among them.
    digests = await table_digests(before)
    assert digests["chunk"][0] > 0
    assert digests["knowledge_supersession"][0] == 1
    assert await table_digests(after) == digests

    # Every object: extensions, tables with their generated lexical column, functions, triggers,
    # constraints and indexes, the HNSW index the writer made for the embedding key among them.
    assert backup.programs.schema(database_name(backup.restored)) == backup.programs.schema(
        database_name(backup.original)
    )
    index = VectorIndex.of(fake.spec)
    assert await vector_index_exists(after, index)
    extensions = "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension"
    assert await scalar(after, extensions) == "pg_trgm,plpgsql,vector"

    # The service starts on it: every migration is recorded, nothing is pending.
    assert await pending_migrations(after) == []
    assert await migrate(backup.restored) == []

    # The index fingerprint came with it, so search is not refused and nothing needs a reindex.
    state = await read_index_state(after, backup.git.workspace)
    assert state is not None
    assert state == await read_index_state(before, backup.git.workspace)
    assert state.fingerprint == current_fingerprint(fake.spec).digest

    # The same hits, in the same order, with the same scores, in every mode.
    for mode, text, filters in SEARCHES:
        query = SearchQuery(text=text, mode=mode, filters=filters, limit=20)
        expected = await search(before, fake, backup.reader, query)
        assert expected, (mode, text)
        assert await search(after, fake, backup.reader, query) == expected, (mode, text)

    # The restored HNSW index serves a dense search.
    async with after.transaction():
        await after.execute("SET LOCAL enable_seqscan = off")
        cur = await after.execute(
            sql.SQL("EXPLAIN SELECT c.id FROM chunk c WHERE {} ORDER BY {} LIMIT 5").format(
                index.predicate("c"), index.distance(sql.Literal(fake_query(fake)), "c")
            )
        )
        plan = "\n".join(row[0] for row in await cur.fetchall())
    assert index.name in plan


async def test_the_journal_tokens_and_credential_work_as_before(
    backup: Backup, both: tuple[Conn, Conn]
) -> None:
    before, after = both
    first, second, _ = backup.items

    # The journal: the same items and links; the first item stays superseded by the second.
    history = ItemFilters(statuses=ALL_STATUSES)
    assert await list_items(after, backup.reader, history) == await list_items(
        before, backup.reader, history
    )
    chain = await get_item(after, backup.reader, first)
    assert chain == await get_item(before, backup.reader, first)
    assert chain.item.status == "superseded"

    # Its triggers are back and enabled: a stored item is never edited or deleted.
    with pytest.raises(errors.CheckViolation) as refused:
        await after.execute(
            "UPDATE knowledge_item SET statement = 'Never water the beds.' WHERE id = %s",
            (second,),
        )
    assert refused.value.diag.constraint_name == "knowledge_item_append_only"
    with pytest.raises(errors.CheckViolation) as refused:
        await after.execute("DELETE FROM knowledge_item WHERE id = %s", (first,))
    assert refused.value.diag.constraint_name == "knowledge_item_append_only"

    # Tokens are stored as digests: the token in use authenticates, the revoked one does not.
    principal = await authenticate(after, backup.read_token)
    assert (principal.principal_id, principal.scopes) == (backup.git.owner, frozenset({"read"}))
    with pytest.raises(AuthenticationError) as failure:
        await authenticate(after, backup.revoked_token)
    assert failure.value.reason == "revoked"

    # The credential: ciphertext in the database, readable with the key kept apart, not without.
    credential = await read_api_key(after, Keyring(CredentialKey(KEY)), source_id=backup.chat)
    assert credential.api_key.get_secret_value() == API_KEY
    with pytest.raises(CredentialKeyUnavailableError):
        await read_api_key(after, Keyring(CredentialKey(OTHER_KEY)), source_id=backup.chat)


async def test_the_dump_holds_no_secret_in_clear(backup: Backup) -> None:
    script = backup.programs.script(backup.archive)

    assert "COPY public.credential " in script  # the ciphertext is in the dump
    assert API_KEY not in script
    assert base64.b64encode(KEY).decode() not in script
    assert backup.read_token not in script
    assert backup.revoked_token not in script


# --- Mirrors are cloned again --------------------------------------------------------------------


async def test_a_sync_after_a_restore_clones_the_mirror_and_goes_on_from_the_pin(
    backup: Backup, tmp_path: Path
) -> None:
    # The backup is older than the repository: a commit came after it.
    head = backup.repo.commit({"src/greenhouse.py": source(WATER_EDITED, PRUNE, LAMP).decode()})
    mirrors = MirrorManager(git_options(tmp_path / "new-mirrors", backup.sources_dir))
    embedder = FakeEmbedder()
    async with (
        open_pool(backup.restored, min_size=1, max_size=4, timeout=5) as pool,
        connect(backup.restored, autocommit=True) as db,
    ):
        # No mirror: search answers from the database, reading a file waits for the next sync.
        with pytest.raises(NotSyncedError):
            await read_source(
                db, backup.reader, mirrors, repository="greenhouse", path="src/greenhouse.py"
            )
        old = await stream_pin(db, backup.git.source, MAIN)
        assert old is not None

        run = await sync(pool, mirrors, embedder, backup.git)

        assert run["status"] == "succeeded", run["error"]
        steps = [(step["name"], step["status"]) for step in run["report"]["steps"]]
        assert steps == [("fetch", "succeeded"), ("diff", "succeeded"), ("index", "succeeded")]
        counts = run["report"]["counts"]
        changed = (counts["files_changed"], counts["files_added"], counts["files_deleted"])
        assert changed == (1, 0, 0)
        assert counts["texts_embedded"] == 1  # the changed function only
        assert len(embedder.texts) == 1
        new = await stream_pin(db, backup.git.source, MAIN)
        assert new is not None
        assert new.commit == head != old.commit
        read = await read_source(
            db, backup.reader, mirrors, repository="greenhouse", path="src/greenhouse.py"
        )
        assert read.commit == head
        assert read.pinned
        assert "LIMIT // 2" in (read.content or "")
