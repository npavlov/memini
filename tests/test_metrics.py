"""Metrics without a database: the text format, the series limit, ``GET /metrics`` and its token,
labels that never carry a path, a name or a query, and the counters of rate limits and MCP tools.

Every value is synthetic. The database-backed gauges are in tests/test_metrics_db.py.
"""

import io
import math
import re
import secrets
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr

from memini import __version__, logs
from memini.api import create_app
from memini.api.metrics import METRICS_PATH, UNMATCHED
from memini.auth import AuthenticationError, Principal, Scope, generate_token
from memini.cli.serve import configure_logging
from memini.config import ConfigError, Settings, load_settings
from memini.mcp import MCP_PATH
from memini.metrics import (
    CONTENT_TYPE,
    HTTP_REQUESTS,
    MAX_SERIES,
    MCP_TOOL_CALLS,
    OTHER,
    RATE_LIMIT_REFUSALS,
    Counter,
    Histogram,
    Registry,
    family,
    gauge,
    render,
)

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
METRICS_TOKEN = "synthetic-metrics-" + secrets.token_hex(16)
BASE_URL = "http://memini.test"
PRIVATE_NAME = "synthetic-private-workspace"
PRIVATE_QUERY = "synthetic-private-query"

# One sample line of the text format: a name, labels, a value.
_SAMPLE = re.compile(
    r'(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*",?)*)\})?'
    r" (?P<value>\S+)"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


class FakeAuthenticator:
    """Resolves the Memini tokens it issued; refuses every other one as unknown."""

    def __init__(self) -> None:
        self.tokens: dict[str, Principal] = {}

    def issue(self, *scopes: Scope) -> str:
        token = generate_token()
        self.tokens[token] = Principal(
            principal_id=uuid.UUID(int=1), token_id=uuid.uuid4(), scopes=frozenset(scopes)
        )
        return token

    async def __call__(self, token: str) -> Principal:
        try:
            return self.tokens[token]
        except KeyError:
            raise AuthenticationError("unknown") from None


def settings_with(**values: Any) -> Settings:
    return Settings(database_url=SecretStr(FAKE_URL), **values)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def samples(text: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    """Every sample of ``text``, by name and label set; fails on a line that is not one."""
    found: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
    for line in text.splitlines():
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            continue
        match = _SAMPLE.fullmatch(line)
        assert match is not None, line
        labels = frozenset(_LABEL.findall(match.group("labels") or ""))
        found[(match.group("name"), labels)] = float(match.group("value"))
    return found


def value(text: str, name: str, **labels: str) -> float:
    return samples(text)[(name, frozenset(labels.items()))]


@pytest.fixture
def authenticator() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def client(authenticator: FakeAuthenticator) -> Iterator[TestClient]:
    app = create_app(
        settings_with(metrics_token=SecretStr(METRICS_TOKEN)), authenticator=authenticator
    )
    with TestClient(app) as client:
        yield client


def scrape(client: TestClient) -> str:
    response = client.get(METRICS_PATH, headers=bearer(METRICS_TOKEN))
    assert response.status_code == 200, response.text
    return response.text


# --- The text format ------------------------------------------------------------------------------


def test_counters_histograms_and_gauges_render_in_the_text_format() -> None:
    registry = Registry()
    requests = registry.counter("test_requests_total", "Requests.\nTwo lines.", ("route",))
    took = registry.histogram("test_seconds", "Time.", buckets=(0.1, 1.0))
    requests.inc(route='/a"b\\c\nd')
    requests.inc(2, route="/x")
    for seconds in (0.05, 0.1, 0.5, 7.0):
        took.observe(seconds)
    took.observe(math.nan)  # ignored

    text = render(
        [
            *registry.collect(),
            gauge("test_up", "Up.", [({}, 1.0)]),
            gauge("test_ratio", "A ratio.", [({"kind": "a"}, 0.25), ({"kind": "b"}, math.inf)]),
            family("test_external_total", "counter", "Kept elsewhere.", [({}, 3.0)]),
        ]
    )

    assert text.endswith("\n")
    assert text.splitlines() == [
        "# HELP test_requests_total Requests.\\nTwo lines.",
        "# TYPE test_requests_total counter",
        'test_requests_total{route="/a\\"b\\\\c\\nd"} 1',
        'test_requests_total{route="/x"} 2',
        "# HELP test_seconds Time.",
        "# TYPE test_seconds histogram",
        'test_seconds_bucket{le="0.1"} 2',
        'test_seconds_bucket{le="1"} 3',
        'test_seconds_bucket{le="+Inf"} 4',
        "test_seconds_sum 7.65",
        "test_seconds_count 4",
        "# HELP test_up Up.",
        "# TYPE test_up gauge",
        "test_up 1",
        "# HELP test_ratio A ratio.",
        "# TYPE test_ratio gauge",
        'test_ratio{kind="a"} 0.25',
        'test_ratio{kind="b"} +Inf',
        "# HELP test_external_total Kept elsewhere.",
        "# TYPE test_external_total counter",
        "test_external_total 3",
    ]
    assert requests.value(route="/x") == 2
    assert took.count() == 4


def test_a_metric_keeps_a_bounded_number_of_series() -> None:
    counter = Counter("test_bounded_total", "Bounded.", ("value",))
    for n in range(MAX_SERIES + 50):
        counter.inc(value=f"v{n}")

    rendered = counter.collect().samples
    assert len(rendered) == MAX_SERIES + 1
    assert counter.value(value=OTHER) == 50
    counter.inc(value="v0")  # a series it keeps is still counted under its own labels
    assert counter.value(value="v0") == 2

    histogram = Histogram("test_bounded_seconds", "Bounded.", ("value",), buckets=(1.0,))
    for n in range(MAX_SERIES + 1):
        histogram.observe(0.5, value=f"v{n}")
    assert histogram.count(value=OTHER) == 1


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("1bad", ()),
        ("has space", ()),
        ("test_total", ("bad-label",)),
        ("test_total", ("__reserved",)),
        ("test_total", ("le",)),
        ("test_total", ("route", "route")),
    ],
)
def test_names_and_labels_are_checked(name: str, labels: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match=r"name|label"):
        Counter(name, "Help.", labels)


def test_a_metric_takes_exactly_its_labels_and_only_goes_up() -> None:
    counter = Counter("test_strict_total", "Strict.", ("route",))
    with pytest.raises(ValueError, match="takes the labels"):
        counter.inc()
    with pytest.raises(ValueError, match="takes the labels"):
        counter.inc(route="/a", method="GET")
    with pytest.raises(ValueError, match="only goes up"):
        counter.inc(-1, route="/a")
    with pytest.raises(ValueError, match="in order"):
        Histogram("test_bad_seconds", "Bad.", buckets=(1.0, 0.5))
    registry = Registry()
    registry.counter("test_twice_total", "Once.")
    with pytest.raises(ValueError, match="exists"):
        registry.counter("test_twice_total", "Twice.")
    with pytest.raises(ValueError, match="two metrics"):
        render([gauge("test_same", "A."), gauge("test_same", "B.")])


# --- Access ---------------------------------------------------------------------------------------


def test_without_a_metrics_token_there_is_no_route() -> None:
    client = TestClient(create_app(settings_with()))

    assert client.get(METRICS_PATH).status_code == 404
    assert METRICS_PATH not in client.get("/openapi.json").json()["paths"]


def test_the_route_needs_the_metrics_token_and_no_other(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    missing = client.get(METRICS_PATH)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["WWW-Authenticate"] == 'Bearer realm="memini"'

    for header in (
        bearer(METRICS_TOKEN + "x"),
        bearer(METRICS_TOKEN[:-1]),
        bearer(authenticator.issue("read", "admin")),  # a Memini token, an admin one at that
        {"Authorization": f"Basic {METRICS_TOKEN}"},
    ):
        refused = client.get(METRICS_PATH, headers=header)
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] in ("unauthorized", "invalid_token")
        assert METRICS_TOKEN not in refused.text

    answered = client.get(METRICS_PATH, headers={"Authorization": f"bearer  {METRICS_TOKEN} "})
    assert answered.status_code == 200
    assert answered.headers["content-type"] == CONTENT_TYPE
    assert answered.headers["cache-control"] == "no-store"
    text = answered.text
    samples(text)  # every line parses
    # Without a database: not reachable, so not current.
    assert value(text, "memini_database_reachable") == 0
    assert value(text, "memini_base_current") == 0
    assert value(text, "memini_index_state", state="off") == 1
    assert value(text, "memini_info", version=__version__) == 1
    documented = client.get("/openapi.json").json()["paths"][METRICS_PATH]["get"]
    assert CONTENT_TYPE in documented["responses"]["200"]["content"]


def test_the_metrics_token_is_a_secret_setting_of_some_length(tmp_path: Path) -> None:
    token_file = tmp_path / "metrics-token"
    token_file.write_text(METRICS_TOKEN + "\n")
    loaded = load_settings(
        {"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_METRICS_TOKEN_FILE": str(token_file)}
    )
    assert loaded.metrics_token is not None
    assert loaded.metrics_token.get_secret_value() == METRICS_TOKEN
    assert METRICS_TOKEN not in repr(loaded)

    for bad in ("too-short", "has whitespace " + "x" * 40):
        with pytest.raises(ConfigError, match="MEMINI_METRICS_TOKEN") as raised:
            load_settings({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_METRICS_TOKEN": bad})
        assert bad not in str(raised.value)
    config = tmp_path / "memini.toml"
    config.write_text(f'metrics_token = "{METRICS_TOKEN}"\n')
    with pytest.raises(ConfigError, match=r"metrics_token.*secret"):
        load_settings({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_CONFIG_FILE": str(config)})


def test_serve_masks_the_metrics_token_in_logs(log_output: io.StringIO) -> None:
    configure_logging(settings_with(metrics_token=SecretStr(METRICS_TOKEN)))

    assert logs.redact(f"scraped with {METRICS_TOKEN}") == f"scraped with {logs.REDACTED}"


# --- Labels ---------------------------------------------------------------------------------------


def test_requests_are_labelled_by_route_template_never_by_path_or_query(
    client: TestClient, authenticator: FakeAuthenticator
) -> None:
    token = authenticator.issue("read")
    template = "/v1/workspaces/{workspace}/sources"
    before = HTTP_REQUESTS.value(route=template, method="GET", status="503")
    unmatched = HTTP_REQUESTS.value(route=UNMATCHED, method="GET", status="404")
    other = HTTP_REQUESTS.value(route="/healthz", method="other", status="405")

    client.get(f"/v1/workspaces/{PRIVATE_NAME}/sources?q={PRIVATE_QUERY}", headers=bearer(token))
    client.get(f"/{PRIVATE_NAME}/nowhere")
    client.request("PROPFIND", "/healthz")

    text = scrape(client)
    assert HTTP_REQUESTS.value(route=template, method="GET", status="503") == before + 1
    assert HTTP_REQUESTS.value(route=UNMATCHED, method="GET", status="404") == unmatched + 1
    assert HTTP_REQUESTS.value(route="/healthz", method="other", status="405") == other + 1
    assert PRIVATE_NAME not in text
    assert PRIVATE_QUERY not in text
    assert token not in text
    for (_name, labels), _ in samples(text).items():
        for label, label_value in labels:
            if label == "route":
                assert label_value == UNMATCHED or label_value.startswith("/"), label_value
                assert "synthetic" not in label_value


def test_a_refusal_by_the_rate_limits_is_counted_with_its_route_and_budget(
    authenticator: FakeAuthenticator,
) -> None:
    app = create_app(
        settings_with(metrics_token=SecretStr(METRICS_TOKEN), http_rate_limit_unauthenticated=1),
        authenticator=authenticator,
    )
    template = "/v1/workspaces/{workspace}"
    refusals = RATE_LIMIT_REFUSALS.value(budget="unauthenticated")
    answered = HTTP_REQUESTS.value(route=template, method="GET", status="429")
    with TestClient(app) as client:
        assert client.get(f"/v1/workspaces/{PRIVATE_NAME}").status_code == 401
        assert client.get(f"/v1/workspaces/{PRIVATE_NAME}").status_code == 429
        text = scrape(client)  # /metrics is not rate-limited

    assert RATE_LIMIT_REFUSALS.value(budget="unauthenticated") == refusals + 1
    assert HTTP_REQUESTS.value(route=template, method="GET", status="429") == answered + 1
    assert value(text, "memini_rate_limit_refusals_total", budget="unauthenticated") >= 1
    assert PRIVATE_NAME not in text


# --- MCP tool calls -------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_client(app: FastAPI, token: str) -> AsyncGenerator[Client]:
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=bearer(token)
        ) as http,
    ):
        transport = streamable_http_client(f"{BASE_URL}{MCP_PATH}", http_client=http)
        async with Client(transport) as mcp:
            yield mcp


@pytest.mark.anyio
async def test_mcp_tool_calls_are_counted_by_tool_and_outcome(
    authenticator: FakeAuthenticator,
) -> None:
    app = create_app(settings_with(), authenticator=authenticator)
    ok = MCP_TOOL_CALLS.value(tool="status", outcome="ok")
    error = MCP_TOOL_CALLS.value(tool="status", outcome="error")
    unknown = MCP_TOOL_CALLS.value(tool="unknown", outcome="refused")
    write = MCP_TOOL_CALLS.value(tool="record_decision", outcome="refused")

    async with mcp_client(app, authenticator.issue("read")) as mcp:
        assert (await mcp.call_tool("status", {})).is_error is False
        assert (await mcp.call_tool("status", {"workspace": PRIVATE_NAME})).is_error is True
        with pytest.raises(MCPError):
            await mcp.call_tool(PRIVATE_NAME, {})
        with pytest.raises(MCPError):
            await mcp.call_tool("record_decision", {})  # needs the ingest scope

    assert MCP_TOOL_CALLS.value(tool="status", outcome="ok") == ok + 1
    assert MCP_TOOL_CALLS.value(tool="status", outcome="error") == error + 1
    assert MCP_TOOL_CALLS.value(tool="unknown", outcome="refused") == unknown + 1
    assert MCP_TOOL_CALLS.value(tool="record_decision", outcome="refused") == write + 1
    assert MCP_TOOL_CALLS.value(tool=PRIVATE_NAME, outcome="refused") == 0
