"""The Markdown chunker: one document in, a chunk per section out. The rules are in
:mod:`memini.chunking` (Documents)."""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final

from memini.chunking.chunker import Key, chunk_id
from memini.chunking.model import DEFAULT_SIZES, Chunk, ChunkSizes, FileChunks
from memini.chunking.text import (
    Lines,
    Span,
    check_path,
    check_record_id,
    fits,
    is_binary,
    windows,
)

__all__ = ["chunk_markdown", "markdown_chunks"]

_FENCE: Final = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
"""The opening line of a fenced code block: its fence, then the info string."""
_ATX: Final = re.compile(r" {0,3}(#{1,6})(?:[ \t](.*))?")
"""An ATX heading: one to six ``#``, then a space or the end of the line."""
_ATX_CLOSE: Final = re.compile(r"(?:^|[ \t])#+$")
"""The optional closing sequence of an ATX heading."""
_SETEXT: Final = re.compile(r" {0,3}(?:=+|-+)")
"""The underline of a setext heading: ``=`` for level 1, ``-`` for level 2."""
_THEMATIC: Final = re.compile(r" {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})")
"""A thematic break: it ends a paragraph."""
_COMMENT: Final = re.compile(r" {0,3}<!--")
"""The start of an HTML comment: no line in it is a heading, up to the line with ``-->``."""
_NOT_UNDERLINED: Final = re.compile(
    r" {0,3}(?:[-+*](?:[ \t]|$)|\d{1,9}[.)](?:[ \t]|$)|>|<[A-Za-z/!?])"
)
"""The first line of a paragraph that an underline does not make a heading of: a list item, a block
quote or HTML. The underline then ends the list or quote instead."""
_FRONT_MATTER: Final = {"---": ("---", "..."), "+++": ("+++",)}
"""The first line of front matter (YAML, TOML) and the lines that may close it."""


def chunk_markdown(
    record_id: str, path: str, content: bytes, *, sizes: ChunkSizes = DEFAULT_SIZES
) -> FileChunks:
    """The chunks of the Markdown document at ``path`` with ``content``: one per section.

    ``record_id`` is the id of the document's record (:func:`memini.ids.record_id`); with each
    chunk's key it makes the chunk id. ``path`` goes into chunk headers only. A document has no
    symbols and no language.

    Raises ``ValueError`` for a record id that is not 64 lowercase hex characters, or a path that
    is not valid Unicode or holds a NUL character.
    """
    check_record_id(record_id)
    check_path(path)
    if is_binary(content):
        return FileChunks(None, (), (), fallback="binary", syntax_errors=False)
    chunks = markdown_chunks(record_id, path, content, sizes=sizes)
    return FileChunks(None, tuple(chunk for chunk, _ in chunks), (), None, False)


def markdown_chunks(
    record_id: str,
    label: str,
    content: bytes,
    *,
    sizes: ChunkSizes = DEFAULT_SIZES,
    key_prefix: Key = (),
) -> list[tuple[Chunk, str]]:
    """The chunks of a Markdown text as :func:`chunk_markdown` cuts them, each with the trail of
    its first section (``Install > Docker``; empty for the text before the first heading).

    For the chunkers of records that hold Markdown in a field (:mod:`memini.chunking.payloads`):
    ``label`` stands where a path stands in the headers, and ``key_prefix`` goes before each
    chunk's key in its id, so that two Markdown texts of one record cut apart get distinct ids.
    Checks neither its arguments nor whether ``content`` is binary.
    """
    lines = Lines(content)
    headings, opaque = _scan(lines)
    builder = _Builder(lines, sizes, opaque)
    drafts = builder.group([d for s in _sections(lines, headings) for d in builder.drafts(s)])
    chunks = _assemble(record_id, label, lines, drafts, key_prefix)
    return [(chunk, draft.sections[0].name) for chunk, draft in zip(chunks, drafts, strict=True)]


# --- Headings -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Heading:
    level: int
    title: str
    row: int
    """The heading line; for a setext heading, the first line of its text."""


def _scan(lines: Lines) -> tuple[list[_Heading], dict[int, int]]:
    """The headings of the document, and its opaque blocks: front matter, fenced code and HTML
    comments, in which no line is a heading. An opaque block is given as its first row mapped to
    its last; a fence or a comment that is not closed runs to the end of the document."""
    rows = [text.rstrip() for text in lines.text]
    rows[0] = rows[0].lstrip("\N{ZERO WIDTH NO-BREAK SPACE}")  # a byte order mark
    headings: list[_Heading] = []
    opaque: dict[int, int] = {}
    count = len(rows)
    row = _front_matter_end(rows)
    if row:
        opaque[0] = row - 1
    paragraph: int | None = None  # the first row of the paragraph being read
    while row < count:
        line = rows[row]
        if not line.strip():
            paragraph, row = None, row + 1
            continue
        fence = _FENCE.fullmatch(line)
        if fence and not (fence[1][0] == "`" and "`" in fence[2]):
            char, length = fence[1][0], len(fence[1])
            end = next((r for r in range(row + 1, count) if _closes(rows[r], char, length)), None)
            opaque[row] = count - 1 if end is None else end
            paragraph, row = None, opaque[row] + 1
            continue
        if _COMMENT.match(line):
            opening = line[line.index("<!--") + 4 :]
            end = row if "-->" in opening else None
            if end is None:
                end = next((r for r in range(row + 1, count) if "-->" in rows[r]), count - 1)
            opaque[row] = end
            paragraph, row = None, end + 1
            continue
        atx = _ATX.fullmatch(line)
        if atx:
            title = _ATX_CLOSE.sub("", (atx[2] or "").strip()).strip()
            headings.append(_Heading(len(atx[1]), title, row))
            paragraph, row = None, row + 1
            continue
        if (
            paragraph is not None
            and _SETEXT.fullmatch(line)
            and _NOT_UNDERLINED.match(rows[paragraph]) is None
            and not _indented(rows[paragraph])
        ):
            title = " ".join(text.strip() for text in rows[paragraph:row])
            headings.append(_Heading(1 if line.strip()[0] == "=" else 2, title, paragraph))
            paragraph, row = None, row + 1
            continue
        if _THEMATIC.fullmatch(line):
            paragraph, row = None, row + 1
            continue
        if paragraph is None:
            paragraph = row
        row += 1
    return headings, opaque


def _front_matter_end(rows: list[str]) -> int:
    """The first row after the front matter, or 0 if the document has none."""
    closers = _FRONT_MATTER.get(rows[0])
    if closers is None:
        return 0
    return next((r + 1 for r in range(1, len(rows)) if rows[r] in closers), 0)


def _closes(line: str, char: str, length: int) -> bool:
    """Whether ``line`` closes a fence of ``length`` times ``char``."""
    body = line.lstrip(" ")
    return len(line) - len(body) <= 3 and len(body) >= length and body == char * len(body)


def _indented(line: str) -> bool:
    """Whether ``line`` is indented as code: four columns or more."""
    indent = line[: len(line) - len(line.lstrip(" \t"))]
    return "\t" in indent or len(indent) > 3


# --- Sections and drafts ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Section:
    """A heading and the text up to the next heading, or the text before the first heading."""

    key: Key
    trail: tuple[str, ...]
    """The titles of the enclosing headings and of its own, outermost first; empty before the
    first heading."""
    first_row: int
    last_row: int

    @property
    def name(self) -> str:
        """The trail for a header: ``Install > Docker``; empty titles are left out."""
        return " > ".join(title for title in self.trail if title)

    def contains(self, other: "_Section") -> bool:
        """Whether ``other`` is inside the heading that encloses this section (or anywhere, for
        the text before the first heading): a group of sections does not leave it."""
        container = self.trail[:-1]
        return other.trail[: len(container)] == container

    def encloses(self, other: "_Section") -> bool:
        """Whether ``other`` is a subsection of this one: under its heading."""
        return bool(self.trail) and other.trail[: len(self.trail)] == self.trail != other.trail


_PREAMBLE: Final[Key] = ("preamble",)


def _sections(lines: Lines, headings: list[_Heading]) -> list[_Section]:
    """The sections of the document in order, each trimmed of blank rows at its ends."""
    starts = [heading.row for heading in headings] + [len(lines)]
    sections: list[_Section] = []
    span = _trim(lines, 0, starts[0] - 1)
    if span is not None:
        sections.append(_Section(_PREAMBLE, (), *span))
    stack: list[tuple[int, str]] = []
    ordinals: dict[tuple[str, ...], int] = {}
    for heading, end in zip(headings, starts[1:], strict=True):
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        stack.append((heading.level, heading.title))
        trail = tuple(title for _, title in stack)
        ordinal = ordinals.get(trail, 0)
        ordinals[trail] = ordinal + 1
        span = _trim(lines, heading.row, end - 1)
        assert span is not None  # noqa: S101 -- a heading row is never blank
        sections.append(_Section(("section", "\n".join(trail), str(ordinal)), trail, *span))
    return sections


def _trim(lines: Lines, first: int, last: int) -> tuple[int, int] | None:
    while first <= last and lines.blank(first):
        first += 1
    while first <= last and lines.blank(last):
        last -= 1
    return (first, last) if first <= last else None


@dataclass(slots=True)
class _Draft:
    """A chunk before it has an id and a text: one section, a window of one, or a group."""

    first_row: int
    last_row: int
    key: Key
    sections: list[_Section] = field(default_factory=list[_Section])
    small: bool = False
    whole: bool = True
    """It holds whole sections, not a window of one."""
    byte_range: tuple[int, int] | None = None


class _Builder:
    def __init__(self, lines: Lines, sizes: ChunkSizes, opaque: dict[int, int]) -> None:
        self.lines = lines
        self.sizes = sizes
        self.opaque = opaque

    def drafts(self, section: _Section) -> list[_Draft]:
        """The section as one draft if it fits, else as windows of whole blocks."""
        first, last = section.first_row, section.last_row
        if fits(self.lines, self.sizes, first, last):
            small = last - first + 1 <= self.sizes.small_lines
            return [_Draft(first, last, section.key, [section], small)]
        drafts: list[_Draft] = []
        for part, (span_first, span_last, byte_range) in enumerate(self._windows(first, last)):
            key = (*section.key, str(part)) if part else section.key
            drafts.append(
                _Draft(span_first, span_last, key, [section], whole=False, byte_range=byte_range)
            )
        return drafts

    def _windows(self, first: int, last: int) -> list[Span]:
        """Rows ``first`` to ``last`` packed into windows of whole blocks, each at most
        ``window_lines`` rows and ``max_chars`` characters. A block larger than that is cut
        into line windows (:func:`memini.chunking.text.windows`)."""
        spans: list[Span] = []
        current: tuple[int, int] | None = None
        for block_first, block_last in self._blocks(first, last):
            window_lines = self.sizes.window_lines
            if current is not None:
                if fits(self.lines, self.sizes, current[0], block_last, window_lines):
                    current = (current[0], block_last)
                    continue
                spans.append((*current, None))
                current = None
            if fits(self.lines, self.sizes, block_first, block_last, window_lines):
                current = (block_first, block_last)
            else:
                spans += windows(self.lines, self.sizes, block_first, block_last)
        if current is not None:
            spans.append((*current, None))
        return spans

    def _blocks(self, first: int, last: int) -> list[tuple[int, int]]:
        """Runs of non-blank rows from ``first`` to ``last``. An opaque block (a fence, a
        comment, front matter) is never cut, blank rows inside it included."""
        blocks: list[tuple[int, int]] = []
        row = first
        while row <= last:
            if self.lines.blank(row):
                row += 1
                continue
            start = row
            while row <= last and not self.lines.blank(row):
                row = min(self.opaque.get(row, row), last) + 1
            blocks.append((start, row - 1))
        return blocks

    def group(self, drafts: list[_Draft]) -> list[_Draft]:
        """Merge runs of small neighbours, and a small section with its first subsection; see
        :meth:`_joins`."""
        merged: list[_Draft] = []
        for draft in drafts:
            last = merged[-1] if merged else None
            if last is not None and self._joins(last, draft):
                last.last_row = draft.last_row
                last.sections += draft.sections
                last.small = last.small and draft.small
            else:
                merged.append(draft)
        return merged

    def _joins(self, last: _Draft, draft: _Draft) -> bool:
        """Whether ``draft`` joins ``last``, the draft before it.

        Small neighbours merge while the merged chunk spans at most ``merge_lines`` and stays
        inside the heading that encloses its first section. A small section that is not merged
        yet goes with its first subsection when the two fit ``max_lines`` and ``max_chars``, like
        the head of a class with its first member: a heading with no text of its own is never a
        chunk alone.
        """
        if not (last.small and last.whole and draft.whole):
            return False
        first = last.sections[0]
        if draft.small and first.contains(draft.sections[0]):
            merge_lines = self.sizes.merge_lines
            if fits(self.lines, self.sizes, last.first_row, draft.last_row, merge_lines):
                return True
        return (
            len(last.sections) == 1
            and first.encloses(draft.sections[0])
            and fits(self.lines, self.sizes, last.first_row, draft.last_row)
        )


# --- Chunks -------------------------------------------------------------------------------------


def _label(key: Key) -> str:
    """The readable form of a key: ``Install > Docker#0``, its second window
    ``Install > Docker#0@1``; ``(preamble)`` for the text before the first heading."""
    if key[0] == "section":
        _, trail, ordinal, *window = key
        label = f"{trail.replace(chr(10), ' > ')}#{ordinal}"
    else:
        window = list(key[1:])
        label = "(preamble)"
    return label + "".join(f"@{part}" for part in window)


def _assemble(
    record_id: str, path: str, lines: Lines, drafts: list[_Draft], key_prefix: Key
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for draft in drafts:
        if draft.byte_range is not None:
            start, end = draft.byte_range
        else:
            start, end = lines.start(draft.first_row), lines.end(draft.last_row)
        names = list(dict.fromkeys(section.name for section in draft.sections if section.name))
        header = f"{path}: {', '.join(names)}" if names else path
        content = lines.slice(draft.first_row, draft.last_row, draft.byte_range)
        chunks.append(
            Chunk(
                id=chunk_id(record_id, (*key_prefix, *draft.key)),
                key=_label(draft.key),
                kind="group" if len(draft.sections) > 1 else "section",
                symbol=None,
                language=None,
                start_line=draft.first_row + 1,
                end_line=draft.last_row + 1,
                start_byte=start,
                end_byte=end,
                header=header,
                content=content,
                content_hash=hashlib.sha256(f"{header}\n{content}".encode()).hexdigest(),
            )
        )
    if len({chunk.id for chunk in chunks}) != len(chunks):
        raise RuntimeError(f"two chunks of {path!r} have the same key")  # a bug in the rules
    return tuple(chunks)
