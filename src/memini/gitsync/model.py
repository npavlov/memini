"""What the git connector stores: the identity of a file's record, and the pin of a stream.

File records
    One record per file per tracked ref: kind ``document`` for a Markdown file, ``code_file`` for
    any other (:func:`memini.gitsync.rules.file_kind`). Its ``external_id`` is the full ref name
    and the path, joined by ``:`` (:func:`code_external_id`), so the same file on two refs is two
    records, and the ref and path can be read back (:func:`split_code_external_id`): git does not
    allow ``:`` in a ref name. The kind is part of the record id, so a file whose kind changed is
    another record. ``content_hash`` is the file's git blob id, ``occurred_at`` the committer time
    of the last commit that changed the file on the ref, as far as the sync looked back
    (:mod:`memini.gitsync.sync`, "Times"), ``stream_id`` the stream of the ref, ``payload`` NULL:
    the file itself stays in the mirror (requirements, section 3).

Pins
    ``stream.cursor`` of a git stream is a :class:`Pin`::

        {"commit": "<40 hex digits>", "rules": "<digest of the file rules>"}

    ``commit`` is the commit the stream's records were complete at, ``rules`` the digest of the
    rules they were selected by (:attr:`memini.gitsync.rules.FileRules.digest`). A sync that
    committed some records past the pin and then failed adds ``"partial": true``; the pin itself
    stays. ``checked_until`` is the database time just before the fetch that found the commit.
    Before the first complete sync both are NULL.

A ref that is gone
    When a fetch succeeds and the tracked ref is not in the mirror (deleted at the remote, or
    never there), the sync turns the ref's records into tombstones and replaces the pin with a
    marker (:func:`missing_cursor`)::

        {"missing": true, "last_commit": "<the commit it was pinned at, if any>"}

    It holds no pin (:func:`read_pin` gives ``None``): the records are complete at no commit of the
    ref. ``checked_until`` moves as for a pin, since the sync stored what the remote holds.
    :func:`ref_missing` tells the marker; :mod:`memini.status` reports it as ``missing``. If the
    ref comes back, the next sync finds no pin, lists it in full and brings its records back.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from memini.gitsync.rules import file_kind
from memini.ids import record_id

__all__ = [
    "Pin",
    "code_external_id",
    "code_record_id",
    "file_record_id",
    "missing_cursor",
    "read_pin",
    "ref_missing",
    "split_code_external_id",
    "stream_pin",
]

type Conn = AsyncConnection[TupleRow]

_OID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def code_external_id(ref: str, path: str) -> str:
    """The external id of the file at ``path`` on the tracked ref ``ref``: ``ref:path``. The same
    for a code file and a Markdown document."""
    if not ref.startswith("refs/") or ":" in ref or not path:
        raise ValueError("a code record needs a full ref name without ':' and a path")
    return f"{ref}:{path}"


def split_code_external_id(external_id: str) -> tuple[str, str]:
    """The ref and the path of a file record's external id; :class:`ValueError` if it is not one."""
    ref, separator, path = external_id.partition(":")
    if not separator or not ref.startswith("refs/") or not path:
        raise ValueError("not the external id of a code record (refs/...:path)")
    return ref, path


def code_record_id(source_id: UUID, ref: str, path: str) -> str:
    """The id of the ``code_file`` record of the file at ``path`` on ``ref`` in the repository
    ``source_id``. Every file but a Markdown one has this record; :func:`file_record_id` gives the
    record of the kind the file becomes."""
    return record_id(source_id, "code_file", code_external_id(ref, path))


def file_record_id(source_id: UUID, ref: str, path: str) -> str:
    """The record id of the file at ``path`` on ``ref`` in the repository ``source_id``, of the
    kind the file becomes (:func:`memini.gitsync.rules.file_kind`)."""
    return record_id(source_id, file_kind(path), code_external_id(ref, path))


@dataclass(frozen=True, slots=True)
class Pin:
    """The pinned commit of a stream, and the rules its files were selected by."""

    commit: str
    rules: str
    partial: bool = False
    """A sync committed records past the pin and failed: the next one lists the ref in full."""

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in ``stream.cursor``."""
        value: dict[str, Any] = {"commit": self.commit, "rules": self.rules}
        if self.partial:
            value["partial"] = True
        return value


def read_pin(cursor: object) -> Pin | None:
    """The pin in a stream's ``cursor``, or ``None`` if it holds none (no complete sync yet, or a
    value this connector did not write)."""
    if not isinstance(cursor, Mapping):
        return None
    value: Mapping[str, object] = cursor  # pyright: ignore[reportUnknownVariableType] - JSON
    commit, rules = value.get("commit"), value.get("rules")
    if not isinstance(commit, str) or _OID.fullmatch(commit) is None or not isinstance(rules, str):
        return None
    return Pin(commit=commit, rules=rules, partial=value.get("partial") is True)


def ref_missing(cursor: object) -> bool:
    """Whether a stream's ``cursor`` says its ref no longer exists at the remote."""
    if not isinstance(cursor, Mapping):
        return False
    value: Mapping[str, object] = cursor  # pyright: ignore[reportUnknownVariableType] - JSON
    return value.get("missing") is True


def missing_cursor(cursor: object) -> dict[str, Any]:
    """The cursor of a stream whose ref is gone, from the cursor it had: the commit it was pinned
    at, or that a marker already names, is kept as ``last_commit``."""
    pin = read_pin(cursor)
    last: object = None if pin is None else pin.commit
    if pin is None and isinstance(cursor, Mapping):
        value: Mapping[str, object] = cursor  # pyright: ignore[reportUnknownVariableType] - JSON
        last = value.get("last_commit")
    marker: dict[str, Any] = {"missing": True}
    if isinstance(last, str) and _OID.fullmatch(last):
        marker["last_commit"] = last
    return marker


async def stream_pin(conn: Conn, source_id: UUID, ref: str) -> Pin | None:
    """The pin of the stream of ``ref`` in the source ``source_id``, or ``None`` if the ref is not
    tracked or has not completed a sync. What a reader of code at "the pinned commit" asks for.

    Runs one statement; no authorization: the caller has checked that it may read the source.
    """
    cur = await conn.execute(
        "SELECT cursor FROM stream WHERE source_id = %s AND key = %s", (source_id, ref)
    )
    row = await cur.fetchone()
    return None if row is None else read_pin(row[0])
