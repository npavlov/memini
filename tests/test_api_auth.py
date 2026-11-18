"""Bearer-token authentication and scopes in the HTTP API, with a fake authenticator.

No database: the tokens are generated here and resolved by a dictionary. The database-backed path
is in tests/test_auth_db.py.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from memini.api import create_app
from memini.api.auth import require_scope
from memini.auth import AuthenticationError, Principal, Scope, generate_token
from memini.config import Settings

OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")


def error_of(response: Response, status_code: int, code: str) -> dict[str, Any]:
    """Assert the error shape and the code; return the ``error`` object."""
    assert response.status_code == status_code
    body = response.json()
    assert list(body) == ["error"]
    error: dict[str, Any] = body["error"]
    assert error["code"] == code
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert error["details"] == []
    return error


class FakeAuthenticator:
    """Resolves the tokens it was given; refuses every other one as unknown."""

    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}
        self.calls: list[str] = []

    def issue(self, *scopes: Scope) -> str:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=OWNER, token_id=uuid.uuid4(), scopes=frozenset(scopes)
        )
        return token

    async def __call__(self, token: str) -> Principal:
        self.calls.append(token)
        try:
            return self.tokens[token]
        except KeyError:
            raise AuthenticationError("unknown") from None


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def client(settings: Settings, authenticator: FakeAuthenticator) -> TestClient:
    # No pool: a request that gets past authentication and scopes answers 503 for the database.
    return TestClient(create_app(settings, authenticator=authenticator))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/version", "/openapi.json"])
def test_public_routes_need_no_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": ""}, {"Authorization": "Basic dXNlcjpwYXNz"}],
    ids=["no-header", "empty", "basic"],
)
def test_no_bearer_token_is_401(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/v1/workspaces", headers=headers)
    error = error_of(response, 401, "unauthorized")
    assert error["message"] == "A bearer token is required."
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="memini"'


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer not-a-token", "Bearer memini_short", "Bearer {token}{token}"],
)
def test_a_malformed_token_is_401_without_a_lookup(
    client: TestClient, authenticator: FakeAuthenticator, header: str
) -> None:
    token = authenticator.issue("read")
    value = header.format(token=token)
    response = client.get("/v1/workspaces", headers={"Authorization": value})
    error_of(response, 401, "invalid_token")
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="memini", error="invalid_token"'
    assert authenticator.calls == []
    assert value not in response.text


def test_an_unknown_token_is_401(client: TestClient, authenticator: FakeAuthenticator) -> None:
    token = generate_token()
    response = client.get("/v1/workspaces", headers=bearer(token))
    error = error_of(response, 401, "invalid_token")
    assert error["message"] == "The bearer token is not valid."
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="memini", error="invalid_token"'
    assert authenticator.calls == [token]
    assert token not in response.text


@pytest.mark.parametrize("reason", ["unknown", "revoked", "expired"])
def test_every_refused_token_gets_the_same_answer(settings: Settings, reason: Any) -> None:
    async def refuse(token: str) -> Principal:
        raise AuthenticationError(reason)

    client = TestClient(create_app(settings, authenticator=refuse))
    response = client.get("/v1/workspaces", headers=bearer(generate_token()))
    error = error_of(response, 401, "invalid_token")
    assert error["message"] == "The bearer token is not valid."


def test_the_scheme_is_case_insensitive(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    token = authenticator.issue("read")
    response = client.get("/v1/workspaces", headers={"Authorization": f"bearer {token}"})
    assert authenticator.calls == [token]
    error_of(response, 503, "unavailable")  # past authentication; no database here


def test_a_token_without_the_scope_is_403(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    token = authenticator.issue("ingest", "admin")
    response = client.get("/v1/workspaces", headers=bearer(token))
    error = error_of(response, 403, "insufficient_scope")
    assert error["message"] == "The token lacks the read scope."
    assert response.headers["WWW-Authenticate"] == (
        'Bearer realm="memini", error="insufficient_scope", scope="read"'
    )
    assert token not in response.text


def test_a_token_with_the_scope_gets_through(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    # Authentication and the scope pass; the route then needs the database, which is absent.
    response = client.get("/v1/workspaces", headers=bearer(authenticator.issue("read")))
    error = error_of(response, 503, "unavailable")
    assert error["message"] == "The database is not available."


def test_without_an_authenticator_v1_is_unavailable(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    error_of(client.get("/v1/workspaces"), 401, "unauthorized")
    response = client.get("/v1/workspaces", headers=bearer(generate_token()))
    error = error_of(response, 503, "unavailable")
    assert error["message"] == "Authentication is not available."


def test_an_unknown_v1_route_is_404(client: TestClient) -> None:
    error_of(client.get("/v1/nothing-here"), 404, "not_found")


def test_openapi_documents_the_bearer_scheme_on_v1_only(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"] == {
        "bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "A Memini token: memini_ followed by 43 characters. Created with the "
            "CLI.",
        }
    }
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            security = operation.get("security")
            if path.startswith("/v1/"):
                assert security == [{"bearer": []}], path
            else:
                assert security is None, path


def test_require_scope_gives_one_dependency_per_scope() -> None:
    assert require_scope("read") is require_scope("read")
    assert require_scope("read") is not require_scope("admin")
    with pytest.raises(ValueError, match="unknown scope"):
        require_scope("write")  # type: ignore[arg-type]
