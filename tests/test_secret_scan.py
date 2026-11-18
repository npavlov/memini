"""The secret scan: the pre-commit hook that `make hooks` installs blocks a commit that adds a key,
and the history scan that CI runs finds a key committed past the hook.

Acceptance of memini-3va. Each test works in a throwaway repository that gets this repository's
hook (.beads/hooks/pre-commit), scan script (scripts/secret-scan.sh) and rules (.gitleaks.toml),
runs `make hooks` there and commits; this repository is never touched. The keys are fake and
assembled at run time, with random parts where a rule wants entropy (tests/redaction_helpers.py),
so no literal of one is committed and the scan of this repository stays clean.

The hook tests need the pinned gitleaks (`make hooks` fetches it). They skip without it, unless
MEMINI_REQUIRE_GITLEAKS=1, as in the secret-scan job of CI, which installs it first."""

import os
import secrets
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from redaction_helpers import PEM, PEM_BODY, fake

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "scripts" / "secret-scan.sh"
RULES = ROOT / ".gitleaks.toml"
HOOK = ".beads/hooks/pre-commit"
COPIED = (HOOK, "scripts/secret-scan.sh", ".gitleaks.toml")
TIMEOUT = 120


def gitleaks_installed() -> bool:
    if shutil.which("bash") is None:
        return False
    done = subprocess.run(  # noqa: S603 - the scan script of this repository, fixed arguments
        [str(SCAN), "path"], capture_output=True, check=False, timeout=TIMEOUT
    )
    return done.returncode == 0


REQUIRED = os.environ.get("MEMINI_REQUIRE_GITLEAKS") == "1"


def fake_github_token() -> str:
    """ghp_ and 36 characters: Fake, then random hex, so that the rule's entropy floor is met."""
    return fake("ghp_", "Fake", secrets.token_hex(16))


def fake_memini_token() -> str:
    """memini_ and 43 base64url characters: Fake, then random ones (the memini-token rule)."""
    return fake("memini_", "Fake", secrets.token_urlsafe(32)[:39])


class Throwaway:
    """A new repository with this repository's hook, scan and rules, set up by `make hooks`."""

    def __init__(self, path: Path) -> None:
        self.path = path / "repo"
        for rel in COPIED:
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, target)  # keeps the executable bits
        # A stand-in for bd: the beads part of the hook runs and passes, with or without bd here.
        stand_ins = path / "bin"
        stand_ins.mkdir()
        (stand_ins / "bd").write_text("#!/bin/sh\nexit 0\n")
        (stand_ins / "bd").chmod(0o755)
        # The caller's environment, for the gitleaks cache (HOME, XDG_CACHE_HOME, MEMINI_TOOLS_DIR),
        # without its git settings: no global or system config, no GIT_DIR of an outer hook.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env |= {
            "PATH": os.pathsep.join((str(stand_ins), os.environ.get("PATH", "/usr/bin:/bin"))),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.invalid",
            "LC_ALL": "C",
        }
        self.run("git", "init", "--quiet", "--initial-branch=main")
        self.run("make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "hooks")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(  # noqa: S603 - git, make and the scan script, fixed arguments
            args,
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT,
        )
        if check:
            assert done.returncode == 0, f"{args}: {done.stdout}{done.stderr}"
        return done

    def commit(self, files: dict[str, str | None], *, verify: bool = True) -> str:
        """Write (or, for ``None``, delete) ``files`` and commit; returns what git and the hook
        printed. Asserts nothing: the caller checks ``head()``."""
        for rel, text in files.items():
            if text is None:
                self.run("git", "rm", "--quiet", "--", rel)
                continue
            (self.path / rel).write_text(text)
            self.run("git", "add", "--", rel)
        args = ["git", "commit", "--quiet", "-m", "change"]
        done = self.run(*args, *([] if verify else ["--no-verify"]), check=False)
        return done.stdout + done.stderr

    def head(self) -> str | None:
        done = self.run("git", "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        return done.stdout.strip() or None

    def scan_history(self) -> subprocess.CompletedProcess[str]:
        return self.run("scripts/secret-scan.sh", "history", check=False)


@pytest.fixture
def repo(tmp_path: Path) -> Throwaway:
    for tool in ("git", "make", "bash"):
        if shutil.which(tool) is None:
            pytest.skip(f"needs {tool}")
    if not REQUIRED and not gitleaks_installed():
        pytest.skip("the pinned gitleaks is not installed (make hooks); CI requires it")
    repo = Throwaway(tmp_path)
    output = repo.commit({"README.md": "# A throwaway repository\n"})
    assert repo.head() is not None, output
    return repo


# --- The pre-commit hook -------------------------------------------------------------------------


@pytest.mark.git
def test_make_hooks_points_git_at_the_versioned_hooks(repo: Throwaway) -> None:
    done = repo.run("git", "config", "--get", "core.hooksPath")
    assert done.stdout.strip() == ".beads/hooks"


# A fake key for each kind of rule: a default rule without an entropy floor, a default rule
# with one, and the rule of .gitleaks.toml.
FAKE_KEYS: dict[str, Callable[[], str]] = {
    "private-key": lambda: PEM,
    "github-pat": fake_github_token,
    "memini-token": fake_memini_token,
}


@pytest.mark.git
@pytest.mark.parametrize("rule", sorted(FAKE_KEYS))
def test_the_hook_blocks_a_commit_that_adds_a_key(repo: Throwaway, rule: str) -> None:
    secret = FAKE_KEYS[rule]()
    before = repo.head()
    output = repo.commit({"settings.py": f'KEY = """{secret}"""\n'})
    assert repo.head() == before, "the commit went through"
    assert f"RuleID:      {rule}" in output
    assert "settings.py" in output
    # Redacted: the finding names the rule, the file and the line, never the key.
    assert secret not in output
    assert PEM_BODY[0] not in output


@pytest.mark.git
def test_the_hook_lets_a_commit_without_keys_through(repo: Throwaway) -> None:
    before = repo.head()
    output = repo.commit({"notes.md": "The key to the shed hangs by the door.\n"})
    assert repo.head() != before, output
    assert "no leaks found" in output  # the scan ran


# --- The history scan (CI) -----------------------------------------------------------------------


@pytest.mark.git
def test_the_history_scan_finds_a_key_committed_past_the_hook(repo: Throwaway) -> None:
    assert repo.scan_history().returncode == 0

    token = fake_github_token()
    repo.commit({"deploy.sh": f"export TOKEN={token}\n"}, verify=False)
    # Removing it again does not help: the key stays in the history, which is what CI scans.
    before = repo.head()
    repo.commit({"deploy.sh": None})
    assert repo.head() != before

    done = repo.scan_history()
    assert done.returncode == 1
    assert "RuleID:      github-pat" in done.stdout + done.stderr
    assert token not in done.stdout + done.stderr


# --- The rules -----------------------------------------------------------------------------------


def test_the_rules_extend_the_defaults_and_allow_by_shape_only() -> None:
    rules: dict[str, Any] = tomllib.loads(RULES.read_text())
    assert rules["extend"] == {"useDefault": True}
    assert "allowlist" not in rules  # the global form, which would apply to every rule
    allowlists: list[dict[str, Any]] = rules["allowlists"]
    assert 0 < len(allowlists) <= 5, "keep the allowlist small"
    for entry in allowlists:
        assert entry["description"]
        assert entry["targetRules"] == ["generic-api-key"]
        assert entry["regexes"]
        assert not {"paths", "commits", "stopwords"} & entry.keys(), entry["description"]
    assert [rule["id"] for rule in rules["rules"]] == ["memini-token"]


def test_exceptions_live_in_the_rules_only() -> None:
    assert not (ROOT / ".gitleaksignore").exists(), "an exception goes into .gitleaks.toml"
    assert "--ignore-gitleaks-allow" in SCAN.read_text()
