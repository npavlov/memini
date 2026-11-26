"""The repository the benchmark syncs: a local repository as it is, or a public one cloned once.

A local path is used in place: the benchmark's repository points at it, and the mirror is fetched
from it through ``upload-pack`` as the service does for any local source, so nothing is written to
it; it must hold the whole history of the ref (not a shallow clone, which a mirror cannot fetch
from). An https URL is cloned once into the cache directory, bare and shallow (one commit of one
branch or tag), and reused by later runs. A mirror refuses refs that would make it shallow, so the
clone's tree is committed again as a commit without parents (fixed author and dates, so the same
tree gives the same commit on every run), the ref is moved there and the clone stops being shallow;
the upstream commit is kept in the report. The history walk of a sync then finds one commit: the
time it costs is small either way (requirements, section 9, Code sync).

The clone is hardened like the service's own git (:mod:`memini.git.command`): an environment built
from scratch, no user or system config, no credential helper, no prompts, no hooks, only the https
transport. A public repository is untrusted data: nothing from it is run, it is only read by git
and the chunker.
"""

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_REF",
    "DEFAULT_REPOSITORY",
    "PreparedRepository",
    "RepositoryError",
    "default_cache_dir",
    "prepare_repository",
]

DEFAULT_REPOSITORY: Final = "https://github.com/django/django.git"
"""The repository of the published numbers (docs/operations/performance.md): Django, BSD-3-Clause,
Python with JavaScript, HTML and documentation, about 7,000 files."""
DEFAULT_REF: Final = "refs/tags/6.1.3"
"""Its tag the numbers were measured at; a tag, so a later run reads the same tree."""

_CLONE_TIMEOUT: Final = 1800.0
_UPSTREAM_FILE: Final = "memini-bench-upstream"
_IDENTITY: Final = {
    "GIT_AUTHOR_NAME": "Memini Bench",
    "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Memini Bench",
    "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
_SLUG: Final = re.compile(r"[^A-Za-z0-9._-]+")


class RepositoryError(Exception):
    """The repository cannot be used: not a git repository, a URL that is not https, or a clone
    that failed."""


@dataclass(frozen=True, slots=True)
class PreparedRepository:
    """Where the benchmark's repository points, and what it tracks."""

    location: Path
    """An absolute path of a git repository (bare or not)."""
    ref: str
    """The tracked ref: ``refs/heads/<name>`` or ``refs/tags/<name>``."""
    origin: str
    """What was asked for: the path or URL."""
    cloned: bool
    """Whether this run cloned it (False: a local path, or a clone cached by an earlier run)."""
    upstream_commit: str | None = None
    """For a URL, the commit the ref named upstream; the clone's own commit has its tree."""


def default_cache_dir(environ: dict[str, str] | None = None) -> Path:
    """``$XDG_CACHE_HOME/memini/bench``, or ``~/.cache/memini/bench``."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CACHE_HOME", "")
    root = Path(base) if os.path.isabs(base) else Path.home() / ".cache"
    return root / "memini" / "bench"


def full_ref(ref: str) -> str:
    """``main`` is ``refs/heads/main``; a full name stays as it is."""
    return ref if ref.startswith("refs/") else f"refs/heads/{ref}"


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    }


_HARDENING: Final = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "submodule.recurse=false",
)


def _git(
    args: list[str], *, cwd: Path, timeout: float, extra_env: dict[str, str] | None = None
) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
            ["git", *_HARDENING, *args],  # noqa: S607
            cwd=cwd,
            env=_env(cwd) | (extra_env or {}),
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RepositoryError("the benchmark needs the git executable on PATH") from None
    except subprocess.CalledProcessError as error:
        lines = error.stderr.decode(errors="replace").strip().splitlines()
        raise RepositoryError(f"git {args[0]} failed: {lines[-1] if lines else error}") from None
    except subprocess.TimeoutExpired:
        raise RepositoryError(f"git {args[0]} timed out") from None
    return done.stdout.decode(errors="replace").strip()


def _has_ref(repository: Path, ref: str) -> bool:
    try:
        _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repository, timeout=60)
    except RepositoryError:
        return False
    return True


def prepare_repository(origin: str, ref: str, *, cache_dir: Path) -> PreparedRepository:
    """The repository at ``origin``, a local path or an https URL, with ``ref`` (see the module).

    Raises:
        RepositoryError: a path that is not a git repository or lacks ``ref``, a URL that is not
            https or holds a user name, or a clone that failed.
    """
    tracked = full_ref(ref)
    path = Path(origin).expanduser()
    if path.exists():
        location = path.resolve()
        if not _has_ref(location, tracked):
            raise RepositoryError(f"{location} is not a git repository with {tracked}")
        return PreparedRepository(location, tracked, origin, cloned=False)
    parts = urlsplit(origin)
    if parts.scheme != "https" or not parts.hostname:
        raise RepositoryError("the repository is a local path or an https URL")
    if parts.username or parts.password:
        raise RepositoryError("a repository URL may not hold a user name or password")
    name = tracked.removeprefix("refs/heads/").removeprefix("refs/tags/")
    digest = hashlib.sha256(f"{origin}\n{tracked}".encode()).hexdigest()[:12]
    slug = _SLUG.sub("-", f"{parts.hostname}{parts.path}".removesuffix(".git")).strip("-")
    target = cache_dir / "repos" / f"{slug}-{_SLUG.sub('-', name)}-{digest}.git"
    upstream_file = target / _UPSTREAM_FILE
    if target.exists() and _has_ref(target, tracked) and upstream_file.exists():
        upstream = upstream_file.read_text(encoding="ascii").strip()
        return PreparedRepository(target, tracked, origin, cloned=False, upstream_commit=upstream)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    if partial.exists():  # left by a clone that stopped
        shutil.rmtree(partial)
    _git(
        [
            "clone",
            "--quiet",
            "--bare",
            "--depth=1",
            "--single-branch",
            "--no-tags",
            f"--branch={name}",
            origin,
            str(partial),
        ],
        cwd=target.parent,
        timeout=_CLONE_TIMEOUT,
    )
    upstream = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=partial, timeout=60)
    tree = _git(["rev-parse", "--verify", f"{upstream}^{{tree}}"], cwd=partial, timeout=60)
    commit = _git(
        ["commit-tree", tree, "-m", f"memini bench: {origin} {tracked} at {upstream}"],
        cwd=partial,
        timeout=60,
        extra_env=_IDENTITY,
    )
    _git(["update-ref", tracked, commit], cwd=partial, timeout=60)
    _git(["update-ref", "--no-deref", "HEAD", commit], cwd=partial, timeout=60)
    (partial / "shallow").unlink(missing_ok=True)
    (partial / _UPSTREAM_FILE).write_text(upstream + "\n", encoding="ascii")
    partial.rename(target)
    if not _has_ref(target, tracked):
        raise RepositoryError(f"the clone of {origin} has no {tracked}")
    return PreparedRepository(target, tracked, origin, cloned=True, upstream_commit=upstream)
