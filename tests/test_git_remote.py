"""Tests for memini.git.remote and memini.git.options: which source locations are allowed, and
the options built from the settings. No process runs here."""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from memini.config import load_settings
from memini.git import (
    GitOptions,
    Remote,
    RemoteNotAllowed,
    Transport,
    default_mirror_dir,
    parse_remote,
)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """A local root with one repository directory in it, and the mirror directory."""
    sources = tmp_path / "sources"
    (sources / "repo").mkdir(parents=True)
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    return sources, mirrors


@pytest.fixture
def options(roots: tuple[Path, Path]) -> GitOptions:
    sources, mirrors = roots
    return GitOptions(mirror_dir=mirrors, local_roots=(sources,))


EXAMPLE = "git.example.test"
TEAM_REPO = f"{EXAMPLE}/team/repo.git"


@pytest.mark.parametrize(
    ("location", "transport", "url", "host", "port"),
    [
        (f"https://{TEAM_REPO}", "https", f"https://{TEAM_REPO}", EXAMPLE, None),
        ("HTTPS://Git.Example.test/repo", "https", "https://Git.Example.test/repo", EXAMPLE, None),
        (f"https://{EXAMPLE}:8443/repo", "https", f"https://{EXAMPLE}:8443/repo", EXAMPLE, 8443),
        ("https://192.0.2.10/r.git", "https", "https://192.0.2.10/r.git", "192.0.2.10", None),
        (f"ssh://git@{TEAM_REPO}", "ssh", f"ssh://git@{TEAM_REPO}", EXAMPLE, None),
        (f"ssh://{EXAMPLE}:2222/repo", "ssh", f"ssh://{EXAMPLE}:2222/repo", EXAMPLE, 2222),
        (f"git@{EXAMPLE}:team/repo.git", "ssh", f"git@{EXAMPLE}:team/repo.git", EXAMPLE, None),
        (f"{EXAMPLE}:/srv/repo.git", "ssh", f"{EXAMPLE}:/srv/repo.git", EXAMPLE, None),
    ],
)  # fmt: skip
def test_network_locations_that_are_allowed(
    options: GitOptions,
    location: str,
    transport: Transport,
    url: str,
    host: str,
    port: int | None,
) -> None:
    assert parse_remote(location, options) == Remote(transport, url, host, port)


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("", "control characters"),
        (" https://git.example.test/repo", "whitespace"),
        ("https://git.example.test/repo\n", "whitespace"),
        ("https://git.example.test/re\x00po", "control characters"),
        ("ext::sh -c touch% /tmp/pwned", "transport helpers"),
        ("fd::17", "transport helpers"),
        ("git.example.test::repo", "transport helpers"),
        ("git://git.example.test/repo.git", "scheme git://"),
        ("ftp://git.example.test/repo.git", "scheme ftp://"),
        ("rsync://git.example.test/repo.git", "scheme rsync://"),
        ("http://git.example.test/repo.git", "plain http"),
        ("https://user:fake-password@git.example.test/repo.git", "no user name or password"),
        ("https://user@git.example.test/repo.git", "no user name or password"),
        ("https://git.example.test/repo.git?x=1", "no query or fragment"),
        ("https://git.example.test/repo.git#main", "no query or fragment"),
        ("https://git.example.test", "no repository path"),
        ("https://git.example.test/", "no repository path"),
        ("https://git.example.test:99999/repo", "port"),
        ("https://-oProxyCommand=x/repo", "host"),
        ("ssh://-oProxyCommand=touch%20x/repo", "host"),
        ("ssh://user:fake-password@git.example.test/repo", "no password"),
        ("ssh://-user@git.example.test/repo", "user name"),
        ("-oProxyCommand=touch:x", "not a URL"),
        ("git.example.test:-repo", "not a URL"),
        ("relative/path", "not a URL"),
        ("./repo", "not a URL"),
        ("~/repo", "not a URL"),
        ("file://other.example.test/srv/repo", "localhost"),
    ],
)
def test_locations_that_are_refused(options: GitOptions, location: str, reason: str) -> None:
    with pytest.raises(RemoteNotAllowed, match=reason):
        parse_remote(location, options)


def test_plain_http_is_allowed_only_when_set(roots: tuple[Path, Path]) -> None:
    sources, mirrors = roots
    allowing = GitOptions(mirror_dir=mirrors, local_roots=(sources,), allow_http=True)

    remote = parse_remote("http://git.example.test/repo.git", allowing)

    assert remote == Remote("http", "http://git.example.test/repo.git", "git.example.test")


def test_a_local_path_under_a_root_is_allowed_and_resolved(
    options: GitOptions, roots: tuple[Path, Path]
) -> None:
    sources, _ = roots
    real = os.path.realpath(sources / "repo")

    assert parse_remote(str(sources / "repo"), options) == Remote("file", real)
    assert parse_remote(f"file://{sources}/repo", options) == Remote("file", real)
    assert parse_remote(f"file://localhost{sources}/repo", options) == Remote("file", real)
    assert parse_remote(str(sources / "other" / ".." / "repo"), options) == Remote("file", real)


def test_local_paths_are_refused_without_roots(roots: tuple[Path, Path]) -> None:
    sources, mirrors = roots
    no_roots = GitOptions(mirror_dir=mirrors)

    with pytest.raises(RemoteNotAllowed, match="MEMINI_GIT_LOCAL_ROOTS"):
        parse_remote(str(sources / "repo"), no_roots)
    with pytest.raises(RemoteNotAllowed, match="MEMINI_GIT_LOCAL_ROOTS"):
        parse_remote(f"file://{sources}/repo", no_roots)


def test_a_local_path_outside_the_roots_is_refused(
    options: GitOptions, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    sources, _ = roots
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (sources / "link").symlink_to(elsewhere)

    for location in (
        str(elsewhere),
        str(sources / ".." / "elsewhere"),
        str(sources / "link"),  # a symlink inside a root that points out of it
        "/etc",
    ):
        with pytest.raises(RemoteNotAllowed, match="outside"):
            parse_remote(location, options)


def test_the_mirror_directory_is_never_a_source(roots: tuple[Path, Path]) -> None:
    sources, _ = roots
    # The mirrors live inside an allowed root, as when the root is a whole home directory.
    inside = GitOptions(mirror_dir=sources / "mirrors", local_roots=(sources,))
    (sources / "mirrors" / "some-source").mkdir(parents=True)

    with pytest.raises(RemoteNotAllowed, match="mirror directory"):
        parse_remote(str(sources / "mirrors" / "some-source"), inside)
    with pytest.raises(RemoteNotAllowed, match="mirror directory"):
        parse_remote(str(sources / "mirrors"), inside)


def test_options_refuse_a_token_without_hosts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hosts"):
        GitOptions(mirror_dir=tmp_path, https_token=SecretStr("fake-token-not-real-0000"))
    with pytest.raises(ValueError, match="absolute"):
        GitOptions(mirror_dir=Path("relative"))


def test_options_follow_the_settings() -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": "postgresql://fake:fake@db.invalid:5432/memini",
            "MEMINI_GIT_MIRROR_DIR": "/var/lib/memini/mirrors",
            "MEMINI_GIT_FETCH_TIMEOUT": "90",
            "MEMINI_GIT_MAX_MIRROR_SIZE": "1G",
            "MEMINI_GIT_LOCAL_ROOTS": "/srv/repos",
            "MEMINI_GIT_HTTPS_TOKEN": "fake-token-not-real-0001",
            "MEMINI_GIT_HTTPS_TOKEN_HOSTS": "git.example.test",
        }
    )

    options = GitOptions.from_settings(settings)

    assert options.mirror_dir == Path("/var/lib/memini/mirrors")
    assert options.fetch_timeout == 90
    assert options.max_mirror_size == 1024**3
    assert options.local_roots == (Path("/srv/repos"),)
    assert options.https_token == settings.git_https_token
    assert options.https_token_hosts == frozenset({"git.example.test"})
    assert "fake-token-not-real-0001" not in repr(options)


def test_the_default_mirror_directory_is_in_the_user_data_directory() -> None:
    assert default_mirror_dir({"XDG_DATA_HOME": "/data/someone"}) == Path(
        "/data/someone/memini/mirrors"
    )
    assert default_mirror_dir({"XDG_DATA_HOME": "relative"}) == (
        Path.home() / ".local" / "share" / "memini" / "mirrors"
    )
    assert default_mirror_dir({}) == Path.home() / ".local" / "share" / "memini" / "mirrors"
