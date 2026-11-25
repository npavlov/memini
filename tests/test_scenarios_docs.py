"""The scenario documents: each file has its parts, and the index lists each one. No database
needed.

``docs/scenarios`` holds one file per user scenario and an index, ``README.md``. People and
agents navigate by the index, so a scenario that is missing from it, or listed with another
title, status or issue than its file gives, is lost. The user guide takes a page's title from its
first line, so a scenario file starts with its heading.
"""

import re
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parents[1] / "docs" / "scenarios"

PARTS = ("Who and pain", "Question", "Answer", "Data", "Check")
"""The headings every scenario has, in this order; others may follow."""
STATUSES = frozenset({"works", "ready", "partial", "planned", "to confirm"})

_FILE = re.compile(r"S(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_HEADING = re.compile(r"# S(\d+)\. (.+)")
_FACTS = re.compile(r"\| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|")
_INDEX_ROW = re.compile(
    r"\| \[S(\d+)\]\(([^)]+)\) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]*)\|", re.MULTILINE
)


def _scenario_files() -> list[Path]:
    return sorted(SCENARIOS.glob("S*.md"))


def _facts(path: Path) -> tuple[int, str, str, str]:
    """The number, title, status and issue a scenario file gives ('' for no issue)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    heading = _HEADING.fullmatch(lines[0])
    assert heading, f"{path.name}: the first line is '# S<number>. <question>'"
    assert lines[2].startswith("| Area | Who | Status | Issue |"), f"{path.name}: no facts table"
    row = _FACTS.fullmatch(lines[4])
    assert row, f"{path.name}: the facts table has one row of four cells"
    issue = row[4].strip()
    return int(heading[1]), heading[2], row[3].strip(), "" if issue == "none" else issue


def test_there_are_scenarios() -> None:
    assert _scenario_files()


@pytest.mark.parametrize("path", _scenario_files(), ids=lambda path: path.name)
def test_a_scenario_file_has_its_number_its_parts_and_a_status(path: Path) -> None:
    named = _FILE.fullmatch(path.name)
    assert named, f"{path.name}: named SNN-short-name.md"

    number, _title, status, _issue = _facts(path)

    assert number == int(named[1])
    assert status in STATUSES
    headings = re.findall(r"^## (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    assert tuple(headings[: len(PARTS)]) == PARTS


def test_the_index_lists_every_scenario_as_its_file_gives_it() -> None:
    index = (SCENARIOS / "README.md").read_text(encoding="utf-8")
    listed = {
        int(number): (target, title.strip(), status.strip(), issue.strip())
        for number, target, title, _who, status, issue in _INDEX_ROW.findall(index)
    }
    files = {_facts(path)[0]: path for path in _scenario_files()}

    assert len(files) == len(_scenario_files()), "two scenario files share a number"
    assert sorted(listed) == sorted(files)
    for number, path in files.items():
        _, title, status, issue = _facts(path)
        assert listed[number] == (path.name, title, status, issue)
