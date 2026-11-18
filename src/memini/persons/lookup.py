"""Finding persons by what a caller knows of them: "me", a person id, or an alias.

The list tools (``list_meetings``, ``list_messages``) and the ``persons`` filter of search name a
person this way, and read the records of everyone that person stands for: its canonical person
(``person_root``) and the persons merged into it (``person_members``), whose links keep naming
them (requirements, section 3). :func:`resolve_persons` gives the canonical persons a list of
references names, by workspace; :func:`member_ids` the persons they stand for, the ones
``record_person`` links name.

A reference is one of:

- ``"me"`` (:data:`ME`): the person marked with the caller's principal in each workspace, resolved
  to its canonical person. At most one per workspace; a workspace without one gives nothing.
- a person id (a :class:`~uuid.UUID`): its canonical person, in its workspace.
- any other string, an alias as sources show it. A mail address (``ann@example.test``, also
  ``Ann Example <ann@example.test>``) is looked up as an ``email``; anything else as a
  ``chat_user_id`` as it stands, then as a ``display_name``, then as a ``git_author``: the order in
  which ingest resolves a bare name (:mod:`memini.persons.resolve`), with git authors last. In each
  workspace the first kind that names someone gives the person; an alias names one person per kind,
  so a reference gives at most one person per workspace.

Every read goes through the caller's :class:`~memini.auth.ReadFilter`; a person, alias or
workspace it may not read gives nothing, as one that does not exist.
"""

from collections.abc import Collection, Sequence
from typing import Final
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from memini.auth import ReadFilter
from memini.persons.identities import identity_of, normalize_chat_user_id, normalize_name
from memini.persons.model import AliasKind

__all__ = ["ME", "PersonRef", "alias_keys", "member_ids", "parse_person_ref", "resolve_persons"]

type Conn = AsyncConnection[TupleRow]

ME: Final = "me"
"""The reference to the caller's own person."""

type PersonRef = str | UUID
"""A person as a caller names one: :data:`ME`, a person id, or an alias (see the module)."""


def parse_person_ref(value: str) -> PersonRef:
    """A reference from text, as a tool or a query string gives it: a person id when it is a
    uuid, otherwise the text itself (``"me"`` or an alias)."""
    text = value.strip()
    try:
        return UUID(text)
    except ValueError:
        return text


def alias_keys(value: str) -> tuple[tuple[AliasKind, str], ...]:
    """The aliases a reference may be, as ``(kind, normalized value)``, in the order they are
    tried: an email alone for a mail address, otherwise a chat user id, a display name and a git
    author name. Empty for a value that is no alias at all (blank, too long, control
    characters)."""
    identity = identity_of(value)
    if identity is None:
        return ()
    if identity.email is not None:
        return (("email", identity.email),)
    keys: list[tuple[AliasKind, str]] = []
    chat = normalize_chat_user_id(value)
    if chat is not None:
        keys.append(("chat_user_id", chat))
    name = normalize_name(value)
    if name is not None:
        keys.append(("display_name", name))
        keys.append(("git_author", name))
    return tuple(keys)


def _scope(access: ReadFilter, alias: str) -> sql.Composable:
    return access.where(
        workspace_id=sql.Identifier(alias, "workspace_id"),
        owner_id=sql.Identifier(alias, "owner_id"),
    )


_ME: Final = sql.SQL("""
SELECT p.workspace_id, person_root(p.id) FROM person p
WHERE p.principal_id = %s AND p.workspace_id = ANY(%s) AND {access}
""")

_BY_ID: Final = sql.SQL("""
SELECT p.workspace_id, person_root(p.id) FROM person p
WHERE p.id = ANY(%s) AND p.workspace_id = ANY(%s) AND {access}
""")

_BY_ALIAS: Final = sql.SQL("""
SELECT k.ref, k.rank, a.workspace_id, person_root(a.person_id)
FROM unnest(%s::int[], %s::int[], %s::text[], %s::text[]) AS k (ref, rank, kind, key)
JOIN alias a ON a.kind = k.kind AND a.normalized_value = k.key
WHERE a.workspace_id = ANY(%s) AND {access}
ORDER BY k.ref, a.workspace_id, k.rank
""")


async def resolve_persons(
    conn: Conn, access: ReadFilter, workspace_ids: Collection[UUID], refs: Sequence[PersonRef]
) -> dict[UUID, frozenset[UUID]]:
    """The canonical persons ``refs`` name in the workspaces ``workspace_ids``, by workspace.

    A workspace where no reference names anyone is left out; so is a reference that names no one
    (see the module). Runs in the caller's transaction, if one is open.
    """
    workspaces = sorted(workspace_ids)
    found: dict[UUID, set[UUID]] = {}
    if not workspaces or not refs:
        return {}

    def add(rows: Sequence[tuple[UUID, UUID | None]]) -> None:
        for workspace_id, root in rows:
            if root is not None:
                found.setdefault(workspace_id, set()).add(root)

    names = [ref for ref in refs if isinstance(ref, str)]
    ids = [ref for ref in refs if isinstance(ref, UUID)]
    if ME in names:
        cur = await conn.execute(
            _ME.format(access=_scope(access, "p")),
            (access.principal.principal_id, workspaces),
        )
        add([(row[0], row[1]) for row in await cur.fetchall()])
    if ids:
        cur = await conn.execute(_BY_ID.format(access=_scope(access, "p")), (ids, workspaces))
        add([(row[0], row[1]) for row in await cur.fetchall()])
    keyed = [
        (number, rank, kind, key)
        for number, name in enumerate(names)
        if name != ME
        for rank, (kind, key) in enumerate(alias_keys(name))
    ]
    if keyed:
        cur = await conn.execute(
            _BY_ALIAS.format(access=_scope(access, "a")),
            (
                [entry[0] for entry in keyed],
                [entry[1] for entry in keyed],
                [entry[2] for entry in keyed],
                [entry[3] for entry in keyed],
                workspaces,
            ),
        )
        # Per reference and workspace, the first kind that names someone: rows come in that order.
        seen: set[tuple[int, UUID]] = set()
        for ref, _rank, workspace_id, root in await cur.fetchall():
            if (ref, workspace_id) in seen:
                continue
            seen.add((ref, workspace_id))
            add([(workspace_id, root)])
    return {workspace_id: frozenset(roots) for workspace_id, roots in found.items()}


_MEMBERS: Final = """
SELECT DISTINCT m.id FROM unnest(%s::uuid[]) AS r (id)
CROSS JOIN LATERAL person_members(r.id) AS m (id)
ORDER BY m.id
"""


async def member_ids(conn: Conn, persons: Collection[UUID]) -> tuple[UUID, ...]:
    """``persons`` and everyone merged into them, directly or through others: the persons whose
    links (``record_person``) are theirs. Pass canonical persons (:func:`resolve_persons`)."""
    if not persons:
        return ()
    cur = await conn.execute(_MEMBERS, (sorted(persons),))
    return tuple(row[0] for row in await cur.fetchall())
