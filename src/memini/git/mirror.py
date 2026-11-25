"""Bare mirrors of git sources: the raw store of code (requirements, section 3).

Layout under the mirror directory (``git_mirror_dir``)::

    <source id>/          a bare repository: refs/heads/*, refs/tags/*, HEAD
    <source id>.lock      held (flock) by the one writer of that mirror
    <source id>.new/      a mirror being created; renamed into place when complete

A directory is named after the source id, never after the URL, so no location can steer a path.
The directory is created with mode 0700: mirrors hold private code.

How a mirror is created and kept current:

- Created by :meth:`MirrorManager.ensure_mirror` (or :meth:`~MirrorManager.sync`): ``git init
  --bare`` with no template (no sample hooks), a fetch as below, ``HEAD`` set to the remote's
  default branch, then an atomic rename into place. A failed creation leaves nothing behind.
- Kept current by :meth:`MirrorManager.fetch`. The fetch goes into a namespace readers never look
  at, ``refs/memini/incoming/{heads,tags}/*`` (with ``--prune --atomic``), then the size of the
  mirror is checked, then ``refs/heads/*`` and ``refs/tags/*`` are set to match the incoming refs
  in one ``git update-ref --stdin`` transaction, each update conditional on the old value.
  Readers see the old refs until that transaction commits, and the new ones after. A fetch that
  fails, times out, is cancelled or grows the mirror past ``git_max_mirror_size`` changes no ref
  readers see; the objects of the previous state are never removed by a fetch. Branches and tags
  only: pull request refs (``refs/pull/*``) and other namespaces would multiply the size.
- The source is never written. A local source is fetched through ``upload-pack``, like a remote
  one: no hard links, no alternates, no lock files or ref changes in the source repository.
  Nothing is cloned with ``--local``; there is no clone at all, only fetches.
- After a successful fetch ``git gc --auto`` runs in the foreground (never detached), so the
  number of packs stays bounded. It prunes unreachable objects only after git's default two weeks.

Concurrency: one writer per mirror. The job queue already runs one job per source at a time (the
advisory lock in :mod:`memini.jobs`); in addition every write takes an exclusive ``flock`` on the
mirror's lock file and raises :class:`MirrorBusy` if another writer holds it. Readers take no lock:
git updates refs atomically and adds objects before refs point to them.

Reads take full object ids (a pinned commit), except :meth:`MirrorManager.resolve`, which turns a
ref name into one. Paths are checked (:func:`check_path`) before git sees them.

History is read on demand and never stored: :meth:`MirrorManager.log` walks the commits reachable
from the commits it is given, one ``git log`` process with a count and a time limit.

Not handled yet: repositories with sha256 object ids (the mirror is created with sha1), the
remote's default branch changing after the mirror was created, submodule contents.
"""

import asyncio
import contextlib
import logging
import math
import os
import re
import shutil
import uuid
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from memini.git.command import Git, SizeLimit, directory_size
from memini.git.errors import (
    BlobTooLarge,
    GitCommandError,
    InvalidPath,
    InvalidRevision,
    MirrorBusy,
    MirrorNotFound,
    MirrorTooLarge,
    NotABlob,
    PathNotFound,
    RevisionNotFound,
)
from memini.git.options import GitOptions
from memini.git.remote import Remote, parse_remote

__all__ = [
    "INCOMING_NAMESPACE",
    "MAX_LOG_OUTPUT",
    "ChangeTimes",
    "Commit",
    "CommitChange",
    "CommitLog",
    "FetchResult",
    "FileChange",
    "Mirror",
    "MirrorManager",
    "RefUpdate",
    "TreeEntry",
    "check_path",
    "check_tracked_ref",
]

log = logging.getLogger(__name__)

INCOMING_NAMESPACE: Final = "refs/memini/incoming/"
"""Where a fetch writes; never read by readers."""

_MIRRORED: Final = ("refs/heads/", "refs/tags/")
_REFSPECS: Final = tuple(f"+{prefix}*:{INCOMING_NAMESPACE}{prefix[5:]}*" for prefix in _MIRRORED)

_OID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF_FORBIDDEN: Final = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
_SMALL_OUTPUT: Final = 1024**2

MAX_LOG_OUTPUT: Final = 64 * 1024**2
"""Bytes one history walk (:meth:`MirrorManager.log`) may print unless the caller says otherwise."""

# What `git log -z` prints for a commit with the format below: the mark, the nine fields and the
# format's own terminator, each ended by a NUL; then its changed paths, each as a status and a
# path, both ended by a NUL. The first status follows a line break; a merge has an empty field
# there instead, also when it lists no path. No field of a commit holds a NUL, and a status is
# letters only, so the mark, a control character, can only be the start of the next commit.
_LOG_MARK: Final = b"\x1e"
_LOG_FIELDS: Final = 9
_LOG_FORMAT: Final = "%x1e%x00%H%x00%h%x00%P%x00%an%x00%ae%x00%at%x00%ct%x00%s%x00%b"


@dataclass(frozen=True, slots=True)
class Mirror:
    """The bare mirror of one source."""

    source_id: uuid.UUID
    path: Path


@dataclass(frozen=True, slots=True)
class RefUpdate:
    """A ref that a fetch created (``old`` is ``None``), moved, or deleted (``new`` is ``None``)."""

    ref: str
    old: str | None
    new: str | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """What :meth:`MirrorManager.fetch` or :meth:`~MirrorManager.sync` did."""

    mirror: Mirror
    created: bool
    """The mirror did not exist and was created by this call."""
    updates: tuple[RefUpdate, ...]
    """The branches and tags that changed, by ref name."""
    size: int
    """Bytes the mirror holds on disk after the fetch."""


type EntryKind = Literal["blob", "tree", "commit"]


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of a commit's tree.

    ``path`` is the repository-relative path. git paths are bytes; one that is not valid UTF-8 is
    decoded with ``surrogateescape``, so it reads back the same file but holds lone surrogates
    (it cannot be stored as Postgres text as it is).
    """

    path: str
    mode: str
    """``100644`` file, ``100755`` executable, ``120000`` symlink, ``160000`` submodule,
    ``040000`` directory."""
    kind: EntryKind
    oid: str
    size: int | None
    """Bytes of a blob; ``None`` for a directory or a submodule."""

    @property
    def is_file(self) -> bool:
        """A regular file: a blob that is not a symlink."""
        return self.kind == "blob" and self.mode in ("100644", "100755")


@dataclass(frozen=True, slots=True)
class ChangeTimes:
    """When files last changed along the first-parent history of a commit: one walk of it
    (:meth:`MirrorManager.change_times`)."""

    times: Mapping[str, datetime]
    """Path to the committer time of the newest commit walked that added, changed or deleted it,
    in UTC. A merge counts with its changes against its first parent: the time a change reached
    the branch."""
    commits: int
    """Commits walked."""
    complete: bool
    """The walk reached its end (the root commit, or the commit it started after) within its
    limit; a path not in ``times`` was not changed in the range."""
    oldest: datetime | None
    """The committer time of the last commit walked; ``None`` if none was. When the walk is not
    complete, a path not in ``times`` last changed at that commit or before."""


type ChangeStatus = Literal["added", "modified", "deleted", "type_changed"]

_STATUS: Final[Mapping[str, ChangeStatus]] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "type_changed",
}


@dataclass(frozen=True, slots=True)
class FileChange:
    """A path that differs between two commits. Renames are a deletion and an addition."""

    status: ChangeStatus
    path: str
    old_mode: str | None
    new_mode: str | None
    old_oid: str | None
    new_oid: str | None


@dataclass(frozen=True, slots=True)
class CommitChange:
    """A path a commit changed, against its parent. Renames are a deletion and an addition. For a
    merge, a path that differs from every parent, which is what the merge itself changed: a
    resolved conflict, not what the merged branch brought."""

    status: ChangeStatus
    path: str


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit of a history walk (:meth:`MirrorManager.log`).

    Names, messages and paths are what the repository holds, decoded as UTF-8 with U+FFFD for
    bytes that are not: text to show, not a key to read a file back with."""

    oid: str
    abbrev: str
    """The object id abbreviated as git abbreviates it: unique in the mirror, 7 digits or more."""
    parents: tuple[str, ...]
    """Object ids of the parents: none for a root commit, more than one for a merge."""
    author_name: str
    author_email: str
    """The author as the commit object names it; no mailmap is applied."""
    authored_at: datetime
    committed_at: datetime
    """The committer time, in UTC: what ``since`` and ``until`` compare and git orders by."""
    subject: str
    """The first paragraph of the message on one line."""
    body: str
    """The message after its subject, without the line breaks at its end; empty when it has
    none."""
    changes: tuple[CommitChange, ...]
    """The paths changed, by path; within the paths the walk was limited to, if it was."""


@dataclass(frozen=True, slots=True)
class CommitLog:
    """What one history walk read (:meth:`MirrorManager.log`)."""

    commits: tuple[Commit, ...]
    """As ``git log`` orders them: newest first by committer time, a commit before its parents
    where the two disagree."""
    complete: bool
    """The walk ended by itself, below its limit: no other commit matches."""
    starts: tuple[str, ...]
    """The commits the walk started from: those given that the mirror holds."""


def check_path(path: str) -> str:
    """``path`` if it is a plain repository-relative path, otherwise :class:`InvalidPath`.

    Refused: empty, absolute, a trailing slash, empty, ``.`` or ``..`` components, control
    characters. Such a path can name nothing in a tree, and refusing it early keeps traversal
    attempts out of git and out of logs of successful reads.
    """
    if (
        not path
        or len(path) > 4096
        or path.startswith("/")
        or path.endswith("/")
        or any(ord(char) < 0x20 or char == "\x7f" for char in path)
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise InvalidPath(
            "a path is relative to the repository root, without '.', '..' or empty components"
            " and without control characters"
        )
    return path


def check_tracked_ref(ref: str) -> str:
    """``ref`` if it is a full branch or tag name that a mirror keeps, otherwise
    :class:`InvalidRevision`.

    A tracked ref is under ``refs/heads/`` or ``refs/tags/`` (nothing else is mirrored) and follows
    git's rules for ref names; like ``git branch``, it refuses a name ``HEAD`` or one that starts
    with ``-``. For checking what a source tracks before anything is fetched.
    """
    name = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    if name == ref or not name or name == "HEAD" or name.startswith("-"):
        raise InvalidRevision(
            "a tracked ref is a branch or tag: refs/heads/<name> or refs/tags/<name>"
        )
    return _check_ref_name(ref)


def _check_oid(oid: str) -> str:
    if _OID.fullmatch(oid) is None:
        raise InvalidRevision("an object id is 40 or 64 lowercase hexadecimal digits")
    return oid


def _check_ref_name(name: str) -> str:
    parts = name.split("/")
    if (
        not name
        or name == "@"
        or name.startswith("-")
        or name.endswith((".", "/"))
        or _REF_FORBIDDEN.search(name) is not None
        or any(part.startswith(".") or part.endswith(".lock") for part in parts)
    ):
        raise InvalidRevision(
            "a ref is HEAD, a branch or tag name, a full ref under refs/heads/ or refs/tags/, or"
            " an object id; revision expressions are not accepted"
        )
    return name


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


class MirrorManager:
    """Creates, fetches and reads the bare mirrors of git sources under one directory."""

    def __init__(self, options: GitOptions, environ: Mapping[str, str] | None = None) -> None:
        self._options = options
        self._git = Git(options, environ)
        self._git_checked = False

    @property
    def options(self) -> GitOptions:
        return self._options

    @property
    def root(self) -> Path:
        return self._options.mirror_dir

    def path(self, source_id: uuid.UUID) -> Path:
        """Where the mirror of ``source_id`` lives, whether it exists or not."""
        return self.root / str(source_id)

    def find(self, source_id: uuid.UUID) -> Mirror | None:
        """The mirror of ``source_id``, or ``None`` if it has none yet. Never clones."""
        path = self.path(source_id)
        return Mirror(source_id, path) if (path / "HEAD").is_file() else None

    def open(self, source_id: uuid.UUID) -> Mirror:
        """The mirror of ``source_id``, for reading. Raises :class:`MirrorNotFound`."""
        mirror = self.find(source_id)
        if mirror is None:
            raise MirrorNotFound(f"source {source_id} has no mirror yet")
        return mirror

    def remote(self, location: str) -> Remote:
        """``location`` checked against the allowlist; :class:`RemoteNotAllowed` if refused."""
        return parse_remote(location, self._options)

    # --- Writes ----------------------------------------------------------------------------------

    async def ensure_mirror(self, source_id: uuid.UUID, location: str) -> Mirror:
        """The mirror of ``source_id``, created from ``location`` if it does not exist.

        An existing mirror is returned as it is, not fetched. ``location`` is checked either way.
        """
        remote = self.remote(location)
        await self._check_git()
        with self._writer(source_id):
            mirror = self.find(source_id)
            if mirror is None:
                mirror = (await self._create(source_id, remote)).mirror
            return mirror

    async def fetch(self, mirror: Mirror, location: str) -> FetchResult:
        """Fetch ``location`` into ``mirror``: branches and tags, deleted ones pruned.

        Raises:
            RemoteNotAllowed: ``location`` was refused; nothing ran.
            MirrorBusy: another writer holds the mirror.
            GitCommandError, GitTimeout, MirrorTooLarge: the fetch failed; refs are as before.
        """
        remote = self.remote(location)
        await self._check_git()
        with self._writer(mirror.source_id):
            if self.find(mirror.source_id) is None:
                raise MirrorNotFound(f"source {mirror.source_id} has no mirror yet")
            return await self._fetch(mirror, remote, created=False)

    async def sync(self, source_id: uuid.UUID, location: str) -> FetchResult:
        """Create the mirror if it does not exist, otherwise fetch: what a sync job calls."""
        remote = self.remote(location)
        await self._check_git()
        with self._writer(source_id):
            mirror = self.find(source_id)
            if mirror is None:
                return await self._create(source_id, remote)
            return await self._fetch(mirror, remote, created=False)

    async def remove(self, source_id: uuid.UUID) -> bool:
        """Delete the mirror of ``source_id``; ``False`` if there was none."""
        with self._writer(source_id):
            path = self.path(source_id)
            existed = path.exists()
            await asyncio.to_thread(_remove_tree, path)
            await asyncio.to_thread(_remove_tree, self._staging(source_id))
            return existed

    def _staging(self, source_id: uuid.UUID) -> Path:
        return self.root / f"{source_id}.new"

    @contextlib.contextmanager
    def _writer(self, source_id: uuid.UUID) -> Generator[None]:
        import fcntl

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / f"{source_id}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MirrorBusy(f"another writer holds the mirror of source {source_id}") from None
            yield
        finally:
            os.close(fd)  # releases the lock

    async def _check_git(self) -> None:
        if not self._git_checked:
            await self._git.version()
            self._git_checked = True

    async def _create(self, source_id: uuid.UUID, remote: Remote) -> FetchResult:
        staging = self._staging(source_id)
        await asyncio.to_thread(_remove_tree, staging)  # left by a process that died
        try:
            await self._git.run(
                ["init", "--quiet", "--bare", "--template=", str(staging)], git_dir=None
            )
            building = Mirror(source_id, staging)
            head = await self._remote_head(building, remote)
            result = await self._fetch(building, remote, created=True)
            if head is not None and any(update.ref == head for update in result.updates):
                await self._git.run(["symbolic-ref", "HEAD", head], git_dir=staging)
            path = self.path(source_id)
            await asyncio.to_thread(os.rename, staging, path)
        except BaseException:
            _remove_tree(staging)
            raise
        log.info(
            "mirror created",
            extra={"source_id": str(source_id), "refs": len(result.updates), "size": result.size},
        )
        return FetchResult(Mirror(source_id, path), True, result.updates, result.size)

    async def _remote_head(self, mirror: Mirror, remote: Remote) -> str | None:
        """The branch the remote's HEAD points to, or ``None`` (an empty repository)."""
        output = await self._git.run(
            ["ls-remote", "--symref", "--", remote.url, "HEAD"],
            git_dir=mirror.path,
            remote=remote,
            timeout=self._options.fetch_timeout,
            max_output=_SMALL_OUTPUT,
        )
        for line in output.stdout.splitlines():
            target, _, name = line.partition(b"\t")
            if name == b"HEAD" and target.startswith(b"ref: refs/heads/"):
                return _decode(target[len(b"ref: ") :])
        return None

    async def _fetch(self, mirror: Mirror, remote: Remote, *, created: bool) -> FetchResult:
        limit = self._options.max_mirror_size
        try:
            await self._git.run(
                [
                    "fetch",
                    "--quiet",
                    "--prune",
                    "--atomic",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--no-auto-maintenance",
                    "--",
                    remote.url,
                    *_REFSPECS,
                ],
                git_dir=mirror.path,
                remote=remote,
                timeout=self._options.fetch_timeout,
                max_output=_SMALL_OUTPUT,
                size_limit=SizeLimit(mirror.path, limit),
            )
            size = await asyncio.to_thread(directory_size, mirror.path)
            if size > limit:
                raise MirrorTooLarge(size, limit)
            updates = await self._promote(mirror)
        except BaseException as error:
            await self._clean_failed_fetch(mirror, drop_incoming=isinstance(error, MirrorTooLarge))
            raise
        if not created:
            await self._gc(mirror)
            log.info(
                "mirror fetched",
                extra={"source_id": str(mirror.source_id), "refs": len(updates), "size": size},
            )
        return FetchResult(mirror, created, updates, size)

    async def _promote(self, mirror: Mirror) -> tuple[RefUpdate, ...]:
        """Set the mirrored refs to the incoming ones in one transaction; return the changes."""
        current = await self._refs(mirror, _MIRRORED)
        incoming = {
            "refs/" + name.removeprefix(INCOMING_NAMESPACE): oid
            for name, oid in (await self._refs(mirror, (INCOMING_NAMESPACE,))).items()
        }
        updates = tuple(
            RefUpdate(ref, current.get(ref), incoming.get(ref))
            for ref in sorted(current.keys() | incoming.keys())
            if current.get(ref) != incoming.get(ref)
        )
        if updates:
            await self._git.run(
                ["update-ref", "--stdin", "-z"],
                git_dir=mirror.path,
                stdin=b"".join(_transaction_line(update) for update in updates),
            )
        return updates

    async def _clean_failed_fetch(self, mirror: Mirror, *, drop_incoming: bool) -> None:
        """Remove what a failed fetch left: temporary packs, ``.keep`` files of an interrupted
        transfer and, when the mirror grew too large, the incoming refs, so that ``gc`` can
        reclaim their objects. Never touches the refs readers see. Best effort."""
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_remove_fetch_leftovers, mirror.path)
        if drop_incoming:
            with contextlib.suppress(Exception):
                stale = await self._refs(mirror, (INCOMING_NAMESPACE,))
                if stale:
                    await self._git.run(
                        ["update-ref", "--stdin", "-z"],
                        git_dir=mirror.path,
                        stdin=b"".join(_encode(f"delete {ref}\0\0") for ref in stale),
                    )

    async def _gc(self, mirror: Mirror) -> None:
        try:
            await self._git.run(
                ["gc", "--auto", "--quiet"],
                git_dir=mirror.path,
                timeout=self._options.fetch_timeout,
            )
        except Exception as error:
            # The fetch succeeded; housekeeping is retried after the next one.
            log.warning(
                "git gc failed on a mirror: %s", error, extra={"source_id": str(mirror.source_id)}
            )

    # --- Reads -----------------------------------------------------------------------------------

    async def refs(self, mirror: Mirror) -> dict[str, str]:
        """Branches and tags of the mirror: ref name to object id (a tag object for an annotated
        tag)."""
        return await self._refs(mirror, _MIRRORED)

    async def _refs(self, mirror: Mirror, prefixes: Sequence[str]) -> dict[str, str]:
        output = await self._git.run(
            ["for-each-ref", "--format=%(refname)%00%(objectname)", *prefixes],
            git_dir=mirror.path,
        )
        refs: dict[str, str] = {}
        for line in output.stdout.splitlines():
            name, _, oid = line.partition(b"\0")
            refs[_decode(name)] = oid.decode("ascii")
        return refs

    async def head(self, mirror: Mirror) -> str | None:
        """The branch ``HEAD`` names (the remote's default branch when the mirror was created),
        or ``None`` if that branch does not exist."""
        output = await self._git.run(
            ["symbolic-ref", "--quiet", "HEAD"], git_dir=mirror.path, check=False
        )
        name = _decode(output.stdout.strip())
        if output.returncode != 0 or not name:
            return None
        return name if name in await self.refs(mirror) else None

    async def resolve(self, mirror: Mirror, ref: str) -> str:
        """The commit ``ref`` names: ``HEAD``, a branch or tag name (``main``, ``v1.0``), a full
        ref under ``refs/heads/`` or ``refs/tags/``, or an object id. Tags are peeled.

        Raises:
            InvalidRevision: anything else, revision expressions included (``main~1``,
                ``HEAD@{1}``, ``:/text``).
            RevisionNotFound: no such ref or commit.
        """
        if _OID.fullmatch(ref):
            candidates = [ref]
        elif ref == "HEAD":
            candidates = ["HEAD"]
        elif ref.startswith("refs/"):
            if not ref.startswith(_MIRRORED):
                raise InvalidRevision("a full ref is under refs/heads/ or refs/tags/")
            candidates = [_check_ref_name(ref)]
        else:
            name = _check_ref_name(ref)
            candidates = [f"refs/heads/{name}", f"refs/tags/{name}"]
        for candidate in candidates:
            commit = await self._peel(mirror, candidate)
            if commit is not None:
                return commit
        raise RevisionNotFound(f"no commit {ref!r} in the mirror")

    async def _peel(self, mirror: Mirror, revision: str) -> str | None:
        output = await self._git.run(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
            git_dir=mirror.path,
            check=False,
        )
        commit = output.stdout.strip().decode("ascii", "replace")
        return commit if output.returncode == 0 and _OID.fullmatch(commit) else None

    async def _commit(self, mirror: Mirror, commit: str) -> str:
        if await self._peel(mirror, _check_oid(commit)) != commit:
            raise RevisionNotFound(f"no commit {commit} in the mirror")
        return commit

    async def ls_tree(self, mirror: Mirror, commit: str) -> list[TreeEntry]:
        """Every file of ``commit`` (an object id), recursively: blobs, symlinks and submodules,
        not directories."""
        await self._commit(mirror, commit)
        output = await self._git.run(
            ["ls-tree", "-r", "-z", "-l", "--full-tree", commit], git_dir=mirror.path
        )
        return [_tree_entry(record) for record in output.stdout.split(b"\0") if record]

    async def entry(self, mirror: Mirror, commit: str, path: str) -> TreeEntry:
        """The entry at ``path`` in ``commit``: a file, a directory or a submodule.

        Raises:
            InvalidPath: see :func:`check_path`.
            RevisionNotFound: ``commit`` is not in the mirror.
            PathNotFound: nothing at ``path``.
        """
        check_path(path)
        await self._commit(mirror, commit)
        output = await self._git.run(
            ["ls-tree", "-z", "-l", "--full-tree", commit, "--", path], git_dir=mirror.path
        )
        for record in output.stdout.split(b"\0"):
            if record and (entry := _tree_entry(record)).path == path:
                return entry
        raise PathNotFound(f"no {path!r} in commit {commit}")

    async def list_directory(self, mirror: Mirror, commit: str, path: str = "") -> list[TreeEntry]:
        """The entries of the directory at ``path`` in ``commit`` (an object id), not recursive:
        files, symlinks, submodules and directories, in git's order (by name). ``""`` is the root.

        Each entry's ``path`` is its full repository-relative path. A symlink is listed, never
        followed.

        Raises:
            InvalidPath: see :func:`check_path`; ``""`` is allowed here.
            RevisionNotFound: ``commit`` is not in the mirror.
            PathNotFound: no directory at ``path``: nothing there, or a file.
        """
        if path:
            check_path(path)
        await self._commit(mirror, commit)
        args = ["ls-tree", "-z", "-l", "--full-tree", commit]
        if path:
            # With the trailing slash, ls-tree lists what is inside the directory; a file of that
            # name matches nothing.
            args += ["--", f"{path}/"]
        output = await self._git.run(args, git_dir=mirror.path)
        entries = [_tree_entry(record) for record in output.stdout.split(b"\0") if record]
        if path and not entries:  # a git tree is never empty, but the root of an empty commit
            raise PathNotFound(f"no directory {path!r} in commit {commit}")
        return entries

    async def read_blob(
        self, mirror: Mirror, commit: str, path: str, *, max_size: int | None = None
    ) -> bytes:
        """The content of the file at ``path`` in ``commit``; a symlink gives its target.

        Raises:
            InvalidPath, RevisionNotFound, PathNotFound: see :meth:`entry`.
            NotABlob: ``path`` is a directory or a submodule.
            BlobTooLarge: the file is larger than ``max_size`` (default ``git_max_blob_size``).
        """
        entry = await self.entry(mirror, commit, path)
        if entry.kind != "blob" or entry.size is None:
            raise NotABlob(f"{path!r} is not a file in commit {commit}")
        limit = self._options.max_blob_size if max_size is None else max_size
        if entry.size > limit:
            raise BlobTooLarge(path, entry.size, limit)
        output = await self._git.run(
            ["cat-file", "blob", entry.oid], git_dir=mirror.path, max_output=limit
        )
        return output.stdout

    async def read_blobs(self, mirror: Mirror, oids: Sequence[str]) -> dict[str, bytes]:
        """The contents of many blobs by object id, in one ``git cat-file --batch``.

        For indexing: take the ids and sizes from :meth:`ls_tree` or :meth:`diff_names`, leave out
        files over the size limit, and ask in batches whose total size fits in memory. Ids that
        are missing or not blobs are left out of the result.

        Raises:
            InvalidRevision: an id is malformed.
            BlobTooLarge: a blob is larger than ``git_max_blob_size``.
        """
        if not oids:
            return {}
        for oid in oids:
            _check_oid(oid)
        limit = self._options.max_blob_size
        output = await self._git.run(
            ["cat-file", "--batch"],
            git_dir=mirror.path,
            stdin="".join(f"{oid}\n" for oid in oids).encode("ascii"),
            max_output=len(oids) * (limit + 128),
        )
        blobs: dict[str, bytes] = {}
        data = output.stdout
        position = 0
        while position < len(data):
            end = data.index(b"\n", position)
            header = data[position:end].split(b" ")
            position = end + 1
            if len(header) != 3:  # "<oid> missing" or "<oid> ambiguous"
                continue
            oid, kind, size = header[0].decode("ascii"), header[1], int(header[2])
            content = data[position : position + size]
            position += size + 1
            if kind != b"blob":
                continue
            if size > limit:
                raise BlobTooLarge(oid, size, limit)
            blobs[oid] = content
        return blobs

    async def blob_sizes(self, mirror: Mirror, oids: Sequence[str]) -> dict[str, int]:
        """The sizes in bytes of many blobs by object id, in one ``git cat-file --batch-check``.

        :meth:`diff_names` gives object ids without sizes; this sizes them before
        :meth:`read_blobs`, so a file over the read limit can be left out. Ids that are missing or
        not blobs are left out of the result.

        Raises:
            InvalidRevision: an id is malformed.
        """
        if not oids:
            return {}
        for oid in oids:
            _check_oid(oid)
        output = await self._git.run(
            ["cat-file", "--batch-check"],
            git_dir=mirror.path,
            stdin="".join(f"{oid}\n" for oid in oids).encode("ascii"),
            max_output=len(oids) * 128,
        )
        sizes: dict[str, int] = {}
        for line in output.stdout.splitlines():
            fields = line.split(b" ")
            if len(fields) == 3 and fields[1] == b"blob":  # else "<oid> missing" or another type
                sizes[fields[0].decode("ascii")] = int(fields[2])
        return sizes

    async def committed_at(self, mirror: Mirror, commit: str) -> datetime:
        """The committer time of ``commit`` (an object id), in UTC.

        Raises:
            InvalidRevision: ``commit`` is not an object id.
            RevisionNotFound: no such commit in the mirror.
        """
        await self._commit(mirror, commit)
        output = await self._git.run(
            ["show", "--no-patch", "--no-notes", "--format=%ct", commit],
            git_dir=mirror.path,
            max_output=_SMALL_OUTPUT,
        )
        return datetime.fromtimestamp(int(output.stdout.strip()), UTC)

    async def change_times(
        self, mirror: Mirror, commit: str, *, since: str | None = None, max_commits: int
    ) -> ChangeTimes:
        """When each file last changed on the first-parent line of ``commit`` (an object id),
        from one ``git log`` walk of at most ``max_commits`` commits, newest first: the commits
        after ``since`` (an object id, excluded with its ancestors), or back to the root.

        One process whatever the number of files; its cost grows with the commits walked and the
        files they touched, so ``max_commits`` bounds it on a long history.

        Raises:
            InvalidRevision: an id is malformed.
            ValueError: ``max_commits`` is less than 1.
            GitCommandError: a commit is not in the mirror.
            OutputTooLarge: the walk listed more than the output limit.
        """
        _check_oid(commit)
        if since is not None:
            _check_oid(since)
        if max_commits < 1:
            raise ValueError("a walk takes at least one commit")
        output = await self._git.run(
            [
                "log",
                "--first-parent",
                "--diff-merges=first-parent",
                "--root",
                "--no-renames",
                "--name-only",
                "-z",
                "--format=%x00%ct",
                f"--max-count={max_commits}",
                "--end-of-options",
                commit,
                *([] if since is None else [f"^{since}"]),
                "--",
            ],
            git_dir=mirror.path,
        )
        # Each commit is "\0<time>\0", then its paths, each ended by "\0", the first after a
        # "\n". A path is never empty, so an empty field always comes before a commit's time.
        times: dict[str, datetime] = {}
        commits = 0
        current: datetime | None = None
        header = first_path = False
        for field in output.stdout.split(b"\0"):
            if not field:
                header = True
            elif header:
                if not field.isdigit():
                    raise GitCommandError("log", 0, "unexpected output of git log")
                current = datetime.fromtimestamp(int(field), UTC)
                commits += 1
                header, first_path = False, True
            elif current is not None:
                if first_path:
                    field = field.removeprefix(b"\n")
                    first_path = False
                times.setdefault(_decode(field), current)
        return ChangeTimes(times, commits, commits < max_commits, current)

    async def log(
        self,
        mirror: Mirror,
        starts: Sequence[str],
        *,
        paths: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        max_commits: int,
        timeout: float | None = None,
        max_output: int = MAX_LOG_OUTPUT,
    ) -> CommitLog:
        """The commits reachable from ``starts`` (object ids), newest first, each with its author,
        times, message and the paths it changed: one ``git log`` of at most ``max_commits``
        commits. Nothing but ``starts`` is walked from: no other branch or tag of the mirror.

        Args:
            starts: commits to walk from, their ancestors included. One the mirror no longer
                holds (a pin that a force push and ``gc`` removed) is left out; with none left,
                nothing is walked.
            paths: only commits that changed one of these files, or a file below one of these
                directories, and only those paths among a commit's changes. They are taken
                literally (:func:`check_path`), never as patterns. Git then leaves out a merge
                that agrees with one of its parents there, as ``git log -- <path>`` does; a
                merge it prints may still list no path, when it changed none against every
                parent.
            since: only commits committed at or after this time.
            until: only commits committed before this time.
            max_commits: the most commits printed. Git stops there, so the walk is bounded by
                the commits that match, not by the length of the history; ``timeout`` bounds
                the rest.
            timeout: seconds the walk may take; ``options.command_timeout`` by default.
            max_output: bytes the walk may print.

        With ``since``, git stops a line of history at its first commit older than that, as
        ``git log --since`` does: a commit whose clock ran behind hides its ancestors.

        Raises:
            InvalidRevision: an id is malformed.
            InvalidPath: see :func:`check_path`.
            ValueError: ``max_commits`` is less than 1, or a time has no zone.
            GitTimeout: the walk ran past ``timeout`` and was killed.
            OutputTooLarge: the walk printed more than ``max_output``.
        """
        if max_commits < 1:
            raise ValueError("a walk takes at least one commit")
        for commit in starts:
            _check_oid(commit)
        for path in paths:
            check_path(path)
        args = [
            "log",
            "--no-renames",
            "--name-status",
            "--diff-merges=combined",
            "--no-ext-diff",
            "--no-textconv",
            "--encoding=UTF-8",
            "-z",
            f"--format={_LOG_FORMAT}",
            f"--max-count={max_commits}",
        ]
        if since is not None:
            first = _epoch(since)
            if first > 0:  # no commit is older than the epoch
                args.append(f"--since=@{first} +0000")
        if until is not None:
            # git's --until includes its second; a commit time has no fraction of one.
            last = _epoch(until) - 1
            if last < 0:
                return CommitLog((), True, ())
            args.append(f"--until=@{last} +0000")
        present = [
            commit for commit in dict.fromkeys(starts) if await self._peel(mirror, commit) == commit
        ]
        if not present:  # with no revision, git log would walk from HEAD
            return CommitLog((), True, ())
        output = await self._git.run(
            [*args, "--end-of-options", *present, "--", *paths],
            git_dir=mirror.path,
            timeout=timeout,
            max_output=max_output,
        )
        commits = _parse_log(output.stdout)
        return CommitLog(tuple(commits), len(commits) < max_commits, tuple(present))

    async def diff_names(self, mirror: Mirror, old: str, new: str) -> list[FileChange]:
        """The files that differ from commit ``old`` to commit ``new`` (object ids).

        Raises:
            InvalidRevision: an id is malformed.
            RevisionNotFound: a commit is not in the mirror, for example an old pin that a force
                push made unreachable and ``gc`` removed; list the new commit in full instead.
        """
        await self._commit(mirror, old)
        await self._commit(mirror, new)
        output = await self._git.run(
            [
                "diff-tree",
                "-r",
                "-z",
                "--raw",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-abbrev",
                old,
                new,
            ],
            git_dir=mirror.path,
        )
        fields = output.stdout.split(b"\0")
        changes: list[FileChange] = []
        for index in range(0, len(fields) - 1, 2):
            meta, raw_path = fields[index], fields[index + 1]
            if not meta.startswith(b":"):
                break
            old_mode, new_mode, old_oid, new_oid, status = meta[1:].decode("ascii").split(" ")
            kind = _STATUS.get(status[:1])
            if kind is None:
                continue
            changes.append(
                FileChange(
                    kind,
                    _decode(raw_path),
                    None if kind == "added" else old_mode,
                    None if kind == "deleted" else new_mode,
                    None if kind == "added" else old_oid,
                    None if kind == "deleted" else new_oid,
                )
            )
        return changes


def _tree_entry(record: bytes) -> TreeEntry:
    meta, _, raw_path = record.partition(b"\t")
    mode, kind, oid, size = meta.decode("ascii").split()
    if kind not in ("blob", "tree", "commit"):
        raise GitCommandError("ls-tree", 0, f"unexpected entry type {kind!r}")
    entry_kind: EntryKind = "blob" if kind == "blob" else "tree" if kind == "tree" else "commit"
    return TreeEntry(_decode(raw_path), mode, entry_kind, oid, int(size) if size != "-" else None)


def _epoch(moment: datetime) -> int:
    """The first whole second at or after ``moment``, as seconds since the epoch."""
    if moment.utcoffset() is None:
        raise ValueError("a time needs a zone")
    return math.ceil(moment.timestamp())


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _log_time(raw: bytes) -> datetime:
    """A commit time as git prints it, in UTC; the epoch for one no calendar holds."""
    try:
        return datetime.fromtimestamp(int(raw), UTC)
    except (ValueError, OverflowError, OSError):
        return datetime.fromtimestamp(0, UTC)


def _log_status(raw: bytes) -> ChangeStatus:
    """The status of a changed path. A merge has one letter per parent: the path was added or
    deleted when every parent says so, and modified otherwise."""
    letters = set(raw.decode("ascii", "replace"))
    if len(letters) == 1:
        return _STATUS.get(letters.pop(), "modified")
    return "modified"


def _parse_log(output: bytes) -> list[Commit]:
    """The commits ``git log -z`` printed with :data:`_LOG_FORMAT` and ``--name-status``."""
    fields = output.split(b"\0")
    end = len(fields)
    commits: list[Commit] = []
    index = 0
    while index < end:
        if not fields[index]:  # the end of the output
            index += 1
            continue
        # A NUL ends the last field too, so after the fields of a whole commit comes one more.
        if fields[index] != _LOG_MARK or index + _LOG_FIELDS + 2 > end:
            raise GitCommandError("log", 0, "unexpected output of git log")
        oid, abbrev, parents, name, email, authored, committed, subject, body = fields[
            index + 1 : index + 1 + _LOG_FIELDS
        ]
        index += 1 + _LOG_FIELDS
        changes: list[CommitChange] = []
        while index < end and fields[index] != _LOG_MARK:
            status = fields[index].removeprefix(b"\n")
            if not status:  # what stands before a merge's paths, or the end of the output
                index += 1
                continue
            if index + 1 >= end or not fields[index + 1]:  # a status without its path
                raise GitCommandError("log", 0, "unexpected output of git log")
            changes.append(CommitChange(_log_status(status), _text(fields[index + 1])))
            index += 2
        commits.append(
            Commit(
                oid=oid.decode("ascii", "replace"),
                abbrev=abbrev.decode("ascii", "replace"),
                parents=tuple(parents.decode("ascii", "replace").split()),
                author_name=_text(name),
                author_email=_text(email),
                authored_at=_log_time(authored),
                committed_at=_log_time(committed),
                subject=_text(subject),
                body=_text(body).rstrip("\n"),
                changes=tuple(changes),
            )
        )
    return commits


def _transaction_line(update: RefUpdate) -> bytes:
    if update.old is None:
        return _encode(f"create {update.ref}\0{update.new}\0")
    if update.new is None:
        return _encode(f"delete {update.ref}\0{update.old}\0")
    return _encode(f"update {update.ref}\0{update.new}\0{update.old}\0")


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _remove_fetch_leftovers(git_dir: Path) -> None:
    packs = git_dir / "objects" / "pack"
    if packs.is_dir():
        for leftover in packs.iterdir():
            if leftover.name.startswith(("tmp_", ".tmp-")) or leftover.suffix == ".keep":
                leftover.unlink(missing_ok=True)
