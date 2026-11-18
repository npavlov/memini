"""Tests for memini.git.command: the hardened git invocation. Every credential here is fake."""

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from memini.git import (
    Git,
    GitOptions,
    GitTimeout,
    GitUnavailable,
    MirrorTooLarge,
    OutputTooLarge,
    Remote,
    RemoteNotAllowed,
    SizeLimit,
)
from memini.git.command import MIN_GIT_VERSION, PASSWORD_VAR, USERNAME_VAR

pytestmark = [
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

TOKEN = "fake-git-token-for-tests-only-7d1e"  # noqa: S105 - fake, for leak checks
CREDENTIAL_HOST = "git.example.test"
OTHER_HOST = "other.example.test"


@pytest.fixture
def options(tmp_path: Path) -> GitOptions:
    return GitOptions(
        mirror_dir=tmp_path / "mirrors",
        https_token=SecretStr(TOKEN),
        https_token_hosts=frozenset({CREDENTIAL_HOST}),
        allow_http=True,
        ssh_key_path=Path("/run/secrets/fake key"),
        ssh_known_hosts_path=Path("/etc/memini/known_hosts"),
        size_check_interval=0.05,
    )


def config_of(argv: tuple[str, ...]) -> list[str]:
    return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"]


# --- The command line and the environment --------------------------------------------------------


def test_every_command_is_hardened(options: GitOptions, tmp_path: Path) -> None:
    hostile_environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/someone",
        "GIT_DIR": "/somewhere/else",
        "GIT_TRACE": "1",
        "GIT_CURL_VERBOSE": "1",
        "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='/tmp/hooks'",
        "SSH_AUTH_SOCK": "/run/user/agent.sock",
        "MEMINI_GIT_HTTPS_TOKEN": TOKEN,
        "https_proxy": "http://proxy.example.test:3128",
    }
    git = Git(options, hostile_environment)

    command = git.command(["ls-tree", "HEAD"], git_dir=tmp_path / "m")
    config = config_of(command.argv)

    assert command.argv[:2] == ("git", f"--git-dir={tmp_path / 'm'}")
    assert command.argv[-2:] == ("ls-tree", "HEAD")
    assert command.name == "ls-tree"
    for setting in (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "credential.helper=",
        "protocol.allow=never",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
        "gc.autoDetach=false",
    ):
        assert setting in config
    assert not [item for item in config if item.endswith(".allow=always")]
    env = command.env
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["HOME"] == "/nonexistent"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["https_proxy"] == "http://proxy.example.test:3128"
    for inherited in (
        "GIT_DIR",
        "GIT_TRACE",
        "GIT_CURL_VERBOSE",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_PARAMETERS",
        "SSH_AUTH_SOCK",
        "MEMINI_GIT_HTTPS_TOKEN",
        PASSWORD_VAR,
    ):
        assert inherited not in env


@pytest.mark.parametrize(
    ("remote", "allowed"),
    [
        (Remote("https", f"https://{OTHER_HOST}/r.git", OTHER_HOST), "protocol.https.allow=always"),
        (
            Remote("http", f"http://{CREDENTIAL_HOST}/r.git", CREDENTIAL_HOST),
            "protocol.http.allow=always",
        ),
        (
            Remote("ssh", f"git@{CREDENTIAL_HOST}:r.git", CREDENTIAL_HOST),
            "protocol.ssh.allow=always",
        ),
        (Remote("file", "/srv/repos/r"), "protocol.file.allow=always"),
    ],
)
def test_only_the_transport_of_the_remote_is_allowed(
    options: GitOptions, remote: Remote, allowed: str
) -> None:
    config = config_of(Git(options).command(["fetch"], git_dir=None, remote=remote).argv)

    assert "protocol.allow=never" in config
    assert [item for item in config if item.endswith(".allow=always")] == [allowed]


def test_the_token_goes_to_its_hosts_over_https_only_and_never_into_arguments(
    options: GitOptions,
) -> None:
    git = Git(options)
    to_token_host = git.command(
        ["fetch"],
        git_dir=None,
        remote=Remote("https", f"https://{CREDENTIAL_HOST}:8443/r.git", CREDENTIAL_HOST, 8443),
    )
    to_other_host = git.command(
        ["fetch"], git_dir=None, remote=Remote("https", f"https://{OTHER_HOST}/r.git", OTHER_HOST)
    )
    over_http = git.command(
        ["fetch"],
        git_dir=None,
        remote=Remote("http", f"http://{CREDENTIAL_HOST}/r.git", CREDENTIAL_HOST),
    )

    assert to_token_host.env[PASSWORD_VAR] == TOKEN
    assert to_token_host.env[USERNAME_VAR] == "x-access-token"
    helpers = [item for item in config_of(to_token_host.argv) if ".helper=" in item]
    assert helpers[0] == "credential.helper="
    assert helpers[1].startswith(f"credential.https://{CREDENTIAL_HOST}:8443.helper=!")
    for command in (to_token_host, to_other_host, over_http):
        assert not any(TOKEN in arg for arg in command.argv)
    for command in (to_other_host, over_http):
        assert PASSWORD_VAR not in command.env
        assert TOKEN not in command.env.values()
        assert [item for item in config_of(command.argv) if ".helper=" in item] == [
            "credential.helper="
        ]


def test_ssh_runs_batch_mode_with_the_configured_key_and_known_hosts(options: GitOptions) -> None:
    command = Git(options).command(
        ["fetch"],
        git_dir=None,
        remote=Remote("ssh", f"git@{CREDENTIAL_HOST}:r.git", CREDENTIAL_HOST),
    )
    ssh = command.env["GIT_SSH_COMMAND"]

    assert ssh.startswith("ssh -F /dev/null ")
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "UpdateHostKeys=no",
        "ForwardAgent=no",
        "'IdentityFile=/run/secrets/fake key'",
        "UserKnownHostsFile=/etc/memini/known_hosts",
    ):
        assert f"-o {option}" in ssh
    assert "ssh.variant=ssh" in config_of(command.argv)


def test_ssh_needs_a_key_and_known_hosts(tmp_path: Path) -> None:
    git = Git(GitOptions(mirror_dir=tmp_path))

    with pytest.raises(RemoteNotAllowed, match="MEMINI_GIT_SSH_KEY_PATH"):
        git.command(
            ["fetch"],
            git_dir=None,
            remote=Remote("ssh", f"git@{CREDENTIAL_HOST}:r.git", CREDENTIAL_HOST),
        )


def test_a_local_remote_is_trusted_as_a_safe_directory(options: GitOptions) -> None:
    config = config_of(
        Git(options).command(["fetch"], git_dir=None, remote=Remote("file", "/srv/repos/r")).argv
    )

    assert "safe.directory=/srv/repos/r" in config
    assert "safe.directory=/srv/repos/r/.git" in config


# --- Running -------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_credential_helper_answers_only_for_the_token_host(options: GitOptions) -> None:
    git = Git(options)
    remote = Remote("https", f"https://{CREDENTIAL_HOST}/r.git", CREDENTIAL_HOST)

    asked = await git.run(
        ["credential", "fill"],
        git_dir=None,
        remote=remote,
        stdin=f"protocol=https\nhost={CREDENTIAL_HOST}\npath=r.git\n\n".encode(),
    )
    # A redirect to another host: the helper is scoped to the remote's host, so git has no answer
    # and, with prompts off, gives up.
    redirected = await git.run(
        ["credential", "fill"],
        git_dir=None,
        remote=remote,
        stdin=f"protocol=https\nhost={OTHER_HOST}\npath=r.git\n\n".encode(),
        check=False,
    )

    assert f"password={TOKEN}\n".encode() in asked.stdout
    assert b"username=x-access-token\n" in asked.stdout
    assert redirected.returncode != 0
    assert TOKEN.encode() not in redirected.stdout
    assert TOKEN not in redirected.stderr


@pytest.mark.anyio
async def test_a_timeout_kills_the_whole_process_group(options: GitOptions, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    # An alias that starts a shell, which records its pid and becomes a long sleep: a child of git
    # in git's process group, like ssh or git-remote-https during a fetch.
    hang = f"alias.hang=!echo $$ > '{pid_file}'; exec sleep 60"
    started = time.monotonic()

    with pytest.raises(GitTimeout, match=r"git hang ran longer than 0\.5 s"):
        await Git(options).run(["-c", hang, "hang"], git_dir=None, timeout=0.5)

    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    assert await _gone(child), "the child of git outlived the timeout"


@pytest.mark.anyio
async def test_cancelling_the_caller_kills_the_process_group(
    options: GitOptions, tmp_path: Path
) -> None:
    pid_file = tmp_path / "child.pid"
    hang = f"alias.hang=!echo $$ > '{pid_file}'; exec sleep 60"
    task = asyncio.ensure_future(Git(options).run(["-c", hang, "hang"], git_dir=None, timeout=30))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _gone(int(pid_file.read_text()))


@pytest.mark.anyio
async def test_a_growing_directory_is_stopped_at_the_size_limit(
    options: GitOptions, tmp_path: Path
) -> None:
    watched = tmp_path / "watched"
    watched.mkdir()
    grow = f"alias.grow=!head -c 100000 /dev/zero > '{watched}/big'; exec sleep 60"
    started = time.monotonic()

    with pytest.raises(MirrorTooLarge) as caught:
        await Git(options).run(
            ["-c", grow, "grow"],
            git_dir=None,
            timeout=30,
            size_limit=SizeLimit(watched, 10_000),
        )

    assert caught.value.limit == 10_000
    assert caught.value.size >= 100_000
    assert time.monotonic() - started < 10


@pytest.mark.anyio
async def test_output_over_the_limit_kills_the_command(options: GitOptions) -> None:
    with pytest.raises(OutputTooLarge):
        await Git(options).run(["version"], git_dir=None, max_output=3)


@pytest.mark.anyio
async def test_a_missing_git_is_reported(tmp_path: Path) -> None:
    git = Git(GitOptions(mirror_dir=tmp_path, git=str(tmp_path / "no-such-git")))

    with pytest.raises(GitUnavailable, match="cannot start git"):
        await git.run(["version"], git_dir=None)


@pytest.mark.anyio
async def test_the_installed_git_is_recent_enough(options: GitOptions) -> None:
    assert await Git(options).version() >= MIN_GIT_VERSION


async def _gone(pid: int, wait: float = 10.0) -> bool:
    """Whether process ``pid`` ends (and is reaped by whoever inherited it) within ``wait`` s."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False
