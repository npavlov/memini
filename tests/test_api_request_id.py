"""Request ids: generated, or taken from a safe X-Request-ID. Every value here is synthetic."""

import re

import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from memini.api import REQUEST_ID_HEADER, create_app, current_request_id, request_id_var
from memini.config import Settings

GENERATED = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)

    @app.get("/test/request-id")
    async def echo_request_id() -> dict[str, str | None]:
        return {"request_id": current_request_id()}

    async def mounted(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mounted"})

    app.mount("/test/mounted", mounted)
    return TestClient(app)


def test_request_id_is_generated(client: TestClient) -> None:
    response = client.get("/healthz")

    assert GENERATED.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_each_request_gets_its_own_id(client: TestClient) -> None:
    first = client.get("/healthz").headers[REQUEST_ID_HEADER]
    second = client.get("/healthz").headers[REQUEST_ID_HEADER]

    assert first != second


@pytest.mark.parametrize(
    "incoming",
    ["a", "req-42", "trace_01.HK7Q", "0f8c2b9e-7d1a-4c55-9a3e-2b6f1d0c8e47", "x" * 64],
)
def test_valid_incoming_id_is_kept(client: TestClient, incoming: str) -> None:
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: incoming})

    assert response.headers[REQUEST_ID_HEADER] == incoming


@pytest.mark.parametrize(
    "incoming",
    ["", "x" * 65, "has space", "semi;colon", "path/like", "<script>", 'quote"d', "café"],
)
def test_unsafe_incoming_id_is_replaced(client: TestClient, incoming: str) -> None:
    response = client.get("/healthz", headers={REQUEST_ID_HEADER.encode(): incoming.encode()})

    assert GENERATED.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_request_id_is_in_the_contextvar_during_the_request(client: TestClient) -> None:
    response = client.get("/test/request-id", headers={REQUEST_ID_HEADER: "req-ctx-1"})

    assert response.json() == {"request_id": "req-ctx-1"}
    assert response.headers[REQUEST_ID_HEADER] == "req-ctx-1"


def test_contextvar_is_cleared_after_the_request(client: TestClient) -> None:
    client.get("/healthz")

    assert request_id_var.get() is None


def test_incoming_id_reaches_the_error_body(client: TestClient) -> None:
    response = client.get("/no/such/path", headers={REQUEST_ID_HEADER: "req-err-7"})

    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == "req-err-7"
    assert response.json()["error"]["request_id"] == "req-err-7"


def test_mounted_app_gets_a_request_id(client: TestClient) -> None:
    response = client.get("/test/mounted/anything")

    assert response.text == "mounted"
    assert GENERATED.fullmatch(response.headers[REQUEST_ID_HEADER])
