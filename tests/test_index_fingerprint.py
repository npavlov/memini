"""The index fingerprint as a value: what goes in, what does not, and its fixed encoding."""

from dataclasses import replace

import pytest

from memini.embeddings import EmbeddingSpec
from memini.ids import hash_fields
from memini.index import IndexFingerprint, embedding_key, reindex_command
from memini.index.fingerprint import EMBEDDING_KEY_DOMAIN, INDEX_FINGERPRINT_DOMAIN

LOCAL = EmbeddingSpec(
    provider="local",
    model="synthetic-org/synthetic-model",
    dimension=384,
    digest="sha256:" + "ab" * 32,
    pooling="mean",
    max_tokens=512,
    document_prefix="passage: ",
)
REMOTE = EmbeddingSpec(provider="openai_compatible", model="synthetic-embed:v1", dimension=768)
BASE = IndexFingerprint(LOCAL, chunker_version="1")


def test_the_encoding_is_fixed() -> None:
    """Pinned values: a change here changes every stored fingerprint and blocks every index."""
    assert BASE.embedding_key == "1750e7648c299782fdc9405dfc868d99f65f0eed96144d290234b73103c962ef"
    assert BASE.digest == "480281c14c254316a15671af2f3e438916dc1b5cb7dcd97c5eeda40e56d13f27"


def test_the_encoding_is_hash_fields_over_the_documented_parts() -> None:
    key = hash_fields(
        EMBEDDING_KEY_DOMAIN,
        b"local",
        b"synthetic-org/synthetic-model",
        LOCAL.digest.encode(),
        b"384",
        b"l2",
        b"mean",
        b"512",
        b"passage: ",
    )

    assert embedding_key(LOCAL) == key
    assert BASE.digest == hash_fields(INDEX_FINGERPRINT_DOMAIN, bytes.fromhex(key), b"1")


def test_a_remote_spec_encodes_missing_parts_as_empty() -> None:
    key = hash_fields(
        EMBEDDING_KEY_DOMAIN, b"openai_compatible", b"synthetic-embed:v1", b"", b"768", b"l2", b"",
        b"0", b"",
    )  # fmt: skip

    assert embedding_key(REMOTE) == key


SERVED = replace(LOCAL, provider="openai_compatible", pooling=None, max_tokens=None)
"""The same model, served by an endpoint instead of run in the process."""


@pytest.mark.parametrize(
    ("changed", "part"),
    [
        (IndexFingerprint(replace(LOCAL, model="synthetic-org/other-model"), "1"), "model"),
        (IndexFingerprint(replace(LOCAL, digest="sha256:" + "cd" * 32), "1"), "model digest"),
        (IndexFingerprint(replace(LOCAL, dimension=768), "1"), "dimension"),
        (IndexFingerprint(replace(LOCAL, pooling="cls"), "1"), "pooling"),
        (IndexFingerprint(replace(LOCAL, max_tokens=256), "1"), "max tokens"),
        (IndexFingerprint(replace(LOCAL, document_prefix=""), "1"), "document prefix"),
        (IndexFingerprint(SERVED, "1"), "provider"),
        (IndexFingerprint(LOCAL, "2"), "chunker version"),
    ],
)
def test_every_part_changes_the_fingerprint(changed: IndexFingerprint, part: str) -> None:
    assert changed.digest != BASE.digest
    assert part in BASE.changes(changed)


def test_a_new_chunker_keeps_the_embedding_key() -> None:
    """Vectors are reused across chunker versions: only the chunks change, not the vector space."""
    rechunked = IndexFingerprint(LOCAL, chunker_version="2")

    assert rechunked.embedding_key == BASE.embedding_key
    assert rechunked.digest != BASE.digest
    assert BASE.changes(rechunked) == ("chunker version",)


def test_an_equal_fingerprint_has_no_changes() -> None:
    same = IndexFingerprint(replace(LOCAL), chunker_version="1")

    assert same.digest == BASE.digest
    assert BASE.changes(same) == ()


def test_a_fingerprint_needs_a_chunker_version() -> None:
    with pytest.raises(ValueError, match="chunker version"):
        IndexFingerprint(LOCAL, chunker_version="")


def test_describe_and_the_rebuild_command() -> None:
    assert IndexFingerprint(REMOTE, "3").describe() == (
        "model synthetic-embed:v1 (openai_compatible, dimension 768), chunker 3"
    )
    assert reindex_command("acme") == "memini reindex --workspace acme"
