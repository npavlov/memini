"""What the chunkers share: checks of their input, decoding, the rows of a file and line windows.

Both the code chunker (:mod:`memini.chunking.chunker`) and the Markdown chunker
(:mod:`memini.chunking.markdown`) cut a file into spans of whole rows, measure them in rows and
characters (:class:`~memini.chunking.ChunkSizes`), and cut a span that is too large into line
windows (:func:`windows`) the same way.
"""

import re
from typing import Final

from memini.chunking.model import ChunkSizes

__all__ = [
    "BINARY_PROBE",
    "Lines",
    "Span",
    "check_path",
    "check_record_id",
    "decode_text",
    "fits",
    "is_binary",
    "windows",
]

BINARY_PROBE: Final = 8000
"""A NUL byte among the first this many bytes makes a file binary, as in git."""

_RECORD_ID: Final = re.compile(r"[0-9a-f]{64}")

type Span = tuple[int, int, tuple[int, int] | None]
"""First and last row of a span, and the byte range of a piece of a row that is too long (else
None)."""


def check_record_id(record_id: str) -> None:
    """Refuse a record id that is not 64 lowercase hex characters."""
    if _RECORD_ID.fullmatch(record_id) is None:
        raise ValueError("a record id is 64 lowercase hex characters")


def check_path(path: str) -> None:
    """Refuse a path that cannot be stored as text: not valid Unicode, or with a NUL character
    (git paths never have one)."""
    try:
        path.encode()
    except UnicodeEncodeError:
        raise ValueError("the path is not valid Unicode") from None
    if "\0" in path:
        raise ValueError("the path holds a NUL character")


def is_binary(content: bytes) -> bool:
    """Whether the file is binary: a NUL byte among its first :data:`BINARY_PROBE` bytes."""
    return b"\0" in content[:BINARY_PROBE]


def decode_text(data: bytes) -> str:
    """``data`` as text Postgres can store: UTF-8, with every invalid byte and every NUL byte
    replaced by U+FFFD. One character per NUL, so character counts do not change."""
    return data.decode("utf-8", "replace").replace("\0", "\N{REPLACEMENT CHARACTER}")


class Lines:
    """The rows of a file: byte offsets, decoded text, character counts. Rows are 0-based."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        raw = content.split(b"\n")
        self.text = [decode_text(row) for row in raw]
        self._starts: list[int] = []
        self._chars = [0]  # characters before each row, newlines included
        offset = 0
        for row, text in zip(raw, self.text, strict=True):
            self._starts.append(offset)
            offset += len(row) + 1
            self._chars.append(self._chars[-1] + len(text) + 1)
        self._lengths = [len(row) for row in raw]
        nonblank = [i for i, text in enumerate(self.text) if text.strip()]
        self.first_nonblank = nonblank[0] if nonblank else 0
        self.last_nonblank = nonblank[-1] if nonblank else -1

    def __len__(self) -> int:
        return len(self.text)

    def start(self, row: int) -> int:
        return self._starts[row]

    def end(self, row: int) -> int:
        """Offset of the end of ``row``, before its newline."""
        return self._starts[row] + self._lengths[row]

    def chars(self, first: int, last: int) -> int:
        """Characters of rows ``first`` to ``last``, with the newlines between them."""
        return self._chars[last + 1] - self._chars[first] - 1

    def blank(self, row: int) -> bool:
        return not self.text[row].strip()

    def slice(self, first: int, last: int, byte_range: tuple[int, int] | None) -> str:
        if byte_range is not None:
            return decode_text(self.content[byte_range[0] : byte_range[1]])
        return "\n".join(self.text[first : last + 1])


def fits(
    lines: Lines, sizes: ChunkSizes, first: int, last: int, max_lines: int | None = None
) -> bool:
    """Whether rows ``first`` to ``last`` span at most ``max_lines`` (by default
    :attr:`~memini.chunking.ChunkSizes.max_lines`) rows and ``max_chars`` characters."""
    limit = sizes.max_lines if max_lines is None else max_lines
    return last - first + 1 <= limit and lines.chars(first, last) <= sizes.max_chars


def windows(lines: Lines, sizes: ChunkSizes, first: int, last: int) -> list[Span]:
    """Rows ``first`` to ``last`` as one span if they fit, else as overlapping line windows.

    Windows of ``window_lines`` rows and at most ``max_chars`` characters; each starts
    ``window_overlap`` rows (at most half of the window before) before the end of the one before.
    A row longer than ``max_chars`` is cut into pieces; a window of blank rows only is left out.
    """
    if fits(lines, sizes, first, last):
        return [(first, last, None)]
    spans: list[Span] = []
    row, covered = first, first - 1
    while row <= last:
        if lines.chars(row, row) > sizes.max_chars:
            spans += [(row, row, piece) for piece in _line_pieces(lines, sizes, row)]
            covered, row = row, row + 1
            continue
        end = row
        while (
            end < last
            and end + 1 - row < sizes.window_lines
            and lines.chars(row, end + 1) <= sizes.max_chars
        ):
            end += 1
        if end <= covered:  # the overlap adds nothing: a line too long follows
            row = covered + 1
            continue
        if not all(lines.blank(r) for r in range(row, end + 1)):
            spans.append((row, end, None))
        covered = end
        if end >= last:
            break
        row = end + 1 - min(sizes.window_overlap, (end - row + 1) // 2)
    return spans


def _line_pieces(lines: Lines, sizes: ChunkSizes, row: int) -> list[tuple[int, int]]:
    """Byte ranges of at most ``max_chars`` bytes that cut ``row`` at character boundaries."""
    content, limit = lines.content, sizes.max_chars
    at, end = lines.start(row), lines.end(row)
    pieces: list[tuple[int, int]] = []
    while at < end:
        cut = min(at + limit, end)
        while at < cut < end and content[cut] & 0xC0 == 0x80:  # a UTF-8 continuation byte
            cut -= 1
        if cut == at:
            cut = min(at + limit, end)
        pieces.append((at, cut))
        at = cut
    return pieces
