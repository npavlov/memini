#!/usr/bin/env python3
"""Manage the packaged Memini release using Docker Compose."""

import argparse
import codecs
import contextlib
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
# Folders of this computer the server may read as git sources (docs/install.md, "Repositories on
# this computer"): their list in the state directory, and the Compose file made from it, which
# mounts each one read-only under LOCAL_ROOT and lists LOCAL_ROOT in MEMINI_GIT_LOCAL_ROOTS.
LOCAL_REPOS = "local-repos.json"
LOCAL_COMPOSE = "compose.local.yaml"
LOCAL_ROOT = "/srv/local"
# The name of a local folder: a handle, as the names of the service are.
HANDLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
# A host name or an IPv4 address: what ssh-keyscan and the https token are given.
HOST = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
# The files of git access in the state directory, as the compose secrets in .env name them.
SSH_KEY = "secrets/git_ssh_key"
SSH_KNOWN_HOSTS = "secrets/git_ssh_known_hosts"
HTTPS_TOKEN = "secrets/git_https_token"  # noqa: S105 - the path of a file, not a token
# The most bytes of an ssh key or an https token read from stdin.
MAX_SECRET = 16384
# Seconds one run of ssh-keygen, or ssh-keyscan per host, may take.
SSH_TIMEOUT = 20
NOT_SHARED = (
    "Docker could not mount the folder, or mounted it empty. With Docker Desktop, add the folder"
    " or a folder above it in Settings, Resources, File sharing (a disk under /Volumes"
    " included), then add it again"
)


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
        if (directory / LOCAL_COMPOSE).is_file():
            self.arguments.extend(["-f", str(directory / LOCAL_COMPOSE)])
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


def uninstall(directory: Path, *, quiet: bool = False) -> int:
    """Remove the managed server in ``directory``: its containers, its volumes (the database and
    the search model) and the directory with its settings and secrets. Docker keeps the images,
    which a later start reuses. Only a directory with the manager's marker is removed."""
    if not directory.exists():
        if not quiet:
            print(f"No managed server at {directory}")
        return 0
    if not (directory / MARKER).is_file():
        raise ValueError(f"no managed server at {directory}; nothing was removed")
    Project(directory, quiet=quiet).run("down", "--volumes", "--remove-orphans")
    shutil.rmtree(directory)
    if not quiet:
        print(f"Removed the server at {directory} with its data; the Docker images are kept")
    return 0


def set_lines(path: Path, values: dict[str, str | None]) -> None:
    """Set the ``NAME=value`` line of each name in the env file at ``path``, ``None`` removing
    it, and keep every other line."""
    before = path.read_text(encoding="utf-8") if path.is_file() else ""
    kept = [line for line in before.splitlines() if assigned(line) not in values]
    added = [f"{name}={value}" for name, value in values.items() if value is not None]
    write_private(path, "\n".join([*kept, *added]) + "\n")


def managed(directory: Path) -> Path:
    if not (directory / MARKER).is_file():
        raise ValueError(f"no managed server at {directory}; set one up first")
    return directory


def recreate(project: Project, *, force: bool = False) -> bool:
    """Recreate the service's container while it runs, so it takes changed mounts and settings;
    ``force`` also for a changed secret file, which Compose does not notice. Returns whether the
    service runs."""
    if memini_service(project.services()).get("State") != "running":
        return False
    command = ["up", "-d", "--no-build", "--wait"]
    if force:
        command.append("--force-recreate")
    project.run(*command, "memini")
    return True


def local_repos(directory: Path) -> list[dict[str, str]]:
    """The local folders the server may read, with their names, in the order they were added."""
    path = directory / LOCAL_REPOS
    if not path.is_file():
        return []
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError(f"{path} is not the launcher's list of folders") from None
    found: list[dict[str, str]] = []
    for item in objects(value):
        name, folder = item.get("name"), item.get("path")
        if isinstance(name, str) and HANDLE.fullmatch(name) and isinstance(folder, str):
            found.append({"name": name, "path": folder})
    return found


def save_local_repos(directory: Path, repos: list[dict[str, str]]) -> None:
    """Write the list of folders and the Compose file that mounts them; no file for none."""
    compose_file = directory / LOCAL_COMPOSE
    if not repos:
        (directory / LOCAL_REPOS).unlink(missing_ok=True)
        compose_file.unlink(missing_ok=True)
        return
    write_private(directory / LOCAL_REPOS, json.dumps(repos, indent=2) + "\n")
    service = {
        "environment": {"MEMINI_GIT_LOCAL_ROOTS": LOCAL_ROOT},
        "volumes": [
            {
                "type": "bind",
                "source": repo["path"],
                "target": f"{LOCAL_ROOT}/{repo['name']}",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
            for repo in repos
        ],
    }
    # JSON is YAML: each path is a quoted string, whatever characters it holds.
    text = json.dumps({"services": {"memini": service}}, indent=2)
    header = (
        "# Made by memini-server from local-repos.json; change it with `memini-server repos`.\n"
    )
    write_private(compose_file, header + text + "\n")


def check_folder(text: str) -> Path:
    """The folder of a git repository at ``text``: an absolute path, resolved, of a working copy
    with its own ``.git`` directory, or of a bare repository."""
    if not text or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("the folder is an absolute path without control characters")
    given = Path(text)
    if not given.is_absolute():
        raise ValueError("the folder is an absolute path")
    try:
        folder = given.resolve(strict=True)
    except OSError:
        raise ValueError(f"there is no folder at {given}") from None
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    if (folder / ".git").is_file():
        raise ValueError(
            f"{folder} is a linked worktree or a submodule; add the repository it belongs to"
        )
    bare = (folder / "HEAD").is_file() and (folder / "objects").is_dir()
    if not (folder / ".git").is_dir() and not bare:
        raise ValueError(f"{folder} is not a git repository")
    return folder


def current_branch(folder: Path) -> str | None:
    """The branch checked out in ``folder``, from its HEAD; ``None`` for a detached HEAD."""
    head = folder / ".git" / "HEAD"
    if not head.is_file():
        head = folder / "HEAD"
    try:
        line = head.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    ref = line.removeprefix("ref: ")
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None


def folder_name(folder: Path, taken: set[str]) -> str:
    """A name for ``folder`` from its own, unique among ``taken``."""
    base = re.sub(r"[^a-z0-9_-]+", "-", folder.name.lower()).strip("-_")[:56] or "repository"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def describe_repo(repo: dict[str, str]) -> dict[str, object]:
    folder = Path(repo["path"])
    present = folder.is_dir()
    return {
        "name": repo["name"],
        "path": repo["path"],
        # What a git source names as its location: the folder where the service sees it.
        "location": f"{LOCAL_ROOT}/{repo['name']}",
        "branch": current_branch(folder) if present else None,
        "present": present,
    }


def mounted_empty(project: Project, repo: dict[str, str]) -> bool:
    """Whether the folder is empty in the container, as Docker Desktop mounts a folder it does
    not share."""
    target = f"{LOCAL_ROOT}/{repo['name']}"
    return not project.run("exec", "-T", "memini", "ls", "-A", target, capture=True).strip()


def repos(directory: Path, action: str, target: str | None, quiet: bool) -> dict[str, object]:
    """``repos add|remove|list``: the fields of the object --json prints."""
    managed(directory)
    listed = local_repos(directory)
    answer: dict[str, object] = {"repository": None, "warning": None}
    if action == "add":
        folder = check_folder(target or "")
        existing = next((repo for repo in listed if repo["path"] == str(folder)), None)
        if existing is not None:
            answer["repository"] = describe_repo(existing)
        else:
            repo = {"name": folder_name(folder, {r["name"] for r in listed}), "path": str(folder)}
            project = Project(directory, quiet=quiet)
            # Docker first: a failure to answer is not a failure to mount.
            running = memini_service(project.services()).get("State") == "running"
            save_local_repos(directory, [*listed, repo])
            empty = False
            if running:
                try:
                    project = Project(directory, quiet=quiet)
                    project.run("up", "-d", "--no-build", "--wait", "memini")
                    empty = mounted_empty(project, repo)
                except subprocess.CalledProcessError:
                    empty = True
            if empty:
                # Back to the folders that worked, and the service with them.
                save_local_repos(directory, listed)
                with contextlib.suppress(subprocess.CalledProcessError):
                    recreate(Project(directory, quiet=quiet))
                raise ValueError(NOT_SHARED)
            answer["repository"] = describe_repo(repo)
            if not running:
                answer["warning"] = "the server is stopped: it reads the folder once it starts"
    elif action == "remove":
        kept = [repo for repo in listed if repo["name"] != target]
        if len(kept) == len(listed):
            raise ValueError(f"no folder is named {target}")
        save_local_repos(directory, kept)
        recreate(Project(directory, quiet=quiet))
    answer["repositories"] = [describe_repo(repo) for repo in local_repos(directory)]
    return answer


def openssh(program: str) -> str:
    found = shutil.which(program)
    if not found:
        raise ValueError(f"{program} was not found; install the OpenSSH client")
    return found


def fingerprints(path: Path) -> list[dict[str, str]]:
    """What ``ssh-keygen -l`` says of the keys in ``path``: each one's type, SHA256 fingerprint
    and, in a known_hosts file, the host it is for. Never a key itself."""
    result = subprocess.run(  # noqa: S603 - resolved ssh-keygen, argument vector only
        [openssh("ssh-keygen"), "-l", "-f", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=SSH_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    found: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        # 256 SHA256:abc… git.example.org (ED25519)
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("SHA256:"):
            typed = parts[-1].startswith("(") and parts[-1].endswith(")")
            label = parts[2:-1] if typed else parts[2:]
            found.append(
                {
                    "type": parts[-1].strip("()") if typed else "",
                    "fingerprint": parts[1],
                    "for": " ".join(label),
                }
            )
    return found


def git_access(directory: Path) -> dict[str, object]:
    """What the server holds to reach private remotes: only what identifies a secret."""
    env = directory / ".env"
    ssh: dict[str, object] | None = None
    if setting(env, "MEMINI_GIT_SSH_KEY_PATH") and (directory / SSH_KEY).is_file():
        known = directory / SSH_KNOWN_HOSTS
        hosts: dict[str, list[dict[str, str]]] = {}
        for item in fingerprints(known) if known.is_file() else []:
            hosts.setdefault(item["for"], []).append(
                {"type": item["type"], "fingerprint": item["fingerprint"]}
            )
        public = directory / (SSH_KEY + ".pub")
        key = fingerprints(public) if public.is_file() else []
        ssh = {
            "key": {"type": key[0]["type"], "fingerprint": key[0]["fingerprint"]} if key else None,
            "hosts": [{"host": host, "keys": keys} for host, keys in hosts.items()],
        }
    https: dict[str, object] | None = None
    if setting(env, "MEMINI_GIT_HTTPS_TOKEN_FILE") and (directory / HTTPS_TOKEN).is_file():
        settings = settings_file(directory)
        hosts_line = setting(settings, "MEMINI_GIT_HTTPS_TOKEN_HOSTS") or ""
        https = {
            "hosts": [host for host in hosts_line.split(",") if host],
            "username": setting(settings, "MEMINI_GIT_HTTPS_USERNAME") or "x-access-token",
        }
    return {"ssh": ssh, "https": https}


def read_secret(kind: str) -> str:
    """The secret on stdin: an ssh private key or an https token, never an argument."""
    data = sys.stdin.buffer.read(MAX_SECRET + 1)
    if len(data) > MAX_SECRET:
        raise ValueError(f"the {kind} on stdin is longer than {MAX_SECRET} bytes")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"the {kind} on stdin is not text") from None


def check_hosts(hosts: list[str]) -> list[str]:
    if not hosts:
        raise ValueError("name at least one host with --host")
    for host in hosts:
        if not HOST.fullmatch(host):
            raise ValueError(f"{host!r} is not a host name")
    return list(dict.fromkeys(host.lower() for host in hosts))


def set_ssh_key(directory: Path, hosts: list[str], text: str) -> None:
    """Keep the private key and the host keys of ``hosts`` as the server's ssh access. The host
    keys are fetched now, and the user compares their fingerprints with the ones the host
    publishes."""
    key = text.strip() + "\n"
    if "-----BEGIN" not in key or "PRIVATE KEY-----" not in key:
        raise ValueError("stdin holds no ssh private key")
    keygen = openssh("ssh-keygen")
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        staged = Path(scratch) / "key"
        write_private(staged, key)
        try:
            public = subprocess.run(  # noqa: S603 - resolved ssh-keygen, argument vector only
                [keygen, "-y", "-P", "", "-f", str(staged)],
                check=True,
                capture_output=True,
                text=True,
                timeout=SSH_TIMEOUT,
                stdin=subprocess.DEVNULL,
            ).stdout
        except subprocess.CalledProcessError:
            raise ValueError(
                "ssh-keygen could not read the key: it is not a private key, or it has a"
                " passphrase, which the server cannot type; use a key without one, such as a"
                " deploy key made for Memini"
            ) from None
    scanned = subprocess.run(  # noqa: S603 - resolved ssh-keyscan, argument vector only
        [openssh("ssh-keyscan"), "-T", "10", "-t", "ed25519,ecdsa,rsa", *hosts],
        capture_output=True,
        text=True,
        timeout=SSH_TIMEOUT * len(hosts),
        stdin=subprocess.DEVNULL,
        check=False,
    ).stdout
    lines = [line for line in scanned.splitlines() if line.strip() and not line.startswith("#")]
    missing = [host for host in hosts if not any(line.split()[0] == host for line in lines)]
    if missing:
        raise ValueError(f"ssh-keyscan got no host key from {', '.join(missing)}")
    write_private(directory / SSH_KEY, key)
    write_private(directory / (SSH_KEY + ".pub"), public)
    write_private(directory / SSH_KNOWN_HOSTS, "\n".join(lines) + "\n")
    set_lines(
        directory / ".env",
        {
            "MEMINI_GIT_SSH_KEY_PATH": f"./{SSH_KEY}",
            "MEMINI_GIT_SSH_KNOWN_HOSTS_PATH": f"./{SSH_KNOWN_HOSTS}",
        },
    )


def set_https_token(directory: Path, hosts: list[str], username: str | None, text: str) -> None:
    """Keep the token as the server's https access, sent only to ``hosts``."""
    token = text.strip()
    if not re.fullmatch(r"[\x21-\x7e]{1,4096}", token):
        raise ValueError("stdin holds no token: one line of printable characters, no spaces")
    if username is not None and not re.fullmatch(r"[\x21-\x7e]{1,200}", username):
        raise ValueError("the user name is printable ASCII without spaces")
    write_private(directory / HTTPS_TOKEN, token)
    set_lines(directory / ".env", {"MEMINI_GIT_HTTPS_TOKEN_FILE": f"./{HTTPS_TOKEN}"})
    set_lines(
        settings_file(directory),
        {"MEMINI_GIT_HTTPS_TOKEN_HOSTS": ",".join(hosts), "MEMINI_GIT_HTTPS_USERNAME": username},
    )


def remove_access(directory: Path, kind: str) -> None:
    if kind == "ssh":
        set_lines(
            directory / ".env",
            {"MEMINI_GIT_SSH_KEY_PATH": None, "MEMINI_GIT_SSH_KNOWN_HOSTS_PATH": None},
        )
        for name in (SSH_KEY, SSH_KEY + ".pub", SSH_KNOWN_HOSTS):
            (directory / name).unlink(missing_ok=True)
        return
    set_lines(directory / ".env", {"MEMINI_GIT_HTTPS_TOKEN_FILE": None})
    set_lines(
        settings_file(directory),
        {"MEMINI_GIT_HTTPS_TOKEN_HOSTS": None, "MEMINI_GIT_HTTPS_USERNAME": None},
    )
    (directory / HTTPS_TOKEN).unlink(missing_ok=True)


def git(directory: Path, choice: argparse.Namespace, quiet: bool) -> dict[str, object]:
    """``git status|ssh-key|https-token|remove``: the fields of the object --json prints. The
    secret of ssh-key and https-token is read from stdin."""
    managed(directory)
    action: str = choice.action
    if action == "ssh-key":
        set_ssh_key(directory, check_hosts(choice.host), read_secret("ssh key"))
    elif action == "https-token":
        set_https_token(directory, check_hosts(choice.host), choice.username, read_secret("token"))
    elif action == "remove":
        remove_access(directory, choice.kind)
    if action != "status":
        recreate(Project(directory, quiet=quiet), force=True)
    return git_access(directory)


def manage(
    directory: Path, command: str, args: list[str], port: int | None, *, quiet: bool = False
) -> int:
    """Run ``command`` on the managed server in ``directory``; ``quiet`` prints nothing on
    stdout."""
    if command == "uninstall":
        return uninstall(directory, quiet=quiet)
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


def configuration_parser(prog: str, command: str) -> argparse.ArgumentParser:
    """The arguments of ``repos`` and ``git``."""
    details = argparse.ArgumentParser(prog=f"{prog} {command}")
    details.add_argument(
        "--json", action="store_true", help="print one JSON object on stdout, for programs"
    )
    if command == "repos":
        details.add_argument("action", choices=("list", "add", "remove"))
        details.add_argument("target", nargs="?", help="add: the folder; remove: its name")
        return details
    details.add_argument("action", choices=("status", "ssh-key", "https-token", "remove"))
    details.add_argument("kind", nargs="?", choices=("ssh", "https"), help="remove: which")
    details.add_argument(
        "--host",
        action="append",
        default=[],
        help="ssh-key: a host whose key is trusted; https-token: a host the token is sent to",
    )
    details.add_argument("--username", help="https-token: the user name sent with the token")
    return details


def configure(directory: Path, command: str, choice: argparse.Namespace) -> int:
    """``repos`` and ``git``: one object on stdout with --json, lines for a person without."""
    failure: str | None = None
    answer: dict[str, object] = {}
    try:
        if command == "repos":
            answer = repos(directory, choice.action, choice.target, quiet=choice.json)
        else:
            answer = git(directory, choice, quiet=choice.json)
    except subprocess.CalledProcessError as error:
        failure = f"{Path(str(error.cmd[0])).name} failed with exit status {error.returncode}"
    except subprocess.TimeoutExpired as error:
        failure = f"{Path(str(error.cmd[0])).name} did not answer in {error.timeout:g} seconds"
    except (ValueError, OSError) as error:
        failure = str(error)
    if choice.json:
        print(json.dumps({"schema": SCHEMA, "ok": failure is None, "error": failure, **answer}))
        return 0 if failure is None else 1
    if failure is not None:
        raise ValueError(failure)
    for line in describe_configuration(command, answer):
        print(line)
    return 0


def describe_configuration(command: str, answer: dict[str, object]) -> list[str]:
    if command == "repos":
        lines = [
            f"{repo['name']}: {repo['path']} as {repo['location']}"
            + ("" if repo["present"] else " (the folder is gone)")
            for repo in objects(answer["repositories"])
        ]
        warning = answer.get("warning")
        return (lines or ["No folder of this computer is given to the server"]) + (
            [str(warning)] if warning else []
        )
    lines: list[str] = []
    ssh = answer.get("ssh")
    if isinstance(ssh, dict):
        ssh_access = cast(dict[str, object], ssh)
        key = ssh_access.get("key")
        printed = cast(dict[str, object], key)["fingerprint"] if isinstance(key, dict) else "?"
        lines.append(f"ssh key {printed}")
        for host in objects(ssh_access.get("hosts")):
            for item in objects(host.get("keys")):
                lines.append(f"  {host['host']}: {item['type']} {item['fingerprint']}")
    https = answer.get("https")
    if isinstance(https, dict):
        https_access = cast(dict[str, object], https)
        hosts = cast(list[str], https_access.get("hosts") or [])
        lines.append(f"https token for {', '.join(hosts)} as {https_access.get('username')}")
    return lines or ["No git access is set: public remotes and local folders only"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="`mcp on`, `mcp off` and `mcp status` switch or show the server's MCP endpoint."
        " `bootstrap` prints the first admin token of the server. start, status, stop, mcp and"
        " bootstrap take --json after the command: one JSON object on stdout. `uninstall --yes`"
        " removes the server with its data. `repos list|add PATH|remove NAME` gives the server"
        " folders of this computer as git sources; `git status|ssh-key|https-token|remove` sets"
        " its access to private remotes, the secret on stdin.",
    )
    parser.add_argument("--state-dir", type=Path, help="directory for this local server")
    parser.add_argument(
        "command",
        choices=(
            "start",
            "status",
            "stop",
            "mcp",
            "bootstrap",
            "uninstall",
            "repos",
            "git",
            "path",
            "cli",
        ),
    )
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    options = parser.parse_args()
    command: str = options.command
    args: list[str] = options.args
    port: int | None = None
    as_json = False
    force = False
    if command in {"start", "status", "stop", "mcp", "bootstrap", "uninstall"}:
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
        if command == "uninstall":
            details.add_argument(
                "--yes",
                action="store_true",
                help="confirm: the containers, the database and the settings are deleted",
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
        if command == "uninstall" and not chosen.yes:
            details.error("uninstall deletes the server and all its data; add --yes to confirm")
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
    if command in {"repos", "git"}:
        details = configuration_parser(parser.prog, command)
        choice = details.parse_args(args)
        if command == "repos" and (choice.action == "list") != (choice.target is None):
            details.error("add takes a folder and remove a name; list takes nothing")
        if command == "git" and (choice.action == "remove") != (choice.kind is not None):
            details.error("remove takes ssh or https; the other actions take neither")
        return configure(directory, command, choice)
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
