"""The payloads of pushed records: the fields of each kind, parsed, and the record's plain text.

A record of kind ``thread``, ``meeting`` or ``document`` carries its content as a JSON object, its
payload (requirements, section 4). :func:`parse_payload` checks a payload and returns what the
parsers of the kinds read: a :class:`Thread`, a :class:`Meeting` or a :class:`Document`. Ingest
refuses a payload it does not take (:mod:`memini.ingest.validate`), the chunkers cut the parsed
form (:mod:`memini.chunking.payloads`), and :func:`normalize` renders it as the record's plain
text. Pure: no database, no clock; the same payload always gives the same result.

Fields
    A field the parsers do not know is kept in the payload and not read; a field set to ``null``
    is the same as one left out, and so is an optional text that is blank. Names (authors,
    recipients, participants, speakers, owners) are strings as the source shows them; a blank
    name in a list names nobody and is left out. Persons are resolved from names elsewhere.

    ``thread``: ``messages``, a list of one or more messages in the order of the thread. A
    message has ``author`` (a string, not blank), ``sent_at`` (a time with a zone) and ``text`` (a
    string, possibly empty); optionally ``id`` (its id in the source), ``recipients``, ``cc``
    and ``mentions`` (lists of names) and ``url`` (an http or https link to the message). ``cc``
    is read for persons only, not rendered. A thread
    may have a ``title`` (a subject), a ``channel`` (where it was posted) and ``people``.

    ``meeting``: ``title`` (not blank) and ``started_at``; optionally ``ended_at`` (not before
    ``started_at``), ``participants`` (a list of names), ``summary`` (Markdown), ``action_items``
    (a list; an item is a string, or an object with ``text`` and optionally ``owner``, ``due``
    and ``id``) and ``transcript`` (a list of segments in order, each an object with ``text``,
    not blank, and optionally ``speaker``, ``start`` and ``end``, in seconds from the start of
    the meeting, and ``id``), and ``people``.

    ``document``: ``title`` (not blank) and ``text`` (Markdown, possibly empty).

    ``people`` (threads and meetings): what the source knows of the names it uses, a list of
    objects with ``name`` (not blank, each name once) and optionally ``email`` and
    ``chat_user_id``: a name of the payload equal to a ``name`` here (both trimmed) is the person
    with that address or user id (:mod:`memini.persons`). A name is also a mail address on its own
    (``ann@example.test``, ``Ann Example <ann@example.test>``). Not part of the normalized text.

    A time is an ISO 8601 string with a zone offset or ``Z``: ``2026-03-01T09:30:00Z``.

Ids
    Evidence and locators point at a message or a transcript segment by its id (section 3), so
    each has one that stays when the record is sent again: the ``id`` the source gave it, unique
    in its record, or one derived from what an edit of the text does not change: a message's
    author and time, a segment's speaker and start (its position, when it has no start). A
    derived id is 16 hexadecimal digits of :func:`memini.ids.hash_fields`; when two items would
    get the same one, the later ones get ``-2``, ``-3`` … appended. An id is at most
    :data:`MAX_ITEM_ID_LENGTH` characters without control characters.

Normalized text
    :func:`normalize` renders a payload as plain text, the record's ``normalized_text``. Line
    ends become ``\\n``, a NUL character becomes U+FFFD (Postgres text cannot hold one), and blank
    lines at the ends of a text are left out; names and titles are put on one line.

    A thread: its channel and title on a line, then each message after a blank line: a line
    ``author → recipients · 2026-03-01 09:30 UTC``, then its text. A meeting: its title, its time
    (``2026-03-01 14:00 to 14:30 UTC``) and ``Participants: …``; then, each after a blank line and
    a line naming it, ``Summary`` (the summary's text), ``Action items`` (``- owner: text (due
    …)``) and ``Transcript`` (``[mm:ss] speaker: text``, one segment after another). A document:
    its title, a blank line, its text. Times are shown in UTC to the minute.

    The chunks of a pushed record are cut from this text (:mod:`memini.chunking.payloads`), and
    their lines and byte offsets count in it, so a chunk always quotes the normalized text. The
    renderers (:func:`render`) say which rows hold each message, segment or part.
"""

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, assert_never, cast
from urllib.parse import urlsplit

from memini.ids import hash_fields

__all__ = [
    "MAX_ITEM_ID_LENGTH",
    "MESSAGE_ID_DOMAIN",
    "PAYLOAD_KINDS",
    "SEGMENT_ID_DOMAIN",
    "ActionItem",
    "Contact",
    "Document",
    "Meeting",
    "Message",
    "Parsed",
    "PayloadError",
    "PayloadKind",
    "RenderedDocument",
    "RenderedMeeting",
    "RenderedThread",
    "Rows",
    "Segment",
    "Thread",
    "format_minute",
    "format_offset",
    "load_payload",
    "meeting_time",
    "normalize",
    "one_line",
    "parse_payload",
    "render",
    "render_document",
    "render_meeting",
    "render_thread",
    "thread_label",
]

PayloadKind = Literal["thread", "meeting", "document"]
"""Kinds of record that carry a payload; ``code_file`` records keep none."""

PAYLOAD_KINDS: Final[tuple[PayloadKind, ...]] = ("thread", "meeting", "document")

MAX_ITEM_ID_LENGTH: Final = 256
"""The longest id of a message, a segment or an action item, in characters."""
MESSAGE_ID_DOMAIN: Final = "memini.message.v1"
"""Domain tag of derived message ids: author and time."""
SEGMENT_ID_DOMAIN: Final = "memini.segment.v1"
"""Domain tag of derived segment ids: speaker and start, or position."""

_DERIVED_ID_DIGITS: Final = 16
_MAX_TIME_LENGTH: Final = 64
_MAX_URL_LENGTH: Final = 2048
_URL_SCHEMES: Final = frozenset({"http", "https"})
_SPACE_OR_CONTROL: Final = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
"""Control characters, and the line and paragraph separators, which Postgres counts as control
characters too."""
_TIME_HINT: Final = "a time with a zone, such as 2026-03-01T09:30:00Z"


class PayloadError(ValueError):
    """A payload without the fields of its kind. ``field`` names where (``payload.messages[0].
    sent_at``) and ``reason`` why; neither ever holds a value of the payload."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# --- The parsed forms ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One message of a thread."""

    id: str
    """Given by the source, or derived from the author and the time (see the module)."""
    author: str
    sent_at: datetime
    text: str
    recipients: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    url: str | None = None
    cc: tuple[str, ...] = ()
    """Recipients in copy; persons count them as recipients. Not part of the normalized text."""


@dataclass(frozen=True, slots=True)
class Contact:
    """What the source knows of a name it uses: the person's mail address, its chat user id. As
    the payload gives them; :mod:`memini.persons` checks and normalizes them."""

    name: str
    email: str | None = None
    chat_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class Thread:
    """Messages with their authors, times, recipients and mentions (requirements, section 3)."""

    messages: tuple[Message, ...]
    title: str | None = None
    channel: str | None = None
    people: tuple[Contact, ...] = ()

    @property
    def kind(self) -> Literal["thread"]:
        return "thread"


@dataclass(frozen=True, slots=True)
class ActionItem:
    """Something a meeting left someone to do."""

    text: str
    owner: str | None = None
    due: str | None = None
    """As the source wrote it: a date, a time, "next week"."""
    id: str | None = None


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch of a meeting's transcript."""

    id: str
    """Given by the source, or derived from the speaker and the start (see the module)."""
    text: str
    speaker: str | None = None
    start: float | None = None
    """Seconds from the start of the meeting."""
    end: float | None = None


@dataclass(frozen=True, slots=True)
class Meeting:
    """A meeting with its notes: title, times, participants, summary, action items, transcript."""

    title: str
    started_at: datetime
    ended_at: datetime | None = None
    participants: tuple[str, ...] = ()
    summary: str | None = None
    """Markdown."""
    action_items: tuple[ActionItem, ...] = ()
    transcript: tuple[Segment, ...] = ()
    people: tuple[Contact, ...] = ()

    @property
    def kind(self) -> Literal["meeting"]:
        return "meeting"


@dataclass(frozen=True, slots=True)
class Document:
    """A titled text, Markdown."""

    title: str
    text: str

    @property
    def kind(self) -> Literal["document"]:
        return "document"


type Parsed = Thread | Meeting | Document


# --- Parsing -------------------------------------------------------------------------------------


def _fields(value: object, field: str, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PayloadError(field, reason)
    fields: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
    return fields


def _list(value: object, field: str, reason: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise PayloadError(field, reason)
    return cast("Sequence[object]", value)


def _text(payload: Mapping[str, Any], key: str, field: str, *, required: bool) -> str:
    """A string at ``payload[key]``; ``required``: and not blank."""
    value = payload.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise PayloadError(
            f"{field}.{key}", "a string that is not blank" if required else "a string"
        )
    return value


def _optional_text(payload: Mapping[str, Any], key: str, field: str) -> str | None:
    """A string at ``payload[key]``; None when it is absent, null or blank."""
    if payload.get(key) is None:
        return None
    value = _text(payload, key, field, required=False)
    return value if value.strip() else None


def _time(payload: Mapping[str, Any], key: str, field: str) -> datetime:
    """The time at ``payload[key]``: an ISO 8601 string with a zone."""
    value = payload.get(key)
    where = f"{field}.{key}"
    if not isinstance(value, str) or len(value) > _MAX_TIME_LENGTH:
        raise PayloadError(where, _TIME_HINT)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PayloadError(where, _TIME_HINT)
    return parsed


def _names(payload: Mapping[str, Any], key: str, field: str) -> tuple[str, ...]:
    """A list of names at ``payload[key]``, strings; none when absent. A blank name names nobody
    and is left out."""
    value = payload.get(key)
    if value is None:
        return ()
    where = f"{field}.{key}"
    names: list[str] = []
    for index, item in enumerate(_list(value, where, "a list of names")):
        if not isinstance(item, str):
            raise PayloadError(f"{where}[{index}]", "a name is a string")
        if item.strip():
            names.append(item)
    return tuple(names)


def _seconds(payload: Mapping[str, Any], key: str, field: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise PayloadError(f"{field}.{key}", "seconds from the start: a number, at least 0")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise PayloadError(f"{field}.{key}", "seconds from the start: a number, at least 0")
    return seconds


def _url(payload: Mapping[str, Any], key: str, field: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    where = f"{field}.{key}"
    reason = f"an http or https URL of at most {_MAX_URL_LENGTH} characters, no spaces"
    if (
        not isinstance(value, str)
        or len(value) > _MAX_URL_LENGTH
        or _SPACE_OR_CONTROL.search(value)
    ):
        raise PayloadError(where, reason)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise PayloadError(where, reason) from None
    if parts.scheme.lower() not in _URL_SCHEMES or not parts.netloc:
        raise PayloadError(where, reason)
    return value


def _parse_people(payload: Mapping[str, Any], field: str) -> tuple[Contact, ...]:
    """The ``people`` of a thread or a meeting; none when absent."""
    value = payload.get("people")
    if value is None:
        return ()
    where = f"{field}.people"
    contacts: list[Contact] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(_list(value, where, "a list of people")):
        at = f"{where}[{index}]"
        person = _fields(entry, at, "a person is an object with name, email, chat_user_id")
        name = _text(person, "name", at, required=True)
        first = seen.setdefault(name.strip(), index)
        if first != index:
            raise PayloadError(f"{at}.name", f"the name of {where}[{first}] again")
        contacts.append(
            Contact(
                name=name,
                email=_optional_text(person, "email", at),
                chat_user_id=_optional_text(person, "chat_user_id", at),
            )
        )
    return tuple(contacts)


def _given_id(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get("id")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_ITEM_ID_LENGTH
        or _CONTROL.search(value)
    ):
        raise PayloadError(
            f"{field}.id",
            f"a string of 1 to {MAX_ITEM_ID_LENGTH} characters, not blank, without control"
            " characters",
        )
    return value


def _derived_id(domain: str, *parts: str) -> str:
    return hash_fields(domain, *(part.encode() for part in parts))[:_DERIVED_ID_DIGITS]


def _check_unique(given: Sequence[str | None], field: str) -> set[str]:
    """The given ids, each once; ``field`` is the list they are in."""
    seen: dict[str, int] = {}
    for index, item_id in enumerate(given):
        if item_id is None:
            continue
        first = seen.setdefault(item_id, index)
        if first != index:
            raise PayloadError(f"{field}[{index}].id", f"the id of {field}[{first}] again")
    return set(seen)


def _assign_ids(given: Sequence[str | None], derived: Sequence[str], field: str) -> list[str]:
    """The id of each item: its given id, unique in the record, else its derived one, with a
    suffix where it would repeat an id before it (see the module)."""
    taken = _check_unique(given, field)
    ids: list[str] = []
    for item_id, fallback in zip(given, derived, strict=True):
        if item_id is not None:
            ids.append(item_id)
            continue
        candidate, n = fallback, 1
        while candidate in taken:
            n += 1
            candidate = f"{fallback}-{n}"
        taken.add(candidate)
        ids.append(candidate)
    return ids


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_thread(payload: Mapping[str, Any], field: str) -> Thread:
    where = f"{field}.messages"
    entries = _list(payload.get("messages"), where, "a list of one or more messages")
    if not entries:
        raise PayloadError(where, "a list of one or more messages")
    drafts: list[tuple[str | None, str, datetime, str, Mapping[str, Any], str]] = []
    for index, entry in enumerate(entries):
        at = f"{where}[{index}]"
        message = _fields(entry, at, "a message is an object")
        author = _text(message, "author", at, required=True)
        sent_at = _time(message, "sent_at", at)
        text = _text(message, "text", at, required=False)
        drafts.append((_given_id(message, at), author, sent_at, text, message, at))
    ids = _assign_ids(
        [draft[0] for draft in drafts],
        [_derived_id(MESSAGE_ID_DOMAIN, draft[1], _utc_text(draft[2])) for draft in drafts],
        where,
    )
    messages = tuple(
        Message(
            id=message_id,
            author=author,
            sent_at=sent_at,
            text=text,
            recipients=_names(message, "recipients", at),
            mentions=_names(message, "mentions", at),
            url=_url(message, "url", at),
            cc=_names(message, "cc", at),
        )
        for message_id, (_, author, sent_at, text, message, at) in zip(ids, drafts, strict=True)
    )
    return Thread(
        messages=messages,
        title=_optional_text(payload, "title", field),
        channel=_optional_text(payload, "channel", field),
        people=_parse_people(payload, field),
    )


def _parse_action_items(payload: Mapping[str, Any], field: str) -> tuple[ActionItem, ...]:
    value = payload.get("action_items")
    if value is None:
        return ()
    where = f"{field}.action_items"
    items: list[ActionItem] = []
    for index, entry in enumerate(_list(value, where, "a list of action items")):
        at = f"{where}[{index}]"
        if isinstance(entry, str):
            if not entry.strip():
                raise PayloadError(at, "an action item is text that is not blank, or an object")
            items.append(ActionItem(text=entry))
            continue
        item = _fields(entry, at, "an action item is text that is not blank, or an object")
        items.append(
            ActionItem(
                text=_text(item, "text", at, required=True),
                owner=_optional_text(item, "owner", at),
                due=_optional_text(item, "due", at),
                id=_given_id(item, at),
            )
        )
    _check_unique([item.id for item in items], where)
    return tuple(items)


def _parse_transcript(payload: Mapping[str, Any], field: str) -> tuple[Segment, ...]:
    value = payload.get("transcript")
    if value is None:
        return ()
    where = f"{field}.transcript"
    drafts: list[tuple[str | None, str, str | None, float | None, float | None]] = []
    for index, entry in enumerate(_list(value, where, "a list of segments")):
        at = f"{where}[{index}]"
        segment = _fields(entry, at, "a segment is an object")
        start = _seconds(segment, "start", at)
        end = _seconds(segment, "end", at)
        if start is not None and end is not None and end < start:
            raise PayloadError(f"{at}.end", "not before start")
        drafts.append(
            (
                _given_id(segment, at),
                _text(segment, "text", at, required=True),
                _optional_text(segment, "speaker", at),
                start,
                end,
            )
        )
    derived = [
        _derived_id(
            SEGMENT_ID_DOMAIN,
            speaker or "",
            f"#{index}" if start is None else repr(start),
        )
        for index, (_, _, speaker, start, _) in enumerate(drafts)
    ]
    ids = _assign_ids([draft[0] for draft in drafts], derived, where)
    return tuple(
        Segment(id=segment_id, text=text, speaker=speaker, start=start, end=end)
        for segment_id, (_, text, speaker, start, end) in zip(ids, drafts, strict=True)
    )


def _parse_meeting(payload: Mapping[str, Any], field: str) -> Meeting:
    title = _text(payload, "title", field, required=True)
    started_at = _time(payload, "started_at", field)
    ended_at: datetime | None = None
    if payload.get("ended_at") is not None:
        ended_at = _time(payload, "ended_at", field)
        if ended_at < started_at:
            raise PayloadError(f"{field}.ended_at", "not before started_at")
    summary: str | None = None
    if payload.get("summary") is not None:
        summary = _text(payload, "summary", field, required=False)
    return Meeting(
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        participants=_names(payload, "participants", field),
        summary=summary,
        action_items=_parse_action_items(payload, field),
        transcript=_parse_transcript(payload, field),
        people=_parse_people(payload, field),
    )


def _parse_document(payload: Mapping[str, Any], field: str) -> Document:
    return Document(
        title=_text(payload, "title", field, required=True),
        text=_text(payload, "text", field, required=False),
    )


def parse_payload(kind: str, payload: Mapping[str, Any], *, field: str = "payload") -> Parsed:
    """The payload of a record of ``kind``, checked and parsed (see the module).

    Raises:
        PayloadError: the payload lacks a field of its kind, or a field it has is not of the
            shape its kind reads; ``field`` is the prefix of the place named.
        ValueError: ``kind`` carries no payload.
    """
    if kind == "thread":
        return _parse_thread(payload, field)
    if kind == "meeting":
        return _parse_meeting(payload, field)
    if kind == "document":
        return _parse_document(payload, field)
    raise ValueError(f"records of kind {kind!r} carry no payload")


def load_payload(kind: str, data: bytes) -> Parsed:
    """A stored payload (canonical JSON, as ingest stores it), parsed.

    Raises:
        PayloadError: as :func:`parse_payload`; also for bytes that are not a JSON object.
        ValueError: ``kind`` carries no payload.
    """
    try:
        value: object = json.loads(data)
    except (ValueError, RecursionError):
        raise PayloadError("payload", "a JSON object") from None
    return parse_payload(kind, _fields(value, "payload", "a JSON object"))


# --- Rendering -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rows:
    """Rows ``first`` to ``last`` of a normalized text, 0-based and inclusive."""

    first: int
    last: int


@dataclass(frozen=True, slots=True)
class RenderedThread:
    text: str
    messages: tuple[Rows, ...]
    """The rows of each message: its line, then its text."""


@dataclass(frozen=True, slots=True)
class RenderedMeeting:
    text: str
    overview: Rows
    """Title, time and participants."""
    summary: Rows | None
    """The summary's text, without the line that names it; None without a summary."""
    action_items: Rows | None
    """The items, without the line that names them."""
    segments: tuple[Rows, ...]
    """The rows of each transcript segment."""


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    text: str
    title: Rows
    body: Rows | None
    """The document's text; None when it has none."""


def one_line(text: str) -> str:
    """A name, title or label on one line: whitespace runs, line ends included, become one space,
    control characters a space too, NUL U+FFFD (as everywhere in the normalized text)."""
    return " ".join(_CONTROL.sub(" ", text.replace("\0", "\N{REPLACEMENT CHARACTER}")).split())


def _body(text: str) -> list[str]:
    """The lines of a text: CR LF and CR as line ends, NUL as U+FFFD, blank lines at both ends left
    out."""
    lines = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\0", "\N{REPLACEMENT CHARACTER}")
        .split("\n")
    )
    first = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    last = next((i for i in range(len(lines) - 1, first - 1, -1) if lines[i].strip()), first - 1)
    return lines[first : last + 1]


def format_minute(moment: datetime) -> str:
    """``2026-03-01 09:30 UTC``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def format_offset(seconds: float) -> str:
    """Seconds from the start of a meeting: ``12:05``, or ``1:02:05`` past an hour."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def meeting_time(meeting: Meeting) -> str:
    """``2026-03-01 14:00 to 14:30 UTC``; the end with its date when it is on another day."""
    start = meeting.started_at.astimezone(UTC)
    if meeting.ended_at is None:
        return format_minute(start)
    end = meeting.ended_at.astimezone(UTC)
    if end.date() == start.date():
        return f"{start:%Y-%m-%d %H:%M} to {end:%H:%M} UTC"
    return f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC"


def thread_label(thread: Thread) -> str:
    """The channel and the title of a thread on one line, ``·`` between; empty without either."""
    parts = [one_line(part) for part in (thread.channel, thread.title) if part is not None]
    return " · ".join(part for part in parts if part)


def _message_line(message: Message) -> str:
    to = ""
    if message.recipients:
        to = " → " + ", ".join(one_line(name) for name in message.recipients)
    return f"{one_line(message.author)}{to} · {format_minute(message.sent_at)}"


def render_thread(thread: Thread) -> RenderedThread:
    rows: list[str] = []
    label = thread_label(thread)
    if label:
        rows += [label, ""]
    spans: list[Rows] = []
    for index, message in enumerate(thread.messages):
        if index:
            rows.append("")
        first = len(rows)
        rows.append(_message_line(message))
        rows += _body(message.text)
        spans.append(Rows(first, len(rows) - 1))
    return RenderedThread("\n".join(rows), tuple(spans))


def _action_lines(item: ActionItem) -> list[str]:
    lines = _body(item.text) or [""]
    owner = f"{one_line(item.owner)}: " if item.owner else ""
    due = f" (due {one_line(item.due)})" if item.due else ""
    return [f"- {owner}{lines[0]}{due}", *(f"  {line}" for line in lines[1:])]


def _segment_lines(segment: Segment) -> list[str]:
    lines = _body(segment.text) or [""]
    prefix = "" if segment.start is None else f"[{format_offset(segment.start)}] "
    if segment.speaker:
        prefix += f"{one_line(segment.speaker)}: "
    return [f"{prefix}{lines[0]}", *lines[1:]]


def render_meeting(meeting: Meeting) -> RenderedMeeting:
    rows = [one_line(meeting.title), meeting_time(meeting)]
    if meeting.participants:
        rows.append("Participants: " + ", ".join(one_line(n) for n in meeting.participants))
    overview = Rows(0, len(rows) - 1)
    summary: Rows | None = None
    lines = _body(meeting.summary or "")
    if lines:
        rows += ["", "Summary"]
        summary = Rows(len(rows), len(rows) + len(lines) - 1)
        rows += lines
    actions: Rows | None = None
    if meeting.action_items:
        rows += ["", "Action items"]
        first = len(rows)
        for item in meeting.action_items:
            rows += _action_lines(item)
        actions = Rows(first, len(rows) - 1)
    segments: list[Rows] = []
    if meeting.transcript:
        rows += ["", "Transcript"]
        for segment in meeting.transcript:
            first = len(rows)
            rows += _segment_lines(segment)
            segments.append(Rows(first, len(rows) - 1))
    return RenderedMeeting("\n".join(rows), overview, summary, actions, tuple(segments))


def render_document(document: Document) -> RenderedDocument:
    rows = [one_line(document.title)]
    body: Rows | None = None
    lines = _body(document.text)
    if lines:
        rows.append("")
        body = Rows(len(rows), len(rows) + len(lines) - 1)
        rows += lines
    return RenderedDocument("\n".join(rows), Rows(0, 0), body)


def render(parsed: Parsed) -> RenderedThread | RenderedMeeting | RenderedDocument:
    """The normalized text of a parsed payload, with the rows of its parts."""
    match parsed:
        case Thread():
            return render_thread(parsed)
        case Meeting():
            return render_meeting(parsed)
        case Document():
            return render_document(parsed)
        case _:  # pyright: ignore[reportUnnecessaryComparison]
            assert_never(parsed)


def normalize(kind: str, payload: Mapping[str, Any]) -> str:
    """The plain text of a record of ``kind`` with ``payload``: its ``normalized_text`` (see the
    module). Raises as :func:`parse_payload`."""
    return render(parse_payload(kind, payload)).text
