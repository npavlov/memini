import json
import re
import tomllib
from pathlib import Path

import memini


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]

    assert memini.__version__ == declared


def test_desktop_app_has_the_same_version() -> None:
    """The app shows its own version; it moves with the service's, written the semver way
    (0.2.0rc1 in Python is 0.2.0-rc.1 in Cargo and npm)."""
    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    semver = re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1-\2.\3", declared["version"])
    cargo = tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))

    assert cargo["package"]["version"] == semver
    assert package["version"] == semver
