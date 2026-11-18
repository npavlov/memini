"""Shared by the backup tests: pg_dump and pg_restore for the test server, of its major version.

pg_dump refuses a server of a newer major version than its own, and the Postgres client of a host
is often older than the server (the runners of CI have one of an earlier major version), or not
installed at all. So the programs are taken, in this order, from:

1. the container that ``MEMINI_TEST_PG_CONTAINER`` names, run with ``docker exec``;
2. the PATH, when pg_dump there is of the server's major version or later;
3. the Docker container that publishes the test server's port on this machine (the compose database
   of ``make db``, the service container of CI), run with ``docker exec``.

In a container they connect over its Unix socket, as ``docker compose exec db pg_dump`` does in
scripts/backup.sh; on the host, to the host and port of the test URL. A password goes through
``PGPASSWORD``, never on a command line. Every value here is synthetic.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

CONTAINER_ENV: Final = "MEMINI_TEST_PG_CONTAINER"
"""A running container with the server's pg_dump and pg_restore, e.g. ``memini-db-1``."""

_TIMEOUT: Final = 120
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_VERSION: Final = re.compile(r"\(PostgreSQL\) (\d+)")


@dataclass(frozen=True)
class PgPrograms:
    """Runs pg_dump and pg_restore against one server, on this machine or in its container."""

    where: str
    """For messages: ``on this machine`` or ``in container <name>``."""
    prefix: tuple[str, ...]
    """Before the program: ``docker exec ...``, or nothing."""
    connection: tuple[str, ...]
    """How the programs reach the server: user, and host and port on this machine."""
    env: Mapping[str, str]

    def run(self, program: str, *args: str, stdin: bytes | None = None) -> bytes:
        """Run ``program`` with ``args``; its standard output. Fails the test if it fails."""
        done = subprocess.run(  # noqa: S603 - fixed programs; the arguments are the test's own
            [*self.prefix, program, *args],
            input=stdin,
            capture_output=True,
            env=dict(self.env),
            timeout=_TIMEOUT,
            check=False,
        )
        if done.returncode != 0:
            stderr = done.stderr.decode(errors="replace").strip()
            raise AssertionError(f"{program} {self.where} exited {done.returncode}: {stderr}")
        return done.stdout

    def dump(self, database: str) -> bytes:
        """The archive of ``database`` that scripts/backup.sh takes: ``pg_dump --format=custom``."""
        return self.run("pg_dump", "--format=custom", *self.connection, f"--dbname={database}")

    def restore(self, database: str, archive: bytes) -> None:
        """Restore ``archive`` into the empty ``database``, as scripts/restore.sh does."""
        self.run(
            "pg_restore",
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
            *self.connection,
            f"--dbname={database}",
            stdin=archive,
        )

    def script(self, archive: bytes) -> str:
        """The SQL a restore of ``archive`` runs: pg_restore without a database."""
        return self.run("pg_restore", "--file=-", stdin=archive).decode()

    def schema(self, database: str) -> str:
        """The schema of ``database`` as SQL, so that two databases with the same objects give
        the same text: without comments, the ``\\restrict`` lines (their key is new on every run)
        and parentheses. Postgres prints an expression as it was parsed, and a restore parses the
        dump's text again: ``x BETWEEN 1 AND 1000 AND y`` of a migration prints as
        ``((x >= 1) AND (x <= 1000)) AND y``, which a restore reads as one ``AND`` of three."""
        text = self.run(
            "pg_dump", "--schema-only", "--no-owner", *self.connection, f"--dbname={database}"
        ).decode()
        lines = (
            line.replace("(", "").replace(")", "")
            for line in text.splitlines()
            if line and not line.startswith(("--", "\\restrict", "\\unrestrict"))
        )
        return "\n".join(lines)


def find_programs(database_url: str) -> PgPrograms:
    """pg_dump and pg_restore that can dump the database of ``database_url``; see the module.

    Fails the test, with what to do, when there are none.
    """
    info = conninfo_to_dict(database_url)
    user = str(info.get("user") or os.environ.get("USER") or "postgres")
    database = str(info["dbname"])
    host = str(info.get("host") or "localhost")
    port = str(info.get("port") or "5432")
    env = dict(os.environ)
    password = info.get("password")
    if password:
        env["PGPASSWORD"] = str(password)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        row = conn.execute("SELECT current_setting('server_version_num')::int / 10000").fetchone()
    assert row is not None
    server: int = row[0]

    named = os.environ.get(CONTAINER_ENV)
    if named:
        found = _in_container(named, user, database, server, env)
        if isinstance(found, str):
            pytest.fail(f"{CONTAINER_ENV}={named}: {found}", pytrace=False)
        return found

    local = _major_version(["pg_dump", "--version"], env) if shutil.which("pg_dump") else None
    if local is not None and local >= server and shutil.which("pg_restore"):
        return PgPrograms(
            "on this machine", (), ("--host", host, "--port", port, "--username", user), env
        )

    tried = [f"pg_dump on the PATH: {f'version {local}' if local is not None else 'none'}"]
    if host in _LOCAL_HOSTS and shutil.which("docker"):
        containers = _output(["docker", "ps", "--quiet", "--filter", f"publish={port}"], env)
        for container in (containers or "").split():
            found = _in_container(container, user, database, server, env)
            if not isinstance(found, str):
                return found
            tried.append(f"container {container}: {found}")
        if not containers:
            tried.append(f"no Docker container publishes port {port}")
    pytest.fail(
        f"the backup tests need pg_dump and pg_restore of PostgreSQL {server} or later, for the"
        f" server at {host}:{port}. Tried: {'; '.join(tried)}. Install them, or set"
        f" {CONTAINER_ENV} to a running container of that server.",
        pytrace=False,
    )


def _in_container(
    container: str, user: str, database: str, server: int, env: Mapping[str, str]
) -> PgPrograms | str:
    """The programs in ``container`` if they fit the server of ``database``; else why not."""
    prefix = ("docker", "exec", "--interactive", "--env", "PGPASSWORD", container)
    version = _major_version([*prefix, "pg_dump", "--version"], env)
    if version is None:
        return "not a running container with pg_dump"
    if version < server:
        return f"pg_dump {version} is older than the server ({server})"
    # The same server: the container sees the test's database, whose name is new on every run.
    psql = ("psql", "--no-psqlrc", "--tuples-only", "--no-align", f"--username={user}")
    seen = _output([*prefix, *psql, f"--dbname={database}", "--command=SELECT 1"], env)
    if seen is None or seen.strip() != "1":
        return f"its server has no database {database}: another server"
    return PgPrograms(f"in container {container}", prefix, ("--username", user), env)


def _major_version(command: list[str], env: Mapping[str, str]) -> int | None:
    match = _VERSION.search(_output(command, env) or "")
    return int(match[1]) if match else None


def _output(command: list[str], env: Mapping[str, str]) -> str | None:
    """The output of ``command``, or ``None`` if it failed."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed programs and arguments
            command, capture_output=True, env=dict(env), timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.decode(errors="replace") if done.returncode == 0 else None
