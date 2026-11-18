"""Principals and workspaces: who owns rows, and the units that isolate them.

v1 has one principal, the user. It must exist before it can own a workspace or a token, so setup
runs :func:`ensure_principal` first, then :func:`create_workspace` and
:func:`memini.auth.create_token`. Reads made for a request take the
:class:`~memini.auth.ReadFilter` of the caller, so they cannot bypass the authorization filter.

The CLI is the exception: it acts for a principal with the database's own credentials, not with a
token, so there is no token to filter by. It lists what that principal owns,
:func:`list_owned_workspaces`, as :func:`memini.auth.list_tokens` lists their tokens.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter

__all__ = [
    "HANDLE_PATTERN",
    "Workspace",
    "check_handle",
    "create_workspace",
    "ensure_principal",
    "get_workspace",
    "list_owned_workspaces",
    "list_workspaces",
]

HANDLE_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
"""Names of principals, workspaces and sources: lowercase letters, digits, ``-`` and ``_``, starting
with a letter or digit, at most 63 characters. The database checks the same pattern."""


def check_handle(kind: str, value: str) -> str:
    """``value`` if it is a valid handle; raises ``ValueError`` naming ``kind`` if not."""
    if HANDLE_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"a {kind} name is 1 to 63 lowercase letters, digits, '-' or '_',"
            " starting with a letter or digit"
        )
    return value


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    owner_id: UUID
    name: str
    created_at: datetime


_WORKSPACE_COLUMNS: Final = sql.SQL("w.id, w.owner_id, w.name, w.created_at")


def _workspace(row: Sequence[Any]) -> Workspace:
    return Workspace(id=row[0], owner_id=row[1], name=row[2], created_at=row[3])


async def ensure_principal(conn: AsyncConnection[TupleRow], name: str) -> UUID:
    """The id of the principal ``name``, created if it does not exist. Idempotent.

    Raises:
        ValueError: ``name`` is not a valid handle.
    """
    check_handle("principal", name)
    cur = await conn.execute(
        "INSERT INTO principal (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
        (name,),
    )
    row = await cur.fetchone()
    if row is None:  # it existed
        cur = await conn.execute("SELECT id FROM principal WHERE name = %s", (name,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"principal {name} vanished during ensure_principal")
    principal_id: UUID = row[0]
    return principal_id


async def create_workspace(
    conn: AsyncConnection[TupleRow], *, owner_id: UUID, name: str
) -> Workspace:
    """Create the workspace ``name`` owned by ``owner_id``.

    Raises:
        ValueError: ``name`` is not a valid handle.
        psycopg.errors.UniqueViolation: a workspace of that name exists.
        psycopg.errors.ForeignKeyViolation: the owner does not exist.
    """
    check_handle("workspace", name)
    cur = await conn.execute(
        sql.SQL("INSERT INTO workspace AS w (owner_id, name) VALUES (%s, %s) RETURNING {}").format(
            _WORKSPACE_COLUMNS
        ),
        (owner_id, name),
    )
    row = await cur.fetchone()
    if row is None:  # INSERT ... RETURNING always returns the row
        raise RuntimeError("INSERT INTO workspace returned no row")
    return _workspace(row)


def _readable(access: ReadFilter) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
    )


async def list_workspaces(conn: AsyncConnection[TupleRow], access: ReadFilter) -> list[Workspace]:
    """The workspaces ``access`` may read, by name."""
    query = sql.SQL("SELECT {} FROM workspace w WHERE {} ORDER BY w.name").format(
        _WORKSPACE_COLUMNS, _readable(access)
    )
    cur = await conn.execute(query)
    return [_workspace(row) for row in await cur.fetchall()]


async def get_workspace(
    conn: AsyncConnection[TupleRow], access: ReadFilter, name: str
) -> Workspace | None:
    """The workspace ``name``, or ``None`` if there is none that ``access`` may read.

    A workspace the caller may not read and one that does not exist look the same.
    """
    query = sql.SQL("SELECT {} FROM workspace w WHERE w.name = %s AND {}").format(
        _WORKSPACE_COLUMNS, _readable(access)
    )
    cur = await conn.execute(query, (name,))
    row = await cur.fetchone()
    return None if row is None else _workspace(row)


async def list_owned_workspaces(
    conn: AsyncConnection[TupleRow], *, owner_id: UUID
) -> list[Workspace]:
    """The workspaces ``owner_id`` owns, by name.

    For the CLI. A read made for a request uses :func:`list_workspaces` with the caller's filter.
    """
    query = sql.SQL("SELECT {} FROM workspace w WHERE w.owner_id = %s ORDER BY w.name").format(
        _WORKSPACE_COLUMNS
    )
    cur = await conn.execute(query, (owner_id,))
    return [_workspace(row) for row in await cur.fetchall()]
