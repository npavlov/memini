"""Check a release tag against project metadata and print its changelog section."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

# Appended to every release's notes: the app's bundles are attached unsigned (release.yml).
DESKTOP_APP = """### Desktop app

The desktop app is attached for macOS (Apple silicon and Intel, `.dmg`), Windows x64
(`-setup.exe`) and Linux x86_64 (`.AppImage`, `.deb`); `desktop-SHA256SUMS` lists their checksums.
It is not signed, so the system asks once before the first start:

- **macOS:** open the disk image and drag Memini to Applications. Then right-click Memini in
  Applications and choose **Open**, or run
  `xattr -dr com.apple.quarantine /Applications/Memini.app`.
- **Windows:** when SmartScreen says it protected your PC, choose **More info**, then
  **Run anyway**.
- **Linux:** `chmod +x` the AppImage and run it, or install the package with
  `sudo apt install ./<file>.deb`.

It runs a server on this computer, with Docker, or connects to an existing one. See
[Desktop app](https://github.com/npavlov/memini/blob/main/docs/install.md#desktop-app)."""


def release_notes(tag: str, project: Path, changelog: Path) -> str:
    number = r"(?:0|[1-9][0-9]*)"
    match = re.fullmatch(rf"v({number}\.{number}\.{number})(?:-rc\.({number}))?", tag)
    if match is None:
        raise ValueError("expected vX.Y.Z or vX.Y.Z-rc.N")
    version, rc = match.groups()
    package = version if rc is None else f"{version}rc{rc}"
    if tomllib.loads(project.read_text())["project"]["version"] != package:
        raise ValueError(f"set project.version to {package} and refresh uv.lock")
    # The desktop app carries the same version in semver form; scripts/bump-version.py writes all.
    root = project.parent
    cargo, npm = root / "desktop/src-tauri/Cargo.toml", root / "desktop/package.json"
    if cargo.is_file() and tomllib.loads(cargo.read_text())["package"]["version"] != tag[1:]:
        raise ValueError(f"set the desktop app's version to {tag[1:]}: scripts/bump-version.py")
    if npm.is_file() and json.loads(npm.read_text())["version"] != tag[1:]:
        raise ValueError(f"set the desktop app's version to {tag[1:]}: scripts/bump-version.py")
    # The bundles take Cargo.toml's version unless tauri.conf.json names one of its own.
    tauri = root / "desktop/src-tauri/tauri.conf.json"
    if tauri.is_file() and json.loads(tauri.read_text()).get("version", tag[1:]) != tag[1:]:
        raise ValueError("tauri.conf.json names a version: remove it, Cargo.toml's is the app's")
    headings = re.split(r"^## ", changelog.read_text(), flags=re.MULTILINE)[1:]
    for section in headings:
        title, _, body = section.partition("\n")
        heading = rf"\[{re.escape(tag[1:])}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?"
        if re.fullmatch(heading, title) and body.strip():
            return body.strip()
    raise ValueError(f"missing changelog entries for [{tag[1:]}]")


if __name__ == "__main__":
    try:
        notes = release_notes(sys.argv[1], Path("pyproject.toml"), Path("CHANGELOG.md"))
        print(f"{notes}\n\n{DESKTOP_APP}")
    except ValueError as error:
        sys.exit(str(error))
