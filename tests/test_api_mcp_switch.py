"""The switch of the MCP endpoint, ``mcp_enabled``: off, the path answers 404 ``mcp_disabled`` to
everyone and the HTTP API goes on.

No database: the tokens are generated here and resolved by a dictionary. Every value is synthetic.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from memini.api import create_app
from memini.api.mcp import MCP_DISABLED
from memini.auth import AuthenticationError, Principal, Scope, generate_token
from memini.config import Settings
from memini.mcp import MCP_PATH

OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")
TOOL_CALL: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "status", "arguments": {}},
}


class FakeAuthenticator:
    """Resolves the tokens it issued, and records every token it was asked about."""

    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}
        self.calls: list[str] = []

    def issue(self, *scopes: Scope) -> dict[str, str]:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=OWNER, token_id=uuid.uuid4(), scopes=frozenset(scopes)
        )
        return {"Authorization": f"Bearer {token}"}

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
def off(settings: Settings) -> Settings:
    """The settings of a service whose MCP endpoint is switched off."""
    return settings.model_copy(update={"mcp_enabled": False})


def switched_off(response: Response) -> None:
    """Assert the answer of a switched-off endpoint: the error shape, the code, no challenge."""
    assert response.status_code == 404
    body = response.json()
    assert list(body) == ["error"]
    assert body["error"]["code"] == MCP_DISABLED == "mcp_disabled"
    assert body["error"]["message"] == "The MCP endpoint is switched off on this server."
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
    assert body["error"]["details"] == []
    assert "WWW-Authenticate" not in response.headers


def test_the_endpoint_is_served_by_default(
    settings: Settings, authenticator: FakeAuthenticator
) -> None:
    client = TestClient(create_app(settings, authenticator=authenticator))

    assert settings.mcp_enabled is True
    # The endpoint is there: it asks for a token, as it does before it reads a request.
    refused = client.post(MCP_PATH, json=TOOL_CALL)
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "unauthorized"
    status = client.get("/v1/status", headers=authenticator.issue("read"))
    assert status.status_code == 200
    assert status.json()["mcp_enabled"] is True


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("scopes", [(), ("read",), ("read", "ingest", "admin")])
def test_a_switched_off_endpoint_answers_mcp_disabled_to_everyone(
    off: Settings, authenticator: FakeAuthenticator, method: str, scopes: tuple[Scope, ...]
) -> None:
    client = TestClient(create_app(off, authenticator=authenticator))
    headers = authenticator.issue(*scopes) if scopes else {}

    response = client.request(
        method, MCP_PATH, headers=headers, json=TOOL_CALL if method == "POST" else None
    )

    switched_off(response)
    assert authenticator.calls == []  # answered before a token is looked at


def test_a_token_that_is_not_valid_gets_the_same_answer(
    off: Settings, authenticator: FakeAuthenticator
) -> None:
    client = TestClient(create_app(off, authenticator=authenticator))
    unknown = {"Authorization": f"Bearer {generate_token()}"}

    switched_off(client.post(MCP_PATH, headers=unknown, json=TOOL_CALL))
    switched_off(client.post(MCP_PATH, headers={"Authorization": "Bearer nonsense"}, json={}))


def test_no_mcp_server_is_built_so_no_tool_runs(
    off: Settings, authenticator: FakeAuthenticator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build(**_: object) -> None:
        raise AssertionError("an MCP server was built for a switched-off endpoint")

    monkeypatch.setattr("memini.api.app.build_server", build)

    # Entered, so the application's lifespan runs: it has no MCP endpoint to start.
    with TestClient(create_app(off, authenticator=authenticator)) as client:
        switched_off(client.post(MCP_PATH, headers=authenticator.issue("read"), json=TOOL_CALL))
        assert client.get("/healthz").status_code == 200


def test_the_http_api_goes_on_and_status_says_mcp_is_off(
    off: Settings, authenticator: FakeAuthenticator
) -> None:
    client = TestClient(create_app(off, authenticator=authenticator))

    for path in ("/healthz", "/readyz", "/version", "/openapi.json"):
        assert client.get(path).status_code == 200, path
    # The API still asks for its token and its scope, and answers with them.
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=authenticator.issue("ingest")).status_code == 403
    answered = client.get("/v1/status", headers=authenticator.issue("read"))
    assert answered.status_code == 200
    assert answered.json()["mcp_enabled"] is False
    assert answered.json()["database"]["reachable"] is False  # the rest of the answer is there
