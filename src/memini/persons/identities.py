"""Who appears in a record's payload, and the keys a person is looked up by. No database.

**Normalization** (:func:`normalize_alias`), the lookup key of each kind of alias:

- ``email``: trimmed, a leading ``mailto:`` dropped, lowercased. One ``@`` with text on both
  sides, and no spaces, quotes, brackets, commas or semicolons; anything else is not an address.
- ``chat_user_id``: trimmed, otherwise as given: chat systems compare ids exactly. A client that
  sends the ids of more than one chat system into one workspace qualifies them
  (``teamchat:U012AB``), so that two systems cannot give two people one id.
- ``display_name``, ``git_author``: Unicode NFKC, case-folded, runs of white space made one space,
  trimmed. "Ann  Example" and "ann example" are one key; "Ann Example" and "A. Example" are two.

A value with a control character, or longer than :data:`~memini.persons.MAX_VALUE_LENGTH`, is
not a key.

**Identities in a payload** (:func:`extract_occurrences`) are read from the payload as
:mod:`memini.payloads` parses it, whose names are strings as the source shows them. A name equal
to the ``name`` of an entry of the payload's ``people`` (both trimmed) takes that entry's ``email``
and ``chat_user_id``. Otherwise a name is a mail address (``ann@example.test``), a name with an
address (``Ann Example <ann@example.test>``), or else a display name. By kind of record:

- ``thread``: each message's ``author`` (role ``author``), ``recipients`` and ``cc``
  (``recipient``) and ``mentions`` (``mention``), anchored at the message's id (the source's, or
  the one the parser derives) and timed at its ``sent_at``.
- ``meeting``: its ``participants`` and the speakers of its transcript (``participant``) and the
  owners of its action items (``mention``), for the meeting as a whole, at ``started_at``.
- ``document``: nobody.

A name that holds no usable key or name (too long, control characters) is skipped; an address or
user id in ``people`` that is not one is left out of the identity.
"""

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from memini.payloads import Contact, Document, Meeting, Parsed, Thread
from memini.persons.model import (
    DETERMINISTIC_KINDS,
    MAX_NAME_LENGTH,
    MAX_VALUE_LENGTH,
    AliasKind,
    InvalidAliasError,
    Role,
)

__all__ = [
    "Identity",
    "Occurrence",
    "check_display_name",
    "clean_display_name",
    "extract_occurrences",
    "identity_of",
    "normalize_alias",
    "normalize_chat_user_id",
    "normalize_email",
    "normalize_name",
]

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ADDRESS: Final = re.compile(r"[^@\s<>()\[\],;:\"\\]+@[^@\s<>()\[\],;:\"\\]+")
_NAMED_ADDRESS: Final = re.compile(r"(?P<name>[^<>]*?)\s*<(?P<address>[^<>]*)>")
_MAILTO: Final = "mailto:"


def _usable(value: object) -> str | None:
    """``value`` trimmed, if it is a string that can be a key: not empty, no control character,
    not too long."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_VALUE_LENGTH or _CONTROL.search(text):
        return None
    return text


def normalize_email(value: object) -> str | None:
    """The lookup key of a mail address, or ``None`` if ``value`` is not one."""
    text = _usable(value)
    if text is None:
        return None
    if text[: len(_MAILTO)].lower() == _MAILTO:
        text = text[len(_MAILTO) :]
    if _ADDRESS.fullmatch(text) is None:
        return None
    return text.lower()


def normalize_chat_user_id(value: object) -> str | None:
    """The lookup key of a chat user id, or ``None`` if ``value`` is not one. An integer is taken
    as its decimal digits."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _usable(value)


def normalize_name(value: object) -> str | None:
    """The lookup key of a display name or a git author name, or ``None`` if ``value`` is not
    one."""
    text = _usable(value)
    if text is None:
        return None
    key = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return key or None


def clean_display_name(value: object) -> str | None:
    """A name to show: NFC, runs of white space made one space, trimmed; ``None`` if ``value`` is
    not a name (see :func:`normalize_name`)."""
    text = _usable(value)
    if text is None or normalize_name(text) is None:
        return None
    cleaned = " ".join(unicodedata.normalize("NFC", text).split())
    return cleaned if len(cleaned) <= MAX_NAME_LENGTH else None


_NORMALIZERS: Final = {
    "email": normalize_email,
    "chat_user_id": normalize_chat_user_id,
    "display_name": normalize_name,
    "git_author": normalize_name,
}

_EXPECTED: Final[dict[AliasKind, str]] = {
    "email": "a mail address: one @ with text on both sides, no spaces",
    "chat_user_id": "a user id",
    "display_name": "a name",
    "git_author": "a name",
}


def normalize_alias(kind: AliasKind, value: str) -> tuple[str, str]:
    """The value to store and the lookup key of an alias given by a user.

    Raises:
        InvalidAliasError: ``value`` is not valid for ``kind``; the reason never repeats it.
    """
    key = _NORMALIZERS[kind](value)
    if key is None:
        raise InvalidAliasError(
            "value",
            f"{_EXPECTED[kind]} of at most {MAX_VALUE_LENGTH} characters, without control"
            " characters",
        )
    shown = value.strip()
    if kind in ("display_name", "git_author"):
        shown = clean_display_name(shown) or shown
    return shown, key


# --- Identities ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """One way a payload names a person. At least one field is set."""

    name: str | None = None
    """The name as shown (:func:`clean_display_name`)."""
    email: str | None = None
    """Normalized."""
    chat_user_id: str | None = None
    """Normalized."""

    def deterministic_keys(self) -> tuple[tuple[AliasKind, str], ...]:
        """The deterministic keys, in the order resolution tries them."""
        found: list[tuple[AliasKind, str]] = []
        for kind in DETERMINISTIC_KINDS:
            value = self.email if kind == "email" else self.chat_user_id
            if value is not None:
                found.append((kind, value))
        return tuple(found)

    def name_key(self) -> str | None:
        return None if self.name is None else normalize_name(self.name)

    @property
    def label(self) -> str:
        """The display name of a person made for this identity."""
        label = self.name or self.email or self.chat_user_id
        if label is None:  # an identity has at least one field
            raise ValueError("an empty identity")
        return label[:MAX_NAME_LENGTH]


@dataclass(frozen=True, slots=True)
class Occurrence:
    """An identity in a record, with its role, place and time."""

    identity: Identity
    role: Role
    anchor: str | None
    occurred_at: datetime


def _make(
    name: object = None, email: object = None, chat_user_id: object = None
) -> Identity | None:
    identity = Identity(
        name=clean_display_name(name),
        email=normalize_email(email),
        chat_user_id=normalize_chat_user_id(chat_user_id),
    )
    if identity.name is None and identity.email is None and identity.chat_user_id is None:
        return None
    return identity


def identity_of(value: str) -> Identity | None:
    """The identity a name names on its own, or ``None`` if it names none (see the module)."""
    text = value.strip()
    address = normalize_email(text)
    if address is not None:
        return Identity(email=address)
    named = _NAMED_ADDRESS.fullmatch(text)
    if named is not None:
        address = normalize_email(named["address"])
        if address is not None:
            return Identity(
                name=clean_display_name(named["name"].strip().strip('"')), email=address
            )
    return _make(name=text)


class _Names:
    """Names of one payload to identities, through its ``people``."""

    def __init__(self, people: Iterable[Contact]) -> None:
        self._known: dict[str, Identity | None] = {}
        for contact in people:
            identity = _make(contact.name, contact.email, contact.chat_user_id)
            if identity is not None and identity.email is None and identity.chat_user_id is None:
                identity = identity_of(contact.name)  # no usable key: the name as it stands
            self._known[contact.name.strip()] = identity

    def __call__(self, name: str) -> Identity | None:
        key = name.strip()
        if key in self._known:
            return self._known[key]
        return identity_of(name)


type _Named = tuple[tuple[Role, Iterable[str]], ...]


def _thread(thread: Thread) -> Iterator[Occurrence]:
    names = _Names(thread.people)
    for message in thread.messages:
        named: _Named = (
            ("author", (message.author,)),
            ("recipient", message.recipients),
            ("recipient", message.cc),
            ("mention", message.mentions),
        )
        for role, found in named:
            for name in found:
                identity = names(name)
                if identity is not None:
                    yield Occurrence(identity, role, message.id, message.sent_at)


def _meeting(meeting: Meeting) -> Iterator[Occurrence]:
    names = _Names(meeting.people)
    named: _Named = (
        ("participant", meeting.participants),
        ("participant", (s.speaker for s in meeting.transcript if s.speaker is not None)),
        ("mention", (item.owner for item in meeting.action_items if item.owner is not None)),
    )
    for role, found in named:
        for name in found:
            identity = names(name)
            if identity is not None:
                yield Occurrence(identity, role, None, meeting.started_at)


def extract_occurrences(parsed: Parsed) -> list[Occurrence]:
    """The identities in a parsed payload, in the order the payload has them (see the module)."""
    match parsed:
        case Thread():
            return list(_thread(parsed))
        case Meeting():
            return list(_meeting(parsed))
        case Document():
            return []


def check_display_name(value: str) -> str:
    """A display name given by a user, cleaned.

    Raises:
        InvalidAliasError: it is blank, too long or holds a control character.
    """
    cleaned = clean_display_name(value)
    if cleaned is None:
        raise InvalidAliasError(
            "display_name",
            f"a name of at most {MAX_NAME_LENGTH} characters, without control characters",
        )
    return cleaned
