"""Persons, their aliases, "me", and who appears in which record (requirements, section 3).

A **person** is a human seen in the sources of a workspace. It is found through its **aliases**:
mail addresses and chat user ids, the deterministic keys, and display names and git author names.
The tables, and the rules the database keeps, are in the persons migration.

Who calls what:

- **Ingest** calls :func:`link_records` in the transaction of each batch, with the records the
  batch wrote. It reads the identities of each payload (:func:`extract_occurrences`), resolves
  each to a person by its deterministic keys first and by its name only when it has none, makes
  persons and aliases for keys no one holds, and writes the record's links (``record_person``:
  author, recipient, mention, participant). See :mod:`memini.persons.resolve`.
- **Administration** (the HTTP API) lists and finds persons, adds and removes aliases, merges and
  splits persons and marks "me": :mod:`memini.persons.directory`.
- **Queries** that list a person's records (``list_meetings``, ``list_messages``) read
  ``record_person`` through the person's merges: the SQL functions ``person_root(id)`` (the
  canonical person) and ``person_members(id)`` (a person and everyone merged into it) give the
  persons one canonical person stands for, ``person_members(person_root(x))``.

A merge is reversible: it marks the merged person and changes no other row, so a split restores
exactly what was there. Persons are never deleted by it.
"""

from memini.persons.directory import (
    DEFAULT_PERSON_PAGE,
    add_alias,
    create_person,
    find_person,
    get_me,
    get_person,
    list_persons,
    merge_person,
    remove_alias,
    rename_person,
    set_me,
    split_person,
    unset_me,
)
from memini.persons.identities import (
    Identity,
    Occurrence,
    check_display_name,
    clean_display_name,
    extract_occurrences,
    identity_of,
    normalize_alias,
    normalize_chat_user_id,
    normalize_email,
    normalize_name,
)
from memini.persons.model import (
    ALIAS_KINDS,
    DETERMINISTIC_KINDS,
    MAX_NAME_LENGTH,
    MAX_PERSON_PAGE,
    MAX_VALUE_LENGTH,
    ROLES,
    Alias,
    AliasAdded,
    AliasHeldError,
    AliasKind,
    InvalidAliasError,
    LinkCounts,
    MergeError,
    MergeRefusal,
    Person,
    PersonCursor,
    PersonPage,
    Role,
)
from memini.persons.resolve import (
    PERSON_LOCK_NAMESPACE,
    LinkableRecord,
    link_records,
    lock_persons,
    person_lock_key,
)
from memini.workspaces import NotFoundError

__all__ = [
    "ALIAS_KINDS",
    "DEFAULT_PERSON_PAGE",
    "DETERMINISTIC_KINDS",
    "MAX_NAME_LENGTH",
    "MAX_PERSON_PAGE",
    "MAX_VALUE_LENGTH",
    "PERSON_LOCK_NAMESPACE",
    "ROLES",
    "Alias",
    "AliasAdded",
    "AliasHeldError",
    "AliasKind",
    "Identity",
    "InvalidAliasError",
    "LinkCounts",
    "LinkableRecord",
    "MergeError",
    "MergeRefusal",
    "NotFoundError",
    "Occurrence",
    "Person",
    "PersonCursor",
    "PersonPage",
    "Role",
    "add_alias",
    "check_display_name",
    "clean_display_name",
    "create_person",
    "extract_occurrences",
    "find_person",
    "get_me",
    "get_person",
    "identity_of",
    "link_records",
    "list_persons",
    "lock_persons",
    "merge_person",
    "normalize_alias",
    "normalize_chat_user_id",
    "normalize_email",
    "normalize_name",
    "person_lock_key",
    "remove_alias",
    "rename_person",
    "set_me",
    "split_person",
    "unset_me",
]
