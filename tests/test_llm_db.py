"""The LLM policy gate against a real Postgres: run with `make test-integration`.

The acceptance check of the gate: content of a source whose llm_policy is local or none never
reaches a cloud endpoint, whatever the workspace's setting and whichever providers are
configured. The providers are a fake chat server on two hosts (tests/llm_helpers.py) that records
which host received what. Names, keys and content are synthetic.
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from psycopg import AsyncConnection, errors
from psycopg.rows import TupleRow

from llm_helpers import (
    CLOUD_HOST,
    CLOUD_MODEL,
    LOCAL_HOST,
    LOCAL_MODEL,
    FakeChatServer,
    Sleeps,
    Verdict,
    llm_settings,
)
from memini.auth import WriteAccess
from memini.db import connect
from memini.llm import (
    ChatRequest,
    LlmGate,
    LlmPolicy,
    LlmRefused,
    Message,
    create_llm_gate,
    get_workspace_llm_policy,
    set_workspace_llm_policy,
)
from memini.logs import clear_secrets
from memini.runs import record_run
from memini.workspaces import NotFoundError, create_workspace, ensure_principal

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

type Conn = AsyncConnection[TupleRow]

HOST: dict[str, str] = {"local": LOCAL_HOST, "cloud": CLOUD_HOST}
"""The host of each provider class in llm_settings()."""


@dataclass(frozen=True)
class World:
    owner: uuid.UUID
    alpha: uuid.UUID
    beta: uuid.UUID
    sources: dict[str, uuid.UUID]
    """By name: in alpha ``none``, ``local``, ``cloud`` and ``cloud2`` (each with that policy);
    in beta ``beta-cloud``."""


@pytest.fixture(autouse=True)
def _forget_secrets() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    clear_secrets()


@pytest.fixture
async def db(migrated_database_url: str) -> AsyncIterator[Conn]:
    async with connect(migrated_database_url, autocommit=True) as conn:
        yield conn


async def add_source(
    conn: Conn, workspace: uuid.UUID, owner: uuid.UUID, name: str, policy: str
) -> uuid.UUID:
    cur = await conn.execute(
        "INSERT INTO source (workspace_id, owner_id, name, kind, llm_policy)"
        " VALUES (%s, %s, %s, 'push', %s) RETURNING id",
        (workspace, owner, name, policy),
    )
    row = await cur.fetchone()
    assert row is not None
    source_id: uuid.UUID = row[0]
    return source_id


@pytest.fixture
async def world(db: Conn) -> World:
    owner = await ensure_principal(db, "owner")
    alpha = (await create_workspace(db, owner_id=owner, name="alpha")).id
    beta = (await create_workspace(db, owner_id=owner, name="beta")).id
    found = {
        name: await add_source(db, alpha, owner, name, policy)
        for name, policy in (
            ("none", "none"),
            ("local", "local"),
            ("cloud", "cloud"),
            ("cloud2", "cloud"),
        )
    }
    found["beta-cloud"] = await add_source(db, beta, owner, "beta-cloud", "cloud")
    await set_workspace_llm_policy(db, WriteAccess.for_owner(owner), "beta", "cloud")
    return World(owner=owner, alpha=alpha, beta=beta, sources=found)


def canary(source: str) -> str:
    return f"private-content-of-{source}-7e1f"


def request_with(*source_names: str) -> ChatRequest[Verdict]:
    """A request whose content carries a canary of each source it came from."""
    text = "\n".join(f"message: {canary(name)}" for name in source_names)
    return ChatRequest(
        messages=[Message("system", "Extract the decision."), Message("user", text)],
        output=Verdict,
    )


async def ask(gate: LlmGate, db: Conn, workspace: uuid.UUID, world: World, *names: str) -> str:
    """The provider class that answered, or the reason of the refusal."""
    try:
        completion = await gate.complete(
            db,
            workspace_id=workspace,
            source_ids=[world.sources[name] for name in names],
            request=request_with(*names),
        )
    except LlmRefused as refused:
        return refused.reason
    assert completion.output == Verdict(answer="yes", confidence=0.75)
    return completion.grant.provider_class


# --- The acceptance matrix -----------------------------------------------------------------------

PROVIDERS: dict[str, tuple[bool, bool]] = {
    "no provider": (False, False),
    "local only": (True, False),
    "cloud only": (False, True),
    "local and cloud": (True, True),
}

# Workspace policy, configured providers -> for the source of each policy: the provider class
# that read it, or the reason the gate refused. Written out: this is the rule.
NO_LLM = {"none": "policy_none", "local": "policy_none", "cloud": "policy_none"}
MATRIX: dict[tuple[LlmPolicy, str], dict[str, str]] = {
    ("none", "no provider"): NO_LLM,
    ("none", "local only"): NO_LLM,
    ("none", "cloud only"): NO_LLM,
    ("none", "local and cloud"): NO_LLM,
    ("local", "no provider"): {
        "none": "policy_none",
        "local": "no_provider",
        "cloud": "no_provider",
    },
    ("local", "local only"): {"none": "policy_none", "local": "local", "cloud": "local"},
    ("local", "cloud only"): {
        "none": "policy_none",
        "local": "no_provider",
        "cloud": "no_provider",
    },
    ("local", "local and cloud"): {"none": "policy_none", "local": "local", "cloud": "local"},
    ("cloud", "no provider"): {
        "none": "policy_none",
        "local": "no_provider",
        "cloud": "no_provider",
    },
    ("cloud", "local only"): {"none": "policy_none", "local": "local", "cloud": "local"},
    ("cloud", "cloud only"): {"none": "policy_none", "local": "no_provider", "cloud": "cloud"},
    ("cloud", "local and cloud"): {"none": "policy_none", "local": "local", "cloud": "cloud"},
}


@pytest.mark.parametrize(("workspace_policy", "providers"), list(MATRIX))
async def test_local_and_none_content_never_reaches_a_cloud_endpoint(
    db: Conn, world: World, workspace_policy: LlmPolicy, providers: str
) -> None:
    """Acceptance: policy none/local/cloud x workspace none/local/cloud (cloud being the opt-in)
    x providers configured. A fake server on both hosts records where content went."""
    await set_workspace_llm_policy(
        db, WriteAccess.for_owner(world.owner), "alpha", workspace_policy
    )
    local, cloud = PROVIDERS[providers]
    server = FakeChatServer()
    settings = llm_settings(local=local, cloud=cloud)

    got: dict[str, str] = {}
    async with create_llm_gate(settings, transport=server.transport(), sleep=Sleeps()) as gate:
        for policy in ("none", "local", "cloud"):
            sent_before = len(server.requests)
            got[policy] = await ask(gate, db, world.alpha, world, policy)
            sent = server.hosts()[sent_before:]
            if got[policy] in HOST:
                assert sent == [HOST[got[policy]]]
            else:
                assert sent == [], "a refused request sent something"
            # check() decides as complete() did, and sends nothing.
            try:
                grant = await gate.check(
                    db, workspace_id=world.alpha, source_ids=[world.sources[policy]]
                )
                checked = grant.provider_class
            except LlmRefused as refused:
                checked = refused.reason
            assert checked == got[policy]
            assert len(server.requests) == sent_before + len(sent)

    assert got == MATRIX[(workspace_policy, providers)]
    # The acceptance check itself, on what the cloud host received.
    for request in server.requests:
        if request.url.host == CLOUD_HOST:
            body = request.content.decode()
            assert canary("none") not in body
            assert canary("local") not in body
            assert canary("cloud") in body


async def test_mixed_content_takes_the_strictest_policy(db: Conn, world: World) -> None:
    await set_workspace_llm_policy(db, WriteAccess.for_owner(world.owner), "alpha", "cloud")
    server = FakeChatServer()

    async with create_llm_gate(llm_settings(), transport=server.transport()) as gate:
        assert await ask(gate, db, world.alpha, world, "cloud", "cloud2") == "cloud"
        assert await ask(gate, db, world.alpha, world, "cloud", "local") == "local"
        assert await ask(gate, db, world.alpha, world, "cloud", "cloud2", "local") == "local"
        assert await ask(gate, db, world.alpha, world, "cloud", "none") == "policy_none"
        assert await ask(gate, db, world.alpha, world, "local", "none") == "policy_none"

    assert server.hosts() == [CLOUD_HOST, LOCAL_HOST, LOCAL_HOST]
    assert canary("local") not in server.requests[0].content.decode()
    assert canary("local") in server.requests[1].content.decode()


async def test_content_of_unknown_origin_is_refused(db: Conn, world: World) -> None:
    await set_workspace_llm_policy(db, WriteAccess.for_owner(world.owner), "alpha", "cloud")
    server = FakeChatServer()
    request = request_with("cloud")

    async def reason(workspace: uuid.UUID, *source_ids: uuid.UUID) -> str:
        with pytest.raises(LlmRefused) as info:
            await gate.complete(db, workspace_id=workspace, source_ids=source_ids, request=request)
        return info.value.reason

    async with create_llm_gate(llm_settings(), transport=server.transport()) as gate:
        cloud, other = world.sources["cloud"], world.sources["beta-cloud"]
        # A source of another workspace, even one that opted in, does not count as in this one.
        assert await reason(world.alpha, cloud, other) == "unknown_source"
        assert await reason(world.alpha, other) == "unknown_source"
        assert await reason(world.alpha, cloud, uuid.uuid4()) == "unknown_source"
        assert await reason(uuid.uuid4(), cloud) == "unknown_workspace"
        assert await reason(world.alpha) == "no_sources"

    assert server.requests == []


async def test_a_change_of_policy_takes_effect_at_the_next_call(db: Conn, world: World) -> None:
    access = WriteAccess.for_owner(world.owner)
    await set_workspace_llm_policy(db, access, "alpha", "cloud")
    server = FakeChatServer()

    async with create_llm_gate(llm_settings(), transport=server.transport()) as gate:
        assert await ask(gate, db, world.alpha, world, "cloud") == "cloud"
        await db.execute(
            "UPDATE source SET llm_policy = 'local' WHERE id = %s", (world.sources["cloud"],)
        )
        assert await ask(gate, db, world.alpha, world, "cloud") == "local"
        await set_workspace_llm_policy(db, access, "alpha", "none")
        assert await ask(gate, db, world.alpha, world, "cloud") == "policy_none"

    assert server.hosts() == [CLOUD_HOST, LOCAL_HOST]


async def test_token_use_lands_in_the_run_report(
    migrated_database_url: str, db: Conn, world: World
) -> None:
    await set_workspace_llm_policy(db, WriteAccess.for_owner(world.owner), "alpha", "cloud")
    server = FakeChatServer()

    async with (
        connect(migrated_database_url, autocommit=True) as reports,
        create_llm_gate(llm_settings(), transport=server.transport()) as gate,
    ):
        async with record_run(
            reports, workspace_id=world.alpha, owner_id=world.owner, kind="extract"
        ) as run:
            await ask(gate, db, world.alpha, world, "cloud")
            await ask(gate, db, world.alpha, world, "cloud", "local")
            await ask(gate, db, world.alpha, world, "local")
            await ask(gate, db, world.alpha, world, "none")  # refused: no call, no tokens
        cur = await reports.execute("SELECT report FROM run WHERE id = %s", (run.id,))
        row = await cur.fetchone()

    assert row is not None
    assert row[0]["llm"] == {
        "calls": 3,
        "input_tokens": 33,
        "output_tokens": 21,
        "by_model": {
            f"cloud/{CLOUD_MODEL}": {"calls": 1, "input_tokens": 11, "output_tokens": 7},
            f"local/{LOCAL_MODEL}": {"calls": 2, "input_tokens": 22, "output_tokens": 14},
        },
    }


# --- The workspace's policy ----------------------------------------------------------------------


async def test_a_workspace_is_local_until_the_operator_opts_it_in(db: Conn, world: World) -> None:
    access = WriteAccess.for_owner(world.owner)

    assert await get_workspace_llm_policy(db, owner_id=world.owner, name="alpha") == "local"
    assert await set_workspace_llm_policy(db, access, "alpha", "cloud") == "local"
    assert await set_workspace_llm_policy(db, access, "alpha", "none") == "cloud"
    assert await get_workspace_llm_policy(db, owner_id=world.owner, name="alpha") == "none"


async def test_a_workspace_out_of_reach_is_not_found(db: Conn, world: World) -> None:
    only_beta = WriteAccess(owner_id=world.owner, workspace_ids=frozenset({world.beta}))

    with pytest.raises(NotFoundError):
        await set_workspace_llm_policy(db, only_beta, "alpha", "cloud")
    with pytest.raises(NotFoundError):
        await set_workspace_llm_policy(db, WriteAccess.for_owner(world.owner), "gamma", "local")
    with pytest.raises(NotFoundError):
        await get_workspace_llm_policy(db, owner_id=uuid.uuid4(), name="alpha")
    assert await get_workspace_llm_policy(db, owner_id=world.owner, name="alpha") == "local"


async def test_an_unknown_policy_is_refused_in_code_and_by_the_schema(
    db: Conn, world: World
) -> None:
    with pytest.raises(ValueError, match="none, local or cloud"):
        await set_workspace_llm_policy(
            db,
            WriteAccess.for_owner(world.owner),
            "alpha",
            "remote",  # pyright: ignore[reportArgumentType]
        )
    with pytest.raises(errors.CheckViolation, match="workspace_llm_policy_check"):
        await db.execute("UPDATE workspace SET llm_policy = 'remote' WHERE id = %s", (world.alpha,))


async def test_the_fake_server_saw_json_requests(db: Conn, world: World) -> None:
    """The matrix reads hosts; this checks the requests are what an endpoint gets."""
    server = FakeChatServer()

    async with create_llm_gate(llm_settings(cloud=False), transport=server.transport()) as gate:
        await ask(gate, db, world.alpha, world, "local")

    (body,) = server.bodies()
    assert body["model"] == LOCAL_MODEL
    assert canary("local") in json.dumps(body)
