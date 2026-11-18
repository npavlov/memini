"""Deterministic ids.

A record id is derived from what the record is, not from when or where it was stored. The same
record ingested twice, on any machine, gets the same id, so ingest is an upsert (principle 6).

Encoding, version 1: SHA-256 over a sequence of fields, each preceded by its length in bytes as an
unsigned 64-bit big-endian integer. The first field is a domain tag naming the kind of id and its
version, so ids of different kinds or versions never coincide. The length prefix on every field
makes the encoding injective: no choice of values moves bytes from one field into the next. The
id is the digest as 64 lowercase hex characters. Only `hashlib`, `int.to_bytes` and UTF-8 are
involved, so the result does not depend on the machine or the Python version.

The database has the same function, `record_id` in migration 0002, and checks every record row
against it; tests pin both to the same fixed values. A change of the function changes every id: it
takes a new domain version and a migration, never an edit of this one.
"""

import hashlib
import uuid
from typing import Final, Literal, get_args

RecordKind = Literal["code_file", "thread", "meeting", "document"]
"""Kinds of raw record (requirements, section 3). The database checks the same list."""

RECORD_KINDS: Final[frozenset[str]] = frozenset(get_args(RecordKind))

RECORD_ID_DOMAIN: Final = "memini.record.v1"
"""Domain tag of record ids. A new version of the function gets a new tag."""

_LENGTH_BYTES: Final = 8


def hash_fields(domain: str, *fields: bytes) -> str:
    """SHA-256 over `domain` (UTF-8) and `fields`, each length-prefixed; 64 lowercase hex chars.

    The building block of every deterministic id. `domain` names the id and its version.
    """
    digest = hashlib.sha256()
    for field in (domain.encode(), *fields):
        digest.update(len(field).to_bytes(_LENGTH_BYTES, "big"))
        digest.update(field)
    return digest.hexdigest()


def record_id(source_id: uuid.UUID, kind: RecordKind, external_id: str) -> str:
    """The id of the record with `external_id` and `kind` in the source `source_id`.

    Fields after the domain `memini.record.v1`: the 16 bytes of `source_id` (`UUID.bytes`, the
    byte order of RFC 9562), then `kind` and `external_id` in UTF-8. `external_id` is used exactly
    as given; two strings that differ in any way, Unicode normalization or case included, are two
    records. The stream is not an input: a record reached through two streams of one source is one
    record.

    Raises `ValueError` for an unknown kind, an empty external id, or an external id that is not
    valid Unicode (a lone surrogate cannot be encoded).
    """
    if kind not in RECORD_KINDS:
        raise ValueError(f"unknown record kind {kind!r}; expected one of {sorted(RECORD_KINDS)}")
    if not external_id:
        raise ValueError("a record needs a non-empty external id")
    return hash_fields(RECORD_ID_DOMAIN, source_id.bytes, kind.encode(), external_id.encode())
