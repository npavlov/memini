"""Shared by the git tests: a repository a test writes to, as a user would. Content and identities
are synthetic; author and committer dates are fixed, so commit ids are the same on every run."""

import os
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def stamp(name: str, committed: datetime, *, authored: datetime | None = None) -> dict[str, str]:
    """The ``env`` of a commit by the invented author ``name`` ("Ann Example" writes from
    ``ann@example.test``) at ``committed``, written at ``authored`` if that is another time."""
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": f"{name.split()[0].lower()}@example.test",
        "GIT_AUTHOR_DATE": (authored or committed).isoformat(),
        "GIT_COMMITTER_DATE": committed.isoformat(),
    }


class SourceRepo:
    """A repository a test writes to, as a user would: the source a mirror is made from."""

    def __init__(self, path: Path, *, bare: bool = False) -> None:
        self.path = path
        self._commits = 0
        path.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch=main", *(["--bare"] if bare else []))
        # No background gc or maintenance: nothing may change the source behind the test's back.
        self.git("config", "gc.auto", "0")
        self.git("config", "maintenance.auto", "false")

    def git(
        self, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git in the repository. ``env`` replaces variables of the fixed environment: a test
        that needs its own authors or times sets ``GIT_AUTHOR_NAME``, ``GIT_COMMITTER_DATE`` …"""
        day = 1 + self._commits
        date = f"2026-01-{day:02d}T12:00:00+00:00"
        fixed = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.invalid",
            "GIT_COMMITTER_DATE": date,
            "LC_ALL": "C",
        }
        done = subprocess.run(  # noqa: S603 - fixed git arguments in a test
            ["git", *args],  # noqa: S607
            cwd=self.path,
            env={**fixed, **(env or {})},
            input=stdin,
            capture_output=True,
            check=True,
        )
        return done.stdout.decode().strip()

    def commit(
        self,
        files: Mapping[str, str | bytes | None],
        message: str = "change",
        *,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Write (or, for ``None``, delete) ``files`` and commit them; returns the commit id.
        ``env`` is passed to :meth:`git` for the commit."""
        for name, content in files.items():
            target = self.path / name
            if content is None:
                self.git("rm", "--quiet", "--", name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
            self.git("add", "--", name)
        self._commits += 1
        self.git("commit", "--quiet", "--allow-empty", "-m", message, env=env)
        return self.head()

    def head(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def refs(self) -> dict[str, str]:
        listing = self.git(
            "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags"
        )
        return dict(line.split(" ") for line in listing.splitlines() if line)
