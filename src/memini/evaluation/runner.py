"""Running a suite: a scratch index of the fixture repositories, every question in every mode.

:func:`run_eval` does, in order:

1. Commits each fixture repository to a new git repository in a temporary directory, with fixed
   author, committer and dates, so its commit is the same on every run.
2. Creates a scratch database on the configured Postgres server (``memini_eval_`` and 12 random
   hex digits, from ``template0``) and migrates it. The user's database is only connected to for
   ``CREATE DATABASE``; nothing in it is read or written. The role needs ``CREATEDB``.
3. Adds the suite's workspaces and one git source per repository, and syncs each through the
   service's own path (:func:`memini.gitsync.run_git_sync`: mirror, file rules, chunkers, index
   writer) with the embedder given. The mirrors live in the temporary directory, and only it is
   an allowed local root: no setting of the user's git connector is used.
4. Runs the lexical variant's ``prepare``, then resolves every expected result against the index
   (:class:`Target`); one that the index does not hold, a symbol the chunker did not find say,
   stops the run with :class:`ExpectationError`.
5. Searches each question in each mode it targets that the run measures, through
   :func:`memini.search.search` with the run's options, lexical statement and reranker, and
   computes recall at each k and the reciprocal rank (:mod:`memini.evaluation.metrics`), and
   their means per group of questions.
6. Drops the database and removes the temporary directory, unless ``keep``.

A search that fails (a timeout, a pattern Postgres refuses) is recorded with the question as an
error, with metrics of 0; the run goes on.
"""

import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

from memini import __version__
from memini.auth import Principal, WriteAccess, authorize_read
from memini.db import Pool, connect, migrate, open_pool
from memini.embeddings import Embedder, FakeEmbedder
from memini.evaluation.metrics import Ranked, first_ranks, mean, recall_at_k, reciprocal_rank
from memini.evaluation.report import GroupSummary, ModeResult, QuestionResult, Report
from memini.evaluation.suite import KINDS, LANGUAGES, Expected, Question, Suite
from memini.evaluation.variants import (
    DEFAULT_LEXICAL,
    DEFAULT_RERANKER,
    LEXICAL_VARIANTS,
    LexicalVariant,
)
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, run_git_sync
from memini.index import current_fingerprint
from memini.jobs import Job
from memini.search import (
    MAX_LIMIT,
    RANKED_MODES,
    Hit,
    InvalidSearchError,
    Mode,
    Reranker,
    SearchOptions,
    SearchQuery,
    SearchTimeoutError,
    search,
)
from memini.sources import NewGitSource, create_git_source
from memini.workspaces import create_workspace, ensure_principal

__all__ = [
    "DEFAULT_KS",
    "SCRATCH_PREFIX",
    "EvalConfig",
    "EvalSetupError",
    "ExpectationError",
    "Target",
    "header_trails",
    "hit_matches",
    "run_eval",
    "summarize",
]

type Conn = AsyncConnection[TupleRow]

DEFAULT_KS: Final = (1, 5, 10)
SCRATCH_PREFIX: Final = "memini_eval_"
"""The name of every scratch database starts with this."""
PRINCIPAL: Final = "eval"
REF: Final = "main"
COMMIT_DATE: Final = "2026-01-01T12:00:00+00:00"


class EvalSetupError(Exception):
    """The eval cannot build its scratch index: no git, no right to create a database."""


class ExpectationError(Exception):
    """The suite expects results that the index does not hold. ``problems`` lists them."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(
            f"{len(problems)} expected results are not in the index: " + "; ".join(problems)
        )
        self.problems = tuple(problems)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvalConfig:
    """What a run measures, and with what (besides the embedder)."""

    modes: tuple[Mode, ...]
    ks: tuple[int, ...] = DEFAULT_KS
    options: SearchOptions = field(default_factory=SearchOptions)
    lexical: LexicalVariant = field(default_factory=lambda: LEXICAL_VARIANTS[DEFAULT_LEXICAL])
    reranker_name: str = DEFAULT_RERANKER
    reranker: Reranker | None = None
    query_prefix: str = ""
    """The query prefix the embedder adds, recorded in the report: it is not part of the
    embedding spec, but it changes what dense search finds."""

    def __post_init__(self) -> None:
        if not self.modes:
            raise ValueError("an eval measures at least one mode")
        if not self.ks or list(self.ks) != sorted(set(self.ks)):
            raise ValueError("k values are distinct and ascending")
        if self.ks[0] < 1 or self.ks[-1] > MAX_LIMIT:
            raise ValueError(f"k is from 1 to {MAX_LIMIT}")

    @property
    def depth(self) -> int:
        """Hits fetched per search: the largest k, or the hybrid candidates if more, so that a
        tie at the cutoff is seen whole and a hybrid search fuses what the service's would."""
        return min(MAX_LIMIT, max(self.ks[-1], self.options.candidates))


# --- Matching hits with expected results ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """An expected result, resolved against the index: what a hit must be to match it."""

    expected: Expected
    spans: tuple[tuple[int, int], ...] | None = None
    """Line spans a hit must overlap; ``None``: any line of the file."""


def header_trails(header: str, path: str | None) -> tuple[str, ...]:
    """The names a chunk's header lists after its path: qualified names of code, heading trails
    of a document (``Install > Docker``)."""
    if path is None or not header.startswith(f"{path}: "):
        return ()
    return tuple(header[len(path) + 2 :].split(", "))


def _trail_ends_with(trail: str, heading: str) -> bool:
    return trail == heading or trail.endswith(f" > {heading}")


def hit_matches(hit: Hit, target: Target) -> bool:
    """Whether ``hit`` shows the expected result ``target`` (see :mod:`memini.evaluation.suite`)."""
    expected = target.expected
    if hit.source != expected.repository or hit.path != expected.path:
        return False
    if expected.heading is not None:
        return any(
            _trail_ends_with(t, expected.heading) for t in header_trails(hit.header, hit.path)
        )
    if target.spans is None:
        return True
    if hit.definition is not None:
        start, end = hit.definition.start_line, hit.definition.end_line
    else:
        start, end = hit.start_line, hit.end_line
    if start is None or end is None:
        return False
    return any(start <= last and first <= end for first, last in target.spans)


async def _resolve(conn: Conn, item: Expected) -> Target | str:
    """The target of ``item``, or why the index cannot hold it."""
    cur = await conn.execute(
        "SELECT c.header FROM chunk c"
        " JOIN record r ON r.id = c.record_id JOIN source s ON s.id = c.source_id"
        " WHERE s.name = %s AND c.path = %s AND r.deleted_at IS NULL",
        (item.repository, item.path),
    )
    chunks = await cur.fetchall()
    if not chunks:
        return f"{item.describe()}: the file has no chunks (excluded, binary or empty)"
    if item.symbol is not None:
        cur = await conn.execute(
            "SELECT DISTINCT s.start_line, s.end_line FROM symbol s"
            " JOIN source src ON src.id = s.source_id"
            " WHERE src.name = %s AND s.path = %s AND s.qualified_name = %s"
            " ORDER BY s.start_line, s.end_line",
            (item.repository, item.path, item.symbol),
        )
        spans = tuple((row[0], row[1]) for row in await cur.fetchall())
        if not spans:
            return f"{item.describe()}: the symbol table has no definition of that name in the file"
        return Target(item, spans)
    if item.heading is not None:
        heading = item.heading
        if not any(
            _trail_ends_with(trail, heading)
            for (header,) in chunks
            for trail in header_trails(header, item.path)
        ):
            return f"{item.describe()}: no section of the file has that heading"
        return Target(item)
    if item.lines is not None:
        return Target(item, (item.lines,))
    return Target(item)


# --- Building the scratch index ------------------------------------------------------------------


def _git(directory: Path, *args: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(directory),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Memini Eval",
        "GIT_AUTHOR_EMAIL": "eval@example.invalid",
        "GIT_AUTHOR_DATE": COMMIT_DATE,
        "GIT_COMMITTER_NAME": "Memini Eval",
        "GIT_COMMITTER_EMAIL": "eval@example.invalid",
        "GIT_COMMITTER_DATE": COMMIT_DATE,
        "LC_ALL": "C",
    }
    try:
        subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
            cwd=directory,
            env=env,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip().splitlines()
        raise EvalSetupError(
            f"git {args[0]} failed for a fixture repository: {detail[-1] if detail else error}"
        ) from None
    except subprocess.TimeoutExpired:
        raise EvalSetupError(f"git {args[0]} timed out for a fixture repository") from None


def _commit_fixtures(suite: Suite, root: Path) -> dict[str, Path]:
    """Commit each fixture repository to a new git repository under ``root``."""
    locations: dict[str, Path] = {}
    for repo in suite.repositories:
        target = root / repo.name
        target.mkdir(parents=True)
        for rel in repo.files:
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo.directory / rel, destination)
        _git(target, "init", "--quiet", f"--initial-branch={REF}")
        _git(target, "add", "--all", "--force")
        _git(target, "commit", "--quiet", "-m", f"fixture {repo.name}")
        locations[repo.name] = target
    return locations


@asynccontextmanager
async def _scratch_database(server_url: str, *, keep: bool) -> AsyncGenerator[tuple[str, str]]:
    """A new, empty database on the server of ``server_url``: its name and connection string.
    Dropped on exit unless ``keep``."""
    name = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"
    async with connect(server_url, autocommit=True) as admin:
        try:
            await admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
        except errors.InsufficientPrivilege:
            raise EvalSetupError(
                "the eval builds its index in a scratch database, and the database role may not"
                " create one: grant it CREATEDB, or run the eval as a role that has it"
            ) from None
    try:
        yield name, make_conninfo(server_url, dbname=name)
    finally:
        if not keep:
            async with connect(server_url, autocommit=True) as admin:
                await admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


async def _build_index(
    conn: Conn,
    pool: Pool,
    embedder: Embedder,
    suite: Suite,
    locations: Mapping[str, Path],
    mirror_dir: Path,
) -> tuple[uuid.UUID, int, int]:
    """Workspaces, sources and their sync; returns the principal and the counts of live records
    and chunks."""
    options = GitOptions(mirror_dir=mirror_dir, local_roots=(mirror_dir.parent,))
    owner = await ensure_principal(conn, PRINCIPAL)
    for workspace in suite.workspaces:
        await create_workspace(conn, owner_id=owner, name=workspace)
    access = WriteAccess.for_owner(owner)
    mirrors = MirrorManager(options)
    for repo in suite.repositories:
        source = await create_git_source(
            conn,
            access,
            repo.workspace,
            NewGitSource(name=repo.name, location=str(locations[repo.name]), refs=(REF,)),
            options=options,
        )
        job = Job(
            id=uuid.uuid4(),
            workspace_id=source.workspace_id,
            owner_id=owner,
            source_id=source.id,
            stream_id=None,
            kind=GIT_SYNC,
            args={},
            attempt=1,
            max_attempts=1,
            worker="eval",
        )
        await run_git_sync(pool, mirrors, embedder, job)
    cur = await conn.execute(
        "SELECT (SELECT count(*) FROM record WHERE deleted_at IS NULL),"
        " (SELECT count(*) FROM chunk)"
    )
    row = await cur.fetchone()
    records, chunks = (0, 0) if row is None else (int(row[0]), int(row[1]))
    return owner, records, chunks


# --- Running -------------------------------------------------------------------------------------


def _quiet(text: str) -> None:
    del text


async def run_eval(
    suite: Suite,
    embedder: Embedder,
    config: EvalConfig,
    *,
    server_url: str,
    keep: bool = False,
    say: Callable[[str], None] = _quiet,
) -> Report:
    """Run ``suite`` with ``embedder`` and ``config``; see the module.

    ``server_url`` is a connection string of the Postgres server to create the scratch database
    on; its own database is not touched. ``say`` gets a line on each step.

    Raises:
        EvalSetupError: git is missing or failed, or the role may not create a database.
        ExpectationError: an expected result is not in the index.
        EmbeddingError: the embedding model failed.
        psycopg.Error: the server failed.
    """
    if shutil.which("git") is None:
        raise EvalSetupError("the eval needs the git executable on PATH")
    work = Path(tempfile.mkdtemp(prefix="memini-eval-")).resolve()
    try:
        locations = await anyio.to_thread.run_sync(_commit_fixtures, suite, work / "repos")
        async with _scratch_database(server_url, keep=keep) as (name, url):
            say(f"scratch database {name}" + (" (kept)" if keep else ""))
            await migrate(url)
            async with (
                connect(url, autocommit=True) as conn,
                open_pool(url, min_size=1, max_size=4, timeout=30) as pool,
            ):
                owner, records, chunks = await _build_index(
                    conn, pool, embedder, suite, locations, work / "mirrors"
                )
                say(
                    f"indexed {len(suite.repositories)} repositories: {records} files,"
                    f" {chunks} chunks, with {embedder.spec.describe()}"
                )
                if config.lexical.prepare is not None:
                    await config.lexical.prepare(conn)
                targets = await _targets(conn, suite)
                say(f"running {len(suite.questions)} questions in {', '.join(config.modes)}")
                async with pool.connection() as search_conn:
                    modes: dict[Mode, ModeResult] = {
                        mode: await _run_mode(
                            search_conn, embedder, config, suite, targets, owner, mode
                        )
                        for mode in config.modes
                    }
    finally:
        if keep:
            say(f"kept the fixture repositories and mirrors in {work}")
        else:
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(work, ignore_errors=True))
    return Report(
        setup=_setup(suite, embedder, config),
        ks=config.ks,
        depth=config.depth,
        modes=modes,
        notes=_notes(embedder, config),
    )


async def _targets(conn: Conn, suite: Suite) -> dict[str, tuple[Target, ...]]:
    targets: dict[str, tuple[Target, ...]] = {}
    problems: list[str] = []
    resolved: dict[Expected, Target | str] = {}
    for question in suite.questions:
        found: list[Target] = []
        for item in question.expect:
            if item not in resolved:
                resolved[item] = await _resolve(conn, item)
            target = resolved[item]
            if isinstance(target, str):
                problems.append(f"question {question.id}: {target}")
            else:
                found.append(target)
        targets[question.id] = tuple(found)
    if problems:
        raise ExpectationError(problems)
    return targets


async def _run_mode(
    conn: Conn,
    embedder: Embedder,
    config: EvalConfig,
    suite: Suite,
    targets: Mapping[str, tuple[Target, ...]],
    owner: uuid.UUID,
    mode: Mode,
) -> ModeResult:
    principal = Principal(principal_id=owner, token_id=uuid.UUID(int=0), scopes=frozenset({"read"}))
    access = authorize_read(principal)
    results: dict[str, QuestionResult] = {}
    for question in suite.questions:
        if mode not in question.modes:
            continue
        expected = targets[question.id]
        query = SearchQuery(
            text=question.text, mode=mode, limit=config.depth, filters=question.filters
        )
        try:
            hits = await search(
                conn,
                embedder,
                access,
                query,
                options=config.options,
                reranker=config.reranker,
                lexical=config.lexical.statement,
            )
        except (InvalidSearchError, SearchTimeoutError) as error:
            results[question.id] = QuestionResult(
                recall={k: 0.0 for k in config.ks},
                rr=0.0,
                ranks=(None,) * len(expected),
                error=str(error),
            )
            continue
        ranked = [
            Ranked(
                matches=frozenset(
                    i for i, target in enumerate(expected) if hit_matches(hit, target)
                ),
                score=hit.score if mode in RANKED_MODES else None,
            )
            for hit in hits
        ]
        results[question.id] = QuestionResult(
            recall={k: recall_at_k(ranked, len(expected), k) for k in config.ks},
            rr=reciprocal_rank(ranked, depth=config.ks[-1]),
            ranks=first_ranks(ranked, len(expected)),
        )
    return ModeResult(groups=summarize(suite.questions, results, config.ks), questions=results)


def summarize(
    questions: Sequence[Question], results: Mapping[str, QuestionResult], ks: Sequence[int]
) -> dict[str, GroupSummary]:
    """Means of the results over ``all`` questions, each ``kind:``, ``language:`` and ``tag:``.
    Questions without a result (not run in the mode) are left out; so is an empty group."""
    members: dict[str, list[QuestionResult]] = {"all": []}
    for kind in KINDS:
        members[f"kind:{kind}"] = []
    for language in LANGUAGES:
        members[f"language:{language}"] = []
    for tag in sorted({tag for question in questions for tag in question.tags}):
        members[f"tag:{tag}"] = []
    for question in questions:
        result = results.get(question.id)
        if result is None:
            continue
        for group in (
            "all",
            f"kind:{question.kind}",
            f"language:{question.language}",
            *(f"tag:{tag}" for tag in question.tags),
        ):
            members[group].append(result)
    return {
        group: GroupSummary(
            n=len(found),
            recall={k: mean([r.recall[k] for r in found]) for k in ks},
            mrr=mean([r.rr for r in found]),
        )
        for group, found in members.items()
        if found
    }


def _setup(suite: Suite, embedder: Embedder, config: EvalConfig) -> dict[str, Any]:
    spec = embedder.spec
    fingerprint = current_fingerprint(spec)
    fake = isinstance(embedder, FakeEmbedder)
    return {
        "memini": __version__,
        "suite": {
            "name": suite.name,
            "version": suite.version,
            "digest": suite.digest,
            "questions": len(suite.questions),
            "repositories": len(suite.repositories),
        },
        "embedding": {
            "provider": spec.provider,
            "model": spec.model,
            "dimension": spec.dimension,
            "digest": spec.digest,
            "pooling": spec.pooling,
            "max_tokens": spec.max_tokens,
            "document_prefix": spec.document_prefix,
            "query_prefix": config.query_prefix,
            "fake": fake,
        },
        "embedding_description": spec.describe() + (" [FakeEmbedder]" if fake else ""),
        "embedding_key": fingerprint.embedding_key,
        "index_fingerprint": fingerprint.digest,
        "chunker": fingerprint.chunker_version,
        "lexical": config.lexical.name,
        "reranker": config.reranker_name,
        "search": {
            "candidates": config.options.candidates,
            "rrf_k": config.options.rrf_k,
            "rerank_depth": config.options.rerank_depth,
        },
    }


def _notes(embedder: Embedder, config: EvalConfig) -> tuple[str, ...]:
    notes: list[str] = []
    meaningless = [mode for mode in config.modes if mode in ("dense", "hybrid")]
    if isinstance(embedder, FakeEmbedder) and meaningless:
        notes.append(
            f"the FakeEmbedder's vectors carry no meaning: {' and '.join(meaningless)} results"
            " measure nothing about retrieval"
        )
    return tuple(notes)
