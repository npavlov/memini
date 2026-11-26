"""What a repository would index at a ref, before a sync does it: counts, not content.

A person choosing what a code base indexes (scenario S18) needs to see how many files a set of
rules keeps, by kind of file and by folder, before the model spends an hour on them. The preview
lists the tree of the ref in the source's mirror and asks each entry the same question the sync
asks (:meth:`memini.gitsync.rules.FileRules.skip_reason`), so what it counts is what a sync with
those rules writes. Two things it cannot know without reading every file: a binary file is counted
as indexed (the sync finds it when it reads it, and writes no chunks), and a file forgotten by hand
is counted too. It writes nothing and needs no transaction.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

from memini.git import Mirror, MirrorManager
from memini.gitsync.rules import FileRules, SkipReason

__all__ = ["MAX_GROUPS", "Preview", "PreviewGroup", "preview_ref", "summarize"]

MAX_GROUPS: Final = 50
"""Groups listed by extension and by folder; the rest are summed into one named ``…``."""

NO_EXTENSION: Final = "(none)"
ROOT_FOLDER: Final = "(root)"
OTHER: Final = "…"


class _Entry(Protocol):
    @property
    def path(self) -> str: ...
    @property
    def mode(self) -> str: ...
    @property
    def size(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class PreviewGroup:
    """Files of one extension or one top-level folder: how many the ref has, how many are kept."""

    key: str
    tracked: int
    indexed: int


@dataclass(frozen=True, slots=True)
class Preview:
    """What the rules keep of a ref's tree."""

    ref: str
    commit: str
    tracked: int
    """Regular files of the tree (not symlinks, submodules or directories)."""
    indexed: int
    """Of those, the files the rules keep."""
    skipped: dict[SkipReason, int]
    """Entries left out, by reason."""
    by_extension: tuple[PreviewGroup, ...]
    by_folder: tuple[PreviewGroup, ...]


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem or not suffix:  # no dot, or a dot file such as .gitignore
        return NO_EXTENSION
    return "." + suffix.lower()


def _folder(path: str) -> str:
    head, slash, _ = path.partition("/")
    return head if slash else ROOT_FOLDER


def _groups(tracked: Counter[str], indexed: Counter[str], limit: int) -> tuple[PreviewGroup, ...]:
    ordered = sorted(tracked, key=lambda key: (-tracked[key], key))
    shown = [PreviewGroup(key, tracked[key], indexed[key]) for key in ordered[:limit]]
    rest = ordered[limit:]
    if rest:
        shown.append(
            PreviewGroup(
                OTHER, sum(tracked[key] for key in rest), sum(indexed[key] for key in rest)
            )
        )
    return tuple(shown)


def summarize(
    entries: Iterable[_Entry], rules: FileRules, *, ref: str, commit: str, limit: int = MAX_GROUPS
) -> Preview:
    """Count what ``rules`` keep of ``entries``, the tree of ``commit``."""
    skipped: Counter[SkipReason] = Counter()
    tracked_ext: Counter[str] = Counter()
    indexed_ext: Counter[str] = Counter()
    tracked_dir: Counter[str] = Counter()
    indexed_dir: Counter[str] = Counter()
    for entry in entries:
        reason = rules.skip_reason(entry.path, entry.mode, entry.size)
        if reason in ("not_a_file", "bad_path"):
            skipped[reason] += 1
            continue
        extension, folder = _extension(entry.path), _folder(entry.path)
        tracked_ext[extension] += 1
        tracked_dir[folder] += 1
        if reason is not None:
            skipped[reason] += 1
            continue
        indexed_ext[extension] += 1
        indexed_dir[folder] += 1
    return Preview(
        ref=ref,
        commit=commit,
        tracked=sum(tracked_ext.values()),
        indexed=sum(indexed_ext.values()),
        skipped=dict(skipped),
        by_extension=_groups(tracked_ext, indexed_ext, limit),
        by_folder=_groups(tracked_dir, indexed_dir, limit),
    )


async def preview_ref(
    mirrors: MirrorManager, mirror: Mirror, ref: str, rules: FileRules
) -> Preview:
    """The preview of ``ref`` in ``mirror`` under ``rules``.

    Raises:
        RevisionNotFound: the mirror has no such ref.
        GitError: git failed.
    """
    commit = await mirrors.resolve(mirror, ref)
    return summarize(await mirrors.ls_tree(mirror, commit), rules, ref=ref, commit=commit)
