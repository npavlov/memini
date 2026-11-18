"""Redacting a text or a JSON value with the detectors of :mod:`memini.redaction.detectors`."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from memini.redaction.detectors import (
    DETECTORS,
    Detector,
    RedactionKind,
    is_card_number,
    is_id_key,
    is_placeholder,
    is_secret_key,
    marker,
)

__all__ = [
    "REDACTION_VERSION",
    "Redacted",
    "RedactionCollisionError",
    "add_counts",
    "redact_json",
    "redact_text",
]

REDACTION_VERSION: Final = "1"
"""Changes whenever the detectors can redact a text differently. Chunk text is redacted when it
is written, so this is part of the index fingerprint (:func:`memini.index.current_fingerprint`):
a new version makes ``memini reindex`` redact the stored chunks again."""


@dataclass(frozen=True, slots=True)
class Redacted[T]:
    """A value with its secrets replaced, and how many of each kind were."""

    value: T
    counts: Mapping[RedactionKind, int]
    """Values replaced, by kind; only kinds with at least one. Never a value."""

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class RedactionCollisionError(ValueError):
    """Two keys of one JSON object are the same once their secrets are redacted. Keeping one of
    them would drop the other's value without a word, so the value is refused."""


def add_counts(*counts: Mapping[RedactionKind, int]) -> dict[RedactionKind, int]:
    """The sum of several :attr:`Redacted.counts`."""
    total: Counter[RedactionKind] = Counter()
    for each in counts:
        total.update(each)
    return dict(total)


_NO_KINDS: Final[frozenset[RedactionKind]] = frozenset()
_NOT_CARDS: Final[frozenset[RedactionKind]] = frozenset({"card"})


def redact_text(text: str, *, skip: frozenset[RedactionKind] = _NO_KINDS) -> Redacted[str]:
    """``text`` with every secret and card number replaced by its marker (``[REDACTED:card]``).

    A value that spans several lines (a PEM block) is replaced by its marker and the line breaks
    it held, so the text keeps its lines and line numbers. The result redacted again is the same
    text. ``skip``: kinds not to look for.
    """
    counts: Counter[RedactionKind] = Counter()
    return Redacted(_redact(text, counts, skip), dict(counts))


def _redact(text: str, counts: Counter[RedactionKind], skip: frozenset[RedactionKind]) -> str:
    if not text:
        return text
    folded: str | None = None
    current = text
    for detector in DETECTORS:
        if detector.kind in skip:
            continue
        if detector.hints:
            if detector.folded:
                folded = text.lower() if folded is None else folded
                if not any(hint in folded for hint in detector.hints):
                    continue
            elif not any(hint in text for hint in detector.hints):
                continue
        if detector.requires is not None and detector.requires.search(text) is None:
            continue
        current = detector.pattern.sub(_replacer(detector, counts), current)
    return current


def _replacer(detector: Detector, counts: Counter[RedactionKind]) -> Any:
    def replace(match: re.Match[str]) -> str:
        whole = match.group(0)
        value = match.group(detector.group)
        if value is None or (detector.check is not None and not detector.check(value)):
            return whole
        if detector.rewrite is None:
            replacement = marker(detector.kind) + "\n" * value.count("\n")
            counts[detector.kind] += 1
        else:
            replacement, found = detector.rewrite(value)
            if not found:
                return whole
            counts[detector.kind] += found
        offset = match.start(0)
        start, end = match.span(detector.group)
        return whole[: start - offset] + replacement + whole[end - offset :]

    return replace


def redact_json(value: Mapping[str, Any]) -> Redacted[dict[str, Any]]:
    """A copy of the JSON object ``value`` with its secrets and card numbers replaced.

    - Every string, keys included, goes through :func:`redact_text`.
    - A string under a key that names a secret (:func:`~memini.redaction.is_secret_key`:
      ``password``, ``client_secret``, ``apiKey``, ``access_token``, ``authorization`` …) is
      replaced whole, ``[REDACTED:secret]``, unless it is empty or a placeholder (``****``,
      ``${VAR}``): the key says what it is, whatever its shape.
    - A non-negative integer that is a card number (as :func:`~memini.redaction.is_card_number`
      has it) becomes the string ``[REDACTED:card]``.
    - A value under a key that names an id (:func:`~memini.redaction.is_id_key`: ``id``,
      ``message_id``) is not looked at as a card number: ids of digits would collide.
    - The items of a list count as under the list's key.

    Everything else is kept as it is. Values that are not JSON are not refused here; the caller
    checks the value (depth included: this function recurses) before or after.

    Raises:
        RedactionCollisionError: two keys of one object are the same once redacted.
    """
    counts: Counter[RedactionKind] = Counter()
    return Redacted(_object(value, counts), dict(counts))


def _object(value: Mapping[Any, Any], counts: Counter[RedactionKind]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        name: str = key if isinstance(key, str) else str(key)
        clean = _redact(name, counts, _NO_KINDS)
        if clean in result:
            raise RedactionCollisionError(
                "two keys of an object are the same once their secrets are redacted"
            )
        result[clean] = _value(item, counts, name)
    return result


def _value(value: object, counts: Counter[RedactionKind], key: str | None) -> object:
    """``key``: the key the value is under, also for the items of a list under it."""
    an_id = key is not None and is_id_key(key)
    if isinstance(value, str):
        if key is not None and value and not is_placeholder(value) and is_secret_key(key):
            counts["secret"] += 1
            return marker("secret")
        return _redact(value, counts, _NOT_CARDS if an_id else _NO_KINDS)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if not an_id and value >= 0 and is_card_number(str(value)):
            counts["card"] += 1
            return marker("card")
        return value
    if isinstance(value, Mapping):
        mapping: Mapping[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        return _object(mapping, counts)
    if isinstance(value, list | tuple):
        items: list[object] | tuple[object, ...] = value  # pyright: ignore[reportUnknownVariableType]
        return [_value(item, counts, key) for item in items]
    return value
