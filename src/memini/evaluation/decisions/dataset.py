"""The sets of the decision eval: labelled synthetic threads and meetings, read from files.

A set is a directory in the layout of ``eval/comms`` (its README describes it):

    batches/<source>/NN-<stream>.json   push API bodies, delivered in the order of NN per source
    labels/decisions.json               the labels: items, links, restatements, traps, no_items

The batches are a timeline (a late reply, an edit, a tombstone, a snapshot that leaves a record
out). :func:`load_set` replays it as ingest would and keeps each record as it stands after the
last batch; the live threads and meetings are what the eval extracts from. Each payload goes
through :func:`memini.ingest.prepare_batch`, so an extractor reads the redacted, normalized text
ingest stores (:func:`memini.extraction.record_input`). Documents are not extracted.

The labels, in the words of ``eval/comms/README.md``:

- ``items``: decisions and open questions, with statement, ``decided_at``, language,
  participants, evidence (a record and an anchor in it) and the status each ends with.
- ``links``: ``replaces`` and ``answers``, from the newer item to the older.
- ``restatements``: records that say an item again; the supersession check should call what is
  extracted there a duplicate.
- ``traps``: what a careless extractor finds and is wrong: a false positive when extracted.
- ``no_items``: threads and meetings with nothing to extract.

:func:`load_set` checks the labels against the records: every reference resolves to a live
thread or meeting, every anchor is a part of it, links join items of the set (``answers`` from a
decision to an open question, never to an earlier time), the statuses follow from the links,
and every live thread and meeting is labelled exactly one way: it holds evidence, a restatement
or a trap, or it is in ``no_items``.

A record's language, for the groups of the report, is Russian when its parts hold at least as
many Cyrillic letters as Latin ones, and English otherwise (:func:`text_language`); an item's is
its label's.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError

from memini.api.ingest import BatchBody
from memini.extraction import EXTRACTED_KINDS, RecordInput, RecordRef, record_input
from memini.ingest import DEFAULT_LIMITS, InvalidBatchError, PreparedRecord, prepare_batch
from memini.knowledge import ALL_STATUSES, ITEM_KINDS, RELATIONS, ItemKind, ItemStatus, Relation
from memini.payloads import PayloadError
from memini.workspaces import check_handle

__all__ = [
    "BATCHES_DIR",
    "DEFAULT_SETS",
    "LABELS_FILE",
    "DecisionSet",
    "DecisionSetError",
    "Evidence",
    "LabelItem",
    "LabelLink",
    "NoItems",
    "Restatement",
    "Trap",
    "load_set",
    "load_sets",
    "text_language",
]

DEFAULT_SETS: Final = (Path("eval/comms"), Path("eval/decisions/cases"))
"""The sets of this repository, relative to its root: the comms fixtures of M2 and the hard
cases of the decision eval."""
BATCHES_DIR: Final = "batches"
LABELS_FILE: Final = Path("labels") / "decisions.json"

_CYRILLIC: Final = re.compile(r"[Ѐ-ӿ]")
_LATIN: Final = re.compile(r"[A-Za-z]")

type Json = dict[str, Any]


class DecisionSetError(ValueError):
    """A set that cannot be read, or whose labels disagree with its records."""


@dataclass(frozen=True, slots=True)
class Evidence:
    ref: RecordRef
    anchor: str


@dataclass(frozen=True, slots=True)
class LabelItem:
    """A labelled decision or open question."""

    id: str
    kind: ItemKind
    statement: str
    decided_at: datetime
    language: str
    participants: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    status: ItemStatus

    @property
    def records(self) -> tuple[RecordRef, ...]:
        """The records its evidence is in, in the order of the evidence."""
        return tuple(dict.fromkeys(evidence.ref for evidence in self.evidence))

    def anchors_in(self, ref: RecordRef) -> frozenset[str]:
        return frozenset(e.anchor for e in self.evidence if e.ref == ref)


@dataclass(frozen=True, slots=True)
class LabelLink:
    """``new`` replaces or answers ``old``."""

    new: str
    old: str
    relation: Relation


@dataclass(frozen=True, slots=True)
class Restatement:
    """``ref`` says the item ``item`` again, at ``anchors``."""

    item: str
    ref: RecordRef
    anchors: frozenset[str]


@dataclass(frozen=True, slots=True)
class Trap:
    """What an extractor should not find in ``ref``: an item with this statement is wrong."""

    id: str
    kind: ItemKind
    statement: str
    ref: RecordRef
    anchors: frozenset[str]


@dataclass(frozen=True, slots=True)
class NoItems:
    ref: RecordRef
    why: str


@dataclass(frozen=True, slots=True)
class DecisionSet:
    """One set, loaded and checked: its live threads and meetings and their labels."""

    name: str
    """The directory's name; ``set:<name>`` in the report."""
    workspace: str
    """Supersession is decided among the items of one workspace: each set is one."""
    digest: str
    """SHA-256 of the batches and the labels file, as read."""
    records: tuple[RecordInput, ...]
    """Live threads and meetings, in the order they are extracted: by ``occurred_at``, then by
    reference."""
    items: tuple[LabelItem, ...]
    links: tuple[LabelLink, ...]
    restatements: tuple[Restatement, ...]
    traps: tuple[Trap, ...]
    no_items: tuple[NoItems, ...]
    languages: Mapping[RecordRef, str] = field(default_factory=dict[RecordRef, str])
    """The language of each record (:func:`text_language`)."""

    def item(self, item_id: str) -> LabelItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def links_from(self, item_id: str) -> tuple[LabelLink, ...]:
        return tuple(link for link in self.links if link.new == item_id)

    def record(self, ref: RecordRef) -> RecordInput:
        for record in self.records:
            if record.ref == ref:
                return record
        raise KeyError(str(ref))


def text_language(texts: Iterable[str]) -> str:
    """``ru`` when ``texts`` hold at least as many Cyrillic letters as Latin ones and some
    Cyrillic, else ``en``."""
    cyrillic = latin = 0
    for text in texts:
        cyrillic += len(_CYRILLIC.findall(text))
        latin += len(_LATIN.findall(text))
    return "ru" if cyrillic and cyrillic >= latin else "en"


# --- Reading -------------------------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    stream: str | None
    record: PreparedRecord
    deleted: bool = False


def _read_json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DecisionSetError(f"{path}: cannot read {what}: {error.strerror or error}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionSetError(f"{path}: {what} is not JSON: {error}") from None


def _batch_files(root: Path) -> list[tuple[str, Path]]:
    batches = root / BATCHES_DIR
    if not batches.is_dir():
        raise DecisionSetError(f"{root}: no {BATCHES_DIR} directory")
    files: list[tuple[str, Path]] = []
    for source in sorted(path for path in batches.iterdir() if path.is_dir()):
        files += [(source.name, path) for path in sorted(source.glob("*.json"))]
    if not files:
        raise DecisionSetError(f"{batches}: no batch files")
    return files


def _final_state(root: Path, files: Sequence[tuple[str, Path]]) -> dict[RecordRef, _State]:
    """Every record of the batches as it stands after the last one (see the module)."""
    state: dict[RecordRef, _State] = {}
    for source, path in files:
        body = _read_json(path, "a batch")
        name = path.relative_to(root)
        try:
            batch = BatchBody.model_validate(body).to_batch()
            prepared = prepare_batch(batch, DEFAULT_LIMITS)
        except ValidationError as error:
            raise DecisionSetError(f"{name}: not a push API body: {error}") from None
        except (InvalidBatchError, PayloadError) as error:
            raise DecisionSetError(f"{name}: {error}") from None
        held: set[RecordRef] = set()
        for record in prepared.records:
            ref = RecordRef(source, record.kind, record.external_id)
            held.add(ref)
            state[ref] = _State(batch.stream, record)
        for tombstone in prepared.tombstones:
            ref = RecordRef(source, tombstone.kind, tombstone.external_id)
            held.add(ref)
            if ref in state:
                state[ref].deleted = True
        window = batch.window
        if window is not None and window.snapshot:
            for ref, each in state.items():
                if (
                    ref.source == source
                    and each.stream == batch.stream
                    and not each.deleted
                    and window.start <= each.record.occurred_at < window.end
                    and ref not in held
                ):
                    each.deleted = True
    return state


def _digest(root: Path, files: Sequence[tuple[str, Path]]) -> str:
    sha = hashlib.sha256()
    for path in [*(path for _, path in files), root / LABELS_FILE]:
        sha.update(path.relative_to(root).as_posix().encode() + b"\0")
        sha.update(path.read_bytes() + b"\0")
    return sha.hexdigest()


# --- The labels ----------------------------------------------------------------------------------


class _Labels:
    """Reads the labels file, naming the place of anything wrong."""

    def __init__(self, path: Path, records: Mapping[RecordRef, RecordInput]) -> None:
        self.path = path
        self.records = records

    def fail(self, where: str, reason: str) -> DecisionSetError:
        return DecisionSetError(f"{self.path}: {where}: {reason}")

    def obj(self, value: object, where: str) -> Json:
        if not isinstance(value, dict):
            raise self.fail(where, "expected an object")
        return cast(Json, value)

    def array(self, data: Json, key: str, where: str, *, required: bool = True) -> list[object]:
        if key not in data and not required:
            return []
        value = data.get(key)
        if not isinstance(value, list):
            raise self.fail(f"{where}.{key}", "expected a list")
        return cast("list[object]", value)

    def text(self, data: Json, key: str, where: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise self.fail(f"{where}.{key}", "expected a non-empty string")
        return value

    def time(self, data: Json, key: str, where: str) -> datetime:
        try:
            moment = datetime.fromisoformat(self.text(data, key, where))
        except ValueError:
            raise self.fail(f"{where}.{key}", "expected a time with a zone") from None
        if moment.utcoffset() is None:
            raise self.fail(f"{where}.{key}", "expected a time with a zone")
        return moment

    def kind(self, data: Json, where: str) -> ItemKind:
        value = data.get("kind")
        for kind in ITEM_KINDS:
            if value == kind:
                return kind
        raise self.fail(f"{where}.kind", f"one of {', '.join(ITEM_KINDS)}")

    def ref(self, data: Json, where: str) -> RecordRef:
        ref = RecordRef(
            self.text(data, "source", where),
            self.text(data, "record_kind", where),
            self.text(data, "external_id", where),
        )
        if ref.kind not in EXTRACTED_KINDS:
            raise self.fail(where, f"names a {ref.kind}; decisions come from threads and meetings")
        if ref not in self.records:
            raise self.fail(where, f"no live record {ref}")
        return ref

    def anchor(self, ref: RecordRef, value: object, where: str) -> str:
        if not isinstance(value, str) or value not in self.records[ref].anchors:
            raise self.fail(where, f"not an anchor of {ref}")
        return value

    def anchors(self, ref: RecordRef, data: Json, where: str) -> frozenset[str]:
        values = self.array(data, "anchors", where)
        if not values:
            raise self.fail(f"{where}.anchors", "at least one")
        return frozenset(
            self.anchor(ref, value, f"{where}.anchors[{i}]") for i, value in enumerate(values)
        )

    def item(self, value: object, where: str) -> LabelItem:
        data = self.obj(value, where)
        evidence: list[Evidence] = []
        for i, raw in enumerate(self.array(data, "evidence", where)):
            place = f"{where}.evidence[{i}]"
            entry = self.obj(raw, place)
            ref = self.ref(entry, place)
            evidence.append(Evidence(ref, self.anchor(ref, entry.get("anchor"), place)))
        if not evidence:
            raise self.fail(f"{where}.evidence", "at least one (principle 2)")
        status = data.get("status")
        if status not in ALL_STATUSES:
            raise self.fail(f"{where}.status", f"one of {', '.join(ALL_STATUSES)}")
        participants = self.array(data, "participants", where, required=False)
        return LabelItem(
            id=self.text(data, "id", where),
            kind=self.kind(data, where),
            statement=self.text(data, "statement", where),
            decided_at=self.time(data, "decided_at", where),
            language=self.text(data, "language", where),
            participants=tuple(str(p) for p in participants),
            evidence=tuple(evidence),
            status=status,
        )


def _check_links(
    labels: _Labels, items: Mapping[str, LabelItem], links: Sequence[LabelLink]
) -> None:
    status: dict[str, ItemStatus] = dict.fromkeys(items, "active")
    for i, link in enumerate(links):
        where = f"links[{i}]"
        if link.new not in items or link.old not in items:
            raise labels.fail(where, "names an item the set does not have")
        new, old = items[link.new], items[link.old]
        if new.decided_at < old.decided_at:
            raise labels.fail(where, "an item supersedes only items said no later than itself")
        if link.relation == "answers" and (new.kind, old.kind) != ("decision", "open_question"):
            raise labels.fail(where, "a decision answers an open question")
        if status[old.id] != "active":
            raise labels.fail(where, f"{old.id} is superseded twice")
        status[old.id] = "superseded" if link.relation == "replaces" else "closed"
    for item in items.values():
        if item.status != status[item.id]:
            raise labels.fail(
                f"item {item.id}",
                f"its status is {item.status}; the links make it {status[item.id]}",
            )


@dataclass(frozen=True, slots=True)
class _Read:
    workspace: str
    items: tuple[LabelItem, ...]
    links: tuple[LabelLink, ...]
    restatements: tuple[Restatement, ...]
    traps: tuple[Trap, ...]
    no_items: tuple[NoItems, ...]


def _read_labels(path: Path, records: Mapping[RecordRef, RecordInput]) -> _Read:
    labels = _Labels(path, records)
    top = labels.obj(_read_json(path, "the labels"), "the file")
    if top.get("labels") is not True:
        raise labels.fail("labels", 'a labels file says "labels": true')
    try:
        workspace = check_handle("workspace", labels.text(top, "workspace", "the file"))
    except ValueError as error:
        raise labels.fail("workspace", str(error)) from None

    items = tuple(
        labels.item(raw, f"items[{i}]")
        for i, raw in enumerate(labels.array(top, "items", "the file"))
    )
    by_id = {item.id: item for item in items}
    if len(by_id) != len(items):
        raise labels.fail("items", "ids are unique")

    links: list[LabelLink] = []
    for i, raw in enumerate(labels.array(top, "links", "the file", required=False)):
        data = labels.obj(raw, f"links[{i}]")
        relation = data.get("relation")
        if relation not in RELATIONS:
            raise labels.fail(f"links[{i}].relation", f"one of {', '.join(RELATIONS)}")
        links.append(
            LabelLink(
                new=labels.text(data, "from", f"links[{i}]"),
                old=labels.text(data, "to", f"links[{i}]"),
                relation=relation,
            )
        )
    _check_links(labels, by_id, links)

    restatements: list[Restatement] = []
    for i, raw in enumerate(labels.array(top, "restatements", "the file", required=False)):
        where = f"restatements[{i}]"
        data = labels.obj(raw, where)
        item_id = labels.text(data, "item", where)
        if item_id not in by_id:
            raise labels.fail(f"{where}.item", "names an item the set does not have")
        ref = labels.ref(labels.obj(data.get("record"), f"{where}.record"), f"{where}.record")
        restatements.append(Restatement(item_id, ref, labels.anchors(ref, data, where)))

    traps: list[Trap] = []
    for i, raw in enumerate(labels.array(top, "traps", "the file", required=False)):
        where = f"traps[{i}]"
        data = labels.obj(raw, where)
        ref = labels.ref(labels.obj(data.get("record"), f"{where}.record"), f"{where}.record")
        traps.append(
            Trap(
                id=labels.text(data, "id", where),
                kind=labels.kind(data, where),
                statement=labels.text(data, "statement", where),
                ref=ref,
                anchors=labels.anchors(ref, data, where),
            )
        )

    no_items: list[NoItems] = []
    for i, raw in enumerate(labels.array(top, "no_items", "the file", required=False)):
        where = f"no_items[{i}]"
        data = labels.obj(raw, where)
        no_items.append(NoItems(labels.ref(data, where), str(data.get("why", ""))))

    labelled = {ref for item in items for ref in item.records}
    labelled |= {each.ref for each in restatements} | {trap.ref for trap in traps}
    empty = [entry.ref for entry in no_items]
    if len(set(empty)) != len(empty):
        raise labels.fail("no_items", "a record is named once")
    both = labelled & set(empty)
    if both:
        raise labels.fail("no_items", f"{min(both)} holds labels too")
    unlabelled = set(records) - labelled - set(empty)
    if unlabelled:
        raise labels.fail(
            "the file",
            f"{min(unlabelled)} is not labelled: name it in no_items if it holds nothing",
        )
    return _Read(workspace, items, tuple(links), tuple(restatements), tuple(traps), tuple(no_items))


def load_set(root: Path) -> DecisionSet:
    """The set in the directory ``root`` (see the module). Raises :class:`DecisionSetError`."""
    if not root.is_dir():
        raise DecisionSetError(f"{root}: not a directory")
    files = _batch_files(root)
    state = _final_state(root, files)
    labels_path = root / LABELS_FILE
    if not labels_path.is_file():
        raise DecisionSetError(f"{root}: no {LABELS_FILE.as_posix()}")
    records: dict[RecordRef, RecordInput] = {}
    for ref, each in state.items():
        if each.deleted or ref.kind not in EXTRACTED_KINDS:
            continue
        records[ref] = record_input(
            workspace="",  # the labels name it; set below
            ref=ref,
            occurred_at=each.record.occurred_at,
            payload=cast(Json, json.loads(each.record.payload)),
            external_url=each.record.external_url,
        )
    read = _read_labels(labels_path, records)
    ordered = tuple(
        sorted(
            (replace(record, workspace=read.workspace) for record in records.values()),
            key=lambda r: (r.occurred_at, r.ref),
        )
    )
    return DecisionSet(
        name=root.resolve().name,
        workspace=read.workspace,
        digest=_digest(root, files),
        records=ordered,
        items=read.items,
        links=read.links,
        restatements=read.restatements,
        traps=read.traps,
        no_items=read.no_items,
        languages={
            record.ref: text_language(part.text for part in record.parts) for record in ordered
        },
    )


def load_sets(roots: Sequence[Path]) -> tuple[DecisionSet, ...]:
    """Every set of ``roots``, each once; two sets may not share a name or a workspace."""
    sets = tuple(load_set(root) for root in dict.fromkeys(roots))
    for attribute in ("name", "workspace"):
        values = [getattr(each, attribute) for each in sets]
        if len(values) != len(set(values)):
            raise DecisionSetError(f"two sets have the same {attribute}")
    if not sets:
        raise DecisionSetError("no set to run")
    return sets
