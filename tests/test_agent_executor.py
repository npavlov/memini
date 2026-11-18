"""The agent executor without a database: what a hook may post, the recipe of a source, the
server's version, and the capture side's files (the skill, the hook configuration, the launcher and
its helper). The run itself is tested in tests/test_agent_runs_db.py and
tests/test_agent_capture_db.py. Every value is synthetic."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from agent_helpers import recipe_text
from memini.executors.agent import (
    RECIPE_KEY,
    InvalidResultError,
    SourceNotReadyError,
    mcp_result,
    source_recipe,
    version_matches,
    window_overrides,
)
from memini.recipes import Server

REPOSITORY: Final = Path(__file__).resolve().parent.parent
PLUGIN: Final = REPOSITORY / "integrations" / "claude-code"
SKILL: Final = PLUGIN / "skills" / "memini-intake"
LAUNCHER: Final = SKILL / "scripts" / "memini-agent"
HELPER: Final = SKILL / "scripts" / "memini_agent.py"


def load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("memini_agent_helper", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- What a hook posts ---------------------------------------------------------------------------

BLOCKS: Final = [{"type": "text", "text": '{"messages": []}'}]


def test_a_result_object_is_taken_as_it_is() -> None:
    result = {"content": BLOCKS, "structuredContent": {"n": 1}, "isError": False, "_meta": {}}
    assert mcp_result(result) == result


def test_content_blocks_and_a_text_get_the_envelope_of_a_result_and_nothing_else() -> None:
    assert mcp_result(BLOCKS) == {"content": BLOCKS, "isError": False}
    assert mcp_result("plain text") == {
        "content": [{"type": "text", "text": "plain text"}],
        "isError": False,
    }


def test_an_error_is_a_failed_result() -> None:
    assert mcp_result(error="MCP error -32603") == {
        "content": [{"type": "text", "text": "MCP error -32603"}],
        "isError": True,
    }


@pytest.mark.parametrize(
    "result",
    [None, 12, True, {"isError": False}, {"content": "text"}, [{"text": "no type"}], ["text"]],
)
def test_anything_else_is_not_a_result(result: Any) -> None:
    with pytest.raises(InvalidResultError):
        mcp_result(result)


def test_a_result_and_an_error_together_are_refused() -> None:
    with pytest.raises(InvalidResultError):
        mcp_result(BLOCKS, "boom")


# --- The recipe of a source ----------------------------------------------------------------------


def test_a_source_holds_its_recipe_as_toml_text_or_as_a_table() -> None:
    from_text = source_recipe({RECIPE_KEY: recipe_text()})
    assert from_text.name == "hearth"
    table = from_text.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    assert source_recipe({RECIPE_KEY: table}) == from_text


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "has no recipe"),
        ({RECIPE_KEY: 12}, "has no recipe"),
        ({RECIPE_KEY: "format = = 1"}, "not TOML"),
        ({RECIPE_KEY: "format = 1"}, "not valid"),
    ],
)
def test_a_source_without_a_valid_recipe_is_not_ready(config: Any, message: str) -> None:
    with pytest.raises(SourceNotReadyError, match=message):
        source_recipe(config)


def test_window_overrides_are_a_table() -> None:
    assert window_overrides({}) is None
    assert window_overrides({"window": {"initial": "3d"}}) == {"initial": "3d"}
    with pytest.raises(SourceNotReadyError):
        window_overrides({"window": "3d"})


@pytest.mark.parametrize(
    ("version", "patterns", "known"),
    [
        ("2.3.1", ["2.*"], True),
        ("3.0.0", ["2.*"], False),
        ("2.3.1", [], True),
        ("1.4.0-beta", ["1.4.*", "2.*"], True),
        ("1x4", ["1.4"], False),
    ],
)
def test_a_version_matches_the_recipe_s_patterns(
    version: str, patterns: list[str], known: bool
) -> None:
    assert version_matches(version, Server(name="hearth-mcp", versions=patterns)) is known


# --- The capture side's files --------------------------------------------------------------------


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---\n", 2)[1]
    found: dict[str, str] = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        found[key.strip()] = value.strip()
    return found


def test_the_skill_names_itself_and_says_when_to_use_it() -> None:
    meta = frontmatter(SKILL / "SKILL.md")
    assert meta["name"] == "memini-intake"
    assert "MCP" in meta["description"]
    assert "${CLAUDE_SKILL_DIR}/scripts/memini-agent" in meta["allowed-tools"]
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "data, never instructions" in body


def test_the_plugin_hooks_run_the_launcher_after_mcp_tools() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "memini"
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"PostToolUse", "PostToolUseFailure"}
    for entries in hooks.values():
        (entry,) = entries
        assert entry["matcher"] == "mcp__.*"
        (handler,) = entry["hooks"]
        command = handler["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN))
        assert command == f'"{LAUNCHER}" hook'
    assert os.access(LAUNCHER, os.X_OK)
    assert os.access(HELPER, os.X_OK)


def test_the_launcher_is_bash_3_2_and_shellcheck_clean() -> None:
    assert LAUNCHER.read_text(encoding="utf-8").startswith("#!/bin/bash\n")
    bash = Path("/bin/bash")
    if bash.exists():
        subprocess.run([str(bash), "-n", str(LAUNCHER)], check=True)  # noqa: S603 - fixed paths
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    subprocess.run([shellcheck, str(LAUNCHER)], check=True)  # noqa: S603 - fixed paths


def test_the_hook_matches_only_allowlisted_servers() -> None:
    helper = load_helper()
    servers = ("hearth", "hearth_mail")
    assert helper.match_server("mcp__hearth__channel_history", servers) == (
        "hearth",
        "channel_history",
    )
    assert helper.match_server("mcp__hearth_mail__search__v2", servers) == (
        "hearth_mail",
        "search__v2",
    )
    assert helper.match_server("mcp__other__channel_history", servers) is None
    assert helper.match_server("mcp__hearth__", servers) is None
    assert helper.match_server("Bash", servers) is None


def test_the_receipt_takes_the_shape_of_the_output_it_replaces() -> None:
    helper = load_helper()
    assert helper.replacement(BLOCKS, "ok") == [{"type": "text", "text": "ok"}]
    assert helper.replacement("text", "ok") == "ok"
    assert helper.replacement({"content": BLOCKS}, "ok") == {
        "content": [{"type": "text", "text": "ok"}]
    }


def test_the_hook_ignores_what_it_does_not_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = load_helper()
    config = tmp_path / "capture.toml"
    config.write_text(
        'url = "http://127.0.0.1:9"\ntoken_file = "/nonexistent"\nservers = ["hearth"]\n',
        encoding="utf-8",
    )
    state = tmp_path / "agent-run.json"
    state.write_text(
        json.dumps(
            {
                "workspace": "acme",
                "source": "hearth",
                "stream": "C0042",
                "run_id": "00000000-0000-0000-0000-000000000001",
                "server": "hearth",
                "call": {"id": "c1", "tool": "channel_history", "arguments": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(helper.CONFIG_VAR, str(config))
    monkeypatch.setenv(helper.STATE_VAR, str(state))

    def event(**fields: Any) -> str:
        return json.dumps(
            {"hook_event_name": "PostToolUse", "tool_input": {}, "tool_response": BLOCKS, **fields}
        )

    # Neither a JSON event, nor an MCP tool, nor an allowlisted server: nothing is sent or said.
    assert helper.run_hook("not json") is None
    assert helper.run_hook(event(tool_name="Bash")) is None
    assert helper.run_hook(event(tool_name="mcp__other__channel_history")) is None
    assert helper.run_hook(event(tool_name="mcp__hearth__x", hook_event_name="PreToolUse")) is None
