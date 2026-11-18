"""The checks of a batch and the stored form of a payload, and the ingest route before the
database. The database-backed paths are in tests/test_ingest_db.py and tests/test_api_ingest_db.py.
Every value is synthetic."""

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ingest_helpers import DAY, HOUR, T0, document, iso, meeting, thread, tombstone
from memini.api import create_app
from memini.auth import AuthenticationError, Principal, Scope, generate_token
from memini.config import Settings
from memini.ingest import (
    MAX_EXTERNAL_ID_BYTES,
    MAX_PAYLOAD_DEPTH,
    Batch,
    BatchTooLargeError,
    IngestLimits,
    InvalidBatchError,
    RecordIn,
    WindowClaim,
    canonical_json,
    content_hash,
    prepare_batch,
)

PRIVATE = "private-words-that-must-not-echo-41c9"
LIMITS = IngestLimits(max_records=10, max_payload_size=4096)


# --- The stored form of a payload ----------------------------------------------------------------


def test_a_payload_is_stored_as_canonical_json() -> None:
    separator = chr(0x2028)  # a line separator: JSON text, written as it is
    payload = {"b": [1, 2.5, True, None], "a": {"z": "Grüße\n", "y": separator}, "c": -0.0}
    expected = '{"a":{"y":"%s","z":"Grüße\\n"},"b":[1,2.5,true,null],"c":-0.0}'
    assert canonical_json(payload) == (expected % separator).encode()
    reordered = json.loads(
        json.dumps(
            {"c": -0.0, "a": {"z": "Grüße\n", "y": separator}, "b": [1, 2.5, True, None]},
            indent=4,
        )
    )
    assert canonical_json(reordered) == canonical_json(payload)
    assert canonical_json(MappingProxyType({"t": ("x", "y")})) == b'{"t":["x","y"]}'
    data = canonical_json(payload)
    assert content_hash(data) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"n": float("nan")}, "not finite"),
        ({"n": float("inf")}, "not finite"),
        ({1: "one"}, "key that is not a string"),
        ({"s": "\ud800"}, "not valid Unicode"),
        ({"\udfff": "s"}, "not valid Unicode"),
        ({"when": datetime(2026, 1, 1)}, "not JSON"),
        ({"set": {1, 2}}, "not JSON"),
    ],
)
def test_a_payload_canonical_json_cannot_hold_exactly_is_refused(
    payload: Mapping[Any, Any], reason: str
) -> None:
    with pytest.raises(InvalidBatchError, match=reason) as refused:
        canonical_json(payload, field="records[0].payload")
    assert refused.value.field == "records[0].payload"


def test_a_payload_nested_too_deep_is_refused() -> None:
    deep: Any = "leaf"
    for _ in range(MAX_PAYLOAD_DEPTH - 1):
        deep = [deep]
    assert canonical_json({"x": deep})  # exactly the limit
    with pytest.raises(InvalidBatchError, match="levels deep"):
        canonical_json({"x": [deep]})


# --- Records -------------------------------------------------------------------------------------


def one(record: RecordIn, **kwargs: Any) -> Batch:
    return Batch(records=[record], **kwargs)


def refuse(batch: Batch, field: str, limits: IngestLimits = LIMITS) -> InvalidBatchError:
    with pytest.raises(InvalidBatchError) as refused:
        prepare_batch(batch, limits)
    assert refused.value.field == field, refused.value
    assert PRIVATE not in str(refused.value)
    return refused.value


def test_a_valid_batch_is_prepared() -> None:
    batch = Batch(
        stream="chat/general",
        window=WindowClaim(start=T0, end=T0 + DAY, complete=True, cursor={"page": 3}),
        records=[
            thread("general/0001"),
            meeting("m-1"),
            document("doc-1", "# Notes\n"),
            tombstone("x"),
        ],
    )
    prepared = prepare_batch(batch, LIMITS)
    assert [record.index for record in prepared.records] == [0, 1, 2]
    assert [(t.index, t.kind, t.external_id) for t in prepared.tombstones] == [(3, "thread", "x")]
    first = prepared.records[0]
    assert first.payload == canonical_json(thread("general/0001").payload or {})
    assert first.content_hash == hashlib.sha256(first.payload).hexdigest()
    assert first.external_url == "https://chat.example.invalid/archives/general/0001"
    assert prepared.cursor == {"page": 3}
    assert prepared.stream == "chat/general"


def with_payload(record: RecordIn, payload: Mapping[str, Any]) -> RecordIn:
    return RecordIn(
        kind=record.kind,
        external_id=record.external_id,
        occurred_at=record.occurred_at,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (with_payload(thread("t"), {}), "records[0].payload.messages"),
        (with_payload(thread("t"), {"messages": []}), "records[0].payload.messages"),
        (with_payload(thread("t"), {"messages": [PRIVATE]}), "records[0].payload.messages[0]"),
        (
            with_payload(thread("t"), {"messages": [{"sent_at": iso(T0), "text": PRIVATE}]}),
            "records[0].payload.messages[0].author",
        ),
        (
            with_payload(
                thread("t"), {"messages": [{"author": " ", "sent_at": iso(T0), "text": ""}]}
            ),
            "records[0].payload.messages[0].author",
        ),
        (
            with_payload(
                thread("t"),
                {"messages": [{"author": "Ann", "sent_at": "2026-03-01T09:00:00", "text": ""}]},
            ),
            "records[0].payload.messages[0].sent_at",
        ),
        (
            with_payload(thread("t"), {"messages": [{"author": "Ann", "sent_at": PRIVATE}]}),
            "records[0].payload.messages[0].sent_at",
        ),
        (
            with_payload(thread("t"), {"messages": [{"author": "Ann", "sent_at": iso(T0)}]}),
            "records[0].payload.messages[0].text",
        ),
        (with_payload(meeting("m"), {"started_at": iso(T0)}), "records[0].payload.title"),
        (with_payload(meeting("m"), {"title": "Plan"}), "records[0].payload.started_at"),
        (
            with_payload(
                meeting("m"), {"title": "Plan", "started_at": iso(T0), "ended_at": iso(T0 - HOUR)}
            ),
            "records[0].payload.ended_at",
        ),
        (with_payload(document("d", ""), {"title": "Notes"}), "records[0].payload.text"),
        (with_payload(document("d", ""), {"text": PRIVATE}), "records[0].payload.title"),
    ],
)
def test_a_payload_without_the_fields_of_its_kind_is_refused(record: RecordIn, field: str) -> None:
    refuse(one(record), field)


def test_a_record_needs_a_kind_an_id_a_zoned_time_and_an_object() -> None:
    error = refuse(
        one(RecordIn(kind="code_file", external_id="main:a.py", deleted=True)), "records[0].kind"
    )
    assert "git sources" in error.reason
    refuse(one(RecordIn(kind="note", external_id="n", deleted=True)), "records[0].kind")
    refuse(one(RecordIn(kind="thread", external_id="", deleted=True)), "records[0].external_id")
    refuse(one(RecordIn(kind="thread", external_id="a\0b", deleted=True)), "records[0].external_id")
    refuse(
        one(RecordIn(kind="thread", external_id="\ud800", deleted=True)), "records[0].external_id"
    )
    longest = "é" * (MAX_EXTERNAL_ID_BYTES // 2)
    prepare_batch(one(tombstone(longest)), LIMITS)
    refuse(one(tombstone(longest + "e")), "records[0].external_id")

    naive = RecordIn(kind="document", external_id="d", occurred_at=datetime(2026, 3, 1), payload={})
    refuse(one(naive), "records[0].occurred_at")
    refuse(one(RecordIn(kind="document", external_id="d", payload={})), "records[0].occurred_at")
    refuse(one(RecordIn(kind="document", external_id="d", occurred_at=T0)), "records[0].payload")
    refuse(
        one(RecordIn(kind="thread", external_id="t", deleted=True, payload={"messages": []})),
        "records[0]",
    )
    refuse(
        one(RecordIn(kind="thread", external_id="t", deleted=True, occurred_at=T0)), "records[0]"
    )
    offset = RecordIn(
        kind="document",
        external_id="d",
        occurred_at=T0.astimezone(timezone(timedelta(hours=3))),
        payload={"title": "Notes", "text": ""},
    )
    assert prepare_batch(one(offset), LIMITS).records[0].occurred_at == T0


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://files.example.invalid/a",
        "https://",
        "https://chat.example.invalid/a b",
        "https://chat.example.invalid/\x07",
        "/relative/path",
        "https://chat.example.invalid/" + "a" * 2048,
    ],
)
def test_an_external_url_is_an_http_link(url: str) -> None:
    record = RecordIn(
        kind="document",
        external_id="d",
        occurred_at=T0,
        payload={"title": "Notes", "text": ""},
        external_url=url,
    )
    refuse(one(record), "records[0].external_url")


def test_a_kind_and_id_appear_once_per_batch() -> None:
    error = refuse(Batch(records=[thread("t"), meeting("t"), tombstone("t")]), "records[2]")
    assert "records[0]" in error.reason


# --- Streams, windows and limits -----------------------------------------------------------------


def test_a_window_is_claimed_on_a_stream_and_ends_after_it_starts() -> None:
    claim = WindowClaim(start=T0, end=T0 + DAY, complete=True)
    refuse(Batch(window=claim), "window")
    refuse(Batch(stream="s", window=WindowClaim(start=T0, end=T0, complete=True)), "window.to")
    refuse(
        Batch(stream="s", window=WindowClaim(start=datetime(2026, 3, 1), end=T0, complete=True)),
        "window.from",
    )
    refuse(
        Batch(
            stream="s", window=WindowClaim(start=T0, end=T0 + DAY, complete=False, snapshot=True)
        ),
        "window.snapshot",
    )
    refuse(
        Batch(
            stream="s",
            window=WindowClaim(start=T0, end=T0 + DAY, complete=True, cursor={"k": "x" * 5000}),
        ),
        "window.cursor",
    )
    for key in ("", "  ", "a\nb", "\ud800", "k" * 1025):
        refuse(Batch(stream=key), "stream")
    assert prepare_batch(Batch(stream="k" * 1024), LIMITS).stream == "k" * 1024


def test_the_limits_of_a_batch_have_their_own_error() -> None:
    with pytest.raises(BatchTooLargeError) as many:
        prepare_batch(Batch(records=[tombstone(f"t{n}") for n in range(11)]), LIMITS)
    assert (many.value.code, many.value.field) == ("too_many_records", "records")

    big = with_payload(document("d", ""), {"title": "Notes", "text": PRIVATE * 200})
    with pytest.raises(BatchTooLargeError) as heavy:
        prepare_batch(Batch(records=[tombstone("a"), big]), LIMITS)
    assert (heavy.value.code, heavy.value.field) == ("payload_too_large", "records[1].payload")
    assert PRIVATE not in str(heavy.value)


# --- The route, before the database --------------------------------------------------------------

OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")
RECORDS = "/v1/workspaces/alpha/sources/chat/records"


class FakeAuthenticator:
    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}

    def issue(self, *scopes: Scope) -> dict[str, str]:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=OWNER, token_id=uuid.uuid4(), scopes=frozenset(scopes)
        )
        return {"Authorization": f"Bearer {token}"}

    async def __call__(self, token: str) -> Principal:
        try:
            return self.tokens[token]
        except KeyError:
            raise AuthenticationError("unknown") from None


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def client(settings: Settings, authenticator: FakeAuthenticator) -> TestClient:
    small = settings.model_copy(update={"ingest_max_body_size": 2048})
    # No pool: a request that gets past the checks answers 503 for the database.
    return TestClient(create_app(small, authenticator=authenticator))


BATCH = {
    "stream": "chat/general",
    "records": [{"kind": "thread", "external_id": "general/0001", "deleted": True}],
}


def test_a_batch_needs_the_ingest_scope(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    refusals: list[tuple[Scope, ...]] = [("read", "admin"), ("read",)]
    for scopes in refusals:
        response = client.post(RECORDS, json=BATCH, headers=authenticator.issue(*scopes))
        assert response.status_code == 403
        assert response.headers["WWW-Authenticate"].endswith('scope="ingest"')
    admitted = client.post(RECORDS, json=BATCH, headers=authenticator.issue("ingest"))
    assert admitted.status_code == 503  # past the scope, at the missing database
    assert client.post(RECORDS, json=BATCH).status_code == 401


def test_a_body_over_the_limit_is_refused_before_it_is_read(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    headers = {**authenticator.issue("ingest"), "Content-Type": "application/json"}
    big = json.dumps({**BATCH, "padding": "x" * 4096}).encode()
    response = client.post(RECORDS, content=big, headers=headers)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "content_too_large"


def test_a_body_with_a_key_twice_is_refused(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    headers = {**authenticator.issue("ingest"), "Content-Type": "application/json"}
    body = b'{"stream": "a", "stream": "b", "records": []}'
    response = client.post(RECORDS, content=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_key"
    response = client.post(RECORDS, content=b"\xff\xfe{", headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"invalid_json", "validation_error"}


def test_the_schema_documents_the_ingest_route(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/workspaces/{workspace}/sources/{source}/records"]["post"]
    assert "200" in operation["responses"]
    assert "default" in operation["responses"]
    components = schema["components"]["schemas"]
    assert set(components["WindowBody"]["properties"]) == {
        "from",
        "to",
        "complete",
        "snapshot",
        "cursor",
    }
    assert set(components["RecordBody"]["properties"]["kind"]["enum"]) == {
        "thread",
        "meeting",
        "document",
    }
    assert "IngestAccepted" in components
    assert "NewPushSource" in components
