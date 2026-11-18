"""Readiness against a real database: run with `make test-integration`."""

import httpx2
import psycopg
import pytest
from psycopg import sql

from memini.api import create_app
from memini.config import Settings
from memini.db import database_check, open_pool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_readiness_follows_the_database(
    migrated_database_url: str, postgres_url: str, settings: Settings
) -> None:
    database = migrated_database_url.rsplit("/", 1)[1]
    async with open_pool(migrated_database_url, min_size=1, max_size=2, timeout=1.0) as pool:
        app = create_app(
            settings, readiness_checks={"database": database_check(pool)}, readiness_timeout=1.0
        )
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://memini.test") as client:
            up = await client.get("/readyz")
            assert up.status_code == 200
            assert up.json() == {"status": "ready", "checks": {"database": {"status": "ok"}}}

            # Take the database away under the pool: its connections are terminated and it can
            # no longer reconnect.
            with psycopg.connect(postgres_url, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
                )

            down = await client.get("/readyz")
            assert down.status_code == 503
            body = down.json()
            assert body["status"] == "not_ready"
            assert body["checks"]["database"]["status"] in ("failed", "timeout")

            # Liveness does not depend on the database.
            assert (await client.get("/healthz")).status_code == 200
