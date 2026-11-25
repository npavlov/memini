"""Tests for MirrorManager.log, the history walk behind list_commits, with a repository made
here. Authors, messages and files are invented; every commit has a fixed author and time."""

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from git_helpers import SourceRepo, stamp
from memini.git import (
    Commit,
    CommitChange,
    GitCommandError,
    GitOptions,
    GitTimeout,
    InvalidPath,
    InvalidRevision,
    Mirror,
    MirrorManager,
)
from memini.git.mirror import _parse_log  # pyright: ignore[reportPrivateUsage]

pytestmark = [
    pytest.mark.git,
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

SOURCE_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")
ANN, BO = "Ann Example", "Bo Example"

LEDGER = "".join(f"entry {number}\n" for number in range(1, 13))
"""Twelve lines: two branches change its ends, and git merges them without a conflict."""


def day(number: int, hour: int = 10) -> datetime:
    return datetime(2026, 3, number, hour, tzinfo=UTC)


def build_ledger(path: Path) -> SourceRepo:
    """main: five commits and two merges. ``side`` adds a file and is merged cleanly; ``both``
    changes the ledger main changes too. ``elsewhere`` is a branch main never merges."""
    repo = SourceRepo(path)
    repo.commit(
        {"README.md": "# Ledger\n", "src/fees/calc.py": "RATE = 2\n", "ledger.txt": LEDGER},
        "Add the fee calculation\n\nThe rate is fixed for now.\nA table comes later.",
        env=stamp(ANN, day(1), authored=day(1, hour=8)),
    )
    repo.commit({"src/fees/calc.py": "RATE = 3\n"}, "Raise the rate", env=stamp(BO, day(2)))
    repo.git("checkout", "--quiet", "-b", "side")
    repo.commit({"src/fees/table.py": "TABLE = {}\n"}, "Add a fee table", env=stamp(ANN, day(3)))
    repo.git("checkout", "--quiet", "main")
    repo.commit({"README.md": "# Ledger\n\nFees.\n"}, "Describe fees", env=stamp(BO, day(4)))
    repo.git("merge", "--quiet", "--no-ff", "side", "-m", "Merge side", env=stamp(BO, day(5)))
    repo.git("checkout", "--quiet", "-b", "both")
    repo.commit(
        {"ledger.txt": LEDGER.replace("entry 12\n", "entry twelve\n")},
        "Spell out the last entry",
        env=stamp(ANN, day(6)),
    )
    repo.git("checkout", "--quiet", "main")
    repo.commit(
        {"ledger.txt": LEDGER.replace("entry 1\n", "entry one\n")},
        "Spell out the first entry",
        env=stamp(BO, day(7)),
    )
    repo.git("merge", "--quiet", "--no-ff", "both", "-m", "Merge both", env=stamp(BO, day(8)))
    repo.commit({"README.md": None}, "Drop the readme", env=stamp(ANN, day(9)))
    repo.git("checkout", "--quiet", "-b", "elsewhere")
    repo.commit({"src/fees/calc.py": "RATE = 9\n"}, "Never merged", env=stamp(ANN, day(10)))
    repo.git("checkout", "--quiet", "main")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> SourceRepo:
    return build_ledger(tmp_path / "sources" / "ledger")


@pytest.fixture
def options(tmp_path: Path) -> GitOptions:
    return GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(tmp_path / "sources",),
        fetch_timeout=30,
        command_timeout=30,
    )


@pytest.fixture
def manager(options: GitOptions) -> MirrorManager:
    return MirrorManager(options)


@pytest.fixture
async def mirror(manager: MirrorManager, repo: SourceRepo) -> Mirror:
    return (await manager.sync(SOURCE_ID, str(repo.path))).mirror


def subjects(commits: tuple[Commit, ...]) -> list[str]:
    return [commit.subject for commit in commits]


MAIN = [
    "Drop the readme",
    "Merge both",
    "Spell out the first entry",
    "Spell out the last entry",
    "Merge side",
    "Describe fees",
    "Add a fee table",
    "Raise the rate",
    "Add the fee calculation",
]
"""What is reachable from main, newest first."""


async def test_log_lists_commits_newest_first_with_author_message_and_paths(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")

    log = await manager.log(mirror, [head], max_commits=50)

    assert subjects(log.commits) == MAIN
    assert (log.complete, log.starts) == (True, (head,))
    newest, oldest = log.commits[0], log.commits[-1]
    assert newest.oid == head
    assert head.startswith(newest.abbrev)
    assert len(newest.abbrev) >= 7
    assert newest.parents == (repo.head("main~1"),)
    assert (newest.author_name, newest.author_email) == (ANN, "ann@example.test")
    assert newest.committed_at == day(9)
    assert newest.changes == (CommitChange("deleted", "README.md"),)
    assert newest.body == ""
    assert oldest.parents == ()
    assert (oldest.authored_at, oldest.committed_at) == (day(1, hour=8), day(1))
    assert oldest.body == "The rate is fixed for now.\nA table comes later."
    assert oldest.changes == (
        CommitChange("added", "README.md"),
        CommitChange("added", "ledger.txt"),
        CommitChange("added", "src/fees/calc.py"),
    )
    raised = log.commits[-2]
    assert (raised.author_name, raised.author_email) == (BO, "bo@example.test")
    assert raised.changes == (CommitChange("modified", "src/fees/calc.py"),)


async def test_log_walks_only_from_the_commits_it_is_given(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    main, elsewhere, side = repo.head("main"), repo.head("elsewhere"), repo.head("side")
    gone = "1" * 40

    from_main = await manager.log(mirror, [main], max_commits=50)
    from_both = await manager.log(mirror, [main, elsewhere], max_commits=50)
    from_side = await manager.log(mirror, [side], max_commits=50)
    one_gone = await manager.log(mirror, [gone, main], max_commits=50)
    all_gone = await manager.log(mirror, [gone], max_commits=50)
    none = await manager.log(mirror, [], max_commits=50)

    # The mirror holds the branch, and its HEAD names main: neither is walked unless given.
    assert "Never merged" not in subjects(from_main.commits)
    assert subjects(from_both.commits) == ["Never merged", *MAIN]
    assert subjects(from_side.commits) == [
        "Add a fee table",
        "Raise the rate",
        "Add the fee calculation",
    ]
    assert (subjects(one_gone.commits), one_gone.starts) == (MAIN, (main,))
    for empty in (all_gone, none):
        assert (empty.commits, empty.complete, empty.starts) == ((), True, ())


async def test_log_narrows_to_literal_paths(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")

    one_file = await manager.log(mirror, [head], paths=["src/fees/calc.py"], max_commits=50)
    directory = await manager.log(mirror, [head], paths=["src/fees"], max_commits=50)
    two = await manager.log(mirror, [head], paths=["README.md", "src/fees"], max_commits=50)
    pattern = await manager.log(mirror, [head], paths=["src/fees/*.py"], max_commits=50)

    assert subjects(one_file.commits) == ["Raise the rate", "Add the fee calculation"]
    assert subjects(directory.commits) == [
        "Add a fee table",
        "Raise the rate",
        "Add the fee calculation",
    ]
    # Only the paths asked for are listed, also for a commit that changed others.
    assert directory.commits[-1].changes == (CommitChange("added", "src/fees/calc.py"),)
    # The merge joined a change of each path and changed neither itself: printed, with no path.
    assert subjects(two.commits) == [
        "Drop the readme",
        "Merge side",
        "Describe fees",
        "Add a fee table",
        "Raise the rate",
        "Add the fee calculation",
    ]
    assert two.commits[1].changes == ()
    assert pattern.commits == ()  # taken literally: no file has that name


async def test_log_takes_since_inclusive_and_until_exclusive(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")
    second = timedelta(seconds=1)

    window = await manager.log(mirror, [head], since=day(4), until=day(7), max_commits=50)
    just_after = await manager.log(mirror, [head], since=day(8) + second, max_commits=50)
    just_before = await manager.log(mirror, [head], until=day(2), max_commits=50)
    fraction = await manager.log(
        mirror, [head], until=day(2) + timedelta(milliseconds=1), max_commits=50
    )
    other_zone = await manager.log(
        mirror, [head], since=day(9).astimezone(), until=day(9) + second, max_commits=50
    )
    before_time = await manager.log(
        mirror, [head], until=datetime(1969, 1, 1, tzinfo=UTC), max_commits=50
    )

    assert subjects(window.commits) == ["Spell out the last entry", "Merge side", "Describe fees"]
    assert subjects(just_after.commits) == ["Drop the readme"]
    assert subjects(just_before.commits) == ["Add the fee calculation"]
    assert subjects(fraction.commits) == ["Raise the rate", "Add the fee calculation"]
    assert subjects(other_zone.commits) == ["Drop the readme"]
    assert before_time.commits == ()
    with pytest.raises(ValueError, match="needs a zone"):
        await manager.log(mirror, [head], since=datetime(2026, 3, 1), max_commits=50)


async def test_a_merge_lists_what_it_changed_against_every_parent(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")

    log = await manager.log(mirror, [head], max_commits=50)
    ledger = await manager.log(mirror, [head], paths=["ledger.txt"], max_commits=50)
    table = await manager.log(mirror, [head], paths=["src/fees/table.py"], max_commits=50)

    by_subject = {commit.subject: commit for commit in log.commits}
    clean, combined = by_subject["Merge side"], by_subject["Merge both"]
    assert (len(clean.parents), clean.changes) == (2, ())  # the branch brought it, not the merge
    assert len(combined.parents) == 2
    assert combined.changes == (CommitChange("modified", "ledger.txt"),)
    assert subjects(ledger.commits) == [
        "Merge both",
        "Spell out the first entry",
        "Spell out the last entry",
        "Add the fee calculation",
    ]
    assert subjects(table.commits) == ["Add a fee table"]  # as `git log -- path`: no merge


async def test_log_stops_at_max_commits_and_says_so(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")

    cut = await manager.log(mirror, [head], max_commits=3)
    exact = await manager.log(mirror, [head], max_commits=len(MAIN))
    whole = await manager.log(mirror, [head], max_commits=len(MAIN) + 1)
    narrowed = await manager.log(mirror, [head], paths=["src/fees"], max_commits=2)

    assert (subjects(cut.commits), cut.complete) == (MAIN[:3], False)
    assert (subjects(exact.commits), exact.complete) == (MAIN, False)  # it cannot tell
    assert (subjects(whole.commits), whole.complete) == (MAIN, True)
    # The count is of commits that match, not of commits passed on the way to them.
    assert subjects(narrowed.commits) == ["Add a fee table", "Raise the rate"]
    assert narrowed.complete is False


async def test_log_refuses_what_is_not_an_id_or_a_plain_path(
    manager: MirrorManager, mirror: Mirror, repo: SourceRepo
) -> None:
    head = repo.head("main")

    for start in ("main", "HEAD", "--all", head[:12], f"{head}^"):
        with pytest.raises(InvalidRevision):
            await manager.log(mirror, [start], max_commits=5)
    for path in ("../outside", "/etc/passwd", "src/", "", ":(glob)src/**", "src/\x00"):
        if path.startswith(":"):  # literal pathspecs: magic is a file name, and names nothing
            assert (await manager.log(mirror, [head], paths=[path], max_commits=5)).commits == ()
            continue
        with pytest.raises(InvalidPath):
            await manager.log(mirror, [head], paths=[path], max_commits=5)
    with pytest.raises(ValueError, match="at least one commit"):
        await manager.log(mirror, [head], max_commits=0)


async def test_log_is_killed_at_its_timeout(
    tmp_path: Path, options: GitOptions, mirror: Mirror, repo: SourceRepo
) -> None:
    slow = MirrorManager(
        GitOptions(
            mirror_dir=options.mirror_dir,
            local_roots=options.local_roots,
            git=str(slow_git(tmp_path)),
        )
    )

    with pytest.raises(GitTimeout, match=r"git log ran longer than 0\.3 s"):
        await slow.log(mirror, [repo.head("main")], max_commits=5, timeout=0.3)


def slow_git(directory: Path) -> Path:
    """A git whose ``log`` never answers; every other command is git's."""
    script = directory / "slow-git"
    script.write_text(
        '#!/bin/sh\nfor arg in "$@"; do\n  if [ "$arg" = log ]; then exec sleep 60; fi\ndone\n'
        'exec git "$@"\n'
    )
    script.chmod(0o755)
    return script


# --- The output of git log ------------------------------------------------------------------------


def record(*fields: bytes) -> bytes:
    return b"".join(field + b"\0" for field in fields)


HEADER = (b"\x1e", b"a" * 40, b"aaaaaaa", b"", b"Ann Example", b"ann@example.test", b"10", b"20")


def test_parse_log_reads_text_that_is_not_utf8_and_times_no_calendar_holds() -> None:
    output = record(
        b"\x1e",
        b"a" * 40,
        b"aaaaaaa",
        b"b" * 40 + b" " + b"c" * 40,
        b"Ann \xff",
        b"ann@example.test",
        b"99999999999999999999",
        b"20",
        b"caf\xe9",
        b"line one\n\nline three\n\n",
        b"",
        b"AM",
        b"docs/caf\xe9.md",
        b"DD",
        b"old.txt",
    ) + record(*HEADER, b"second", b"", b"\nT", b"link")

    merge, plain = _parse_log(output)

    assert merge.parents == ("b" * 40, "c" * 40)
    assert (merge.author_name, merge.subject) == ("Ann �", "caf�")
    assert merge.body == "line one\n\nline three"
    assert merge.authored_at == datetime.fromtimestamp(0, UTC)
    assert merge.committed_at == datetime.fromtimestamp(20, UTC)
    assert merge.changes == (
        CommitChange("modified", "docs/caf�.md"),
        CommitChange("deleted", "old.txt"),
    )
    assert (plain.subject, plain.body, plain.parents) == ("second", "", ())
    assert plain.changes == (CommitChange("type_changed", "link"),)
    assert _parse_log(b"") == []


@pytest.mark.parametrize(
    "output",
    [
        b"fatal: not a log\0",
        record(*HEADER, b"subject"),  # a commit cut short
        record(*HEADER, b"subject", b"", b"\nM"),  # a status without its path
    ],
)
def test_parse_log_refuses_output_it_does_not_know(output: bytes) -> None:
    with pytest.raises(GitCommandError, match="unexpected output of git log"):
        _parse_log(output)
