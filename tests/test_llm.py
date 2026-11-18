"""memini.llm without a database: the policy decision, the endpoint against a fake server
(httpx2.MockTransport), and the checks that nothing reaches an endpoint but through the gate.

The DB-backed acceptance matrix is in test_llm_db.py. Every key and URL here is an obvious fake.
"""

import ast
import io
import itertools
import json
import random
import traceback
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import anyio
import httpx2
import pytest
from pydantic import SecretStr

import memini
import memini.llm
from llm_helpers import (
    CLOUD_KEY,
    CLOUD_MODEL,
    CLOUD_URL,
    LOCAL_KEY,
    LOCAL_MODEL,
    LOCAL_URL,
    FakeChatServer,
    Sleeps,
    Verdict,
    chat_answer,
    llm_settings,
)
from memini import sources
from memini.api import create_app
from memini.config import LlmStructuredOutput
from memini.llm import (
    LLM_POLICIES,
    ChatRequest,
    Grant,
    LlmError,
    LlmGate,
    LlmOutputError,
    LlmPolicy,
    LlmRefused,
    Message,
    ProviderClass,
    _endpoint,
    configured_providers,
    create_llm_gate,
    decide,
    strictest,
)
from memini.llm._endpoint import (
    _GATE_KEY,  # pyright: ignore[reportPrivateUsage] - tests of the endpoint itself
    ChatEndpoint,
    parse_output,
)
from memini.logs import REDACTED, clear_secrets, redact

pytestmark = pytest.mark.anyio

WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000001")
SOURCE = uuid.UUID("01890000-0000-7000-8000-000000000002")
CANARY = "canary-private-content-4b1d"
ASK = ChatRequest(messages=[Message("user", "Was it decided?")], output=Verdict)


@pytest.fixture
def server() -> FakeChatServer:
    return FakeChatServer()


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Endpoints register their keys as secrets; forget them after each test."""
    yield
    clear_secrets()


def endpoint(
    server: FakeChatServer,
    provider_class: ProviderClass = "local",
    *,
    sleeps: Sleeps | None = None,
    structured_output: LlmStructuredOutput = "json_schema",
    timeout: float = 30.0,
    max_attempts: int = 4,
) -> ChatEndpoint:
    local = provider_class == "local"
    return ChatEndpoint(
        _GATE_KEY,
        provider_class,
        url=LOCAL_URL if local else CLOUD_URL,
        model=LOCAL_MODEL if local else CLOUD_MODEL,
        api_key=SecretStr(LOCAL_KEY if local else CLOUD_KEY),
        structured_output=structured_output,
        timeout=timeout,
        max_attempts=max_attempts,
        transport=server.transport(),
        sleep=sleeps or Sleeps(),
        rng=random.Random(7),  # noqa: S311 - jitter, not cryptography
    )


def grant_for(target: ChatEndpoint, policy: LlmPolicy = "local") -> Grant:
    assert policy != "none"
    return Grant(
        workspace_id=WORKSPACE,
        source_ids=frozenset({SOURCE}),
        policy=policy,
        provider_class=target.provider_class,
        model=target.model,
        target=target.target,
    )


# --- The decision ---------------------------------------------------------------------------------

PROVIDER_SETS: list[frozenset[ProviderClass]] = [
    frozenset(),
    frozenset({"local"}),
    frozenset({"cloud"}),
    frozenset({"local", "cloud"}),
]

# What a request with content of one source gets: refused (the reason) or the provider class, for
# the provider sets of PROVIDER_SETS in order. Written out, not computed: this is the rule.
NONE4 = ("policy_none", "policy_none", "policy_none", "policy_none")
EXPECTED: dict[tuple[LlmPolicy, LlmPolicy], tuple[str, str, str, str]] = {
    ("none", "none"): NONE4,
    ("none", "local"): NONE4,
    ("none", "cloud"): NONE4,
    ("local", "none"): NONE4,
    ("local", "local"): ("no_provider", "local", "no_provider", "local"),
    ("local", "cloud"): ("no_provider", "local", "no_provider", "local"),
    ("cloud", "none"): NONE4,
    ("cloud", "local"): ("no_provider", "local", "no_provider", "local"),
    ("cloud", "cloud"): ("no_provider", "local", "cloud", "cloud"),
}


def outcome(
    workspace: str, sources_: list[tuple[str, str]], configured: frozenset[ProviderClass]
) -> str:
    try:
        _, provider_class = decide(("workspace w", workspace), sources_, configured)
    except LlmRefused as refused:
        return refused.reason
    return provider_class


@pytest.mark.parametrize(("workspace", "source"), list(EXPECTED))
def test_one_source_gets_what_the_rule_table_says(workspace: LlmPolicy, source: LlmPolicy) -> None:
    got = tuple(
        outcome(workspace, [("source s", source)], configured) for configured in PROVIDER_SETS
    )

    assert got == EXPECTED[(workspace, source)]


def test_cloud_only_when_the_workspace_and_every_source_allow_it() -> None:
    """Every workspace policy, every mix of up to three sources, every set of providers."""
    checked = 0
    for workspace in LLM_POLICIES:
        for size in (1, 2, 3):
            for mix in itertools.product(LLM_POLICIES, repeat=size):
                # One label for every source: two sources never merge into one.
                policies = [("source s", policy) for policy in mix]
                for configured in PROVIDER_SETS:
                    got = outcome(workspace, policies, configured)
                    checked += 1
                    if got == "cloud":
                        assert workspace == "cloud"
                        assert set(mix) == {"cloud"}
                    if "none" in mix or workspace == "none":
                        assert got == "policy_none"
                    elif got in ("local", "cloud"):
                        assert got in configured
    assert checked == 3 * (3 + 9 + 27) * 4


def test_mixed_sources_take_the_strictest() -> None:
    both: frozenset[ProviderClass] = frozenset({"local", "cloud"})

    assert decide(
        ("workspace w", "cloud"), [("source a", "cloud"), ("source b", "cloud")], both
    ) == (
        "cloud",
        "cloud",
    )
    assert decide(
        ("workspace w", "cloud"), [("source a", "cloud"), ("source b", "local")], both
    ) == (
        "local",
        "local",
    )
    with pytest.raises(LlmRefused, match="source b: llm_policy none") as info:
        decide(("workspace w", "cloud"), [("source a", "cloud"), ("source b", "none")], both)
    assert info.value.reason == "policy_none"


def test_cloud_content_falls_back_to_the_local_provider() -> None:
    assert decide(("workspace w", "cloud"), [("source a", "cloud")], {"local"}) == (
        "cloud",
        "local",
    )


def test_a_refusal_names_who_limited_the_content() -> None:
    with pytest.raises(LlmRefused) as info:
        decide(("workspace alpha", "local"), [("source notes", "cloud")], {"cloud"})

    assert info.value.reason == "no_provider"
    message = str(info.value)
    assert "workspace alpha: llm_policy local" in message
    assert "MEMINI_LLM_LOCAL_URL" in message
    assert "the cloud provider may not read it" in message


@pytest.mark.parametrize("bad", ["remote", "", "Cloud", "LOCAL"])
def test_an_unknown_policy_is_refused(bad: str) -> None:
    with pytest.raises(LlmRefused) as on_source:
        decide(("workspace w", "cloud"), [("source a", bad)], {"local", "cloud"})
    with pytest.raises(LlmRefused) as on_workspace:
        decide(("workspace w", bad), [("source a", "cloud")], {"local", "cloud"})

    assert on_source.value.reason == on_workspace.value.reason == "unknown_policy"


def test_content_of_no_source_is_refused() -> None:
    with pytest.raises(LlmRefused) as info:
        decide(("workspace w", "cloud"), [], {"local", "cloud"})

    assert info.value.reason == "no_sources"


def test_strictest_orders_none_local_cloud() -> None:
    assert strictest(["cloud", "local", "cloud"]) == "local"
    assert strictest(["cloud", "none"]) == "none"
    assert strictest(["cloud"]) == "cloud"
    with pytest.raises(LlmRefused):
        strictest([])


def test_the_policies_are_the_ones_the_sources_have() -> None:
    assert get_args(LlmPolicy) == get_args(sources.LlmPolicy) == LLM_POLICIES


# --- The endpoint: requests --------------------------------------------------------------------


async def test_json_schema_mode_sends_the_schema_in_response_format(server: FakeChatServer) -> None:
    target = endpoint(server)
    request = ChatRequest(
        messages=[Message("system", "You extract decisions."), Message("user", "Thread text")],
        output=Verdict,
        temperature=0.2,
        max_tokens=300,
        output_name="verdict",
    )

    completion = await target.complete(grant_for(target), request)
    await target.aclose()

    (sent,) = server.requests
    assert str(sent.url) == f"{LOCAL_URL}/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {LOCAL_KEY}"
    assert json.loads(sent.content) == {
        "model": LOCAL_MODEL,
        "temperature": 0.2,
        "max_tokens": 300,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": Verdict.model_json_schema()},
        },
        "messages": [
            {"role": "system", "content": "You extract decisions."},
            {"role": "user", "content": "Thread text"},
        ],
    }
    assert completion.output == Verdict(answer="yes", confidence=0.75)
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (11, 7)
    assert completion.grant.provider_class == "local"


async def test_json_object_mode_puts_the_schema_into_the_system_message(
    server: FakeChatServer,
) -> None:
    target = endpoint(server, structured_output="json_object")
    request = ChatRequest(
        messages=[Message("system", "You extract decisions."), Message("user", "Thread text")],
        output=Verdict,
    )

    await target.complete(grant_for(target), request)

    body = server.bodies()[0]
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith("You extract decisions.\n\nReply with one JSON object")
    assert json.dumps(Verdict.model_json_schema(), sort_keys=True) in system["content"]
    assert user == {"role": "user", "content": "Thread text"}


async def test_none_mode_sends_no_response_format_and_adds_a_system_message(
    server: FakeChatServer,
) -> None:
    target = endpoint(server, structured_output="none")

    await target.complete(grant_for(target), ASK)

    body = server.bodies()[0]
    assert "response_format" not in body
    system, user = body["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith("Reply with one JSON object and nothing else.")
    assert user == {"role": "user", "content": "Was it decided?"}


async def test_without_a_key_no_authorization_header_is_sent(server: FakeChatServer) -> None:
    gate = create_llm_gate(
        llm_settings(cloud=False, llm_local_api_key=None), transport=server.transport()
    )
    target = gate._endpoints["local"]  # pyright: ignore[reportPrivateUsage]

    await target.complete(grant_for(target), ASK)

    assert "authorization" not in server.requests[0].headers


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"messages": []}, "at least one message"),
        ({"temperature": 2.5}, "temperature"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"output_name": "has space"}, "output_name"),
    ],
)
def test_a_bad_request_is_refused(kwargs: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {"messages": [Message("user", "hi")], "output": Verdict} | kwargs
    with pytest.raises(ValueError, match=message):
        ChatRequest(**values)


# --- The endpoint: structured output -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"answer": "yes"}',
        '  \n{"answer": "yes"}\n',
        'Here you are:\n```json\n{"answer": "yes"}\n```\nAnything else?',
        '```\n{"answer": "yes"}\n```',
        '<think>The user wants {"answer": "no"}? Let me see.</think>{"answer": "yes"}',
        'Sure. {"answer": "yes"} Hope that helps.',
    ],
)
def test_a_reply_is_read_with_or_without_wrapping(content: str) -> None:
    assert parse_output(content, Verdict, what="model m") == Verdict(answer="yes")


def test_a_reply_that_is_not_json_is_an_output_error() -> None:
    with pytest.raises(LlmOutputError, match="the reply of model m is not JSON"):
        parse_output(f"I think {CANARY} was decided.", Verdict, what="model m")


def test_a_reply_that_breaks_the_schema_names_fields_never_values() -> None:
    content = json.dumps({"answer": ["a", CANARY], "confidence": CANARY})

    with pytest.raises(LlmOutputError) as info:
        parse_output(content, Verdict, what="model m")

    message = str(info.value)
    assert "does not match the schema Verdict" in message
    assert "answer: string_type" in message
    assert "confidence: float_parsing" in message
    assert CANARY not in message
    assert info.value.__cause__ is None


async def test_a_reply_cut_at_max_tokens_is_an_output_error(server: FakeChatServer) -> None:
    server.script.append(lambda request: chat_answer('{"answer": "y', finish_reason="length"))
    target = endpoint(server)

    with pytest.raises(LlmOutputError, match=r"cut at max_tokens \(1024\)"):
        await target.complete(grant_for(target), ASK)


async def test_a_refusal_of_the_model_is_an_output_error(server: FakeChatServer) -> None:
    server.script.append(lambda request: chat_answer(None, refusal="I can't help with that."))
    target = endpoint(server)

    with pytest.raises(LlmOutputError, match="refused to answer"):
        await target.complete(grant_for(target), ASK)


@pytest.mark.parametrize("body", [{"choices": []}, {"data": []}, "not json at all"])
async def test_a_malformed_answer_is_an_error(server: FakeChatServer, body: object) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if isinstance(body, str):
            return httpx2.Response(200, text=body)
        return httpx2.Response(200, json=body)

    server.script.append(respond)
    target = endpoint(server)

    with pytest.raises(LlmError, match="not an OpenAI chat completion"):
        await target.complete(grant_for(target), ASK)


async def test_usage_goes_to_the_current_run_even_when_the_reply_fails(
    server: FakeChatServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[str, int, int]] = []

    class Recorder:
        def llm_tokens(self, *, model: str, input: int, output: int) -> None:
            recorded.append((model, input, output))

    monkeypatch.setattr(_endpoint, "current_run", Recorder)
    server.script.append(lambda request: chat_answer("not json", usage=(40, 3)))
    server.script.append(lambda request: chat_answer('{"answer": "no"}', usage=None))
    target = endpoint(server, "cloud")

    with pytest.raises(LlmOutputError):
        await target.complete(grant_for(target, "cloud"), ASK)
    await target.complete(grant_for(target, "cloud"), ASK)

    assert recorded == [(f"cloud/{CLOUD_MODEL}", 40, 3), (f"cloud/{CLOUD_MODEL}", 0, 0)]


# --- The endpoint: failures and retries --------------------------------------------------------


def status(code: int, body: object = None, headers: dict[str, str] | None = None) -> Any:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(code, json=body, headers=headers)

    return respond


async def test_server_errors_are_retried_with_growing_waits(server: FakeChatServer) -> None:
    server.script.extend([status(503), status(502), status(500)])
    sleeps = Sleeps()
    target = endpoint(server, sleeps=sleeps)

    completion = await target.complete(grant_for(target), ASK)

    assert completion.output.answer == "yes"
    assert len(server.requests) == 4
    assert len(sleeps.delays) == 3
    assert sleeps.delays[0] < sleeps.delays[1] < sleeps.delays[2]


async def test_retry_after_is_honoured_and_capped(server: FakeChatServer) -> None:
    server.script.extend(
        [status(429, headers={"Retry-After": "2"}), status(429, headers={"Retry-After": "999"})]
    )
    sleeps = Sleeps()
    target = endpoint(server, sleeps=sleeps)

    await target.complete(grant_for(target), ASK)

    assert sleeps.delays == [2.0, 30.0]


async def test_connection_errors_are_retried(server: FakeChatServer) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic connection refused", request=request)

    server.script.append(refuse)
    target = endpoint(server)

    assert (await target.complete(grant_for(target), ASK)).output.answer == "yes"
    assert len(server.requests) == 2


async def test_an_attempt_that_takes_too_long_times_out_and_is_retried(
    server: FakeChatServer,
) -> None:
    async def hang(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(5)
        return chat_answer('{"answer": "late"}')

    server.script.extend([hang, hang])
    target = endpoint(server, timeout=0.05, max_attempts=2)

    with pytest.raises(LlmError) as info:
        await target.complete(grant_for(target), ASK)

    assert str(info.value) == (
        "the local LLM endpoint at http://llm-local.test:11434 failed 2 times;"
        " last: no answer within 0.05 s"
    )


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (401, "check MEMINI_LLM_LOCAL_API_KEY"),
        (403, "check MEMINI_LLM_LOCAL_API_KEY"),
        (404, "check MEMINI_LLM_LOCAL_URL"),
        (400, "set MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT to json_object or none"),
    ],
)
async def test_a_refusal_is_not_retried(server: FakeChatServer, code: int, hint: str) -> None:
    server.script.append(status(code, {"error": {"message": "synthetic refusal"}}))
    target = endpoint(server)

    with pytest.raises(LlmError, match=f"HTTP {code}") as info:
        await target.complete(grant_for(target), ASK)

    assert hint in str(info.value)
    assert len(server.requests) == 1


async def test_the_keys_never_reach_an_error_or_a_log_line(
    server: FakeChatServer, log_output: io.StringIO
) -> None:
    """A server that echoes the key in its error, and transport errors that carry it."""
    echo = {"error": f"invalid api key {LOCAL_KEY} / Bearer {CLOUD_KEY}"}

    def leak(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"cannot connect with {LOCAL_KEY}", request=request)

    server.script.extend([leak, status(503), status(422, echo)])
    gate = create_llm_gate(llm_settings(), transport=server.transport(), sleep=Sleeps())
    target = gate._endpoints["local"]  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(LlmError) as info:
        await target.complete(grant_for(target), ASK)
    await gate.aclose()

    text = str(info.value) + "".join(traceback.format_exception(info.value))
    logged = log_output.getvalue()
    assert REDACTED in str(info.value)
    assert "invalid api key" in str(info.value)
    assert logged, "nothing was logged; the check would prove nothing"
    for key in (LOCAL_KEY, CLOUD_KEY):
        assert key not in text
        assert key not in logged
        assert key not in repr(gate.providers)
        assert redact(f"sent {key}") == f"sent {REDACTED}"


# --- The endpoint: construction and the grant ----------------------------------------------------


def test_an_endpoint_is_created_with_the_gates_key_only() -> None:
    with pytest.raises(TypeError, match="created by the gate only"):
        ChatEndpoint(object(), "local", url=LOCAL_URL, model=LOCAL_MODEL)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("provider_class", "url", "message"),
    [
        ("cloud", "http://llm-cloud.test/v1", "https://"),
        ("local", "ftp://llm-local.test/v1", "http:// or https://"),
        ("local", "http://user:secret-pass@llm-local.test/v1", "must not hold credentials"),
        ("cloud", "https://user:secret-pass@llm-cloud.test/v1", "must not hold credentials"),
    ],
)
def test_a_bad_url_is_refused(provider_class: ProviderClass, url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as info:
        ChatEndpoint(_GATE_KEY, provider_class, url=url, model="m")

    assert "secret-pass" not in str(info.value)


async def test_a_cloud_endpoint_takes_only_a_cloud_grant(server: FakeChatServer) -> None:
    target = endpoint(server, "cloud")

    with pytest.raises(LlmRefused) as info:
        await target.complete(grant_for(target, "local"), ASK)

    assert info.value.reason == "grant_mismatch"
    assert server.requests == []


async def test_a_local_endpoint_takes_a_local_or_cloud_grant(server: FakeChatServer) -> None:
    target = endpoint(server, "local")

    await target.complete(grant_for(target, "local"), ASK)
    await target.complete(grant_for(target, "cloud"), ASK)

    assert len(server.requests) == 2


async def test_a_grant_for_another_endpoint_is_refused(server: FakeChatServer) -> None:
    local, cloud = endpoint(server, "local"), endpoint(server, "cloud")
    forged = Grant(
        workspace_id=WORKSPACE,
        source_ids=frozenset({SOURCE}),
        policy="cloud",
        provider_class="cloud",
        model=CLOUD_MODEL,
        target=local.target,
    )

    with pytest.raises(LlmRefused, match="not for the cloud LLM endpoint"):
        await cloud.complete(forged, ASK)

    assert server.requests == []


# --- The gate without a database ------------------------------------------------------------------


def test_no_provider_configured_gives_a_gate_with_none() -> None:
    gate = create_llm_gate(llm_settings(local=False, cloud=False))

    assert gate.providers == {}


def test_providers_are_described_without_key_or_path() -> None:
    settings = llm_settings()

    described = {c: info.describe() for c, info in configured_providers(settings).items()}

    assert described == {
        "local": f"model {LOCAL_MODEL} at http://llm-local.test:11434",
        "cloud": f"model {CLOUD_MODEL} at https://llm-cloud.test",
    }
    gate = create_llm_gate(settings)
    assert gate.providers == configured_providers(settings)


def test_a_url_without_its_model_is_refused() -> None:
    with pytest.raises(LlmError, match="MEMINI_LLM_CLOUD_MODEL"):
        create_llm_gate(llm_settings(llm_cloud_model=None))


def test_a_cloud_endpoint_filed_as_local_is_refused(server: FakeChatServer) -> None:
    cloud = endpoint(server, "cloud")

    with pytest.raises(ValueError, match="not its own"):
        LlmGate({"local": cloud})


async def test_one_source_id_alone_is_refused(server: FakeChatServer) -> None:
    gate = create_llm_gate(llm_settings(), transport=server.transport())

    with pytest.raises(TypeError, match="collection of source ids"):
        await gate.check(None, workspace_id=WORKSPACE, source_ids=SOURCE)  # pyright: ignore[reportArgumentType]
    with pytest.raises(LlmRefused) as info:
        await gate.check(None, workspace_id=WORKSPACE, source_ids=[])  # pyright: ignore[reportArgumentType]

    assert info.value.reason == "no_sources"
    assert server.requests == []


# --- Nothing reaches an endpoint but through the gate --------------------------------------------

SRC = Path(memini.__file__).parent
LLM = SRC / "llm"
REPOSITORY = SRC.parent.parent


def service_modules() -> list[Path]:
    """Every module of the service and of its scripts: the code that runs in production."""
    return sorted([*SRC.rglob("*.py"), *(REPOSITORY / "scripts").rglob("*.py")])


def imported(tree: ast.Module, path: Path) -> set[str]:
    """Every module and module member ``path`` imports, as dotted names, relative ones resolved."""
    # The package a relative import starts from: the directory of the module, as a dotted name.
    inside = path.is_relative_to(SRC.parent)
    package = list(path.relative_to(SRC.parent).parts[:-1]) if inside else []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package[: len(package) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def strings(tree: ast.Module) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def identifiers(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name)
            found.add(node.name.rsplit(".", 1)[-1])
    return found


def test_only_the_gate_reaches_an_endpoint() -> None:
    """The bypass check, like the spy on the authorization filter: no module of the service
    imports the private endpoint module, holds its key, names a chat endpoint or reads the LLM
    settings, except the gate (and the endpoint itself)."""
    may_import = {LLM / "gate.py"}
    may_hold_key = {LLM / "gate.py", LLM / "_endpoint.py"}
    may_name_path = {LLM / "_endpoint.py"}
    may_read_settings = {SRC / "config.py", LLM / "gate.py"}
    offences: list[str] = []
    importers: set[Path] = set()
    for path in service_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = str(path.relative_to(REPOSITORY))
        private = {name for name in imported(tree, path) if name.startswith("memini.llm._")}
        if private:
            importers.add(path)
            if path not in may_import:
                offences.append(f"{where} imports {sorted(private)}")
        if "_GATE_KEY" in identifiers(tree) and path not in may_hold_key:
            offences.append(f"{where} uses the key of the gate")
        for text in strings(tree):
            if "/completions" in text and path not in may_name_path:
                offences.append(f"{where} names a completions endpoint: {text[:60]!r}")
            if "memini.llm._" in text and path.parent != LLM:
                offences.append(f"{where} names a private module of memini.llm: {text[:60]!r}")
        settings = {
            name
            for name in identifiers(tree)
            if name.startswith(("llm_local_", "llm_cloud_")) or name == "llm_timeout"
        }
        if settings and path not in may_read_settings:
            offences.append(f"{where} reads the LLM settings {sorted(settings)}")

    assert offences == []
    assert importers == may_import, "the scan found no import of the endpoint; it proves nothing"


def test_the_package_exports_no_endpoint() -> None:
    exported = [getattr(memini.llm, name) for name in memini.llm.__all__]

    assert ChatEndpoint not in exported
    assert all(not name.startswith("_") for name in memini.llm.__all__)
    public = sorted(name for name in dir(LlmGate) if not name.startswith("_"))
    assert public == ["aclose", "check", "complete", "providers"]


def test_no_http_route_changes_a_workspaces_policy() -> None:
    """The opt-in to cloud is the operator's, through the CLI: no token can make it. Only source
    routes take an llm_policy (a source can narrow the workspace's, never widen it)."""
    app = create_app(llm_settings())
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    takers: list[str] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if body is None:
                continue
            ref: str = body["schema"].get("$ref", "")
            properties: dict[str, Any] = (
                schemas[ref.rsplit("/", 1)[-1]]["properties"] if ref else {}
            )
            if "llm_policy" in properties:
                takers.append(f"{method.upper()} {path}")

    assert takers
    assert all("/sources" in taker for taker in takers), takers
    assert sorted(spec["paths"]["/v1/workspaces/{workspace}"]) == ["get"]
