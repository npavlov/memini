"""Checks for scripts/compose-smoke.sh, run on the host against the running compose stack.

    MEMINI_SMOKE_TOKEN=... uv run python scripts/compose_smoke.py mcp http://127.0.0.1:6364
    MEMINI_SMOKE_TOKEN=... uv run python scripts/compose_smoke.py logs FILE

``mcp``: liveness and readiness answer 200; the MCP endpoint refuses a request without a token and
one with an unknown token (401); a real MCP client (the SDK's streamable HTTP client) connects with
the token and the ``status`` tool answers with the database reachable and fully migrated.

``logs``: every line of FILE, the service log, is a JSON object, and none holds the token.

The token is read from the environment, never from the command line (which other users can see),
and is never printed. Prints one line per check; exits 1 at the first that fails.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, cast

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOKEN_VAR: Final = "MEMINI_SMOKE_TOKEN"  # noqa: S105 - the name of a variable, not a token
TIMEOUT: Final = 15.0


class CheckFailed(Exception):
    """A check did not pass; the message says which and why, without the token."""


def ok(text: str) -> None:
    print(f"ok    {text}", flush=True)


def token_from_env() -> str:
    token = os.environ.get(TOKEN_VAR, "").strip()
    if not token:
        raise CheckFailed(f"{TOKEN_VAR} is not set")
    return token


def expect_status(response: httpx2.Response, expected: int, what: str) -> None:
    if response.status_code != expected:
        raise CheckFailed(f"{what}: expected HTTP {expected}, got {response.status_code}")


def as_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CheckFailed(f"{what}: expected a JSON object, got {type(value).__name__}")
    return cast("Mapping[str, Any]", value)


async def check_mcp(base_url: str, token: str) -> None:
    async with httpx2.AsyncClient(base_url=base_url, timeout=TIMEOUT) as http:
        expect_status(await http.get("/healthz"), 200, "GET /healthz")
        ok("GET /healthz: 200")
        readyz = await http.get("/readyz")
        expect_status(readyz, 200, "GET /readyz")
        ok(f"GET /readyz: 200, {as_mapping(readyz.json(), 'readyz').get('status')}")

        # A valid MCP request, so that only the missing or unknown token can be the refusal.
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "memini-compose-smoke", "version": "0"},
            },
        }
        accept = {"Accept": "application/json, text/event-stream"}
        for label, headers in (
            ("without a token", accept),
            ("with an unknown token", {**accept, "Authorization": "Bearer unknown"}),
        ):
            refused = await http.post("/mcp", json=initialize, headers=headers)
            expect_status(refused, 401, f"POST /mcp {label}")
            if "www-authenticate" not in refused.headers:
                raise CheckFailed(f"POST /mcp {label}: 401 without a WWW-Authenticate header")
            ok(f"POST /mcp {label}: 401")

    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=TIMEOUT) as http,
        Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client,
    ):
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools.tools)
        if "status" not in names:
            raise CheckFailed(f"MCP tools/list: no status tool among {names}")
        ok(f"MCP tools/list with the token: {', '.join(names)}")
        result = await client.call_tool("status", {})

    if result.is_error:
        raise CheckFailed("MCP status: the tool returned an error")
    status = as_mapping(result.structured_content, "MCP status result")
    database = as_mapping(status.get("database"), "MCP status database")
    if database.get("reachable") is not True:
        raise CheckFailed("MCP status: database.reachable is not true")
    schema, expected = database.get("schema_version"), database.get("expected_schema_version")
    if schema != expected:
        raise CheckFailed(f"MCP status: schema version {schema}, expected {expected}")
    workspaces = status.get("workspaces")
    count = len(cast("list[object]", workspaces)) if isinstance(workspaces, list) else "?"
    ok(
        f"MCP status: Memini {status.get('version')}, database reachable,"
        f" schema version {schema}, workspaces {count}"
    )


def check_logs(path: Path, token: str) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise CheckFailed(f"{path}: the service log is empty")
    for number, line in enumerate(lines, start=1):
        if token in line:
            raise CheckFailed(f"{path}:{number}: the log holds the token")
        try:
            as_mapping(json.loads(line), f"{path}:{number}")
        except json.JSONDecodeError:
            raise CheckFailed(f"{path}:{number}: not a JSON line") from None
    ok(f"service log: {len(lines)} lines, each a JSON object, none holds the token")


def describe(error: BaseException, token: str) -> str:
    """One line for an unexpected error: the first error inside task groups, the token masked."""
    while isinstance(error, BaseExceptionGroup):
        error = cast("BaseExceptionGroup[BaseException]", error).exceptions[0]
    text = f"{type(error).__name__}: {error}"
    return text.replace(token, "<token>") if token else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Checks for scripts/compose-smoke.sh against the running compose stack."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    mcp = commands.add_parser("mcp", help="check the HTTP and MCP endpoints")
    mcp.add_argument("base_url", help="e.g. http://127.0.0.1:6364")
    logs = commands.add_parser("logs", help="check a file of service log lines")
    logs.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    token = ""
    try:
        token = token_from_env()
        if args.command == "mcp":
            asyncio.run(check_mcp(str(args.base_url).rstrip("/"), token))
        else:
            check_logs(args.file, token)
    except CheckFailed as failure:
        print(f"FAIL  {failure}", file=sys.stderr, flush=True)
        return 1
    except Exception as error:  # a connection error, an MCP error: one line, never the token
        print(f"FAIL  {describe(error, token)}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
