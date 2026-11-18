#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Memini's agent executor from a Claude Code session: the client the memini-intake skill runs, and
the capture hook's helper. Standard library only; Python 3.11 or later (tomllib). The launcher
``memini-agent`` next to it picks the interpreter and runs it.

Commands (the skill runs them; a person can too)::

    memini-agent start WORKSPACE SOURCE STREAM --server NAME [--server-version VERSION]
    memini-agent next      # the next call to make, or that a window or the run is done
    memini-agent finish    # parse and ingest what was captured; the run report
    memini-agent status    # the current run
    memini-agent hook      # stdin: a PostToolUse or PostToolUseFailure event of Claude Code

**Configuration**, a TOML file: ``$MEMINI_CAPTURE_CONFIG``, else
``${XDG_CONFIG_HOME:-~/.config}/memini/capture.toml``::

    url = "http://127.0.0.1:6364"                 # the Memini service
    token_file = "~/.config/memini/capture.token"  # a token with the ingest scope, in a file
    servers = ["hearth"]                           # MCP servers whose responses may be captured

``servers`` is the allowlist: the names of MCP servers as Claude Code names their tools
(``mcp__<name>__<tool>``). A response of any other server is never sent. The token is read from its
file when a request is made; it is never printed and never passed on a command line.

**State**: the run in progress and the call handed out, a JSON file: ``$MEMINI_CAPTURE_STATE``,
else ``${XDG_STATE_HOME:-~/.local/state}/memini/agent-run.json``. ``start`` writes it, ``next``
records each call in it, ``finish`` removes it. The hook does nothing while it does not exist.

**The hook** posts the raw result of an MCP call to the service only when the server is in the
allowlist and the tool is the one of the call handed out (the service checks the arguments too).
It never changes the result. It prints a short receipt for the agent, as the hook's JSON output,
and asks Claude Code to show the agent that receipt in place of the result: what the server
returned goes to Memini, not into the conversation. For anything else it prints nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

MIN_PYTHON = (3, 11)
CONFIG_VAR = "MEMINI_CAPTURE_CONFIG"
STATE_VAR = "MEMINI_CAPTURE_STATE"
TIMEOUT = 60.0
MAX_TOKEN_FILE = 4096
USER_AGENT = "memini-agent-capture/1"

Json = dict[str, Any]


class Failure(Exception):
    """A problem to report in one line; never holds the token or a value of a response."""


# --- Configuration and state ---------------------------------------------------------------------


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def config_path() -> Path:
    found = os.environ.get(CONFIG_VAR)
    if found:
        return Path(found)
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "memini" / "capture.toml"


def state_path() -> Path:
    found = os.environ.get(STATE_VAR)
    if found:
        return Path(found)
    base = os.environ.get("XDG_STATE_HOME") or str(_home() / ".local" / "state")
    return Path(base) / "memini" / "agent-run.json"


class Config:
    """The capture configuration (see the module)."""

    def __init__(self, url: str, token_file: Path, servers: tuple[str, ...], timeout: float):
        self.url = url
        self.token_file = token_file
        self.servers = servers
        self.timeout = timeout

    @classmethod
    def load(cls, path: Path) -> Config:
        import tomllib

        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise Failure(f"cannot read {path}: {error.strerror or type(error).__name__}") from None
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise Failure(f"{path} is not TOML") from None
        url = data.get("url", "http://127.0.0.1:6364")
        token_file = data.get("token_file")
        servers = data.get("servers")
        timeout = data.get("timeout", TIMEOUT)
        if not isinstance(url, str):
            raise Failure(f"{path}: url is the service's URL, such as http://127.0.0.1:6364")
        if not isinstance(token_file, str) or not token_file:
            raise Failure(f"{path}: token_file names the file that holds a Memini token")
        if not isinstance(servers, list) or not all(
            isinstance(name, str) and name for name in cast(list[Any], servers)
        ):
            raise Failure(f"{path}: servers lists the MCP servers to capture, by their names")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise Failure(f"{path}: timeout is a number of seconds above 0")
        return cls(
            url=_check_url(url, path),
            token_file=Path(token_file).expanduser(),
            servers=tuple(cast(list[str], servers)),
            timeout=float(timeout),
        )

    def token(self) -> str:
        try:
            raw = self.token_file.read_bytes()[: MAX_TOKEN_FILE + 1]
        except OSError as error:
            reason = error.strerror or type(error).__name__
            raise Failure(f"cannot read the token file {self.token_file}: {reason}") from None
        if len(raw) > MAX_TOKEN_FILE:
            raise Failure(f"the token file {self.token_file} is too large to hold a token")
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise Failure(f"the token file {self.token_file} is not UTF-8 text") from None
        if not token or any(c.isspace() or not c.isprintable() for c in token):
            raise Failure(f"the token file {self.token_file} does not hold a token")
        return token


def _check_url(value: str, path: Path) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Failure(f"{path}: url is the service's URL, such as http://127.0.0.1:6364")
    if parts.username is not None or parts.password is not None:
        raise Failure(
            f"{path}: url holds a user name or password; the token file is the credential"
        )
    if parts.query or parts.fragment:
        raise Failure(f"{path}: url has a query or a fragment")
    return value.strip().rstrip("/")


def read_state() -> Json | None:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise Failure(f"the run state {path} cannot be read; remove it to start over") from None
    if not isinstance(data, dict):
        raise Failure(f"the run state {path} is not a run; remove it to start over")
    return cast(Json, data)


def write_state(state: Json) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(path.name + ".tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(state, file, sort_keys=True)
    partial.replace(path)


def clear_state() -> None:
    state_path().unlink(missing_ok=True)


# --- The service ---------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token goes to the configured URL and nowhere else."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


class ApiError(Failure):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{message} ({code}, HTTP {status})")
        self.status = status
        self.code = code


def call_api(config: Config, method: str, path: str, body: Json | None = None) -> Json:
    """One request to the service; its JSON answer. Errors become :class:`Failure`."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - http(s) only, checked in _check_url
        config.url + "/" + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + config.token(),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with _OPENER.open(request, timeout=config.timeout) as answer:
            found = json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as error:
        raise _api_error(error) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise Failure(f"the service at {config.url} cannot be reached") from None
    except ValueError:
        raise Failure("the service answered something that is not JSON") from None
    if not isinstance(found, dict):
        raise Failure("the service answered something that is not a JSON object")
    return cast(Json, found)


def _api_error(error: urllib.error.HTTPError) -> ApiError:
    try:
        body = json.loads(error.read() or b"{}")
        detail = cast(Json, body["error"])
        code, message = str(detail["code"]), str(detail["message"])
    except (ValueError, KeyError, TypeError, OSError):
        code, message = "http_error", "the service refused the request"
    return ApiError(error.code, code, message)


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def run_path(state: Json, *rest: str) -> str:
    parts = [
        "v1/workspaces",
        _segment(str(state["workspace"])),
        "sources",
        _segment(str(state["source"])),
        "agent-runs",
        _segment(str(state["run_id"])),
        *rest,
    ]
    return "/".join(parts)


# --- Commands ------------------------------------------------------------------------------------


def _window(window: Any) -> str:
    if not isinstance(window, dict):
        return "no window"
    found = cast(Json, window)
    bounds = f"{found.get('from')}/{found.get('to')}"
    return f"window {found.get('position')} ({found.get('kind')} {bounds})"


def command_start(args: argparse.Namespace, config: Config) -> int:
    if args.server not in config.servers:
        raise Failure(
            f"the server {args.server} is not in the allowlist (servers in {config_path()}):"
            " its responses would not be captured"
        )
    current = read_state()
    if current is not None:
        raise Failure(
            f"agent run {current.get('run_id')} is in progress: run `finish` first, or remove"
            f" {state_path()} to forget it"
        )
    server: Json = {"name": args.server_name or args.server}
    if args.server_version:
        server["version"] = args.server_version
    body: Json = {"stream": args.stream}
    if args.server_name or args.server_version:
        body["server"] = server
    path = f"v1/workspaces/{_segment(args.workspace)}/sources/{_segment(args.source)}/agent-runs"
    run = call_api(config, "POST", path, body)
    write_state(
        {
            "workspace": args.workspace,
            "source": args.source,
            "stream": args.stream,
            "run_id": run["id"],
            "server": args.server,
            "call": None,
        }
    )
    windows = cast(list[Any], run.get("windows") or [])
    print(f"agent run {run['id']} started: {len(windows)} windows planned up to {run.get('as_of')}")
    print("Run `next` for the first call.")
    return 0


def _require_state() -> Json:
    state = read_state()
    if state is None:
        raise Failure("no agent run is in progress: run `start` first")
    return state


def command_next(_args: argparse.Namespace, config: Config) -> int:
    state = _require_state()
    step = call_api(config, "GET", run_path(state, "next"))
    status = step.get("status")
    if status == "call":
        call = cast(Json, step["call"])
        tool = f"mcp__{state['server']}__{call['tool']}"
        state["call"] = {"id": call["id"], "tool": call["tool"], "arguments": call["arguments"]}
        write_state(state)
        print(
            f"Call {call['call']} page {call['page']} of {_window(step.get('window'))},"
            f" attempt {call['attempt']}. Make exactly this MCP call, then run `next` again:"
        )
        print(f"tool: {tool}")
        print(f"arguments: {json.dumps(call['arguments'], ensure_ascii=False, sort_keys=True)}")
        return 0
    state["call"] = None
    write_state(state)
    if status == "window_done":
        complete = (
            "complete" if step.get("complete") else f"incomplete, {step.get('left')} calls left"
        )
        print(f"{_window(step.get('window'))} is done ({complete}). Run `next` again.")
        return 0
    print(f"The run is done: {step.get('reason')}. Run `finish`.")
    return 0


def command_finish(_args: argparse.Namespace, config: Config) -> int:
    state = _require_state()
    result = call_api(config, "POST", run_path(state, "finish"))
    clear_state()
    windows = [cast(Json, w) for w in cast(list[Any], result.get("windows") or [])]
    ingested = [w for w in windows if w.get("ingested")]
    complete = sum(1 for w in ingested if cast(Json, w["window"]).get("complete"))
    records = sum(int(w.get("records") or 0) for w in ingested)
    print(
        f"agent run finished: {len(ingested)} of {len(windows)} windows ingested, {complete}"
        f" complete, {records} records; checkpoint {result.get('checked_until')}"
    )
    print(f"run report: {result.get('run_id')}")
    return 0


def command_status(_args: argparse.Namespace, config: Config) -> int:
    state = _require_state()
    run = call_api(config, "GET", run_path(state))
    windows = [cast(Json, w) for w in cast(list[Any], run.get("windows") or [])]
    print(f"agent run {run.get('id')}: {run.get('state')}, stream {run.get('stream')}")
    for window in windows:
        print(f"  {_window(window)}: {window.get('status')}")
    counts = cast(Json, run.get("counts") or {})
    print("  " + ", ".join(f"{name} {value}" for name, value in sorted(counts.items())))
    return 0


# --- The hook ------------------------------------------------------------------------------------

EVENTS = ("PostToolUse", "PostToolUseFailure")


def match_server(tool_name: str, servers: tuple[str, ...]) -> tuple[str, str] | None:
    """The allowlisted server and the tool's name at it, from Claude Code's ``mcp__S__T``."""
    for server in sorted(servers, key=len, reverse=True):
        prefix = f"mcp__{server}__"
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
            return server, tool_name[len(prefix) :]
    return None


def replacement(shape: Any, text: str) -> Any:
    """``text`` in the shape of the tool's output, for Claude Code to show in its place."""
    block = {"type": "text", "text": text}
    if isinstance(shape, str):
        return text
    if isinstance(shape, dict):
        return {"content": [block]}
    return [block]


def hook_output(event: str, receipt: str, shape: Any = None, *, replace: bool) -> Json:
    specific: Json = {"hookEventName": event, "additionalContext": receipt}
    if replace and event == "PostToolUse":
        replaced = replacement(shape, receipt)
        specific["updatedMCPToolOutput"] = replaced
        specific["updatedToolOutput"] = replaced
    return {"hookSpecificOutput": specific}


def receipt_text(answer: Json) -> str:
    window = _window(answer.get("window"))
    if answer.get("is_error"):
        what = "the failed call was recorded; `next` hands it out again if it may be retried"
    elif answer.get("stored"):
        what = "the response was stored"
    else:
        what = "the same response was stored before"
    return (
        f"memini: {what} (response {answer.get('position')} of {window},"
        f" {answer.get('responses')} in the window). The result is kept by Memini and not shown"
        " here. Run `next` for the next call."
    )


def run_hook(stdin: str) -> Json | None:
    """What the hook prints for one event: ``None`` for an event it does not capture."""
    try:
        event = json.loads(stdin)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    found = cast(Json, event)
    name = found.get("hook_event_name")
    tool_name = found.get("tool_name")
    if name not in EVENTS or not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return None
    path = config_path()
    if not path.exists():
        return None
    config = Config.load(path)
    matched = match_server(tool_name, config.servers)
    if matched is None:
        return None  # not an allowlisted server: nothing is sent, nothing is said
    server, tool = matched
    state = read_state()
    call = None if state is None else state.get("call")
    if state is None or not isinstance(call, dict) or state.get("server") != server:
        return None
    issued = cast(Json, call)
    if issued.get("tool") != tool:
        note = (
            f"memini: not captured: this is not the call handed out (mcp__{server}__"
            f"{issued.get('tool')}). Make that call exactly as given, or run `next`."
        )
        return hook_output(str(name), note, replace=False)

    arguments = found.get("tool_input")
    body: Json = {
        "call_id": issued["id"],
        "tool": tool,
        "arguments": arguments if isinstance(arguments, dict) else {},
    }
    if name == "PostToolUseFailure":
        error = found.get("error")
        body["error"] = error if isinstance(error, str) and error else "the call failed"
    else:
        body["result"] = found.get("tool_response")
    try:
        answer = call_api(config, "POST", run_path(state, "responses"), body)
    except Failure as failure:
        note = f"memini: the response was not stored: {failure}. Run `next` to go on."
        return hook_output(str(name), note, found.get("tool_response"), replace=True)
    state["call"] = None
    write_state(state)
    return hook_output(str(name), receipt_text(answer), found.get("tool_response"), replace=True)


def command_hook(_args: argparse.Namespace) -> int:
    try:
        output = run_hook(sys.stdin.read())
    except Failure as failure:
        # A broken configuration: say so to the person, never block the agent's call.
        print(f"memini capture hook: {failure}", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output))
    return 0


# --- Entry ---------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memini_agent.py",
        description="Run a Memini agent intake from this session; see the memini-intake skill.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start an agent run of a stream")
    start.add_argument("workspace")
    start.add_argument("source")
    start.add_argument("stream", help="the stream key, such as a channel id")
    start.add_argument(
        "--server", required=True, help="the MCP server as Claude Code names it (mcp__NAME__…)"
    )
    start.add_argument("--server-name", help="the name the server reports (serverInfo.name)")
    start.add_argument("--server-version", help="the version the server reports")
    commands.add_parser("next", help="the next call to make")
    commands.add_parser("finish", help="parse and ingest what was captured")
    commands.add_parser("status", help="the run in progress")
    commands.add_parser("hook", help="the capture hook (stdin: a Claude Code hook event)")
    return parser


def _fail(message: str) -> NoReturn:
    print(f"memini: {message}", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "hook":
        return command_hook(args)
    try:
        config = Config.load(config_path())
        handlers = {
            "start": command_start,
            "next": command_next,
            "finish": command_finish,
            "status": command_status,
        }
        return handlers[args.command](args, config)
    except Failure as failure:
        _fail(str(failure))


if __name__ == "__main__":
    if sys.version_info < MIN_PYTHON:
        print("memini_agent.py needs Python 3.11 or later", file=sys.stderr)
        raise SystemExit(0 if sys.argv[1:2] == ["hook"] else 1)
    raise SystemExit(main())
