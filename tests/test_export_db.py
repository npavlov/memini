"""Workspace JSONL exports through the operator CLI, with synthetic sources."""

import asyncio
import json

import psycopg
import pytest

from ingest_helpers import HOUR, T0, document
from memini.auth import WriteAccess
from memini.cli.common import EXIT_FAILURE, EXIT_OK
from memini.db import connect
from memini.ids import record_id
from memini.ingest import Batch, ingest
from memini.knowledge import EvidenceRef, NewItem, SupersedesRef, record_item
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal
from test_cli_db import invoke

pytestmark = pytest.mark.integration


async def seed(url: str) -> tuple[str, str]:
    async with connect(url, autocommit=True) as conn:
        owner = await ensure_principal(conn, "tester")
        access = WriteAccess.for_owner(owner)
        workspace = await create_workspace(conn, owner_id=owner, name="garden")
        await create_workspace(conn, owner_id=owner, name="other")
        public = await create_push_source(
            conn, access, "garden", NewPushSource(name="shared", visibility="workspace")
        )
        private = await create_push_source(
            conn, access, "garden", NewPushSource(name="private-notes")
        )
        await create_push_source(
            conn, access, "other", NewPushSource(name="other", visibility="workspace")
        )
        for space, source, title, body in (
            ("garden", public.name, "guide", "Plant\nmarigolds."),
            ("garden", private.name, "private-title", "confidential body"),
            ("other", "other", "other-title", "unrelated body"),
        ):
            await ingest(conn, access, space, source, Batch(records=[document(title, body)]))

        shared = EvidenceRef(record_id(public.id, "document", "guide"))
        confidential = EvidenceRef(record_id(private.id, "document", "private-title"))
        original = await record_item(
            conn, access, workspace.id, NewItem("decision", "Plant marigolds.", T0, [shared])
        )
        mixed = await record_item(
            conn,
            access,
            workspace.id,
            NewItem(
                "decision",
                "Confidential revision.",
                T0 + HOUR,
                [shared, confidential],
                supersedes=[SupersedesRef(original.id)],
            ),
        )
        return str(original.id), str(mixed.id)


def test_export_omits_private_sources_mixed_decisions_and_links(
    empty_database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMINI_DATABASE_URL", empty_database_url)
    monkeypatch.setenv("MEMINI_PRINCIPAL", "tester")
    assert invoke(capsys, "migrate").code == EXIT_OK
    original, mixed = asyncio.run(seed(empty_database_url))
    for private in (False, True):
        args = ["--include-private"] if private else []
        answer = invoke(capsys, "export", "--workspace", "garden", *args)
        assert answer.code == EXIT_OK, answer.err
        rows = [json.loads(line) for line in answer.out.splitlines()]
        records = {r["external_id"]: r for r in rows if r["type"] == "record"}
        items = {r["id"]: r for r in rows if r["type"] == "knowledge_item"}
        assert records["guide"]["payload"]["text"] == "Plant\nmarigolds."
        assert set(records) == ({"guide", "private-title"} if private else {"guide"})
        assert set(items) == ({original, mixed} if private else {original})
        if private:
            assert items[original]["superseded_by"]["item_id"] == mixed
            assert len(items[mixed]["evidence"]) == 2
        else:
            assert "private" not in answer.out
            assert "Confidential" not in answer.out
            assert mixed not in answer.out
            assert items[original]["superseded_by"] is None
        assert "unrelated" not in answer.out
    with psycopg.connect(empty_database_url) as conn:
        conn.execute("DELETE FROM source WHERE name = 'private-notes'")
    after_removal = invoke(capsys, "export", "--workspace", "garden")
    assert after_removal.code == EXIT_OK
    assert mixed not in after_removal.out
    assert "private-title" not in after_removal.out
    monkeypatch.setenv("MEMINI_PRINCIPAL", "stranger")
    refused = invoke(capsys, "export", "--workspace", "garden", "--include-private")
    assert refused.code == EXIT_FAILURE
    assert refused.out == ""
