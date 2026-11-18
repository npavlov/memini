"""Chunking: split a source file, a document or a pushed record into chunks to embed and search;
list symbols.

:func:`chunk_file` takes a record id, a repository-relative path and the file's bytes, and returns
a :class:`FileChunks`: the chunks, the symbols, and why the file was not parsed if it was not.
:func:`chunk_markdown` does the same for a Markdown document (see Documents below), and
:func:`chunk_payload` for a thread, a meeting or a document pushed with a payload (see Pushed
records). All are pure: no database, no clock, no randomness; the same input gives the same output
on any machine.

Languages
    Python, JavaScript (with JSX), TypeScript (with TSX), Ruby and Go are parsed with tree-sitter,
    chosen by file extension (:data:`memini.chunking.languages.EXTENSIONS`) or by the ``language``
    argument. Any other file is cut into line windows, and so is a file larger than
    ``max_parse_bytes``. A file with a NUL byte in its first 8000 bytes is binary and gives no
    chunks. Bytes that are not UTF-8, and NUL bytes further on, are decoded with U+FFFD in their
    place, so a chunk's text is what the database stores and its content hash is computed over
    it; offsets are always into the original bytes. tree-sitter recovers from syntax errors; the
    parts it cannot place become ``code`` chunks, and :attr:`FileChunks.syntax_errors` says so.

Chunks
    The members of a file are its top-level statements and definitions. Each definition is one
    chunk: a function, a method, a class, a type, a constant. A comment block with no blank line
    between it and a definition belongs to the definition, and so do decorators. Statements
    outside definitions (imports, module-level code) are ``code`` chunks.

    A class or module larger than ``merge_lines`` (or ``max_chars``) is split into its members,
    which are chunked by the same rules; its own lines before the first member (the class line,
    a docstring that is not a member) go with the first member's chunk and its closing lines with
    the last, when they fit. A smaller class is one chunk.

    Small neighbours (at most ``small_lines`` each) merge into one ``group`` chunk while it spans
    at most ``merge_lines``. A definition or a statement larger than ``max_lines`` or
    ``max_chars`` is cut into windows of ``window_lines`` that overlap by ``window_overlap``; a
    single line longer than ``max_chars`` is cut into pieces. Default sizes: :data:`DEFAULT_SIZES`.

    The text to embed is :attr:`Chunk.text`: a header line, ``<path>: <qualified names>``, then
    the source lines. Line numbers are not in it, so a chunk that only moves keeps its text and
    its content hash.

Chunk ids
    ``id = hash_fields("memini.chunk.v1", record_id, *key)`` (:func:`chunk_id`). The key names the
    chunk by structure, never by line number or content:

    - a definition: ``("def", qualified_name, ordinal)``; its later windows add the window number;
    - statements: counted from the definition before them in the same container,
      ``("after", qualified_name, ordinal, n)``, or from the start of the container,
      ``("lead", container_name, container_ordinal, n)`` (empty name for the file);
    - a group: the key of its first member.

    ``ordinal`` counts earlier definitions with the same qualified name in the file, so overloads,
    a property and its setter, or a reopened Ruby class do not collide. Editing a function changes
    the content hash of its chunk and nothing else, as long as the edit does not move it across a
    size limit; adding a definition changes only the keys of statements counted from its
    neighbour. :attr:`Chunk.key` is the readable form (``Order.total#0``).

Symbols
    Every definition, nested ones included, with its qualified name (``.`` between names in every
    language), kind, span, parent and the chunk that holds its first line. See
    :mod:`memini.chunking.languages` for what counts as a definition in each language.

Documents
    :func:`chunk_markdown` cuts a Markdown document by its headings. A heading is an ATX heading
    (``#`` to ``######``) or a setext heading (a paragraph underlined with ``=`` or ``-``), as
    CommonMark has them at the top level. No line of front matter (``---`` or ``+++`` on the first
    line, up to its closing line), of a fenced code block (up to its closing fence, or the end of
    the document) or of an HTML comment is a heading.

    A section is a heading and the text up to the next heading of any level. Its trail is the
    titles of the headings that enclose it and its own (``Install > Docker``); the text before the
    first heading, front matter included, is a section with an empty trail. Each section is one
    ``section`` chunk. One larger than ``max_lines`` or ``max_chars`` is cut into windows of whole
    blocks (runs of non-blank lines; a fenced block or a comment, blank lines inside it included,
    is one block) of at most ``window_lines`` and ``max_chars``, without overlap; a block larger
    than that is cut into line windows like code. Small neighbours (at most ``small_lines`` each)
    merge into one ``group`` chunk while it spans at most ``merge_lines`` and stays inside the
    heading that encloses its first section. A small section also goes with its first subsection
    when the two fit ``max_lines`` and ``max_chars``, as a class line goes with its first member:
    a heading with no text of its own is never a chunk alone.

    The header line is the path, then the trail of each section the chunk holds:
    ``README.md: Shop > Install > Docker``; the text before the first heading has none. A document
    has no symbols and no language. A chunk id is ``chunk_id(record_id, key)`` with the key
    ``("section", trail, ordinal)`` (the titles joined by a newline; ``ordinal`` counts earlier
    sections with the same trail), or ``("preamble",)``; later windows add the window number.
    Editing a section changes the content hash of its chunk and nothing else, as long as the edit
    does not move it across a size limit; renaming a heading changes the ids of its section and of
    the sections under it.

Pushed records
    A thread, a meeting or a document of a push or MCP source is cut from its normalized text
    (:mod:`memini.payloads`): a chunk's lines and byte offsets count in that text, its content is
    rows of it, and its header is one line that names the record and the part. A chunk has an
    anchor, the place evidence and locators name (:attr:`Chunk.anchor`), and may have a URL and
    a time. Sizes: :class:`~memini.chunking.payloads.PayloadSizes`.

    A thread is cut into groups of consecutive messages (kind ``messages``). A group closes
    before a message when it holds 20 messages, when the message would take it over 50 lines or
    2000 characters, or when it holds at least 3 messages and the message's id hashes to a
    boundary (:func:`~memini.chunking.payloads.is_boundary`, one id in 4). The boundaries hang on
    the ids, not on the text, so an edited message changes its own group and, only when it moves
    a size limit, the next ones up to the next boundary; a reply added at the end changes the
    last group or starts one. A message is never split, unless it is larger than a group alone:
    then it is cut into line windows. The header is the thread's channel and title, the authors
    of the group and its time span: ``general · Release · Ann Example, Bo Sample · 2026-03-01
    09:30 to 11:05 UTC``. The key is ``("messages", <id of the first message>)``, a later window
    adds its number; the anchor is ``msg-<id>``, the URL that message's ``url``, the time its
    ``sent_at``.

    A meeting gives the sections of its summary, cut by the Markdown chunker (kind ``summary``,
    key ``("summary", *markdown key)``, anchor ``summary`` or ``summary/<trail>``); its action
    items, one chunk or line windows of them (``action_items``, ``("action_items",)``,
    ``action-items``); and windows of its transcript, groups of consecutive segments by the rule
    of threads (at least 8, one id in 8, at most 60 segments; ``transcript``,
    ``("transcript", <id of the first segment>)``, ``seg-<id>``). A meeting with none of them is
    one chunk of its overview (``summary``, ``("overview",)``, ``overview``). Headers start with
    the title and the time; summary and action items add the participants, a transcript window
    its span of the meeting and its speakers: ``Release planning · 2026-03-01 14:00 to 14:30 UTC
    · Transcript 00:05 to 12:40 · Ann Example, Bo Sample``. The time of a chunk is the start of
    the meeting, or of its window when the segment says when it starts.

    A pushed document is its text cut by the Markdown chunker, its title standing where a file
    of a repository has its path (``Greenhouse handbook: Install > Docker``); the keys are the
    Markdown chunker's, the anchor of a chunk is the trail of its first section. A document
    without text is one chunk of its title.

Index fingerprint
    :data:`CHUNKER_VERSION` changes whenever the output of the code chunker can change,
    :data:`MARKDOWN_CHUNKER_VERSION` whenever the output of the Markdown chunker can, and
    :data:`PAYLOAD_CHUNKER_VERSION` whenever the output of the chunkers of pushed records can.
    :func:`chunker_fingerprint` has the three and the versions of tree-sitter and the grammar
    packages, which can change the output too.
"""

from memini.chunking.chunker import chunk_file, chunk_id, chunker_fingerprint
from memini.chunking.languages import EXTENSIONS, detect_language
from memini.chunking.markdown import chunk_markdown
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
from memini.chunking.payloads import (
    DEFAULT_PAYLOAD_SIZES,
    GroupRule,
    PayloadSizes,
    chunk_document_payload,
    chunk_meeting,
    chunk_parsed,
    chunk_payload,
    chunk_thread,
)

__all__ = [
    "CHUNKER_VERSION",
    "CHUNK_ID_DOMAIN",
    "DEFAULT_PAYLOAD_SIZES",
    "DEFAULT_SIZES",
    "EXTENSIONS",
    "LANGUAGES",
    "MARKDOWN_CHUNKER_VERSION",
    "PAYLOAD_CHUNKER_VERSION",
    "Chunk",
    "ChunkKind",
    "ChunkSizes",
    "Fallback",
    "FileChunks",
    "GroupRule",
    "Language",
    "PayloadSizes",
    "Symbol",
    "SymbolKind",
    "chunk_document_payload",
    "chunk_file",
    "chunk_id",
    "chunk_markdown",
    "chunk_meeting",
    "chunk_parsed",
    "chunk_payload",
    "chunk_thread",
    "chunker_fingerprint",
    "detect_language",
]
