"""Reading and changing persons: lists and lookups, aliases, merge and split, "me".

Reads take the caller's :class:`~memini.auth.ReadFilter`, writes its
:class:`~memini.auth.WriteAccess`; a workspace, person or alias the caller may not see raises
:class:`~memini.workspaces.NotFoundError`, as one that does not exist does. Every write runs in a
transaction of its own, or a savepoint of the caller's, under the lock on the persons of the
workspace (:func:`~memini.persons.lock_persons`) that ingest takes to make persons: changes of
persons in one workspace never interleave.

**Merge and split.** :func:`merge_person` merges a canonical person B into a person A: it sets
``B.merged_into`` and nothing else. B keeps its aliases, its links to records and the knowledge
items that cite it; reads resolve B to the canonical person of A (``Person.canonical_id``), and A
lists B's aliases with its own. :func:`split_person` clears ``B.merged_into``: B is canonical
again, with everything it had, the persons merged into B included. A merge followed by a split
leaves every row as it was before, except what ingest wrote in between, which stays with the
person whose alias it matched. A person merged into another is not merged again before it is
split; merging two persons that are one already is refused.

**Adding an alias.** An alias no one holds is added to the person. One that the person, or a
person merged into it, holds already changes nothing. One that another person holds says that the
two are one person: the canonical person of the holder is merged into this person, and a split of
it undoes that. So the same person seen as a mail address and as a chat user id becomes one person
when one of them gets the other's alias.

**"Me"** is a person marked with the caller's principal (``person.principal_id``), at most one per
workspace. Reads resolve it to its canonical person: if the person marked is merged into another,
that other one is "me".
"""

from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter, WriteAccess
from memini.persons.identities import check_display_name, normalize_alias
from memini.persons.model import (
    MAX_PERSON_PAGE,
    Alias,
    AliasAdded,
    AliasHeldError,
    AliasKind,
    MergeError,
    Person,
    PersonCursor,
    PersonPage,
)
from memini.persons.resolve import lock_persons
from memini.workspaces import NotFoundError, Workspace, get_workspace, get_writable_workspace

__all__ = [
    "DEFAULT_PERSON_PAGE",
    "add_alias",
    "create_person",
    "find_person",
    "get_me",
    "get_person",
    "list_persons",
    "merge_person",
    "remove_alias",
    "rename_person",
    "set_me",
    "split_person",
    "unset_me",
]

type Conn = AsyncConnection[TupleRow]
type Access = ReadFilter | WriteAccess

DEFAULT_PERSON_PAGE: Final = 50


def _scope(access: Access, alias: str) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
    )


def _principal(access: Access) -> UUID:
    return access.principal.principal_id if isinstance(access, ReadFilter) else access.owner_id


async def _readable_workspace(conn: Conn, access: ReadFilter, workspace: str) -> Workspace:
    found = await get_workspace(conn, access, workspace)
    if found is None:
        raise NotFoundError("workspace")
    return found


# --- Loading -------------------------------------------------------------------------------------

_PERSONS: Final = sql.SQL("""
SELECT p.id, p.display_name, person_root(p.id), p.merged_into, p.merged_at, p.created_at,
       coalesce(person_root(p.id) = (
           SELECT person_root(m.id) FROM person m
           WHERE m.workspace_id = %(workspace)s AND m.principal_id = %(principal)s
       ), false)
FROM person p
WHERE p.id = ANY(%(ids)s::uuid[]) AND p.workspace_id = %(workspace)s AND {access}
""")

_MEMBERS: Final = sql.SQL("""
SELECT p.merged_into, p.id FROM person p
WHERE p.merged_into = ANY(%(ids)s::uuid[]) AND p.workspace_id = %(workspace)s AND {access}
ORDER BY p.merged_into, p.id
""")

_ALIASES: Final = sql.SQL("""
SELECT x.id, a.id, a.person_id, a.kind, a.value, a.normalized_value, a.created_at
FROM unnest(%(ids)s::uuid[]) AS x (id)
CROSS JOIN LATERAL person_members(x.id) AS m (id)
JOIN alias a ON a.person_id = m.id
WHERE a.workspace_id = %(workspace)s AND {access}
ORDER BY x.id, a.kind, a.normalized_value
""")


async def _load(
    conn: Conn, access: Access, workspace: Workspace, ids: Sequence[UUID]
) -> list[Person]:
    """The persons ``ids`` of the workspace that ``access`` may see, in the order given."""
    if not ids:
        return []
    params: dict[str, Any] = {
        "ids": list(ids),
        "workspace": workspace.id,
        "principal": _principal(access),
    }
    cur = await conn.execute(_PERSONS.format(access=_scope(access, "p")), params)
    rows = {row[0]: row for row in await cur.fetchall()}
    cur = await conn.execute(_MEMBERS.format(access=_scope(access, "p")), params)
    members: dict[UUID, list[UUID]] = {}
    for parent, member in await cur.fetchall():
        members.setdefault(parent, []).append(member)
    cur = await conn.execute(_ALIASES.format(access=_scope(access, "a")), params)
    aliases: dict[UUID, list[Alias]] = {}
    for of, alias_id, person_id, kind, value, normalized, created_at in await cur.fetchall():
        aliases.setdefault(of, []).append(
            Alias(alias_id, person_id, kind, value, normalized, created_at)
        )
    found: list[Person] = []
    for person_id in ids:
        row = rows.get(person_id)
        if row is None:
            continue
        found.append(
            Person(
                id=row[0],
                workspace_id=workspace.id,
                workspace=workspace.name,
                display_name=row[1],
                canonical_id=row[2],
                merged_into=row[3],
                merged_at=row[4],
                members=tuple(members.get(person_id, ())),
                aliases=tuple(aliases.get(person_id, ())),
                is_me=row[6],
                created_at=row[5],
            )
        )
    return found


async def _one(conn: Conn, access: Access, workspace: Workspace, person_id: UUID) -> Person:
    found = await _load(conn, access, workspace, [person_id])
    if not found:
        raise NotFoundError("person")
    return found[0]


async def _root(conn: Conn, person_id: UUID) -> UUID:
    cur = await conn.execute("SELECT person_root(%s)", (person_id,))
    row = await cur.fetchone()
    if row is None or row[0] is None:
        raise NotFoundError("person")
    root: UUID = row[0]
    return root


# --- Reads ---------------------------------------------------------------------------------------


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_LIST: Final = sql.SQL("""
SELECT p.id, p.display_name FROM person p
WHERE p.workspace_id = %(workspace)s AND {access}
  AND (%(merged)s OR p.merged_into IS NULL)
  AND (%(pattern)s::text IS NULL
       OR p.display_name ILIKE %(pattern)s
       OR EXISTS (SELECT 1 FROM person_members(p.id) AS m (id)
                  JOIN alias a ON a.person_id = m.id
                  WHERE a.value ILIKE %(pattern)s OR a.normalized_value ILIKE %(pattern)s))
  AND (%(after_id)s::uuid IS NULL
       OR (p.display_name, p.id) > (%(after_name)s::text, %(after_id)s::uuid))
ORDER BY p.display_name, p.id
LIMIT %(limit)s
""")


async def list_persons(
    conn: Conn,
    access: ReadFilter,
    workspace: str,
    *,
    query: str | None = None,
    merged: bool = False,
    limit: int = DEFAULT_PERSON_PAGE,
    after: PersonCursor | None = None,
) -> PersonPage:
    """The persons of a workspace by display name, a page at a time.

    Args:
        query: only persons whose display name, or an alias of anyone merged into them, contains
            it, ignoring case.
        merged: list persons merged into others too; by default only canonical persons, each
            standing for everyone merged into it.
        limit: persons per page, 1 to :data:`~memini.persons.MAX_PERSON_PAGE`.
        after: the ``next`` of the previous page.

    Raises:
        NotFoundError: no such workspace that ``access`` may read.
        ValueError: ``limit`` is out of range.
    """
    if not 1 <= limit <= MAX_PERSON_PAGE:
        raise ValueError(f"limit is 1 to {MAX_PERSON_PAGE}")
    found = await _readable_workspace(conn, access, workspace)
    text = None if query is None else query.strip() or None
    cur = await conn.execute(
        _LIST.format(access=_scope(access, "p")),
        {
            "workspace": found.id,
            "merged": merged,
            "pattern": None if text is None else _like(text),
            "after_id": None if after is None else after.id,
            "after_name": None if after is None else after.display_name,
            "limit": limit + 1,
        },
    )
    rows = await cur.fetchall()
    page = rows[:limit]
    persons = await _load(conn, access, found, [row[0] for row in page])
    following = None
    if len(rows) > limit:
        last = page[-1]
        following = PersonCursor(display_name=last[1], id=last[0])
    return PersonPage(persons=tuple(persons), next=following)


async def get_person(conn: Conn, access: ReadFilter, workspace: str, person_id: UUID) -> Person:
    """One person, as it is: merged or canonical, with the aliases of everyone merged into it.

    Raises:
        NotFoundError: no such workspace or person that ``access`` may read.
    """
    found = await _readable_workspace(conn, access, workspace)
    return await _one(conn, access, found, person_id)


async def find_person(
    conn: Conn, access: ReadFilter, workspace: str, kind: AliasKind, value: str
) -> Person:
    """The canonical person an alias resolves to: the lookup ingest makes.

    Raises:
        InvalidAliasError: ``value`` is not valid for ``kind``.
        NotFoundError: no such workspace, or no person with the alias, that ``access`` may read.
    """
    _, key = normalize_alias(kind, value)
    found = await _readable_workspace(conn, access, workspace)
    query = sql.SQL(
        "SELECT person_root(a.person_id) FROM alias a"
        " WHERE a.workspace_id = %s AND a.kind = %s AND a.normalized_value = %s AND {}"
    ).format(_scope(access, "a"))
    cur = await conn.execute(query, (found.id, kind, key))
    row = await cur.fetchone()
    if row is None or row[0] is None:
        raise NotFoundError("person")
    return await _one(conn, access, found, row[0])


async def get_me(conn: Conn, access: ReadFilter, workspace: str) -> Person | None:
    """The caller's "me" in the workspace, resolved to its canonical person; ``None`` if no person
    is marked.

    Raises:
        NotFoundError: no such workspace that ``access`` may read.
    """
    found = await _readable_workspace(conn, access, workspace)
    query = sql.SQL(
        "SELECT person_root(p.id) FROM person p"
        " WHERE p.workspace_id = %s AND p.principal_id = %s AND {}"
    ).format(_scope(access, "p"))
    cur = await conn.execute(query, (found.id, _principal(access)))
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return await _one(conn, access, found, row[0])


# --- Writes --------------------------------------------------------------------------------------


async def _writable(conn: Conn, access: WriteAccess, workspace: str) -> Workspace:
    """The workspace, locked for changes of its persons until the transaction ends."""
    found = await get_writable_workspace(conn, access, workspace)
    await lock_persons(conn, found.id)
    return found


async def _person_row(
    conn: Conn, access: WriteAccess, workspace: Workspace, person_id: UUID
) -> UUID | None:
    """The person's merged_into; NotFoundError if there is no such person ``access`` may change."""
    query = sql.SQL(
        "SELECT p.merged_into FROM person p WHERE p.id = %s AND p.workspace_id = %s AND {}"
    )
    cur = await conn.execute(query.format(_scope(access, "p")), (person_id, workspace.id))
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("person")
    merged_into: UUID | None = row[0]
    return merged_into


async def create_person(
    conn: Conn,
    access: WriteAccess,
    workspace: str,
    display_name: str,
    aliases: Sequence[tuple[AliasKind, str]] = (),
) -> Person:
    """Make a person by hand, with aliases no one holds: for example "me", before any record
    names it.

    Raises:
        InvalidAliasError: the display name or an alias is not valid.
        AliasHeldError: another person holds an alias; add the alias to it, or merge.
        NotFoundError: no such workspace that ``access`` may change.
    """
    name = check_display_name(display_name)
    keys: dict[tuple[AliasKind, str], str] = {}
    for kind, value in aliases:
        shown, key = normalize_alias(kind, value)
        keys.setdefault((kind, key), shown)
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        for kind, key in keys:
            cur = await conn.execute(
                "SELECT person_id FROM alias"
                " WHERE workspace_id = %s AND kind = %s AND normalized_value = %s",
                (found.id, kind, key),
            )
            row = await cur.fetchone()
            if row is not None:
                raise AliasHeldError(row[0])
        cur = await conn.execute(
            "INSERT INTO person (workspace_id, owner_id, display_name) VALUES (%s, %s, %s)"
            " RETURNING id",
            (found.id, access.owner_id, name),
        )
        row = await cur.fetchone()
        if row is None:  # INSERT ... RETURNING returns the row
            raise RuntimeError("INSERT INTO person returned no row")
        person_id: UUID = row[0]
        for (kind, key), shown in keys.items():
            await conn.execute(
                "INSERT INTO alias (workspace_id, owner_id, person_id, kind, value,"
                " normalized_value) VALUES (%s, %s, %s, %s, %s, %s)",
                (found.id, access.owner_id, person_id, kind, shown, key),
            )
        return await _one(conn, access, found, person_id)


async def rename_person(
    conn: Conn, access: WriteAccess, workspace: str, person_id: UUID, display_name: str
) -> Person:
    """Change the display name of a person. Ingest never changes it.

    Raises:
        InvalidAliasError: the name is not valid.
        NotFoundError: no such workspace or person that ``access`` may change.
    """
    name = check_display_name(display_name)
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        await _person_row(conn, access, found, person_id)
        await conn.execute("UPDATE person SET display_name = %s WHERE id = %s", (name, person_id))
        return await _one(conn, access, found, person_id)


async def add_alias(
    conn: Conn, access: WriteAccess, workspace: str, person_id: UUID, kind: AliasKind, value: str
) -> AliasAdded:
    """Add an alias to a person; if another person holds it, merge that one into this one (see
    the module).

    Raises:
        InvalidAliasError: ``value`` is not valid for ``kind``.
        NotFoundError: no such workspace or person that ``access`` may change.
    """
    shown, key = normalize_alias(kind, value)
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        await _person_row(conn, access, found, person_id)
        cur = await conn.execute(
            "SELECT id, person_id, kind, value, normalized_value, created_at FROM alias"
            " WHERE workspace_id = %s AND kind = %s AND normalized_value = %s",
            (found.id, kind, key),
        )
        row = await cur.fetchone()
        merged: UUID | None = None
        if row is None:
            cur = await conn.execute(
                "INSERT INTO alias (workspace_id, owner_id, person_id, kind, value,"
                " normalized_value) VALUES (%s, %s, %s, %s, %s, %s)"
                " RETURNING id, person_id, kind, value, normalized_value, created_at",
                (found.id, access.owner_id, person_id, kind, shown, key),
            )
            row = await cur.fetchone()
            if row is None:  # INSERT ... RETURNING returns the row
                raise RuntimeError("INSERT INTO alias returned no row")
            created = True
        else:
            created = False
            holder = await _root(conn, row[1])
            if holder != await _root(conn, person_id):
                await _merge(conn, holder, person_id)
                merged = holder
        alias = Alias(*row)
        person = await _one(conn, access, found, person_id)
        return AliasAdded(person=person, alias=alias, created=created, merged=merged)


async def remove_alias(
    conn: Conn, access: WriteAccess, workspace: str, person_id: UUID, alias_id: UUID
) -> Person:
    """Remove an alias of a person or of a person merged into it. Links stay; a record written
    again with that key then resolves anew.

    Raises:
        NotFoundError: no such workspace, person or alias of the person that ``access`` may
            change.
    """
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        await _person_row(conn, access, found, person_id)
        query = sql.SQL(
            "DELETE FROM alias a WHERE a.id = %s AND a.workspace_id = %s"
            " AND a.person_id IN (SELECT person_members(%s)) AND {} RETURNING a.id"
        ).format(_scope(access, "a"))
        cur = await conn.execute(query, (alias_id, found.id, person_id))
        if await cur.fetchone() is None:
            raise NotFoundError("alias")
        return await _one(conn, access, found, person_id)


async def _merge(conn: Conn, person_id: UUID, into: UUID) -> None:
    await conn.execute(
        "UPDATE person SET merged_into = %s, merged_at = now() WHERE id = %s", (into, person_id)
    )


async def merge_person(
    conn: Conn, access: WriteAccess, workspace: str, person_id: UUID, into: UUID
) -> Person:
    """Merge the person ``person_id`` into the person ``into``; returns the canonical person they
    are now. :func:`split_person` of ``person_id`` undoes it.

    Raises:
        MergeError: ``already_merged`` if ``person_id`` is merged into another person;
            ``same_person`` if the two are one person already.
        NotFoundError: no such workspace or person that ``access`` may change.
    """
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        merged_into = await _person_row(conn, access, found, person_id)
        await _person_row(conn, access, found, into)
        if merged_into is not None:
            raise MergeError("already_merged")
        root = await _root(conn, into)
        if root == person_id:
            raise MergeError("same_person")
        await _merge(conn, person_id, into)
        return await _one(conn, access, found, root)


async def split_person(conn: Conn, access: WriteAccess, workspace: str, person_id: UUID) -> Person:
    """Undo the merge of ``person_id``: it is a canonical person again, with what it had.

    Raises:
        MergeError: ``not_merged`` if it is not merged into another person.
        NotFoundError: no such workspace or person that ``access`` may change.
    """
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        if await _person_row(conn, access, found, person_id) is None:
            raise MergeError("not_merged")
        await conn.execute(
            "UPDATE person SET merged_into = NULL, merged_at = NULL WHERE id = %s", (person_id,)
        )
        return await _one(conn, access, found, person_id)


async def set_me(conn: Conn, access: WriteAccess, workspace: str, person_id: UUID) -> Person:
    """Mark the person as the caller's "me" in the workspace, instead of any other.

    Raises:
        NotFoundError: no such workspace or person that ``access`` may change.
    """
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        await _person_row(conn, access, found, person_id)
        await conn.execute(
            "UPDATE person SET principal_id = NULL"
            " WHERE workspace_id = %s AND principal_id = %s AND id <> %s",
            (found.id, access.owner_id, person_id),
        )
        await conn.execute(
            "UPDATE person SET principal_id = %s"
            " WHERE id = %s AND principal_id IS DISTINCT FROM %s",
            (access.owner_id, person_id, access.owner_id),
        )
        return await _one(conn, access, found, person_id)


async def unset_me(conn: Conn, access: WriteAccess, workspace: str) -> None:
    """Mark no person as the caller's "me" in the workspace.

    Raises:
        NotFoundError: no such workspace that ``access`` may change.
    """
    async with conn.transaction():
        found = await _writable(conn, access, workspace)
        await conn.execute(
            "UPDATE person SET principal_id = NULL WHERE workspace_id = %s AND principal_id = %s",
            (found.id, access.owner_id),
        )
