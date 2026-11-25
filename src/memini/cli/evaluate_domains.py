"""``memini eval domains``: run the domain eval and compare it with a baseline.

A thin wrapper of :func:`memini.evaluation.domains.run_domain_eval`; the set, the assigners and the
metrics are described there. The ``keyword`` stub reads the set's file only. The ``llm`` assigner
takes its provider from the service's settings and a scratch database on the server of
``MEMINI_DATABASE_URL`` (the role needs ``CREATEDB``) for the policies the LLM gate reads.

Exit codes (besides those of every command): 0 when nothing fell; 1 when the assigner failed; 2
for a set or baseline file that is not valid; :data:`EXIT_REGRESSION` when accuracy fell, or the
none rate moved away from the labelled one, by more than the margin.
"""

import argparse
from pathlib import Path
from typing import Final

from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, message
from memini.cli.evaluate import EXIT_REGRESSION, parse_margin
from memini.config import Settings
from memini.evaluation.domains import (
    ASSIGNERS,
    DEFAULT_ASSIGNER,
    DEFAULT_BASELINE,
    DEFAULT_MARGIN,
    DEFAULT_SET,
    DomainEvalError,
    DomainReport,
    LlmEvalAssigner,
    compare,
    format_report,
    load_set,
    read_report,
    run_domain_eval,
    write_report,
)
from memini.llm import LlmError

__all__ = ["DESCRIPTION", "HELP", "configure_parser", "domains_command"]

HELP: Final = "run the domain eval on the labelled decisions and the catalog"
DESCRIPTION: Final = (
    "Ask the assigner named for the domains of each labelled synthetic decision and open"
    " question of the set, as the assign_domains job asks the model, and print accuracy and the"
    " rate of items answered with no domain; compare with a baseline. The keyword stub reads"
    " files only. Exit codes: 0 no regression, 1 the assigner failed, 2 an invalid set or"
    f" baseline, {EXIT_REGRESSION} a metric fell below the baseline by more than the margin."
)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--set",
        dest="set_path",
        metavar="PATH",
        type=Path,
        default=DEFAULT_SET,
        help=f"the set file (default: {DEFAULT_SET}, from the repository root)",
    )
    parser.add_argument(
        "--assigner",
        choices=sorted(ASSIGNERS),
        default=DEFAULT_ASSIGNER,
        help="keyword: the stub of shared word stems; llm: the assign_domains call on the local"
        f" LLM provider (default: {DEFAULT_ASSIGNER})",
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
    parser.set_defaults(command=domains_command)


async def domains_command(args: argparse.Namespace, settings: Settings) -> int:
    baseline_path: Path | None = args.baseline
    write_baseline: bool = args.write_baseline
    json_path: Path | None = args.json
    if write_baseline and baseline_path is None:
        raise CommandError("--write-baseline needs --baseline PATH", EXIT_USAGE)
    try:
        data = load_set(args.set_path)
        baseline: DomainReport | None = None
        if baseline_path is not None and not write_baseline:
            baseline = read_report(baseline_path)
    except DomainEvalError as error:
        raise CommandError(f"invalid set or baseline: {error}", EXIT_USAGE) from None
    try:
        assigner = ASSIGNERS[args.assigner](settings)
    except DomainEvalError as error:
        raise CommandError(str(error)) from None
    message(f"domain eval of {len(data.cases)} items with {assigner.name}")
    try:
        report = await run_domain_eval(data, assigner)
    except (DomainEvalError, LlmError) as error:
        raise CommandError(str(error)) from None
    finally:
        await assigner.aclose()
    if isinstance(assigner, LlmEvalAssigner):
        message(assigner.summary())

    to_stdout = json_path is not None and str(json_path) == "-"
    if not to_stdout:
        print(format_report(report))
    if json_path is not None:
        write_report(json_path, report)
    if write_baseline and baseline_path is not None:
        write_report(baseline_path, report)
        message(f"wrote the baseline to {baseline_path}")
        return EXIT_OK
    if baseline is None:
        return EXIT_OK
    regressions = compare(report, baseline, margin=args.margin)
    for line in regressions:
        message(f"regression against {baseline_path}: {line}")
    if not regressions:
        message(f"no regression against {baseline_path}")
    return EXIT_REGRESSION if regressions else EXIT_OK
