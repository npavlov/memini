"""Which files of a tracked ref are indexed: the source's globs, built-in exclusions, type and size.

A file of a tree is indexed when it is a regular file (mode ``100644`` or ``100755``: not a
symlink, not a submodule), its path can be stored (UTF-8), the source's ``include`` and ``exclude``
globs select it (:meth:`memini.sources.GitConfig.selects`), no built-in exclusion matches it
(:data:`BUILTIN_EXCLUDE`), it is at most ``git_max_blob_size`` bytes, and it is not binary (a NUL
byte in its first 8000 bytes, the chunker's rule). Every other file is skipped, for one
:data:`SkipReason`; a skipped file that had a record becomes a tombstone.

The built-in exclusions are generated files that are large, change with every dependency update and
answer no question about how something is implemented: lock files, minified bundles, source maps,
installed packages. They are few on purpose; vendored code, generated code and fixtures are left to
the source's own ``exclude``. They cannot be switched off yet.

:attr:`FileRules.digest` names the rules; the pin of a stream stores it, and a sync whose rules
differ from the pinned ones lists the ref in full instead of diffing it, so a changed glob or size
limit adds and removes files that no commit touched.
"""

from dataclasses import dataclass
from typing import Final, Literal

from memini.ids import hash_fields
from memini.sources import GitConfig, path_matches

__all__ = [
    "BUILTIN_EXCLUDE",
    "FILE_MODES",
    "RULES_DOMAIN",
    "FileRules",
    "SkipReason",
    "storable_path",
]

RULES_DOMAIN: Final = "memini.gitsync.rules.v1"
"""Domain tag of :attr:`FileRules.digest`. A change of what the digest covers takes a new one."""

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
"""Path globs (:func:`memini.sources.path_matches`) that are never indexed, in any source."""

FILE_MODES: Final = frozenset({"100644", "100755"})
"""Tree entry modes of regular files: a plain and an executable file."""

SkipReason = Literal["bad_path", "not_a_file", "excluded", "too_large", "binary"]
"""Why a file is not indexed: a path that is not UTF-8; a symlink, submodule or directory; left
out by a glob; larger than ``git_max_blob_size``; binary."""


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
    """The rules of one git source: its globs, and the read limit of the mirrors."""

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
            path_matches(pattern, path) for pattern in BUILTIN_EXCLUDE
        ):
            return "excluded"
        if size > self.max_size:
            return "too_large"
        return None

    @property
    def digest(self) -> str:
        """A digest of everything that decides which files are indexed, besides their content."""
        include, exclude = self.config.include, self.config.exclude
        return hash_fields(
            RULES_DOMAIN,
            str(self.max_size).encode(),
            str(len(include)).encode(),
            *(pattern.encode() for pattern in include),
            str(len(exclude)).encode(),
            *(pattern.encode() for pattern in exclude),
            *(pattern.encode() for pattern in BUILTIN_EXCLUDE),
        )
