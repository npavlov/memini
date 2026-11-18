"""Tokens, authentication parsing and the authorization filter, without a database.

Every token here is generated on the spot; every id is synthetic.
"""

import dataclasses
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from psycopg import sql
from pydantic import SecretStr

from memini.auth import (
    SCOPES,
    TOKEN_PREFIX,
    AuthenticationError,
    InsufficientScopeError,
    NewToken,
    Principal,
    ReadFilter,
    TokenInfo,
    authorize_read,
    bearer_token,
    create_token,
    ensure_scope,
    generate_token,
    hash_token,
    is_well_formed_token,
)
from memini.auth.tokens import DISPLAY_PREFIX_LENGTH, display_prefix
from memini.workspaces import check_handle

# What the logs redact (memini-hvj). Every token must match it.
LOG_REDACTION = re.compile(r"memini_[A-Za-z0-9_-]{20,}")

WS_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
WS_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")
OWNER = uuid.UUID("00000000-0000-7000-8000-0000000000f0")
TOKEN_ID = uuid.UUID("00000000-0000-7000-8000-0000000000e0")


def principal(*scopes: str, workspace_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        principal_id=OWNER,
        token_id=TOKEN_ID,
        scopes=frozenset(cast(Any, scopes)),
        workspace_id=workspace_id,
    )


# --- Token format and digest -------------------------------------------------------------------


def test_a_token_is_the_prefix_and_43_base64url_characters() -> None:
    token = generate_token()
    assert token.startswith(TOKEN_PREFIX)
    assert re.fullmatch(r"memini_[A-Za-z0-9_-]{43}", token)
    assert is_well_formed_token(token)


def test_tokens_are_unique() -> None:
    assert len({generate_token() for _ in range(1000)}) == 1000


def test_a_token_is_caught_by_log_redaction_and_its_display_prefix_is_not() -> None:
    token = generate_token()
    assert LOG_REDACTION.fullmatch(token)
    shown = display_prefix(token)
    assert token.startswith(shown)
    assert len(shown) == DISPLAY_PREFIX_LENGTH <= 16  # token.prefix holds 1 to 16 characters
    assert LOG_REDACTION.search(shown) is None


def test_the_digest_is_sha256_of_the_token() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert len(digest) == 32  # token.hash holds exactly 32 bytes
    assert digest == hashlib.sha256(token.encode()).digest()
    assert hash_token(token[:-1] + ("A" if token[-1] != "A" else "B")) != digest


@pytest.mark.parametrize(
    "value",
    [
        "",
        "memini_",
        "memini_" + "a" * 42,
        "memini_" + "a" * 44,
        "Memini_" + "a" * 43,
        "mem_" + "a" * 46,
        "memini_" + "a" * 42 + "=",
        "memini_" + "a" * 42 + "+",
        "memini_" + "a" * 42 + "é",
        " memini_" + "a" * 43,
        "memini_" + "a" * 43 + "\n",
    ],
)
def test_malformed_tokens_are_recognized(value: str) -> None:
    assert not is_well_formed_token(value)


def test_the_scopes() -> None:
    assert SCOPES == ("read", "ingest", "admin")


def test_a_new_token_does_not_show_its_secret_in_repr() -> None:
    token = generate_token()
    info = TokenInfo(
        id=TOKEN_ID,
        owner_id=OWNER,
        workspace_id=None,
        name="test agent",
        prefix=display_prefix(token),
        scopes=("read",),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
        last_used_at=None,
    )
    new = NewToken(secret=SecretStr(token), info=info)
    assert token not in repr(new)
    assert token not in str(new)
    assert new.secret.get_secret_value() == token


def test_token_status() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    info = TokenInfo(
        id=TOKEN_ID,
        owner_id=OWNER,
        workspace_id=None,
        name="test agent",
        prefix="memini_abcdefgh",
        scopes=("read",),
        created_at=created,
        expires_at=created + timedelta(days=2),
        revoked_at=None,
        last_used_at=None,
    )
    assert info.status(created + timedelta(days=1)) == "active"
    assert info.status(created + timedelta(days=2)) == "expired"
    revoked = dataclasses.replace(info, revoked_at=created + timedelta(hours=1))
    assert revoked.status(created) == "active"
    assert revoked.status(created + timedelta(hours=1)) == "revoked"
    assert revoked.status(created + timedelta(days=3)) == "revoked"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"name": "test agent", "scopes": []}, "at least one scope"),
        ({"name": "test agent", "scopes": ["read", "write"]}, "unknown scopes"),
        ({"name": "  ", "scopes": ["read"]}, "needs a name"),
        (
            {"name": "test agent", "scopes": ["read"], "expires_at": datetime(2099, 1, 1)},
            "timezone-aware",
        ),
    ],
)
async def test_create_token_refuses_bad_arguments_before_the_database(
    arguments: dict[str, Any], problem: str
) -> None:
    # No connection: the arguments are checked before it is used.
    with pytest.raises(ValueError, match=problem):
        await create_token(cast(Any, None), owner_id=OWNER, **arguments)


@pytest.mark.parametrize("name", ["alpha", "a", "ws-1_b", "0" * 63])
def test_handles(name: str) -> None:
    assert check_handle("workspace", name) == name


@pytest.mark.parametrize("name", ["", "Alpha", "-a", "_a", "a b", "a.b", "0" * 64, "é"])
def test_bad_handles(name: str) -> None:
    with pytest.raises(ValueError, match="workspace name"):
        check_handle("workspace", name)


# --- The Authorization header ------------------------------------------------------------------


def test_bearer_token_takes_the_token_out_of_the_header() -> None:
    token = generate_token()
    assert bearer_token(f"Bearer {token}") == token
    assert bearer_token(f"bearer {token}") == token
    assert bearer_token(f"BEARER   {token}  ") == token


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (None, "missing"),
        ("", "missing"),
        ("Basic dXNlcjpwYXNz", "missing"),
        ("Token {token}", "missing"),
        ("{token}", "missing"),
        ("Bearer", "malformed"),
        ("Bearer ", "malformed"),
        ("Bearer not-a-token", "malformed"),
        ("Bearer {token} {token}", "malformed"),
        ("Bearer {token}x", "malformed"),
    ],
)
def test_bearer_token_refuses(header: str | None, reason: str) -> None:
    token = generate_token()
    value = None if header is None else header.format(token=token)
    with pytest.raises(AuthenticationError) as refused:
        bearer_token(value)
    assert refused.value.reason == reason
    assert token not in str(refused.value)


# --- Scopes and the authorization filter -------------------------------------------------------


def test_ensure_scope() -> None:
    ensure_scope(principal("read", "ingest"), "ingest")
    with pytest.raises(InsufficientScopeError) as refused:
        ensure_scope(principal("read"), "admin")
    assert refused.value.scope == "admin"


def rendered(access: ReadFilter) -> str:
    condition = access.where(
        workspace_id=sql.Identifier("t", "workspace_id"),
        owner_id=sql.Identifier("t", "owner_id"),
        source_id=sql.Identifier("t", "source_id"),
    )
    return condition.as_string(None)


def test_v1_lets_everything_through_for_a_token_without_a_workspace() -> None:
    access = authorize_read(principal("read"))
    assert access.workspace_ids is None
    assert rendered(access) == "TRUE"


def test_a_token_limited_to_a_workspace_reads_only_that_workspace() -> None:
    access = authorize_read(principal("read", workspace_id=WS_A))
    assert access.workspace_ids == {WS_A}
    condition = rendered(access)
    assert condition.startswith('"t"."workspace_id" = ANY(')
    assert WS_A.hex in condition
    assert WS_B.hex not in condition


def test_requested_workspaces_narrow_the_filter() -> None:
    assert authorize_read(principal("read"), workspaces=[WS_A, WS_B]).workspace_ids == {WS_A, WS_B}
    limited = principal("read", workspace_id=WS_A)
    assert authorize_read(limited, workspaces=[WS_A, WS_B]).workspace_ids == {WS_A}


def test_a_request_beyond_the_token_reads_nothing() -> None:
    access = authorize_read(principal("read", workspace_id=WS_A), workspaces=[WS_B])
    assert access.workspace_ids == frozenset()
    assert rendered(access) == "FALSE"
    assert rendered(authorize_read(principal("read"), workspaces=[])) == "FALSE"


def test_the_filter_keeps_the_principal() -> None:
    caller = principal("read", workspace_id=WS_A)
    assert authorize_read(caller).principal is caller
