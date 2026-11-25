import email.message
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from redaction_helpers import MEMINI

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/memini-server.py"


def load_manager(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("memini_server", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(monkeypatch: pytest.MonkeyPatch, module: ModuleType, state: Path, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--state-dir", str(state), *args])
    assert module.main() == 0


def test_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    launcher = package / SCRIPT.name
    shutil.copyfile(SCRIPT, launcher)
    module = load_manager(launcher)
    # Unit fixtures stay independent of development-branch Compose pins and shallow Git clones.
    contents = b"services: {}\n"
    for name in ("compose.yaml", "compose.release.yaml"):
        (package / name).write_bytes(contents)
        monkeypatch.setitem(module.COMPOSE_FILES, name, hashlib.sha256(contents).hexdigest())
    state = tmp_path / "new server"
    calls: list[tuple[list[str], dict[str, str]]] = []
    cached = False

    def docker(
        argv: list[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal cached
        calls.append((argv, dict(env)))
        response = ""
        if argv[-1] == "list" and cached:
            response = "NAME ID SIZE\nqwen3-embedding:0.6b c690 639MB\n"
        if argv[-2] == "pull":
            cached = True
        if argv[-3:] == ["port", "memini", "6364"]:
            response = "127.0.0.1:36364\n"
        return subprocess.CompletedProcess(argv, 0, response)

    monkeypatch.setattr(subprocess, "run", docker)

    def locate(_name: str) -> str:
        return "/fake/docker"

    monkeypatch.setattr(shutil, "which", locate)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "some-other-project")
    monkeypatch.setenv("MEMINI_DB_PASSWORD", "ambient")
    monkeypatch.delenv("MEMINI_TOKEN", raising=False)
    monkeypatch.delenv("MEMINI_TOKEN_FILE", raising=False)
    invoke(monkeypatch, module, state, "status")
    invoke(monkeypatch, module, state, "stop")
    assert not calls
    assert not state.exists()

    invoke(monkeypatch, module, state, "start", "--http-port", "0")
    settings = (state / ".env").read_bytes()
    assert b"MEMINI_HTTP_PORT=0" in settings
    assert b"ambient" not in settings
    if os.name != "nt":
        assert (state / ".env").stat().st_mode & 0o077 == 0
    invoke(monkeypatch, module, state, "stop")
    assert calls[-1][0][-1] == "stop"
    invoke(monkeypatch, module, state, "start")
    assert sum("pull" in args for args, _ in calls) == 1
    assert (state / ".env").read_bytes() == settings
    assert len({args[args.index("--project-name") + 1] for args, _ in calls}) == 1
    assert all(
        "COMPOSE_PROJECT_NAME" not in env and "MEMINI_DB_PASSWORD" not in env for _, env in calls
    )
    assert all("@sha256:" in env["MEMINI_RELEASE_IMAGE"] for _, env in calls)
    with pytest.raises(ValueError, match="update MEMINI_HTTP_PORT"):
        invoke(monkeypatch, module, state, "start", "--http-port", "12345")

    token = tmp_path / "fixture.token"
    token.write_text("fixture-value\r\n", encoding="utf-16")
    monkeypatch.setenv("MEMINI_TOKEN_FILE", str(token))
    invoke(monkeypatch, module, state, "cli", "--version")
    argv, env = calls[-1]
    assert argv[-5:] == ["-e", "MEMINI_TOKEN", "memini", "memini", "--version"]
    assert env["MEMINI_TOKEN"] == token.read_text(encoding="utf-16").strip()
    assert "fixture-value" not in argv
    assert "MEMINI_TOKEN_FILE" not in env


def test_existing_directory_is_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = load_manager(SCRIPT)
    original = tmp_path / ".env"
    original.write_text("existing project")
    for action in ("start", "stop", "status", "cli"):
        with pytest.raises(ValueError, match="no managed server"):
            invoke(monkeypatch, module, tmp_path, action)
    assert list(tmp_path.iterdir()) == [original]
    assert original.read_text() == "existing project"


def test_packaged_assets_verified_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packaged = tmp_path / "distribution"
    packaged.mkdir()
    launcher = packaged / SCRIPT.name
    shutil.copyfile(SCRIPT, launcher)
    (packaged / "compose.yaml").write_text("wrong release")
    state = tmp_path / "state"
    module = load_manager(launcher)
    invoke(monkeypatch, module, state, "path")
    assert capsys.readouterr().out.strip() == str(packaged)
    with pytest.raises(ValueError, match="checksum mismatch"):
        invoke(monkeypatch, module, state, "start")
    assert not state.exists()


# --- Output for programs (--json) and the MCP switch ---------------------------------------------
#
# The objects below are a contract with the programs that parse them (docs/install.md): a test
# that has to change for a renamed field or another type also has to raise SCHEMA.

OK = {"schema": 1, "ok": True, "error": None}
STATUS_FIELDS = [
    "schema",
    "installed",
    "running",
    "api_url",
    "mcp_url",
    "mcp_enabled",
    "image",
    "directory",
    "docker",
    "services",
]
UP_MEMINI = ("up", "-d", "--no-build", "--wait", "memini")


class FakeDocker:
    """Stands in for ``subprocess.run``: records each command, answers ``ps`` with ``rows`` and
    ``ollama list`` with the model, and raises ``fail`` instead while it is set."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rows: list[dict[str, object]] = []
        self.one_array = False
        self.fail: Exception | None = None
        self.token = ""
        """What the service's ``bootstrap`` prints."""
        self.listing = "HEAD\nobjects\n"
        """What ``ls`` in the service's container prints of a mounted folder."""
        self.mount_fails = False
        """Whether ``up`` fails while a local folder is mounted, as an unshared one does."""
        self.locked_key = False
        """Whether ``ssh-keygen -y`` refuses the key, as it does a key with a passphrase."""
        self.scanned = "git.example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIsynthetichost\n"
        """What ``ssh-keyscan`` prints."""

    def __call__(
        self, argv: list[str], *, env: dict[str, str] | None = None, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.fail is not None:
            raise self.fail
        response = ""
        if self.mount_fails and "up" in argv and any("compose.local.yaml" in a for a in argv):
            raise subprocess.CalledProcessError(1, argv)
        if argv[-3:-1] == ["ls", "-A"]:
            response = self.listing
        if argv[1:4] == ["-y", "-P", ""]:
            if self.locked_key:
                raise subprocess.CalledProcessError(255, argv)
            response = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIsynthetickey\n"
        if argv[1:3] == ["-l", "-f"]:
            known = argv[3].endswith("known_hosts")
            response = (
                "256 SHA256:hostprint git.example.org (ED25519)\n"
                if known
                else "256 SHA256:keyprint no comment (ED25519)\n"
            )
        if argv[1:3] == ["-T", "10"]:
            response = self.scanned
        if argv[-4:] == ["ps", "--all", "--format", "json"]:
            lines = "".join(json.dumps(row) + "\n" for row in self.rows)
            response = json.dumps(self.rows) if self.one_array else lines
        if argv[-1] == "list":
            response = "NAME ID SIZE\nqwen3-embedding:0.6b c690 639MB\n"
        if "bootstrap" in argv:
            response = self.token + "\n"
        return subprocess.CompletedProcess(argv, 0, response)

    def ran(self, *command: str) -> bool:
        return any(argv[-len(command) :] == list(command) for argv in self.calls)


class FakeService:
    """Stands in for ``mcp_served``: gives ``answer`` and records the addresses it was asked at."""

    def __init__(self) -> None:
        self.answer: bool | None = None
        self.asked: list[str] = []

    def __call__(self, api_url: str) -> bool | None:
        self.asked.append(api_url)
        return self.answer


@dataclass(frozen=True)
class Printed:
    code: int
    value: dict[str, Any]
    text: str
    """Everything the command wrote: stdout, then stderr."""


def container(
    service: str, state: str = "running", health: str = "healthy", port: int | None = None
) -> dict[str, object]:
    """A row of ``docker compose ps --format json``: the fields the manager reads, and one it has
    no reason to print."""
    publishers: list[dict[str, object]] = []
    if port is not None:
        publishers.append(
            {"URL": "127.0.0.1", "TargetPort": 6364, "PublishedPort": port, "Protocol": "tcp"}
        )
    return {
        "Service": service,
        "State": state,
        "Health": health,
        "Image": f"registry.invalid/{service}:9",
        "Publishers": publishers,
        "Labels": "synthetic=label",
    }


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """The launcher as a package holds it, next to compose files of this test."""
    package = tmp_path / "package"
    package.mkdir()
    launcher = package / SCRIPT.name
    shutil.copyfile(SCRIPT, launcher)
    module = load_manager(launcher)
    contents = b"services: {}\n"
    for name in ("compose.yaml", "compose.release.yaml"):
        (package / name).write_bytes(contents)
        monkeypatch.setitem(module.COMPOSE_FILES, name, hashlib.sha256(contents).hexdigest())
    return module


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()

    def locate(_name: str) -> str:
        return "/fake/docker"

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(shutil, "which", locate)
    return fake


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, manager: ModuleType) -> FakeService:
    fake = FakeService()
    monkeypatch.setattr(manager, "mcp_served", fake)
    return fake


@pytest.fixture
def state(manager: ModuleType, tmp_path: Path) -> Path:
    """The state directory of a managed server whose API is to be published on port 6399."""
    directory = (tmp_path / "server").resolve()
    manager.initialize(directory, 6399)
    return directory


@pytest.fixture
def launch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], manager: ModuleType
) -> Callable[..., Printed]:
    """Runs a command with ``--json`` and checks the one promise of every such command: stdout
    holds one JSON object on one line, and nothing else."""

    def run(directory: Path, *args: str) -> Printed:
        argv = [str(SCRIPT), "--state-dir", str(directory), *args, "--json"]
        monkeypatch.setattr(sys, "argv", argv)
        capsys.readouterr()
        code = manager.main()
        captured = capsys.readouterr()
        assert captured.out.endswith("\n")
        assert captured.out.count("\n") == 1
        value: dict[str, Any] = json.loads(captured.out)
        assert isinstance(value, dict)
        return Printed(code, value, captured.out + captured.err)

    return run


def test_status_json_without_a_server_says_whether_docker_is_there(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    state = tmp_path / "nothing here"
    nothing: dict[str, Any] = {
        "schema": 1,
        "installed": False,
        "running": False,
        "api_url": None,
        "mcp_url": None,
        "mcp_enabled": None,
        "image": None,
        "directory": None,
        "docker": {"available": True, "error": None},
        "services": [],
    }

    printed = launch(state, "status")

    assert (printed.code, printed.value) == (0, nothing)
    assert list(printed.value) == STATUS_FIELDS
    # Nothing to list yet, so the engine is asked whether it is there.
    assert docker.calls == [["/fake/docker", "version", "--format", "{{.Server.Version}}"]]

    docker.fail = subprocess.CalledProcessError(1, "docker")
    printed = launch(state, "status")
    assert printed.code == 0
    assert printed.value["docker"] == {
        "available": False,
        "error": "docker failed with exit status 1; start the Docker engine, with Compose 2.24"
        " or later",
    }

    docker.fail = subprocess.TimeoutExpired("docker", 20)
    printed = launch(state, "status")
    assert printed.code == 0
    assert printed.value["docker"] == {
        "available": False,
        "error": "docker did not answer in 20 seconds",
    }

    def missing(_name: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", missing)
    printed = launch(state, "status")
    assert printed.code == 0
    assert printed.value == nothing | {
        "docker": {
            "available": False,
            "error": "the docker command was not found; install Docker with Compose 2.24 or later",
        }
    }
    assert not state.exists()


@pytest.mark.parametrize("one_array", [False, True], ids=["a-line-each", "one-array"])
def test_status_json_of_a_running_server_gives_the_published_address(
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
    one_array: bool,
) -> None:
    docker.rows = [container("ollama"), container("memini", port=36364), container("db")]
    docker.one_array = one_array  # as Compose printed its answer before 2.21

    printed = launch(state, "status")

    assert printed.code == 0
    assert printed.value == {
        "schema": 1,
        "installed": True,
        "running": True,
        # What Docker published, not the 6399 of the settings.
        "api_url": "http://127.0.0.1:36364",
        "mcp_url": "http://127.0.0.1:36364/mcp",
        "mcp_enabled": True,
        "image": "registry.invalid/memini:9",
        "directory": str(state),
        "docker": {"available": True, "error": None},
        "services": [
            {"name": "db", "state": "running", "health": "healthy"},
            {"name": "memini", "state": "running", "health": "healthy"},
            {"name": "ollama", "state": "running", "health": "healthy"},
        ],
    }
    assert list(printed.value) == STATUS_FIELDS
    assert docker.calls[-1][-4:] == ["ps", "--all", "--format", "json"]
    # The private settings hold the database password: it is in no output, nor is anything else
    # of Docker's answer.
    password = manager.setting(state / ".env", "MEMINI_DB_PASSWORD")
    assert len(password) == 48
    assert password not in printed.text
    assert "synthetic=label" not in printed.text


def test_status_json_of_a_stopped_server_gives_the_configured_address(
    manager: ModuleType,
    tmp_path: Path,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    docker.rows = [container(name, "exited", "") for name in ("db", "memini", "ollama")]

    stopped = launch(state, "status").value

    assert (stopped["installed"], stopped["running"]) == (True, False)
    assert stopped["api_url"] == "http://127.0.0.1:6399"
    assert stopped["mcp_url"] == "http://127.0.0.1:6399/mcp"
    assert stopped["services"][1] == {"name": "memini", "state": "exited", "health": None}

    # Before the first container, the image is the one a start runs.
    docker.rows = []
    assert launch(state, "status").value["image"] == manager.IMAGE

    # Docker is down: still an answer, from the settings alone.
    docker.fail = subprocess.CalledProcessError(1, "docker")
    down = launch(state, "status")
    assert down.code == 0
    assert down.value["docker"]["available"] is False
    assert (down.value["running"], down.value["services"]) == (False, [])
    assert down.value["api_url"] == "http://127.0.0.1:6399"
    assert down.value["directory"] == str(state)

    # Port 0: Docker chooses the port at every start, so a stopped server has no address.
    docker.fail = None
    any_port = (tmp_path / "any port").resolve()
    manager.initialize(any_port, 0)
    unknown = launch(any_port, "status").value
    assert (unknown["api_url"], unknown["mcp_url"], unknown["mcp_enabled"]) == (None, None, True)


def test_start_and_stop_print_one_result_object(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    state = tmp_path / "new server"

    started = launch(state, "start", "--http-port", "0")

    assert (started.code, started.value) == (0, OK)
    assert docker.ran("up", "-d", "--no-build", "--wait")
    assert not docker.ran("port", "memini", "6364")  # the address is for status to give
    assert b"MEMINI_HTTP_PORT=0" in (state / ".env").read_bytes()

    stopped = launch(state, "stop")
    assert (stopped.code, stopped.value) == (0, OK)
    assert docker.calls[-1][-1] == "stop"

    docker.fail = subprocess.CalledProcessError(17, "docker")
    failed = launch(state, "stop")
    assert failed.code == 17
    assert failed.value == {
        "schema": 1,
        "ok": False,
        "error": "docker compose failed with exit status 17",
    }


def test_a_refusal_is_a_result_object_and_exit_status_1(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    for command in (("start",), ("stop",), ("mcp", "off")):
        refused = launch(foreign, *command)
        assert refused.code == 1, command
        assert refused.value["schema"] == 1
        assert refused.value["ok"] is False
        assert refused.value["error"].startswith("no managed server at ")
    assert list(foreign.iterdir()) == []
    assert docker.calls == []

    # Nothing to stop is no failure.
    nothing = launch(tmp_path / "nothing here", "stop")
    assert (nothing.code, nothing.value) == (0, OK)


def test_uninstall_removes_the_containers_the_volumes_and_the_state(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    removed = launch(state, "uninstall", "--yes")

    assert (removed.code, removed.value) == (0, OK)
    assert docker.calls[-1][-3:] == ["down", "--volumes", "--remove-orphans"]
    assert docker.calls[-1][3].startswith("memini-managed-")  # the project of this server only
    assert not state.exists()

    # Nothing left to remove is no failure, and Docker is not asked.
    calls = len(docker.calls)
    again = launch(state, "uninstall", "--yes")
    assert (again.code, again.value) == (0, OK)
    assert len(docker.calls) == calls


def test_uninstall_keeps_the_state_when_docker_fails(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.fail = subprocess.CalledProcessError(3, "docker")

    failed = launch(state, "uninstall", "--yes")

    assert failed.code == 3
    assert failed.value == {
        "schema": 1,
        "ok": False,
        "error": "docker compose failed with exit status 3",
    }
    # The secrets of the database stay with it, so another try can still reach it.
    assert (state / ".env").is_file()


def test_uninstall_needs_yes_and_a_directory_of_the_manager(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("a file of someone else\n")

    refused = launch(foreign, "uninstall", "--yes")

    assert refused.code == 1
    assert refused.value["error"] == (
        f"no managed server at {foreign.resolve()}; nothing was removed"
    )
    assert (foreign / "keep.txt").is_file()
    with pytest.raises(SystemExit) as unconfirmed:
        launch(foreign, "uninstall")
    assert unconfirmed.value.code == 2
    assert docker.calls == []


def test_bootstrap_prints_the_first_admin_token_as_one_object(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.token = MEMINI

    created = launch(state, "bootstrap")

    assert created.code == 0
    assert created.value == {"schema": 1, "ok": True, "error": None, "token": MEMINI}
    argv = docker.calls[-1]
    assert argv[-7:] == ["exec", "-T", "memini", "memini", "bootstrap", "--name", argv[-1]]
    assert "--force" not in argv
    # The token travels on the pipe only: no command line holds it.
    assert all(MEMINI not in part for call in docker.calls for part in call)


def test_bootstrap_passes_force_on_only_when_asked(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.token = MEMINI

    forced = launch(state, "bootstrap", "--force")

    assert forced.value["token"] == MEMINI
    assert docker.calls[-1][-1] == "--force"


def test_bootstrap_gives_the_services_reason_when_it_refuses(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    refusal = "memini: an active admin token exists (memini_Fake0Fak); pass --force"
    docker.fail = subprocess.CalledProcessError(1, "docker", stderr=f"\n{refusal}\n")

    refused = launch(state, "bootstrap")

    assert refused.code == 1
    assert refused.value == {"schema": 1, "ok": False, "error": refusal, "token": None}


@pytest.mark.parametrize("printed", ["", "not a token", MEMINI + " and more", MEMINI[:-1]])
def test_bootstrap_refuses_output_that_is_not_one_token(
    state: Path, docker: FakeDocker, launch: Callable[..., Printed], printed: str
) -> None:
    docker.token = printed

    refused = launch(state, "bootstrap")

    assert refused.code == 1
    assert refused.value["ok"] is False
    assert refused.value["token"] is None
    assert refused.value["error"] == "the service's bootstrap did not print a token"
    assert printed == "" or printed not in refused.text


def test_bootstrap_needs_a_managed_server(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    missing = launch(tmp_path / "nothing here", "bootstrap")

    assert missing.code == 1
    assert missing.value["token"] is None
    assert missing.value["error"].startswith("no managed server at ")
    assert docker.calls == []


def test_bootstrap_without_json_prints_the_token_alone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
) -> None:
    docker.token = MEMINI

    invoke(monkeypatch, manager, state, "bootstrap")

    assert capsys.readouterr().out == MEMINI + "\n"


def test_mcp_off_and_on_set_the_switch_and_restart_a_running_service(
    state: Path, docker: FakeDocker, service: FakeService, launch: Callable[..., Printed]
) -> None:
    settings = state / ".env.memini"
    settings.write_text("MEMINI_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    docker.rows = [container("db"), container("memini", port=36364), container("ollama")]

    service.answer = False
    switched = launch(state, "mcp", "off")

    assert (switched.code, switched.value) == (0, OK)
    assert settings.read_text(encoding="utf-8") == (
        "MEMINI_LOG_LEVEL=DEBUG\nMEMINI_MCP_ENABLED=false\n"
    )
    assert docker.ran(*UP_MEMINI)
    assert service.asked == ["http://127.0.0.1:36364"]
    status = launch(state, "status").value
    assert (status["mcp_enabled"], status["mcp_url"]) == (False, None)
    assert status["api_url"] == "http://127.0.0.1:36364"  # the HTTP API stays
    assert launch(state, "mcp", "status").value == status

    docker.calls.clear()
    service.answer = True
    switched = launch(state, "mcp", "on")

    assert (switched.code, switched.value) == (0, OK)
    assert settings.read_text(encoding="utf-8") == (
        "MEMINI_LOG_LEVEL=DEBUG\nMEMINI_MCP_ENABLED=true\n"
    )
    assert docker.ran(*UP_MEMINI)
    status = launch(state, "status").value
    assert (status["mcp_enabled"], status["mcp_url"]) == (True, "http://127.0.0.1:36364/mcp")


def test_mcp_switch_of_a_stopped_server_waits_for_its_next_start(
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    service: FakeService,
    launch: Callable[..., Printed],
) -> None:
    docker.rows = [container("memini", "exited", "")]

    switched = launch(state, "mcp", "off")

    assert (switched.code, switched.value) == (0, OK)
    assert manager.mcp_enabled(state) is False
    assert not docker.ran(*UP_MEMINI)
    assert service.asked == []
    if os.name != "nt":
        assert (state / ".env.memini").stat().st_mode & 0o077 == 0

    # The private settings may name another settings file: the switch goes where Compose reads.
    with (state / ".env").open("a", encoding="utf-8") as private:
        private.write("MEMINI_ENV_FILE=service.env\n")
    assert launch(state, "mcp", "off").value == OK
    assert (state / "service.env").read_text(encoding="utf-8") == "MEMINI_MCP_ENABLED=false\n"
    assert launch(state, "status").value["mcp_enabled"] is False


def test_mcp_switch_is_put_back_when_the_service_does_not_apply_it(
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    service: FakeService,
    launch: Callable[..., Printed],
) -> None:
    """An image from before the switch ignores the setting: ``ok`` would not be true."""
    docker.rows = [container("memini", port=36364)]
    service.answer = True  # it serves MCP after the restart all the same

    refused = launch(state, "mcp", "off")

    assert refused.code == 1
    assert refused.value["ok"] is False
    assert "did not apply MEMINI_MCP_ENABLED" in refused.value["error"]
    assert docker.ran(*UP_MEMINI)
    assert not (state / ".env.memini").exists()  # as it was: no file
    assert launch(state, "status").value["mcp_enabled"] is True

    # No answer, or a refusal by the rate limit: nothing contradicts the setting.
    service.answer = None
    assert launch(state, "mcp", "off").value == OK
    assert manager.mcp_enabled(state) is False


def test_mcp_switch_changes_nothing_while_docker_does_not_answer(
    state: Path, docker: FakeDocker, service: FakeService, launch: Callable[..., Printed]
) -> None:
    docker.fail = subprocess.CalledProcessError(1, "docker")

    refused = launch(state, "mcp", "off")

    assert refused.code == 1
    assert refused.value == {
        "schema": 1,
        "ok": False,
        "error": "docker compose failed with exit status 1",
    }
    assert not (state / ".env.memini").exists()
    assert service.asked == []


def test_mcp_commands_tell_a_person_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    service: FakeService,
) -> None:
    def said(*args: str) -> str:
        capsys.readouterr()
        invoke(monkeypatch, manager, state, *args)
        return capsys.readouterr().out

    docker.rows = [container("memini", port=36364)]
    assert said("mcp", "status") == "MCP is on at http://127.0.0.1:36364/mcp\n"
    service.answer = False
    assert said("mcp", "off") == "MCP is off\n"
    assert said("mcp", "status") == "MCP is off\n"
    service.answer = True
    assert said("mcp", "on") == "MCP is on at http://127.0.0.1:36364/mcp\n"

    docker.rows = [container("memini", "exited", "")]
    assert said("mcp", "off") == "MCP will be off when the server starts\n"
    assert said("mcp", "status") == "MCP is off\n"


def test_setting_reads_one_value_of_an_env_file_as_compose_reads_it(tmp_path: Path) -> None:
    module = load_manager(SCRIPT)
    path = tmp_path / ".env"
    path.write_text(
        '# NAME=commented\nNAME=first\nOTHER=plain # a comment\nexport NAME = "second value" # x\n',
        encoding="utf-8",
    )

    assert module.setting(path, "NAME") == "second value"  # the last line wins
    assert module.setting(path, "OTHER") == "plain"
    assert module.setting(path, "ABSENT") is None
    assert module.setting(tmp_path / "absent", "NAME") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", True),
        ("# MEMINI_MCP_ENABLED=false\n", True),
        ("MEMINI_MCP_ENABLED=\n", True),
        ("MEMINI_MCP_ENABLED=TRUE\n", True),
        ("MEMINI_MCP_ENABLED=false\n", False),
        ("MEMINI_MCP_ENABLED=true\nMEMINI_MCP_ENABLED=0\n", False),
        ("MEMINI_MCP_ENABLED=perhaps\n", None),
    ],
)
def test_the_switch_is_read_as_the_service_reads_it(
    tmp_path: Path, text: str, expected: bool | None
) -> None:
    module = load_manager(SCRIPT)
    (tmp_path / ".env.memini").write_text(text, encoding="utf-8")

    assert module.mcp_enabled(tmp_path) is expected


def test_mcp_served_tells_a_switched_off_endpoint_from_one_that_asks_for_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_manager(SCRIPT)
    outcome: list[tuple[int, bytes] | OSError | None] = [None]
    asked: list[str] = []

    def answer(
        _opener: object, url: str, data: object = None, timeout: float | None = None
    ) -> io.BytesIO:
        asked.append(url)
        result = outcome[0]
        if result is None:
            return io.BytesIO(b"{}")
        if isinstance(result, OSError):
            raise result
        status, body = result
        raise urllib.error.HTTPError(
            url, status, "synthetic", email.message.Message(), io.BytesIO(body)
        )

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", answer)

    def served(result: tuple[int, bytes] | OSError | None) -> bool | None:
        outcome[0] = result
        return module.mcp_served("http://127.0.0.1:6399")

    assert served((404, b'{"error": {"code": "mcp_disabled", "message": "Off."}}')) is False
    assert served((401, b'{"error": {"code": "unauthorized"}}')) is True
    assert served((404, b"<html>another server</html>")) is True
    assert served((404, b"[]")) is True
    assert served(None) is True
    assert served((429, b'{"error": {"code": "rate_limited"}}')) is None
    assert served(urllib.error.URLError("synthetic: connection refused")) is None
    assert set(asked) == {"http://127.0.0.1:6399/mcp"}


# --- Folders of this computer and git access ------------------------------------------------------

# Not a key: the markers of one around a word, split so that no secret scanner takes the source
# for a key.
SYNTHETIC_KEY = (
    "-----BEGIN OPENSSH PRIVATE"
    + " KEY-----\nsynthetic\n-----END OPENSSH PRIVATE"
    + (" KEY-----\n")
)
SYNTHETIC_GIT_TOKEN = "synthetic-git-token-0001"  # noqa: S105 - fake, checked by the test


def working_copy(parent: Path, name: str, branch: str = "trunk") -> Path:
    """A folder that looks like a git working copy, with ``branch`` checked out."""
    folder = parent / name
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return folder


def stdin_of(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode())))


def local_compose(state: Path) -> dict[str, Any]:
    lines = (state / "compose.local.yaml").read_text().splitlines()
    assert lines[0].startswith("# ")
    value: dict[str, Any] = json.loads("\n".join(lines[1:]))
    return value


def up_calls(docker: FakeDocker) -> list[list[str]]:
    return [argv for argv in docker.calls if argv[-len(UP_MEMINI) :] == list(UP_MEMINI)]


def test_repos_add_mounts_a_folder_read_only_and_names_its_location(
    tmp_path: Path, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.rows = [container("memini", port=6399)]
    folder = working_copy(tmp_path / "work", "Harbour App")

    added = launch(state, "repos", "add", str(folder))

    assert added.code == 0, added.text
    repository = {
        "name": "harbour-app",
        "path": str(folder.resolve()),
        "location": "/srv/local/harbour-app",
        "branch": "trunk",
        "present": True,
    }
    assert added.value == OK | {
        "repository": repository,
        "warning": None,
        "repositories": [repository],
    }
    service = local_compose(state)["services"]["memini"]
    assert service["environment"] == {"MEMINI_GIT_LOCAL_ROOTS": "/srv/local"}
    assert service["volumes"] == [
        {
            "type": "bind",
            "source": str(folder.resolve()),
            "target": "/srv/local/harbour-app",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    # The running service was recreated with the mount, and the mount was looked into.
    assert str(state / "compose.local.yaml") in up_calls(docker)[-1]
    assert docker.ran("exec", "-T", "memini", "ls", "-A", "/srv/local/harbour-app")

    # The same folder again changes nothing; another of the same name gets a name of its own.
    assert launch(state, "repos", "add", str(folder)).value["repositories"] == [repository]
    other = working_copy(tmp_path / "elsewhere", "Harbour App", branch="main")
    both = launch(state, "repos", "add", str(other)).value["repositories"]
    assert [repo["name"] for repo in both] == ["harbour-app", "harbour-app-2"]
    assert [repo["name"] for repo in launch(state, "repos", "list").value["repositories"]] == [
        "harbour-app",
        "harbour-app-2",
    ]

    removed = launch(state, "repos", "remove", "harbour-app")
    assert [repo["name"] for repo in removed.value["repositories"]] == ["harbour-app-2"]
    launch(state, "repos", "remove", "harbour-app-2")
    assert not (state / "compose.local.yaml").exists()
    assert not (state / "local-repos.json").exists()
    gone = launch(state, "repos", "remove", "harbour-app")
    assert (gone.code, gone.value["error"]) == (1, "no folder is named harbour-app")


def test_repos_add_writes_a_dollar_of_the_path_as_compose_reads_it_literally(
    tmp_path: Path, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.rows = [container("memini", port=6399)]
    folder = working_copy(tmp_path / "work", "price$HOME")

    added = launch(state, "repos", "add", str(folder))

    assert added.code == 0, added.text
    # Compose interpolates $ in a value; $$ is one literal $.
    volume = local_compose(state)["services"]["memini"]["volumes"][0]
    assert volume["source"] == str(folder.resolve()).replace("$", "$$")
    # The list keeps the path as it is.
    assert added.value["repository"]["path"] == str(folder.resolve())
    assert added.value["repository"]["name"] == "price-home"


def test_repos_add_refuses_what_is_not_the_folder_of_a_repository(
    tmp_path: Path, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere/.git/worktrees/linked\n")

    for given, reason in [
        ("relative/folder", "the folder is an absolute path"),
        (str(tmp_path / "missing"), f"there is no folder at {tmp_path / 'missing'}"),
        (str(plain), f"{plain.resolve()} is not a git repository"),
        (str(linked), f"{linked.resolve()} is a linked worktree or a submodule"),
    ]:
        refused = launch(state, "repos", "add", given)
        assert refused.code == 1, given
        assert refused.value["ok"] is False
        assert str(refused.value["error"]).startswith(reason), refused.value
    assert not (state / "local-repos.json").exists()
    assert docker.calls == []


def test_repos_add_takes_back_a_folder_docker_does_not_share(
    tmp_path: Path, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.rows = [container("memini", port=6399)]
    folder = working_copy(tmp_path, "outside")
    docker.listing = ""  # Docker Desktop mounts a folder it does not share as an empty one

    empty = launch(state, "repos", "add", str(folder))

    assert empty.code == 1
    assert "File sharing" in str(empty.value["error"])
    assert not (state / "compose.local.yaml").exists()
    # The service was made again without the folder.
    assert len(up_calls(docker)) == 2
    assert str(state / "compose.local.yaml") not in up_calls(docker)[-1]

    docker.listing = "HEAD\n"
    docker.mount_fails = True  # or Docker refuses to mount it at all
    refused = launch(state, "repos", "add", str(folder))
    assert refused.code == 1
    assert "File sharing" in str(refused.value["error"])
    assert launch(state, "repos", "list").value["repositories"] == []


def test_repos_add_to_a_stopped_server_waits_for_its_start(
    tmp_path: Path, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.rows = [container("memini", "exited", "")]

    added = launch(state, "repos", "add", str(working_copy(tmp_path, "app")))

    assert added.code == 0
    assert added.value["warning"] == "the server is stopped: it reads the folder once it starts"
    assert up_calls(docker) == []
    assert (state / "compose.local.yaml").is_file()


def test_git_ssh_key_keeps_the_key_and_the_host_keys_and_prints_fingerprints_only(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    docker.rows = [container("memini", port=6399)]
    stdin_of(monkeypatch, SYNTHETIC_KEY)

    printed = launch(state, "git", "ssh-key", "--host", "git.example.org")

    assert printed.code == 0, printed.text
    assert printed.value == OK | {
        "ssh": {
            "key": {"type": "ED25519", "fingerprint": "SHA256:keyprint"},
            "hosts": [
                {
                    "host": "git.example.org",
                    "keys": [{"type": "ED25519", "fingerprint": "SHA256:hostprint"}],
                }
            ],
        },
        "https": None,
    }
    assert "PRIVATE KEY" not in printed.text
    key = state / "secrets/git_ssh_key"
    assert key.read_text() == SYNTHETIC_KEY
    assert key.stat().st_mode & 0o777 == 0o600
    assert "ssh-ed25519" in (state / "secrets/git_ssh_known_hosts").read_text()
    env = state / ".env"
    assert manager.setting(env, "MEMINI_GIT_SSH_KEY_PATH") == "./secrets/git_ssh_key"
    assert manager.setting(env, "MEMINI_GIT_SSH_KNOWN_HOSTS_PATH") == (
        "./secrets/git_ssh_known_hosts"
    )
    assert manager.setting(env, "MEMINI_DB_PASSWORD")  # the other lines stay
    # Compose does not notice a changed secret file: the container is made anew.
    assert docker.ran("up", "-d", "--no-build", "--wait", "--force-recreate", "memini")
    assert launch(state, "git", "status").value["ssh"] == printed.value["ssh"]

    removed = launch(state, "git", "remove", "ssh")
    assert removed.value == OK | {"ssh": None, "https": None}
    assert not key.exists()
    assert manager.setting(env, "MEMINI_GIT_SSH_KEY_PATH") is None


def test_git_ssh_key_refuses_a_key_with_a_passphrase_a_silent_host_and_a_bad_host(
    monkeypatch: pytest.MonkeyPatch, state: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    docker.locked_key = True
    stdin_of(monkeypatch, SYNTHETIC_KEY)
    locked = launch(state, "git", "ssh-key", "--host", "git.example.org")
    assert locked.code == 1
    assert "passphrase" in str(locked.value["error"])

    docker.locked_key = False
    docker.scanned = ""
    stdin_of(monkeypatch, SYNTHETIC_KEY)
    silent = launch(state, "git", "ssh-key", "--host", "git.example.org")
    assert silent.value["error"] == "ssh-keyscan got no host key from git.example.org"

    stdin_of(monkeypatch, SYNTHETIC_KEY)
    option = launch(state, "git", "ssh-key", "--host=-oProxyCommand=x")
    assert option.value["error"] == "'-oProxyCommand=x' is not a host name"
    stdin_of(monkeypatch, SYNTHETIC_KEY)
    assert launch(state, "git", "ssh-key").value["error"] == "name at least one host with --host"
    stdin_of(monkeypatch, "not a key")
    assert launch(state, "git", "ssh-key", "--host", "git.example.org").value["error"] == (
        "stdin holds no ssh private key"
    )
    assert not (state / "secrets/git_ssh_key").exists()


def test_git_https_token_is_kept_for_its_hosts_and_never_printed(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    stdin_of(monkeypatch, SYNTHETIC_GIT_TOKEN + "\n")

    printed = launch(
        state,
        "git",
        "https-token",
        "--host",
        "git.example.org",
        "--host",
        "code.example.org",
        "--username",
        "oauth2",
    )

    assert printed.code == 0, printed.text
    assert printed.value["https"] == {
        "hosts": ["git.example.org", "code.example.org"],
        "username": "oauth2",
    }
    assert SYNTHETIC_GIT_TOKEN not in printed.text
    assert (state / "secrets/git_https_token").read_text() == SYNTHETIC_GIT_TOKEN
    assert manager.setting(state / ".env", "MEMINI_GIT_HTTPS_TOKEN_FILE") == (
        "./secrets/git_https_token"
    )
    settings = state / ".env.memini"
    assert manager.setting(settings, "MEMINI_GIT_HTTPS_TOKEN_HOSTS") == (
        "git.example.org,code.example.org"
    )
    # The server is stopped: it takes the token at its next start.
    assert not docker.ran("--force-recreate", "memini")

    removed = launch(state, "git", "remove", "https")
    assert removed.value["https"] is None
    assert manager.setting(settings, "MEMINI_GIT_HTTPS_TOKEN_HOSTS") is None
    assert not (state / "secrets/git_https_token").exists()

    stdin_of(monkeypatch, "two words")
    spaced = launch(state, "git", "https-token", "--host", "git.example.org")
    assert spaced.value["error"] == (
        "stdin holds no token: one line of printable characters, no spaces"
    )


def test_repos_and_git_need_a_managed_server(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    nothing = tmp_path / "nothing here"
    for command in (("repos", "list"), ("git", "status")):
        refused = launch(nothing, *command)
        assert refused.code == 1
        assert str(refused.value["error"]).startswith("no managed server at ")
    assert not nothing.exists()


# --- How much of the computer the search model uses ----------------------------------------------


def indexing_compose(state: Path) -> Any:
    text = (state / "compose.resources.yaml").read_text()
    return json.loads(text.split("\n", 1)[1])


def test_indexing_limits_the_model_to_half_the_cores_by_default(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    monkeypatch.setattr(manager, "cores", lambda: 8)

    before = launch(state, "indexing", "status")

    assert before.value == OK | {
        "indexing": {"mode": "half", "cpus": 4.0, "cores": 8, "runtime": "container"}
    }
    assert not (state / "compose.resources.yaml").exists()

    # A start writes the default, and every command adds its Compose file.
    assert launch(state, "start").code == 0
    assert indexing_compose(state) == {"services": {"ollama": {"cpus": 4.0}}}
    assert str(state / "compose.resources.yaml") in docker.calls[-1]


@pytest.mark.parametrize(
    ("mode", "available", "cpus"),
    [("background", 4, 1.0), ("background", 12, 2.0), ("half", 2, 1.0), ("half", 1, 1.0)],
)
def test_each_mode_sets_a_limit_from_the_cores(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
    mode: str,
    available: int,
    cpus: float,
) -> None:
    monkeypatch.setattr(manager, "cores", lambda: available)

    answer = launch(state, "indexing", "set", mode)

    assert answer.value["indexing"] == {
        "mode": mode,
        "cpus": cpus,
        "cores": available,
        "runtime": "container",
    }
    assert indexing_compose(state)["services"]["ollama"]["cpus"] == cpus


def test_full_takes_the_limit_away_and_a_running_model_is_recreated(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    launch: Callable[..., Printed],
) -> None:
    monkeypatch.setattr(manager, "cores", lambda: 8)
    docker.rows = [container("ollama", health=""), container("memini", port=6399)]
    launch(state, "indexing", "set", "background")

    full = launch(state, "indexing", "set", "full")

    assert full.value["indexing"] == {
        "mode": "full",
        "cpus": None,
        "cores": 8,
        "runtime": "container",
    }
    assert not (state / "compose.resources.yaml").exists()
    assert docker.ran("up", "-d", "--no-build", "--wait", "ollama")
    assert launch(state, "indexing", "status").value["indexing"]["mode"] == "full"


def test_indexing_needs_a_managed_server_and_a_mode_to_set(
    tmp_path: Path, docker: FakeDocker, launch: Callable[..., Printed]
) -> None:
    refused = launch(tmp_path / "nothing here", "indexing", "status")

    assert refused.code == 1
    assert refused.value["error"].startswith("no managed server at ")
    with pytest.raises(SystemExit):
        launch(tmp_path / "nothing here", "indexing", "set")
    with pytest.raises(SystemExit):
        launch(tmp_path / "nothing here", "indexing", "set", "turbo")
    assert docker.calls == []


class FakeHostOllama:
    """Stands in for Ollama on this computer: ``host_ollama`` answers from ``found`` and
    ``digest``, and ``pull_host_model`` gives it the model and counts the pulls."""

    def __init__(self, manager: ModuleType) -> None:
        self.manager = manager
        self.found = True
        self.has_model = True
        self.digest = "c690" + "0" * 60
        self.pulls = 0

    def answer(self) -> dict[str, object]:
        return {
            "found": self.found,
            "version": "0.34.2" if self.found else None,
            "model": self.manager.MODEL,
            "has_model": self.found and self.has_model,
            "digest": self.digest if self.found and self.has_model else None,
        }

    def pull(self) -> None:
        self.pulls += 1
        self.has_model = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, manager: ModuleType) -> FakeHostOllama:
    fake = FakeHostOllama(manager)
    monkeypatch.setattr(manager, "host_ollama", fake.answer)
    monkeypatch.setattr(manager, "pull_host_model", fake.pull)
    return fake


def test_embedding_status_answers_before_a_server_is_set_up(
    tmp_path: Path, docker: FakeDocker, host: FakeHostOllama, launch: Callable[..., Printed]
) -> None:
    printed = launch(tmp_path / "nothing here", "embedding", "status")

    assert printed.code == 0
    assert printed.value["indexing"] is None
    assert printed.value["host"]["found"] is True
    assert printed.value["host"]["has_model"] is True
    assert docker.calls == []


def test_a_first_start_with_the_model_on_this_computer_starts_no_ollama_service(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    tmp_path: Path,
    docker: FakeDocker,
    host: FakeHostOllama,
    launch: Callable[..., Printed],
) -> None:
    monkeypatch.setattr(manager, "cores", lambda: 8)
    host.has_model = False
    state = tmp_path / "new server"

    started = launch(state, "start", "--embedding", "host")

    assert (started.code, started.value) == (0, OK)
    assert host.pulls == 1  # the model is downloaded by Ollama on this computer
    assert not docker.ran("up", "-d", "--no-build", "--wait", "ollama")
    assert not any("ollama" in argv and "exec" in argv for argv in docker.calls)
    assert docker.ran("up", "-d", "--no-build", "--wait")
    resources = indexing_compose(state)
    assert resources["services"]["ollama"] == {"cpus": 4.0, "profiles": ["container-model"]}
    memini = resources["services"]["memini"]
    assert memini["environment"]["MEMINI_EMBEDDING_URL"] == "http://host.docker.internal:11434/v1"
    assert memini["environment"]["MEMINI_EMBEDDING_BATCH_SIZE"] == "32"
    assert memini["depends_on"] == {"ollama": {"condition": "service_healthy", "required": False}}
    assert memini["extra_hosts"] == ["host.docker.internal:host-gateway"]
    status = launch(state, "embedding", "status")
    assert status.value["indexing"]["runtime"] == "host"

    # The runtime is chosen at the first start only; a later start may not move it.
    moved = launch(state, "start", "--embedding", "container")
    assert moved.code == 1
    assert moved.value["error"] == "use `memini-server embedding container` to move the model"


def test_a_start_in_host_mode_without_ollama_says_how_to_get_it(
    tmp_path: Path, docker: FakeDocker, host: FakeHostOllama, launch: Callable[..., Printed]
) -> None:
    host.found = False

    refused = launch(tmp_path / "new server", "start", "--embedding", "host")

    assert refused.code == 1
    assert "https://ollama.com/download" in refused.value["error"]
    assert "brew install ollama" in refused.value["error"]
    assert not docker.ran("up", "-d", "--no-build", "--wait")


def test_moving_a_running_server_to_ollama_on_this_computer(
    monkeypatch: pytest.MonkeyPatch,
    manager: ModuleType,
    state: Path,
    docker: FakeDocker,
    host: FakeHostOllama,
    launch: Callable[..., Printed],
) -> None:
    monkeypatch.setattr(manager, "cores", lambda: 8)
    docker.rows = [container("ollama", health=""), container("memini", port=6399)]
    assert launch(state, "start").code == 0
    assert launch(state, "embedding", "status").value["indexing"]["runtime"] == "container"

    moved = launch(state, "embedding", "host")

    assert moved.code == 0
    assert moved.value["indexing"]["runtime"] == "host"
    assert docker.ran(*UP_MEMINI)
    assert docker.calls[-1][-2:] == ["stop", "ollama"]
    assert "memini" in indexing_compose(state)["services"]

    # Back to the container: the model service comes back before the service moves to it.
    back = launch(state, "embedding", "container")
    assert back.value["indexing"]["runtime"] == "container"
    assert docker.ran("up", "-d", "--no-build", "--wait", "ollama")
    assert docker.calls[-1][-5:] == list(UP_MEMINI)
    assert indexing_compose(state) == {"services": {"ollama": {"cpus": 4.0}}}


def test_another_build_of_the_model_on_this_computer_is_refused(
    state: Path, docker: FakeDocker, host: FakeHostOllama, launch: Callable[..., Printed]
) -> None:
    docker.rows = [container("ollama", health=""), container("memini", port=6399)]
    assert launch(state, "start").code == 0
    host.digest = "ffff" + "0" * 60  # the container lists c690

    refused = launch(state, "embedding", "host")

    assert refused.code == 1
    assert "another build" in refused.value["error"]
    assert launch(state, "embedding", "status").value["indexing"]["runtime"] == "container"


def test_embedding_takes_a_known_place_and_needs_a_server_to_move(
    tmp_path: Path, docker: FakeDocker, host: FakeHostOllama, launch: Callable[..., Printed]
) -> None:
    refused = launch(tmp_path / "nothing here", "embedding", "host")

    assert refused.code == 1
    assert refused.value["error"].startswith("no managed server at ")
    with pytest.raises(SystemExit):
        launch(tmp_path / "nothing here", "embedding", "gpu")
    with pytest.raises(SystemExit):
        launch(tmp_path / "nothing here", "start", "--embedding", "cloud")
    assert docker.calls == []
