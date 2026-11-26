"""One version for everything a release ships: scripts/check-release-versions.py."""

import importlib.util
import re
import runpy
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
TAG = "v0.3.0-rc.2"
IMAGE = "ghcr.io/npavlov/memini:0.3.0-rc.2@sha256:" + "0f" * 32
DIST = ["memini-0.3.0rc2-py3-none-any.whl", "memini-0.3.0rc2.tar.gz"]
DESKTOP = [
    "Memini-0.3.0-rc.2-macos-arm64.dmg",
    "Memini-0.3.0-rc.2-macos-x86_64.dmg",
    "Memini-0.3.0-rc.2-windows-x64-setup.exe",
    "Memini-0.3.0-rc.2-linux-x86_64.AppImage",
    "Memini-0.3.0-rc.2-linux-amd64.deb",
]
PINS = {"macos-15": IMAGE, "macos-15-intel": IMAGE, "ubuntu-24.04": IMAGE, "windows-2022": IMAGE}


def load() -> ModuleType:
    path = ROOT / "scripts/check-release-versions.py"
    spec = importlib.util.spec_from_file_location("check_release_versions", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_release_whose_parts_share_the_tags_version_passes() -> None:
    assert load().problems(TAG, IMAGE, DIST, DESKTOP, PINS) == []
    final = "ghcr.io/npavlov/memini:1.0.0@sha256:" + "0f" * 32
    assert (
        load().problems(
            "v1.0.0",
            final,
            ["memini-1.0.0-py3-none-any.whl", "memini-1.0.0.tar.gz"],
            [name.replace("0.3.0-rc.2", "1.0.0") for name in DESKTOP],
            {"macos-15": final},
        )
        == []
    )


def test_an_image_of_another_version_fails() -> None:
    previous = "ghcr.io/npavlov/memini:0.3.0-rc.1@sha256:" + "0f" * 32
    found = load().problems(TAG, previous, DIST, DESKTOP, {"macos-15": previous})
    assert any("the image" in problem for problem in found)


def test_a_launcher_that_pins_the_previous_image_fails() -> None:
    previous = "ghcr.io/npavlov/memini:0.3.0-rc.1@sha256:" + "0f" * 32
    found = load().problems(TAG, IMAGE, DIST, DESKTOP, {**PINS, "windows-2022": previous})
    assert found == [f"the launcher of the app for windows-2022 pins {previous!r}, not {IMAGE!r}"]
    assert load().problems(TAG, IMAGE, DIST, DESKTOP, {}) == [
        "no frozen launcher reported the image it pins"
    ]


def test_a_python_package_or_a_bundle_of_another_version_fails() -> None:
    check = load()
    stale = ["memini-0.3.0rc1-py3-none-any.whl", "memini-0.3.0rc2.tar.gz"]
    assert any(
        "0.3.0rc1" in problem for problem in check.problems(TAG, IMAGE, stale, DESKTOP, PINS)
    )
    semver = ["memini-0.3.0-rc.2-py3-none-any.whl", "memini-0.3.0rc2.tar.gz"]
    assert check.problems(TAG, IMAGE, semver, DESKTOP, PINS) != []
    bundles = [*DESKTOP[:-1], "Memini-0.3.0-rc.1-linux-amd64.deb"]
    assert any("bundles" in problem for problem in check.problems(TAG, IMAGE, DIST, bundles, PINS))
    assert check.problems(TAG, IMAGE, DIST, DESKTOP[:-1], PINS) != []


def test_a_tag_that_is_no_version_fails() -> None:
    assert load().problems("0.3.0", IMAGE, DIST, DESKTOP, PINS) != []
    assert load().problems("v0.3.0rc2", IMAGE, DIST, DESKTOP, PINS) != []


def test_the_repositorys_launcher_pins_the_release_its_package_names() -> None:
    """The launcher package and its image move together (docs/releasing.md, step 3)."""
    launcher = runpy.run_path(str(ROOT / "scripts/memini-server.py"))
    package = runpy.run_path(str(ROOT / "packaging/server/build.py"))
    match = re.fullmatch(r"ghcr\.io/npavlov/memini:([^@]+)@sha256:[0-9a-f]{64}", launcher["IMAGE"])
    assert match is not None
    assert match.group(1) == package["VERSION"]
    assert package["TAG"] == f"v{package['VERSION']}"
