"""Resolving the persons of records at ingest, and the lock that orders every change of persons.

:func:`link_records` runs inside the transaction of an ingest batch, for the records whose rows
the batch wrote. For each record it parses the payload as stored, redacted if redaction changed
it (:func:`memini.payloads.load_payload`), reads its identities
(:func:`~memini.persons.extract_occurrences`), finds or makes the person of each, and writes the
record's links (``record_person``) so that they are exactly what the payload says.

**Resolution of one identity**, deterministic keys first:

1. Its email, then its chat user id: the first that an alias of the workspace holds gives the
   person. Keys of the identity that no alias holds yet are added to that person, as the payload
   shows them together; a key another person holds stays with it (two persons are made one only
   by the user, :func:`~memini.persons.merge_person`).
2. With keys but none held: a new person, named by the identity's name or else its key, with an
   alias for each key. The name is not made an alias: a name is not a key of a person that has
   keys.
3. Without keys, its name as it stands: a ``chat_user_id`` alias equal to it (chats name people
   by their user ids), then a ``display_name`` alias holding the same normalized name, gives the
   person; else a new person with a ``display_name`` alias. So an unmatched name stays a person of
   its own until the user merges it.

The person found is the one the alias names, not its canonical person: a link to a merged person
is read through its merge, and a split gives it back unchanged. Identities are resolved in the
order the batch shows them (records by id, each payload in its order), so the same batch always
makes the same persons.

**Idempotent.** A key that is held makes nothing. Links are compared with the stored ones and
only differences are written. A batch sent again writes no record rows, so this does not even
run; a record written again with the same persons changes no row here.

**Locking.** Persons and aliases are made under a transaction advisory lock on the workspace
(:func:`person_lock_key`), which every change of persons in :mod:`memini.persons` takes, so two
batches cannot make two persons for one key, and a merge cannot interleave with a resolution.
Lookups that find every key take no lock: in a settled workspace batches do not wait for each
other here.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.payloads import PayloadError, load_payload
from memini.persons.identities import (
    Identity,
    Occurrence,
    extract_occurrences,
    normalize_chat_user_id,
)
from memini.persons.model import AliasKind, LinkCounts, Role

__all__ = [
    "PERSON_LOCK_NAMESPACE",
    "LinkableRecord",
    "link_records",
    "lock_persons",
    "person_lock_key",
]

type Conn = AsyncConnection[TupleRow]

PERSON_LOCK_NAMESPACE: Final = 0x6D707273
"""First key of the transaction advisory lock on the persons of a workspace: "mprs" in ASCII."""


def person_lock_key(workspace_id: UUID) -> tuple[int, int]:
    """The two int4 keys of the advisory lock on the persons of a workspace."""
    digest = hashlib.blake2b(workspace_id.bytes, digest_size=4).digest()
    return PERSON_LOCK_NAMESPACE, int.from_bytes(digest, "big", signed=True)


async def lock_persons(conn: Conn, workspace_id: UUID) -> None:
    """Take the lock on the persons of the workspace until the transaction ends."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", person_lock_key(workspace_id))


class LinkableRecord(Protocol):
    """What resolution reads of a stored record; :class:`memini.ingest.StoredRecord` is one."""

    @property
    def id(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def payload(self) -> bytes:
        """The payload as stored: canonical JSON."""
        ...


type _Key = tuple[AliasKind, str]
type _LinkKey = tuple[str, UUID, Role, str | None]


@dataclass(slots=True)
class _Plan:
    """What resolving the identities against the aliases found needs written."""

    persons: dict[Identity, UUID | int] = field(default_factory=dict[Identity, "UUID | int"])
    """The person of each identity: an id, or the index of a new person in ``new``."""
    new: list[Identity] = field(default_factory=list[Identity])
    """Persons to make, by the identity that makes each."""
    aliases: dict[_Key, tuple[UUID | int, str]] = field(
        default_factory=dict[_Key, "tuple[UUID | int, str]"]
    )
    """Aliases to add: key -> (person, value as seen)."""

    @property
    def writes(self) -> bool:
        return bool(self.new or self.aliases)


def _plan(identities: Sequence[Identity], held: Mapping[_Key, UUID]) -> _Plan:
    """Resolve each identity in order against the aliases ``held`` and the ones planned before."""
    plan = _Plan()
    known: dict[_Key, UUID | int] = dict(held)
    for identity in identities:
        keys = identity.deterministic_keys()
        if keys:
            person = next((known[key] for key in keys if key in known), None)
            if person is None:
                plan.new.append(identity)
                person = len(plan.new) - 1
            for key in keys:
                if key not in known:
                    known[key] = person
                    plan.aliases[key] = (person, key[1])
        else:
            name_key = identity.name_key()
            if name_key is None or identity.name is None:  # an identity has a field
                continue
            key: _Key = ("display_name", name_key)
            person = known.get(_bare_chat_key(identity.name), known.get(key))
            if person is None:
                plan.new.append(identity)
                person = len(plan.new) - 1
                known[key] = person
                plan.aliases[key] = (person, identity.name)
        plan.persons[identity] = person
    return plan


async def _held(conn: Conn, workspace_id: UUID, keys: Sequence[_Key]) -> dict[_Key, UUID]:
    if not keys:
        return {}
    cur = await conn.execute(
        "SELECT a.kind, a.normalized_value, a.person_id FROM alias a"
        " JOIN unnest(%s::text[], %s::text[]) AS k (kind, value)"
        "   ON a.kind = k.kind AND a.normalized_value = k.value"
        " WHERE a.workspace_id = %s",
        ([kind for kind, _ in keys], [value for _, value in keys], workspace_id),
    )
    return {(kind, value): person for kind, value, person in await cur.fetchall()}


def _bare_chat_key(name: str) -> _Key:
    """A name without keys looked up as a chat user id, as it stands."""
    return ("chat_user_id", normalize_chat_user_id(name) or name)


def _keys(identities: Sequence[Identity]) -> list[_Key]:
    found: dict[_Key, None] = {}
    for identity in identities:
        keys = identity.deterministic_keys()
        if keys:
            found.update(dict.fromkeys(keys))
        else:
            name_key = identity.name_key()
            if name_key is not None and identity.name is not None:
                found[_bare_chat_key(identity.name)] = None
                found[("display_name", name_key)] = None
    return list(found)


async def _write_plan(
    conn: Conn, workspace_id: UUID, owner_id: UUID, plan: _Plan, counts: LinkCounts
) -> dict[Identity, UUID]:
    """Make the persons and aliases of ``plan``; the person id of every identity."""
    ids: list[UUID] = []
    if plan.new:
        cur = await conn.execute(
            "SELECT uuidv7() FROM generate_series(1, %s) ORDER BY 1", (len(plan.new),)
        )
        ids = [row[0] for row in await cur.fetchall()]
        await conn.execute(
            "INSERT INTO person (id, workspace_id, owner_id, display_name)"
            " SELECT u.id, %s, %s, u.name FROM unnest(%s::uuid[], %s::text[]) AS u (id, name)",
            (workspace_id, owner_id, ids, [identity.label for identity in plan.new]),
        )
        counts.persons_created += len(ids)

    def resolved(person: UUID | int) -> UUID:
        return ids[person] if isinstance(person, int) else person

    if plan.aliases:
        rows = sorted(
            (kind, value, shown, resolved(person))
            for (kind, value), (person, shown) in plan.aliases.items()
        )
        await conn.execute(
            "INSERT INTO alias (workspace_id, owner_id, person_id, kind, value, normalized_value)"
            " SELECT %s, %s, u.person_id, u.kind, u.shown, u.value"
            " FROM unnest(%s::uuid[], %s::text[], %s::text[], %s::text[])"
            "   AS u (person_id, kind, shown, value)",
            (
                workspace_id,
                owner_id,
                [row[3] for row in rows],
                [row[0] for row in rows],
                [row[2] for row in rows],
                [row[1] for row in rows],
            ),
        )
        counts.aliases_created += len(rows)
    return {identity: resolved(person) for identity, person in plan.persons.items()}


def _desired(
    records: Sequence[LinkableRecord],
    occurrences: Mapping[str, Sequence[Occurrence]],
    persons: Mapping[Identity, UUID],
) -> dict[_LinkKey, datetime]:
    """The links each record should have, with their times: the latest of repeated ones."""
    desired: dict[_LinkKey, datetime] = {}
    for record in records:
        for occurrence in occurrences[record.id]:
            person = persons.get(occurrence.identity)
            if person is None:
                continue
            key: _LinkKey = (record.id, person, occurrence.role, occurrence.anchor)
            seen = desired.get(key)
            if seen is None or occurrence.occurred_at > seen:
                desired[key] = occurrence.occurred_at
    return desired


async def _write_links(
    conn: Conn,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    source_id: UUID,
    records: Sequence[LinkableRecord],
    desired: Mapping[_LinkKey, datetime],
    counts: LinkCounts,
) -> None:
    ids = sorted(record.id for record in records)
    cur = await conn.execute(
        "SELECT record_id, person_id, role, anchor, occurred_at FROM record_person"
        " WHERE record_id = ANY(%s::text[]) AND workspace_id = %s",
        (ids, workspace_id),
    )
    stored: dict[_LinkKey, datetime] = {
        (record_id, person_id, role, anchor): at
        for record_id, person_id, role, anchor, at in await cur.fetchall()
    }
    gone = sorted((key for key in stored if key not in desired), key=_order)
    new = sorted((key for key in desired if key not in stored), key=_order)
    moved = sorted(
        (key for key in desired if key in stored and stored[key] != desired[key]), key=_order
    )
    if gone:
        await conn.execute(
            "DELETE FROM record_person l"
            " USING unnest(%s::text[], %s::uuid[], %s::text[], %s::text[])"
            "   AS g (record_id, person_id, role, anchor)"
            " WHERE l.record_id = g.record_id AND l.person_id = g.person_id"
            " AND l.role = g.role AND l.anchor IS NOT DISTINCT FROM g.anchor",
            _columns(gone),
        )
        counts.links_deleted += len(gone)
    if moved:
        await conn.execute(
            "UPDATE record_person l SET occurred_at = m.occurred_at"
            " FROM unnest(%s::text[], %s::uuid[], %s::text[], %s::text[], %s::timestamptz[])"
            "   AS m (record_id, person_id, role, anchor, occurred_at)"
            " WHERE l.record_id = m.record_id AND l.person_id = m.person_id"
            " AND l.role = m.role AND l.anchor IS NOT DISTINCT FROM m.anchor",
            (*_columns(moved), [desired[key] for key in moved]),
        )
        counts.links_updated += len(moved)
    if new:
        kinds = {record.id: record.kind for record in records}
        await conn.execute(
            "INSERT INTO record_person (workspace_id, owner_id, source_id, record_kind, record_id,"
            "                           person_id, role, anchor, occurred_at)"
            " SELECT %s, %s, %s, u.kind, u.record_id, u.person_id, u.role, u.anchor, u.occurred_at"
            " FROM unnest(%s::text[], %s::text[], %s::uuid[], %s::text[], %s::text[],"
            "             %s::timestamptz[])"
            "   AS u (kind, record_id, person_id, role, anchor, occurred_at)",
            (
                workspace_id,
                owner_id,
                source_id,
                [kinds[key[0]] for key in new],
                *_columns(new),
                [desired[key] for key in new],
            ),
        )
        counts.links_inserted += len(new)


def _order(key: _LinkKey) -> tuple[str, str, str, str]:
    record_id, person_id, role, anchor = key
    return record_id, str(person_id), role, "" if anchor is None else "\x01" + anchor


def _columns(keys: Sequence[_LinkKey]) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    return (
        [key[0] for key in keys],
        [key[1] for key in keys],
        [key[2] for key in keys],
        [key[3] for key in keys],
    )


async def link_records(
    conn: Conn,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    source_id: UUID,
    records: Sequence[LinkableRecord],
) -> LinkCounts:
    """Resolve the persons of ``records`` and write their links (see the module).

    The ingest hook of the persons layer: :func:`memini.ingest.ingest` calls it in the batch's
    transaction with the records whose rows the batch wrote, all of the source ``source_id`` in
    the workspace ``workspace_id``. Persons and aliases it makes are owned by ``owner_id``. Runs
    in the caller's transaction; a failure rolls back with it.
    """
    counts = LinkCounts()
    if not records:
        return counts
    ordered = sorted(records, key=lambda record: record.id)
    occurrences: dict[str, list[Occurrence]] = {}
    for record in ordered:
        try:
            parsed = load_payload(record.kind, record.payload)
        except (PayloadError, ValueError):  # ingest stores none it cannot parse; and code files
            occurrences[record.id] = []
            continue
        occurrences[record.id] = extract_occurrences(parsed)
    identities = list(
        dict.fromkeys(
            occurrence.identity for record in ordered for occurrence in occurrences[record.id]
        )
    )
    keys = _keys(identities)
    plan = _plan(identities, await _held(conn, workspace_id, keys))
    if plan.writes:
        # Something to make: under the lock, against what is held once it is ours.
        await lock_persons(conn, workspace_id)
        plan = _plan(identities, await _held(conn, workspace_id, keys))
    persons = await _write_plan(conn, workspace_id, owner_id, plan, counts)
    await _write_links(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        source_id=source_id,
        records=ordered,
        desired=_desired(ordered, occurrences, persons),
        counts=counts,
    )
    return counts
