"""What the chunker returns: chunks, symbols, and the sizes that shape them."""

from dataclasses import dataclass
from typing import Final, Literal, get_args

__all__ = [
    "CHUNKER_VERSION",
    "CHUNK_ID_DOMAIN",
    "DEFAULT_SIZES",
    "LANGUAGES",
    "Chunk",
    "ChunkKind",
    "ChunkSizes",
    "Fallback",
    "FileChunks",
    "Language",
    "Symbol",
    "SymbolKind",
]

CHUNKER_VERSION: Final = 2
"""Version of the chunking rules. It changes whenever the output for some input can change: chunk
boundaries, ids, texts, headers, symbols or the default sizes. The index fingerprint includes it
(principle 9); :func:`memini.chunking.chunker_fingerprint` adds the grammar versions.

2: a NUL byte past the binary probe becomes U+FFFD in the chunk text, so the content hash is
computed over the text that is stored."""

CHUNK_ID_DOMAIN: Final = "memini.chunk.v1"
"""Domain tag of chunk ids (see :mod:`memini.chunking`). A new rule gets a new tag."""

Language = Literal["python", "javascript", "typescript", "ruby", "go"]
"""Languages parsed with tree-sitter. TSX is ``typescript``, JSX is ``javascript``."""

LANGUAGES: Final[tuple[Language, ...]] = get_args(Language)

SymbolKind = Literal[
    "class",
    "struct",
    "interface",
    "enum",
    "type",
    "module",
    "function",
    "method",
    "constant",
    "variable",
]
"""Kinds of definitions. ``module`` is a Ruby module or a TypeScript namespace; ``struct`` is a
Go struct type; ``type`` is any other type definition or alias."""

ChunkKind = SymbolKind | Literal["group", "code", "window"]
"""A chunk is one definition, or a part of one (the kind of that definition); several small
siblings merged together (``group``); statements outside definitions (``code``); or a line window
of a file that was not parsed (``window``)."""

Fallback = Literal["unknown_language", "too_large", "binary"]
"""Why a file was not parsed: no grammar for its language, larger than
:attr:`ChunkSizes.max_parse_bytes`, or binary (no chunks at all)."""


@dataclass(frozen=True, slots=True)
class ChunkSizes:
    """Size limits, in lines and characters. Characters stand in for tokens: the chunker has no
    tokenizer. The header line of a chunk is not counted.

    - ``max_lines``, ``max_chars``: no chunk is larger. A definition that does not fit is cut
      into line windows.
    - ``merge_lines``: adjacent small siblings merge while the merged chunk spans at most this many
      lines. A class or module of at most this many lines stays one chunk; a larger one is split
      into its members.
    - ``small_lines``: a definition or statement of at most this many lines is small and can merge
      with its small neighbours.
    - ``window_lines``, ``window_overlap``: line windows, for a definition that is too large and
      for a file that is not parsed. Consecutive windows share ``window_overlap`` lines, at most
      half of the window before.
    - ``max_parse_bytes``: a larger file is not parsed and is cut into line windows.
    """

    max_lines: int = 80
    max_chars: int = 3000
    merge_lines: int = 40
    small_lines: int = 6
    window_lines: int = 60
    window_overlap: int = 10
    max_parse_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.small_lines < 1 or self.window_overlap < 0:
            raise ValueError("small_lines must be at least 1 and window_overlap at least 0")
        if not self.small_lines <= self.merge_lines <= self.max_lines:
            raise ValueError("expected small_lines <= merge_lines <= max_lines")
        if not self.window_overlap < self.window_lines <= self.max_lines:
            raise ValueError("expected window_overlap < window_lines <= max_lines")
        if self.max_chars < 100:
            raise ValueError("max_chars must be at least 100")
        if self.max_parse_bytes < 1:
            raise ValueError("max_parse_bytes must be at least 1")


DEFAULT_SIZES: Final = ChunkSizes()


@dataclass(frozen=True, slots=True)
class Chunk:
    """A piece of a file to embed and to search.

    Lines are 1-based and inclusive. Bytes are offsets into the file, ``[start_byte, end_byte)``;
    a chunk starts at the beginning of a line and ends before the newline of its last line, except
    a piece of a line longer than ``max_chars``.
    """

    id: str
    """Deterministic: a hash of the record id and :attr:`key` (see :mod:`memini.chunking`)."""
    key: str
    """The identity of the chunk inside its file, readable: ``Order.total#0``, ``Order.total#0@1``
    (second window of it), ``Order.total#0+0`` (first statements after it), ``Order#0/+0`` (first
    statements inside it, before any member), ``/+0`` (first statements of the file)."""
    kind: ChunkKind
    symbol: str | None
    """Qualified name of the definition this chunk is or is a part of; for a group or for
    statements, the definition that encloses them; None at the top of a file."""
    language: Language | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    header: str
    """One line: the path, then the definitions the chunk holds, or the one it is a part of."""
    content: str
    """The source lines, decoded as UTF-8 with invalid bytes and NUL bytes replaced by U+FFFD:
    Postgres text cannot hold a NUL character."""
    content_hash: str
    """SHA-256 of :attr:`text` in UTF-8, 64 lowercase hex characters."""

    @property
    def text(self) -> str:
        """What is embedded: the header line, a newline, the content."""
        return f"{self.header}\n{self.content}"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A definition: where it is, what it is, what encloses it."""

    name: str
    qualified_name: str
    """Names from the outermost enclosing definition, joined with ``.`` in every language:
    ``Order.total``. A Go method is qualified by its receiver type, ``Point.Area``; a Ruby
    ``class A::B`` gives ``A.B``."""
    kind: SymbolKind
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    parent: str | None
    """Qualified name of the enclosing definition (for a Go method, its receiver type), or None."""
    ordinal: int
    """How many symbols with the same qualified name come before this one in the file: 0 unless
    the name is defined again (overloads, a property setter, a reopened Ruby class)."""
    chunk_id: str
    """The chunk that holds the first line of the symbol."""


@dataclass(frozen=True, slots=True)
class FileChunks:
    """Everything the chunker derives from one file."""

    language: Language | None
    chunks: tuple[Chunk, ...]
    symbols: tuple[Symbol, ...]
    fallback: Fallback | None
    """Why the file was cut into line windows or skipped; None when it was parsed."""
    syntax_errors: bool
    """The parser recovered from syntax errors; the parts it could not place are ``code``."""
