"""``memini eval``: run the retrieval eval and compare it with a baseline.

A thin wrapper of :func:`memini.evaluation.run_eval`; the suite format, the scratch index and the
metrics are described there. The command loads the suite and the baseline first, so a bad file is
refused before anything is built; builds the embedder (the configured one, or the FakeEmbedder
with ``--fake-embeddings``); runs; prints the table to stdout and progress to stderr; writes the
JSON report and the baseline when asked; and compares with the baseline.

Exit codes (besides those of every command): 0 when nothing fell; 1 when a search failed or an
expected result is not in the index; 2 for a suite or baseline file that is not valid, and for
an embedding provider that is not set without ``--fake-embeddings``; :data:`EXIT_REGRESSION`
when a tracked metric fell below the baseline by more than the margin.
"""

import argparse
from pathlib import Path
from typing import Final

from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CommandError, DatabaseUrl, message
from memini.config import Settings
from memini.embeddings import Embedder, FakeEmbedder, create_embedder
from memini.evaluation import (
    LEXICAL_VARIANTS,
    RERANKERS,
    EvalConfig,
    EvalSetupError,
    ExpectationError,
    Report,
    ReportError,
    SuiteError,
    compare,
    format_comparison,
    format_report,
    load_suite,
    read_report,
    run_eval,
    write_report,
)
from memini.evaluation.suite import keep
from memini.rerank import RerankError
from memini.search import MAX_LIMIT, MODES, Mode, SearchOptions

__all__ = [
    "DEFAULT_MARGIN",
    "DEFAULT_SUITE",
    "EXIT_REGRESSION",
    "FAKE_MODES",
    "eval_command",
    "parse_ks",
    "parse_margin",
    "parse_modes",
]

EXIT_REGRESSION: Final = 4
"""A tracked metric fell below the baseline by more than the margin."""

DEFAULT_SUITE: Final = Path("eval/code/suite.toml")
"""The code suite of this repository, relative to its root."""
DEFAULT_MARGIN: Final = 0.02

FAKE_MODES: Final[tuple[Mode, ...]] = ("lexical", "exact", "regex", "symbol")
"""The modes measured by default with the FakeEmbedder: those its vectors do not take part in."""


def parse_ks(value: str) -> tuple[int, ...]:
    """``1,5,10``: distinct values from 1 to :data:`MAX_LIMIT`, sorted."""
    try:
        ks = sorted({int(part) for part in value.split(",") if part.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError(
            "k is a comma-separated list of numbers, e.g. 1,5,10"
        ) from None
    if not ks or ks[0] < 1 or ks[-1] > MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"each k is from 1 to {MAX_LIMIT}")
    return tuple(ks)


def parse_modes(value: str) -> tuple[Mode, ...]:
    """``lexical,exact`` or ``all``: modes of :data:`memini.search.MODES`, in that order."""
    names = {part.strip() for part in value.split(",") if part.strip()}
    if names == {"all"}:
        return MODES
    unknown = names - set(MODES)
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"modes are some of {', '.join(MODES)}, or all")
    return keep(MODES, names)


def parse_margin(value: str) -> float:
    try:
        margin = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("the margin is a number, e.g. 0.02") from None
    if not 0 <= margin < 1:
        raise argparse.ArgumentTypeError("the margin is from 0 to below 1")
    return margin


async def eval_command(args: argparse.Namespace, settings: Settings) -> int:
    suite_path: Path = args.suite
    baseline_path: Path | None = args.baseline
    write_baseline: bool = args.write_baseline
    json_path: Path | None = args.json
    fake: bool = args.fake_embeddings
    if write_baseline and baseline_path is None:
        raise CommandError("--write-baseline needs --baseline PATH", EXIT_USAGE)
    try:
        suite = load_suite(suite_path)
    except SuiteError as error:
        raise CommandError(f"invalid suite: {error}", EXIT_USAGE) from None
    baseline: Report | None = None
    if baseline_path is not None and not write_baseline:
        try:
            baseline = read_report(baseline_path)
        except ReportError as error:
            raise CommandError(f"invalid baseline: {error}", EXIT_USAGE) from None

    used = suite.modes()
    requested: tuple[Mode, ...] | None = args.modes
    modes: tuple[Mode, ...] = keep(requested or (FAKE_MODES if fake else MODES), used)
    if not modes:
        raise CommandError("no question of the suite targets the modes asked for", EXIT_USAGE)

    reranker_variant = RERANKERS[args.reranker]
    reranker = None if reranker_variant.create is None else reranker_variant.create(settings)
    if reranker is not None:
        try:
            await reranker.load()  # a missing model stops the run: search would fall back
        except RerankError as error:
            raise CommandError(f"the reranker cannot run: {error}") from None
    embedder: Embedder = FakeEmbedder() if fake else create_embedder(settings)
    config = EvalConfig(
        modes=modes,
        ks=args.ks,
        options=SearchOptions.from_settings(settings),
        lexical=LEXICAL_VARIANTS[args.lexical],
        reranker_name=reranker_variant.name
        + ("" if reranker is None else f" ({reranker.describe()})"),
        reranker=reranker,
        query_prefix="" if fake else settings.embedding_query_prefix,
    )
    database = DatabaseUrl(settings)
    message(
        f"eval of suite {suite.name} v{suite.version} ({len(suite.questions)} questions) with"
        f" {embedder.spec.describe()}{' (FakeEmbedder)' if fake else ''}, on the server at"
        f" {database.target}"
    )
    try:
        report = await run_eval(
            suite,
            embedder,
            config,
            server_url=settings.database_url.get_secret_value(),
            keep=args.keep,
            say=message,
        )
    except EvalSetupError as error:
        raise CommandError(str(error)) from None
    except ExpectationError as error:
        for problem in error.problems:
            message(f"not in the index: {problem}")
        raise CommandError(
            f"{len(error.problems)} expected results of the suite are not in the index; fix"
            " the suite, or the chunker that lost them"
        ) from None
    finally:
        await embedder.aclose()
        if reranker is not None:
            await reranker.aclose()

    to_stdout = json_path is not None and str(json_path) == "-"
    if not to_stdout:
        print(format_report(report))
    if json_path is not None:
        write_report(json_path, report)
        if not to_stdout:
            message(f"wrote the report to {json_path}")
    regressed = False
    if write_baseline and baseline_path is not None:
        write_report(baseline_path, report)
        message(f"wrote the baseline to {baseline_path}")
    elif baseline is not None and baseline_path is not None:
        comparison = compare(report, baseline, margin=args.margin)
        text = format_comparison(comparison, str(baseline_path))
        if to_stdout:
            message(text)
        else:
            print(f"\n{text}")
        regressed = bool(comparison.regressions)
    if report.errors:
        message(f"{len(report.errors)} searches failed; their questions count as not found")
        return EXIT_FAILURE
    return EXIT_REGRESSION if regressed else EXIT_OK
