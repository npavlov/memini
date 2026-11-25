#!/usr/bin/env python3
"""Manage the packaged Memini release using Docker Compose."""

import argparse
import codecs
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

COMPOSE_FILES = {
    "compose.yaml": "a9a6f377445eca908d12921396e4d40a61f460abf1727bd70651beb1fb288f6d",
    "compose.release.yaml": "dc4b77f7f527dad31d12b1c47cadabd40dca49d6526e5c18ef4fc48d10efc714",
}
IMAGE = (
    "ghcr.io/npavlov/memini:0.1.0"
    "@sha256:a55fed2b8db036a5b79a482887bebf875c39d3ac10da785407d15598e87021fc"
)
MODEL = "qwen3-embedding:0.6b"
MARKER = ".memini-server-v1"
# The objects --json prints (docs/install.md) are a contract with the programs that read them: a
# field that is renamed, removed or changes its type raises this number.
SCHEMA = 1
# The service's port in its container, and the default of the published one (compose.yaml).
API_PORT = 6364
# The service's settings (compose.yaml): this file in the state directory, unless MEMINI_ENV_FILE
# in the private settings names another. Compose passes its lines into the container.
SETTINGS = ".env.memini"
MCP_SWITCH = "MEMINI_MCP_ENABLED"
# Seconds status waits for Docker: an engine that is still starting can keep its client waiting.
DOCKER_TIMEOUT = 20
# A token of the service (src/memini/auth/tokens.py): what bootstrap must print, and nothing else.
TOKEN = re.compile(r"memini_[A-Za-z0-9_-]{43}")
# The name of the admin token `bootstrap` creates, as the service's list of tokens shows it.
BOOTSTRAP_NAME = "memini-server bootstrap"
# Seconds the service's bootstrap may take: one transaction in a database that is up.
BOOTSTRAP_TIMEOUT = 60


def resources() -> Path:
    directory = Path(__file__).resolve().parent
    if directory.name == "scripts" and not (directory / "compose.yaml").exists():
        return directory.parent
    return directory


def state_directory() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "Memini" / "Server"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Memini" / "Server"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "memini/server"


def initialize(directory: Path, port: int) -> None:
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Publish the complete first-run state at once. A concurrent start keeps the winner's secrets.
    with tempfile.TemporaryDirectory(dir=directory.parent) as scratch:
        staged = Path(scratch) / "state"
        staged.mkdir(mode=0o700)
        for filename, checksum in COMPOSE_FILES.items():
            data = (resources() / filename).read_bytes()
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError(f"release checksum mismatch for {filename}")
            (staged / filename).write_bytes(data)
        (staged / "secrets").mkdir()
        (staged / "secrets/unset").touch()
        settings = staged / ".env"
        settings.touch(mode=0o600)
        settings.write_text(
            f"MEMINI_DB_PASSWORD={secrets.token_hex(24)}\nMEMINI_DB_PORT=0\nMEMINI_HTTP_PORT={port}\n",
            encoding="utf-8",
        )
        (staged / MARKER).write_text(secrets.token_hex(16), encoding="ascii")
        if not directory.exists():
            try:
                staged.rename(directory)
            except OSError:
                if not (directory / MARKER).is_file():
                    raise


def assigned(line: str) -> str:
    """The variable a line of an env file sets; never a variable's name for a comment or a line
    that sets none."""
    name, separator, _ = line.strip().removeprefix("export ").partition("=")
    return name.strip() if separator else ""


def setting(path: Path, name: str) -> str | None:
    """The value of ``name`` in the env file at ``path``, from its last ``NAME=value`` line as
    Compose reads it; ``None`` without the file or the line. Nothing else leaves the file: the
    private settings hold the database password."""
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if assigned(line) == name:
            value = line.partition("=")[2].strip()
            quoted = value[:1] in ("'", '"')
            found = value[1:].partition(value[0])[0] if quoted else value.partition(" #")[0].strip()
    return found


def settings_file(directory: Path) -> Path:
    """The file whose lines Compose passes into the service's container."""
    return directory / (setting(directory / ".env", "MEMINI_ENV_FILE") or SETTINGS)


def mcp_enabled(directory: Path) -> bool | None:
    """Whether the service's settings switch its MCP endpoint on, as they do without a line for
    it; ``None`` for a value the service does not start with."""
    value = (setting(settings_file(directory), MCP_SWITCH) or "true").lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    return False if value in {"0", "false", "f", "no", "n", "off"} else None


def write_private(path: Path, text: str | None) -> None:
    """Put ``text`` at ``path`` in one step, for its owner only; ``None`` removes the file."""
    if text is None:
        path.unlink(missing_ok=True)
        return
    staged = path.with_name(path.name + ".new")
    staged.touch(mode=0o600)
    staged.write_text(text, encoding="utf-8")
    staged.replace(path)


def save_mcp(directory: Path, enabled: bool) -> str | None:
    """Set the MCP switch in the service's settings and keep every other line. Returns the text
    of the file as it was, ``None`` for no file, which :func:`write_private` puts back."""
    path = settings_file(directory)
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    kept = [line for line in (before or "").splitlines() if assigned(line) != MCP_SWITCH]
    value = "true" if enabled else "false"
    write_private(path, "\n".join([*kept, f"{MCP_SWITCH}={value}"]) + "\n")
    return before


def environment() -> dict[str, str]:
    """The environment of Docker's commands: this process's without the variables of Compose and
    of Memini, which the managed project takes from its own files, and with the pinned image."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("COMPOSE_", "MEMINI_"))
    }
    env["MEMINI_RELEASE_IMAGE"] = IMAGE
    return env


def objects(value: object) -> list[dict[str, object]]:
    """The objects in ``value`` if it is a decoded JSON array; none if it is anything else."""
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, object], item) for item in items if isinstance(item, dict)]


class Project:
    """The Compose project of the managed server in ``directory``."""

    def __init__(self, directory: Path, *, quiet: bool = False) -> None:
        nonce = (directory / MARKER).read_text(encoding="ascii")
        if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
            raise ValueError(f"invalid manager state in {directory}")
        identity = hashlib.sha256(
            os.fsencode(os.path.normcase(directory)) + nonce.encode()
        ).hexdigest()
        self.directory = directory
        # With --json, stdout carries the one object: what Docker prints there goes to stderr.
        self.quiet = quiet
        self.docker = shutil.which("docker")
        self.arguments = [
            "compose",
            "--project-name",
            f"memini-managed-{identity[:20]}",
            "--project-directory",
            str(directory),
            "--env-file",
            str(directory / ".env"),
        ]
        for filename in COMPOSE_FILES:
            self.arguments.extend(["-f", str(directory / filename)])
        self.env = environment()

    def run(self, *argv: str, capture: bool = False, timeout: float | None = None) -> str:
        if not self.docker:
            raise ValueError(
                "install Docker with Compose 2.24 or later and start the Docker engine"
            )
        result = subprocess.run(  # noqa: S603 - resolved docker executable, argument vector only
            [self.docker, *self.arguments, *argv],
            cwd=self.directory,
            env=self.env,
            check=True,
            stdout=subprocess.PIPE if capture else sys.stderr if self.quiet else None,
            text=capture,
            timeout=timeout,
        )
        return result.stdout or ""

    def services(self, timeout: float | None = None) -> list[dict[str, object]]:
        """What ``docker compose ps`` says of each container of the project, stopped ones too."""
        output = self.run("ps", "--all", "--format", "json", capture=True, timeout=timeout).strip()
        # An object per line; Compose before 2.21 printed one array.
        decoded: object = (
            json.loads(output)
            if output.startswith("[")
            else [json.loads(line) for line in output.splitlines() if line.strip()]
        )
        return objects(decoded)


def memini_service(rows: list[dict[str, object]]) -> dict[str, object]:
    """The row of the Memini service's container; empty while it has none."""
    for row in rows:
        if row.get("Service") == "memini":
            return row
    return {}


def published(row: dict[str, object]) -> str | None:
    """The API's address on this machine by the port Docker published for the container of
    ``row``; ``None`` while it publishes none."""
    for publisher in objects(row.get("Publishers")):
        port = publisher.get("PublishedPort")
        if publisher.get("TargetPort") == API_PORT and isinstance(port, int) and port > 0:
            host = publisher.get("URL")
            # Published on every address: this machine reaches it on its own.
            if not isinstance(host, str) or host in {"", "0.0.0.0", "::"}:  # noqa: S104 - read
                host = "127.0.0.1"
            return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    return None


def configured(directory: Path) -> str | None:
    """The API's address at the next start, by ``MEMINI_HTTP_PORT`` of the private settings, which
    compose.yaml publishes on 127.0.0.1; ``None`` for port 0, which Docker chooses anew at every
    start, and for a value that is no port."""
    port = setting(directory / ".env", "MEMINI_HTTP_PORT") or str(API_PORT)
    if port.isascii() and port.isdigit() and 0 < int(port) <= 65535:
        return f"http://127.0.0.1:{int(port)}"
    return None


def mcp_served(api_url: str) -> bool | None:
    """Whether the service at ``api_url`` serves its MCP endpoint, asked without a token:
    ``False`` when it answers ``mcp_disabled``, ``True`` for another answer (401 where MCP is on),
    ``None`` when that cannot be told: no answer, or a refusal by the rate limit."""
    # A local address: never through a proxy of the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{api_url}/mcp", timeout=10):
            return True
    except urllib.error.HTTPError as error:
        with error:
            if error.code == 429:
                return None
            try:
                body: object = json.loads(error.read())
            except ValueError:
                return True
        failure = cast(dict[str, object], body).get("error") if isinstance(body, dict) else None
        code = cast(dict[str, object], failure).get("code") if isinstance(failure, dict) else None
        return code != "mcp_disabled"
    except OSError:  # no connection, no answer in time
        return None


def switch_mcp(project: Project, enabled: bool) -> str:
    """Set the MCP switch in the service's settings; while the service runs, also recreate its
    container, as Compose does for a changed environment, and ask the service whether it serves
    MCP now. Returns the line that tells a person.

    Raises:
        ValueError: the service answers the other way after its restart, as an image from before
            the switch does, which ignores the setting. The settings are then as they were.
    """
    directory = project.directory
    # Docker first: without its answer, a service could go on running with its old setting.
    running = memini_service(project.services()).get("State") == "running"
    before = save_mcp(directory, enabled)
    word = "on" if enabled else "off"
    if not running:
        return f"MCP will be {word} when the server starts"
    project.run("up", "-d", "--no-build", "--wait", "memini")
    url = published(memini_service(project.services())) or configured(directory)
    if url is not None and mcp_served(url) is (not enabled):
        write_private(settings_file(directory), before)
        raise ValueError(
            f"the server did not apply {MCP_SWITCH} after its restart, as an image from before"
            " the switch does; the setting was put back"
        )
    return f"MCP is on at {url}/mcp" if enabled and url else f"MCP is {word}"


def report(directory: Path) -> dict[str, object]:
    """The object ``status --json`` prints; docs/install.md says what each field means."""
    installed = (directory / MARKER).is_file()
    project = Project(directory, quiet=True) if installed else None
    docker = shutil.which("docker") if project is None else project.docker
    rows: list[dict[str, object]] = []
    problem: str | None = None
    if not docker:
        problem = "the docker command was not found; install Docker with Compose 2.24 or later"
    else:
        try:
            if project is not None:
                rows = project.services(DOCKER_TIMEOUT)
            else:  # nothing to list yet: ask the engine whether it is there
                subprocess.run(  # noqa: S603 - resolved docker executable, argument vector only
                    [docker, "version", "--format", "{{.Server.Version}}"],
                    env=environment(),
                    check=True,
                    stdout=subprocess.DEVNULL,
                    timeout=DOCKER_TIMEOUT,
                )
        except subprocess.CalledProcessError as error:
            problem = (
                f"docker failed with exit status {error.returncode}; start the Docker engine,"
                " with Compose 2.24 or later"
            )
        except subprocess.TimeoutExpired:
            problem = f"docker did not answer in {DOCKER_TIMEOUT} seconds"
        except OSError:
            problem = "the docker command could not be run"
        except ValueError:
            problem = "docker compose did not answer in JSON"
    memini = memini_service(rows)
    running = memini.get("State") == "running"
    services: list[dict[str, object]] = []
    for row in rows:
        health = row.get("Health")
        services.append(
            {
                "name": str(row.get("Service") or ""),
                "state": str(row.get("State") or ""),
                "health": health if isinstance(health, str) and health else None,
            }
        )
    answer: dict[str, object] = {
        "schema": SCHEMA,
        "installed": installed,
        "running": running,
        "api_url": None,
        "mcp_url": None,
        "mcp_enabled": None,
        "image": None,
        "directory": None,
        "docker": {"available": problem is None, "error": problem},
        "services": sorted(services, key=lambda service: str(service["name"])),
    }
    if installed:
        enabled = mcp_enabled(directory)
        url = (published(memini) if running else None) or configured(directory)
        image = memini.get("Image")
        answer.update(
            api_url=url,
            mcp_url=f"{url}/mcp" if url and enabled else None,
            mcp_enabled=enabled,
            # The image of the service's container; before it has one, the image a start runs.
            image=image if isinstance(image, str) and image else IMAGE,
            directory=str(directory),
        )
    return answer


def first_token(project: Project, *, force: bool) -> str:
    """The first admin token of the managed server, from the service's own ``bootstrap``.

    The token comes back on the pipe of Docker's command and is returned to the caller: it is
    never put on a command line, into the environment or into a message. The service refuses
    while an active admin token exists, unless ``force``; its own reason is the error.
    """
    if not project.docker:
        raise ValueError("install Docker with Compose 2.24 or later and start the Docker engine")
    command = ["exec", "-T", "memini", "memini", "bootstrap", "--name", BOOTSTRAP_NAME]
    if force:
        command.append("--force")
    try:
        result = subprocess.run(  # noqa: S603 - resolved docker executable, argument vector only
            [project.docker, *project.arguments, *command],
            cwd=project.directory,
            env=project.env,
            check=True,
            capture_output=True,
            text=True,
            timeout=BOOTSTRAP_TIMEOUT,
        )
    except subprocess.CalledProcessError as error:
        # What the service says of a refusal names token prefixes at most, never a token.
        said = [line.strip() for line in str(error.stderr or "").splitlines() if line.strip()]
        reason = f"docker compose failed with exit status {error.returncode}"
        raise ValueError(said[-1] if said else reason) from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"bootstrap did not answer in {BOOTSTRAP_TIMEOUT} seconds") from None
    token = str(result.stdout or "").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("the service's bootstrap did not print a token")
    return token


def bootstrap(directory: Path, *, force: bool, as_json: bool) -> int:
    """Print the first admin token of the managed server in ``directory``: alone on stdout, or
    with ``as_json`` as the ``token`` of the one object, which a failure prints with its reason
    and a null token. This object is the only output of the launcher that holds a secret."""
    token: str | None = None
    failure: str | None = None
    try:
        if not (directory / MARKER).is_file():
            raise ValueError(f"no managed server at {directory}; start one first")
        token = first_token(Project(directory, quiet=True), force=force)
    except (ValueError, OSError) as error:
        if not as_json:
            raise
        failure = str(error)
    if as_json:
        answer = {"schema": SCHEMA, "ok": failure is None, "error": failure, "token": token}
        print(json.dumps(answer))
    else:
        print(token)
    return 0 if failure is None else 1


def mcp_line(answer: dict[str, object], directory: Path) -> str:
    """What ``mcp status`` tells a person, from the object of :func:`report`."""
    if not answer["installed"]:
        return f"No managed server at {directory}"
    if answer["mcp_enabled"] is None:
        return f"{MCP_SWITCH} in {settings_file(directory)} is neither true nor false"
    if not answer["mcp_enabled"]:
        return "MCP is off"
    served = answer["running"] and answer["mcp_url"]
    return f"MCP is on at {answer['mcp_url']}" if served else "MCP is on"


def compose(directory: Path, command: str, args: list[str], *, quiet: bool = False) -> int:
    project = Project(directory, quiet=quiet)
    run = project.run
    if command == "start":
        run("up", "-d", "--no-build", "--wait", "ollama")
        if MODEL not in {
            line.split()[0]
            for line in run("exec", "-T", "ollama", "ollama", "list", capture=True).splitlines()
            if line.split()
        }:
            run("exec", "-T", "ollama", "ollama", "pull", MODEL)
        run("up", "-d", "--no-build", "--wait")
        if not quiet:
            address = run("port", "memini", str(API_PORT), capture=True).strip()
            print("Memini is ready at http://" + address)
    elif command == "status":
        run("ps", "--all")
    elif command == "stop":
        run("stop")
    elif command == "mcp":
        message = switch_mcp(project, args == ["on"])
        if not quiet:
            print(message)
    else:
        token = os.environ.get("MEMINI_TOKEN", "")
        token_file = os.environ.get("MEMINI_TOKEN_FILE")
        if token_file:
            if token:
                raise ValueError("set only one of MEMINI_TOKEN and MEMINI_TOKEN_FILE")
            data = Path(token_file).read_bytes()
            # Windows PowerShell 5.1 redirects native stdout to UTF-16 with a BOM.
            encoding = (
                "utf-16"
                if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
                else "utf-8-sig"
            )
            token = data.decode(encoding).rstrip("\r\n")
        flags: list[str] = []
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            flags.append("-T")
        if token:
            project.env["MEMINI_TOKEN"] = token
            flags.extend(["-e", "MEMINI_TOKEN"])
        run("exec", *flags, "memini", "memini", *args)
    return 0


def manage(
    directory: Path, command: str, args: list[str], port: int | None, *, quiet: bool = False
) -> int:
    """Run ``command`` on the managed server in ``directory``; ``quiet`` prints nothing on
    stdout."""
    if command == "start":
        if not directory.exists():
            initialize(directory, API_PORT if port is None else port)
        elif (
            port is not None
            and (directory / MARKER).is_file()
            and f"MEMINI_HTTP_PORT={port}" not in (directory / ".env").read_text().splitlines()
        ):
            raise ValueError(f"update MEMINI_HTTP_PORT in {directory / '.env'} to change port")
    if not directory.exists() and command in {"status", "stop"}:
        if not quiet:
            print(f"No managed server at {directory}")
        return 0
    if not (directory / MARKER).is_file():
        raise ValueError(f"no managed server at {directory}; start with a new state directory")
    return compose(directory, command, args, quiet=quiet)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="`mcp on`, `mcp off` and `mcp status` switch or show the server's MCP endpoint."
        " `bootstrap` prints the first admin token of the server. start, status, stop, mcp and"
        " bootstrap take --json after the command: one JSON object on stdout.",
    )
    parser.add_argument("--state-dir", type=Path, help="directory for this local server")
    parser.add_argument(
        "command", choices=("start", "status", "stop", "mcp", "bootstrap", "path", "cli")
    )
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    options = parser.parse_args()
    command: str = options.command
    args: list[str] = options.args
    port: int | None = None
    as_json = False
    force = False
    if command in {"start", "status", "stop", "mcp", "bootstrap"}:
        details = argparse.ArgumentParser(prog=f"{parser.prog} {command}")
        details.add_argument(
            "--json", action="store_true", help="print one JSON object on stdout, for programs"
        )
        if command == "start":
            details.add_argument(
                "--http-port", type=int, help="first-run local port (default 6364)"
            )
        if command == "bootstrap":
            details.add_argument(
                "--force",
                action="store_true",
                help="create another admin token although an active one exists",
            )
        if command == "mcp":
            details.add_argument(
                "switch",
                choices=("on", "off", "status"),
                help="switch the MCP endpoint on or off and restart the service, or show it",
            )
        chosen = details.parse_args(args)
        as_json = chosen.json
        force = command == "bootstrap" and chosen.force
        if command == "start":
            port = chosen.http_port
            if port is not None and not 0 <= port <= 65535:
                details.error("--http-port must be between 0 and 65535")
        args = [chosen.switch] if command == "mcp" else []
    elif command == "path" and args:
        parser.error(f"unexpected arguments for {command}")
    if command == "path":
        print(resources())
        return 0
    directory = (options.state_dir or state_directory()).expanduser().resolve()
    if command == "bootstrap":
        return bootstrap(directory, force=force, as_json=as_json)
    if (command == "status" and as_json) or (command == "mcp" and args == ["status"]):
        answer = report(directory)
        print(json.dumps(answer) if as_json else mcp_line(answer, directory))
        return 0
    if not as_json:
        return manage(directory, command, args, port)
    # One object on stdout whatever happens; what Docker says of a failure is on stderr.
    failure: str | None = None
    try:
        code = manage(directory, command, args, port, quiet=True)
    except subprocess.CalledProcessError as error:
        code = error.returncode or 1
        failure = f"docker compose failed with exit status {error.returncode}"
    except (ValueError, OSError) as error:
        code = 1
        failure = str(error)
    print(json.dumps({"schema": SCHEMA, "ok": failure is None, "error": failure}))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (ValueError, OSError) as error:
        sys.exit(f"memini-server: {error}")
