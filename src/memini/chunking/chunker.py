"""The chunker: one file in, chunks and symbols out. The rules are in :mod:`memini.chunking`."""

import functools
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata

import tree_sitter
from tree_sitter import Node

from memini.chunking.languages import (
    GRAMMAR_PACKAGES,
    Level,
    Spec,
    detect_language,
    grammar,
    spec,
)
from memini.chunking.model import (
    CHUNK_ID_DOMAIN,
    CHUNKER_VERSION,
    DEFAULT_SIZES,
    LANGUAGES,
    MARKDOWN_CHUNKER_VERSION,
    PAYLOAD_CHUNKER_VERSION,
    Chunk,
    ChunkKind,
    ChunkSizes,
    Fallback,
    FileChunks,
    Language,
    Symbol,
    SymbolKind,
)
from memini.chunking.text import (
    Lines,
    Span,
    check_path,
    check_record_id,
    fits,
    is_binary,
    windows,
)
from memini.ids import hash_fields

__all__ = ["chunk_file", "chunk_id", "chunker_fingerprint"]

Key = tuple[str, ...]
"""The identity of a chunk inside its file; see :mod:`memini.chunking`."""


def chunk_id(record_id: str, key: Key) -> str:
    """The id of the chunk with ``key`` in the record ``record_id``.

    SHA-256 over the domain ``memini.chunk.v1``, the record id (64 hex characters, as ASCII) and
    each field of the key in UTF-8, every field length-prefixed (:func:`memini.ids.hash_fields`).
    """
    return hash_fields(CHUNK_ID_DOMAIN, record_id.encode(), *(part.encode() for part in key))


def chunker_fingerprint() -> str:
    """:data:`CHUNKER_VERSION`, :data:`MARKDOWN_CHUNKER_VERSION`,
    :data:`PAYLOAD_CHUNKER_VERSION` and the versions of tree-sitter and of every grammar package.

    The chunkers' output depends on all of them, so the index fingerprint should include this.
    """
    versions = " ".join(f"{name}/{metadata.version(name)}" for name in GRAMMAR_PACKAGES)
    return (
        f"memini-chunker/{CHUNKER_VERSION} markdown/{MARKDOWN_CHUNKER_VERSION}"
        f" payload/{PAYLOAD_CHUNKER_VERSION} {versions}"
    )


def chunk_file(
    record_id: str,
    path: str,
    content: bytes,
    *,
    language: Language | None = None,
    sizes: ChunkSizes = DEFAULT_SIZES,
) -> FileChunks:
    """Chunks and symbols of the file at ``path`` (relative to the repository) with ``content``.

    ``record_id`` is the id of the file's record (:func:`memini.ids.record_id`); with each chunk's
    key it makes the chunk id. ``path`` goes into chunk headers and symbols only. ``language``
    overrides the detection by file extension.

    Safe to call from several threads at once: each call has its own parser.

    Raises ``ValueError`` for a record id that is not 64 lowercase hex characters, a path that is
    not valid Unicode or holds a NUL character, or an unknown language.
    """
    check_record_id(record_id)
    check_path(path)
    if language is not None and language not in LANGUAGES:
        raise ValueError(f"unknown language {language!r}; expected one of {list(LANGUAGES)}")
    language = language or detect_language(path)
    if is_binary(content):
        return FileChunks(language, (), (), fallback="binary", syntax_errors=False)

    lines = Lines(content)
    fallback: Fallback | None = None
    if language is None:
        fallback = "unknown_language"
    elif len(content) > sizes.max_parse_bytes:
        fallback = "too_large"

    builder = _Builder(lines, sizes, None if language is None else spec(language))
    syntax_errors = False
    if language is None or fallback is not None:
        drafts = builder.whole_file("window")
    else:
        tree = tree_sitter.Parser(grammar(language, path)).parse(content)
        syntax_errors = tree.root_node.has_error
        drafts = builder.file(tree.root_node)
    return _assemble(record_id, path, language, fallback, syntax_errors, lines, builder, drafts)


def _rows(node: Node) -> tuple[int, int]:
    """First and last row of ``node``; a node that ends at a line start ends on the row before."""
    first, (last, column) = node.start_point.row, node.end_point
    if column == 0 and last > first:
        last -= 1
    return first, last


@dataclass(slots=True)
class _Sym:
    name: str
    qualified: str
    kind: SymbolKind
    parent: str | None
    ordinal: int
    first_row: int
    last_row: int
    start_byte: int
    end_byte: int

    def key(self, *rest: str) -> Key:
        return ("def", self.qualified, str(self.ordinal), *rest)


@dataclass(slots=True)
class _Unit:
    """A member of a file or class body, with the comments and decorators that belong to it."""

    first_row: int
    last_row: int
    syms: list[_Sym] = field(default_factory=list[_Sym])
    """Definitions made by the member itself; the first names its chunk."""
    members: "list[_Unit] | None" = None
    """Members of the body when the member is a container that is split (``syms[0]``)."""
    names: list[str] = field(default_factory=list[str])
    """Definitions in a statement that holds members (Ruby ``class << self``), for the header."""


@dataclass(slots=True)
class _Draft:
    """A chunk before it has an id and a text."""

    first_row: int
    last_row: int
    key: Key
    kind: ChunkKind
    symbol: str | None
    names: list[str]
    small: bool = False
    byte_range: tuple[int, int] | None = None
    """For a piece of a line longer than ``max_chars``."""


@dataclass(slots=True)
class _Anchor:
    """Keys of statement chunks: counted from the definition before them, or from the start of
    their container (``lead``)."""

    base: Key
    count: int = 0

    def next(self) -> Key:
        self.count += 1
        return (*self.base, str(self.count - 1))

    def after(self, sym: _Sym) -> None:
        self.base, self.count = ("after", sym.qualified, str(sym.ordinal)), 0


class _Builder:
    def __init__(self, lines: Lines, sizes: ChunkSizes, language: Spec | None) -> None:
        self.lines = lines
        self.sizes = sizes
        self.spec = language
        self.symbols: list[_Sym] = []
        self._ordinals: dict[str, int] = {}

    # Sizes -------------------------------------------------------------------------------------

    def _fits(self, first: int, last: int, max_lines: int | None = None) -> bool:
        return fits(self.lines, self.sizes, first, last, max_lines)

    def _windows(self, first: int, last: int) -> list[Span]:
        """Rows ``first`` to ``last`` as one span if they fit, else as overlapping windows."""
        return windows(self.lines, self.sizes, first, last)

    # Symbols -----------------------------------------------------------------------------------

    def _spec(self) -> Spec:
        assert self.spec is not None  # noqa: S101 -- only parsed files have symbols
        return self.spec

    def _register(
        self, found_name: tuple[str, ...], kind: SymbolKind, span: Node, scope: str | None
    ) -> _Sym:
        parts = [scope, *found_name] if scope else list(found_name)
        qualified = ".".join(parts)
        ordinal = self._ordinals.get(qualified, 0)
        self._ordinals[qualified] = ordinal + 1
        start = span
        decorators = self._spec().decorators
        if decorators:  # decorators standing before a class member belong to it
            sibling = span.prev_named_sibling
            while sibling is not None and sibling.type in decorators | self._spec().comments:
                if sibling.type in decorators:
                    start = sibling
                sibling = sibling.prev_named_sibling
        sym = _Sym(
            name=found_name[-1],
            qualified=qualified,
            kind=kind,
            parent=".".join(parts[:-1]) or None,
            ordinal=ordinal,
            first_row=start.start_point.row,
            last_row=_rows(span)[1],
            start_byte=start.start_byte,
            end_byte=span.end_byte,
        )
        self.symbols.append(sym)
        return sym

    def _big(self, first: int, last: int) -> bool:
        return not self._fits(first, last, self.sizes.merge_lines)

    def _find(self, nodes: list[Node], scope: str | None, level: Level) -> None:
        """Register every definition in ``nodes`` and below them, in document order."""
        language = self._spec()
        skip = language.comments | language.decorators
        stack: list[tuple[Node, str | None, Level]] = [
            (node, scope, level) for node in reversed(nodes)
        ]
        while stack:
            node, node_scope, node_level = stack.pop()
            if node.type in skip:
                continue
            found = language.classify(node, node_level)
            if found:
                bodies: list[tuple[list[Node], str, Level]] = []
                for definition in found:
                    sym = self._register(
                        definition.name_path, definition.kind, definition.span, node_scope
                    )
                    if definition.body is not None:
                        bodies.append(
                            (definition.body.named_children, sym.qualified, definition.body_level)
                        )
                for children, body_scope, body_level in reversed(bodies):
                    stack.extend((child, body_scope, body_level) for child in reversed(children))
                continue
            body = language.anonymous_container(node)
            child_level: Level = "local" if body is None else "class"
            children = (node if body is None else body).named_children
            stack.extend((child, node_scope, child_level) for child in reversed(children))

    # Units -------------------------------------------------------------------------------------

    def _members(self, children: list[Node], scope: str | None, level: Level) -> list[_Unit]:
        """The members of a file or of a container that is split, in order.

        A comment block with no blank line before the next member, and any decorator before it,
        belong to that member. Other comment blocks are members of their own. Comments after the
        last member are not members: they fall to the end of the container. A node that starts
        on the row where the previous member ends joins that member.
        """
        language = self._spec()
        units: list[_Unit] = []
        pending: list[Node] = []
        for child in children:
            if child.start_byte == child.end_byte:
                continue  # a token the parser inserted to recover from an error
            first, last = _rows(child)
            loose = child.type in language.comments or child.type in language.decorators
            if units and not pending and first <= units[-1].last_row:
                previous = units[-1]
                if not loose:
                    for unit in self._unit(child, scope, level):
                        previous.syms += unit.syms
                        previous.names += unit.names
                    previous.members = None
                previous.last_row = max(previous.last_row, last)
                continue
            if loose:
                pending.append(child)
                continue
            new = self._unit(child, scope, level)
            units += self._attach(pending, new[0])
            units += new
            pending = []
        return units

    def _attach(self, pending: list[Node], unit: _Unit) -> list[_Unit]:
        """Attach the comments and decorators before ``unit`` that belong to it; the rest become
        comment members, a block of adjacent comments each."""
        decorators = self._spec().decorators
        cut = next((i for i, node in enumerate(pending) if node.type in decorators), len(pending))
        start = _rows(pending[cut])[0] if cut < len(pending) else unit.first_row
        while cut > 0 and _rows(pending[cut - 1])[1] + 1 >= start:
            cut -= 1
            start = _rows(pending[cut])[0]
        unit.first_row = min(unit.first_row, start)
        loose: list[_Unit] = []
        for node in pending[:cut]:
            first, last = _rows(node)
            if loose and first <= loose[-1].last_row + 1:
                loose[-1].last_row = max(loose[-1].last_row, last)
            else:
                loose.append(_Unit(first, last))
        return loose

    def _unit(self, child: Node, scope: str | None, level: Level) -> list[_Unit]:
        """``child`` as a member: one unit, or the members of a large anonymous container."""
        language = self._spec()
        first, last = _rows(child)
        found = language.classify(child, level)
        syms = [self._register(f.name_path, f.kind, f.span, scope) for f in found]
        unit = _Unit(first, last, syms)
        for definition, sym in zip(found, syms, strict=True):
            if definition.body is None:
                continue
            children = definition.body.named_children
            if definition.container and len(found) == 1 and self._big(sym.first_row, sym.last_row):
                unit.members = self._members(children, sym.qualified, definition.body_level) or None
            else:
                self._find(children, sym.qualified, definition.body_level)
        if found:
            return [unit]
        body = language.anonymous_container(child)
        if body is None:
            self._find(child.named_children, scope, "local")
            return [unit]
        if not self._big(first, last):
            before = len(self.symbols)
            self._find(body.named_children, scope, "class")
            unit.names = [s.qualified for s in self.symbols[before:] if s.parent == scope]
            return [unit]
        members = self._members(body.named_children, scope, "class")
        if not members:
            return [unit]
        # Spliced into the enclosing body: the opening line goes with the first member, the
        # closing line with the last.
        members[0].first_row = first
        members[-1].last_row = max(members[-1].last_row, last)
        return members

    # Drafts ------------------------------------------------------------------------------------

    def file(self, root: Node) -> list[_Draft]:
        """Drafts of a parsed file."""
        units = self._members(root.named_children, None, "top")
        if not units:
            return self.whole_file("code")
        anchor = _Anchor(("lead", "", ""))
        statements = functools.partial(self._leaf, anchor=anchor)
        return self._wrap(
            self._drafts(units, None, anchor),
            (self.lines.first_nonblank, units[0].first_row - 1),
            (units[-1].last_row + 1, self.lines.last_nonblank),
            head_drafts=statements,
            tail_drafts=statements,
            head_names=[],
        )

    def whole_file(self, kind: ChunkKind) -> list[_Draft]:
        """Drafts of a file cut into line windows only."""
        if self.lines.last_nonblank < 0:
            return []
        first, last = self.lines.first_nonblank, self.lines.last_nonblank
        return self._leaf(first, last, anchor=_Anchor(("lead", "", "")), kind=kind)

    def _leaf(
        self,
        first: int,
        last: int,
        *,
        sym: _Sym | None = None,
        names: list[str] | None = None,
        scope: str | None = None,
        anchor: _Anchor | None = None,
        kind: ChunkKind = "code",
    ) -> list[_Draft]:
        """Rows ``first`` to ``last`` as one draft, or as windows if they do not fit.

        With ``sym`` the drafts are that definition, keyed by it; without, they are statements in
        ``scope``, keyed from ``anchor``. The first draft is named ``names`` (by default, ``sym``).
        """
        spans = self._windows(first, last)
        single = len(spans) == 1 and spans[0][2] is None
        drafts: list[_Draft] = []
        for part, (span_first, span_last, byte_range) in enumerate(spans):
            if sym is not None:
                key = sym.key(str(part)) if part else sym.key()
                label = (names if names is not None else [sym.qualified]) if part == 0 else []
                draft = _Draft(span_first, span_last, key, sym.kind, sym.qualified, list(label))
            else:
                assert anchor is not None  # noqa: S101 -- statements always have an anchor
                label = list(names or []) if part == 0 else []
                draft = _Draft(span_first, span_last, anchor.next(), kind, scope, label)
            draft.small = single and span_last - span_first + 1 <= self.sizes.small_lines
            draft.byte_range = byte_range
            drafts.append(draft)
        return drafts

    def _drafts(self, units: list[_Unit], scope: str | None, anchor: _Anchor) -> list[_Draft]:
        """Drafts of the members of one container, small neighbours merged."""
        pieces: list[_Draft] = []
        for unit in units:
            if unit.members is not None:
                pieces += self._split(unit, unit.members)
            elif unit.syms:
                names = [sym.qualified for sym in unit.syms]
                pieces += self._leaf(unit.first_row, unit.last_row, sym=unit.syms[0], names=names)
            else:
                pieces += self._leaf(
                    unit.first_row, unit.last_row, names=unit.names, scope=scope, anchor=anchor
                )
            if unit.syms:
                anchor.after(unit.syms[0])
        return self._group(pieces, scope)

    def _split(self, unit: _Unit, members: list[_Unit]) -> list[_Draft]:
        """Drafts of a container that is split into its members. None of them merges further."""
        container = unit.syms[0]
        inner = _Anchor(("lead", container.qualified, str(container.ordinal)))
        drafts = self._wrap(
            self._drafts(members, container.qualified, inner),
            (unit.first_row, members[0].first_row - 1),
            (members[-1].last_row + 1, unit.last_row),
            head_drafts=functools.partial(self._leaf, sym=container),
            tail_drafts=functools.partial(self._leaf, scope=container.qualified, anchor=inner),
            head_names=[container.qualified],
        )
        for draft in drafts:
            draft.small = False
        return drafts

    def _wrap(
        self,
        drafts: list[_Draft],
        head: tuple[int, int],
        tail: tuple[int, int],
        *,
        head_drafts: Callable[[int, int], list[_Draft]],
        tail_drafts: Callable[[int, int], list[_Draft]],
        head_names: list[str],
    ) -> list[_Draft]:
        """Add the rows before the first member (a class line, say) to the first draft, and the
        rows after the last member (an ``end``) to the last draft, where they fit; else they
        become drafts of their own."""
        first, last = head
        while first <= last and self.lines.blank(last):
            last -= 1
        if first <= last:
            top = drafts[0]
            if top.byte_range is None and self._fits(first, top.last_row):
                top.first_row = first
                top.names = head_names + top.names
            else:
                drafts = head_drafts(first, last) + drafts
        first, last = tail
        while first <= last and self.lines.blank(first):
            first += 1
        if first <= last:
            bottom = drafts[-1]
            if bottom.byte_range is None and self._fits(bottom.first_row, last):
                bottom.last_row = last
            else:
                drafts = drafts + tail_drafts(first, last)
        return drafts

    def _group(self, pieces: list[_Draft], scope: str | None) -> list[_Draft]:
        """Merge runs of small neighbours while the merged chunk spans at most ``merge_lines``."""
        merged: list[_Draft] = []
        for piece in pieces:
            last = merged[-1] if merged else None
            if (
                last is not None
                and last.small
                and piece.small
                and self._fits(last.first_row, piece.last_row, self.sizes.merge_lines)
            ):
                last.last_row = piece.last_row
                last.names += piece.names
                last.kind = "group" if last.names else "code"
                last.symbol = scope
            else:
                merged.append(piece)
        return merged


def _label(key: Key) -> str:
    """The readable form of a key: ``A.b#0``, ``A.b#0@1``, ``A.b#0+2``, ``A#0/+0``, ``/+0``."""
    tag, name, ordinal, *rest = key
    if tag == "def":
        return f"{name}#{ordinal}" + "".join(f"@{part}" for part in rest)
    count = rest[0]
    if tag == "after":
        return f"{name}#{ordinal}+{count}"
    return f"{name}#{ordinal}/+{count}" if name else f"/+{count}"


def _assemble(
    record_id: str,
    path: str,
    language: Language | None,
    fallback: Fallback | None,
    syntax_errors: bool,
    lines: Lines,
    builder: _Builder,
    drafts: list[_Draft],
) -> FileChunks:
    chunks: list[Chunk] = []
    for draft in drafts:
        if draft.byte_range is not None:
            start, end = draft.byte_range
        else:
            start, end = lines.start(draft.first_row), lines.end(draft.last_row)
        names = list(dict.fromkeys(draft.names)) or ([draft.symbol] if draft.symbol else [])
        header = f"{path}: {', '.join(names)}" if names else path
        content = lines.slice(draft.first_row, draft.last_row, draft.byte_range)
        chunks.append(
            Chunk(
                id=chunk_id(record_id, draft.key),
                key=_label(draft.key),
                kind=draft.kind,
                symbol=draft.symbol,
                language=language,
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
    symbols = tuple(
        Symbol(
            name=sym.name,
            qualified_name=sym.qualified,
            kind=sym.kind,
            path=path,
            start_line=sym.first_row + 1,
            end_line=sym.last_row + 1,
            start_byte=sym.start_byte,
            end_byte=sym.end_byte,
            parent=sym.parent,
            ordinal=sym.ordinal,
            chunk_id=_holder(chunks, sym.start_byte),
        )
        for sym in builder.symbols
    )
    return FileChunks(language, tuple(chunks), symbols, fallback, syntax_errors)


def _holder(chunks: list[Chunk], offset: int) -> str:
    """The id of the first chunk that holds byte ``offset``, else of the last one before it."""
    holder = chunks[0]
    for chunk in chunks:
        if chunk.start_byte <= offset < chunk.end_byte:
            return chunk.id
        if chunk.start_byte <= offset:
            holder = chunk
    return holder.id
