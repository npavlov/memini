"""``memini push``: records from a file or stdin to a push source, through the push API.

Usage::

    memini push --workspace NAME --source NAME [--stream KEY] [--batch-size N] [--dry-run]
                [--json] FILE|-

A client command: it reads the input where it runs and sends it to ``POST /v1/workspaces/
{workspace}/sources/{source}/records`` with a token that has the ``ingest`` scope, so it works
against a remote service as against a local one. One command for every kind of data: chat and mail
threads, meetings and documents all have the shape of the push contract (requirements, section 4;
docs/intake.md, with an example of each).

**Input.** JSON or JSON lines in UTF-8 (a byte order mark is skipped), from FILE, or from stdin
with ``-``:

- one record, ``{"kind": "thread", "external_id": …, "occurred_at": …, "payload": {…}}``;
- an array of records;
- a batch as the push API takes it, ``{"stream": …, "records": […]}``, without ``window``;
- JSON lines: a record on each line; blank lines are skipped.

A record has ``kind`` (``thread``, ``meeting`` or ``document``), ``external_id``, ``occurred_at``,
``payload`` and optionally ``external_url``; a tombstone, ``{"kind", "external_id", "deleted":
true}``, deletes the record of that kind and id. A record pushed again with the same kind and
external id replaces the stored one: that is how an edited message or a thread that grew is
updated, and the same record sent twice changes nothing. Input that is none of these is refused
before anything is sent, naming the line and column (exit 2); an object with a key twice is
refused, as the service refuses it.

**Checks.** Each record is checked here with the code the service checks it with
(:class:`memini.api.ingest.RecordBody`, :func:`memini.ingest.prepare_batch`): its fields, the
fields its kind needs in the payload, its times, its link. A record that fails is rejected with
its position and the reason, and not sent; the others are. The size of a payload is left to the
service, whose limit is a setting. A kind and external id that appear more than once are sent once,
as their last occurrence, as the service would keep the last one sent.

**Batches.** Records are sent in the order of the input, in batches of at most ``--batch-size``
records (:data:`DEFAULT_BATCH_SIZE`) and :data:`MAX_BATCH_BYTES` bytes of body, both under the
service's default limits (``ingest_max_records``, ``ingest_max_body_size``). A batch the service
finds too large (413 ``too_many_records`` or ``content_too_large``) is sent again in halves, and
later batches are made no larger. A batch refused for one of its records (422 ``invalid_batch`` or
a field of the body that fails validation, 413 ``payload_too_large``) is sent again without it, and
that record is rejected with the service's reason. ``--stream`` names the stream every batch goes
through (a channel, a mailbox), created on first use; a batch read from the input may name it too.

**Retries.** A batch is sent again, up to :data:`MAX_ATTEMPTS` times in all, while the service is
busy or briefly unavailable: 429 (after its ``Retry-After``), 503 except ``index_mismatch``, a 502
or 504 from a proxy, 409 ``ingest_conflict``, and when the service cannot be reached or does not
answer in time. The waits double from :data:`FIRST_WAIT` up to :data:`LONGEST_WAIT` seconds.
Sending a batch again is safe: a record the service stored already is unchanged. Any other refusal
stops the push: what was sent stays stored, and the summary says how far it got.

**Output.** A summary on stdout: what was sent, the records stored (inserted, updated, revived),
deleted and unchanged, and every record rejected, with its position (``line 7`` in JSON lines,
``record 3`` in an array or a batch, counted from 1) and why. Waits and retries go to stderr.
``--json`` prints the summary as JSON. ``--dry-run`` checks and batches the input and prints what it
would send, without a request.

A push claims no window, so it moves no stream's checkpoint: a client that syncs a stream window
by window sends batches with ``window`` to the HTTP API.

Exit codes: 0 every record was stored; 1 a record was rejected (the others were sent), or the
service refused the push; 2 the arguments or the input are not valid; 3 the service could not be
reached, also after the retries; 5 the token was refused.
"""

import argparse
import json
import re
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from memini import ingest
from memini.api.ingest import RecordBody
from memini.cli.arguments import Commands, parse_source_name, parse_workspace_name
from memini.cli.client import Api, Json, RequestFailed, segment
from memini.cli.common import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    CommandError,
    message,
    printable,
)
from memini.cli.output import print_json, when

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "FIRST_WAIT",
    "LONGEST_WAIT",
    "MAX_ATTEMPTS",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_SIZE",
    "InputError",
    "Item",
    "PushInput",
    "Pusher",
    "Record",
    "Rejected",
    "Totals",
    "add_commands",
    "batch_body",
    "check",
    "check_all",
    "plan",
    "push_command",
    "read_input",
    "retryable",
]

DEFAULT_BATCH_SIZE: Final = 100
"""Records per batch: well under ``ingest_max_records`` (1000), so that a batch of threads is
embedded within the client's time limit."""
MAX_BATCH_SIZE: Final = 10_000
MAX_BATCH_BYTES: Final = 8 * 1024**2
"""Bytes of a batch's body: half of ``ingest_max_body_size`` (16 MiB). A record larger than this
goes in a batch of its own."""
MAX_ATTEMPTS: Final = 6
"""Times a batch is sent, the first included, while the service is busy or unavailable."""
FIRST_WAIT: Final = 1.0
"""Seconds before the first retry; each later wait is twice the one before."""
LONGEST_WAIT: Final = 30.0
"""The longest wait between two attempts but one the service asks for (``Retry-After``)."""
SHOWN_RECORDS: Final = 10
"""Records a dry run lists."""
SHOWN_REJECTED: Final = 20
"""Rejected records the summary lists; ``--json`` lists them all."""
_CUT: Final = 60
"""Characters of an external id or a title shown in a line."""

sleep: Callable[[float], None] = time.sleep
"""How the command waits between attempts; tests replace it."""

_NO_PAYLOAD_LIMIT: Final = ingest.IngestLimits(max_records=1, max_payload_size=sys.maxsize)
_RECORD_FIELD: Final = re.compile(r"records\[(\d+)\](?:\.([^\s:]+))?: (.+?)\.?$")
"""``records[3].payload.messages[0].sent_at: a time with a zone…`` in the message of a refused
batch: the record, the field and the reason."""
_BATCH_KEYS: Final = frozenset({"records", "stream", "window"})
_REF_KEYS: Final = ("kind", "external_id", "id")
_COUNTS: Final = ("inserted", "updated", "revived", "deleted", "unchanged", "forgotten")
"""The counts of the push API's answer that the summary adds up."""

type By = Literal["check", "service"]


# --- Reading the input ---------------------------------------------------------------------------


class InputError(ValueError):
    """Input that is not a record, an array of records, a batch or JSON lines of records. The
    message says where."""


@dataclass(frozen=True, slots=True)
class Item:
    """One value of the input that should be a record."""

    position: str
    """``line 7`` in JSON lines, ``record 3`` in an array or a batch, counted from 1."""
    value: object


@dataclass(frozen=True, slots=True)
class PushInput:
    items: tuple[Item, ...]
    stream: str | None = None
    """The stream a batch read from the input names."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"an object has the key {printable(key)[:_CUT]!r} twice")
        result[key] = value
    return result


def _constant(name: str) -> object:
    raise InputError(f"{name} is not a JSON number")


def _loads(text: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_constant)
    except RecursionError:
        raise InputError("the JSON is nested too deeply") from None
    except json.JSONDecodeError:
        raise
    except ValueError as error:  # an integer with too many digits
        if isinstance(error, InputError):
            raise
        raise InputError("a number has too many digits") from None


def read_input(data: bytes) -> PushInput:
    """The items of the input: a record, an array of records, a batch or JSON lines.

    Raises:
        InputError: not UTF-8, not JSON, an object with a key twice, a batch with a window or a
            field a batch does not have, or a JSON value that holds no records.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError(f"not UTF-8 text (at byte {error.start + 1})") from None
    text = text.removeprefix("﻿")
    if not text.strip():
        return PushInput(())
    try:
        value = _loads(text)
    except json.JSONDecodeError as error:
        if error.msg != "Extra data":
            raise InputError(
                f"not valid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
            ) from None
        return PushInput(_lines(text))
    if isinstance(value, list):
        items = cast(list[object], value)
        return PushInput(tuple(Item(f"record {n}", v) for n, v in enumerate(items, start=1)))
    if isinstance(value, dict):
        document = cast(dict[str, object], value)
        if "records" in document and "kind" not in document:
            return _batch(document)
        return PushInput((Item("record 1", document),))
    raise InputError(
        "expected a record, an array of records, a batch or JSON lines; found a JSON"
        f" {type(value).__name__.replace('str', 'string').replace('NoneType', 'null')}"
    )


def _lines(text: str) -> tuple[Item, ...]:
    items: list[Item] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            value = _loads(line)
        except json.JSONDecodeError as error:
            raise InputError(
                f"line {number}: not valid JSON at column {error.colno}: {error.msg}"
            ) from None
        except InputError as error:
            raise InputError(f"line {number}: {error}") from None
        items.append(Item(f"line {number}", value))
    return tuple(items)


def _batch(body: Mapping[str, object]) -> PushInput:
    unknown = sorted(set(body) - _BATCH_KEYS)
    if unknown:
        raise InputError(
            f"a batch has no field {printable(unknown[0])[:_CUT]!r}: it holds records and"
            " optionally stream"
        )
    if body.get("window") is not None:
        raise InputError(
            "memini push claims no window, so it moves no stream's checkpoint: leave window out,"
            " or send the batch to the HTTP API"
        )
    records = body["records"]
    if not isinstance(records, list):
        raise InputError("records is not an array")
    stream = body.get("stream")
    if stream is not None and not isinstance(stream, str):
        raise InputError("stream is not a string")
    items = cast(list[object], records)
    return PushInput(
        tuple(Item(f"record {n}", v) for n, v in enumerate(items, start=1)), stream=stream
    )


# --- Checking the records ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Record:
    """A record or a tombstone of the input that passed the checks."""

    position: str
    kind: str
    external_id: str
    deleted: bool
    occurred_at: str | None
    payload: Mapping[str, Any] | None
    body: bytes
    """The record as it was read, as compact JSON in UTF-8: what is sent."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """A record that is not sent, or that the service refused."""

    position: str
    kind: str | None
    external_id: str | None
    field: str | None
    """Where in the record, e.g. ``payload.messages[0].sent_at``; ``None`` for the whole."""
    reason: str
    by: By
    """``check``: this command found it; ``service``: the service refused it."""

    def as_json(self) -> Json:
        return {
            "position": self.position,
            "kind": self.kind,
            "external_id": self.external_id,
            "field": self.field,
            "reason": self.reason,
            "by": self.by,
        }


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def check(item: Item) -> Record | Rejected:
    """``item`` as a :class:`Record` if the service would take it, else why not."""
    value: object = item.value
    if not isinstance(value, dict):
        return Rejected(item.position, None, None, None, "not a JSON object", "check")
    raw = cast(dict[str, Any], value)
    kind, external_id = _string(raw.get("kind")), _string(raw.get("external_id"))

    def rejected(where: str | None, reason: str) -> Rejected:
        return Rejected(item.position, kind, external_id, where or None, reason, "check")

    try:
        record = RecordBody.model_validate(raw)
    except ValidationError as error:
        first = error.errors(include_url=False, include_input=False)[0]
        return rejected(".".join(str(part) for part in first["loc"]), first["msg"])
    try:
        ingest.prepare_batch(ingest.Batch(records=[record.to_record()]), _NO_PAYLOAD_LIMIT)
    except ingest.InvalidBatchError as error:
        return rejected(error.field.removeprefix("records[0]").removeprefix("."), error.reason)
    try:
        body = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        data = body.encode()
    except UnicodeEncodeError:
        return rejected(None, "a string holds a lone surrogate (\\ud800 to \\udfff), not text")
    return Record(
        position=item.position,
        kind=record.kind,
        external_id=record.external_id,
        deleted=record.deleted,
        occurred_at=_string(raw.get("occurred_at")),
        payload=record.payload,
        body=data,
    )


def check_all(items: Iterable[Item]) -> tuple[list[Record], list[Rejected], list[Record]]:
    """The records to send, those rejected, and those left out because the same kind and
    external id come again later in the input (the last one is sent)."""
    records: list[Record] = []
    rejected: list[Rejected] = []
    for item in items:
        result = check(item)
        if isinstance(result, Record):
            records.append(result)
        else:
            rejected.append(result)
    last = {(record.kind, record.external_id): index for index, record in enumerate(records)}
    kept = [r for i, r in enumerate(records) if last[(r.kind, r.external_id)] == i]
    repeated = [r for i, r in enumerate(records) if last[(r.kind, r.external_id)] != i]
    return kept, rejected, repeated


# --- Batches -------------------------------------------------------------------------------------


def batch_body(stream: str | None, records: Sequence[Record]) -> bytes:
    """The body of a batch of ``records``, through ``stream`` if one is given."""
    head = b'{"records":['
    if stream is not None:
        head = b'{"stream":' + json.dumps(stream, ensure_ascii=False).encode() + b',"records":['
    return head + b",".join(record.body for record in records) + b"]}"


def _take(pending: deque[Record], max_records: int, max_bytes: int, overhead: int) -> list[Record]:
    """The next batch from the front of ``pending``: up to ``max_records`` records and
    ``max_bytes`` bytes of body, and always at least one record."""
    batch: list[Record] = []
    size = overhead
    while pending and len(batch) < max_records:
        cost = len(pending[0].body) + (1 if batch else 0)
        if batch and size + cost > max_bytes:
            break
        batch.append(pending.popleft())
        size += cost
    return batch


def plan(
    records: Sequence[Record],
    stream: str | None,
    *,
    max_records: int = DEFAULT_BATCH_SIZE,
    max_bytes: int = MAX_BATCH_BYTES,
) -> list[list[Record]]:
    """The batches ``records`` are sent in, in order, under the limits."""
    pending = deque(records)
    overhead = len(batch_body(stream, ()))
    batches: list[list[Record]] = []
    while pending:
        batches.append(_take(pending, max_records, max_bytes, overhead))
    return batches


# --- Sending -------------------------------------------------------------------------------------


def retryable(failure: RequestFailed) -> bool:
    """Whether a batch refused with ``failure`` may succeed when sent again unchanged."""
    match failure.status:
        case None | 429 | 502 | 504:
            return True
        case 503:
            return failure.code != "index_mismatch"
        case 409:
            return failure.code == "ingest_conflict"
        case _:
            return False


def _wait(failure: RequestFailed, attempt: int) -> float:
    if failure.retry_after is not None:
        return failure.retry_after
    return min(LONGEST_WAIT, FIRST_WAIT * 2 ** (attempt - 1))


def _record_failure(failure: RequestFailed) -> tuple[int, str | None, str] | None:
    """The record a refused batch was refused for, by its position in the batch, with the field
    and the reason; ``None`` when the refusal is not about one record."""
    if failure.status not in (413, 422):
        return None
    if failure.code in ("invalid_batch", "payload_too_large") and failure.message:
        found = _RECORD_FIELD.search(failure.message)
        if found is not None:
            return int(found[1]), found[2], found[3]
    if failure.status == 422:
        for location, kind in failure.fields:
            if len(location) >= 3 and location[:2] == ("body", "records"):
                index = location[2]
                if isinstance(index, int):
                    where = ".".join(str(part) for part in location[3:]) or None
                    return index, where, kind
    return None


@dataclass
class Totals:
    """What the batches did, added up from the service's answers."""

    records: int = 0
    tombstones: int = 0
    batches: int = 0
    inserted: int = 0
    updated: int = 0
    revived: int = 0
    deleted: int = 0
    unchanged: int = 0
    forgotten: int = 0
    not_indexed: int = 0
    index_note: str | None = None
    runs: list[str] = field(default_factory=list[str])
    ids: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    """Every record sent, with the id the service gave it: ``kind``, ``external_id``, ``id``."""

    def add(self, batch: Sequence[Record], answer: Json) -> None:
        self.batches += 1
        for each in answer.get("records") or ():
            if isinstance(each, dict):
                ref = cast(dict[str, object], each)
                self.ids.append({key: printable(str(ref.get(key))) for key in _REF_KEYS})
        self.tombstones += sum(1 for record in batch if record.deleted)
        self.records += sum(1 for record in batch if not record.deleted)
        counts = answer.get("counts")
        if isinstance(counts, dict):
            values = cast(dict[str, object], counts)
            for name in _COUNTS:
                value = values.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    setattr(self, name, getattr(self, name) + value)
        index = answer.get("index")
        if isinstance(index, dict):
            info = cast(dict[str, object], index)
            missing = info.get("records_not_indexed")
            if isinstance(missing, int) and not isinstance(missing, bool):
                self.not_indexed += missing
            note = info.get("note")
            if isinstance(note, str) and self.index_note is None:
                self.index_note = printable(note)
        run = answer.get("run_id")
        if isinstance(run, str):
            self.runs.append(printable(run))

    @property
    def stored(self) -> int:
        return self.inserted + self.updated + self.revived

    def counts(self) -> Json:
        return {name: getattr(self, name) for name in _COUNTS}


class Pusher:
    """Sends checked records to a push source in batches; see the module.

    After :meth:`run`, or when it raised, :attr:`totals` and :attr:`rejected` say what was done.
    """

    def __init__(
        self,
        api: Api,
        workspace: str,
        source: str,
        stream: str | None,
        *,
        max_records: int = DEFAULT_BATCH_SIZE,
        max_bytes: int = MAX_BATCH_BYTES,
    ) -> None:
        self._api = api
        self._path = f"v1/workspaces/{segment(workspace)}/sources/{segment(source)}/records"
        self._stream = stream
        self._overhead = len(batch_body(stream, ()))
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.totals = Totals()
        self.rejected: list[Rejected] = []

    def run(self, records: Sequence[Record]) -> None:
        """Send ``records`` in order.

        Raises:
            RequestFailed: the service refused a batch for a reason no retry and no smaller batch
                fixes, or did not answer after :data:`MAX_ATTEMPTS` attempts.
        """
        pending = deque(records)
        while pending:
            batch = _take(pending, self.max_records, self.max_bytes, self._overhead)
            body = batch_body(self._stream, batch)
            try:
                answer = self._send(body)
            except RequestFailed as failure:
                again = self._refused(batch, body, failure)
                pending.extendleft(reversed(again))
                continue
            self.totals.add(batch, answer)

    def _send(self, body: bytes) -> Json:
        attempt = 1
        while True:
            try:
                answer = self._api.request("POST", self._path, content=body)
            except RequestFailed as failure:
                if attempt >= MAX_ATTEMPTS or not retryable(failure):
                    raise
                wait = _wait(failure, attempt)
                attempt += 1
                message(
                    f"{failure.text}; sending the batch again in {wait:g} s (attempt {attempt}"
                    f" of {MAX_ATTEMPTS})"
                )
                sleep(wait)
                continue
            if answer is None:
                raise CommandError(
                    "the service answered a batch without a body; is MEMINI_URL the Memini service?"
                )
            return answer

    def _refused(self, batch: list[Record], body: bytes, failure: RequestFailed) -> list[Record]:
        """What to send again after ``batch`` was refused with ``failure``, or raise it."""
        if failure.status == 413 and failure.code in ("too_many_records", "content_too_large"):
            if len(batch) > 1:
                if failure.code == "too_many_records":
                    self.max_records = len(batch) // 2
                else:
                    self.max_bytes = max(self._overhead + 1, len(body) // 2)
                message(f"{failure.text}; sending it in smaller batches")
                return batch
            if failure.code == "content_too_large":
                self._reject(batch[0], None, failure.message or failure.text)
                return []
        refused = _record_failure(failure)
        if refused is not None and 0 <= refused[0] < len(batch):
            index, where, reason = refused
            self._reject(batch[index], where, reason)
            return batch[:index] + batch[index + 1 :]
        raise failure

    def _reject(self, record: Record, where: str | None, reason: str) -> None:
        reason = printable(reason)
        self.rejected.append(
            Rejected(record.position, record.kind, record.external_id, where, reason, "service")
        )


# --- The command ---------------------------------------------------------------------------------


def _read(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise CommandError(
            f"cannot read {printable(path)}: {error.strerror or type(error).__name__}", EXIT_USAGE
        ) from None


def _stream(args: argparse.Namespace, found: str | None) -> str | None:
    given: str | None = args.stream
    if given is not None and found is not None and given != found:
        raise CommandError(
            "--stream and the stream of the batch in the input differ; give one", EXIT_USAGE
        )
    stream = given if given is not None else found
    if stream is not None:
        try:
            ingest.prepare_batch(ingest.Batch(stream=stream), _NO_PAYLOAD_LIMIT)
        except ingest.InvalidBatchError as error:
            raise CommandError(f"the stream: {error.reason}", EXIT_USAGE) from None
    return stream


def _cut(value: str) -> str:
    shown = printable(value)
    return shown if len(shown) <= _CUT else shown[: _CUT - 1] + "…"


def _name(kind: str | None, external_id: str | None) -> str:
    if kind is None and external_id is None:
        return ""
    return f" ({kind or '?'} {_cut(external_id) if external_id is not None else '?'})"


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {word}es" if word.endswith("ch") else f"{count} {word}s"


def _items(records: int, tombstones: int) -> str:
    """``3 records``, ``1 tombstone``, ``3 records and 1 tombstone`` or ``nothing``."""
    parts = [
        _plural(count, word)
        for count, word in ((records, "record"), (tombstones, "tombstone"))
        if count
    ]
    return " and ".join(parts) or "nothing"


def _target(args: argparse.Namespace, stream: str | None) -> str:
    where = f"{args.workspace}/{args.source}"
    return f"{where} (stream {printable(stream)})" if stream is not None else where


def _listing(record: Record) -> str:
    if record.deleted:
        about = "deleted"
    else:
        payload = record.payload or {}
        title = payload.get("title")
        if record.kind == "thread":
            messages = payload.get("messages")
            count = len(cast(list[object], messages)) if isinstance(messages, list) else 0
            about = _plural(count, "message")
            channel = payload.get("channel")
            place = title if isinstance(title, str) else channel
            if isinstance(place, str):
                about += f", {_cut(place)}"
        else:
            about = _cut(title) if isinstance(title, str) else ""
    return f"  {record.kind:<8}  {_cut(record.external_id)}  {when(record.occurred_at)}  {about}"


def _print_rejected(rejected: Sequence[Rejected], repeated: Sequence[Record]) -> None:
    if rejected:
        print(f"rejected {len(rejected)}:")
        for each in rejected[:SHOWN_REJECTED]:
            where = f"{each.field}: " if each.field else ""
            print(f"  {each.position}{_name(each.kind, each.external_id)}: {where}{each.reason}")
        if len(rejected) > SHOWN_REJECTED:
            print(f"  … and {len(rejected) - SHOWN_REJECTED} more; --json lists them all")
    if repeated:
        positions = ", ".join(record.position for record in repeated[:SHOWN_REJECTED])
        more = "…" if len(repeated) > SHOWN_REJECTED else ""
        print(
            f"{_plural(len(repeated), 'record')} came again later in the input, and only the last"
            f" of each was sent: {positions}{more}"
        )


def _summary(
    args: argparse.Namespace,
    stream: str | None,
    totals: Totals,
    rejected: Sequence[Rejected],
    repeated: Sequence[Record],
    *,
    error: str | None = None,
) -> None:
    if args.json:
        print_json(
            {
                "workspace": args.workspace,
                "source": args.source,
                "stream": stream,
                "dry_run": False,
                "records": totals.records,
                "tombstones": totals.tombstones,
                "batches": totals.batches,
                "counts": totals.counts(),
                "not_indexed": totals.not_indexed,
                "index_note": totals.index_note,
                "runs": totals.runs,
                "ids": totals.ids,
                "rejected": [each.as_json() for each in rejected],
                "repeated": [record.position for record in repeated],
                "error": error,
            }
        )
        return
    sent = _items(totals.records, totals.tombstones)
    print(f"sent {sent} in {_plural(totals.batches, 'batch')} to {_target(args, stream)}")
    line = (
        f"stored {totals.stored} (inserted {totals.inserted}, updated {totals.updated}, revived"
        f" {totals.revived}), deleted {totals.deleted}, unchanged {totals.unchanged}"
    )
    if totals.forgotten:
        line += f", forgotten {totals.forgotten} (forgotten by hand: not stored again)"
    print(line)
    if totals.not_indexed:
        print(f"not indexed: {_plural(totals.not_indexed, 'record')}. {totals.index_note or ''}")
    _print_rejected(rejected, repeated)


def _dry_run(
    args: argparse.Namespace,
    stream: str | None,
    records: Sequence[Record],
    rejected: Sequence[Rejected],
    repeated: Sequence[Record],
) -> None:
    batches = plan(records, stream, max_records=args.batch_size)
    tombstones = sum(1 for record in records if record.deleted)
    if args.json:
        print_json(
            {
                "workspace": args.workspace,
                "source": args.source,
                "stream": stream,
                "dry_run": True,
                "records": len(records) - tombstones,
                "tombstones": tombstones,
                "batches": len(batches),
                "batch_sizes": [len(batch) for batch in batches],
                "first": [
                    {
                        "position": record.position,
                        "kind": record.kind,
                        "external_id": record.external_id,
                        "occurred_at": record.occurred_at,
                        "deleted": record.deleted,
                    }
                    for record in records[:SHOWN_RECORDS]
                ],
                "rejected": [each.as_json() for each in rejected],
                "repeated": [record.position for record in repeated],
            }
        )
        return
    what = _items(len(records) - tombstones, tombstones)
    count = _plural(len(batches), "batch")
    print(f"dry run, nothing sent: would send {what} in {count} to {_target(args, stream)}")
    for record in records[:SHOWN_RECORDS]:
        print(_listing(record))
    if len(records) > SHOWN_RECORDS:
        print(f"  … and {len(records) - SHOWN_RECORDS} more")
    _print_rejected(rejected, repeated)


def push_command(args: argparse.Namespace, api: Api) -> int:
    path: str = args.file
    try:
        found = read_input(_read(path))
    except InputError as error:
        raise CommandError(f"{printable(path)}: {error}", EXIT_USAGE) from None
    stream = _stream(args, found.stream)
    records, rejected, repeated = check_all(found.items)
    if args.dry_run:
        _dry_run(args, stream, records, rejected, repeated)
        return EXIT_FAILURE if rejected else EXIT_OK
    if not records and not rejected:
        message(f"{printable(path)} holds no records; nothing was sent")
        return EXIT_OK
    pusher = Pusher(api, args.workspace, args.source, stream, max_records=args.batch_size)
    try:
        pusher.run(records)
    except (CommandError, KeyboardInterrupt) as error:  # what was sent stays: say how far it got
        text = error.text if isinstance(error, CommandError) else "interrupted"
        _summary(args, stream, pusher.totals, [*rejected, *pusher.rejected], repeated, error=text)
        raise
    every = [*rejected, *pusher.rejected]
    _summary(args, stream, pusher.totals, every, repeated)
    return EXIT_FAILURE if every else EXIT_OK


# --- Arguments -----------------------------------------------------------------------------------


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError(f"a number from 1 to {MAX_BATCH_SIZE}")
    return size


HELP: Final = "send records from a file or stdin (JSON or JSON lines) to a push source"
DESCRIPTION: Final = (
    "Send records to a push source through the push API: chat and mail threads, meetings,"
    " documents, and tombstones that delete them, in the push contract's record shape"
    " (docs/intake.md). The input is one record, an array of records, a batch"
    ' ({"stream", "records"}) or JSON lines, from FILE or from stdin (-). Each record is checked'
    " first; a record that fails is reported and not sent, the others are. A record pushed again"
    " with the same kind and external id replaces the stored one, and is unchanged when it is the"
    " same. Batches stay under the service's limits; a busy or unavailable service (429, 503) is"
    " retried with waits. Needs the ingest scope. Exit codes: 0 everything stored, 1 a record"
    " rejected or the push refused, 2 the input or the arguments not valid."
)


def add_commands(commands: Commands) -> None:
    """Add ``push``."""
    parser = commands.add_client("push", help=HELP, description=DESCRIPTION)
    parser.add_argument(
        "--workspace",
        metavar="NAME",
        required=True,
        type=parse_workspace_name,
        help="the workspace of the source",
    )
    parser.add_argument(
        "--source",
        metavar="NAME",
        required=True,
        type=parse_source_name,
        help="a push source (memini source add WORKSPACE NAME --kind push)",
    )
    parser.add_argument(
        "--stream",
        metavar="KEY",
        help="the stream the records come through, such as a channel or a mailbox; created on"
        " first use",
    )
    parser.add_argument(
        "--batch-size",
        metavar="N",
        type=_batch_size,
        default=DEFAULT_BATCH_SIZE,
        help=f"records per batch (default {DEFAULT_BATCH_SIZE}); a batch the service finds too"
        " large is split",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check the records and print what would be sent; send nothing",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("file", metavar="FILE", help="a JSON or JSON lines file; - for stdin")
    parser.set_defaults(client=push_command)
