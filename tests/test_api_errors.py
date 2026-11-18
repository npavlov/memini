"""Every error the HTTP API returns has one shape. Every value here is synthetic."""

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import BaseModel, field_validator

from memini.api import ApiError, create_app
from memini.config import Settings

# A distinctive value that must never come back in an error body.
CANARY = "canary-not-a-real-secret-91c2e7"


class Item(BaseModel):
    name: str
    size: int

    @field_validator("name")
    @classmethod
    def name_is_not_reserved(cls, value: str) -> str:
        if value.startswith("reserved"):
            # A careless validator that quotes the input; the error body must not.
            raise ValueError(f"name {value!r} is reserved")
        return value


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.post("/test/items")
    async def create_item(item: Item) -> Item:
        return item

    @app.get("/test/items")
    async def list_items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/test/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="Synthetic conflict.")

    @app.get("/test/unauthorized")
    async def unauthorized() -> None:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})

    @app.get("/test/structured-detail")
    async def structured_detail() -> None:
        raise HTTPException(status_code=400, detail={"token": CANARY})

    @app.get("/test/denied")
    async def denied() -> None:
        raise ApiError(403, "synthetic_denied", "Synthetic denial.", headers={"X-Synthetic": "yes"})

    @app.get("/test/boom")
    async def boom() -> None:
        raise RuntimeError(f"synthetic failure for postgresql://memini:{CANARY}@db.invalid/memini")

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # The server would log the exception Starlette re-raises after the 500; the test client must
    # not raise it, so the response can be checked.
    return TestClient(app, raise_server_exceptions=False)


def error_of(response: Response, status_code: int, code: str) -> dict[str, Any]:
    """Assert the error shape and return the ``error`` object."""
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert list(body) == ["error"]
    error: dict[str, Any] = body["error"]
    assert set(error) == {"code", "message", "request_id", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert CANARY not in response.text
    return error


def test_unknown_path_is_404(client: TestClient) -> None:
    error = error_of(client.get(f"/no/such/{CANARY}"), 404, "not_found")

    assert error["details"] == []


def test_wrong_method_is_405_and_keeps_allow(client: TestClient) -> None:
    response = client.post("/healthz")

    error_of(response, 405, "method_not_allowed")
    assert "GET" in response.headers["allow"]


def test_http_exception_keeps_its_message(client: TestClient) -> None:
    error = error_of(client.get("/test/conflict"), 409, "conflict")

    assert error["message"] == "Synthetic conflict."


def test_http_exception_keeps_its_headers(client: TestClient) -> None:
    response = client.get("/test/unauthorized")

    error = error_of(response, 401, "unauthorized")
    assert error["message"] == "Unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_http_exception_with_structured_detail_falls_back_to_the_reason(client: TestClient) -> None:
    error = error_of(client.get("/test/structured-detail"), 400, "bad_request")

    assert error["message"] == "Bad Request"


def test_api_error_carries_its_code_and_headers(client: TestClient) -> None:
    response = client.get("/test/denied")

    error = error_of(response, 403, "synthetic_denied")
    assert error["message"] == "Synthetic denial."
    assert response.headers["x-synthetic"] == "yes"


def test_invalid_body_gives_locations_and_types_only(client: TestClient) -> None:
    response = client.post("/test/items", json={"name": CANARY, "size": f"many-{CANARY}"})

    error = error_of(response, 422, "validation_error")
    assert error["details"] == [{"location": ["body", "size"], "type": "int_parsing"}]


def test_missing_field_is_reported_by_location(client: TestClient) -> None:
    response = client.post("/test/items", json={"name": CANARY})

    error = error_of(response, 422, "validation_error")
    assert error["details"] == [{"location": ["body", "size"], "type": "missing"}]


def test_validator_message_that_quotes_input_is_not_echoed(client: TestClient) -> None:
    response = client.post("/test/items", json={"name": f"reserved-{CANARY}", "size": 1})

    error = error_of(response, 422, "validation_error")
    assert error["details"] == [{"location": ["body", "name"], "type": "value_error"}]


def test_invalid_query_parameter_is_not_echoed(client: TestClient) -> None:
    response = client.get("/test/items", params={"limit": CANARY})

    error = error_of(response, 422, "validation_error")
    assert error["details"] == [{"location": ["query", "limit"], "type": "int_parsing"}]


def test_malformed_json_is_not_echoed(client: TestClient) -> None:
    response = client.post(
        "/test/items",
        content=f'{{"name": "{CANARY}", "size": '.encode(),
        headers={"content-type": "application/json"},
    )

    error = error_of(response, 422, "validation_error")
    [detail] = error["details"]
    assert detail["type"] == "json_invalid"
    assert detail["location"][0] == "body"


def test_unhandled_exception_is_500_without_its_text(client: TestClient) -> None:
    response = client.get("/test/boom")

    error = error_of(response, 500, "internal_error")
    assert error["message"] == "Internal server error."
    assert error["details"] == []
    for leak in ("RuntimeError", "synthetic failure", "Traceback", "db.invalid"):
        assert leak not in response.text


def test_unhandled_exception_still_reaches_the_server(app: FastAPI) -> None:
    # Starlette re-raises after sending the 500, so the server can log it.
    with pytest.raises(RuntimeError, match="synthetic failure"):
        TestClient(app).get("/test/boom")
