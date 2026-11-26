"""Fail a release unless everything it ships carries the tag's one version (docs/releasing.md).

    python3 scripts/check-release-versions.py TAG IMAGE DIST DESKTOP PINS

TAG is the release tag (vX.Y.Z or vX.Y.Z-rc.N). IMAGE is the image the release published, with
its digest. DIST holds the Python distributions, DESKTOP the app's bundles, and PINS one file per
platform with the image the frozen launcher of that platform's app names (desktop/sidecar/
freeze.py --report). The version is written as scripts/bump-version.py maps it: X.Y.Z-rc.N for the
tag, the image, the app and the launcher package; X.Y.ZrcN for the Python package.
"""

from __future__ import annotations

import re
import runpy
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The bundles of the app, one per platform and kind (.github/workflows/desktop-app.yml).
BUNDLES = (
    "Memini-{v}-macos-arm64.dmg",
    "Memini-{v}-macos-x86_64.dmg",
    "Memini-{v}-windows-x64-setup.exe",
    "Memini-{v}-linux-x86_64.AppImage",
    "Memini-{v}-linux-amd64.deb",
)


def problems(
    tag: str, image: str, dist: list[str], desktop: list[str], pins: dict[str, str]
) -> list[str]:
    """What does not carry the tag's version; empty when everything does."""
    python_version: Callable[[str], str] = runpy.run_path(str(ROOT / "scripts/bump-version.py"))[
        "python_version"
    ]
    if not tag.startswith("v"):
        return [f"the tag {tag!r} does not start with v"]
    version = tag[1:]
    try:
        python = python_version(version)
    except ValueError as error:
        return [f"the tag {tag!r}: {error}"]
    found: list[str] = []
    if not re.fullmatch(
        rf"ghcr\.io/npavlov/memini:{re.escape(version)}@sha256:[0-9a-f]{{64}}", image
    ):
        found.append(f"the image {image!r} is not ghcr.io/npavlov/memini:{version}@sha256:<digest>")
    wheels = [name for name in dist if name.endswith(".whl")]
    sdists = [name for name in dist if name.endswith(".tar.gz")]
    if not wheels or not sdists:
        found.append(f"the Python distributions are missing: {sorted(dist)}")
    for name in dist:
        if not (name.startswith(f"memini-{python}-") or name == f"memini-{python}.tar.gz"):
            found.append(f"the Python distribution {name} is not version {python}")
    expected = {pattern.format(v=version) for pattern in BUNDLES}
    if set(desktop) != expected:
        found.append(f"the app's bundles are {sorted(desktop)}, expected {sorted(expected)}")
    if not pins:
        found.append("no frozen launcher reported the image it pins")
    for platform, pinned in sorted(pins.items()):
        if pinned != image:
            found.append(f"the launcher of the app for {platform} pins {pinned!r}, not {image!r}")
    return found


def main(arguments: list[str]) -> int:
    if len(arguments) != 5:
        sys.exit(__doc__)
    tag, image, dist, desktop, pins = arguments
    found = problems(
        tag,
        image,
        sorted(path.name for path in Path(dist).iterdir()),
        sorted(path.name for path in Path(desktop).iterdir()),
        {path.stem: path.read_text(encoding="utf-8").strip() for path in Path(pins).iterdir()},
    )
    for problem in found:
        print(f"check-release-versions: {problem}", file=sys.stderr)
    if found:
        return 1
    print(f"Everything in {tag} carries its version; the app's launchers pin {image}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
