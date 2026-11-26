"""Which files of a tracked ref are indexed, and as what: the source's globs, built-in exclusions,
type and size, and the kind of record a file becomes.

A file of a tree is indexed when it is a regular file (mode ``100644`` or ``100755``: not a
symlink, not a submodule), its path can be stored (UTF-8), the source's ``include`` and ``exclude``
globs select it (:meth:`memini.sources.GitConfig.selects`), no built-in exclusion matches it
(:data:`BUILTIN_EXCLUDE`), it is at most ``git_max_blob_size`` bytes, and it is not binary (a NUL
byte in its first 8000 bytes, the chunker's rule). Every other file is skipped, for one
:data:`SkipReason`; a skipped file that had a record becomes a tombstone.

The built-in exclusions are generated files that are large, change with every dependency update and
answer no question about how something is implemented: lock files, minified bundles, source maps,
installed packages. They are few on purpose; vendored code, generated code and fixtures are left to
the source's own ``exclude``. A source switches them off with ``"builtin_excludes": false`` in its
configuration (:class:`memini.sources.GitConfig`); its ``exclude`` still applies.

A Markdown file (:data:`DOCUMENT_SUFFIXES`, by the end of its name, in any case) becomes a
``document`` record, chunked by headings; every other file a ``code_file`` record
(:func:`file_kind`). Both keep no payload: the mirror is the raw store of both.

:attr:`FileRules.digest` names the rules; the pin of a stream stores it, and a sync whose rules
differ from the pinned ones lists the ref in full instead of diffing it, so a changed glob or size
limit adds and removes files that no commit touched, and a file whose kind changed is written as a
record of its new kind while its record of the old kind becomes a tombstone.
"""

from dataclasses import dataclass
from typing import Final, Literal

from memini.ids import hash_fields
from memini.sources import GitConfig, path_matches

__all__ = [
    "BUILTIN_EXCLUDE",
    "DOCUMENT_SUFFIXES",
    "FILE_MODES",
    "RULES_DOMAIN",
    "FileKind",
    "FileRules",
    "SkipReason",
    "file_kind",
    "storable_path",
]

RULES_DOMAIN: Final = "memini.gitsync.rules.v2"
"""Domain tag of :attr:`FileRules.digest`. A change of what the digest covers takes a new one.
v2 added the kinds of record (:data:`DOCUMENT_SUFFIXES`)."""

DOCUMENT_SUFFIXES: Final[tuple[str, ...]] = (".md", ".markdown", ".mdx")
"""Ends of file names, in lower case, that make a file a Markdown ``document`` record. MDX is
Markdown with JSX in it; its headings are Markdown headings."""

FileKind = Literal["code_file", "document"]
"""The kinds of record a file of a repository becomes."""

BUILTIN_EXCLUDE: Final[tuple[str, ...]] = (
    "**/node_modules/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.js.map",
    "**/*.css.map",
    "**/package-lock.json",
    "**/npm-shrinkwrap.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/bun.lockb",
    "**/Cargo.lock",
    "**/Gemfile.lock",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/uv.lock",
    "**/composer.lock",
    "**/go.sum",
)
"""Path globs (:func:`memini.sources.path_matches`) that are not indexed, in any source that does
not switch them off (``builtin_excludes``)."""

FILE_MODES: Final = frozenset({"100644", "100755"})
"""Tree entry modes of regular files: a plain and an executable file."""

SkipReason = Literal["bad_path", "not_a_file", "excluded", "too_large", "binary", "forgotten"]
"""Why a file is not indexed: a path that is not UTF-8; a symlink, submodule or directory; left
out by a glob; larger than ``git_max_blob_size``; binary; its record was forgotten by hand
(:func:`memini.removal.forget_record`), so it is not written again."""


def file_kind(path: str) -> FileKind:
    """The kind of record the file at ``path`` becomes: ``document`` for Markdown
    (:data:`DOCUMENT_SUFFIXES`), else ``code_file``."""
    return (
        "document" if path.rsplit("/", 1)[-1].lower().endswith(DOCUMENT_SUFFIXES) else "code_file"
    )


def storable_path(path: str) -> bool:
    """Whether a repository path can be stored as text: git paths are bytes, and one that is not
    UTF-8 reaches Python with lone surrogates (:class:`memini.git.TreeEntry`)."""
    try:
        path.encode()
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class FileRules:
    """The rules of one repository: its globs, and the read limit of the mirrors."""

    config: GitConfig
    max_size: int
    """``git_max_blob_size``: a larger file is skipped."""

    def skip_reason(self, path: str, mode: str, size: int | None) -> SkipReason | None:
        """Why the tree entry is not indexed, or ``None`` when it is, as far as its path, mode and
        size tell (binary files are found when they are read). ``size`` is ``None`` for an entry
        that is not a blob."""
        if not storable_path(path):
            return "bad_path"
        if mode not in FILE_MODES or size is None:
            return "not_a_file"
        if not self.config.selects(path) or any(
            path_matches(pattern, path) for pattern in self.builtin_exclude
        ):
            return "excluded"
        if size > self.max_size:
            return "too_large"
        return None

    @property
    def builtin_exclude(self) -> tuple[str, ...]:
        """The built-in exclusions that apply: :data:`BUILTIN_EXCLUDE`, or none when the source
        switched them off."""
        return BUILTIN_EXCLUDE if self.config.builtin_excludes else ()

    @property
    def digest(self) -> str:
        """A digest of everything that decides which files are indexed, and as which kind of
        record, besides their content.

        The built-in exclusions enter as the list that applies, so a source that keeps them has
        the digest it had before they could be switched off, and switching them off or on changes
        it: the next sync lists the ref in full once."""
        include, exclude = self.config.include, self.config.exclude
        return hash_fields(
            RULES_DOMAIN,
            str(self.max_size).encode(),
            *_counted(include),
            *_counted(exclude),
            *_counted(self.builtin_exclude),
            *_counted(DOCUMENT_SUFFIXES),
        )


def _counted(values: tuple[str, ...]) -> list[bytes]:
    """The number of ``values``, then each of them: fields of a digest that a list of another
    length cannot run into."""
    return [str(len(values)).encode(), *(value.encode() for value in values)]
