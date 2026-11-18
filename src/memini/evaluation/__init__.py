"""The retrieval eval: a question set with expected results, recall at k and MRR, a baseline.

Requirements, section 10: "a question set per workspace with expected records; recall@k per
kind. Run on every change of model, chunker or reranker." Open questions 1 (the lexical
retriever) and 2 (the default embedding model) are settled by its numbers.

- :mod:`memini.evaluation.suite`: the suite file (TOML), its fixture repositories and questions,
  and :func:`load_suite`, which checks them.
- :mod:`memini.evaluation.runner`: :func:`run_eval` indexes the fixtures into a scratch database
  through the service's git sync, runs every question in every mode, and returns a
  :class:`Report`.
- :mod:`memini.evaluation.metrics`: recall at k and reciprocal rank, aware of ties and duplicates.
- :mod:`memini.evaluation.report`: the report as a table and as JSON, and :func:`compare` with a
  baseline.
- :mod:`memini.evaluation.variants`: the lexical retrievers and rerankers a run can measure.

``memini eval`` (:mod:`memini.cli.evaluate`) is the command.
"""

from memini.evaluation.metrics import Ranked, recall_at_k, reciprocal_rank
from memini.evaluation.report import (
    Comparison,
    Report,
    ReportError,
    compare,
    format_comparison,
    format_report,
    read_report,
    write_report,
)
from memini.evaluation.runner import (
    DEFAULT_KS,
    EvalConfig,
    EvalSetupError,
    ExpectationError,
    run_eval,
)
from memini.evaluation.suite import Expected, Question, Repository, Suite, SuiteError, load_suite
from memini.evaluation.variants import (
    DEFAULT_LEXICAL,
    DEFAULT_RERANKER,
    LEXICAL_VARIANTS,
    RERANKERS,
    LexicalVariant,
    ModelReranker,
    RerankerVariant,
)

__all__ = [
    "DEFAULT_KS",
    "DEFAULT_LEXICAL",
    "DEFAULT_RERANKER",
    "LEXICAL_VARIANTS",
    "RERANKERS",
    "Comparison",
    "EvalConfig",
    "EvalSetupError",
    "ExpectationError",
    "Expected",
    "LexicalVariant",
    "ModelReranker",
    "Question",
    "Ranked",
    "Report",
    "ReportError",
    "Repository",
    "RerankerVariant",
    "Suite",
    "SuiteError",
    "compare",
    "format_comparison",
    "format_report",
    "load_suite",
    "read_report",
    "recall_at_k",
    "reciprocal_rank",
    "run_eval",
    "write_report",
]
