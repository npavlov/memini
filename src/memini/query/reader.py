"""Reading code from the mirror of a git source: a file, a line range or a directory listing.

:func:`read_source` serves the ``read_source`` MCP tool and its HTTP route. It reads what the
service's own bare mirror holds (requirements, section 3: the mirror is the raw store of code); it
never clones, fetches or touches a working copy, so a repository that has not synced yet has
nothing to read (:class:`NotSyncedError`).

Which commit
    - No ``ref``: the tracked ref the mirror's ``HEAD`` names (the remote's default branch), or,
      when ``HEAD`` names none of them, the first tracked ref by name that has a pin (a ref
      deleted at the remote has none), else the first.
    - A tracked ref (``main``, ``refs/heads/main``, ``v1`` for a tracked ``refs/tags/v1``): its
      pinned commit, the one its records and chunks are at, so a read agrees with the ``commit``
      a search hit carries (``pinned`` is true). Before its first complete sync, or after a sync
      that failed part way, the ref has no pin to trust: the commit the mirror's ref names.
    - Any other branch or tag the mirror holds: the commit it names in the mirror.
    - A full commit id (40 or 64 hexadecimal digits): that commit, if the mirror holds it.

    Revision expressions (``main~1``, ``HEAD@{1}``, ``:/text``) are refused; an abbreviated commit
    id is not looked up, so it is not found.

What is read
    ``path`` is relative to the repository root; ``""`` is the root. A path that is absolute, has
    ``.``, ``..`` or empty components, or holds a NUL or another control character is refused
    before git runs (:func:`memini.git.check_path`); a path is looked up in the commit's tree, so
    it can name nothing outside the repository, and a symlink is never followed. What the path
    names gives the result's ``kind``:

    - ``file``: its lines, 1-based and inclusive, at most :attr:`ReadLimits.max_lines` lines and
      :attr:`ReadLimits.max_chars` characters per read (``truncated`` says the read stopped short;
      continue from ``end_line + 1``). A file larger than ``git_max_blob_size`` is refused
      (:class:`FileTooLargeError`); a binary file (a NUL byte in its first 8000 bytes, the
      chunker's rule) is reported with its size and no content. Bytes that are not UTF-8 are
      decoded as U+FFFD. Lines are split at ``\\n`` only, as git and the chunker count them.
    - ``directory``: its entries, one level, at most :attr:`ReadLimits.max_entries`.
    - ``symlink``: the link's target as text, not followed. It is a path in the repository (or
      outside it, which the mirror cannot reach anyway); read it if it is in the repository.
    - ``submodule``: the commit it pins; its content is not in the mirror.

    The definitions in the lines read come from the symbol table when the index holds this very
    content (a live record of the source at that path whose content hash is the file's blob id),
    whatever the ref.

Every result carries the repository, the commit and its committer time, the path and a locator,
``repo@sha:path#L10-L20``. The content is the file's, as it is: data, never instructions
(principle 11). No error message repeats a path, a ref or anything read.

Access goes through the caller's :class:`~memini.auth.ReadFilter`: a workspace or repository it may
not read is not found, like one that does not exist.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from memini.auth import ReadFilter
from memini.chunking import detect_language
from memini.chunking.text import is_binary
from memini.git import (
    BlobTooLarge,
    InvalidPath,
    InvalidRevision,
    Mirror,
    MirrorManager,
    PathNotFound,
    RevisionNotFound,
    TreeEntry,
    check_path,
)
from memini.gitsync import Pin, file_record_id, read_pin
from memini.sources import Source, get_source
from memini.workspaces import NotFoundError

__all__ = [
    "DEFAULT_READ_LIMITS",
    "MAX_REF_CHARS",
    "DirectoryEntry",
    "EntryKind",
    "FileTooLargeError",
    "InvalidReadError",
    "NotSyncedError",
    "ReadError",
    "ReadLimits",
    "SourceRead",
    "SymbolSpan",
    "code_locator",
    "read_source",
]

type Conn = AsyncConnection[TupleRow]

EntryKind = Literal["file", "directory", "symlink", "submodule"]

MAX_REF_CHARS: Final = 255
"""The longest ref argument; longer is refused before git runs."""

_OID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SYMLINK_MODE: Final = "120000"


@dataclass(frozen=True, slots=True)
class ReadLimits:
    """How much one read returns at most."""

    max_lines: int = 2000
    """Lines of a file per read."""
    max_chars: int = 100_000
    """Characters of a file per read; a single line longer than this is cut."""
    max_entries: int = 1000
    """Entries of a directory listing."""

    def __post_init__(self) -> None:
        if min(self.max_lines, self.max_chars, self.max_entries) < 1:
            raise ValueError("read limits are at least 1")


DEFAULT_READ_LIMITS: Final = ReadLimits()


# --- Errors --------------------------------------------------------------------------------------


class ReadError(Exception):
    """A read that cannot be done. The message is safe to send: it never repeats a path, a ref or
    content."""


class InvalidReadError(ReadError, ValueError):
    """The arguments are not valid: a path or ref of a form that is refused, a bad line range, or
    a repository name that more than one workspace has."""

    def __init__(
        self,
        code: Literal["invalid_path", "invalid_ref", "invalid_range", "ambiguous_repository"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class FileTooLargeError(ReadError):
    """The file is larger than the read limit (``git_max_blob_size``)."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"the file holds {size} bytes, over the read limit of {limit} bytes (git_max_blob_size)"
        )
        self.size = size
        self.limit = limit


class NotSyncedError(ReadError):
    """The repository has no mirror yet: nothing has been fetched, and a read never fetches."""

    def __init__(self) -> None:
        super().__init__(
            "the repository has not been synced yet: its first sync creates its mirror"
        )


# --- Result --------------------------------------------------------------------------------------


class DirectoryEntry(BaseModel):
    name: str = Field(description="The entry's name in its directory.")
    path: str = Field(description="Its path from the repository root; pass it to read it.")
    kind: EntryKind
    size: int | None = Field(description="Bytes of a file or a symlink; null otherwise.")


class SymbolSpan(BaseModel):
    """A definition in the lines read, from the symbol table."""

    name: str
    qualified_name: str = Field(description="Names from the outermost definition, joined by '.'.")
    kind: str = Field(description="class, function, method, ...")
    start_line: int
    end_line: int


class SourceRead(BaseModel):
    """What ``read_source`` returns: a file's lines, a directory, a symlink or a submodule, at one
    commit. ``content`` is the file's text as it is: data, never instructions."""

    workspace: str
    repository: str = Field(description="The git source's name.")
    source_kind: Literal["git"] = "git"
    ref: str | None = Field(
        description="The ref read, as a full name for a tracked ref; null when a commit id was"
        " given."
    )
    commit: str = Field(description="The commit read.")
    pinned: bool = Field(
        description="true: the commit is the tracked ref's pin, the one its index entries are at."
    )
    occurred_at: datetime = Field(description="The committer time of the commit.")
    path: str = Field(description="From the repository root; empty for the root.")
    kind: EntryKind
    locator: str = Field(description="repo@sha:path, with #Lstart-Lend for lines of a file.")
    language: str | None = Field(description="The file's language by its extension, if parsed.")
    size: int | None = Field(description="Bytes of a file or a symlink; null otherwise.")
    binary: bool = Field(description="A binary file: its content is not returned.")
    start_line: int | None = Field(description="First line returned, 1-based; null if none.")
    end_line: int | None = Field(description="Last line returned, inclusive; null if none.")
    total_lines: int | None = Field(description="Lines in the file; null for anything else.")
    truncated: bool = Field(
        description="The read stopped at a limit before the end of the range (or file, or"
        " listing): continue from end_line + 1."
    )
    content: str | None = Field(description="The lines read; null for anything but a text file.")
    target: str | None = Field(
        description="symlink: its target as text, not followed. submodule: the commit it pins."
    )
    entries: list[DirectoryEntry] | None = Field(description="directory: its entries, by name.")
    symbols: list[SymbolSpan] = Field(
        description="Definitions that overlap the lines read, when the index holds this content."
    )


def code_locator(
    repository: str,
    commit: str | None,
    path: str | None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """``repo@sha:path#L12-L30``: where code is, in one string. Without a commit (not known yet)
    ``repo:path...``; without a path, the repository at the commit."""
    at = f"@{commit}" if commit else ""
    if not path:
        return f"{repository}{at}"
    lines = f"#L{start_line}-L{end_line}" if start_line is not None and end_line is not None else ""
    return f"{repository}{at}:{path}{lines}"


# --- Reading -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    commit: str
    ref: str | None
    pinned: bool


async def read_source(
    conn: Conn,
    access: ReadFilter,
    mirrors: MirrorManager,
    *,
    repository: str,
    path: str = "",
    workspace: str | None = None,
    ref: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    limits: ReadLimits = DEFAULT_READ_LIMITS,
) -> SourceRead:
    """Read ``path`` in the git source ``repository``; see the module.

    ``workspace`` may be left out when exactly one workspace the caller may read has a git source
    of that name. ``conn`` is used for two short transactions (the source and its pins, then the
    symbols) or savepoints when the caller has one open; git runs in between.

    Raises:
        InvalidReadError: a path, ref or line range that is refused, or an ambiguous repository.
        NotFoundError: no such workspace, repository, ref, commit or path that the caller may read.
        NotSyncedError: the repository has no mirror yet.
        FileTooLargeError: the file is larger than ``git_max_blob_size``.
        GitError: git failed.
    """
    path = _check_path(path)
    _check_ref(ref)
    _check_range(start_line, end_line)

    async with conn.transaction():
        source = await _find_repository(conn, access, workspace, repository)
        pins = await _pins(conn, access, source.id)
    mirror = mirrors.find(source.id)
    if mirror is None:
        raise NotSyncedError
    target = await _target(mirrors, mirror, source, pins, ref)
    try:
        occurred_at = await mirrors.committed_at(mirror, target.commit)
    except RevisionNotFound:  # a pin that a force push and gc took from the mirror
        raise NotFoundError("ref or commit") from None

    def result(kind: EntryKind, **fields: Any) -> SourceRead:
        values: dict[str, Any] = {
            "workspace": source.workspace,
            "repository": source.name,
            "ref": target.ref,
            "commit": target.commit,
            "pinned": target.pinned,
            "occurred_at": occurred_at,
            "path": path,
            "kind": kind,
            "locator": code_locator(source.name, target.commit, path),
            "language": None,
            "size": None,
            "binary": False,
            "start_line": None,
            "end_line": None,
            "total_lines": None,
            "truncated": False,
            "content": None,
            "target": None,
            "entries": None,
            "symbols": [],
        }
        values.update(fields)
        return SourceRead.model_validate(values)

    if path:
        try:
            entry = await mirrors.entry(mirror, target.commit, path)
        except PathNotFound:
            raise NotFoundError("path") from None
    else:
        entry = None
    if entry is None or entry.kind == "tree":
        listing = await mirrors.list_directory(mirror, target.commit, path)
        entries = [
            DirectoryEntry(
                name=item.path.rsplit("/", 1)[-1], path=item.path, kind=_kind(item), size=item.size
            )
            for item in listing[: limits.max_entries]
        ]
        return result("directory", entries=entries, truncated=len(listing) > limits.max_entries)
    if entry.kind == "commit":
        return result("submodule", target=entry.oid)

    content = await _blob(mirrors, mirror, entry)
    if entry.mode == _SYMLINK_MODE:
        return result("symlink", size=len(content), target=content.decode("utf-8", "replace"))
    language = detect_language(path)
    if is_binary(content):
        return result("file", size=len(content), language=language, binary=True)
    lines = _lines(content.decode("utf-8", "replace"))
    selected = _select(lines, start_line, end_line, limits)
    if selected.first is None or selected.last is None:  # an empty file
        return result("file", size=0, language=language, content="", total_lines=0)
    async with conn.transaction():
        symbols = await _symbols(
            conn, access, source.id, sorted(pins), path, entry.oid, selected.first, selected.last
        )
    return result(
        "file",
        locator=code_locator(source.name, target.commit, path, selected.first, selected.last),
        size=len(content),
        language=language,
        start_line=selected.first,
        end_line=selected.last,
        total_lines=len(lines),
        truncated=selected.truncated,
        content=selected.text,
        symbols=symbols,
    )


def _check_path(path: str) -> str:
    """``path`` as it is looked up: one trailing slash dropped (``src/`` names the directory)."""
    plain = path[:-1] if path.endswith("/") else path
    if not plain:
        if path:  # "/" alone is absolute
            raise InvalidReadError("invalid_path", _PATH_RULE)
        return ""
    try:
        return check_path(plain)
    except InvalidPath:
        raise InvalidReadError("invalid_path", _PATH_RULE) from None


_PATH_RULE: Final = (
    "a path is relative to the repository root (empty for the root), without '.', '..' or empty"
    " components and without control characters"
)


def _check_ref(ref: str | None) -> None:
    if ref is None:
        return
    if (
        not ref
        or len(ref) > MAX_REF_CHARS
        or any(ord(char) < 0x20 or char == "\x7f" for char in ref)
    ):
        raise InvalidReadError("invalid_ref", _REF_RULE)


_REF_RULE: Final = (
    "a ref is a branch or tag name, a full ref under refs/heads/ or refs/tags/, or a full commit"
    " id of 40 or 64 hexadecimal digits; revision expressions are not accepted"
)


def _check_range(start_line: int | None, end_line: int | None) -> None:
    if start_line is not None and start_line < 1:
        raise InvalidReadError("invalid_range", "the start line is 1 or more")
    if end_line is not None and end_line < 1:
        raise InvalidReadError("invalid_range", "the end line is 1 or more")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise InvalidReadError("invalid_range", "the end line is before the start line")


async def _find_repository(
    conn: Conn, access: ReadFilter, workspace: str | None, repository: str
) -> Source:
    if workspace is None:
        query = sql.SQL(
            "SELECT w.name FROM source s JOIN workspace w ON w.id = s.workspace_id"
            " WHERE s.name = %s AND s.kind = 'git' AND {} ORDER BY w.name LIMIT 2"
        ).format(
            access.where(
                workspace_id=sql.Identifier("s", "workspace_id"),
                owner_id=sql.Identifier("s", "owner_id"),
                source_id=sql.Identifier("s", "id"),
            )
        )
        cur = await conn.execute(query, (repository,))
        names: list[str] = [row[0] for row in await cur.fetchall()]
        if not names:
            raise NotFoundError("repository")
        if len(names) > 1:
            raise InvalidReadError(
                "ambiguous_repository",
                "more than one workspace has a repository of that name: name the workspace",
            )
        workspace = names[0]
    try:
        source = await get_source(conn, access, workspace, repository)
    except NotFoundError as error:
        if error.what == "source":
            raise NotFoundError("repository") from None
        raise
    if source.kind != "git":
        raise NotFoundError("repository")
    return source


async def _pins(conn: Conn, access: ReadFilter, source_id: UUID) -> dict[str, Pin | None]:
    """The pin of every tracked ref of the source, by ref."""
    query = sql.SQL(
        "SELECT st.key, st.cursor FROM stream st WHERE st.source_id = %s AND {}"
    ).format(
        access.where(
            workspace_id=sql.Identifier("st", "workspace_id"),
            owner_id=sql.Identifier("st", "owner_id"),
            source_id=sql.Identifier("st", "source_id"),
        )
    )
    cur = await conn.execute(query, (source_id,))
    return {row[0]: read_pin(row[1]) for row in await cur.fetchall()}


async def _target(
    mirrors: MirrorManager,
    mirror: Mirror,
    source: Source,
    pins: dict[str, Pin | None],
    ref: str | None,
) -> _Target:
    """The commit to read, and how it was chosen; see the module."""
    tracked = sorted(pins)
    if ref is not None and _OID.fullmatch(ref):
        return _Target(await _resolve(mirrors, mirror, ref), None, pinned=False)
    if ref is None:
        if not tracked:
            raise NotFoundError("ref")
        head = await mirrors.head(mirror)
        if head is not None and head in pins:
            chosen = head
        else:  # a ref deleted at the remote has no pin: prefer one that has
            chosen = next((key for key in tracked if pins[key] is not None), tracked[0])
    else:
        names = [ref] if ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}"]
        chosen = next((name for name in names if name in pins), None)
        if chosen is None:  # a branch or tag the mirror has but the source does not track
            return _Target(await _resolve(mirrors, mirror, ref), ref, pinned=False)
    pin = pins[chosen]
    if pin is not None and not pin.partial:
        return _Target(pin.commit, chosen, pinned=True)
    return _Target(await _resolve(mirrors, mirror, chosen), chosen, pinned=False)


async def _resolve(mirrors: MirrorManager, mirror: Mirror, ref: str) -> str:
    try:
        return await mirrors.resolve(mirror, ref)
    except InvalidRevision:
        raise InvalidReadError("invalid_ref", _REF_RULE) from None
    except RevisionNotFound:
        raise NotFoundError("ref or commit") from None


def _kind(entry: TreeEntry) -> EntryKind:
    if entry.kind == "tree":
        return "directory"
    if entry.kind == "commit":
        return "submodule"
    return "symlink" if entry.mode == _SYMLINK_MODE else "file"


async def _blob(mirrors: MirrorManager, mirror: Mirror, entry: TreeEntry) -> bytes:
    """The content of a file or symlink entry, within the read limit."""
    limit = mirrors.options.max_blob_size
    if entry.size is not None and entry.size > limit:
        raise FileTooLargeError(entry.size, limit)
    try:
        blobs = await mirrors.read_blobs(mirror, [entry.oid])
    except BlobTooLarge as error:
        raise FileTooLargeError(error.size, error.limit) from None
    content = blobs.get(entry.oid)
    if content is None:  # listed in the tree, so only a damaged mirror lacks it
        raise RuntimeError(f"the mirror of source {mirror.source_id} lacks blob {entry.oid}")
    return content


def _lines(text: str) -> list[str]:
    """The lines of ``text``, each with its ``\\n``; a final newline starts no line."""
    if not text:
        return []
    lines = text.split("\n")
    with_ends = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        with_ends.append(lines[-1])
    return with_ends


@dataclass(frozen=True, slots=True)
class _Selection:
    first: int | None
    last: int | None
    text: str
    truncated: bool


def _select(
    lines: Sequence[str], start_line: int | None, end_line: int | None, limits: ReadLimits
) -> _Selection:
    """The lines from ``start_line`` to ``end_line`` (1-based, inclusive), within the limits."""
    total = len(lines)
    first = start_line or 1
    if total == 0:
        if first > 1:
            raise InvalidReadError("invalid_range", "the start line is past the end of the file")
        return _Selection(None, None, "", truncated=False)
    if first > total:
        raise InvalidReadError(
            "invalid_range", f"the start line is past the end of the file, which has {total} lines"
        )
    wanted = total if end_line is None else min(end_line, total)
    last = min(wanted, first + limits.max_lines - 1)
    chosen: list[str] = []
    used = 0
    for line in lines[first - 1 : last]:
        if used + len(line) > limits.max_chars:
            if not chosen:  # one line longer than the limit: cut it
                chosen.append(line[: limits.max_chars])
            break
        chosen.append(line)
        used += len(line)
    last = first + len(chosen) - 1
    cut = len(chosen[-1]) < len(lines[last - 1])
    return _Selection(first, last, "".join(chosen), truncated=last < wanted or cut)


async def _symbols(
    conn: Conn,
    access: ReadFilter,
    source_id: UUID,
    refs: Sequence[str],
    path: str,
    blob: str,
    first: int,
    last: int,
) -> list[SymbolSpan]:
    """The definitions of this exact content that overlap lines ``first`` to ``last``.

    From the live record of ``path`` on any tracked ref whose content hash is the blob id: the
    same content has the same definitions, whichever ref or commit it was read at.
    """
    if not refs:
        return []
    query = sql.SQL(
        "SELECT DISTINCT ON (s.start_line, s.qualified_name, s.ordinal)"
        " s.name, s.qualified_name, s.kind, s.start_line, s.end_line"
        " FROM record r JOIN symbol s ON s.record_id = r.id"
        " WHERE r.id = ANY(%s) AND r.content_hash = %s AND r.deleted_at IS NULL"
        " AND s.start_line <= %s AND s.end_line >= %s AND {}"
        " ORDER BY s.start_line, s.qualified_name, s.ordinal"
    ).format(
        access.where(
            workspace_id=sql.Identifier("r", "workspace_id"),
            owner_id=sql.Identifier("r", "owner_id"),
            source_id=sql.Identifier("r", "source_id"),
        )
    )
    record_ids = [file_record_id(source_id, ref, path) for ref in refs]
    cur = await conn.execute(query, (record_ids, blob, last, first))
    return [
        SymbolSpan(
            name=row[0], qualified_name=row[1], kind=row[2], start_line=row[3], end_line=row[4]
        )
        for row in await cur.fetchall()
    ]
