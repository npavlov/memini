"""Run the git CLI hardened: repository content and remotes are untrusted.

Every command is ``git [--git-dir=…] -c … <args>`` started with ``asyncio.create_subprocess_exec``
(no shell) in a new session, so that its process group (git and what it starts: ssh,
git-remote-https, upload-pack, index-pack) can be killed as one. What applies to every command:

- An explicit timeout. When it runs out, or the caller is cancelled, the whole process group gets
  SIGKILL and is reaped; :class:`GitTimeout` names the command.
- A cap on what the command may print (:class:`OutputTooLarge`), and an optional cap on the size
  of a directory while it runs (:class:`MirrorTooLarge`): a fetch is killed when the mirror grows
  past the limit, checked every ``size_check_interval`` seconds.
- An environment built from scratch, not inherited: ``PATH``, proxy and CA variables, and fixed
  values. No user or system git config (``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_NOSYSTEM=1``),
  no prompts (``GIT_TERMINAL_PROMPT=0``, ``BatchMode=yes`` for ssh), no lazy fetches of missing
  objects, no replace refs, pathspecs taken literally, LFS never smudged, a nonexistent ``HOME``.
  ``GIT_TRACE*``, ``GIT_DIR`` and the like of the service's own environment never reach git.
- Config on the command line that turns off what could run code or reach further than asked:
  ``core.hooksPath=/dev/null`` (no hooks), ``core.fsmonitor=false``, no submodule recursion,
  ``protocol.allow=never`` with only the transport of the remote allowed (``file`` only for a local
  source; so no ``ext::``, and a redirect cannot switch transports), no detached gc, no credential
  helper but ours. git LFS is never configured, so LFS objects are not fetched; the pointer files
  are ordinary small blobs.

Credentials (global for now; per-source credentials come later):

- https: the token is sent only to the hosts in ``git_https_token_hosts``, only over https. It goes
  to git through a credential helper scoped to the remote's exact scheme, host and port
  (``credential.https://host.helper``), which prints it from an environment variable of the git
  process. So the token is never in a URL, in the process arguments (visible to every user in
  ``ps``), in the mirror's config or in any file; a redirect to another host gets nothing. The
  environment of a process is readable only by its own user and root. The token is registered for
  log redaction and masked in error text.
- ssh: ``GIT_SSH_COMMAND`` runs ssh with no config file (``-F /dev/null``), the key from
  ``git_ssh_key_path`` only (no agent), ``StrictHostKeyChecking=yes`` against the known_hosts file
  from ``git_ssh_known_hosts_path`` (never written; an unknown host key fails the fetch) and no
  forwarding. Both settings are required for an ssh remote.
"""

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from memini.git.errors import (
    GitCommandError,
    GitTimeout,
    GitUnavailable,
    MirrorTooLarge,
    OutputTooLarge,
    RemoteNotAllowed,
)
from memini.git.options import GitOptions
from memini.git.remote import Remote
from memini.logs import REDACTED, redact, register_secret

__all__ = [
    "MIN_GIT_VERSION",
    "Command",
    "Git",
    "Output",
    "SizeLimit",
    "directory_size",
]

log = logging.getLogger(__name__)

MIN_GIT_VERSION: Final = (2, 31)
"""The oldest git the connector runs with (``fetch --atomic``, ``--no-write-fetch-head``,
``--no-auto-maintenance``)."""

DEFAULT_MAX_OUTPUT: Final = 256 * 1024**2
"""Bytes a command may print to stdout unless the caller says otherwise."""

_STDERR_TAIL: Final = 16 * 1024
"""Bytes of stderr kept: the end, where git says what went wrong."""

_ERROR_TEXT: Final = 2000
"""Characters of stderr put into an error message."""

USERNAME_VAR: Final = "_MEMINI_GIT_USERNAME"
PASSWORD_VAR: Final = "_MEMINI_GIT_PASSWORD"  # noqa: S105 - the name of a variable, not a value

# A git credential helper as a shell snippet: git runs it as `sh -c '<snippet> "$@"' - <action>`.
# It answers "get" with the user name and token from the environment of the git process; "store"
# and "erase" do nothing, so git never writes the token anywhere.
_CREDENTIAL_HELPER: Final = (
    f'!f() {{ test "$1" = get || exit 0; printf "username=%s\\npassword=%s\\n"'
    f' "${USERNAME_VAR}" "${PASSWORD_VAR}"; }}; f'
)

_PASSTHROUGH: Final = (
    "PATH",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)
"""Variables of the service's environment that reach git: finding executables, temporary files,
and the proxies and CA certificates of the network the service runs in."""

_ENVIRONMENT: Final[Mapping[str, str]] = {
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "SSH_ASKPASS_REQUIRE": "never",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
    "LANG": "C",
}

_CONFIG: Final = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "credential.helper=",
    "protocol.allow=never",
    "protocol.ext.allow=never",
    "transfer.credentialsInUrl=die",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "fetch.writeCommitGraph=false",
    "fetch.showForcedUpdates=false",
    "gc.autoDetach=false",
    "maintenance.autoDetach=false",
    "http.followRedirects=initial",
    "init.defaultBranch=main",
)
"""``-c`` options of every command. ``credential.helper=`` empties the helper list, so only the
scoped helper added for a token host is ever asked."""

_SSH_OPTIONS: Final = (
    "BatchMode=yes",
    "IdentitiesOnly=yes",
    "IdentityAgent=none",
    "StrictHostKeyChecking=yes",
    "GlobalKnownHostsFile=/dev/null",
    "UpdateHostKeys=no",
    "CheckHostIP=no",
    "ControlMaster=no",
    "ControlPath=none",
    "ForwardAgent=no",
    "ForwardX11=no",
    "ClearAllForwardings=yes",
    "PermitLocalCommand=no",
    "ConnectTimeout=30",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=4",
    "LogLevel=ERROR",
)


@dataclass(frozen=True, slots=True)
class Command:
    """A git command ready to start: what :meth:`Git.run` passes to the process."""

    name: str
    """The git subcommand, for messages: ``fetch``, ``ls-tree`` …"""
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path


@dataclass(frozen=True, slots=True)
class Output:
    """How a command ended: exit status, stdout, and the end of stderr with secrets masked."""

    returncode: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True, slots=True)
class SizeLimit:
    """Kill the command when ``path`` holds more than ``limit`` bytes."""

    path: Path
    limit: int


def directory_size(path: Path) -> int:
    """Bytes in the regular files under ``path``, symlinks not followed. Files that vanish while it
    counts (a fetch renames its temporary packs) are skipped."""
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
    return total


class Git:
    """Builds and runs hardened git commands for the given options."""

    def __init__(self, options: GitOptions, environ: Mapping[str, str] | None = None) -> None:
        env: Mapping[str, str] = os.environ if environ is None else environ
        self._options = options
        self._base_env = {name: value for name in _PASSTHROUGH if (value := env.get(name))}
        self._token = (
            options.https_token.get_secret_value() if options.https_token is not None else None
        )
        if self._token is not None:
            register_secret(self._token)

    @property
    def options(self) -> GitOptions:
        return self._options

    def command(
        self, args: Sequence[str], *, git_dir: Path | None, remote: Remote | None = None
    ) -> Command:
        """The process to start for ``git <args>`` on ``git_dir``, talking to ``remote`` if any.

        Raises:
            RemoteNotAllowed: an ssh remote without the key or known_hosts setting.
        """
        config: list[str] = list(_CONFIG)
        env = {**self._base_env, **_ENVIRONMENT}
        if remote is not None:
            self._add_transport(remote, config, env)
        argv = [self._options.git]
        if git_dir is not None:
            argv.append(f"--git-dir={git_dir}")
        for item in config:
            argv += ["-c", item]
        argv += args
        cwd = git_dir if git_dir is not None else Path("/")
        return Command(_subcommand(args), tuple(argv), env, cwd)

    def _add_transport(self, remote: Remote, config: list[str], env: dict[str, str]) -> None:
        config.append(f"protocol.{remote.transport}.allow=always")
        if remote.transport == "file":
            # The operator vouched for this directory by listing its root in git_local_roots.
            # upload-pack, the only git process that runs in it, only reads, and no hook runs.
            # Without this, a repository owned by another user (a host directory mounted into the
            # container) is refused as "dubious ownership"; the user's global config, where
            # safe.directory normally goes, is not read.
            config += [f"safe.directory={remote.url}", f"safe.directory={remote.url}/.git"]
        elif remote.transport == "https" and self._sends_token(remote):
            netloc = remote.host or ""
            if ":" in netloc:  # an IPv6 address
                netloc = f"[{netloc}]"
            if remote.port is not None:
                netloc += f":{remote.port}"
            config.append(f"credential.https://{netloc}.helper={_CREDENTIAL_HELPER}")
            env[USERNAME_VAR] = self._options.https_username
            env[PASSWORD_VAR] = self._token or ""
        elif remote.transport == "ssh":
            config.append("ssh.variant=ssh")
            env["GIT_SSH_COMMAND"] = self._ssh_command()

    def _sends_token(self, remote: Remote) -> bool:
        return self._token is not None and remote.host in self._options.https_token_hosts

    def _ssh_command(self) -> str:
        key = self._options.ssh_key_path
        known_hosts = self._options.ssh_known_hosts_path
        if key is None or known_hosts is None:
            raise RemoteNotAllowed(
                "ssh remotes need a key and a known_hosts file: set MEMINI_GIT_SSH_KEY_PATH and"
                " MEMINI_GIT_SSH_KNOWN_HOSTS_PATH"
            )
        parts = ["ssh", "-F", "/dev/null"]
        for option in (*_SSH_OPTIONS, f"IdentityFile={key}", f"UserKnownHostsFile={known_hosts}"):
            parts += ["-o", option]
        return shlex.join(parts)

    def scrub(self, text: str) -> str:
        """``text`` with the token and anything else that looks like a credential masked."""
        if self._token:
            text = text.replace(self._token, REDACTED)
        return redact(text)

    async def run(
        self,
        args: Sequence[str],
        *,
        git_dir: Path | None,
        remote: Remote | None = None,
        timeout: float | None = None,
        stdin: bytes | None = None,
        max_output: int = DEFAULT_MAX_OUTPUT,
        size_limit: SizeLimit | None = None,
        check: bool = True,
    ) -> Output:
        """Run ``git <args>`` and return its output.

        Args:
            git_dir: the repository; ``None`` only for commands that need none (``init``,
                ``version``).
            remote: the remote the command talks to; allows its transport and adds credentials.
            timeout: seconds; ``options.command_timeout`` by default.
            stdin: bytes written to the command's standard input; none if ``None``.
            max_output: bytes the command may print to stdout.
            size_limit: kill the command when this directory grows past the limit.
            check: raise :class:`GitCommandError` on a non-zero exit.

        Raises:
            GitTimeout, OutputTooLarge, MirrorTooLarge: the process group was killed.
            GitCommandError: git exited non-zero and ``check`` is set.
            GitUnavailable: the git executable cannot be started.
            RemoteNotAllowed: see :meth:`command`.
        """
        command = self.command(args, git_dir=git_dir, remote=remote)
        seconds = self._options.command_timeout if timeout is None else timeout
        log.debug("git %s", command.name, extra={"git_dir": str(git_dir), "remote": str(remote)})
        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(command.env),
                cwd=command.cwd,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as error:
            raise GitUnavailable(
                f"cannot start git ({self._options.git}): {error.strerror or type(error).__name__}"
            ) from None
        try:
            returncode, stdout, stderr = await self._wait(
                process, command, seconds, stdin, max_output, size_limit
            )
        finally:
            if process.returncode is None:
                _kill_group(process)
                await process.wait()
        text = self.scrub(stderr.decode("utf-8", "replace").strip())
        if check and returncode != 0:
            raise GitCommandError(command.name, returncode, _error_text(text))
        return Output(returncode, stdout, text)

    async def _wait(
        self,
        process: asyncio.subprocess.Process,
        command: Command,
        timeout: float,
        stdin: bytes | None,
        max_output: int,
        size_limit: SizeLimit | None,
    ) -> tuple[int, bytes, bytes]:
        communicate = asyncio.ensure_future(_communicate(process, stdin, max_output))
        interval = self._options.size_check_interval if size_limit is not None else None
        try:
            async with asyncio.timeout(timeout):
                while not communicate.done():
                    await asyncio.wait({communicate}, timeout=interval)
                    if size_limit is not None and not communicate.done():
                        size = await asyncio.to_thread(directory_size, size_limit.path)
                        if size > size_limit.limit:
                            raise MirrorTooLarge(size, size_limit.limit)
        except TimeoutError:
            raise GitTimeout(command.name, timeout) from None
        finally:
            if not communicate.done():
                # Killing the group closes the pipes, so the readers finish.
                _kill_group(process)
                communicate.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await communicate
        return communicate.result()

    async def version(self) -> tuple[int, int]:
        """The major and minor version of git.

        Raises:
            GitUnavailable: git cannot be started, prints no version, or is older than
                :data:`MIN_GIT_VERSION`.
        """
        try:
            output = await self.run(["version"], git_dir=None)
        except GitCommandError as error:
            raise GitUnavailable(f"git does not run: {error}") from None
        match = re.search(rb"git version (\d+)\.(\d+)", output.stdout)
        if match is None:
            raise GitUnavailable("git printed no version")
        found = (int(match.group(1)), int(match.group(2)))
        if found < MIN_GIT_VERSION:
            wanted = ".".join(map(str, MIN_GIT_VERSION))
            raise GitUnavailable(
                f"git {found[0]}.{found[1]} is too old; {wanted} or later is needed"
            )
        return found


async def _communicate(
    process: asyncio.subprocess.Process, stdin: bytes | None, max_output: int
) -> tuple[int, bytes, bytes]:
    if process.stdout is None or process.stderr is None:  # pragma: no cover - always piped
        raise RuntimeError("git was started without pipes")
    out = asyncio.ensure_future(_read(process.stdout, max_output))
    err = asyncio.ensure_future(_read_tail(process.stderr, _STDERR_TAIL))
    tasks: list[asyncio.Future[Any]] = [out, err]
    if stdin is not None and process.stdin is not None:
        tasks.append(asyncio.ensure_future(_feed(process.stdin, stdin)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()  # nothing to cancel unless one of them failed
    return await process.wait(), out.result(), err.result()


async def _read(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise OutputTooLarge(f"git printed more than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_tail(stream: asyncio.StreamReader, limit: int) -> bytes:
    tail = bytearray()
    while chunk := await stream.read(64 * 1024):
        tail += chunk
        if len(tail) > limit:
            del tail[: len(tail) - limit]
    return bytes(tail)


async def _feed(writer: asyncio.StreamWriter, data: bytes) -> None:
    # git may exit before it reads all of its input, for example on an error.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()


def _subcommand(args: Sequence[str]) -> str:
    """The git subcommand in ``args``, skipping options and the values of ``-c`` and ``-C``."""
    after_option = False
    for arg in args:
        if after_option:
            after_option = False
        elif arg in ("-c", "-C"):
            after_option = True
        elif not arg.startswith("-"):
            return arg
    return "git"


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # The process leads its own session (start_new_session), so its group id is its pid. The pid
    # cannot have been reused: the process has not been reaped (returncode is None).
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _error_text(stderr: str) -> str:
    return stderr if len(stderr) <= _ERROR_TEXT else "…" + stderr[-_ERROR_TEXT:]
