"""Tests for memini.git.mirror with fixture repositories made here. Content and identities are
synthetic; author and committer dates are fixed, so commit ids are the same on every run."""

import asyncio
import fcntl
import hashlib
import io
import os
import random
import shutil
import stat
import subprocess
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from git_helpers import SourceRepo
from memini.git import (
    INCOMING_NAMESPACE,
    BlobTooLarge,
    FileChange,
    GitCommandError,
    GitOptions,
    GitTimeout,
    InvalidPath,
    InvalidRevision,
    MirrorBusy,
    MirrorManager,
    MirrorNotFound,
    MirrorTooLarge,
    NotABlob,
    PathNotFound,
    RefUpdate,
    RemoteNotAllowed,
    RevisionNotFound,
    TreeEntry,
)
from memini.git.command import PASSWORD_VAR
from memini.git.mirror import ChangeStatus

pytestmark = [
    pytest.mark.git,
    pytest.mark.skipif(shutil.which("git") is None, reason="needs the git executable"),
]

TOKEN = "fake-git-token-for-tests-only-3c9b"  # noqa: S105 - fake, for leak checks


def snapshot(root: Path) -> dict[str, tuple[int, int, int, str]]:
    """Every file and directory under ``root``: mode, size, mtime in ns and a content digest."""
    entries: dict[str, tuple[int, int, int, str]] = {}
    for directory, names, files in os.walk(root):
        for name in [".", *names, *files]:
            path = Path(directory) / name
            info = path.lstat()
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
            )
            entries[str(path.relative_to(root))] = (
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                digest,
            )
    return entries


def incompressible(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)  # noqa: S311 - test data, not a secret


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    return tmp_path / "sources"


@pytest.fixture
def options(tmp_path: Path, sources: Path) -> GitOptions:
    return GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        fetch_timeout=30,
        command_timeout=30,
        size_check_interval=0.05,
    )


@pytest.fixture
def manager(options: GitOptions) -> MirrorManager:
    return MirrorManager(options)


@pytest.fixture
def source(sources: Path) -> SourceRepo:
    repo = SourceRepo(sources / "repo")
    repo.commit({"README.md": "# Fixture\n", "src/app.py": "print('one')\n"}, "first")
    repo.git("tag", "-a", "v1", "-m", "release one")
    repo.git("tag", "light")
    return repo


@pytest.fixture
def source_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-7000-8000-000000000001")


@pytest.fixture
async def hanging_server() -> AsyncIterator[int]:
    """A TCP port that accepts connections and never answers."""
    release = asyncio.Event()
    writers: list[asyncio.StreamWriter] = []

    async def hold(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        await release.wait()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        release.set()
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


def mirrored_refs(manager_refs: Mapping[str, str], source: SourceRepo) -> None:
    assert dict(manager_refs) == source.refs()


# --- Creating and fetching -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_sync_creates_a_bare_mirror_of_a_local_repository(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID, options: GitOptions
) -> None:
    result = await manager.sync(source_id, str(source.path))

    mirror = result.mirror
    assert result.created
    assert mirror.path == options.mirror_dir / str(source_id)
    assert result.updates == tuple(
        RefUpdate(ref, None, oid) for ref, oid in sorted(source.refs().items())
    )
    mirrored_refs(await manager.refs(mirror), source)
    assert await manager.head(mirror) == "refs/heads/main"
    assert await manager.resolve(mirror, "HEAD") == source.head()
    assert result.size > 0
    # Bare, with no hooks directory, no remote in its config and no trace of the fetch namespace
    # outside refs/memini/.
    assert sorted(p.name for p in mirror.path.iterdir()) == ["HEAD", "config", "objects", "refs"]
    config = (mirror.path / "config").read_text()
    assert "bare = true" in config
    assert "[remote" not in config
    assert str(source.path) not in config
    assert sorted(p.name for p in options.mirror_dir.iterdir()) == [
        str(source_id),
        f"{source_id}.lock",
    ]
    assert stat.S_IMODE(options.mirror_dir.stat().st_mode) == 0o700


@pytest.mark.anyio
async def test_fetch_brings_new_commits_branches_and_tags(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    first = source.head()
    second = source.commit({"src/app.py": "print('two')\n"}, "second")
    source.git("branch", "feature", first)
    source.git("tag", "-a", "v2", "-m", "release two")
    refs = source.refs()

    result = await manager.fetch(mirror, str(source.path))

    assert not result.created
    assert result.updates == (
        RefUpdate("refs/heads/feature", None, first),
        RefUpdate("refs/heads/main", first, second),
        RefUpdate("refs/tags/v2", None, refs["refs/tags/v2"]),
    )
    mirrored_refs(await manager.refs(mirror), source)
    assert await manager.resolve(mirror, "main") == second
    assert await manager.resolve(mirror, "refs/heads/feature") == first
    assert await manager.resolve(mirror, "v2") == second
    assert (await manager.fetch(mirror, str(source.path))).updates == ()


@pytest.mark.anyio
async def test_fetch_prunes_deleted_branches_and_tags(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    source.git("branch", "short-lived")
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    before = await manager.refs(mirror)
    source.git("branch", "--delete", "short-lived")
    source.git("tag", "--delete", "light")

    result = await manager.fetch(mirror, str(source.path))

    assert result.updates == (
        RefUpdate("refs/heads/short-lived", before["refs/heads/short-lived"], None),
        RefUpdate("refs/tags/light", before["refs/tags/light"], None),
    )
    mirrored_refs(await manager.refs(mirror), source)
    with pytest.raises(RevisionNotFound):
        await manager.resolve(mirror, "short-lived")


@pytest.mark.anyio
async def test_a_force_push_moves_the_ref_and_the_old_commit_stays_readable(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    old = source.commit({"notes.txt": "draft\n"}, "draft")
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    source.git("reset", "--quiet", "--hard", "HEAD~1")
    new = source.commit({"notes.txt": "final\n"}, "final")

    result = await manager.fetch(mirror, str(source.path))

    assert result.updates == (RefUpdate("refs/heads/main", old, new),)
    assert await manager.read_blob(mirror, old, "notes.txt") == b"draft\n"
    assert await manager.read_blob(mirror, new, "notes.txt") == b"final\n"


@pytest.mark.anyio
async def test_ensure_mirror_returns_an_existing_mirror_without_fetching(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    first = await manager.ensure_mirror(source_id, str(source.path))
    before = source.head()
    source.commit({"later.txt": "later\n"})

    again = await manager.ensure_mirror(source_id, str(source.path))

    assert again == first
    assert await manager.resolve(again, "main") == before


@pytest.mark.anyio
async def test_an_empty_repository_is_mirrored_and_filled_later(
    manager: MirrorManager, sources: Path, source_id: uuid.UUID
) -> None:
    empty = SourceRepo(sources / "empty")

    created = await manager.sync(source_id, str(empty.path))
    commit = empty.commit({"a.txt": "a\n"})
    fetched = await manager.sync(source_id, str(empty.path))

    assert created.created
    assert created.updates == ()
    assert fetched.updates == (RefUpdate("refs/heads/main", None, commit),)


@pytest.mark.anyio
async def test_a_bare_source_works_like_a_working_copy(
    manager: MirrorManager, sources: Path, source_id: uuid.UUID
) -> None:
    bare = SourceRepo(sources / "bare.git", bare=True)
    work = SourceRepo(sources / "work")
    commit = work.commit({"a.txt": "a\n"})
    work.git("push", "--quiet", str(bare.path), "main")

    result = await manager.sync(source_id, f"file://{bare.path}")

    assert result.updates == (RefUpdate("refs/heads/main", None, commit),)


# --- The local source is never written ----------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("bare", [False, True], ids=["working-copy", "bare"])
async def test_a_local_source_has_no_new_files_locks_or_ref_changes_after_syncs(
    manager: MirrorManager, sources: Path, source_id: uuid.UUID, bare: bool
) -> None:
    work = SourceRepo(sources / "work")
    work.commit({"a.txt": "a\n", "dir/b.txt": "b\n"}, "first")
    work.git("tag", "-a", "v1", "-m", "one")
    if bare:
        source = SourceRepo(sources / "source.git", bare=True)
        work.git("push", "--quiet", "--tags", str(source.path), "main")
    else:
        source = work
    before = snapshot(source.path)
    refs_before = source.refs()

    await manager.sync(source_id, str(source.path))  # created
    await manager.sync(source_id, str(source.path))  # fetched, nothing new

    assert snapshot(source.path) == before
    assert source.refs() == refs_before

    work.commit({"a.txt": "a2\n"}, "second")
    if bare:
        work.git("push", "--quiet", str(source.path), "main")
    before = snapshot(source.path)
    refs_before = source.refs()

    result = await manager.sync(source_id, str(source.path))  # fetched, one new commit

    assert len(result.updates) == 1
    assert snapshot(source.path) == before
    assert source.refs() == refs_before
    assert not [name for name in before if name.endswith(".lock")]


# --- Failures keep the previous state ------------------------------------------------------------


@pytest.mark.anyio
async def test_a_failed_fetch_from_a_vanished_source_keeps_the_previous_state(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    refs = await manager.refs(mirror)
    commit = source.head()
    location = str(source.path)
    source.path.rename(source.path.with_name("moved-away"))

    with pytest.raises(GitCommandError) as caught:
        await manager.fetch(mirror, location)

    assert caught.value.command == "fetch"
    assert await manager.refs(mirror) == refs
    assert await manager.read_blob(mirror, commit, "src/app.py") == b"print('one')\n"


@pytest.mark.anyio
async def test_a_fetch_that_fails_midway_keeps_the_previous_state(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    refs = await manager.refs(mirror)
    incoming = _incoming(mirror.path)
    commit = source.head()
    source.commit({"data.bin": incompressible(50_000, seed=1)}, "data")
    # Corrupt the new file's object in the source: upload-pack starts to send the pack, then dies
    # when it reads that object.
    blob = source.git("rev-parse", "HEAD:data.bin")
    loose = source.path / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)
    loose.write_bytes(b"not a zlib stream")

    with pytest.raises(GitCommandError):
        await manager.fetch(mirror, str(source.path))

    assert await manager.refs(mirror) == refs
    assert _incoming(mirror.path) == incoming
    assert await manager.resolve(mirror, "main") == commit
    assert await manager.read_blob(mirror, commit, "README.md") == b"# Fixture\n"
    assert not _leftovers(mirror.path)


@pytest.mark.anyio
async def test_a_fetch_from_an_unreachable_remote_keeps_the_previous_state(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    refs = await manager.refs(mirror)

    with pytest.raises(GitCommandError):
        await manager.fetch(mirror, "https://127.0.0.1:1/unreachable.git")

    assert await manager.refs(mirror) == refs


@pytest.mark.anyio
async def test_a_fetch_that_times_out_is_killed_and_keeps_the_previous_state(
    tmp_path: Path, sources: Path, source: SourceRepo, source_id: uuid.UUID, hanging_server: int
) -> None:
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors", local_roots=(sources,), allow_http=True, fetch_timeout=1
    )
    manager = MirrorManager(options)
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    refs = await manager.refs(mirror)

    with pytest.raises(GitTimeout):
        await manager.fetch(mirror, f"http://127.0.0.1:{hanging_server}/repo.git")

    assert await manager.refs(mirror) == refs
    assert await manager.resolve(mirror, "main") == source.head()


@pytest.mark.anyio
async def test_a_failed_creation_leaves_nothing_behind(
    manager: MirrorManager, sources: Path, source_id: uuid.UUID, options: GitOptions
) -> None:
    with pytest.raises(GitCommandError):
        await manager.sync(source_id, str(sources / "does-not-exist"))

    assert manager.find(source_id) is None
    assert sorted(p.name for p in options.mirror_dir.iterdir()) == [f"{source_id}.lock"]


# --- Size limit ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_repository_over_the_size_limit_is_not_mirrored(
    tmp_path: Path, sources: Path, source: SourceRepo, source_id: uuid.UUID
) -> None:
    source.commit({"data.bin": incompressible(200_000, seed=2)}, "big")
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        max_mirror_size=50_000,
        size_check_interval=0.05,
    )
    manager = MirrorManager(options)

    with pytest.raises(MirrorTooLarge) as caught:
        await manager.sync(source_id, str(source.path))

    assert caught.value.limit == 50_000
    assert manager.find(source_id) is None
    assert sorted(p.name for p in options.mirror_dir.iterdir()) == [f"{source_id}.lock"]


@pytest.mark.anyio
async def test_a_fetch_over_the_size_limit_keeps_the_previous_state(
    tmp_path: Path, sources: Path, source: SourceRepo, source_id: uuid.UUID
) -> None:
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        max_mirror_size=60_000,
        size_check_interval=0.05,
    )
    manager = MirrorManager(options)
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    refs = await manager.refs(mirror)
    source.commit({"data.bin": incompressible(200_000, seed=3)}, "big")

    with pytest.raises(MirrorTooLarge):
        await manager.fetch(mirror, str(source.path))

    assert await manager.refs(mirror) == refs
    assert _incoming(mirror.path) == {}  # dropped, so gc can reclaim the objects
    assert not _leftovers(mirror.path)


# --- What may run, and who may write --------------------------------------------------------------


@pytest.mark.anyio
async def test_hooks_in_a_mirror_never_run(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID, tmp_path: Path
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    marker = tmp_path / "hook-ran"
    hooks = mirror.path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "post-update", "pre-auto-gc"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
        hook.chmod(0o755)
    source.commit({"new.txt": "new\n"})

    await manager.fetch(mirror, str(source.path))

    assert not marker.exists()
    # The hook itself works: plain git, without the connector's settings, runs it.
    plain_git(f"--git-dir={mirror.path}", "update-ref", "refs/test/x", source.head())
    assert "reference-transaction" in marker.read_text()


@pytest.mark.anyio
async def test_a_second_writer_is_refused(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID, options: GitOptions
) -> None:
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    lock = os.open(options.mirror_dir / f"{source_id}.lock", os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(MirrorBusy):
            await manager.fetch(mirror, str(source.path))
        with pytest.raises(MirrorBusy):
            await manager.sync(source_id, str(source.path))
    finally:
        os.close(lock)

    await manager.fetch(mirror, str(source.path))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "location",
    [
        "ext::sh -c touch% /tmp/memini-test-pwned",
        "git://git.example.test/repo.git",
        "http://git.example.test/repo.git",
        "https://user:fake-password@git.example.test/repo.git",
        "/etc",
        "relative/repo",
    ],
)
async def test_a_refused_location_starts_no_process(
    manager: MirrorManager,
    source_id: uuid.UUID,
    options: GitOptions,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    async def no_process(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"a process was started: {args[:3]}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_process)

    with pytest.raises(RemoteNotAllowed):
        await manager.sync(source_id, location)
    with pytest.raises(RemoteNotAllowed):
        await manager.ensure_mirror(source_id, location)
    assert not options.mirror_dir.exists()


@pytest.mark.anyio
async def test_the_token_never_reaches_arguments_errors_logs_or_disk(
    tmp_path: Path,
    sources: Path,
    source: SourceRepo,
    source_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    log_output: io.StringIO,
) -> None:
    options = GitOptions(
        mirror_dir=tmp_path / "mirrors",
        local_roots=(sources,),
        https_token=SecretStr(TOKEN),
        https_token_hosts=frozenset({"127.0.0.1"}),
    )
    manager = MirrorManager(options)
    started: list[tuple[tuple[str, ...], dict[str, str]]] = []
    start = asyncio.create_subprocess_exec

    async def recording(*args: Any, **kwargs: Any) -> Any:
        started.append((tuple(str(arg) for arg in args), dict(kwargs["env"])))
        return await start(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    mirror = await manager.ensure_mirror(source_id, str(source.path))

    with pytest.raises(GitCommandError) as caught:
        await manager.fetch(mirror, "https://127.0.0.1:1/private.git")

    fetches = [env for argv, env in started if "fetch" in argv]
    assert [env.get(PASSWORD_VAR) for env in fetches] == [None, TOKEN]  # local, then https
    for argv, env in started:
        assert not any(TOKEN in arg for arg in argv)
        assert [name for name, value in env.items() if TOKEN in value] in ([], [PASSWORD_VAR])
    assert TOKEN not in str(caught.value)
    assert TOKEN not in caught.value.stderr
    assert TOKEN not in log_output.getvalue()
    for path in (tmp_path / "mirrors").rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


# --- Reads ---------------------------------------------------------------------------------------


@pytest.fixture
async def tree(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> tuple[MirrorManager, Any, str]:
    (source.path / "tool.sh").write_text("#!/bin/sh\necho tool\n")
    (source.path / "tool.sh").chmod(0o755)
    source.git("add", "tool.sh")
    (source.path / "link").symlink_to("README.md")
    source.git("add", "link")
    commit = source.commit({"src/deep/mod.py": "x = 1\n", "big.txt": "y" * 5000})
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    return manager, mirror, commit


@pytest.mark.anyio
async def test_ls_tree_lists_every_file_with_mode_and_size(
    tree: tuple[MirrorManager, Any, str], source: SourceRepo
) -> None:
    manager, mirror, commit = tree

    entries = {entry.path: entry for entry in await manager.ls_tree(mirror, commit)}

    assert sorted(entries) == [
        "README.md",
        "big.txt",
        "link",
        "src/app.py",
        "src/deep/mod.py",
        "tool.sh",
    ]
    assert entries["README.md"] == TreeEntry(
        "README.md", "100644", "blob", source.git("rev-parse", f"{commit}:README.md"), 10
    )
    assert entries["tool.sh"].mode == "100755"
    assert entries["tool.sh"].is_file
    assert entries["link"].mode == "120000"
    assert not entries["link"].is_file
    assert entries["big.txt"].size == 5000


@pytest.mark.anyio
async def test_read_blob_reads_a_file_at_a_commit(tree: tuple[MirrorManager, Any, str]) -> None:
    manager, mirror, commit = tree

    assert await manager.read_blob(mirror, commit, "src/deep/mod.py") == b"x = 1\n"
    assert await manager.read_blob(mirror, commit, "link") == b"README.md"
    entry = await manager.entry(mirror, commit, "src")
    assert entry.kind == "tree"
    with pytest.raises(NotABlob):
        await manager.read_blob(mirror, commit, "src")
    with pytest.raises(PathNotFound):
        await manager.read_blob(mirror, commit, "src/missing.py")
    with pytest.raises(PathNotFound):
        await manager.read_blob(mirror, commit, "README.md/inside")


@pytest.mark.anyio
async def test_list_directory_lists_one_level_and_follows_no_link(
    tree: tuple[MirrorManager, Any, str],
) -> None:
    manager, mirror, commit = tree

    root = await manager.list_directory(mirror, commit)
    assert [(e.path, e.kind, e.mode) for e in root] == [
        ("README.md", "blob", "100644"),
        ("big.txt", "blob", "100644"),
        ("link", "blob", "120000"),
        ("src", "tree", "040000"),
        ("tool.sh", "blob", "100755"),
    ]
    assert root[1].size == 5000
    assert root[3].size is None
    src = await manager.list_directory(mirror, commit, "src")
    assert [(e.path, e.kind) for e in src] == [("src/app.py", "blob"), ("src/deep", "tree")]
    assert [e.path for e in await manager.list_directory(mirror, commit, "src/deep")] == [
        "src/deep/mod.py"
    ]
    for not_a_directory in ("README.md", "link", "missing", "src/app.py"):
        with pytest.raises(PathNotFound):
            await manager.list_directory(mirror, commit, not_a_directory)
    for bad in ("../src", "/src", "src/", "src/../src", "a\x00b"):
        with pytest.raises(InvalidPath):
            await manager.list_directory(mirror, commit, bad)
    with pytest.raises(RevisionNotFound):
        await manager.list_directory(mirror, "0" * 40)


@pytest.mark.anyio
async def test_read_blob_refuses_a_file_over_the_limit(
    tree: tuple[MirrorManager, Any, str], tmp_path: Path, sources: Path
) -> None:
    manager, mirror, commit = tree
    small = MirrorManager(
        GitOptions(mirror_dir=tmp_path / "mirrors", local_roots=(sources,), max_blob_size=1000)
    )

    with pytest.raises(BlobTooLarge) as caught:
        await small.read_blob(mirror, commit, "big.txt")
    assert (caught.value.size, caught.value.limit) == (5000, 1000)
    with pytest.raises(BlobTooLarge):
        await manager.read_blob(mirror, commit, "big.txt", max_size=4999)
    assert len(await manager.read_blob(mirror, commit, "big.txt", max_size=5000)) == 5000


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["", "/README.md", "../README.md", "src/../README.md", "./README.md", "src//app.py", "src/",
     "src/.", "a\nb", "a\x00b", "..", "."],
)  # fmt: skip
async def test_read_blob_refuses_paths_that_are_not_plain(
    tree: tuple[MirrorManager, Any, str], path: str
) -> None:
    manager, mirror, commit = tree

    with pytest.raises(InvalidPath):
        await manager.read_blob(mirror, commit, path)


@pytest.mark.anyio
async def test_read_blobs_reads_many_files_by_id(
    tree: tuple[MirrorManager, Any, str],
) -> None:
    manager, mirror, commit = tree
    entries = [entry for entry in await manager.ls_tree(mirror, commit) if entry.is_file]
    missing = "0" * 40

    blobs = await manager.read_blobs(mirror, [*(entry.oid for entry in entries), missing])

    assert sorted(blobs) == sorted(entry.oid for entry in entries)
    for entry in entries:
        assert blobs[entry.oid] == await manager.read_blob(mirror, commit, entry.path)
    assert await manager.read_blobs(mirror, []) == {}
    with pytest.raises(InvalidRevision):
        await manager.read_blobs(mirror, ["HEAD"])


@pytest.mark.anyio
async def test_blob_sizes_sizes_blobs_by_id_and_leaves_out_the_rest(
    tree: tuple[MirrorManager, Any, str],
) -> None:
    manager, mirror, commit = tree
    entries = {entry.path: entry for entry in await manager.ls_tree(mirror, commit)}
    big = entries["big.txt"].oid

    sizes = await manager.blob_sizes(mirror, [big, entries["link"].oid, commit, "0" * 40])

    assert sizes == {big: 5000, entries["link"].oid: len("README.md")}  # no commit, no missing id
    assert await manager.blob_sizes(mirror, []) == {}
    with pytest.raises(InvalidRevision):
        await manager.blob_sizes(mirror, ["main"])


@pytest.mark.anyio
async def test_committed_at_is_the_committer_time_in_utc(
    tree: tuple[MirrorManager, Any, str], source: SourceRepo
) -> None:
    manager, mirror, commit = tree

    # The fixture's commits are dated a day apart from 2026-01-02, 12:00 UTC.
    assert await manager.committed_at(mirror, commit) == datetime(2026, 1, 3, 12, tzinfo=UTC)
    assert await manager.committed_at(mirror, source.head("v1")) == datetime(
        2026, 1, 2, 12, tzinfo=UTC
    )
    with pytest.raises(RevisionNotFound):
        await manager.committed_at(mirror, "f" * 40)
    with pytest.raises(InvalidRevision):
        await manager.committed_at(mirror, "main")


@pytest.mark.anyio
async def test_resolve_takes_names_and_ids_and_refuses_expressions(
    tree: tuple[MirrorManager, Any, str], source: SourceRepo
) -> None:
    manager, mirror, commit = tree
    first = source.head("v1")

    assert await manager.resolve(mirror, "HEAD") == commit
    assert await manager.resolve(mirror, "main") == commit
    assert await manager.resolve(mirror, "refs/heads/main") == commit
    assert await manager.resolve(mirror, "v1") == first  # an annotated tag, peeled
    assert await manager.resolve(mirror, "refs/tags/light") == first
    assert await manager.resolve(mirror, first) == first
    for expression in (
        "main~1",
        "main^",
        "HEAD@{1}",
        "@{-1}",
        ":/first",
        "main^{tree}",
        "main..v1",
        "-main",
        "refs/remotes/origin/main",
        f"{INCOMING_NAMESPACE}heads/main",
        "main:README.md",
        "a b",
    ):
        with pytest.raises(InvalidRevision):
            await manager.resolve(mirror, expression)
    with pytest.raises(RevisionNotFound):
        await manager.resolve(mirror, "no-such-branch")
    with pytest.raises(RevisionNotFound):
        await manager.resolve(mirror, "f" * 40)
    blob = source.git("rev-parse", f"{commit}:README.md")
    with pytest.raises(RevisionNotFound):
        await manager.resolve(mirror, blob)


@pytest.mark.anyio
async def test_diff_names_lists_added_modified_deleted_and_type_changed_files(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    old = source.commit(
        {"keep.txt": "same\n", "edit.txt": "v1\n", "gone.txt": "bye\n", "kind": "file\n"}
    )
    source.git("rm", "--quiet", "kind")
    (source.path / "kind").symlink_to("keep.txt")
    source.git("add", "kind")
    new = source.commit({"edit.txt": "v2\n", "gone.txt": None, "new/added.txt": "hi\n"})
    mirror = await manager.ensure_mirror(source_id, str(source.path))

    changes = await manager.diff_names(mirror, old, new)

    def change(
        status: ChangeStatus, path: str, old_mode: str | None, new_mode: str | None
    ) -> FileChange:
        old_oid = None if old_mode is None else source.git("rev-parse", f"{old}:{path}")
        new_oid = None if new_mode is None else source.git("rev-parse", f"{new}:{path}")
        return FileChange(status, path, old_mode, new_mode, old_oid, new_oid)

    assert changes == [
        change("modified", "edit.txt", "100644", "100644"),
        change("deleted", "gone.txt", "100644", None),
        change("type_changed", "kind", "100644", "120000"),
        change("added", "new/added.txt", None, "100644"),
    ]
    assert await manager.diff_names(mirror, new, new) == []
    with pytest.raises(RevisionNotFound):
        await manager.diff_names(mirror, "e" * 40, new)
    with pytest.raises(InvalidRevision):
        await manager.diff_names(mirror, "HEAD~1", new)


@pytest.mark.anyio
async def test_change_times_walks_the_first_parent_history_once(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    def day(number: int) -> datetime:
        return datetime(2026, 1, number, 12, tzinfo=UTC)

    # The fixture's first commit (day 2) added README.md and src/app.py.
    second = source.commit({"a.txt": "a\n"})  # day 3
    source.git("checkout", "--quiet", "-b", "feature")
    source.commit({"feat.txt": "f\n", "a.txt": "a2\n"})  # day 4, on the feature branch
    source.git("checkout", "--quiet", "main")
    source.commit({"b.txt": "b\n", "\nlead.txt": "an odd name\n"})  # day 5
    source.git("merge", "--quiet", "--no-ff", "--no-commit", "feature")
    source.commit({}, "merge feature")  # day 6
    head = source.commit({"README.md": "# Fixture, edited\n"})  # day 7
    mirror = await manager.ensure_mirror(source_id, str(source.path))

    walk = await manager.change_times(mirror, head, max_commits=100)

    # Changes merged from the feature branch have the time they reached main: the merge's.
    assert walk.times == {
        "README.md": day(7),
        "a.txt": day(6),
        "feat.txt": day(6),
        "b.txt": day(5),
        "\nlead.txt": day(5),
        "src/app.py": day(2),  # from the root commit
    }
    assert (walk.commits, walk.complete, walk.oldest) == (5, True, day(2))

    after = await manager.change_times(mirror, head, since=second, max_commits=100)

    assert set(after.times) == {"README.md", "a.txt", "feat.txt", "b.txt", "\nlead.txt"}
    assert (after.commits, after.complete, after.oldest) == (3, True, day(5))

    cut = await manager.change_times(mirror, head, max_commits=2)

    assert cut.times == {"README.md": day(7), "a.txt": day(6), "feat.txt": day(6)}
    assert (cut.commits, cut.complete, cut.oldest) == (2, False, day(6))

    with pytest.raises(InvalidRevision):
        await manager.change_times(mirror, "main", max_commits=1)
    with pytest.raises(ValueError, match="at least one commit"):
        await manager.change_times(mirror, head, max_commits=0)
    with pytest.raises(GitCommandError):
        await manager.change_times(mirror, "f" * 40, max_commits=1)


@pytest.mark.anyio
async def test_open_and_remove(
    manager: MirrorManager, source: SourceRepo, source_id: uuid.UUID
) -> None:
    with pytest.raises(MirrorNotFound):
        manager.open(source_id)
    mirror = await manager.ensure_mirror(source_id, str(source.path))
    assert manager.open(source_id) == mirror

    assert await manager.remove(source_id)

    assert manager.find(source_id) is None
    assert not mirror.path.exists()
    assert not await manager.remove(source_id)
    with pytest.raises(MirrorNotFound):
        await manager.fetch(mirror, str(source.path))


# --- Helpers -------------------------------------------------------------------------------------


def plain_git(*args: str) -> str:
    """git as a user runs it, without the connector's settings; for checks and controls."""
    done = subprocess.run(  # noqa: S603 - fixed git arguments in a test
        ["git", *args],  # noqa: S607
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": "/dev/null"},
        capture_output=True,
        check=True,
    )
    return done.stdout.decode()


def _incoming(git_dir: Path) -> dict[str, str]:
    listing = plain_git(
        f"--git-dir={git_dir}",
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        INCOMING_NAMESPACE,
    )
    return dict(line.split(" ") for line in listing.splitlines() if line)


def _leftovers(git_dir: Path) -> list[str]:
    packs = git_dir / "objects" / "pack"
    return [p.name for p in packs.iterdir() if p.name.startswith("tmp_") or p.suffix == ".keep"]
