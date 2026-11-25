import datetime
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

### Added

- A synthetic feature.

## [0.1.0] - 2026-01-02

### Added

- The first release.

[Unreleased]: https://github.com/npavlov/memini/compare/v0.1.0...main
[0.1.0]: https://github.com/npavlov/memini/releases/tag/v0.1.0
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The files a release writes its version into, as small synthetic copies."""
    (tmp_path / "desktop/src-tauri").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    (tmp_path / "desktop/src-tauri/Cargo.toml").write_text(
        '[package]\nname = "memini-desktop"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n'
    )
    (tmp_path / "desktop/src-tauri/Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "serde"\nversion = "1.0.0"\n\n'
        '[[package]]\nname = "memini-desktop"\nversion = "0.1.0"\n'
    )
    (tmp_path / "desktop/package.json").write_text(
        '{\n  "name": "memini-desktop",\n  "version": "0.1.0",\n  "private": true\n}\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def test_bump_writes_every_version_and_opens_the_changelog_section(tree: Path) -> None:
    bump = load("bump-version")

    bump.bump_files(tree, "0.2.0-rc.2", datetime.date(2026, 11, 26))

    assert 'version = "0.2.0rc2"' in (tree / "pyproject.toml").read_text()
    assert 'version = "0.2.0-rc.2"' in (tree / "desktop/src-tauri/Cargo.toml").read_text()
    lock = (tree / "desktop/src-tauri/Cargo.lock").read_text()
    assert 'name = "memini-desktop"\nversion = "0.2.0-rc.2"' in lock
    assert 'name = "serde"\nversion = "1.0.0"' in lock
    assert '"version": "0.2.0-rc.2"' in (tree / "desktop/package.json").read_text()
    changelog = (tree / "CHANGELOG.md").read_text()
    assert (
        "## [Unreleased]\n\n## [0.2.0-rc.2] - 2026-11-26\n\n### Added\n\n- A synthetic" in changelog
    )
    assert (
        "[Unreleased]: https://github.com/npavlov/memini/compare/v0.2.0-rc.2...main\n" in changelog
    )
    assert "[0.2.0-rc.2]: https://github.com/npavlov/memini/releases/tag/v0.2.0-rc.2\n" in changelog

    # What the release run checks then passes, and prints the entries.
    notes = load("release-notes").release_notes(
        "v0.2.0-rc.2", tree / "pyproject.toml", tree / "CHANGELOG.md"
    )
    assert notes == "### Added\n\n- A synthetic feature."


def test_bump_refuses_an_empty_unreleased_section_and_a_bad_version(tree: Path) -> None:
    bump = load("bump-version")
    bump.bump_files(tree, "0.2.0", datetime.date(2026, 11, 26))

    with pytest.raises(ValueError, match=r"no entries under \[Unreleased\]"):
        bump.bump_files(tree, "0.2.1", datetime.date(2026, 11, 26))
    with pytest.raises(ValueError, match=r"expected X\.Y\.Z"):
        bump.python_version("0.2.0rc2")


def test_a_tag_whose_desktop_version_lags_is_refused(tree: Path) -> None:
    notes = load("release-notes")
    (tree / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.2.0"\n')
    changelog = CHANGELOG.replace(
        "## [Unreleased]\n", "## [Unreleased]\n\n## [0.2.0] - 2026-11-26\n"
    )
    (tree / "CHANGELOG.md").write_text(changelog)

    with pytest.raises(ValueError, match=r"desktop app's version to 0\.2\.0"):
        notes.release_notes("v0.2.0", tree / "pyproject.toml", tree / "CHANGELOG.md")
