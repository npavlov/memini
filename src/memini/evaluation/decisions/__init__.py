"""The decision eval: labelled synthetic threads and meetings; precision and recall of
extraction, accuracy of supersession.

Requirements, section 10 ("Decision eval"); plan, M4 ("M4 starts with the eval, not with the
pipeline"). Open question 5 (the smallest local model that extracts decisions well enough) is
settled by its numbers.

- :mod:`~memini.evaluation.decisions.dataset`: the sets (``eval/comms``,
  ``eval/decisions/cases``, ``eval/injection``), read from their batches and labels, and
  checked; and the injection attempts an injected run adds to their records.
- :mod:`~memini.evaluation.decisions.matching`: proposed items against labels, by evidence, then
  by a deterministic likeness of statements.
- :mod:`~memini.evaluation.decisions.runner`: :func:`run_decision_eval` extracts, matches and
  replays supersession with a :class:`Pipeline` (the protocols of :mod:`memini.extraction`).
- :mod:`~memini.evaluation.decisions.report`: the metrics, the report as a table and as JSON,
  and :func:`compare` with a baseline.
- :mod:`~memini.evaluation.decisions.stubs`: the extractors by name: ``oracle`` (the labels) and
  ``keyword`` (cue words; the committed baseline).

``memini eval decisions`` (:mod:`memini.cli.evaluate_decisions`) is the command.
"""

from memini.evaluation.decisions.dataset import (
    DEFAULT_INSERTS,
    DEFAULT_SETS,
    DecisionSet,
    DecisionSetError,
    Inserts,
    load_inserts,
    load_set,
    load_sets,
)
from memini.evaluation.decisions.matching import SIMILARITY_THRESHOLD, match_record, similarity
from memini.evaluation.decisions.report import (
    Comparison,
    DecisionReport,
    DecisionReportError,
    compare,
    format_comparison,
    format_report,
    read_report,
    write_report,
)
from memini.evaluation.decisions.runner import DecisionEvalError, Pipeline, run_decision_eval
from memini.evaluation.decisions.stubs import (
    DEFAULT_EXTRACTOR,
    EXTRACTORS,
    ExtractorVariant,
    VariantContext,
)

__all__ = [
    "DEFAULT_EXTRACTOR",
    "DEFAULT_INSERTS",
    "DEFAULT_SETS",
    "EXTRACTORS",
    "SIMILARITY_THRESHOLD",
    "Comparison",
    "DecisionEvalError",
    "DecisionReport",
    "DecisionReportError",
    "DecisionSet",
    "DecisionSetError",
    "ExtractorVariant",
    "Inserts",
    "Pipeline",
    "VariantContext",
    "compare",
    "format_comparison",
    "format_report",
    "load_inserts",
    "load_set",
    "load_sets",
    "match_record",
    "read_report",
    "run_decision_eval",
    "similarity",
    "write_report",
]
