"""The eval suite: fixture repositories, and questions with the results they expect.

A suite is a TOML file, written by hand, next to the directories of its fixture repositories::

    format = 1                      # of this file; a change of the format takes a new number
    name = "code"
    version = 1                     # of the questions; raise it when they change in meaning
    description = "..."

    [[repository]]
    name = "tidewater"              # the repository the files are synced as
    workspace = "harbor"            # the workspace it is in
    path = "repos/tidewater"        # a directory, relative to the suite file

    [[question]]
    id = "tw-fee-night"             # stable: the baseline compares questions by it
    text = "How much more does a berth cost at night?"
    language = "en"                 # of the question: en or ru
    kind = "code"                   # code, document or symbol: what the expected results are
    modes = ["hybrid", "dense", "lexical"]
    tags = ["paraphrase"]           # optional, free-form; the report groups by them too
    filters = { repositories = ["tidewater"] }    # optional: a search filter, as SearchFilters
    expect = [
      { repository = "tidewater", path = "tidewater/fees.py", symbol = "night_surcharge" },
    ]

An expected result names a place in a fixture file, never a chunk id: chunk ids change with the
chunker, a question must not. It is a repository and a path, and at most one of:

- ``symbol``: the qualified name of a definition in a code file (``FeeSchedule.berth``). A hit
  matches when its lines overlap the definition's, as the symbol table of the index has them;
  in the ``symbol`` mode, the lines of the definition found.
- ``heading``: a heading of a Markdown file, or the end of a heading trail
  (``Configuration > Tide tables``). A hit matches when it holds that section: one of the trails
  in its header ends with it.
- ``lines``: ``[first, last]``, 1-based. A hit matches when its lines overlap them.

With none of them, any hit of the file matches. Each expected result counts once, however many
hits match it.

The kind of a question says what its expected results are: ``code`` (code files), ``document``
(Markdown files) or ``symbol`` (definitions, each named by ``symbol``; the text is a name). The
``symbol`` mode takes a name, so only a ``symbol`` question may target it. Every mode a question
targets is run with the same text: a question in natural language targets the ranked modes, an
identifier also ``exact`` and ``symbol``, a pattern ``regex``.

:func:`load_suite` reads and checks a suite: unknown keys, wrong types, a path that is not a file
of its repository, a symbol in a Markdown file, a duplicate id and the like raise
:class:`SuiteError`, which names the file and the place. Whether each expected symbol and heading
is in the index is checked after indexing, by the runner.
"""

import re
import tomllib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast, get_args

from memini.chunking import LANGUAGES as CODE_LANGUAGES
from memini.gitsync.rules import file_kind
from memini.ids import RecordKind, hash_fields
from memini.search import MODES, InvalidSearchError, Mode, SearchFilters, SearchQuery
from memini.workspaces import check_handle

__all__ = [
    "KINDS",
    "LANGUAGES",
    "SUITE_DIGEST_DOMAIN",
    "SUITE_FORMAT",
    "Expected",
    "Question",
    "QuestionKind",
    "QuestionLanguage",
    "Repository",
    "Suite",
    "SuiteError",
    "keep",
    "load_suite",
    "lookup",
]

SUITE_FORMAT: Final = 1
"""The version of the suite file's format this build reads."""

SUITE_DIGEST_DOMAIN: Final = "memini.eval.suite.v1"

QuestionKind = Literal["code", "document", "symbol"]
"""What a question's expected results are: code, Markdown documents, or definitions by name."""
KINDS: Final[tuple[QuestionKind, ...]] = get_args(QuestionKind)

QuestionLanguage = Literal["en", "ru"]
"""The language a question is asked in."""
LANGUAGES: Final[tuple[QuestionLanguage, ...]] = get_args(QuestionLanguage)

_RECORD_KINDS: Final[tuple[RecordKind, ...]] = get_args(RecordKind)

_QUESTION_ID: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_TAG: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class SuiteError(ValueError):
    """The suite file, or one of its fixture repositories, is not valid. The message names the
    file and the place in it."""


@dataclass(frozen=True, slots=True)
class Repository:
    """A fixture repository: files the runner commits to a new git repository and syncs."""

    name: str
    """The name of its repository."""
    workspace: str
    directory: Path
    """Where its files are, absolute."""
    files: tuple[str, ...]
    """Its files, relative POSIX paths, sorted."""


@dataclass(frozen=True, slots=True)
class Expected:
    """A result a question expects: a place in a fixture file. See the module."""

    repository: str
    path: str
    symbol: str | None = None
    heading: str | None = None
    lines: tuple[int, int] | None = None

    def describe(self) -> str:
        """``repo:path#symbol``, ``repo:path#'heading'``, ``repo:path#L3-L9`` or ``repo:path``."""
        place = f"{self.repository}:{self.path}"
        if self.symbol is not None:
            return f"{place}#{self.symbol}"
        if self.heading is not None:
            return f"{place}#'{self.heading}'"
        if self.lines is not None:
            return f"{place}#L{self.lines[0]}-L{self.lines[1]}"
        return place


@dataclass(frozen=True, slots=True)
class Question:
    """A question of the suite, with the results it expects."""

    id: str
    text: str
    language: QuestionLanguage
    kind: QuestionKind
    modes: tuple[Mode, ...]
    """The modes the question is run in, when the run measures them."""
    expect: tuple[Expected, ...]
    filters: SearchFilters
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Suite:
    """A loaded and checked suite."""

    path: Path
    name: str
    version: int
    description: str
    repositories: tuple[Repository, ...]
    questions: tuple[Question, ...]
    digest: str
    """SHA-256 over the suite file and every fixture file, with their paths: a change of either
    changes it, so a report says exactly which suite it measured."""

    @property
    def workspaces(self) -> tuple[str, ...]:
        """The workspaces of the repositories, in order of first use."""
        return tuple(dict.fromkeys(repo.workspace for repo in self.repositories))

    def modes(self) -> tuple[Mode, ...]:
        """The modes some question targets, in the order of :data:`memini.search.MODES`."""
        return keep(MODES, {mode for question in self.questions for mode in question.modes})


# --- Reading TOML values -------------------------------------------------------------------------

type _Table = Mapping[str, object]


class _Reader:
    """Reads one TOML table, with the place it is at for messages."""

    def __init__(self, file: Path, where: str, table: object) -> None:
        self.file = file
        self.where = where
        if not isinstance(table, dict):
            raise self.error("is not a table")
        self.table = cast(_Table, table)
        self.read: set[str] = set()

    def error(self, text: str) -> SuiteError:
        place = f"{self.where}: " if self.where else ""
        return SuiteError(f"{self.file}: {place}{text}")

    def _get(self, key: str, required: bool) -> object:
        self.read.add(key)
        if key not in self.table:
            if required:
                raise self.error(f"{key} is missing")
            return None
        return self.table[key]

    def string(self, key: str, *, required: bool = True) -> str | None:
        value = self._get(key, required)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise self.error(f"{key} must be a non-empty string")
        return value

    def required_string(self, key: str) -> str:
        value = self.string(key)
        if value is None:  # required: _get raised already
            raise self.error(f"{key} is missing")
        return value

    def integer(self, key: str) -> int:
        value = self._get(key, True)
        if not isinstance(value, int) or isinstance(value, bool):
            raise self.error(f"{key} must be an integer")
        return value

    def strings(self, key: str, *, required: bool = False) -> tuple[str, ...] | None:
        value = self._get(key, required)
        if value is None:
            return None
        if not isinstance(value, list):
            raise self.error(f"{key} must be a list of strings")
        items = cast(list[object], value)
        if not all(isinstance(item, str) and item for item in items):
            raise self.error(f"{key} must be a list of non-empty strings")
        found = cast(list[str], items)
        if len(set(found)) != len(found):
            raise self.error(f"{key} names a value twice")
        return tuple(found)

    def tables(self, key: str) -> list[object]:
        value = self._get(key, True)
        if not isinstance(value, list) or not value:
            raise self.error(f"{key} must be a non-empty array of tables")
        return cast(list[object], value)

    def table_value(self, key: str) -> object:
        return self._get(key, False)

    def done(self) -> None:
        unknown = sorted(set(self.table) - self.read)
        if unknown:
            raise self.error(f"unknown key {unknown[0]}")


def lookup[T: str](value: str, choices: tuple[T, ...]) -> T | None:
    """``value`` as one of ``choices``, typed as they are; ``None`` if it is none of them."""
    for choice in choices:
        if value == choice:
            return choice
    return None


def keep[T: str](choices: tuple[T, ...], names: Collection[str]) -> tuple[T, ...]:
    """The ``choices`` that ``names`` holds, in the order of ``choices``, typed as they are."""
    return tuple(choice for choice in choices if choice in names)


def _choice[T: str](reader: _Reader, key: str, choices: tuple[T, ...]) -> T:
    found = lookup(reader.required_string(key), choices)
    if found is None:
        raise reader.error(f"{key} must be one of {', '.join(choices)}")
    return found


# --- The suite -----------------------------------------------------------------------------------


def load_suite(path: Path) -> Suite:
    """Read and check the suite at ``path``. Raises :class:`SuiteError`."""
    file = path.resolve()
    try:
        raw = file.read_bytes()
    except OSError as error:
        raise SuiteError(f"{path}: cannot read the suite: {error.strerror or error}") from None
    try:
        data = tomllib.loads(raw.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SuiteError(f"{path}: not a valid TOML file: {error}") from None
    top = _Reader(file, "", data)
    form = top.integer("format")
    if form != SUITE_FORMAT:
        raise top.error(f"format {form} is not supported; this build reads format {SUITE_FORMAT}")
    name = top.required_string("name")
    version = top.integer("version")
    if version < 1:
        raise top.error("version must be at least 1")
    description = top.string("description", required=False) or ""
    top.read |= {"repository", "question"}
    top.done()  # an unknown key first: a misspelt [[repository]] would read as a missing one

    repositories: dict[str, Repository] = {}
    for number, table in enumerate(top.tables("repository"), start=1):
        repo = _repository(_Reader(file, f"repository {number}", table), file.parent)
        if repo.name in repositories:
            raise SuiteError(f"{file}: repository {number}: the name {repo.name} is used twice")
        repositories[repo.name] = repo

    questions: dict[str, Question] = {}
    for number, table in enumerate(top.tables("question"), start=1):
        question = _question(_Reader(file, f"question {number}", table), repositories)
        if question.id in questions:
            raise SuiteError(f"{file}: question {number}: the id {question.id} is used twice")
        questions[question.id] = question

    digest = _digest(raw, repositories.values())
    return Suite(
        path=file,
        name=name,
        version=version,
        description=description,
        repositories=tuple(repositories.values()),
        questions=tuple(questions.values()),
        digest=digest,
    )


def _repository(reader: _Reader, base: Path) -> Repository:
    name = reader.required_string("name")
    workspace = reader.required_string("workspace")
    try:
        check_handle("repository", name)
        check_handle("workspace", workspace)
    except ValueError as error:
        raise reader.error(str(error)) from None
    relative = reader.required_string("path")
    directory = (base / relative).resolve()
    if not directory.is_dir():
        raise reader.error(f"path {relative} is not a directory")
    reader.done()
    files: list[str] = []
    for item in sorted(directory.rglob("*")):
        rel = item.relative_to(directory).as_posix()
        if item.is_symlink():
            raise reader.error(f"{rel} is a symbolic link; fixture files are regular files")
        if item.is_dir():
            if item.name == ".git":
                raise reader.error(f"{rel} is a git directory; a fixture holds plain files")
            continue
        files.append(rel)
    if not files:
        raise reader.error(f"path {relative} holds no files")
    return Repository(name=name, workspace=workspace, directory=directory, files=tuple(files))


def _question(reader: _Reader, repositories: Mapping[str, Repository]) -> Question:
    qid = reader.required_string("id")
    if _QUESTION_ID.fullmatch(qid) is None:
        raise reader.error(
            "id must be 1 to 64 lowercase letters, digits, '.', '_' or '-', starting with a"
            " letter or digit"
        )
    reader.where = f"question {qid}"
    text = reader.required_string("text")
    language: QuestionLanguage = _choice(reader, "language", LANGUAGES)
    kind: QuestionKind = _choice(reader, "kind", KINDS)
    mode_names = reader.strings("modes", required=True) or ()
    if not mode_names:
        raise reader.error("modes must name at least one mode")
    modes: list[Mode] = []
    for mode_name in mode_names:
        mode: Mode | None = lookup(mode_name, MODES)
        if mode is None:
            raise reader.error(f"unknown mode {mode_name}; expected some of {', '.join(MODES)}")
        modes.append(mode)
    if "symbol" in modes and kind != "symbol":
        raise reader.error("only a question of kind symbol may target the symbol mode")
    tags = reader.strings("tags") or ()
    for tag in tags:
        if _TAG.fullmatch(tag) is None:
            raise reader.error(f"tag {tag!r} is not 1 to 32 lowercase letters, digits or '-'")
    filters = _filters(reader, repositories)
    for mode in modes:
        try:
            SearchQuery(text=text, mode=mode, filters=filters)
        except InvalidSearchError as error:
            raise reader.error(f"text: {error}") from None

    expect: list[Expected] = []
    for number, table in enumerate(reader.tables("expect"), start=1):
        item = _expected(_Reader(reader.file, f"{reader.where}: expect {number}", table), kind)
        repo = repositories.get(item.repository)
        if repo is None:
            raise reader.error(f"expect {number}: no repository named {item.repository}")
        if item.path not in repo.files:
            raise reader.error(
                f"expect {number}: {item.path} is not a file of repository {item.repository}"
            )
        _check_expected(reader, number, item, kind, repo)
        if item in expect:
            raise reader.error(f"expect {number}: {item.describe()} is expected twice")
        expect.append(item)
    reader.done()
    return Question(
        id=qid,
        text=text,
        language=language,
        kind=kind,
        modes=tuple(modes),
        expect=tuple(expect),
        filters=filters,
        tags=tags,
    )


_FILTER_KEYS: Final = ("workspaces", "repositories", "sources", "kinds", "languages", "paths")


def _filters(reader: _Reader, repositories: Mapping[str, Repository]) -> SearchFilters:
    value = reader.table_value("filters")
    if value is None:
        return SearchFilters()
    table = _Reader(reader.file, f"{reader.where}: filters", value)
    found = {key: table.strings(key) for key in _FILTER_KEYS}
    table.done()
    known = {
        "workspaces": {repo.workspace for repo in repositories.values()},
        "repositories": set(repositories),
        "sources": set(repositories),
    }
    for key, names in known.items():
        for item in found[key] or ():
            if item not in names:
                raise table.error(f"{key} names {item}, which the suite does not define")
    try:
        return SearchFilters(
            workspaces=found["workspaces"],
            repositories=found["repositories"],
            sources=found["sources"],
            kinds=_pick(table, "kinds", found["kinds"], _RECORD_KINDS),
            languages=_pick(table, "languages", found["languages"], CODE_LANGUAGES),
            paths=found["paths"] or (),
        )
    except InvalidSearchError as error:
        raise table.error(str(error)) from None


def _pick[T: str](
    reader: _Reader, key: str, values: tuple[str, ...] | None, allowed: tuple[T, ...]
) -> tuple[T, ...] | None:
    if values is None:
        return None
    picked: list[T] = []
    for value in values:
        match = lookup(value, allowed)
        if match is None:
            raise reader.error(f"{key} names {value}; expected some of {', '.join(allowed)}")
        picked.append(match)
    return tuple(picked)


def _expected(reader: _Reader, kind: QuestionKind) -> Expected:
    repository = reader.required_string("repository")
    path = reader.required_string("path")
    symbol = reader.string("symbol", required=False)
    heading = reader.string("heading", required=False)
    lines: tuple[int, int] | None = None
    raw_lines = reader.table_value("lines")
    if raw_lines is not None:
        items = cast(list[object], raw_lines) if isinstance(raw_lines, list) else []
        if (
            len(items) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in items)
            or not 1 <= cast(int, items[0]) <= cast(int, items[1])
        ):
            raise reader.error("lines must be [first, last], 1-based, first <= last")
        lines = (cast(int, items[0]), cast(int, items[1]))
    reader.done()
    if sum(item is not None for item in (symbol, heading, lines)) > 1:
        raise reader.error("give at most one of symbol, heading and lines")
    if heading is not None and ", " in heading:
        raise reader.error("a heading with ', ' cannot be told apart in a chunk's header")
    if kind == "symbol" and symbol is None:
        raise reader.error("a question of kind symbol expects definitions: give symbol")
    return Expected(repository, path, symbol, heading, lines)


def _check_expected(
    reader: _Reader, number: int, item: Expected, kind: QuestionKind, repo: Repository
) -> None:
    document = file_kind(item.path) == "document"
    where = f"expect {number}"
    if item.symbol is not None and document:
        raise reader.error(f"{where}: {item.path} is a Markdown document; it has no symbols")
    if item.heading is not None and not document:
        raise reader.error(f"{where}: {item.path} is not a Markdown document; it has no headings")
    if kind == "document" and not document:
        raise reader.error(f"{where}: a question of kind document expects Markdown documents")
    if kind != "document" and document:
        raise reader.error(f"{where}: a question of kind {kind} expects code files")
    if item.lines is not None:
        count = len((repo.directory / item.path).read_bytes().splitlines())
        if item.lines[1] > count:
            raise reader.error(f"{where}: {item.path} has {count} lines, fewer than {item.lines}")


def _digest(raw: bytes, repositories: Iterable[Repository]) -> str:
    fields: list[bytes] = [raw]
    for repo in repositories:
        for rel in repo.files:
            fields += [repo.name.encode(), rel.encode(), (repo.directory / rel).read_bytes()]
    return hash_fields(SUITE_DIGEST_DOMAIN, *fields)
