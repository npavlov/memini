"""Redaction against a real Postgres. Acceptance of memini-kk2: fixtures with fake keys and card
numbers leave no trace in any table.

After each scenario the test scans every text, character, JSON, bytea, array and tsvector
column of every table of the database (from information_schema, so a table added later is
scanned too) for the fake values. Pushed records go through the ingest library; a repository
goes through git sync, then get_file. Every secret is fake (tests/redaction_helpers.py).
"""

import io
import json
import shutil
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString

import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import SecretStr

from git_helpers import SourceRepo
from memini.auth import Principal, ReadFilter, WriteAccess
from memini.config import Settings
from memini.db import connect, open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, run_git_sync
from memini.ingest import Batch, PayloadIndexer, RecordIn, WindowClaim, ingest
from memini.jobs import Job
from memini.query.reader import get_file
from memini.runs import record_run
from memini.sources import NewGitSource, NewPushSource, create_git_source, create_push_source
from memini.workspaces import create_workspace, ensure_principal
from redaction_helpers import (
    AMEX,
    AWS_KEY_ID,
    AWS_SECRET,
    GITHUB,
    GOOGLE,
    JWT,
    MASTERCARD,
    MEMINI,
    OPENAI,
    PEM,
    PEM_BODY,
    PEM_ESCAPED,
    SLACK,
    STRIPE,
    VISA,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

T0 = datetime(2026, 3, 1, tzinfo=UTC)
URL_PASSWORD = "Fake-Url-Pass-1"  # noqa: S105 - a fake value to look for
PASSWORD = "hunter22-fake"  # noqa: S105 - a fake value to look for
CURSOR_TOKEN = "fake-cursor-token-77"  # noqa: S105 - a fake value to look for

NEEDLES: tuple[str, ...] = (
    VISA,
    VISA.replace(" ", ""),
    MASTERCARD,
    MASTERCARD.replace("-", ""),
    AMEX,
    AMEX.replace(" ", ""),
    "4111111111111111",
    AWS_KEY_ID,
    AWS_SECRET,
    GITHUB,
    GOOGLE,
    JWT,
    JWT.split(".")[1],
    MEMINI,
    OPENAI,
    SLACK,
    STRIPE,
    *PEM_BODY,
    URL_PASSWORD,
    PASSWORD,
    CURSOR_TOKEN,
)
"""Every fake value of the fixtures, and the parts of some, each on one line (JSON and the
tsvector write line breaks their own way)."""

_SCANNED_TYPES = ("text", "character varying", "character", "json", "jsonb", "bytea", "ARRAY")


async def scan(db: Conn, needles: Sequence[str]) -> tuple[list[str], list[str]]:
    """Where each needle occurs in the database: ``table.column: needle``; and the columns
    scanned, ``table.column``."""
    cur = await db.execute(
        "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c"
        " JOIN information_schema.tables t USING (table_schema, table_name)"
        " WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'"
        " AND (c.data_type = ANY(%s) OR c.udt_name = 'tsvector')"
        " ORDER BY c.table_name, c.column_name",
        (list(_SCANNED_TYPES),),
    )
    columns = [(row[0], row[1], row[2]) for row in await cur.fetchall()]
    found: list[str] = []
    for table, column, data_type in columns:
        target = sql.Identifier(table)
        field = sql.Identifier(column)
        for needle in needles:
            if data_type == "bytea":
                query = sql.SQL("SELECT count(*) FROM {} WHERE position(%s::bytea in {}) > 0")
                value: Any = needle.encode()
            else:
                query = sql.SQL("SELECT count(*) FROM {} WHERE strpos({}::text, %s) > 0")
                value = needle
            cur = await db.execute(query.format(target, field), (value,))
            row = await cur.fetchone()
            if row is not None and row[0]:
                found.append(f"{table}.{column}: {needle}")
    return found, [f"{table}.{column}" for table, column, _ in columns]


async def rows(db: Conn, query: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
    cur = await db.execute(query, params)
    return [tuple(row) for row in await cur.fetchall()]


# --- Pushed records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    db: Conn
    runs: Conn
    access: WriteAccess


@pytest.fixture
async def env(migrated_database_url: str) -> AsyncIterator[Env]:
    async with (
        connect(migrated_database_url, autocommit=True) as db,
        connect(migrated_database_url, autocommit=True) as runs,
    ):
        owner = await ensure_principal(db, "owner")
        await create_workspace(db, owner_id=owner, name="acme")
        access = WriteAccess.for_owner(owner)
        await create_push_source(db, access, "acme", NewPushSource(name="chat"))
        yield Env(db, runs, access)


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


ID_LIKE_A_CARD = "6011111111111117"
"""A message id of digits that passes as a card number: an id, kept as it is."""


def secret_batch() -> Batch:
    """A thread, a meeting and a document full of fake secrets, through a stream whose window
    claims a cursor with a token in it. Secrets stand where the chunks take their anchor (a
    message id) and URL (a message's url) from, and in the normalized text."""
    at = T0 + timedelta(hours=9)
    thread = RecordIn(
        kind="thread",
        external_id="general/0001",
        occurred_at=at,
        external_url=f"https://reader:{URL_PASSWORD}@chat.example.invalid/archives/general/1",
        payload={
            "channel": "general",
            "messages": [
                {
                    "id": f"msg-{SLACK}",
                    "author": "Ann Example",
                    "sent_at": iso(at - timedelta(minutes=5)),
                    "text": f"Pay with {VISA} 12/28 737, or {AMEX}.",
                    "url": f"https://reader:{URL_PASSWORD}@chat.example.invalid/m/1",
                },
                {
                    "id": ID_LIKE_A_CARD,
                    "author": "Bo Sample",
                    "sent_at": iso(at),
                    "text": f"Deploy key:\n{PEM}\nand {AWS_KEY_ID} with {AWS_SECRET}",
                    "files": [{"url": f"https://u:{URL_PASSWORD}@files.example.invalid/a.txt"}],
                },
            ],
            "billing": {"card": 4111111111111111, MASTERCARD: "a card as a key"},
            "credentials": {"password": PASSWORD, "api_key": STRIPE},
        },
    )
    meeting = RecordIn(
        kind="meeting",
        external_id="m-1",
        occurred_at=at,
        payload={
            "title": "Release planning",
            "started_at": iso(at),
            "participants": ["Ann Example", "Bo Sample"],
            "summary": f"Bo pasted {GITHUB} into the chat; rotate it.",
            "action_items": [{"text": "Rotate the key", "owner": "Bo Sample"}],
            "transcript": [
                {
                    "id": "seg-1",
                    "speaker": "Ann Example",
                    "start": 0,
                    "text": f"Authorization: Bearer {JWT}",
                },
                {
                    "id": "seg-2",
                    "speaker": "Bo Sample",
                    "start": 12,
                    "text": f"password={PASSWORD}",
                },
            ],
        },
    )
    document = RecordIn(
        kind="document",
        external_id="handbook",
        occurred_at=at,
        payload={
            "title": "Operations handbook",
            "text": (
                "# Keys\n\n"
                f"Slack bot: {SLACK}\n\n"
                f"Model key: {OPENAI}, maps: {GOOGLE}, Memini: {MEMINI}\n\n"
                f'## Service account\n\n{{"private_key": "{PEM_ESCAPED}"}}\n'
            ),
        },
    )
    return Batch(
        stream="chat/general",
        window=WindowClaim(
            start=T0,
            end=T0 + timedelta(days=1),
            complete=True,
            cursor={"page": 2, "access_token": CURSOR_TOKEN},
        ),
        records=[thread, meeting, document],
    )


async def test_pushed_records_leave_no_secret_in_any_table(
    env: Env, log_output: io.StringIO
) -> None:
    indexer = PayloadIndexer(FakeEmbedder())  # as the push route indexes

    result = await ingest(
        env.db, env.access, "acme", "chat", secret_batch(), runs=env.runs, indexer=indexer
    )

    assert result.inserted == 3
    found, columns = await scan(env.db, NEEDLES)
    assert found == []
    assert set(columns) >= {
        "record.payload",
        "record.normalized_text",
        "record.external_url",
        "chunk.header",
        "chunk.content",
        "chunk.anchor",
        "chunk.url",
        "run.report",
        "stream.cursor",
    }
    # The scan finds what is stored: the text and ids around the secrets are there.
    control, _ = await scan(
        env.db,
        [
            "Model key: [REDACTED:api_key]",
            "Pay with [REDACTED:card] 12/28 737",
            "msg-[REDACTED:slack_token]",
            ID_LIKE_A_CARD,
            "https://REDACTED@chat.example.invalid/m/1",
        ],
    )
    assert set(control) >= {
        "record.payload: Model key: [REDACTED:api_key]",
        "record.normalized_text: Model key: [REDACTED:api_key]",
        "chunk.content: Model key: [REDACTED:api_key]",
        "record.normalized_text: Pay with [REDACTED:card] 12/28 737",
        "chunk.content: Pay with [REDACTED:card] 12/28 737",
        "chunk.anchor: msg-[REDACTED:slack_token]",
        f"record.payload: {ID_LIKE_A_CARD}",
        "chunk.url: https://REDACTED@chat.example.invalid/m/1",
    }
    kinds = await rows(env.db, "SELECT DISTINCT record_kind FROM chunk ORDER BY 1")
    assert kinds == [("document",), ("meeting",), ("thread",)]
    # What was stored in their place.
    payload = (await rows(env.db, "SELECT payload FROM record WHERE external_id = 'm-1'"))[0][0]
    assert [segment["text"] for segment in json.loads(payload)["transcript"]] == [
        "Authorization: Bearer [REDACTED:jwt]",
        "password=[REDACTED:secret]",
    ]
    assert await rows(env.db, "SELECT cursor FROM stream") == [
        ({"page": 2, "access_token": "[REDACTED:secret]"},)
    ]
    url = await rows(env.db, "SELECT external_url FROM record WHERE external_id = 'general/0001'")
    assert url == [("https://REDACTED@chat.example.invalid/archives/general/1",)]

    # The run report has the counts by kind, and nothing else of them.
    report = (await rows(env.db, "SELECT report FROM run WHERE kind = 'ingest'"))[0][0]
    redacted = {name: n for name, n in report["counts"].items() if name.startswith("redacted_")}
    assert redacted == {
        "redacted_api_key": 1,
        "redacted_aws_access_key": 1,
        "redacted_aws_secret_key": 1,
        "redacted_card": 4,
        "redacted_gcp_api_key": 1,
        "redacted_github_token": 1,
        "redacted_jwt": 1,
        "redacted_memini_token": 1,
        "redacted_private_key": 2,
        "redacted_secret": 4,
        "redacted_slack_token": 2,
        "redacted_url_credentials": 3,
    }
    # The chunks came from the redacted payload: the writer's own redaction found nothing left.
    assert report["counts"]["chunks_inserted"] > 0
    assert report["counts"]["chunk_values_redacted"] == 0

    # Sent again, the batch changes nothing: the content hash is over the redacted bytes.
    before = await rows(env.db, "SELECT id, xmin::text, content_hash FROM record ORDER BY id")
    again = await ingest(
        env.db, env.access, "acme", "chat", secret_batch(), runs=env.runs, indexer=indexer
    )
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 3)
    assert await rows(env.db, "SELECT id, xmin::text, content_hash FROM record ORDER BY id") == (
        before
    )

    logs = log_output.getvalue()
    assert logs  # the runs were logged
    assert [needle for needle in NEEDLES if needle in logs] == []


# --- A repository --------------------------------------------------------------------------------

SETTINGS_PY = f'''"""Settings of a synthetic service, with secrets committed by mistake."""

DATABASE_URL = "postgresql://app:{URL_PASSWORD}@db.example.invalid:5432/app"
AWS_ACCESS_KEY_ID = "{AWS_KEY_ID}"
AWS_SECRET_ACCESS_KEY = "{AWS_SECRET}"
DEPLOY_KEY = """{PEM}"""


def connect(password="{PASSWORD}"):
    """Connect with the password."""
    return password


def card():
    return "{VISA}"
'''

SETUP_MD = f"""# Setup

Push with `{GITHUB}`.

## Payments

Test with the card {MASTERCARD}, and the key {STRIPE}.
"""


@pytest.mark.git
@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable")
async def test_a_repository_leaves_no_secret_in_any_table_nor_in_read_source(
    migrated_database_url: str, tmp_path: Path
) -> None:
    repos = tmp_path / "repos"
    settings = Settings(
        database_url=SecretStr(migrated_database_url),
        git_mirror_dir=tmp_path / "mirrors",
        git_local_roots=(repos,),
    )
    repo = SourceRepo(repos / "service")
    (repo.path / "link").symlink_to(f"keys/{GITHUB}")
    repo.git("add", "--", "link")
    repo.commit({"app/settings.py": SETTINGS_PY, "docs/setup.md": SETUP_MD}, "first")
    embedder = FakeEmbedder()
    mirrors = MirrorManager(GitOptions.from_settings(settings))
    async with (
        open_pool(migrated_database_url, min_size=1, max_size=4, timeout=5) as pool,
        connect(migrated_database_url, autocommit=True) as db,
    ):
        owner = await ensure_principal(db, "owner")
        await create_workspace(db, owner_id=owner, name="acme")
        source = await create_git_source(
            db,
            WriteAccess.for_owner(owner),
            "acme",
            NewGitSource(name="service", location=str(repo.path), refs=["main"]),
            options=GitOptions.from_settings(settings),
        )
        job = Job(
            id=uuid.uuid4(),
            workspace_id=source.workspace_id,
            owner_id=source.owner_id,
            source_id=source.id,
            stream_id=None,
            kind=GIT_SYNC,
            args={},
            attempt=1,
            max_attempts=1,
            worker="test",
        )
        async with record_run(
            pool,
            workspace_id=source.workspace_id,
            owner_id=owner,
            kind=GIT_SYNC,
            source_id=source.id,
            progress_interval=None,
        ) as run:
            await run_git_sync(pool, mirrors, embedder, job)
        report = run.report()

        found, _ = await scan(db, NEEDLES)
        assert found == []
        control, _ = await scan(db, ["def card():", "Test with the card [REDACTED:card]"])
        assert set(control) >= {
            "chunk.content: def card():",
            "chunk.content: Test with the card [REDACTED:card]",
        }
        kinds = await rows(db, "SELECT DISTINCT record_kind FROM chunk ORDER BY 1")
        assert kinds == [("code_file",), ("document",)]
        # Six values in settings.py, three in setup.md: counted, never shown.
        assert report["counts"]["chunk_values_redacted"] >= 9

        caller = Principal(principal_id=owner, token_id=uuid.uuid4(), scopes=frozenset({"read"}))
        read = await get_file(
            db,
            ReadFilter(principal=caller, workspace_ids=None),
            mirrors,
            repository="service",
            path="app/settings.py",
        )

        link = await get_file(
            db,
            ReadFilter(principal=caller, workspace_ids=None),
            mirrors,
            repository="service",
            path="link",
        )
        assert link.target == "keys/[REDACTED:github_token]"

    assert read.content is not None
    assert [needle for needle in NEEDLES if needle in read.content] == []
    # The file keeps its lines: line numbers agree with the repository and the symbol table.
    lines = read.content.splitlines()
    assert read.total_lines == len(lines) == SETTINGS_PY.count("\n")
    assert lines.index("def card():") == SETTINGS_PY.splitlines().index("def card():")
    assert {symbol.name for symbol in read.symbols} >= {"connect", "card"}
    for symbol in read.symbols:
        assert symbol.name in lines[symbol.start_line - 1]
    assert 'return "[REDACTED:card]"' in read.content
    assert "[REDACTED:private_key]" in read.content
