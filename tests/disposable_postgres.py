"""A Postgres server of the test's own, in a Docker container, for tests that stop or crash it.

The development database (``make db``) and the service container of CI are shared: other tests,
and other people's work, run against them. A test that kills the database server starts its own
here instead: the image of compose.yaml, pinned by the same digest, under a name of its own
(``memini-test-pg-<random>``), published on a free port of 127.0.0.1 only, and removed with its
volume at the end. Nothing else is touched: no compose project, no other container.

:func:`start_postgres` needs the ``docker`` CLI and a daemon; without them it skips the test.
"""

import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg
import pytest

__all__ = ["DisposablePostgres", "free_port", "postgres_image", "start_postgres"]

COMPOSE_FILE: Final = Path(__file__).parent.parent / "compose.yaml"
_IMAGE: Final = re.compile(r"^\s*image:\s*(pgvector/pgvector:\S+@sha256:[0-9a-f]{64})\s*$", re.M)
_USER: Final = "memini"
_PASSWORD: Final = "memini"  # noqa: S105 - a throwaway server on 127.0.0.1, removed after the test
_READY_TIMEOUT: Final = 90.0


def postgres_image() -> str:
    """The database image of compose.yaml, with its digest."""
    found = _IMAGE.search(COMPOSE_FILE.read_text(encoding="utf-8"))
    if found is None:
        raise RuntimeError("compose.yaml names no pgvector/pgvector image with a digest")
    return found.group(1)


def _docker(*args: str, timeout: float = 60.0) -> str:
    done = subprocess.run(  # noqa: S603 - fixed arguments of the docker CLI
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def free_port() -> int:
    """A TCP port of 127.0.0.1 that nothing listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True)
class DisposablePostgres:
    """A running server of the test's own; see the module."""

    container: str
    port: int

    @property
    def url(self) -> str:
        return f"postgresql://{_USER}:{_PASSWORD}@127.0.0.1:{self.port}/memini"

    def crash(self) -> None:
        """Kill the server at once (SIGKILL), as a power cut would: no shutdown, no checkpoint.
        Every session ends; what was not committed is gone, and the next start recovers from
        the WAL."""
        _docker("kill", "--signal", "KILL", self.container)

    def start(self) -> None:
        """Start the server again and wait until it accepts connections."""
        _docker("start", self.container)
        self.wait_ready()

    def wait_ready(self, timeout: float = _READY_TIMEOUT) -> None:
        """Wait until the server accepts connections over TCP on its published port."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                with psycopg.connect(self.url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                return
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)


@contextmanager
def start_postgres() -> Generator[DisposablePostgres]:
    """Run a server of the test's own for the block, then remove it; see the module."""
    if shutil.which("docker") is None:
        pytest.skip("needs the docker CLI, to run a database server the test may kill")
    try:
        _docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    except (RuntimeError, subprocess.TimeoutExpired):
        pytest.skip("needs a running Docker daemon, to run a database server the test may kill")
    name = f"memini-test-pg-{uuid.uuid4().hex[:10]}"
    port = free_port()
    _docker(
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "memini.test=disposable-postgres",
        "--env",
        f"POSTGRES_USER={_USER}",
        "--env",
        f"POSTGRES_PASSWORD={_PASSWORD}",
        "--env",
        "POSTGRES_DB=memini",
        "--publish",
        f"127.0.0.1:{port}:5432",
        postgres_image(),
        timeout=300,  # a pull, if the image is not there yet
    )
    try:
        server = DisposablePostgres(name, port)
        server.wait_ready()
        yield server
    finally:
        # The volume too (-v): the image declares one for the data directory.
        subprocess.run(  # noqa: S603 - fixed arguments of the docker CLI
            ["docker", "rm", "--force", "--volumes", name],  # noqa: S607
            capture_output=True,
            timeout=60,
            check=False,
        )
