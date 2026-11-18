"""Deterministic ids: fixed values, so that any change of the function is caught."""

import hashlib
import unicodedata
import uuid

import pytest

from memini.db import connect
from memini.ids import RECORD_ID_DOMAIN, RecordKind, hash_fields, record_id

SOURCE_A = uuid.UUID("01234567-89ab-7def-8123-456789abcdef")
SOURCE_B = uuid.UUID("0190a0a0-0000-7000-8000-000000000001")
CAFE_NFC = "notes/café.md"
CAFE_NFD = unicodedata.normalize("NFD", CAFE_NFC)

# Synthetic inputs and the ids version 1 of the function gives them. Never update a value to make
# a test pass: a different id here means every stored record id would change.
FIXED: list[tuple[uuid.UUID, RecordKind, str, str]] = [
    (
        SOURCE_A,
        "thread",
        "C1/1700000000.000100",
        "8a5f9a1016a599cb92b16611ba927c81d075bd01d599013c1385361d02933b9b",
    ),
    (
        SOURCE_A,
        "meeting",
        "meeting-0042",
        "0034e4b968aa974e51ad4f9d6b1ef112da480158b766907cce08d3f4ff744ccf",
    ),
    (
        SOURCE_B,
        "code_file",
        "refs/heads/main:src/app.py",
        "4aa296ab74df51a04689d6ab1dbb55a73ebc3aabc997e1e4cb79c35654b9c38c",
    ),
    (
        SOURCE_B,
        "document",
        CAFE_NFC,
        "ae9575b8c33883b9eb68ea10605a483df817eda2264f05c911b425f390bf83d4",
    ),
    (
        SOURCE_B,
        "document",
        CAFE_NFD,
        "c2544f08273e51c83b0e9245ea6456e331e197075a7d53c7c7e890a5de01a76c",
    ),
    (
        SOURCE_A,
        "thread",
        "заметки/встреча 1",
        "59270ab5394be7fdce075797fe5b12ee9113d087de8b790bd31d3e9592b58f9e",
    ),
]


@pytest.mark.parametrize(("source_id", "kind", "external_id", "expected"), FIXED)
def test_record_ids_are_fixed(
    source_id: uuid.UUID, kind: RecordKind, external_id: str, expected: str
) -> None:
    assert record_id(source_id, kind, external_id) == expected


def test_the_encoding_is_sha256_over_length_prefixed_fields() -> None:
    def field(data: bytes) -> bytes:
        return len(data).to_bytes(8, "big") + data

    encoded = field(b"memini.record.v1") + field(SOURCE_A.bytes) + field(b"thread") + field(b"x")

    assert RECORD_ID_DOMAIN == "memini.record.v1"
    assert record_id(SOURCE_A, "thread", "x") == hashlib.sha256(encoded).hexdigest()


def test_bytes_cannot_move_between_fields() -> None:
    together = hash_fields("d", b"ab", b"c")

    assert together != hash_fields("d", b"a", b"bc")
    assert together != hash_fields("d", b"abc")
    assert together != hash_fields("d", b"ab", b"c", b"")
    assert together != hash_fields("dab", b"c")


def test_every_input_changes_the_id() -> None:
    base = record_id(SOURCE_A, "thread", "t-1")

    assert record_id(SOURCE_B, "thread", "t-1") != base
    assert record_id(SOURCE_A, "document", "t-1") != base
    assert record_id(SOURCE_A, "thread", "t-2") != base
    assert record_id(SOURCE_A, "thread", "T-1") != base


def test_an_id_is_64_lowercase_hex_characters() -> None:
    value = record_id(SOURCE_A, "thread", "t-1")

    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    ("kind", "external_id", "message"),
    [
        ("email", "t-1", "unknown record kind"),
        ("thread", "", "non-empty external id"),
        ("thread", "lone \ud800 surrogate", "surrogate"),
    ],
)
def test_bad_input_is_refused(kind: str, external_id: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        record_id(SOURCE_A, kind, external_id)  # pyright: ignore[reportArgumentType]


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_database_computes_the_same_ids(migrated_database_url: str) -> None:
    async with connect(migrated_database_url) as conn:
        for source_id, kind, external_id, expected in FIXED:
            cur = await conn.execute("SELECT record_id(%s, %s, %s)", (source_id, kind, external_id))
            assert await cur.fetchone() == (expected,), (kind, external_id)
