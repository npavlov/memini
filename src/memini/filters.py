"""Source filters: which records of a push or mcp source are stored, and which messages of a thread.

The rules of a source live in ``source.config`` under ``filters`` (:class:`SourceFilters`), set when
the source is added and changed with it (:mod:`memini.sources`)::

    {"filters": {"allow_channels": ["eng-*", "release"],
                 "allow_domains": ["example.test"],
                 "exclude_authors": ["*bot*", "Build Robot"],
                 "exclude_titles": ["Daily stand-up*"],
                 "exclude_automated": true}}

Every rule is optional; a source without any stores everything it is sent. What each one does:

``allow_channels`` (patterns)
    A thread is stored only when its ``channel`` matches one of them. A thread without a channel
    is not stored while the list is set: an allowlist admits only what it names. Meetings and
    documents have no channel and are not affected.
``allow_domains`` (domains)
    A message of a thread is kept only when an address among its author and recipients is in one
    of the domains or below one (``example.test`` admits ``eu.example.test``). A message without
    any such address, one without any address at all included, is left out. For sources whose
    names are e-mail addresses; meetings and documents are not affected.
``exclude_authors`` (patterns)
    A message whose author matches one of them is left out: the name as the source shows it
    (``Build Robot <build-robot@example.test>``), or the address in it.
``exclude_titles`` (patterns)
    A record whose title matches one of them is not stored: a meeting's title, a document's, a
    thread's (its subject).
``exclude_automated`` (true or false)
    A message of automated mail is left out: its author's address is a no-reply one (its local
    part, without ``.``, ``-``, ``_`` and a ``+tag``, holds ``noreply`` or ``donotreply``, or is
    ``mailerdaemon``), or its ``headers`` object says so: ``Auto-Submitted`` with any value but
    ``no`` (RFC 3834), or ``Precedence`` ``bulk`` or ``junk``. ``headers`` is a field of a message
    that only the filters read: header names, in any case, to a string or a list of strings.

**The unit.** A message left out is taken out of its thread's ``messages``, and the thread is
stored with the others; its other fields stay as they are. A thread none of whose messages is
kept is not stored, nor is a record a rule excludes whole (a channel, a title). :meth:`
SourceFilters.apply` decides, and returns the payload to store.

**Patterns** are globs matched against the whole value, ignoring case: ``*`` matches any run of
characters (none included), ``?`` one character, ``[abc]``, ``[a-z]`` and ``[!abc]`` one character
of a set or not of it; every other character stands for itself, and ``[*]`` for a ``*``. A pattern
is at most :data:`MAX_FILTER_PATTERN_LENGTH` characters and a list holds at most
:data:`MAX_FILTER_PATTERNS`. A glob, not a regular expression: a source's rules run on every
record it is sent, and a regular expression can backtrack without end. Each glob is compiled to a
regular expression whose every ``*`` is an atomic scan for the next fixed run of characters, never
tried again, so a match takes at most the value's length times the pattern's.

**What they read.** The payload as it is stored: redacted (:mod:`memini.redaction`) and parsed by
the parser of its kind (:mod:`memini.payloads`). Prune (:mod:`memini.prune`) reads nothing else,
so a rule decides the same at ingest and at prune; and no rule can match on a secret's value,
which the store never holds. The fields the rules read are names, addresses, channels, titles and
headers, in which redaction finds nothing in practice, so the decision is the one the payload as
sent would get.

Pure: no database, no clock; the same rules and payload always give the same verdict.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from memini.payloads import Document, Meeting, Thread, parse_payload

__all__ = [
    "MAX_FILTER_PATTERNS",
    "MAX_FILTER_PATTERN_LENGTH",
    "FilterError",
    "SourceFilters",
    "Verdict",
    "address_of",
    "check_filters",
    "glob_matches",
]

MAX_FILTER_PATTERNS: Final = 100
"""Patterns, or domains, in one list."""
MAX_FILTER_PATTERN_LENGTH: Final = 256
"""Characters of one pattern or domain."""

_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN: Final = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_MAX_DOMAIN_LENGTH: Final = 253
_ANGLE_ADDRESS: Final = re.compile(r"<\s*([^<>\s@]+@[^<>\s@]+)\s*>\s*$")
_BARE_ADDRESS: Final = re.compile(r"[^<>\s@]+@[^<>\s@]+")
_LOCAL_PUNCTUATION: Final = re.compile(r"[.\-_]")
_NO_REPLY: Final = ("noreply", "donotreply")
_DAEMONS: Final = frozenset({"mailerdaemon"})
_BULK_PRECEDENCE: Final = frozenset({"bulk", "junk"})


class FilterError(ValueError):
    """A rule was refused. ``setting`` names it (``exclude_authors``) and ``reason`` says why; the
    message never repeats the value."""

    def __init__(self, setting: str, reason: str) -> None:
        super().__init__(f"{setting}: {reason}")
        self.setting = setting
        self.reason = reason


# --- Globs ---------------------------------------------------------------------------------------


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _class_end(pattern: str, start: int) -> int | None:
    """The index of the ``]`` that closes a set opened just before ``start``; ``None`` if none."""
    index = start
    if index < len(pattern) and pattern[index] in "!^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1  # a leading ] is a member of the set
    close = pattern.find("]", index)
    return None if close < 0 else close


def _segments(pattern: str) -> list[str]:
    """The pattern as the regular expressions of its fixed runs, split where a ``*`` stands."""
    segments: list[str] = []
    current: list[str] = []
    index, end = 0, len(pattern)
    while index < end:
        char = pattern[index]
        index += 1
        if char == "*":
            while index < end and pattern[index] == "*":
                index += 1
            segments.append("".join(current))
            current = []
        elif char == "?":
            current.append(".")
        elif char == "[" and (close := _class_end(pattern, index)) is not None:
            body = pattern[index:close]
            index = close + 1
            negate = body[:1] in ("!", "^")
            if negate:
                body = body[1:]
            escaped = "".join(c if c == "-" else re.escape(c) for c in body)
            current.append(f"[^{escaped}]" if negate else f"[{escaped}]")
        else:
            current.append(re.escape(char))
    segments.append("".join(current))
    return segments


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """The regular expression of a glob. Every run between two stars is found by an atomic lazy
    scan, at its first occurrence, which is never given back; the last run is anchored at the end.
    For runs of fixed length the first occurrence leaves the most room for the rest, so this
    matches exactly when the glob does, and no scan is tried again for another occurrence of an
    earlier run: the time is at most the value's length times the pattern's.

    Raises:
        re.error: a set whose range runs backwards (``[z-a]``).
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    segments = _segments(pattern)
    if len(segments) == 1:
        regex = rf"\A{segments[0]}\Z"
    else:
        middle = "".join(f"(?>.*?{segment})" for segment in segments[1:-1])
        regex = rf"\A{segments[0]}{middle}.*{segments[-1]}\Z"
    compiled = re.compile(regex, re.IGNORECASE | re.DOTALL)
    if len(_GLOB_CACHE) < 4096:
        _GLOB_CACHE[pattern] = compiled
    return compiled


def glob_matches(pattern: str, value: str) -> bool:
    """Whether the glob ``pattern`` matches the whole of ``value``, ignoring case (see the
    module)."""
    return _glob_regex(pattern).match(value) is not None


def _any_match(patterns: Sequence[str], values: Sequence[str]) -> bool:
    return any(glob_matches(pattern, value) for pattern in patterns for value in values)


# --- Addresses -----------------------------------------------------------------------------------


def address_of(name: str) -> str | None:
    """The e-mail address in a name as a source shows it: ``ann@example.test``, or the one in
    angle brackets at its end, ``Ann Example <ann@example.test>``; ``None`` if it holds none."""
    text = name.strip()
    found = _ANGLE_ADDRESS.search(text)
    if found is not None:
        return found.group(1)
    if _BARE_ADDRESS.fullmatch(text):
        return text
    return None


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[1].lower().rstrip(".")


def _in_domains(address: str, domains: Sequence[str]) -> bool:
    domain = _domain(address)
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in domains)


def _no_reply(address: str) -> bool:
    local = address.rsplit("@", 1)[0].casefold().split("+", 1)[0]
    squashed = _LOCAL_PUNCTUATION.sub("", local)
    return squashed in _DAEMONS or any(marker in squashed for marker in _NO_REPLY)


def _header_values(message: Mapping[str, Any], name: str) -> list[str]:
    headers: object = message.get("headers")
    if not isinstance(headers, Mapping):
        return []
    fields: Mapping[object, object] = headers  # pyright: ignore[reportUnknownVariableType]
    values: list[str] = []
    for key, value in fields.items():
        if not isinstance(key, str) or key.strip().lower() != name:
            continue
        items = cast("list[object]", value) if isinstance(value, list) else [value]
        values.extend(item.strip().lower() for item in items if isinstance(item, str))
    return values


def _automated(author: str, message: Mapping[str, Any]) -> bool:
    address = address_of(author)
    if address is not None and _no_reply(address):
        return True
    for value in _header_values(message, "auto-submitted"):
        if value.split(";", 1)[0].strip() != "no":
            return True
    return any(value in _BULK_PRECEDENCE for value in _header_values(message, "precedence"))


# --- Rules ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the rules make of one record."""

    keep: bool
    """False: the record is not stored."""
    payload: dict[str, Any] | None = None
    """With ``keep``: the payload without the messages left out, or ``None`` when it stays as it
    is."""
    messages_excluded: int = 0
    """Messages of a thread left out, whether the thread is kept or not."""


_KEEP: Final = Verdict(keep=True)


@dataclass(frozen=True, slots=True)
class SourceFilters:
    """The filter rules of a source; see the module. Empty: every record is stored."""

    allow_channels: tuple[str, ...] = ()
    allow_domains: tuple[str, ...] = ()
    exclude_authors: tuple[str, ...] = ()
    exclude_titles: tuple[str, ...] = ()
    exclude_automated: bool = False

    @property
    def empty(self) -> bool:
        """Whether no rule is set."""
        return not (
            self.allow_channels
            or self.allow_domains
            or self.exclude_authors
            or self.exclude_titles
            or self.exclude_automated
        )

    def to_json(self) -> dict[str, Any]:
        """The ``filters`` object stored in ``source.config``: the rules that are set."""
        value: dict[str, Any] = {}
        for key in ("allow_channels", "allow_domains", "exclude_authors", "exclude_titles"):
            items: tuple[str, ...] = getattr(self, key)
            if items:
                value[key] = list(items)
        if self.exclude_automated:
            value["exclude_automated"] = True
        return value

    @classmethod
    def from_json(cls, value: object) -> "SourceFilters":
        """The rules as stored. Not checked again, and lenient: what is not of the shape is left
        out, so reading a source never fails on it."""
        if not isinstance(value, Mapping):
            return cls()
        stored: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return cls(
            allow_channels=_strings(stored.get("allow_channels")),
            allow_domains=_strings(stored.get("allow_domains")),
            exclude_authors=_strings(stored.get("exclude_authors")),
            exclude_titles=_strings(stored.get("exclude_titles")),
            exclude_automated=stored.get("exclude_automated") is True,
        )

    def apply(self, kind: str, payload: Mapping[str, Any]) -> Verdict:
        """What the rules make of a record of ``kind`` with ``payload``, as stored (see the
        module).

        Raises:
            PayloadError: the payload is not one of its kind (:func:`memini.payloads.
                parse_payload`); ingest never stores one.
        """
        if self.empty:
            return _KEEP
        parsed = parse_payload(kind, payload)
        match parsed:
            case Meeting() | Document():
                return _KEEP if not self._title_excluded(parsed.title) else Verdict(keep=False)
            case Thread():
                return self._thread(parsed, payload)

    def _title_excluded(self, title: str | None) -> bool:
        return title is not None and _any_match(self.exclude_titles, [title])

    def _thread(self, thread: Thread, payload: Mapping[str, Any]) -> Verdict:
        if self.allow_channels and (
            thread.channel is None or not _any_match(self.allow_channels, [thread.channel])
        ):
            return Verdict(keep=False)
        if self._title_excluded(thread.title):
            return Verdict(keep=False)
        # The parser keeps the messages in the payload's order, so the parsed message and the
        # object it came from share an index.
        entries: list[Any] = list(payload["messages"])
        kept = [
            entry
            for message, entry in zip(thread.messages, entries, strict=True)
            if self._message_kept(message.author, message.recipients, entry)
        ]
        excluded = len(entries) - len(kept)
        if not kept:
            return Verdict(keep=False, messages_excluded=excluded)
        if not excluded:
            return _KEEP
        return Verdict(keep=True, payload={**payload, "messages": kept}, messages_excluded=excluded)

    def _message_kept(
        self, author: str, recipients: Sequence[str], message: Mapping[str, Any]
    ) -> bool:
        if self.exclude_authors:
            address = address_of(author)
            names = [author] if address is None else [author, address]
            if _any_match(self.exclude_authors, names):
                return False
        if self.exclude_automated and _automated(author, message):
            return False
        if self.allow_domains:
            addresses = [found for name in (author, *recipients) if (found := address_of(name))]
            if not any(_in_domains(address, self.allow_domains) for address in addresses):
                return False
        return True


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items: list[object] = value  # pyright: ignore[reportUnknownVariableType] - JSON from the database
    return tuple(item for item in items if isinstance(item, str))


# --- Checks --------------------------------------------------------------------------------------


def _check_patterns(setting: str, patterns: Sequence[str]) -> tuple[str, ...]:
    if isinstance(patterns, str):
        raise TypeError(f"{setting} is a sequence of patterns, not one string")
    unique = tuple(dict.fromkeys(patterns))
    if len(unique) > MAX_FILTER_PATTERNS:
        raise FilterError(setting, f"at most {MAX_FILTER_PATTERNS} patterns")
    reason = (
        "a pattern is a glob of 1 to"
        f" {MAX_FILTER_PATTERN_LENGTH} characters, not blank, without control characters:"
        " * any characters, ? one, [abc] or [a-z] one of a set, [!abc] one not of it"
    )
    for pattern in unique:
        if (
            not pattern.strip()
            or len(pattern) > MAX_FILTER_PATTERN_LENGTH
            or not pattern.isprintable()
        ):
            raise FilterError(setting, reason)
        try:
            _glob_regex(pattern)
        except re.error:
            raise FilterError(
                setting, f"{reason}; a range of a set runs from low to high"
            ) from None
    return unique


def _check_domains(domains: Sequence[str]) -> tuple[str, ...]:
    setting = "allow_domains"
    if isinstance(domains, str):
        raise TypeError(f"{setting} is a sequence of domains, not one string")
    checked: list[str] = []
    for domain in domains:
        lowered = domain.strip().lower().rstrip(".")
        if not lowered or len(lowered) > _MAX_DOMAIN_LENGTH or not _DOMAIN.fullmatch(lowered):
            raise FilterError(
                setting,
                "a domain is a host name such as example.test: letters, digits and '-' in labels"
                " split by '.', at most 253 characters; its subdomains are admitted too",
            )
        checked.append(lowered)
    unique = tuple(dict.fromkeys(checked))
    if len(unique) > MAX_FILTER_PATTERNS:
        raise FilterError(setting, f"at most {MAX_FILTER_PATTERNS} domains")
    return unique


def check_filters(filters: SourceFilters) -> SourceFilters:
    """The rules as they are stored: lists without duplicates in the order given, domains in
    lower case. :class:`FilterError` naming the rule for one that is refused."""
    if not isinstance(filters.exclude_automated, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise FilterError("exclude_automated", "true or false")
    return SourceFilters(
        allow_channels=_check_patterns("allow_channels", filters.allow_channels),
        allow_domains=_check_domains(filters.allow_domains),
        exclude_authors=_check_patterns("exclude_authors", filters.exclude_authors),
        exclude_titles=_check_patterns("exclude_titles", filters.exclude_titles),
        exclude_automated=filters.exclude_automated,
    )
