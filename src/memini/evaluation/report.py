"""What an eval run measured, as a table and as JSON, and its comparison with a baseline.

A :class:`Report` holds, for each mode run, the metrics of every question and their means over
groups of questions: ``all``, ``kind:<kind>``, ``language:<language>`` and ``tag:<tag>``. Its
``setup`` records what was measured: the suite and its digest, the embedding model and the index
fingerprint, the chunker, the lexical variant, the reranker, the search options and the k values.

A baseline is a report written earlier (``memini eval --write-baseline``), as JSON
(:meth:`Report.to_json`): the same file format, so any report can serve as one. :func:`compare`
finds every tracked metric that fell by more than the margin: recall at each k and the mean
reciprocal rank, of every group of every mode that both reports hold. Differences of setup are
noted, not refused: comparing two models or two lexical variants is what a baseline is for.

The JSON holds no time and no host, and its numbers are rounded to :data:`DIGITS` places, so
writing the baseline again from an unchanged setup changes nothing in the file.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from memini.evaluation.suite import lookup
from memini.search import MODES, Mode

__all__ = [
    "DIGITS",
    "REPORT_FORMAT",
    "Change",
    "Comparison",
    "GroupSummary",
    "ModeResult",
    "QuestionResult",
    "Report",
    "ReportError",
    "compare",
    "format_comparison",
    "format_report",
    "read_report",
    "write_report",
]

REPORT_FORMAT: Final = 1
DIGITS: Final = 4
_EPSILON: Final = 1e-9


class ReportError(ValueError):
    """A report or baseline file that cannot be read."""


def _round(value: float) -> float:
    return round(value, DIGITS)


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """The metrics of one question in one mode."""

    recall: Mapping[int, float]
    """Recall at each k."""
    rr: float
    """Reciprocal rank within the largest k."""
    ranks: tuple[int | None, ...]
    """The place of the first hit of each expected result, as returned; ``None``: not found."""
    error: str | None = None
    """Why the search failed (a timeout, a pattern Postgres refused); the metrics are then 0."""


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """Means over the questions of a group."""

    n: int
    recall: Mapping[int, float]
    mrr: float


@dataclass(frozen=True, slots=True)
class ModeResult:
    groups: Mapping[str, GroupSummary]
    """By group name; ``all`` first."""
    questions: Mapping[str, QuestionResult]
    """By question id, in the order of the suite."""


@dataclass(frozen=True, slots=True)
class Report:
    """What one run measured."""

    setup: Mapping[str, Any]
    """JSON values: what was measured, and with what. See the module."""
    ks: tuple[int, ...]
    depth: int
    """How many hits each search returned. The reciprocal rank looks at the largest k only; the
    hits past it show whether a tie at the cutoff goes on."""
    modes: Mapping[Mode, ModeResult]
    notes: tuple[str, ...] = field(default=())
    """Warnings for the reader, such as dense results from the fake embedder. Not in the JSON."""

    @property
    def errors(self) -> list[tuple[Mode, str, str]]:
        """``(mode, question id, error)`` of every search that failed."""
        return [
            (mode, qid, result.error)
            for mode, mode_result in self.modes.items()
            for qid, result in mode_result.questions.items()
            if result.error is not None
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "format": REPORT_FORMAT,
            "setup": dict(self.setup),
            "k": list(self.ks),
            "depth": self.depth,
            "modes": {
                mode: {
                    "groups": {
                        name: {
                            "n": group.n,
                            "recall": {str(k): _round(v) for k, v in group.recall.items()},
                            "mrr": _round(group.mrr),
                        }
                        for name, group in result.groups.items()
                    },
                    "questions": {
                        qid: {
                            "recall": {str(k): _round(v) for k, v in question.recall.items()},
                            "rr": _round(question.rr),
                            "ranks": list(question.ranks),
                            **({"error": question.error} if question.error else {}),
                        }
                        for qid, question in result.questions.items()
                    },
                }
                for mode, result in self.modes.items()
            },
        }

    @classmethod
    def from_json(cls, data: object) -> "Report":
        """A report from :meth:`to_json`'s output. Raises :class:`ReportError`."""
        try:
            top = _object(data)
            if top["format"] != REPORT_FORMAT:
                raise ReportError(f"format {top['format']!r} is not {REPORT_FORMAT}")
            ks = tuple(_int(k) for k in _list(top["k"]))
            modes: dict[Mode, ModeResult] = {}
            for name, raw in _object(top["modes"]).items():
                mode: Mode | None = lookup(name, MODES)
                if mode is None:
                    raise ReportError(f"unknown mode {name!r}")
                body = _object(raw)
                groups = {
                    group: GroupSummary(
                        n=_int(_object(g)["n"]),
                        recall=_recall(_object(g)["recall"]),
                        mrr=_float(_object(g)["mrr"]),
                    )
                    for group, g in _object(body["groups"]).items()
                }
                questions = {
                    qid: QuestionResult(
                        recall=_recall(_object(q)["recall"]),
                        rr=_float(_object(q)["rr"]),
                        ranks=tuple(
                            None if r is None else _int(r) for r in _list(_object(q)["ranks"])
                        ),
                        error=_error(_object(q).get("error")),
                    )
                    for qid, q in _object(body["questions"]).items()
                }
                modes[mode] = ModeResult(groups=groups, questions=questions)
            return cls(setup=_object(top["setup"]), ks=ks, depth=_int(top["depth"]), modes=modes)
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ReportError):
                raise
            raise ReportError(f"not a report of memini eval: {error!r}") from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return cast(list[object], value)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError("expected an integer")
    return int(value)


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number")
    return float(value)


def _error(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError("expected a string")
    return value


def _recall(value: object) -> dict[int, float]:
    return {_int(k): _float(v) for k, v in _object(value).items()}


def write_report(path: Path, report: Report) -> None:
    """Write ``report`` as JSON to ``path`` (``-``: stdout), UTF-8, with a final newline."""
    text = json.dumps(report.to_json(), indent=2, ensure_ascii=False) + "\n"
    if str(path) == "-":
        print(text, end="")
        return
    path.write_text(text, encoding="utf-8")


def read_report(path: Path) -> Report:
    """Read a report or baseline written by :func:`write_report`. Raises :class:`ReportError`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReportError(f"{path}: cannot read it: {error.strerror or error}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReportError(f"{path}: not JSON: {error}") from None
    try:
        return Report.from_json(data)
    except ReportError as error:
        raise ReportError(f"{path}: {error}") from None


# --- Comparison ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """A tracked metric that moved by more than the margin."""

    mode: Mode
    group: str
    metric: str
    """``R@5``, ``MRR``."""
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


@dataclass(frozen=True, slots=True)
class QuestionChange:
    """A question whose recall at the largest k or reciprocal rank changed."""

    mode: Mode
    question: str
    metric: str
    baseline: float
    current: float


@dataclass(frozen=True, slots=True)
class Comparison:
    margin: float
    regressions: tuple[Change, ...]
    improvements: tuple[Change, ...]
    questions: tuple[QuestionChange, ...]
    notes: tuple[str, ...]
    """Differences of setup, and what the baseline tracks that this run did not measure."""


_SETUP_KEYS: Final = (
    ("suite", "suite"),
    ("embedding", "embedding model"),
    ("chunker", "chunker"),
    ("lexical", "lexical variant"),
    ("reranker", "reranker"),
    ("search", "search options"),
)


def compare(current: Report, baseline: Report, *, margin: float) -> Comparison:
    """Every tracked metric of ``baseline`` that ``current`` lowers, or raises, by more than
    ``margin`` (absolute): recall at each common k and, at the same largest k, the mean reciprocal
    rank, of each group of each mode in both. Values are compared as the JSON holds them, rounded
    to :data:`DIGITS` places."""
    notes: list[str] = []
    for key, label in _SETUP_KEYS:
        if current.setup.get(key) != baseline.setup.get(key):
            notes.append(f"the baseline was measured with another {label}")
    ks = [k for k in current.ks if k in baseline.ks]
    if ks != list(baseline.ks):
        notes.append(f"the baseline has k {list(baseline.ks)}; compared are {ks}")
    track_mrr = current.ks[-1] == baseline.ks[-1]
    if not track_mrr:
        notes.append(
            f"the baseline's MRR is within {baseline.ks[-1]} hits, this run's within"
            f" {current.ks[-1]}: MRR is not compared"
        )
    regressions: list[Change] = []
    improvements: list[Change] = []
    question_changes: list[QuestionChange] = []
    for mode, base in baseline.modes.items():
        now = current.modes.get(mode)
        if now is None:
            notes.append(f"the baseline has the mode {mode}, which this run did not measure")
            continue
        for group, base_group in base.groups.items():
            now_group = now.groups.get(group)
            if now_group is None:
                notes.append(f"the baseline has {mode} {group}; this run has no such questions")
                continue
            pairs = [(f"R@{k}", base_group.recall[k], now_group.recall[k]) for k in ks]
            if track_mrr:
                pairs.append(("MRR", base_group.mrr, now_group.mrr))
            for metric, before, after in ((m, _round(b), _round(a)) for m, b, a in pairs):
                change = Change(mode, group, metric, before, after)
                if after < before - margin - _EPSILON:
                    regressions.append(change)
                elif after > before + margin + _EPSILON:
                    improvements.append(change)
        top = max(ks) if ks else None
        for qid, base_question in base.questions.items():
            now_question = now.questions.get(qid)
            if now_question is None:
                continue
            pairs = (
                []
                if top is None
                else [(f"R@{top}", base_question.recall[top], now_question.recall[top])]
            )
            if track_mrr:
                pairs.append(("RR", base_question.rr, now_question.rr))
            for metric, before, after in ((m, _round(b), _round(a)) for m, b, a in pairs):
                if abs(after - before) > _EPSILON:
                    question_changes.append(QuestionChange(mode, qid, metric, before, after))
    return Comparison(
        margin=margin,
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        questions=tuple(question_changes),
        notes=tuple(notes),
    )


# --- Text ----------------------------------------------------------------------------------------


def format_report(report: Report) -> str:
    """The setup in a few lines, then one row per mode and group: n, recall at each k, MRR."""
    setup = report.setup
    suite = cast(Mapping[str, Any], setup.get("suite", {}))
    search = cast(Mapping[str, Any], setup.get("search", {}))
    lines = [
        f"suite {suite.get('name')} v{suite.get('version')}: {suite.get('questions')} questions,"
        f" {suite.get('repositories')} repositories (digest {str(suite.get('digest'))[:12]})",
        f"embeddings: {setup.get('embedding_description')}",
        f"index fingerprint {str(setup.get('index_fingerprint'))[:12]};"
        f" chunker {setup.get('chunker')}",
        f"lexical {setup.get('lexical')}; reranker {setup.get('reranker')};"
        f" candidates {search.get('candidates')}, rrf_k {search.get('rrf_k')}"
        + (
            f", weights dense {search['dense_weight']} lexical {search['lexical_weight']}"
            if "dense_weight" in search and "lexical_weight" in search
            else ""
        )
        + f"; {report.depth} hits per search",
        *(f"note: {note}" for note in report.notes),
        "",
    ]
    header = ["mode", "group", "n", *(f"R@{k}" for k in report.ks), "MRR"]
    rows: list[list[str]] = []
    for mode, result in report.modes.items():
        for number, (name, group) in enumerate(result.groups.items()):
            rows.append(
                [
                    mode if number == 0 else "",
                    name,
                    str(group.n),
                    *(f"{group.recall[k]:.2f}" for k in report.ks),
                    f"{group.mrr:.2f}",
                ]
            )
    lines += _table(header, rows, numeric_from=2)
    for mode, qid, error in report.errors:
        lines.append(f"error: {mode} {qid}: {error}")
    return "\n".join(lines)


def format_comparison(comparison: Comparison, baseline: str) -> str:
    """The regressions, improvements and changed questions, with the notes."""
    lines = [f"compared with the baseline {baseline} (margin {comparison.margin:g}):"]
    lines += [f"  note: {note}" for note in comparison.notes]
    if not comparison.regressions:
        lines.append("  no tracked metric fell by more than the margin")
    for title, changes in (
        ("regressions", comparison.regressions),
        ("improvements", comparison.improvements),
    ):
        if not changes:
            continue
        lines.append(f"  {title}:")
        rows = [
            [
                c.mode,
                c.group,
                c.metric,
                f"{c.baseline:.2f}",
                "->",
                f"{c.current:.2f}",
                f"({c.delta:+.2f})",
            ]
            for c in changes
        ]
        lines += ["    " + line for line in _table(None, rows, numeric_from=3)]
    if comparison.questions:
        lines.append("  questions that changed:")
        rows = [
            [q.mode, q.question, q.metric, f"{q.baseline:.2f}", "->", f"{q.current:.2f}"]
            for q in comparison.questions
        ]
        lines += ["    " + line for line in _table(None, rows, numeric_from=3)]
    if comparison.improvements and not comparison.regressions:
        lines.append("  to keep the gains, write the baseline again with --write-baseline")
    return "\n".join(lines)


def _table(
    header: Sequence[str] | None, rows: Sequence[Sequence[str]], *, numeric_from: int
) -> list[str]:
    table = ([list(header)] if header else []) + [list(row) for row in rows]
    if not table:
        return []
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    lines: list[str] = []
    for row in table:
        cells = [
            cell.rjust(width) if i >= numeric_from else cell.ljust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append("  ".join(cells).rstrip())
    return lines
