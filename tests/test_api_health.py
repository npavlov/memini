"""Tests for the public operational routes and the OpenAPI schema. Every value here is synthetic."""

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

import memini
from memini.api import ReadinessCheck, create_app
from memini.config import Settings

# A distinctive value that must never leave a failing readiness check.
CANARY = "canary-not-a-real-secret-4b1e8d"
FAKE_URL = f"postgresql://memini:{CANARY}@db.invalid:5432/memini"


async def passing() -> None:
    return None


async def failing() -> None:
    raise ConnectionError(f"could not connect to {FAKE_URL}")


async def hanging() -> None:
    await asyncio.sleep(3600)


def client_for(
    settings: Settings,
    checks: dict[str, ReadinessCheck] | None = None,
    timeout: float = 1.0,
) -> TestClient:
    return TestClient(create_app(settings, readiness_checks=checks, readiness_timeout=timeout))


def test_liveness_answers_without_a_token(settings: Settings) -> None:
    response = client_for(settings).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_runs_no_readiness_check(settings: Settings) -> None:
    calls: list[str] = []

    async def counted() -> None:
        calls.append("database")
        raise ConnectionError("down")

    response = client_for(settings, {"database": counted}).get("/healthz")

    assert response.status_code == 200
    assert calls == []


def test_readiness_without_checks_is_ready(settings: Settings) -> None:
    response = client_for(settings).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {}}


def test_readiness_passes_when_every_check_passes(settings: Settings) -> None:
    response = client_for(settings, {"database": passing, "other": passing}).get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": {"status": "ok"}, "other": {"status": "ok"}},
    }


def test_readiness_fails_when_a_check_fails(settings: Settings) -> None:
    response = client_for(settings, {"database": failing, "other": passing}).get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": {"status": "failed"}, "other": {"status": "ok"}},
    }


def test_readiness_never_shows_or_logs_the_failure_text(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    response = client_for(settings, {"database": failing}).get("/readyz")

    assert response.status_code == 503
    for text in (response.text, caplog.text, str(dict(response.headers))):
        assert CANARY not in text
        assert "db.invalid" not in text
    assert "ConnectionError" in caplog.text  # the log names the kind of failure


def test_readiness_times_out_a_hanging_check(settings: Settings) -> None:
    client = client_for(settings, {"database": hanging, "other": passing}, timeout=0.05)

    started = time.monotonic()
    response = client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": {"status": "timeout"}, "other": {"status": "ok"}},
    }
    assert elapsed < 2.0


def test_readiness_runs_checks_concurrently(settings: Settings) -> None:
    async def slow() -> None:
        await asyncio.sleep(0.2)

    client = client_for(settings, {"a": slow, "b": slow, "c": slow}, timeout=1.0)

    started = time.monotonic()
    response = client.get("/readyz")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 0.5


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_readiness_timeout_is_refused(settings: Settings, timeout: float) -> None:
    with pytest.raises(ValueError, match="readiness timeout"):
        create_app(settings, readiness_timeout=timeout)


def test_version_reports_the_package_version(settings: Settings) -> None:
    response = client_for(settings).get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": memini.__version__}


def test_openapi_schema_is_served(settings: Settings) -> None:
    response = client_for(settings).get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Memini"
    assert schema["info"]["version"] == memini.__version__
    assert {"/healthz", "/readyz", "/version"} <= set(schema["paths"])
    assert "ErrorResponse" in schema["components"]["schemas"]
    readyz = schema["paths"]["/readyz"]["get"]["responses"]
    assert readyz["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Readiness"
    }


def test_openapi_documents_the_error_shape_instead_of_fastapi_default(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/test/items")
    async def list_items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    schema = TestClient(app).get("/openapi.json").json()

    responses = schema["paths"]["/test/items"]["get"]["responses"]
    assert responses["default"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert "HTTPValidationError" not in schema["components"]["schemas"]


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_interactive_docs_are_off(settings: Settings, path: str) -> None:
    response = client_for(settings).get(path)

    assert response.status_code == 404
