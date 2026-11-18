"""The Markdown chunker: golden output, headings, fences, front matter, id stability, size limits.

The golden file sits next to the fixture in ``tests/fixtures/chunking``. After a deliberate change
of the rules, bump ``MARKDOWN_CHUNKER_VERSION``, run ``MEMINI_UPDATE_GOLDENS=1 make test`` and
review the diff of the ``.json`` file. Every document here is synthetic.
"""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from memini.chunking import (
    DEFAULT_SIZES,
    MARKDOWN_CHUNKER_VERSION,
    Chunk,
    ChunkSizes,
    FileChunks,
    chunk_markdown,
)
from memini.ids import record_id

FIXTURES = Path(__file__).parent / "fixtures" / "chunking"
UPDATE_GOLDENS = os.environ.get("MEMINI_UPDATE_GOLDENS") == "1"
SOURCE = uuid.UUID("0190a0a0-0000-7000-8000-00000000c0de")
RID = record_id(SOURCE, "document", "refs/heads/main:docs/guide.md")
FIXTURE = "handbook.md"
SMALL = ChunkSizes(
    max_lines=12, max_chars=600, merge_lines=8, small_lines=3, window_lines=6, window_overlap=2
)
SEPARATE = ChunkSizes(small_lines=1)
"""No section of more than one row merges with another: one chunk per section."""


def chunk(text: str, sizes: ChunkSizes = DEFAULT_SIZES, path: str = "docs/guide.md") -> FileChunks:
    return chunk_markdown(RID, path, text.encode(), sizes=sizes)


def keys(text: str, sizes: ChunkSizes = DEFAULT_SIZES) -> list[str]:
    return [c.key for c in chunk(text, sizes).chunks]


def headers(text: str, sizes: ChunkSizes = DEFAULT_SIZES) -> list[str]:
    return [c.header for c in chunk(text, sizes).chunks]


def section(title: str, lines: int, *, level: int = 2) -> str:
    """A heading and ``lines - 2`` lines of text after a blank line: ``lines`` rows in all."""
    body = "\n".join(f"{title} line {n}." for n in range(1, lines - 1))
    return f"{'#' * level} {title}\n\n{body}\n"


def by_key(result: FileChunks) -> dict[str, Chunk]:
    return {c.key: c for c in result.chunks}


# Golden output ----------------------------------------------------------------------------------


def as_json(result: FileChunks) -> dict[str, Any]:
    return {
        "chunker_version": MARKDOWN_CHUNKER_VERSION,
        "language": result.language,
        "fallback": result.fallback,
        "chunks": [
            {
                "key": c.key,
                "id": c.id,
                "kind": c.kind,
                "lines": [c.start_line, c.end_line],
                "bytes": [c.start_byte, c.end_byte],
                "content_hash": c.content_hash,
                "text": c.text.split("\n"),
            }
            for c in result.chunks
        ],
    }


def chunk_fixture() -> FileChunks:
    rid = record_id(SOURCE, "document", f"refs/heads/main:{FIXTURE}")
    return chunk_markdown(rid, FIXTURE, (FIXTURES / FIXTURE).read_bytes())


def test_output_matches_the_golden_file() -> None:
    actual = as_json(chunk_fixture())
    golden = FIXTURES / f"{FIXTURE}.json"
    if UPDATE_GOLDENS:
        text = json.dumps(actual, indent=2, ensure_ascii=False)
        text = re.sub(r"\[\s+(\d+),\s+(\d+)\s+\]", r"[\1, \2]", text)  # spans on one line
        golden.write_text(text + "\n")
    assert golden.exists(), f"no golden file; run with MEMINI_UPDATE_GOLDENS=1 to write {golden}"
    assert actual == json.loads(golden.read_text())


def test_the_fixture_is_cut_by_its_headings() -> None:
    result = chunk_fixture()

    assert [(c.key, c.kind) for c in result.chunks] == [
        ("(preamble)", "section"),
        ("Lantern Tea Shop#0", "group"),  # a small section with its first subsection
        ("Lantern Tea Shop > Brewing#0", "group"),  # small neighbours
        ("Lantern Tea Shop > Brewing > Oolong#0", "section"),
        ("Lantern Tea Shop > Brewing > Green tea#1", "section"),
        ("Lantern Tea Shop > Cleaning the counter#0", "section"),
        ("Suppliers#0", "section"),
    ]
    assert result.chunks[2].header == (
        "handbook.md: Lantern Tea Shop > Brewing, Lantern Tea Shop > Brewing > Green tea"
    )
    assert (result.language, result.symbols, result.fallback) == (None, (), None)


def test_chunks_cover_the_document_within_the_limits() -> None:
    content = (FIXTURES / FIXTURE).read_bytes()
    result = chunk_fixture()
    lines = content.decode().split("\n")

    covered = {n for c in result.chunks for n in range(c.start_line, c.end_line + 1)}
    assert {i + 1 for i, line in enumerate(lines) if line.strip()} <= covered
    assert len({c.id for c in result.chunks}) == len(result.chunks)
    assert [c.start_byte for c in result.chunks] == sorted(c.start_byte for c in result.chunks)
    for c in result.chunks:
        assert c.content == content[c.start_byte : c.end_byte].decode()
        assert c.end_line - c.start_line + 1 <= DEFAULT_SIZES.max_lines
        assert len(c.content) <= DEFAULT_SIZES.max_chars
        assert c.content_hash == hashlib.sha256(c.text.encode()).hexdigest()
        assert c.header.startswith(FIXTURE)
        assert (c.symbol, c.language) == (None, None)
        assert "\n" not in c.header


def test_two_runs_give_the_same_output() -> None:
    assert chunk_fixture() == chunk_fixture()


# Headings ---------------------------------------------------------------------------------------


def test_atx_headings_and_their_trails() -> None:
    text = (
        "# Shop\n\nIntro.\n\n## Install ##\n\nRun it.\n\n### Docker\n\nPull it.\n\n"
        "## Usage\n\nUse it.\n\n# Appendix #\n\nMore.\n"
    )

    assert headers(text, SEPARATE) == [
        "docs/guide.md: Shop",
        "docs/guide.md: Shop > Install",
        "docs/guide.md: Shop > Install > Docker",
        "docs/guide.md: Shop > Usage",
        "docs/guide.md: Appendix",
    ]


@pytest.mark.parametrize(
    ("line", "title"),
    [
        ("# Title", "Title"),
        ("   ###   Spaced out   ", "Spaced out"),
        ("## Closed ##", "Closed"),
        ("## Closed ##   ", "Closed"),
        ("# C#", "C#"),
        ("# Ends in a hash \\#", "Ends in a hash \\#"),
        ("#\tTabbed", "Tabbed"),
    ],
)
def test_atx_titles(line: str, title: str) -> None:
    (first,) = chunk(f"{line}\n\nText.\n").chunks
    assert first.header == f"docs/guide.md: {title}"


@pytest.mark.parametrize(
    "line",
    ["#hashtag", "####### seven is too many", "    # indented code", "\t# indented code", "> # q"],
)
def test_lines_that_are_not_atx_headings(line: str) -> None:
    (first,) = chunk(f"Text.\n\n{line}\n\nMore text.\n").chunks
    assert first.key == "(preamble)"


def test_setext_headings() -> None:
    text = "Title\n=====\n\nIntro.\n\nA heading on\ntwo lines\n---\n\nBody.\n"

    assert headers(text, SEPARATE) == [
        "docs/guide.md: Title",
        "docs/guide.md: Title > A heading on two lines",
    ]
    assert [c.start_line for c in chunk(text, SEPARATE).chunks] == [1, 6]


@pytest.mark.parametrize(
    "text",
    [
        "Text.\n\n---\n\nMore.\n",  # a thematic break after a blank line
        "- an item\n---\n\nMore.\n",  # a list item is not underlined
        "1. an item\n---\n\nMore.\n",
        "> a quote\n---\n\nMore.\n",
        "<div>\n---\n\nMore.\n",
        "    indented code\n---\n\nMore.\n",
        "***\n---\n\nMore.\n",  # a thematic break ends no paragraph that could be underlined
        "===\n\nMore.\n",
    ],
)
def test_lines_that_are_not_setext_headings(text: str) -> None:
    assert keys(text) == ["(preamble)"]


def test_a_heading_in_a_fence_or_a_comment_is_not_a_heading() -> None:
    text = (
        "# Real\n\n```python\n# a comment\n\n## not a heading\n```\n\n"
        "~~~~\n# tilde\n~~~\n## still in the fence\n~~~~\n\n"
        "<!-- # commented -->\n<!--\n## commented\n-->\n\nEnd.\n"
    )

    assert keys(text) == ["Real#0"]


def test_a_fence_closes_only_with_its_own_kind_and_length() -> None:
    text = "# A\n\n````\n```\n# inside\n````\n\n# B\n\nText.\n"
    assert headers(text, SEPARATE) == ["docs/guide.md: A", "docs/guide.md: B"]


def test_backticks_in_the_info_string_make_no_fence() -> None:
    text = "```inline` code\n# Heading\n\nText.\n"
    assert keys(text, SEPARATE) == ["(preamble)", "Heading#0"]


def test_a_fence_that_is_not_closed_runs_to_the_end() -> None:
    text = "# A\n\n```\n# inside\n\n## also inside\n"
    assert keys(text) == ["A#0"]


def test_front_matter_is_part_of_the_preamble_and_has_no_headings() -> None:
    yaml = "---\ntitle: Guide\n# a YAML comment\nlist:\n---\n\nIntro.\n\n# Guide\n\nText.\n"
    toml = "+++\ntitle = 'Guide'\n# a TOML comment\n+++\n\n# Guide\n\nText.\n"

    for text in (yaml, toml):
        result = chunk(text, SEPARATE)
        assert [c.key for c in result.chunks] == ["(preamble)", "Guide#0"]
        assert result.chunks[0].header == "docs/guide.md"
        assert result.chunks[0].content.startswith(text[:3])


def test_a_dash_line_without_a_closing_one_is_not_front_matter() -> None:
    text = "---\n# Heading\n\nText.\n"
    assert keys(text, SEPARATE) == ["(preamble)", "Heading#0"]


def test_a_byte_order_mark_and_crlf_line_ends() -> None:
    text = "﻿# Title\r\n\r\nText.\r\n\r\nSub\r\n---\r\n\r\nMore.\r\n"

    result = chunk(text, SEPARATE)

    assert [c.header for c in result.chunks] == [
        "docs/guide.md: Title",
        "docs/guide.md: Title > Sub",
    ]


# Sections and their ids -------------------------------------------------------------------------


def test_repeated_trails_get_ordinals_and_skipped_levels_nest() -> None:
    text = (
        "# A\n\nText.\n\n### Example\n\nOne.\n\n## Example\n\nTwo.\n\n"
        "# B\n\nText.\n\n## Example\n\nThree.\n\n## Example\n\nFour.\n"
    )

    assert keys(text, SEPARATE) == [
        "A#0",
        "A > Example#0",
        "A > Example#1",
        "B#0",
        "B > Example#0",
        "B > Example#1",
    ]


def test_an_empty_file_has_no_chunks() -> None:
    for text in ("", "\n\n", "   \n\t\n"):
        assert chunk(text).chunks == ()


def test_a_document_without_headings_is_one_preamble() -> None:
    (only,) = chunk("Just some notes.\n\nOn two paragraphs.\n").chunks

    assert (only.key, only.kind, only.header) == ("(preamble)", "section", "docs/guide.md")
    assert only.content == "Just some notes.\n\nOn two paragraphs."


def test_a_heading_without_a_title() -> None:
    result = chunk("# Shop\n\nText.\n\n##\n\nUntitled.\n", SEPARATE)

    assert [c.key for c in result.chunks] == ["Shop#0", "Shop > #0"]
    assert [c.header for c in result.chunks] == ["docs/guide.md: Shop", "docs/guide.md: Shop"]


def test_editing_one_section_changes_only_its_chunk() -> None:
    parts = [section("Install", 8), section("Usage", 8), section("License", 8)]
    before = by_key(chunk("\n".join(parts)))
    parts[1] = parts[1].replace("Usage line 3.", "Usage line 3, edited.")
    after = by_key(chunk("\n".join(parts)))

    assert list(before) == list(after) == ["Install#0", "Usage#0", "License#0"]
    assert {k: c.id for k, c in before.items()} == {k: c.id for k, c in after.items()}
    changed = {k for k in before if before[k].content_hash != after[k].content_hash}
    assert changed == {"Usage#0"}


def test_adding_a_section_keeps_the_ids_of_the_others() -> None:
    parts = [section("Install", 8), section("Usage", 8)]
    before = by_key(chunk("\n".join(parts)))
    after = by_key(chunk("\n".join([parts[0], section("Upgrade", 8), parts[1]])))

    assert {k: before[k].id for k in before} == {k: after[k].id for k in before}
    assert {k: before[k].content_hash for k in before} == {k: after[k].content_hash for k in before}


def test_renaming_a_heading_changes_its_id_and_those_under_it() -> None:
    text = "\n".join(
        (section("Install", 8, level=1), section("Docker", 8), section("Use", 8, level=1))
    )
    renamed = text.replace("# Install", "# Setup")

    before, after = chunk(text).chunks, chunk(renamed).chunks

    assert [c.key for c in after] == ["Setup#0", "Setup > Docker#0", "Use#0"]
    assert [b.id == a.id for b, a in zip(before, after, strict=True)] == [False, False, True]


def test_the_path_is_not_part_of_the_id() -> None:
    text = section("Install", 8)
    assert chunk(text, path="a.md").chunks[0].id == chunk(text, path="b.md").chunks[0].id


# Sizes ------------------------------------------------------------------------------------------


def test_small_neighbours_merge_up_to_the_budget() -> None:
    text = "".join(f"## S{n}\n\nText {n}.\n\n" for n in range(1, 6))

    result = chunk(text, SMALL)

    # Three rows each; the merged chunk spans at most merge_lines (8): S1 and S2, S3 and S4, S5.
    assert [(c.key, c.kind) for c in result.chunks] == [
        ("S1#0", "group"),
        ("S3#0", "group"),
        ("S5#0", "section"),
    ]
    assert result.chunks[0].header == "docs/guide.md: S1, S2"


def test_a_group_stays_inside_the_heading_of_its_first_section() -> None:
    sizes = ChunkSizes(max_lines=40, merge_lines=20, small_lines=3, window_lines=20)
    # A2 and B are small neighbours, but B is outside A, the heading that encloses A2.
    text = "# A\n\n" + section("A1", 6) + "\n## A2\n\nTwo.\n\n# B\n\nThree.\n"

    assert keys(text, sizes) == ["A#0", "A > A2#0", "B#0"]
    # A top-level section, or the text before the first heading, is enclosed by the document:
    # whatever follows is inside it.
    assert keys("# A\n\nOne.\n\n## A1\n\nTwo.\n\n# B\n\nThree.\n", sizes) == ["A#0"]
    assert keys("Lead.\n\n# A\n\n## A1\n\nOne.\n\n# B\n\nTwo.\n", sizes) == ["(preamble)"]


def test_a_small_section_goes_with_its_first_subsection() -> None:
    sizes = ChunkSizes(max_lines=40, merge_lines=20, small_lines=3, window_lines=20)
    text = "# Guide\n\n" + section("Install", 10) + "\n" + section("Usage", 10)

    result = chunk(text, sizes)

    # A heading with no text of its own is never a chunk alone; the second subsection is not
    # joined: only the first follows its heading.
    assert [(c.key, c.kind) for c in result.chunks] == [
        ("Guide#0", "group"),
        ("Guide > Usage#0", "section"),
    ]
    assert result.chunks[0].header == "docs/guide.md: Guide, Guide > Install"
    assert result.chunks[0].content.startswith("# Guide\n\n## Install\n")
    # Not when the subsection is cut into windows, nor when the two do not fit.
    windowed = chunk("# Guide\n\n" + section("Install", 30), SMALL)
    assert [c.key for c in windowed.chunks][:2] == ["Guide#0", "Guide > Install#0"]
    # Nor a section that is not small, nor a sibling.
    assert keys(section("Intro", 5, level=1) + "\n" + section("Install", 10), sizes) == [
        "Intro#0",
        "Intro > Install#0",
    ]
    assert keys("## A\n\n" + "\n" + section("B", 10), sizes) == ["A#0", "B#0"]


def test_a_large_section_is_cut_into_windows_of_whole_blocks() -> None:
    paragraphs = [f"Paragraph {n} first line.\nParagraph {n} second line." for n in range(1, 8)]
    text = "## Long\n\n" + "\n\n".join(paragraphs) + "\n"

    result = chunk(text, SMALL)

    assert [c.key for c in result.chunks] == ["Long#0", "Long#0@1", "Long#0@2", "Long#0@3"]
    for c in result.chunks:
        assert c.kind == "section"
        assert c.header == "docs/guide.md: Long"
        assert c.end_line - c.start_line + 1 <= SMALL.window_lines
        # A window starts and ends on a block: no paragraph is cut in two.
        assert c.content.startswith(("## Long", "Paragraph"))
        assert c.content.count("first") == c.content.count("second")
    covered = {n for c in result.chunks for n in range(c.start_line, c.end_line + 1)}
    assert {i + 1 for i, line in enumerate(text.split("\n")) if line.strip()} <= covered


def test_a_fence_is_not_cut_when_it_fits_a_window() -> None:
    fence = "```\nline a\n\nline b\n\nline c\n```"
    text = "## Code\n\nIntro text.\n\n" + fence + "\n\nOutro text.\n\nLast words.\n"

    result = chunk(text, SMALL)

    assert any(c.content == fence or fence in c.content for c in result.chunks)
    assert all(c.content.count("```") in (0, 2) for c in result.chunks)


def test_a_fence_larger_than_a_window_is_cut_into_line_windows() -> None:
    fence = "```\n" + "\n".join(f"step {n}" for n in range(1, 21)) + "\n```"
    text = "## Code\n\n" + fence + "\n"

    result = chunk(text, SMALL)

    assert len(result.chunks) > 2
    assert all(c.end_line - c.start_line + 1 <= SMALL.max_lines for c in result.chunks)
    assert all(c.header == "docs/guide.md: Code" for c in result.chunks)


def test_a_line_longer_than_max_chars_is_cut_into_pieces() -> None:
    text = "## Wide\n\n" + "word " * 400 + "\n"

    result = chunk(text, SMALL)

    assert all(len(c.content) <= SMALL.max_chars for c in result.chunks)
    assert "".join(c.content for c in result.chunks[1:]) == ("word " * 400).rstrip("\n")


# Inputs -----------------------------------------------------------------------------------------


def test_a_binary_document_has_no_chunks() -> None:
    result = chunk_markdown(RID, "a.md", b"# Title\n\x00\x01\x02")
    assert (result.chunks, result.fallback) == ((), "binary")


def test_a_nul_byte_past_the_binary_probe_becomes_a_replacement_character() -> None:
    content = b"# Title\n\n" + b"Some text.\n" * 1000 + b"A \x00 here.\n"

    result = chunk_markdown(RID, "a.md", content)

    assert not any("\0" in c.text for c in result.chunks)
    assert any("A \N{REPLACEMENT CHARACTER} here." in c.content for c in result.chunks)


def test_bad_input_is_refused() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        chunk_markdown("ABC", "a.md", b"# A\n")
    with pytest.raises(ValueError, match="not valid Unicode"):
        chunk_markdown(RID, "caf\udce9.md", b"# A\n")
    with pytest.raises(ValueError, match="NUL character"):
        chunk_markdown(RID, "a\0.md", b"# A\n")
