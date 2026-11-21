"""Check a release tag against project metadata and print its changelog section."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


def release_notes(tag: str, project: Path, changelog: Path) -> str:
    number = r"(?:0|[1-9][0-9]*)"
    match = re.fullmatch(rf"v({number}\.{number}\.{number})(?:-rc\.({number}))?", tag)
    if match is None:
        raise ValueError("expected vX.Y.Z or vX.Y.Z-rc.N")
    version, rc = match.groups()
    package = version if rc is None else f"{version}rc{rc}"
    if tomllib.loads(project.read_text())["project"]["version"] != package:
        raise ValueError(f"set project.version to {package} and refresh uv.lock")
    headings = re.split(r"^## ", changelog.read_text(), flags=re.MULTILINE)[1:]
    for section in headings:
        title, _, body = section.partition("\n")
        heading = rf"\[{re.escape(tag[1:])}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?"
        if re.fullmatch(heading, title) and body.strip():
            return body.strip()
    raise ValueError(f"missing changelog entries for [{tag[1:]}]")


if __name__ == "__main__":
    try:
        print(release_notes(sys.argv[1], Path("pyproject.toml"), Path("CHANGELOG.md")))
    except ValueError as error:
        sys.exit(str(error))
