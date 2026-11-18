"""What a decision eval run measured, as a table and as JSON, and its comparison with a baseline.

The three metrics, each for the groups ``all``, ``kind:<kind>``, ``language:<language>`` and
``set:<set>`` (supersession: ``relation:<relation>`` instead of ``kind``):

- **precision** of extraction: labelled items found / (items found + false items). A false item
  is a proposed item that matches no label of its record, or matches a trap. Duplicates and
  restatements count neither way (:mod:`memini.evaluation.decisions.matching`). By kind, a false
  item counts in the kind proposed; by language, in its record's language.
- **recall** of extraction: labelled items found / labelled items.
- **supersession accuracy**: labelled supersession cases reproduced / cases. A case is a
  labelled item (expected: what its links say, ``new`` without one) or a restatement (expected:
  ``duplicate`` of its item). It is reproduced when the item was extracted and the classifier's
  verdict, read back to labels, is the expected one. ``reached`` counts the cases whose item was
  extracted and whose expected targets were among the candidates shown; ``right / reached`` is
  the classifier's own accuracy, apart from what extraction missed.

A metric over nothing is 1: precision with no item proposed, recall with no item labelled.

A baseline is a report written earlier (``memini eval decisions --write-baseline``). JSON holds
no time and no host and rounds to :data:`DIGITS` places, so an unchanged run writes the same file.
:func:`compare` lists every tracked metric (precision, recall, accuracy of every group of the
baseline) that fell, or rose, by more than the margin, and the items and cases that changed.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

__all__ = [
    "DIGITS",
    "REPORT_FORMAT",
    "CaseOutcome",
    "Change",
    "Comparison",
    "DecisionReport",
    "DecisionReportError",
    "ExtractionSummary",
    "ItemOutcome",
    "SupersessionSummary",
    "compare",
    "format_comparison",
    "format_report",
    "read_report",
    "write_report",
]

REPORT_FORMAT: Final = 1
REPORT_KIND: Final = "decisions"
DIGITS: Final = 4
_EPSILON: Final = 1e-9


class DecisionReportError(ValueError):
    """A report or baseline file that cannot be read."""


def _round(value: float) -> float:
    return round(value, DIGITS)


def _ratio(part: int, whole: int) -> float:
    return part / whole if whole else 1.0


@dataclass(frozen=True, slots=True)
class ExtractionSummary:
    """Counts of one group, and precision and recall from them."""

    items: int = 0
    """Labelled items."""
    found: int = 0
    """Labelled items some proposed item matched: true positives."""
    proposed: int = 0
    """Every proposed item, whatever became of it."""
    false: int = 0
    """Proposed items that matched no label, or a trap: false positives."""
    traps: int = 0
    """Of the false ones, those that matched a trap."""
    duplicates: int = 0
    restatements: int = 0

    @property
    def missed(self) -> int:
        return self.items - self.found

    @property
    def precision(self) -> float:
        return _ratio(self.found, self.found + self.false)

    @property
    def recall(self) -> float:
        return _ratio(self.found, self.items)

    def to_json(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "found": self.found,
            "proposed": self.proposed,
            "false": self.false,
            "traps": self.traps,
            "duplicates": self.duplicates,
            "restatements": self.restatements,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "ExtractionSummary":
        return cls(
            items=_int(data["items"]),
            found=_int(data["found"]),
            proposed=_int(data["proposed"]),
            false=_int(data["false"]),
            traps=_int(data.get("traps", 0)),
            duplicates=_int(data.get("duplicates", 0)),
            restatements=_int(data.get("restatements", 0)),
        )


@dataclass(frozen=True, slots=True)
class SupersessionSummary:
    cases: int = 0
    extracted: int = 0
    """Cases whose item was extracted."""
    reached: int = 0
    """Of those, the cases whose expected targets were among the candidates shown."""
    correct: int = 0

    @property
    def accuracy(self) -> float:
        """Cases reproduced, of all cases."""
        return _ratio(self.correct, self.cases)

    @property
    def classifier_accuracy(self) -> float:
        """Cases reproduced, of those extraction reached."""
        return _ratio(self.correct, self.reached)

    def to_json(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "extracted": self.extracted,
            "reached": self.reached,
            "correct": self.correct,
            "accuracy": _round(self.accuracy),
            "classifier_accuracy": _round(self.classifier_accuracy),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "SupersessionSummary":
        return cls(
            cases=_int(data["cases"]),
            extracted=_int(data["extracted"]),
            reached=_int(data["reached"]),
            correct=_int(data["correct"]),
        )


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """A labelled item, and whether it was found."""

    found: bool
    kind: str
    language: str


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """A labelled supersession case, what was expected and what the classifier said."""

    expected: str
    got: str | None
    """``None``: the item was not extracted."""
    reached: bool
    correct: bool


@dataclass(frozen=True, slots=True)
class DecisionReport:
    """What one run measured."""

    setup: Mapping[str, Any]
    """JSON values: the sets (name, workspace, digest, counts), the extractor, the matching."""
    extraction: Mapping[str, ExtractionSummary]
    """By group; ``all`` first."""
    supersession: Mapping[str, SupersessionSummary]
    items: Mapping[str, ItemOutcome] = field(default_factory=dict[str, ItemOutcome])
    """By ``<set>/<item id>``."""
    cases: Mapping[str, CaseOutcome] = field(default_factory=dict[str, CaseOutcome])
    """By ``<set>/<item id>`` for an item, ``<set>/<item id>@<record>`` for a restatement."""
    proposals: tuple[Mapping[str, Any], ...] = ()
    """Every proposed item and what became of it, as JSON values, for the reader."""
    notes: tuple[str, ...] = ()
    """Warnings for the reader. Not in the JSON."""

    @property
    def precision(self) -> float:
        return self.extraction["all"].precision

    @property
    def recall(self) -> float:
        return self.extraction["all"].recall

    @property
    def accuracy(self) -> float:
        return self.supersession["all"].accuracy

    def to_json(self) -> dict[str, Any]:
        return {
            "format": REPORT_FORMAT,
            "kind": REPORT_KIND,
            "setup": dict(self.setup),
            "metrics": {
                "precision": _round(self.precision),
                "recall": _round(self.recall),
                "supersession_accuracy": _round(self.accuracy),
            },
            "extraction": {
                "groups": {name: s.to_json() for name, s in self.extraction.items()},
                "items": {
                    key: {"found": o.found, "kind": o.kind, "language": o.language}
                    for key, o in self.items.items()
                },
                "proposals": [dict(p) for p in self.proposals],
            },
            "supersession": {
                "groups": {name: s.to_json() for name, s in self.supersession.items()},
                "cases": {
                    key: {
                        "expected": o.expected,
                        "got": o.got,
                        "reached": o.reached,
                        "correct": o.correct,
                    }
                    for key, o in self.cases.items()
                },
            },
        }

    @classmethod
    def from_json(cls, data: object) -> "DecisionReport":
        """A report from :meth:`to_json`'s output. Raises :class:`DecisionReportError`."""
        try:
            top = _object(data)
            if top.get("format") != REPORT_FORMAT or top.get("kind") != REPORT_KIND:
                raise DecisionReportError(
                    f"not a report of memini eval decisions (format {REPORT_FORMAT})"
                )
            extraction = _object(top["extraction"])
            supersession = _object(top["supersession"])
            items = {
                key: ItemOutcome(
                    found=_bool(_object(o)["found"]),
                    kind=str(_object(o)["kind"]),
                    language=str(_object(o)["language"]),
                )
                for key, o in _object(extraction.get("items", {})).items()
            }
            cases = {
                key: CaseOutcome(
                    expected=str(_object(o)["expected"]),
                    got=None if _object(o)["got"] is None else str(_object(o)["got"]),
                    reached=_bool(_object(o)["reached"]),
                    correct=_bool(_object(o)["correct"]),
                )
                for key, o in _object(supersession.get("cases", {})).items()
            }
            report = cls(
                setup=_object(top["setup"]),
                extraction={
                    name: ExtractionSummary.from_json(_object(group))
                    for name, group in _object(extraction["groups"]).items()
                },
                supersession={
                    name: SupersessionSummary.from_json(_object(group))
                    for name, group in _object(supersession["groups"]).items()
                },
                items=items,
                cases=cases,
                proposals=tuple(_object(p) for p in _list(extraction.get("proposals", []))),
            )
            if "all" not in report.extraction or "all" not in report.supersession:
                raise DecisionReportError("a report has the group all")
            return report
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DecisionReportError):
                raise
            raise DecisionReportError(f"not a report of memini eval decisions: {error!r}") from None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return cast(list[object], value)


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected an integer")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected true or false")
    return value


def write_report(path: Path, report: DecisionReport) -> None:
    """Write ``report`` as JSON to ``path`` (``-``: stdout), UTF-8, with a final newline."""
    text = json.dumps(report.to_json(), indent=2, ensure_ascii=False) + "\n"
    if str(path) == "-":
        print(text, end="")
        return
    path.write_text(text, encoding="utf-8")


def read_report(path: Path) -> DecisionReport:
    """Read a report or baseline of :func:`write_report`. Raises :class:`DecisionReportError`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DecisionReportError(f"{path}: cannot read it: {error.strerror or error}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionReportError(f"{path}: not JSON: {error}") from None
    try:
        return DecisionReport.from_json(data)
    except DecisionReportError as error:
        raise DecisionReportError(f"{path}: {error}") from None


# --- Comparison ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    """A tracked metric that moved by more than the margin."""

    group: str
    metric: str
    """``precision``, ``recall`` or ``accuracy``."""
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline


@dataclass(frozen=True, slots=True)
class Comparison:
    margin: float
    regressions: tuple[Change, ...]
    improvements: tuple[Change, ...]
    changed: tuple[str, ...]
    """Items found then and missed now, or the reverse; cases right then and wrong now, or the
    reverse; one line each."""
    notes: tuple[str, ...]


def _tracked(report: DecisionReport) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for group, summary in report.extraction.items():
        values[(group, "precision")] = _round(summary.precision)
        values[(group, "recall")] = _round(summary.recall)
    for group, summary in report.supersession.items():
        values[(f"supersession {group}", "accuracy")] = _round(summary.accuracy)
    return values


def compare(current: DecisionReport, baseline: DecisionReport, *, margin: float) -> Comparison:
    """Every tracked metric of ``baseline`` that ``current`` lowers, or raises, by more than
    ``margin`` (absolute), as the JSON holds them (rounded)."""
    notes: list[str] = []
    for key, label in (("sets", "sets"), ("extractor", "extractor"), ("matching", "matching")):
        if current.setup.get(key) != baseline.setup.get(key):
            notes.append(f"the baseline was measured with other {label}")
    now = _tracked(current)
    regressions: list[Change] = []
    improvements: list[Change] = []
    for (group, metric), before in _tracked(baseline).items():
        after = now.get((group, metric))
        if after is None:
            notes.append(f"the baseline has {group}; this run has no such group")
            continue
        change = Change(group, metric, before, after)
        if after < before - margin - _EPSILON:
            regressions.append(change)
        elif after > before + margin + _EPSILON:
            improvements.append(change)
    changed: list[str] = []
    for key, then in baseline.items.items():
        outcome = current.items.get(key)
        if outcome is not None and outcome.found != then.found:
            changed.append(f"item {key}: {'missed' if then.found else 'found'} now")
    for key, before in baseline.cases.items():
        case = current.cases.get(key)
        if case is not None and case.correct != before.correct:
            changed.append(
                f"case {key}: {'right' if case.correct else 'wrong'} now"
                f" (expected {case.expected}, got {case.got or 'nothing extracted'})"
            )
    return Comparison(
        margin=margin,
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        changed=tuple(changed),
        notes=tuple(dict.fromkeys(notes)),
    )


# --- Text ----------------------------------------------------------------------------------------


def _sets_line(setup: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for each in cast(Sequence[Mapping[str, Any]], setup.get("sets", [])):
        lines.append(
            f"set {each.get('name')} (workspace {each.get('workspace')}, digest"
            f" {str(each.get('digest'))[:12]}): {each.get('records')} records,"
            f" {each.get('items')} items, {each.get('links')} links,"
            f" {each.get('restatements')} restatements, {each.get('traps')} traps,"
            f" {each.get('no_items')} with nothing to extract"
        )
    return lines


def format_report(report: DecisionReport) -> str:
    """The setup, the three metrics, then a row per group of extraction and of supersession."""
    setup = report.setup
    matching = cast(Mapping[str, Any], setup.get("matching", {}))
    every = report.extraction["all"]
    cases = report.supersession["all"]
    lines = [
        *_sets_line(setup),
        f"extractor {setup.get('extractor')} ({setup.get('variant')}); matching by evidence,"
        f" then statements alike at {matching.get('threshold')} ({matching.get('similarity')})",
        *(f"note: {note}" for note in report.notes),
        "",
        f"extraction precision     {every.precision:.2f}  ({every.found} items found,"
        f" {every.false} false items)",
        f"extraction recall        {every.recall:.2f}  ({every.found} of {every.items} labelled"
        " items)",
        f"supersession accuracy    {cases.accuracy:.2f}  ({cases.correct} of {cases.cases}"
        f" cases; the classifier was right in {cases.correct} of the {cases.reached} cases"
        " extraction reached)",
        "",
    ]
    header = ["extraction", "items", "found", "proposed", "false", "dup", "restated", "P", "R"]
    rows = [
        [
            name,
            str(s.items),
            str(s.found),
            str(s.proposed),
            str(s.false),
            str(s.duplicates),
            str(s.restatements),
            f"{s.precision:.2f}",
            f"{s.recall:.2f}",
        ]
        for name, s in report.extraction.items()
    ]
    lines += _table(header, rows, numeric_from=1)
    lines.append("")
    header = ["supersession", "cases", "extracted", "reached", "right", "accuracy"]
    rows = [
        [
            name,
            str(s.cases),
            str(s.extracted),
            str(s.reached),
            str(s.correct),
            f"{s.accuracy:.2f}",
        ]
        for name, s in report.supersession.items()
    ]
    lines += _table(header, rows, numeric_from=1)
    return "\n".join(lines)


def format_comparison(comparison: Comparison, baseline: str) -> str:
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
                c.group,
                c.metric,
                f"{c.baseline:.2f}",
                "->",
                f"{c.current:.2f}",
                f"({c.delta:+.2f})",
            ]
            for c in changes
        ]
        lines += ["    " + line for line in _table(None, rows, numeric_from=2)]
    if comparison.changed:
        lines.append("  items and cases that changed:")
        lines += [f"    {line}" for line in comparison.changed]
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
