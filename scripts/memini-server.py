#!/usr/bin/env python3
"""Manage the packaged Memini release using Docker Compose."""

import argparse
import codecs
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


def compose(directory: Path, command: str, args: list[str]) -> int:
    nonce = (directory / MARKER).read_text(encoding="ascii")
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise ValueError(f"invalid manager state in {directory}")
    identity = hashlib.sha256(os.fsencode(os.path.normcase(directory)) + nonce.encode()).hexdigest()
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("install Docker with Compose 2.24 or later and start the Docker engine")
    base = [
        docker,
        "compose",
        "--project-name",
        f"memini-managed-{identity[:20]}",
        "--project-directory",
        str(directory),
        "--env-file",
        str(directory / ".env"),
    ]
    for filename in COMPOSE_FILES:
        base.extend(["-f", str(directory / filename)])
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("COMPOSE_", "MEMINI_"))
    }
    env["MEMINI_RELEASE_IMAGE"] = IMAGE

    def run(*argv: str, capture: bool = False) -> str:
        result = subprocess.run(  # noqa: S603 - resolved docker executable, argument vector only
            [*base, *argv],
            cwd=directory,
            env=env,
            check=True,
            stdout=subprocess.PIPE if capture else None,
            text=capture,
        )
        return result.stdout or ""

    if command == "start":
        run("up", "-d", "--no-build", "--wait", "ollama")
        if MODEL not in {
            line.split()[0]
            for line in run("exec", "-T", "ollama", "ollama", "list", capture=True).splitlines()
            if line.split()
        }:
            run("exec", "-T", "ollama", "ollama", "pull", MODEL)
        run("up", "-d", "--no-build", "--wait")
        print("Memini is ready at http://" + run("port", "memini", "6364", capture=True).strip())
    elif command == "status":
        run("ps", "--all")
    elif command == "stop":
        run("stop")
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
            env["MEMINI_TOKEN"] = token
            flags.extend(["-e", "MEMINI_TOKEN"])
        run("exec", *flags, "memini", "memini", *args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="directory for this local server")
    parser.add_argument("command", choices=("start", "status", "stop", "path", "cli"))
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    options = parser.parse_args()
    args: list[str] = options.args
    port = None
    if options.command == "start":
        start = argparse.ArgumentParser(prog=f"{parser.prog} start")
        start.add_argument("--http-port", type=int, help="first-run local port (default 6364)")
        port = start.parse_args(args).http_port
        if port is not None and not 0 <= port <= 65535:
            start.error("--http-port must be between 0 and 65535")
    elif options.command != "cli" and args:
        parser.error(f"unexpected arguments for {options.command}")
    if options.command == "path":
        print(resources())
        return 0
    directory = (options.state_dir or state_directory()).expanduser().resolve()
    if options.command == "start":
        if not directory.exists():
            initialize(directory, 6364 if port is None else port)
        elif (
            port is not None
            and (directory / MARKER).is_file()
            and f"MEMINI_HTTP_PORT={port}" not in (directory / ".env").read_text().splitlines()
        ):
            raise ValueError(f"update MEMINI_HTTP_PORT in {directory / '.env'} to change port")
    if not directory.exists() and options.command in {"status", "stop"}:
        print(f"No managed server at {directory}")
        return 0
    if not (directory / MARKER).is_file():
        raise ValueError(f"no managed server at {directory}; start with a new state directory")
    return compose(directory, options.command, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (ValueError, OSError) as error:
        sys.exit(f"memini-server: {error}")
