"""The synthetic comms fixtures of ``eval/comms``: the batches, the cast and the labels, and a
loader that ingests the batches through :func:`memini.ingest.ingest`, one batch after another.

Every value there is invented (``eval/comms/SYNTHETIC.md``); ``eval/comms/README.md`` tells the
story and describes the files. In short: ``batches/<source>/NN-<stream>.json`` are push API
bodies, delivered in the order of ``NN`` within a source; ``people.json`` is the cast with each
person's aliases; ``labels/*.json`` are the answers of the M2 checks and the decision eval.

The batches are a timeline: a late reply, an edit or a deletion arrives in a later batch than the
record it changes, so sending them all again would write the older versions on the way. A second
sync sends the source as it stands now: :func:`second_sync`.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Final, NamedTuple, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from memini.api.ingest import BatchBody, RecordBody
from memini.auth import WriteAccess
from memini.ids import RecordKind, record_id
from memini.ingest import Batch, Checkpoint, IngestResult, RecordIndexer, WindowClaim, ingest
from memini.ingest.checkpoint import advance_checkpoint
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace

__all__ = [
    "BATCHES",
    "COMMS",
    "SOURCES",
    "WORKSPACE",
    "Delivery",
    "FinalRecord",
    "RecordRef",
    "anchor_time",
    "anchors_of",
    "attended",
    "checked_ranges",
    "create_sources",
    "decision_labels",
    "deliveries",
    "delivery",
    "final_records",
    "load",
    "me",
    "messages_to",
    "names_of",
    "parse_time",
    "people",
    "person_of",
    "query_labels",
    "record_id_of",
    "second_sync",
    "send",
    "timeline_labels",
]

type Json = dict[str, Any]
type Conn = AsyncConnection[TupleRow]

COMMS: Final = Path(__file__).resolve().parents[1] / "eval" / "comms"
BATCHES: Final = COMMS / "batches"
WORKSPACE: Final = "harbor"
"""The workspace of the fixtures; the code suite's repositories tidewater and beacon live in a
workspace of this name too."""
SOURCES: Final = ("chat", "mail", "meetings", "docs")
"""The push sources, one directory of ``batches`` each."""


def _json(path: Path) -> Json:
    data: Json = json.loads(path.read_text(encoding="utf-8"))
    return data


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class RecordRef(NamedTuple):
    """A record of the fixtures as the labels name it."""

    source: str
    kind: str
    external_id: str

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> "RecordRef":
        """From a label's ``{"source", "record_kind", "external_id"}``."""
        return cls(str(data["source"]), str(data["record_kind"]), str(data["external_id"]))


# --- The batches ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delivery:
    """One batch file: the body of one request to the push API."""

    source: str
    name: str
    """The file's path under ``batches``, e.g. ``chat/01-tidewater.json``."""
    body: Json

    @property
    def batch(self) -> Batch:
        """The batch, read as the push API reads the body."""
        return BatchBody.model_validate(self.body).to_batch()

    @property
    def stream(self) -> str | None:
        stream = self.body.get("stream")
        return None if stream is None else str(stream)

    @property
    def window(self) -> Json | None:
        window = self.body.get("window")
        return None if window is None else cast("Json", window)

    @property
    def items(self) -> list[Json]:
        """The records and tombstones, as in the file."""
        return cast("list[Json]", self.body["records"])

    def item(self, ref: RecordRef) -> Json | None:
        """The item of the batch for ``ref``, if it holds one."""
        if ref.source != self.source:
            return None
        for item in self.items:
            if (item["kind"], item["external_id"]) == (ref.kind, ref.external_id):
                return item
        return None


@cache
def deliveries() -> tuple[Delivery, ...]:
    """Every batch, source by source, each source's in the order of its file names."""
    found: list[Delivery] = []
    for source in SOURCES:
        for path in sorted((BATCHES / source).glob("*.json")):
            found.append(Delivery(source, f"{source}/{path.name}", _json(path)))
    return tuple(found)


def delivery(name: str) -> Delivery:
    """The batch file ``name`` (``chat/01-tidewater.json``)."""
    for each in deliveries():
        if each.name == name:
            return each
    raise KeyError(name)


@dataclass(frozen=True, slots=True)
class FinalRecord:
    """A record as it stands after every batch: its last version, and whether it is deleted."""

    ref: RecordRef
    stream: str | None
    item: Json
    """The last batch item that held the record (not its tombstone)."""
    deleted: bool = False

    @property
    def occurred_at(self) -> datetime:
        return parse_time(self.item["occurred_at"])

    @property
    def payload(self) -> Json:
        return cast("Json", self.item["payload"])


@cache
def final_records() -> dict[RecordRef, FinalRecord]:
    """What the store holds after every batch, ingested in order: the last version of each
    record, with tombstones and the records a snapshot left out marked deleted."""
    state: dict[RecordRef, FinalRecord] = {}
    for each in deliveries():
        held: set[RecordRef] = set()
        for item in each.items:
            ref = RecordRef(each.source, item["kind"], item["external_id"])
            held.add(ref)
            if item.get("deleted"):
                if ref in state:
                    state[ref] = replace(state[ref], deleted=True)
            else:
                state[ref] = FinalRecord(ref, each.stream, item)
        window = each.window
        if window is not None and window.get("snapshot"):
            start, end = parse_time(window["from"]), parse_time(window["to"])
            for ref, record in list(state.items()):
                if (
                    ref.source == each.source
                    and record.stream == each.stream
                    and not record.deleted
                    and start <= record.occurred_at < end
                    and ref not in held
                ):
                    state[ref] = replace(record, deleted=True)
    return state


def checked_ranges(
    until: str | None = None,
) -> dict[tuple[str, str], tuple[datetime | None, datetime | None]]:
    """``(checked_from, checked_until)`` of every stream after the batches up to and including
    the file ``until`` (all of them by default), as the store moves them
    (:func:`memini.ingest.checkpoint.advance_checkpoint`). Raises
    :class:`~memini.ingest.WindowGapError` where a complete window leaves a gap."""
    points: dict[tuple[str, str], Checkpoint] = {}
    for each in deliveries():
        batch = each.batch
        if batch.stream is not None:
            key = (each.source, batch.stream)
            before = points.get(key, Checkpoint(checked_until=None, cursor=None))
            if batch.window is not None:
                after = advance_checkpoint(before, batch.window, batch.window.cursor)
                before = before if after is None else after
            points[key] = before
        if each.name == until:
            break
    return {key: (point.checked_from, point.checked_until) for key, point in points.items()}


def second_sync() -> tuple[tuple[str, Batch], ...]:
    """The sources as they stand after the timeline, as a second sync sends them: per source
    and stream, one batch with the last version of every live record, claiming the stream's
    whole checked range as a complete snapshot; for the documents, which have no stream, one
    batch of them all. Sent after the timeline, it changes nothing."""
    final = final_records()
    ranges = checked_ranges()
    batches: list[tuple[str, Batch]] = []
    for source in SOURCES:
        streams = sorted(
            {record.stream for record in final.values() if record.ref.source == source},
            key=lambda stream: stream or "",
        )
        for stream in streams:
            live = sorted(
                (
                    record
                    for record in final.values()
                    if record.ref.source == source
                    and record.stream == stream
                    and not record.deleted
                ),
                key=lambda record: (record.occurred_at, record.ref),
            )
            records = [RecordBody.model_validate(record.item).to_record() for record in live]
            window = None
            if stream is not None:
                start, end = ranges[(source, stream)]
                assert start is not None
                assert end is not None
                window = WindowClaim(start=start, end=end, complete=True, snapshot=True)
            batches.append((source, Batch(records=records, stream=stream, window=window)))
    return tuple(batches)


# --- The cast and the labels ---------------------------------------------------------------------


@cache
def people() -> Json:
    return _json(COMMS / "people.json")


@cache
def decision_labels() -> Json:
    return _json(COMMS / "labels" / "decisions.json")


@cache
def query_labels() -> Json:
    return _json(COMMS / "labels" / "queries.json")


@cache
def timeline_labels() -> Json:
    return _json(COMMS / "labels" / "timeline.json")


def me() -> str:
    """The key of the person marked as me."""
    return str(people()["me"])


@cache
def _aliases() -> dict[str, str]:
    """Every alias of the cast, and every list's address, to its key."""
    found: dict[str, str] = {}
    for person in cast("list[Json]", people()["people"]):
        for alias in cast("list[Json]", person["aliases"]):
            value = str(alias["value"])
            if value in found:
                raise ValueError(f"the alias {value!r} names two people")
            found[value] = str(person["key"])
    for group in cast("list[Json]", people()["lists"]):
        found[str(group["address"])] = f"list:{group['key']}"
    return found


def person_of(name: str) -> str | None:
    """The key of the person (or ``list:<key>`` of the mailing list) a name of the batches
    stands for: the address of ``Name <address>``, a chat user id or a display name."""
    if name.endswith(">") and "<" in name:
        name = name[name.rindex("<") + 1 : -1]
    return _aliases().get(name)


def names_of(kind: str, payload: Mapping[str, Any]) -> list[str]:
    """Every name a payload holds: authors, recipients, cc and mentions of a thread's messages;
    participants, speakers and owners of a meeting."""
    names: list[str] = []
    if kind == "thread":
        for message in cast("list[Json]", payload["messages"]):
            names.append(str(message["author"]))
            for key in ("recipients", "cc", "mentions"):
                names += [str(name) for name in message.get(key, [])]
    elif kind == "meeting":
        names += [str(name) for name in payload.get("participants", [])]
        for segment in cast("list[Json]", payload.get("transcript", [])):
            if "speaker" in segment:
                names.append(str(segment["speaker"]))
        for action in payload.get("action_items", []):
            if isinstance(action, dict) and "owner" in action:
                names.append(str(cast("Json", action)["owner"]))
    return names


def record_id_of(ref: RecordRef, sources: Mapping[str, UUID]) -> str:
    """The id the store gives ``ref``, with ``sources`` the ids of the push sources by name."""
    return record_id(sources[ref.source], cast("RecordKind", ref.kind), ref.external_id)


# --- Loading -------------------------------------------------------------------------------------


async def create_sources(
    conn: Conn, access: WriteAccess, *, workspace: str = WORKSPACE
) -> dict[str, UUID]:
    """Create the workspace and its push sources; their ids by name."""
    await create_workspace(conn, owner_id=access.owner_id, name=workspace)
    ids: dict[str, UUID] = {}
    for source in SOURCES:
        created = await create_push_source(conn, access, workspace, NewPushSource(name=source))
        ids[source] = created.id
    return ids


async def send(
    conn: Conn,
    access: WriteAccess,
    each: Delivery,
    *,
    workspace: str = WORKSPACE,
    indexer: RecordIndexer | None = None,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
) -> IngestResult:
    """Ingest one batch file into its source."""
    return await ingest(
        conn, access, workspace, each.source, each.batch, indexer=indexer, runs=runs
    )


async def load(
    conn: Conn,
    access: WriteAccess,
    *,
    workspace: str = WORKSPACE,
    indexer: RecordIndexer | None = None,
    runs: AsyncConnectionPool[Any] | AsyncConnection[Any] | None = None,
) -> dict[str, IngestResult]:
    """Ingest every batch, one after another (sources made by :func:`create_sources`); the
    results by file name."""
    results: dict[str, IngestResult] = {}
    for each in deliveries():
        results[each.name] = await send(
            conn, access, each, workspace=workspace, indexer=indexer, runs=runs
        )
    return results


def anchor_time(record: FinalRecord, anchor: str) -> datetime | None:
    """When the message or segment ``anchor`` of ``record`` was said; ``None`` for a part of a
    meeting's notes (``summary``, ``action-items``) and an anchor the record does not hold."""
    payload = record.payload
    if record.ref.kind == "thread":
        for message in cast("list[Json]", payload["messages"]):
            if message.get("id") == anchor:
                return parse_time(message["sent_at"])
    elif record.ref.kind == "meeting":
        for segment in cast("list[Json]", payload.get("transcript", [])):
            if segment.get("id") == anchor:
                start = parse_time(payload["started_at"])
                return start + timedelta(seconds=float(segment.get("start", 0)))
    return None


def anchors_of(record: FinalRecord) -> set[str]:
    """The anchors evidence may name in ``record``: its message ids, or its segment ids and the
    parts of its notes."""
    payload = record.payload
    if record.ref.kind == "thread":
        return {str(m["id"]) for m in cast("list[Json]", payload["messages"])}
    if record.ref.kind == "meeting":
        found = {str(s["id"]) for s in cast("list[Json]", payload.get("transcript", []))}
        if payload.get("summary"):
            found.add("summary")
        if payload.get("action_items"):
            found.add("action-items")
        return found
    return set()


def messages_to(record: FinalRecord, person: str) -> list[str]:
    """The ids of the messages of a thread whose recipients or mentions name ``person``."""
    found: list[str] = []
    for message in cast("list[Json]", record.payload["messages"]):
        named = [*message.get("recipients", []), *message.get("mentions", [])]
        if any(person_of(str(name)) == person for name in named):
            found.append(str(message["id"]))
    return found


def attended(record: FinalRecord, person: str) -> bool:
    """Whether ``person`` is a participant of a meeting."""
    names = cast("Sequence[str]", record.payload.get("participants", []))
    return any(person_of(name) == person for name in names)
