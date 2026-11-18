"""The values of the persons layer: persons, aliases, links, and how a change can be refused."""

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args
from uuid import UUID

__all__ = [
    "ALIAS_KINDS",
    "DETERMINISTIC_KINDS",
    "MAX_NAME_LENGTH",
    "MAX_PERSON_PAGE",
    "MAX_VALUE_LENGTH",
    "ROLES",
    "Alias",
    "AliasAdded",
    "AliasHeldError",
    "AliasKind",
    "InvalidAliasError",
    "LinkCounts",
    "MergeError",
    "MergeRefusal",
    "Person",
    "PersonCursor",
    "PersonPage",
    "Role",
]

AliasKind = Literal["email", "chat_user_id", "display_name", "git_author"]
"""How a person appears in sources. ``email`` and ``chat_user_id`` are deterministic keys: the
same key is the same person. ``display_name`` and ``git_author`` are names: a name is matched only
as it was stored, never guessed from another."""

ALIAS_KINDS: Final[tuple[AliasKind, ...]] = get_args(AliasKind)

DETERMINISTIC_KINDS: Final[tuple[AliasKind, ...]] = ("email", "chat_user_id")
"""The keys resolution tries first, in this order."""

Role = Literal["author", "recipient", "mention", "participant"]
"""What a person is in a record: wrote a message or a document, was sent a message, is mentioned
in one, took part in a meeting."""

ROLES: Final[tuple[Role, ...]] = get_args(Role)

MAX_VALUE_LENGTH: Final = 320
"""Characters of an alias value. Longer values found in a payload are not keys; the API refuses
them. 320 is the longest mail address RFC 5321 lets through."""

MAX_NAME_LENGTH: Final = 320
"""Characters of a person's display name."""

MAX_PERSON_PAGE: Final = 500
"""Persons in one page of a listing."""


@dataclass(frozen=True, slots=True)
class Alias:
    """One way a person appears in sources."""

    id: UUID
    person_id: UUID
    """The person the alias belongs to: the one it was added to, also after a merge."""
    kind: AliasKind
    value: str
    """As seen in the source, or as given."""
    normalized_value: str
    """The lookup key (:func:`memini.persons.normalize_alias`)."""
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Person:
    """A person, with the aliases of everyone it stands for."""

    id: UUID
    workspace_id: UUID
    workspace: str
    display_name: str
    canonical_id: UUID
    """The person this one resolves to: itself unless it is merged into another."""
    merged_into: UUID | None
    """The person it was merged into, if it was: a split clears it."""
    merged_at: datetime | None
    members: tuple[UUID, ...]
    """The persons merged into this one directly, by id."""
    aliases: tuple[Alias, ...]
    """Its aliases and those of every person merged into it, directly or through others; by kind
    and normalized value."""
    is_me: bool
    """Whether the caller's "me" resolves to the same canonical person."""
    created_at: datetime

    @property
    def canonical(self) -> bool:
        return self.merged_into is None


@dataclass(frozen=True, slots=True)
class PersonCursor:
    """Where the next page of a listing starts: after this display name and id."""

    display_name: str
    id: UUID

    def encode(self) -> str:
        text = f"{self.id}|{self.display_name}"
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "PersonCursor":
        """The cursor that :meth:`encode` gave; ``ValueError`` for anything else."""
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
            ident, separator, name = raw.partition("|")
            person_id = UUID(ident)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ValueError("not a cursor of a person listing") from None
        if not separator:
            raise ValueError("not a cursor of a person listing")
        return cls(display_name=name, id=person_id)


@dataclass(frozen=True, slots=True)
class PersonPage:
    persons: tuple[Person, ...]
    next: PersonCursor | None
    """``None`` on the last page."""


@dataclass(frozen=True, slots=True)
class AliasAdded:
    """What adding an alias did."""

    person: Person
    """The person the alias was added to, as it is now."""
    alias: Alias
    created: bool
    """False: the person, or a person merged into it, held the alias already."""
    merged: UUID | None
    """The canonical person that held the alias before and is now merged into this one; a split
    of it undoes that."""


@dataclass(slots=True)
class LinkCounts:
    """What resolving the persons of a batch's records wrote."""

    persons_created: int = 0
    aliases_created: int = 0
    links_inserted: int = 0
    links_updated: int = 0
    links_deleted: int = 0


# --- Errors --------------------------------------------------------------------------------------


class InvalidAliasError(ValueError):
    """An alias value, or a display name, that is not valid for its kind. ``reason`` never repeats
    the value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


class AliasHeldError(Exception):
    """Another person holds the alias already: merge the two instead."""

    def __init__(self, person_id: UUID) -> None:
        super().__init__(f"person {person_id} holds the alias")
        self.person_id = person_id


MergeRefusal = Literal["already_merged", "same_person", "not_merged"]


class MergeError(Exception):
    """A merge or split that cannot be made.

    - ``already_merged``: the person is merged into another; split it first.
    - ``same_person``: the two are one person already.
    - ``not_merged``: a split of a person that is not merged.
    """

    _MESSAGES: Final[dict[MergeRefusal, str]] = {
        "already_merged": "the person is merged into another already; split it first",
        "same_person": "the two are one person already",
        "not_merged": "the person is not merged into another",
    }

    def __init__(self, refusal: MergeRefusal) -> None:
        super().__init__(self._MESSAGES[refusal])
        self.refusal: MergeRefusal = refusal
