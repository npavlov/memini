"""The code chunker: golden output per language, fallbacks, id stability, size limits.

Golden files sit next to the fixtures in ``tests/fixtures/chunking``. After a deliberate change of
the chunking rules, bump ``CHUNKER_VERSION``, run ``MEMINI_UPDATE_GOLDENS=1 make test`` and review
the diff of the ``.json`` files.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from memini.chunking import (
    CHUNK_ID_DOMAIN,
    CHUNKER_VERSION,
    DEFAULT_SIZES,
    MARKDOWN_CHUNKER_VERSION,
    ChunkSizes,
    FileChunks,
    Language,
    chunk_file,
    chunk_id,
    chunker_fingerprint,
    detect_language,
)
from memini.ids import record_id

FIXTURES = Path(__file__).parent / "fixtures" / "chunking"
UPDATE_GOLDENS = os.environ.get("MEMINI_UPDATE_GOLDENS") == "1"
SOURCE = uuid.UUID("0190a0a0-0000-7000-8000-00000000c0de")
FIXTURE_NAMES = [
    "greenhouse.py",
    "ledger.ts",
    "playlist.js",
    "tea_shop.rb",
    "weather.go",
    "kiln.toml",
]
RID = record_id(SOURCE, "code_file", "refs/heads/main:src/sample.py")


def fixture_record_id(name: str) -> str:
    return record_id(SOURCE, "code_file", f"refs/heads/main:{name}")


def chunk_fixture(name: str, sizes: ChunkSizes = DEFAULT_SIZES) -> FileChunks:
    return chunk_file(fixture_record_id(name), name, (FIXTURES / name).read_bytes(), sizes=sizes)


def as_json(result: FileChunks) -> dict[str, Any]:
    by_id = {chunk.id: chunk.key for chunk in result.chunks}
    return {
        "chunker_version": CHUNKER_VERSION,
        "language": result.language,
        "fallback": result.fallback,
        "syntax_errors": result.syntax_errors,
        "chunks": [
            {
                "key": chunk.key,
                "id": chunk.id,
                "kind": chunk.kind,
                "symbol": chunk.symbol,
                "lines": [chunk.start_line, chunk.end_line],
                "bytes": [chunk.start_byte, chunk.end_byte],
                "content_hash": chunk.content_hash,
                "text": chunk.text.split("\n"),
            }
            for chunk in result.chunks
        ],
        "symbols": [
            {
                "qualified_name": symbol.qualified_name,
                "name": symbol.name,
                "kind": symbol.kind,
                "parent": symbol.parent,
                "ordinal": symbol.ordinal,
                "lines": [symbol.start_line, symbol.end_line],
                "bytes": [symbol.start_byte, symbol.end_byte],
                "chunk": by_id[symbol.chunk_id],
            }
            for symbol in result.symbols
        ],
    }


# Golden output ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_output_matches_the_golden_file(name: str) -> None:
    actual = as_json(chunk_fixture(name))
    golden = FIXTURES / f"{name}.json"
    if UPDATE_GOLDENS:
        text = json.dumps(actual, indent=2, ensure_ascii=False)
        text = re.sub(r"\[\s+(\d+),\s+(\d+)\s+\]", r"[\1, \2]", text)  # spans on one line
        golden.write_text(text + "\n")
    assert golden.exists(), f"no golden file; run with MEMINI_UPDATE_GOLDENS=1 to write {golden}"
    assert actual == json.loads(golden.read_text())


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_chunks_cover_the_file_within_the_limits(name: str) -> None:
    content = (FIXTURES / name).read_bytes()
    result = chunk_fixture(name)
    lines = content.decode().split("\n")

    covered = {
        line for chunk in result.chunks for line in range(chunk.start_line, chunk.end_line + 1)
    }
    assert {i + 1 for i, line in enumerate(lines) if line.strip()} <= covered
    assert len({chunk.id for chunk in result.chunks}) == len(result.chunks)
    assert [c.start_byte for c in result.chunks] == sorted(c.start_byte for c in result.chunks)
    for chunk in result.chunks:
        assert chunk.content == content[chunk.start_byte : chunk.end_byte].decode()
        assert chunk.end_line - chunk.start_line + 1 <= DEFAULT_SIZES.max_lines
        assert len(chunk.content) <= DEFAULT_SIZES.max_chars
        assert chunk.text == f"{chunk.header}\n{chunk.content}"
        assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
        assert chunk.header.startswith(name)
    for symbol in result.symbols:
        holder = next(chunk for chunk in result.chunks if chunk.id == symbol.chunk_id)
        assert holder.start_line <= symbol.start_line <= holder.end_line
        assert symbol.path == name


def test_the_chunk_id_is_fixed() -> None:
    def field(data: bytes) -> bytes:
        return len(data).to_bytes(8, "big") + data

    key = ("def", "Tray.plant", "0")
    encoded = field(CHUNK_ID_DOMAIN.encode()) + field(RID.encode())
    encoded += b"".join(field(part.encode()) for part in key)

    assert CHUNK_ID_DOMAIN == "memini.chunk.v1"
    assert chunk_id(RID, key) == hashlib.sha256(encoded).hexdigest()
    # Never update this value to make the test pass: every stored chunk id would change.
    assert chunk_id(RID, key) == "1d58b6bc7509471b26421fe441bad2f251de084b3a4f93c2c1ec7a1c3571a9dd"


# Stability --------------------------------------------------------------------------------------

_DIGEST_SCRIPT = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from test_chunking import FIXTURE_NAMES, as_json, chunk_fixture
digest = hashlib.sha256()
for name in FIXTURE_NAMES:
    digest.update(json.dumps(as_json(chunk_fixture(name)), sort_keys=True).encode())
print(digest.hexdigest())
"""


def digest_of_all_fixtures() -> str:
    digest = hashlib.sha256()
    for name in FIXTURE_NAMES:
        digest.update(json.dumps(as_json(chunk_fixture(name)), sort_keys=True).encode())
    return digest.hexdigest()


def test_two_runs_give_the_same_output() -> None:
    assert digest_of_all_fixtures() == digest_of_all_fixtures()


@pytest.mark.parametrize("hash_seed", ["0", "4242"])
def test_output_does_not_depend_on_the_process(hash_seed: str, tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    tests_dir = str(Path(__file__).parent)
    completed = subprocess.run(  # noqa: S603 -- our own interpreter and script
        [sys.executable, "-c", _DIGEST_SCRIPT, tests_dir],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == digest_of_all_fixtures()


def test_editing_one_function_changes_only_its_chunk() -> None:
    original = (FIXTURES / "greenhouse.py").read_bytes()
    edited = original.replace(
        b'            raise ValueError("the tray is full")',
        b'            raise ValueError(f"tray {self.shelf} is full")\n'
        b"        # one more line, so everything below moves down",
    )
    assert edited != original
    rid = fixture_record_id("greenhouse.py")

    before = {c.id: c for c in chunk_file(rid, "greenhouse.py", original).chunks}
    after = {c.id: c for c in chunk_file(rid, "greenhouse.py", edited).chunks}

    assert before.keys() == after.keys()
    changed = [key for key in before if before[key].content_hash != after[key].content_hash]
    assert [before[key].key for key in changed] == ["Tray#0/+0"]  # the group that holds plant
    moved = [key for key in before if before[key].start_line != after[key].start_line]
    assert moved  # chunks below the edit moved, and kept their ids and hashes


def test_adding_a_function_keeps_the_ids_of_the_others() -> None:
    source = b"def first():\n    return 1\n\n\ndef third():\n    return 3\n"
    grown = source.replace(b"\n\n\ndef third", b"\n\n\ndef second():\n    return 2\n\n\ndef third")
    small = ChunkSizes(small_lines=1, merge_lines=10)

    before = {c.key: c for c in chunk_file(RID, "a.py", source, sizes=small).chunks}
    after = {c.key: c for c in chunk_file(RID, "a.py", grown, sizes=small).chunks}

    assert set(after) == {"first#0", "second#0", "third#0"}
    for key, chunk in before.items():
        assert (after[key].id, after[key].content_hash) == (chunk.id, chunk.content_hash)


def test_the_path_is_not_part_of_the_id() -> None:
    content = (FIXTURES / "greenhouse.py").read_bytes()

    here = chunk_file(RID, "greenhouse.py", content)
    there = chunk_file(RID, "elsewhere/greenhouse.py", content)

    assert [c.id for c in here.chunks] == [c.id for c in there.chunks]
    assert [c.content_hash for c in here.chunks] != [c.content_hash for c in there.chunks]


# Definitions ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "qualified_name", "count"),
    [
        ("greenhouse.py", "rotate", 2),  # redefined
        ("greenhouse.py", "Tray.full", 2),  # property and setter
        ("ledger.ts", "total", 3),  # two overloads and the implementation
        ("ledger.ts", "Ledger.size", 2),  # getter and setter
        ("tea_shop.rb", "Counter.Pot", 2),  # reopened class
        ("weather.go", "init", 2),
    ],
)
def test_same_names_get_ordinals(name: str, qualified_name: str, count: int) -> None:
    result = chunk_fixture(name)

    same = [s for s in result.symbols if s.qualified_name == qualified_name]

    assert [s.ordinal for s in same] == list(range(count))


def test_same_names_do_not_collide_when_each_is_a_chunk() -> None:
    source = b"def f():\n    return 1\n\n\ndef f():\n    return 2\n"

    result = chunk_file(RID, "a.py", source, sizes=ChunkSizes(small_lines=1, merge_lines=10))

    assert [c.key for c in result.chunks] == ["f#0", "f#1"]
    assert result.chunks[0].id != result.chunks[1].id


def test_nested_definitions_are_qualified_by_their_parents() -> None:
    result = chunk_fixture("greenhouse.py")
    parents = {s.qualified_name: s.parent for s in result.symbols}

    assert parents["Tray.Label"] == "Tray"
    assert parents["Tray.Label.render"] == "Tray.Label"
    assert parents["schedule.daily"] == "schedule"
    assert {s.kind for s in result.symbols if s.parent == "Tray"} == {"method", "class"}


def test_a_go_method_is_qualified_by_its_receiver() -> None:
    result = chunk_fixture("weather.go")
    methods = {s.qualified_name: s.parent for s in result.symbols if s.kind == "method"}

    assert methods == {
        "Reading.String": "Reading",
        "Reading.Warm": "Reading",
        "Series.Push": "Series",
    }


def test_comments_and_decorators_belong_to_the_definition_below() -> None:
    source = (
        b"import os\n"
        b"\n"
        b"# A section comment, apart from the code.\n"
        b"\n"
        b"# Says what f does.\n"
        b"@cache\n"
        b"# between the decorator and the function\n"
        b"def f():\n"
        b"    return os.sep\n"
    )

    result = chunk_file(RID, "a.py", source, sizes=ChunkSizes(small_lines=1, merge_lines=10))

    # The section comment is a statement of its own, merged with the import next to it.
    assert [(c.key, c.kind, c.start_line, c.end_line) for c in result.chunks] == [
        ("/+0", "code", 1, 3),
        ("f#0", "function", 5, 9),
    ]
    assert (result.symbols[0].start_line, result.symbols[0].end_line) == (6, 9)


def test_typescript_member_decorators_belong_to_the_member() -> None:
    source = (
        b"class Panel {\n"
        b"  open = false;\n"
        b"\n"
        b"  @Listen('click')\n"
        b"  // toggles the panel\n"
        b"  toggle(): void {\n"
        b"    this.open = !this.open;\n"
        b"  }\n"
        b"}\n"
    )
    sizes = ChunkSizes(small_lines=1, merge_lines=5)

    result = chunk_file(RID, "panel.ts", source, sizes=sizes)

    toggle = next(s for s in result.symbols if s.qualified_name == "Panel.toggle")
    assert (toggle.start_line, toggle.end_line) == (4, 8)
    chunk = next(c for c in result.chunks if c.key == "Panel.toggle#0")
    assert chunk.content.startswith("  @Listen('click')")
    assert chunk.content.endswith("  }\n}")  # the closing brace of the class came along


def test_a_small_class_is_one_chunk_and_a_large_one_is_split() -> None:
    methods = "".join(f"    def m{i}(self):\n        return {i}\n\n" for i in range(8))
    source = f"class Box:\n    size = 1\n\n{methods}".encode()

    whole = chunk_file(RID, "box.py", source)
    split = chunk_file(RID, "box.py", source, sizes=ChunkSizes(small_lines=1, merge_lines=10))

    assert [(c.key, c.kind) for c in whole.chunks] == [("Box#0", "class")]
    assert [c.key for c in split.chunks] == ["Box#0/+0"] + [f"Box.m{i}#0" for i in range(8)]
    assert split.chunks[0].content == "class Box:\n    size = 1"
    assert split.chunks[0].header == "box.py: Box"


def test_the_closing_line_of_a_split_class_goes_with_the_last_member() -> None:
    methods = "".join(f"  m{i}() {{\n    return {i};\n  }}\n" for i in range(4))
    source = f"class Box {{\n{methods}}}\n".encode()

    result = chunk_file(RID, "box.js", source, sizes=ChunkSizes(small_lines=1, merge_lines=5))

    assert [c.key for c in result.chunks] == [f"Box.m{i}#0" for i in range(4)]
    assert result.chunks[0].content.startswith("class Box {\n  m0() {")
    assert result.chunks[-1].content.endswith("    return 3;\n  }\n}")
    assert result.chunks[0].header == "box.js: Box, Box.m0"


def test_small_neighbours_merge_up_to_the_budget() -> None:
    source = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(6)).encode()

    result = chunk_file(RID, "a.py", source, sizes=ChunkSizes(small_lines=2, merge_lines=8))

    assert [(c.key, c.kind, c.start_line, c.end_line) for c in result.chunks] == [
        ("f0#0", "group", 1, 8),
        ("f3#0", "group", 10, 17),
    ]
    assert result.chunks[0].header == "a.py: f0, f1, f2"
    assert result.chunks[0].symbol is None


def test_a_file_of_statements_only() -> None:
    source = b"import sys\n\nfor arg in sys.argv:\n    print(arg)\n"

    result = chunk_file(RID, "script.py", source)

    assert result.symbols == ()
    assert [(c.key, c.kind, c.symbol, c.header) for c in result.chunks] == [
        ("/+0", "code", None, "script.py")
    ]


@pytest.mark.parametrize("content", [b"", b"\n\n   \n\t\n"])
def test_an_empty_file_has_no_chunks(content: bytes) -> None:
    result = chunk_file(RID, "empty.py", content)

    assert (result.chunks, result.symbols, result.fallback) == ((), (), None)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            "hello.tsx",
            b"export function Hello({ name }: { name: string }) {\n  return <p>{name}</p>;\n}\n",
        ),
        ("hello.jsx", b"export function Hello({ name }) {\n  return <p>{name}</p>;\n}\n"),
    ],
)
def test_tsx_and_jsx_parse(path: str, source: bytes) -> None:
    result = chunk_file(RID, path, source)

    assert not result.syntax_errors
    assert [s.qualified_name for s in result.symbols] == ["Hello"]


# Large input ------------------------------------------------------------------------------------


def test_a_large_function_is_cut_into_overlapping_windows() -> None:
    body = "".join(f"    total += {i}\n" for i in range(40))
    source = f"def big():\n    total = 0\n{body}    return total\n".encode()
    sizes = ChunkSizes(
        max_lines=20, merge_lines=10, small_lines=2, window_lines=12, window_overlap=3
    )

    result = chunk_file(RID, "big.py", source, sizes=sizes)

    keys = [c.key for c in result.chunks]
    assert keys == ["big#0"] + [f"big#0@{i}" for i in range(1, len(keys))]
    assert {c.symbol for c in result.chunks} == {"big"}
    assert {c.kind for c in result.chunks} == {"function"}
    for before, after in zip(result.chunks, result.chunks[1:], strict=False):
        assert after.start_line == before.end_line - 2  # three lines shared
        assert before.end_line - before.start_line + 1 == 12
    assert result.chunks[-1].end_line == 43
    assert result.symbols[0].chunk_id == result.chunks[0].id


def test_a_line_longer_than_max_chars_is_cut_into_pieces() -> None:
    long_line = "x = '" + "é" * 250 + "'"
    source = f"{long_line}\ny = 1\n".encode()
    sizes = ChunkSizes(
        max_chars=100, small_lines=1, merge_lines=10, window_lines=10, window_overlap=2
    )

    result = chunk_file(RID, "long.py", source, sizes=sizes)

    pieces = [c for c in result.chunks if c.start_line == c.end_line == 1]
    assert len(pieces) > 1
    assert "".join(c.content for c in pieces) == long_line
    assert all(len(c.content.encode()) <= 100 for c in pieces)
    assert [c.key for c in pieces] == ["x#0"] + [f"x#0@{i}" for i in range(1, len(pieces))]
    assert result.chunks[-1].content == "y = 1"


# Fallbacks --------------------------------------------------------------------------------------


def test_an_unknown_language_is_cut_into_line_windows() -> None:
    lines = [f"line {i}" for i in range(130)]
    source = "\n".join(lines).encode()

    result = chunk_file(RID, "notes.txt", source)

    assert (result.language, result.fallback, result.symbols) == (None, "unknown_language", ())
    assert [(c.key, c.kind, c.start_line, c.end_line) for c in result.chunks] == [
        ("/+0", "window", 1, 60),
        ("/+1", "window", 51, 110),
        ("/+2", "window", 101, 130),
    ]
    assert result.chunks[0].header == "notes.txt"


def test_a_language_can_be_named_for_a_file_without_extension() -> None:
    source = b"def main():\n    return 0\n"

    detected = chunk_file(RID, "bin/tool", source)
    named = chunk_file(RID, "bin/tool", source, language="python")

    assert detected.fallback == "unknown_language"
    assert (named.fallback, named.language) == (None, "python")
    assert [s.qualified_name for s in named.symbols] == ["main"]


def test_a_file_too_large_to_parse_is_cut_into_windows() -> None:
    source = b"def f():\n    return 1\n"

    result = chunk_file(RID, "a.py", source, sizes=ChunkSizes(max_parse_bytes=10))

    assert (result.language, result.fallback, result.symbols) == ("python", "too_large", ())
    assert [c.kind for c in result.chunks] == ["window"]


def test_a_binary_file_has_no_chunks() -> None:
    result = chunk_file(RID, "image.py", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

    assert (result.chunks, result.symbols, result.fallback) == ((), (), "binary")


@pytest.mark.parametrize("path", ["settings.py", "settings.txt"])
def test_a_nul_byte_past_the_binary_probe_becomes_a_replacement_character(path: str) -> None:
    # Not binary: the NUL is past the first 8000 bytes. Postgres text cannot hold it.
    content = b"x = 1\n" * 2000 + b"y = '\x00'\n"

    result = chunk_file(RID, path, content)

    assert result.fallback != "binary"
    assert result.chunks
    assert not any("\0" in chunk.text for chunk in result.chunks)
    (chunk,) = [c for c in result.chunks if "\N{REPLACEMENT CHARACTER}" in c.content]
    assert chunk.content.endswith("y = '\N{REPLACEMENT CHARACTER}'")
    assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
    assert chunk.end_byte == len(content) - 1  # offsets are into the original bytes


def test_a_nul_byte_in_a_piece_of_a_long_line_is_replaced() -> None:
    content = b"x = 1\n" * 2000 + b"y = '" + b"a\x00" * 2000 + b"'\n"

    result = chunk_file(RID, "data.txt", content)

    assert not any("\0" in chunk.text for chunk in result.chunks)
    assert sum(chunk.content.count("\N{REPLACEMENT CHARACTER}") for chunk in result.chunks) == 2000


def test_bytes_that_are_not_utf8_are_replaced_and_offsets_kept() -> None:
    source = b"# caf\xe9 au lait\ndef brew():\n    return 'cr\xe8me'\n"

    result = chunk_file(RID, "cafe.py", source)

    (chunk,) = result.chunks
    assert chunk.content == "# caf� au lait\ndef brew():\n    return 'cr�me'"
    assert (chunk.start_byte, chunk.end_byte) == (0, len(source) - 1)
    assert [s.qualified_name for s in result.symbols] == ["brew"]
    assert chunk_file(RID, "cafe.py", source) == result


def test_syntax_errors_still_give_chunks() -> None:
    source = b"def ok():\n    return 1\n\n\ndef broken(:\n    return\n\n\nx = [1, 2\n"

    result = chunk_file(RID, "broken.py", source)

    assert result.syntax_errors
    assert result.fallback is None
    covered = {n for c in result.chunks for n in range(c.start_line, c.end_line + 1)}
    assert {1, 2, 5, 6, 9} <= covered


# Inputs -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("stubs/a.pyi", "python"),
        ("a.js", "javascript"),
        ("a.jsx", "javascript"),
        ("a.mjs", "javascript"),
        ("a.cjs", "javascript"),
        ("a.ts", "typescript"),
        ("a.tsx", "typescript"),
        ("a.mts", "typescript"),
        ("a.cts", "typescript"),
        ("types/a.d.ts", "typescript"),
        ("a.rb", "ruby"),
        ("a.go", "go"),
        ("A.PY", "python"),
        ("a.rs", None),
        ("Makefile", None),
        ("dir.py/readme", None),
    ],
)
def test_language_by_extension(path: str, language: Language | None) -> None:
    assert detect_language(path) == language


def test_bad_input_is_refused() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        chunk_file("ABC", "a.py", b"x = 1\n")
    with pytest.raises(ValueError, match="unknown language"):
        chunk_file(RID, "a.rs", b"fn main() {}\n", language="rust")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="not valid Unicode"):
        chunk_file(RID, "caf\udce9.py", b"x = 1\n")
    with pytest.raises(ValueError, match="NUL character"):
        chunk_file(RID, "a\0.py", b"x = 1\n")


@pytest.mark.parametrize(
    "changes",
    [
        {"small_lines": 0},
        {"merge_lines": 100},
        {"small_lines": 50},
        {"window_lines": 100},
        {"window_overlap": 60},
        {"max_chars": 10},
        {"max_parse_bytes": 0},
    ],
)
def test_inconsistent_sizes_are_refused(changes: dict[str, int]) -> None:
    with pytest.raises(ValueError, match=r"must be|expected"):
        ChunkSizes(**changes)


def test_the_fingerprint_names_the_version_and_the_grammars() -> None:
    fingerprint = chunker_fingerprint()

    assert fingerprint.startswith(
        f"memini-chunker/{CHUNKER_VERSION} markdown/{MARKDOWN_CHUNKER_VERSION} tree-sitter/"
    )
    for package in ("go", "javascript", "python", "ruby", "typescript"):
        assert f" tree-sitter-{package}/" in fingerprint
