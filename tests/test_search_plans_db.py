"""Every statement of a search is planned for its own values, also once psycopg has prepared it
(memini.search.engine; docs/operations/performance.md, Search). Every value is synthetic."""

import uuid

import pytest

from ingest_helpers import document
from memini.auth import Principal, WriteAccess, authorize_read
from memini.db import connect
from memini.embeddings import FakeEmbedder
from memini.ingest import Batch, PayloadIndexer, ingest
from memini.search import Mode, SearchQuery, search
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

pytestmark = pytest.mark.integration


@pytest.mark.anyio
async def test_a_prepared_search_statement_never_runs_a_generic_plan(
    migrated_database_url: str,
) -> None:
    fake = FakeEmbedder()
    async with connect(migrated_database_url, autocommit=True) as conn:
        owner = await ensure_principal(conn, "planner")
        await create_workspace(conn, owner_id=owner, name="garden")
        write = WriteAccess.for_owner(owner)
        await create_push_source(conn, write, "garden", NewPushSource(name="notes"))
        records = [
            document(f"doc-{n}", f"# Tomatoes {n}\n\nWater the tomatoes and the basil, day {n}.")
            for n in range(12)
        ]
        await ingest(
            conn, write, "garden", "notes", Batch(records=records), indexer=PayloadIndexer(fake)
        )
        access = authorize_read(
            Principal(principal_id=owner, token_id=uuid.UUID(int=0), scopes=frozenset({"read"}))
        )
        # As if the planner always preferred the generic plan of a prepared statement.
        await conn.execute("SET plan_cache_mode = force_generic_plan")
        cases: tuple[tuple[Mode, str], ...] = (
            ("lexical", "water tomatoes"),
            ("hybrid", "basil"),
            ("exact", "basil"),
        )
        for mode, text in cases:
            for _ in range(8):  # psycopg prepares a statement on its fifth run
                hits = await search(conn, fake, access, SearchQuery(text=text, mode=mode))
                assert hits
        cur = await conn.execute(
            "SELECT statement, generic_plans, custom_plans FROM pg_prepared_statements"
            " WHERE statement LIKE '%%FROM chunk c%%'"
        )
        prepared = await cur.fetchall()

    assert len(prepared) >= 3  # the lexical, dense and exact statements at least
    for statement, generic, custom in prepared:
        assert generic == 0, statement
        assert custom > 0, statement
