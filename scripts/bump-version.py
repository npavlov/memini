"""Set the version of a release everywhere it is written, and open its changelog section.

    uv run python scripts/bump-version.py 0.2.0-rc.2

The argument is the version as the tag names it, without the v: X.Y.Z or X.Y.Z-rc.N. The script
writes it to pyproject.toml (in Python's form, 0.2.0rc2), desktop/src-tauri/Cargo.toml,
desktop/src-tauri/Cargo.lock and desktop/package.json (in semver form, 0.2.0-rc.2), refreshes
uv.lock, and moves the entries under [Unreleased] in CHANGELOG.md to a section of the new version
dated today, with its link. It refuses when [Unreleased] holds no entry: a release says what
changed. The steps around it are in docs/releasing.md.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/npavlov/memini"
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"({NUMBER}\.{NUMBER}\.{NUMBER})(?:-rc\.({NUMBER}))?")


def python_version(version: str) -> str:
    """0.2.0-rc.2 as Python writes it: 0.2.0rc2."""
    match = VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"expected X.Y.Z or X.Y.Z-rc.N, not {version!r}")
    release, rc = match.groups()
    return release if rc is None else f"{release}rc{rc}"


def replace_once(text: str, pattern: str, replacement: str, what: str) -> str:
    changed, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"{what} was not found")
    return changed


def bump_files(root: Path, version: str, today: datetime.date) -> None:
    """Write ``version`` into the files of ``root``; uv.lock is left to :func:`main`."""
    python = python_version(version)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        replace_once(
            pyproject.read_text(encoding="utf-8"),
            r'^version = "[^"]*"',
            f'version = "{python}"',
            "the version of pyproject.toml",
        ),
        encoding="utf-8",
    )
    cargo = root / "desktop/src-tauri/Cargo.toml"
    cargo.write_text(
        replace_once(
            cargo.read_text(encoding="utf-8"),
            r'^version = "[^"]*"',
            f'version = "{version}"',
            "the version of Cargo.toml",
        ),
        encoding="utf-8",
    )
    lock = root / "desktop/src-tauri/Cargo.lock"
    lock.write_text(
        replace_once(
            lock.read_text(encoding="utf-8"),
            r'^(name = "memini-desktop"\nversion = )"[^"]*"',
            rf'\g<1>"{version}"',
            "memini-desktop in Cargo.lock",
        ),
        encoding="utf-8",
    )
    package = root / "desktop/package.json"
    package.write_text(
        replace_once(
            package.read_text(encoding="utf-8"),
            r'^(  "version": )"[^"]*"',
            rf'\g<1>"{version}"',
            "the version of package.json",
        ),
        encoding="utf-8",
    )
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    unreleased = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[)", text, re.MULTILINE | re.DOTALL)
    if unreleased is None or not unreleased.group(1).strip():
        raise ValueError("CHANGELOG.md has no entries under [Unreleased] to release")
    if re.search(rf"^## \[{re.escape(version)}\]", text, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md has a section for {version} already")
    text = text.replace(
        "## [Unreleased]\n", f"## [Unreleased]\n\n## [{version}] - {today.isoformat()}\n", 1
    )
    text = replace_once(
        text,
        r"^\[Unreleased\]: .*$",
        f"[Unreleased]: {REPOSITORY}/compare/v{version}...main\n"
        f"[{version}]: {REPOSITORY}/releases/tag/v{version}",
        "the [Unreleased] link of CHANGELOG.md",
    )
    changelog.write_text(text, encoding="utf-8")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        sys.exit(__doc__)
    try:
        bump_files(ROOT, arguments[0], datetime.date.today())
    except ValueError as error:
        sys.exit(f"bump-version: {error}")
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("bump-version: install uv, then run `uv lock` to finish")
    subprocess.run([uv, "lock"], cwd=ROOT, check=True)  # noqa: S603 - resolved uv, fixed argv
    print(f"Version {arguments[0]} written. Next: a PR with these changes (docs/releasing.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
