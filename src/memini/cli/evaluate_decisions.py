"""``memini eval decisions``: run the decision eval and compare it with a baseline.

A thin wrapper of :func:`memini.evaluation.decisions.run_decision_eval`; the sets, the matching
and the metrics are described there. The command loads the sets and the baseline first, so a bad
file is refused before anything runs; builds the extractor named by ``--extractor`` (a stub
needs nothing else); runs; prints the three metrics and the groups to stdout and progress to
stderr; writes the JSON report and the baseline when asked; and compares with the baseline.

The stubs read the sets' files only: no database, no model. The ``llm`` extractor takes its
provider from the service's settings, which every operator command loads, and a scratch database
on the server of ``MEMINI_DATABASE_URL`` (the role needs ``CREATEDB``) for the policies the LLM
gate reads (:mod:`memini.evaluation.decisions.llm`).

``--candidates`` shows each supersession check the top ``--candidate-limit`` active items as the
``extract`` job ranks them (``lexical``, ``dense``, ``hybrid``) instead of every active item
(``all``, the default): :mod:`memini.evaluation.decisions.candidates`. ``lexical`` takes a scratch
database of its own; ``dense`` and ``hybrid`` the embedding provider of the settings.

Exit codes (besides those of every command): 0 when nothing fell; 1 when the extractor or the
classifier failed; 2 for a set or baseline file that is not valid; :data:`EXIT_REGRESSION` when
a tracked metric fell below the baseline by more than the margin.
"""

import argparse
from pathlib import Path
from typing import Final

from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, message
from memini.cli.evaluate import EXIT_REGRESSION, parse_margin
from memini.config import Settings
from memini.evaluation.decisions import (
    DEFAULT_EXTRACTOR,
    DEFAULT_SETS,
    EXTRACTORS,
    DecisionEvalError,
    DecisionReport,
    DecisionReportError,
    DecisionSetError,
    VariantContext,
    compare,
    format_comparison,
    format_report,
    load_sets,
    read_report,
    run_decision_eval,
    write_report,
)
from memini.evaluation.decisions.candidates import SELECTIONS, Selection, create_candidates

__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_MARGIN",
    "DESCRIPTION",
    "HELP",
    "configure_parser",
    "decisions_command",
]

DEFAULT_BASELINE: Final = Path("eval/decisions/baseline-keyword.json")
"""The committed baseline: the ``keyword`` stub on the default sets."""
DEFAULT_MARGIN: Final = 0.02

HELP: Final = "run the decision eval on the labelled threads and meetings"
DESCRIPTION: Final = (
    "Extract decisions and open questions from the labelled synthetic threads and meetings of"
    " the sets with the extractor named, match them with the labels, replay the supersession"
    " check, and print extraction precision and recall and supersession accuracy, per kind,"
    " language and set; compare with a baseline. The stubs read files only. Exit codes: 0 no"
    " regression, 1 the extractor failed, 2 an invalid set or baseline,"
    f" {EXIT_REGRESSION} a metric fell below the baseline by more than the margin."
)


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError("a candidate limit is from 1 to 1000")
    return limit


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        dest="sets",
        metavar="DIR",
        type=Path,
        action="append",
        help="a set directory (batches/ and labels/decisions.json); repeat for several"
        f" (default: {', '.join(str(path) for path in DEFAULT_SETS)}, from the repository root)",
    )
    parser.add_argument(
        "--extractor",
        choices=sorted(EXTRACTORS),
        default=DEFAULT_EXTRACTOR,
        help="what extracts and classifies: "
        + "; ".join(f"{name}: {v.description}" for name, v in sorted(EXTRACTORS.items()))
        + f" (default: {DEFAULT_EXTRACTOR})",
    )
    parser.add_argument(
        "--candidates",
        choices=SELECTIONS,
        default="all",
        help="what each supersession check is shown: all, every active item (default); lexical,"
        " dense or hybrid, the top --candidate-limit items as the extract job ranks them"
        " (dense and hybrid need an embedding provider)",
    )
    parser.add_argument(
        "--candidate-limit",
        metavar="K",
        type=_limit,
        help="how many active items of other records --candidates shows (default: the"
        " extraction_candidates setting)",
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        type=Path,
        help=f"a report to compare with (the committed one: {DEFAULT_BASELINE}); with"
        " --write-baseline, where to write this run's",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="write this run's report to --baseline instead of comparing with it",
    )
    parser.add_argument(
        "--margin",
        type=parse_margin,
        default=DEFAULT_MARGIN,
        help=f"how far a metric may fall below the baseline (default: {DEFAULT_MARGIN})",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        type=Path,
        help="also write the report as JSON to PATH (- for stdout, instead of the table)",
    )
    parser.set_defaults(command=decisions_command)


async def decisions_command(args: argparse.Namespace, settings: Settings) -> int:
    roots: list[Path] = args.sets or list(DEFAULT_SETS)
    baseline_path: Path | None = args.baseline
    write_baseline: bool = args.write_baseline
    json_path: Path | None = args.json
    if write_baseline and baseline_path is None:
        raise CommandError("--write-baseline needs --baseline PATH", EXIT_USAGE)
    try:
        sets = load_sets(roots)
    except DecisionSetError as error:
        raise CommandError(f"invalid set: {error}", EXIT_USAGE) from None
    baseline: DecisionReport | None = None
    if baseline_path is not None and not write_baseline:
        try:
            baseline = read_report(baseline_path)
        except DecisionReportError as error:
            raise CommandError(f"invalid baseline: {error}", EXIT_USAGE) from None

    selection: Selection = args.candidates
    limit: int = args.candidate_limit or settings.extraction_candidates
    variant = EXTRACTORS[args.extractor]
    message(
        f"decision eval of {len(sets)} sets ({sum(len(s.records) for s in sets)} threads and"
        f" meetings, {sum(len(s.items) for s in sets)} labelled items) with {variant.name}"
    )
    try:
        candidates = create_candidates(selection, limit, settings)
    except DecisionEvalError as error:
        raise CommandError(str(error)) from None
    try:
        pipeline = variant.create(VariantContext(sets, settings))
    except DecisionEvalError as error:
        if candidates is not None:
            await candidates.aclose()
        raise CommandError(str(error)) from None
    try:
        report = await run_decision_eval(
            sets, pipeline, variant=variant.name, say=message, candidates=candidates
        )
    except DecisionEvalError as error:
        raise CommandError(str(error)) from None
    finally:
        try:
            if pipeline.aclose is not None:
                await pipeline.aclose()
        finally:
            if candidates is not None:
                await candidates.aclose()
    if pipeline.summary is not None:
        message(pipeline.summary())

    to_stdout = json_path is not None and str(json_path) == "-"
    if not to_stdout:
        print(format_report(report))
    if json_path is not None:
        write_report(json_path, report)
        if not to_stdout:
            message(f"wrote the report to {json_path}")
    if write_baseline and baseline_path is not None:
        write_report(baseline_path, report)
        message(f"wrote the baseline to {baseline_path}")
        return EXIT_OK
    if baseline is None or baseline_path is None:
        return EXIT_OK
    comparison = compare(report, baseline, margin=args.margin)
    text = format_comparison(comparison, str(baseline_path))
    if to_stdout:
        message(text)
    else:
        print(f"\n{text}")
    return EXIT_REGRESSION if comparison.regressions else EXIT_OK
