"""Checks of a batch that need no database, and the stored form of a payload.

:func:`prepare_batch` checks a :class:`~memini.ingest.Batch` and returns the
:class:`~memini.ingest.PreparedBatch` that :func:`memini.ingest.ingest` stores, or raises
:class:`~memini.ingest.InvalidBatchError` naming the first problem. Nothing in an error repeats a
value of the batch: a payload is private, and an error message is logged and sent back.

**The stored payload.** A payload arrives as a JSON object and is stored as its canonical JSON
(:func:`canonical_json`): keys sorted by code point, no whitespace, strings in UTF-8 as they are
(only ``"``, ``\\`` and control characters escaped), numbers as Python writes them (an integer
exactly, any other number as the shortest text of its 64-bit float). The content hash is SHA-256
of those bytes. So the same object always gives the same bytes and the same hash, whichever client
sent it with whatever key order, spacing or escapes: re-sending a record changes nothing
(principle 6). Nothing a parser can use is dropped: every key and value is kept, a key repeated
in one object is refused rather than silently collapsed (by the transport, which parses the
JSON), and a value that is not finite JSON is refused.

**Fields by kind.** A payload is parsed by the parser of its kind (:mod:`memini.payloads`): a
thread needs ``messages``, each with ``author``, ``sent_at`` and ``text``; a meeting ``title`` and
``started_at``; a document ``title`` and ``text``. The optional fields the parsers read
(recipients, mentions, participants, summary, action items, transcript, the ids of messages and
segments) are checked when present; other fields are kept and not checked.

**Normalized text.** The parsed payload is rendered as the record's plain text
(:func:`memini.payloads.normalize`), stored as ``normalized_text`` with the payload.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urlsplit

from memini.ingest.model import (
    INGESTIBLE_KINDS,
    Batch,
    BatchTooLargeError,
    IngestKind,
    IngestLimits,
    InvalidBatchError,
    PreparedBatch,
    PreparedRecord,
    PreparedTombstone,
    WindowClaim,
)
from memini.payloads import PayloadError, parse_payload, render

__all__ = [
    "MAX_CLOCK_SKEW",
    "MAX_CURSOR_SIZE",
    "MAX_EXTERNAL_ID_BYTES",
    "MAX_EXTERNAL_URL_LENGTH",
    "MAX_PAYLOAD_DEPTH",
    "MAX_STREAM_KEY_BYTES",
    "canonical_json",
    "content_hash",
    "prepare_batch",
]

MAX_EXTERNAL_ID_BYTES: Final = 1024
"""An external id in UTF-8. It is part of a unique index, whose entries Postgres limits."""
MAX_STREAM_KEY_BYTES: Final = 1024
MAX_EXTERNAL_URL_LENGTH: Final = 2048
MAX_PAYLOAD_DEPTH: Final = 64
"""Objects and lists nested in a payload or a cursor, the outermost one included."""
MAX_CURSOR_SIZE: Final = 4096
"""Bytes of a window's cursor as canonical JSON."""
MAX_CLOCK_SKEW: Final = timedelta(minutes=5)
"""How far past the database clock a window may end: client clocks differ a little."""

_URL_SCHEMES: Final = frozenset({"http", "https"})
_SPACE_OR_CONTROL: Final = re.compile(r"[\s\x00-\x1f\x7f]")


# --- Payloads ------------------------------------------------------------------------------------


def _check_json(value: object, field: str) -> None:
    """Refuse what canonical JSON cannot hold exactly. Iterative, so depth costs no stack."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            _check_unicode(item, field)
        elif item is None or isinstance(item, bool | int):
            pass
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise InvalidBatchError(field, "holds a number that is not finite")
        elif isinstance(item, Mapping):
            if depth > MAX_PAYLOAD_DEPTH:
                raise InvalidBatchError(field, f"nests more than {MAX_PAYLOAD_DEPTH} levels deep")
            mapping: Mapping[object, object] = item  # pyright: ignore[reportUnknownVariableType]
            for key, child in mapping.items():
                if not isinstance(key, str):
                    raise InvalidBatchError(field, "holds an object key that is not a string")
                _check_unicode(key, field)
                stack.append((child, depth + 1))
        elif isinstance(item, list | tuple):
            if depth > MAX_PAYLOAD_DEPTH:
                raise InvalidBatchError(field, f"nests more than {MAX_PAYLOAD_DEPTH} levels deep")
            items: list[object] | tuple[object, ...] = item  # pyright: ignore[reportUnknownVariableType]
            stack.extend((child, depth + 1) for child in items)
        else:
            raise InvalidBatchError(field, "holds a value that is not JSON")


def _check_unicode(text: str, field: str) -> None:
    try:
        text.encode()
    except UnicodeEncodeError:
        raise InvalidBatchError(field, "holds a string that is not valid Unicode") from None


def _plain(value: object) -> object:
    """``json.dumps`` writes dicts only; a mapping of another type goes as a dict."""
    if isinstance(value, Mapping):
        mapping: Mapping[object, object] = value  # pyright: ignore[reportUnknownVariableType]
        return dict(mapping)
    raise TypeError(f"{type(value).__name__} is not JSON")


def canonical_json(value: Mapping[str, Any], *, field: str = "payload") -> bytes:
    """The stored form of a JSON object: sorted keys, no whitespace, UTF-8 (see the module).

    Raises:
        InvalidBatchError: naming ``field``, for a value canonical JSON cannot hold exactly: a key
            that is not a string, a number that is not finite, a string that is not valid
            Unicode, anything that is not JSON, or more than :data:`MAX_PAYLOAD_DEPTH` levels.
    """
    _check_json(value, field)
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_plain,
    )
    return text.encode()


def content_hash(payload: bytes) -> str:
    """The content hash of a stored payload: SHA-256, 64 lowercase hex characters."""
    return hashlib.sha256(payload).hexdigest()


# --- Fields --------------------------------------------------------------------------------------


def _aware(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.tzinfo.utcoffset(value) is None
    ):
        raise InvalidBatchError(field, "a time with a zone")
    return value


def _normalized(kind: IngestKind, payload: Mapping[str, Any], field: str) -> str:
    """The normalized text of a payload of ``kind``, checked by the parser of the kind."""
    try:
        return render(parse_payload(kind, payload, field=field)).text
    except PayloadError as error:
        raise InvalidBatchError(error.field, error.reason) from None


def _kind(value: str, field: str) -> IngestKind:
    for kind in INGESTIBLE_KINDS:
        if value == kind:
            return kind
    if value == "code_file":
        raise InvalidBatchError(
            field, "code_file records come from git sources; one of thread, meeting, document"
        )
    raise InvalidBatchError(field, "one of thread, meeting, document")


def _external_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidBatchError(field, "a non-empty string")
    _check_unicode(value, field)
    if "\0" in value or len(value.encode()) > MAX_EXTERNAL_ID_BYTES:
        raise InvalidBatchError(
            field, f"at most {MAX_EXTERNAL_ID_BYTES} bytes in UTF-8, without NUL characters"
        )
    return value


def _external_url(value: object, field: str) -> str | None:
    if value is None:
        return None
    reason = f"an http or https URL of at most {MAX_EXTERNAL_URL_LENGTH} characters, no spaces"
    if (
        not isinstance(value, str)
        or len(value) > MAX_EXTERNAL_URL_LENGTH
        or _SPACE_OR_CONTROL.search(value)
    ):
        raise InvalidBatchError(field, reason)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise InvalidBatchError(field, reason) from None
    if parts.scheme.lower() not in _URL_SCHEMES or not parts.netloc:
        raise InvalidBatchError(field, reason)
    _check_unicode(value, field)
    return value


def _stream_key(value: object) -> str:
    reason = f"a key of 1 to {MAX_STREAM_KEY_BYTES} bytes in UTF-8, without control characters"
    if not isinstance(value, str):
        raise InvalidBatchError("stream", reason)
    _check_unicode(value, "stream")
    if not value.strip() or not value.isprintable() or len(value.encode()) > MAX_STREAM_KEY_BYTES:
        raise InvalidBatchError("stream", reason)
    return value


def _window(window: WindowClaim, stream: str | None) -> dict[str, Any] | None:
    """Check the claim; return its cursor as a plain, checked JSON object."""
    if stream is None:
        raise InvalidBatchError("window", "a window is claimed on a stream: name the stream")
    start = _aware(window.start, "window.from")
    end = _aware(window.end, "window.to")
    if start >= end:
        raise InvalidBatchError("window.to", "later than window.from")
    if window.snapshot and not window.complete:
        raise InvalidBatchError("window.snapshot", "a snapshot is a complete window")
    if window.cursor is None:
        return None
    if not isinstance(window.cursor, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise InvalidBatchError("window.cursor", "a JSON object")
    data = canonical_json(window.cursor, field="window.cursor")
    if len(data) > MAX_CURSOR_SIZE:
        raise InvalidBatchError("window.cursor", f"at most {MAX_CURSOR_SIZE} bytes as JSON")
    cursor: dict[str, Any] = json.loads(data)
    return cursor


# --- The batch -----------------------------------------------------------------------------------


def prepare_batch(batch: Batch, limits: IngestLimits) -> PreparedBatch:
    """Check ``batch`` and put each payload in its stored form (see the module).

    Raises:
        BatchTooLargeError: more records than ``limits.max_records``, or a payload larger than
            ``limits.max_payload_size`` in its stored form.
        InvalidBatchError: anything else that is not valid; ``field`` says where.
    """
    if len(batch.records) > limits.max_records:
        raise BatchTooLargeError(
            "records",
            f"at most {limits.max_records} records in one batch; send the rest in another",
            code="too_many_records",
        )
    stream = None if batch.stream is None else _stream_key(batch.stream)
    cursor = None if batch.window is None else _window(batch.window, stream)

    records: list[PreparedRecord] = []
    tombstones: list[PreparedTombstone] = []
    seen: dict[tuple[str, str], int] = {}
    for index, item in enumerate(batch.records):
        field = f"records[{index}]"
        kind = _kind(item.kind, f"{field}.kind")
        external_id = _external_id(item.external_id, f"{field}.external_id")
        first = seen.setdefault((kind, external_id), index)
        if first != index:
            raise InvalidBatchError(
                field, f"the kind and external id of records[{first}] again; send each once"
            )
        if item.deleted:
            if item.payload is not None or item.occurred_at is not None:
                raise InvalidBatchError(
                    field, "a tombstone has a kind and an external id, no payload or time"
                )
            tombstones.append(PreparedTombstone(index, kind, external_id))
            continue
        occurred_at = _aware(item.occurred_at, f"{field}.occurred_at")
        if not isinstance(item.payload, Mapping):
            raise InvalidBatchError(f"{field}.payload", "a JSON object")
        payload = canonical_json(item.payload, field=f"{field}.payload")
        if len(payload) > limits.max_payload_size:
            raise BatchTooLargeError(
                f"{field}.payload",
                f"at most {limits.max_payload_size} bytes as stored",
                code="payload_too_large",
            )
        normalized_text = _normalized(kind, item.payload, f"{field}.payload")
        records.append(
            PreparedRecord(
                index=index,
                kind=kind,
                external_id=external_id,
                occurred_at=occurred_at,
                external_url=_external_url(item.external_url, f"{field}.external_url"),
                payload=payload,
                content_hash=content_hash(payload),
                normalized_text=normalized_text,
            )
        )
    return PreparedBatch(
        records=tuple(records),
        tombstones=tuple(tombstones),
        stream=stream,
        window=batch.window,
        cursor=cursor,
    )
