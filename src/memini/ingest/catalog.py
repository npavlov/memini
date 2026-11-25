"""The project and domains a record is pushed with: their check, and the batch's hook that keeps
``record_catalog`` (migration 0020) as the latest push said.

One mailbox or chat holds messages of several projects, so whoever pushes a record may say which
project of the workspace's catalog it belongs to, and which of its domains (requirements, section
3, **Decisions by project and domain**). The fields are names, as ``put_project`` writes them: a
project's name and a domain's address (``domain`` or ``domain/subdomain``). They are checked as
handles here and not looked up: a record is stored whether or not the catalog holds them yet, and
what a decision belongs to is computed when a question is asked (:mod:`memini.decisions.context`),
which leaves out a name the catalog does not hold. They are not part of the payload or of the
content hash: the record's content is what its source said, the project is what the pusher knows
of it.

:func:`write_record_catalog` runs in the batch's transaction, for every record of the batch that
is stored (not for a forgotten one): it writes a row where the record has a project and none or
another is stored, and deletes the row of a record pushed without one, as a record is replaced
whole. The same batch again writes nothing.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection

from memini.ingest.model import InvalidBatchError, PreparedRecord

__all__ = [
    "ADDRESS_PATTERN",
    "MAX_PLACE_DOMAINS",
    "check_address",
    "check_place",
    "check_project",
    "write_record_catalog",
]

MAX_PLACE_DOMAINS: Final = 16
"""Domains one record, default or link request names at most."""

_HANDLE: Final = r"[a-z0-9][a-z0-9_-]{0,62}"
ADDRESS_PATTERN: Final = re.compile(rf"{_HANDLE}(?:/{_HANDLE})?")
"""A domain's address: ``domain`` or ``domain/subdomain``."""
_PROJECT: Final = re.compile(_HANDLE)

_PROJECT_RULE: Final = (
    "a project name is 1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or"
    " digit"
)
_ADDRESS_RULE: Final = "a domain is a name of the catalog, or domain/subdomain"


def check_project(value: object, field: str) -> str:
    """``value`` if it can name a project, or :class:`InvalidBatchError` naming ``field``."""
    if not isinstance(value, str) or _PROJECT.fullmatch(value) is None:
        raise InvalidBatchError(field, _PROJECT_RULE)
    return value


def check_address(value: object, field: str) -> str:
    """``value`` if it can name a domain or subdomain, or :class:`InvalidBatchError`."""
    if not isinstance(value, str) or ADDRESS_PATTERN.fullmatch(value) is None:
        raise InvalidBatchError(field, _ADDRESS_RULE)
    return value


def check_place(
    project: object, domains: Sequence[object], field: str
) -> tuple[str | None, tuple[str, ...]]:
    """The project and domains of a record, checked, without repeats; ``field`` is where the
    record is in the batch (``records[3]``). Domains need their project."""
    if isinstance(domains, str):
        raise InvalidBatchError(f"{field}.domains", "a list of domains")
    if project is None:
        if domains:
            raise InvalidBatchError(f"{field}.domains", "domains are named with their project")
        return None, ()
    name = check_project(project, f"{field}.project")
    if len(domains) > MAX_PLACE_DOMAINS:
        raise InvalidBatchError(f"{field}.domains", f"at most {MAX_PLACE_DOMAINS} domains")
    found = tuple(
        dict.fromkeys(
            check_address(each, f"{field}.domains[{n}]") for n, each in enumerate(domains)
        )
    )
    return name, found


_DELETE: Final = """
DELETE FROM record_catalog WHERE workspace_id = %s AND record_id = ANY(%s)
"""

# Only for records the batch left stored: a filtered or forgotten one has no row to name.
_UPSERT: Final = """
INSERT INTO record_catalog (record_id, workspace_id, owner_id, source_id, record_kind, project,
                            domains)
SELECT r.id, r.workspace_id, %(owner_id)s, r.source_id, r.kind, w.project,
       string_to_array(w.domains, ' ')
FROM unnest(%(ids)s::text[], %(projects)s::text[], %(domains)s::text[])
    AS w (id, project, domains)
JOIN record r ON r.id = w.id AND r.workspace_id = %(workspace_id)s
             AND r.source_id = %(source_id)s
ON CONFLICT (record_id) DO UPDATE
    SET project = excluded.project, domains = excluded.domains, updated_at = now()
    WHERE (record_catalog.project, record_catalog.domains)
          IS DISTINCT FROM (excluded.project, excluded.domains)
"""


async def write_record_catalog(
    conn: AsyncConnection[Any],
    *,
    workspace_id: UUID,
    owner_id: UUID,
    source_id: UUID,
    records: Sequence[PreparedRecord],
    ids: Mapping[int, str],
) -> int:
    """Make ``record_catalog`` say what ``records`` (by their index in the batch, ``ids``) were
    pushed with; the rows written or deleted. In the caller's transaction."""
    placed = [record for record in records if record.project is not None]
    unplaced = [ids[record.index] for record in records if record.project is None]
    changed = 0
    if unplaced:
        cur = await conn.execute(_DELETE, (workspace_id, sorted(unplaced)))
        changed += max(cur.rowcount, 0)
    if placed:
        placed.sort(key=lambda record: ids[record.index])
        cur = await conn.execute(
            _UPSERT,
            {
                "owner_id": owner_id,
                "workspace_id": workspace_id,
                "source_id": source_id,
                "ids": [ids[record.index] for record in placed],
                "projects": [record.project for record in placed],
                "domains": [" ".join(record.domains) for record in placed],
            },
        )
        changed += max(cur.rowcount, 0)
    return changed
