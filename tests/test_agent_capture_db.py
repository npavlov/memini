"""The capture side of the agent executor (memini-2wl): the memini-intake skill's client and its
Claude Code hook, run as processes of their own against the service under uvicorn and a real
Postgres. A scripted fake agent runs the skill's loop: ``start``, ``next``, the MCP call (the fake
Hearth server), the hook with the PostToolUse event Claude Code would send, and ``finish``.

Acceptance: the run completes end to end and its records are ingested; a response of a server
outside the allowlist is not sent, nothing is stored and the hook prints nothing; the hook's output
holds no payload; the token is never printed. Every value is synthetic (tests/agent_helpers.py).
"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, LiteralString

import psycopg
import pytest
from pydantic import SecretStr

from agent_helpers import (
    SOURCE,
    add_mcp_source,
    call_hearth,
    every_text,
    fake_hearth,
    hearth_parser_registered,
    running_service,
)
from hearth_helpers import CHANNEL, FAKE_TOKEN, FakeHearth
from memini.auth import create_token
from memini.config import Settings
from memini.db import connect, migrate
from memini.workspaces import create_workspace, ensure_principal

pytestmark = [pytest.mark.integration]

REPOSITORY: Final = Path(__file__).resolve().parent.parent
SKILL: Final = REPOSITORY / "integrations" / "claude-code" / "skills" / "memini-intake"
LAUNCHER: Final = SKILL / "scripts" / "memini-agent"
SERVER: Final = "hearth"
"""The MCP server's name in the session: its tools are mcp__hearth__*."""


async def _setup(url: str) -> str:
    """Workspace acme, the mcp source hearth with channel C0042, and a token with the ingest scope
    for the hook. Returns the token."""
    await migrate(url)
    async with connect(url, autocommit=True) as db:
        owner = await ensure_principal(db, "owner")
        acme = await create_workspace(db, owner_id=owner, name="acme")
        await add_mcp_source(db, acme.id, owner)
        new = await create_token(db, owner_id=owner, name="capture hook", scopes=["ingest"])
        return new.secret.get_secret_value()


def count(url: str, query: LiteralString) -> int:
    with psycopg.connect(url) as db:
        row = db.execute(query).fetchone()
    assert row is not None
    return int(row[0])


@dataclass
class Agent:
    """Runs the skill's commands and its hook as Claude Code would, and keeps every output."""

    env: Mapping[str, str]
    outputs: list[str] = field(default_factory=list[str])

    def run(self, *args: str, stdin: str = "", ok: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(  # noqa: S603 - the launcher of this repository, fixed arguments
            [str(LAUNCHER), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=dict(self.env),
            timeout=120,
            check=False,
        )
        self.outputs += [done.stdout, done.stderr]
        if ok:
            assert done.returncode == 0, done.stderr
        return done

    def hook(self, event: Mapping[str, Any]) -> str:
        done = self.run("hook", stdin=json.dumps(event))
        return done.stdout.strip()


def event(tool_name: str, arguments: Mapping[str, Any], result: Any, tmp: Path) -> dict[str, Any]:
    """A PostToolUse event of Claude Code for an MCP tool."""
    return {
        "session_id": "session-0001",
        "transcript_path": str(tmp / "transcript.jsonl"),
        "cwd": str(tmp),
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": dict(arguments),
        "tool_response": result,
        "tool_use_id": "toolu_0001",
    }


def handed_out(output: str) -> tuple[str, dict[str, Any]]:
    """The tool and the arguments ``next`` printed."""
    lines = dict(
        line.split(": ", 1)
        for line in output.splitlines()
        if line.startswith(("tool:", "arguments:"))
    )
    return lines["tool"], json.loads(lines["arguments"])


@dataclass(frozen=True)
class Session:
    url: str
    token: str
    agent: Agent
    tmp: Path
    state: Path


@pytest.fixture
def session(empty_database_url: str, tmp_path: Path) -> Generator[Session]:
    token = asyncio.run(_setup(empty_database_url))
    token_file = tmp_path / "capture.token"
    token_file.write_text(f"{token}\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = tmp_path / "capture.toml"
    state = tmp_path / "state" / "agent-run.json"
    with (
        hearth_parser_registered(),
        running_service(Settings(database_url=SecretStr(empty_database_url))) as service,
    ):
        config.write_text(
            f'url = "{service}"\ntoken_file = "{token_file}"\nservers = ["{SERVER}"]\n',
            encoding="utf-8",
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "MEMINI_CAPTURE_CONFIG": str(config),
            "MEMINI_CAPTURE_STATE": str(state),
            "MEMINI_CAPTURE_PYTHON": sys.executable,
        }
        yield Session(empty_database_url, token, Agent(env), tmp_path, state)


def test_a_scripted_agent_completes_a_run_through_the_skill_and_the_hook(session: Session) -> None:
    server = fake_hearth()
    agent, url = session.agent, session.url
    started = agent.run("start", "acme", SOURCE, CHANNEL, "--server", SERVER).stdout
    assert "2 windows planned" in started
    assert session.state.exists()

    calls = 0
    for _ in range(50):
        step = agent.run("next").stdout
        if "The run is done" in step:
            break
        if "is done" in step:
            continue
        tool, arguments = handed_out(step)
        assert tool.startswith(f"mcp__{SERVER}__")
        name = tool.removeprefix(f"mcp__{SERVER}__")
        result = call_hearth(server, name, arguments)
        blocks = result["content"]  # what Claude Code hands a PostToolUse hook
        stored = count(url, "SELECT count(*) FROM raw_response")

        # The same payload from a server outside the allowlist: not sent, nothing printed.
        assert agent.hook(event(f"mcp__other__{name}", arguments, blocks, session.tmp)) == ""
        assert count(url, "SELECT count(*) FROM raw_response") == stored

        output = json.loads(agent.hook(event(tool, arguments, blocks, session.tmp)))
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "PostToolUse"
        receipt = specific["additionalContext"]
        assert receipt.startswith("memini: the response was stored")
        # Claude Code shows the agent the receipt in place of the result.
        assert specific["updatedMCPToolOutput"] == [{"type": "text", "text": receipt}]
        assert count(url, "SELECT count(*) FROM raw_response") == stored + 1
        calls += 1
    else:
        raise AssertionError("the run never ended")
    assert calls == 6

    finished = agent.run("finish").stdout
    assert "2 of 2 windows ingested, 2 complete, 5 records" in finished
    assert not session.state.exists()
    assert count(url, "SELECT count(*) FROM record WHERE deleted_at IS NULL") == 5
    assert count(url, "SELECT count(*) FROM stream WHERE checked_until IS NOT NULL") == 1
    assert count(url, "SELECT count(*) FROM run WHERE kind = 'agent_sync'") == 1

    # Nothing the agent saw holds the token or a word of what the server returned.
    printed = "\n".join(agent.outputs)
    assert session.token not in printed
    for text in [*every_text(server.threads), FAKE_TOKEN]:
        assert text not in printed


def test_the_hook_sends_nothing_without_a_run_or_for_another_call(session: Session) -> None:
    server: FakeHearth = fake_hearth()
    agent, url, tmp = session.agent, session.url, session.tmp
    arguments = {"channel": CHANNEL, "oldest": 1, "latest": 2, "limit": 100}
    blocks = server.channel_history(arguments)["content"]

    # No run in progress: the launcher drains the event and exits; no process reads it.
    assert agent.hook(event(f"mcp__{SERVER}__channel_history", arguments, blocks, tmp)) == ""
    assert not session.state.exists()

    agent.run("start", "acme", SOURCE, CHANNEL, "--server", SERVER)
    tool, issued = handed_out(agent.run("next").stdout)

    # Another tool of the allowlisted server: not sent; the agent is told which call to make.
    other = json.loads(agent.hook(event(f"mcp__{SERVER}__thread_replies", issued, blocks, tmp)))
    note = other["hookSpecificOutput"]["additionalContext"]
    assert note.startswith("memini: not captured")
    assert tool in note
    assert "updatedMCPToolOutput" not in other["hookSpecificOutput"]

    # The handed-out tool with other arguments: the service refuses it and stores nothing.
    changed = {**issued, "limit": 5}
    refused = json.loads(agent.hook(event(tool, changed, blocks, tmp)))
    assert "call_mismatch" in refused["hookSpecificOutput"]["additionalContext"]
    assert count(url, "SELECT count(*) FROM raw_response") == 0

    # A call that failed: recorded as failed, and handed out again.
    failure = {
        **event(tool, issued, None, tmp),
        "hook_event_name": "PostToolUseFailure",
        "error": "MCP error -32603: the server is unavailable",
    }
    del failure["tool_response"]
    failed = json.loads(agent.hook(failure))
    assert failed["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "failed call was recorded" in failed["hookSpecificOutput"]["additionalContext"]
    assert count(url, "SELECT count(*) FROM raw_response WHERE is_error") == 1
    again = agent.run("next").stdout
    assert "attempt 2" in again
    assert handed_out(again) == (tool, issued)

    # A server not in the allowlist cannot be started at all.
    agent.run("finish")
    refused_start = agent.run("start", "acme", SOURCE, CHANNEL, "--server", "other", ok=False)
    assert refused_start.returncode == 1
    assert "allowlist" in refused_start.stderr
    assert session.token not in "\n".join(agent.outputs)
