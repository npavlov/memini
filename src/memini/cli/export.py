"""Stream workspace records, decision history and the catalog to stdout as JSON Lines.

A line is a ``record``, a ``knowledge_item`` or a ``project``. A project line is the project of
the catalog as its document (requirements, section 3, Catalog): its services with the services
they use, and its domains with their subdomains and code, with the version and who changed each
entry last and when. The catalog is authored data, so the export holds it whole; nothing in it
is derived. Without
``--include-private``, a service over a private source is left out, with the code entries and
the links that name it, as the records of that source are.
"""

import argparse
import base64
import json
from collections.abc import AsyncIterator
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row

from memini.auth import WriteAccess
from memini.catalog.store import Stamp, StoredCode, StoredDomain, read_projects
from memini.cli.common import EXIT_OK, CommandError, require_migrated
from memini.config import Settings
from memini.db import connect
from memini.decisions.model import DecisionItem
from memini.knowledge.reader import select_items

type Conn = AsyncConnection[TupleRow]

_RECORDS: Final = """
SELECT r.id, r.source_id::text AS source_id, s.name AS source, s.kind AS source_kind,
       s.visibility, r.kind, r.external_id,
       r.external_url, r.content_hash, r.occurred_at, r.deleted_at, r.payload, r.normalized_text
FROM record r JOIN source s ON s.id = r.source_id AND s.workspace_id = r.workspace_id
WHERE r.workspace_id = %s AND (%s OR s.visibility = 'workspace')
ORDER BY r.id
"""

_VISIBLE: Final = sql.SQL("""
i.workspace_id = %(workspace)s AND (%(private)s OR NOT EXISTS (
    SELECT 1 FROM knowledge_evidence e
    LEFT JOIN source s ON s.id = e.source_id AND s.workspace_id = e.workspace_id
    WHERE e.item_id = i.id AND s.visibility IS DISTINCT FROM 'workspace'
))
""")


async def _records(conn: Conn, workspace: UUID, private: bool) -> AsyncIterator[dict[str, Any]]:
    async with conn.cursor(name="export_records", row_factory=dict_row) as cursor:
        await cursor.execute(_RECORDS, (workspace, private))
        async for row in cursor:
            payload: bytes | None = row.pop("payload")
            if payload is not None:
                try:
                    row["payload"] = json.loads(payload)
                except (UnicodeDecodeError, ValueError):
                    row["payload_base64"] = base64.b64encode(payload).decode("ascii")
            for key in ("occurred_at", "deleted_at"):
                if row[key] is not None:
                    row[key] = row[key].isoformat()
            yield row


async def _journal(
    conn: Conn, workspace: UUID, owner: UUID, private: bool
) -> AsyncIterator[dict[str, Any]]:
    access = WriteAccess(owner, frozenset({workspace}))
    params: dict[str, Any] = {"workspace": workspace, "private": private}
    condition = _VISIBLE
    while items := await select_items(
        conn, access, condition, params, order=sql.SQL("i.id"), limit=100
    ):
        linked = {link.supersedes_id for item in items for link in item.supersedes}
        linked.update(item.superseded_by.item_id for item in items if item.superseded_by)
        if linked and not private:
            cursor = await conn.execute(
                sql.SQL(
                    "SELECT i.id FROM knowledge_item i WHERE {} AND i.id = ANY(%(linked)s)"
                ).format(_VISIBLE),
                params | {"linked": list(linked)},
            )
            linked = {row[0] for row in await cursor.fetchall()}
        for item in items:
            exported = DecisionItem.of(item)
            exported.supersedes = [
                link for link in exported.supersedes if link.supersedes_id in linked
            ]
            if exported.superseded_by and exported.superseded_by.item_id not in linked:
                exported.superseded_by = None
            yield exported.model_dump(mode="json", exclude={"workspace", "data_notice"})
        params["after"] = items[-1].id
        condition = sql.SQL("{} AND i.id > %(after)s").format(_VISIBLE)


def _stamp(stamp: Stamp) -> dict[str, Any]:
    return {
        "updated_at": stamp.at.isoformat(),
        "updated_by": str(stamp.by),
        "updated_token": None if stamp.token is None else str(stamp.token),
    }


def _domain_row(domain: StoredDomain, code: list[StoredCode]) -> dict[str, Any]:
    return {
        "name": domain.name,
        "title": domain.title,
        "description": domain.description,
        "code": [
            {
                "project": each.project,
                "service": each.service,
                "paths": list(each.paths),
                **_stamp(each.stamp),
            }
            for each in code
            if each.domain_id == domain.id
        ],
        **_stamp(domain.stamp),
    }


async def _projects(
    conn: Conn, workspace: UUID, owner: UUID, private: bool
) -> AsyncIterator[dict[str, Any]]:
    """The projects of the catalog, by name, each as its document."""
    cursor = await conn.execute(
        "SELECT s.id FROM service s JOIN source src ON src.id = s.source_id"
        " WHERE s.workspace_id = %s AND (%s OR src.visibility = 'workspace')",
        (workspace, private),
    )
    visible = {row[0] for row in await cursor.fetchall()}
    access = WriteAccess(owner, frozenset({workspace}))
    for project in await read_projects(conn, access, workspace):
        code = [each for each in project.code if each.service_id in visible]
        yield {
            "name": project.name,
            "title": project.title,
            "description": project.description,
            "version": project.version,
            **_stamp(project.stamp),
            "services": [
                {
                    "name": service.name,
                    "description": service.description,
                    "repository": service.repository,
                    "root": service.root,
                    "uses": [
                        {
                            "project": link.to_project,
                            "service": link.to_service,
                            "kind": link.kind,
                            "note": link.note,
                            "paths": list(link.paths),
                            **_stamp(link.stamp),
                        }
                        for link in project.links
                        if link.service_id == service.id and link.target_id in visible
                    ],
                    **_stamp(service.stamp),
                }
                for service in project.services
                if service.id in visible
            ],
            "domains": [
                {
                    **_domain_row(domain, code),
                    "subdomains": [
                        _domain_row(part, code)
                        for part in project.domains
                        if part.parent_id == domain.id
                    ],
                }
                for domain in project.domains
                if domain.parent_id is None
            ],
        }


async def export_workspace(
    conn: Conn, principal: str, name: str, *, include_private: bool = False
) -> AsyncIterator[str]:
    async with conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor = await conn.execute(
            "SELECT w.id, w.owner_id FROM workspace w JOIN principal p ON p.id = w.owner_id"
            " WHERE w.name = %s AND p.name = %s",
            (name, principal),
        )
        workspace = await cursor.fetchone()
        if workspace is None:
            raise CommandError("No such workspace for the configured principal.")
        workspace_id, owner_id = workspace
        for kind, rows in [
            ("record", _records(conn, workspace_id, include_private)),
            ("knowledge_item", _journal(conn, workspace_id, owner_id, include_private)),
            ("project", _projects(conn, workspace_id, owner_id, include_private)),
        ]:
            async for row in rows:
                yield json.dumps({"type": kind, "workspace": name, **row}, allow_nan=False)


async def export_command(args: argparse.Namespace, settings: Settings) -> int:
    async with connect(settings.database_url.get_secret_value(), autocommit=True) as conn:
        await require_migrated(conn, "run `memini migrate` first")
        async for line in export_workspace(
            conn, settings.principal, args.workspace, include_private=args.include_private
        ):
            print(line)
    return EXIT_OK
