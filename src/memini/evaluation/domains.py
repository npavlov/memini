"""The domain eval: a labelled synthetic set of decisions and open questions, the domains of a
catalog, and how well an assigner puts each item where the labels do (requirements, section 10,
**Domain eval**; the ``assign_domains`` job, :mod:`memini.decisions.assign`).

The set (``eval/domains/set.json``) holds a catalog (projects with their domains and subdomains,
titles and descriptions) and items: a statement, optionally the parts of the record it cites, its
kind, its language, the project it already has (then only that project's domains are offered, as
the job does), and the domains it belongs to, ``project:address``, or none. Everything in it is
synthetic, written by hand.

Each item becomes the :class:`~memini.decisions.assign.DomainQuestion` the job would ask, and the
assigner answers it. The metrics:

- ``accuracy``: the items whose answer is exactly the labelled set of domains, none included.
- ``none_rate``: the items answered with no domain, against ``expected_none_rate``, the items
  labelled so. ``false_none`` counts items with domains answered with none; ``false_domain``
  items labelled none answered with a domain.

Assigners (:data:`ASSIGNERS`): ``keyword``, a stub that picks the domains sharing the most word
stems with the item (no model, no database: the committed baseline,
``eval/domains/baseline-keyword.json``), and ``llm``, the job's call through the LLM gate on the
local provider of the settings. The gate reads the policies of a workspace and of sources, so the
``llm`` assigner makes a scratch database with one workspace and one push source of policy
``local`` (:func:`memini.evaluation.runner.scratch_database`), and drops it at the end.

:func:`compare` finds a regression when accuracy falls by more than the margin, or the none rate
moves further from the expected one by more than the margin.
"""

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import httpx2
from psycopg import AsyncConnection

from memini.auth import WriteAccess
from memini.config import Settings
from memini.db import connect, migrate
from memini.decisions.assign import (
    DomainChoice,
    DomainQuestion,
    LlmAssigner,
    SaidPart,
    assigner_name,
)
from memini.evaluation.runner import scratch_database
from memini.llm import LlmOutputError, configured_providers, create_llm_gate
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

__all__ = [
    "ASSIGNERS",
    "DEFAULT_ASSIGNER",
    "DEFAULT_BASELINE",
    "DEFAULT_MARGIN",
    "DEFAULT_SET",
    "DomainCase",
    "DomainEvalError",
    "DomainReport",
    "DomainSet",
    "EvalAssigner",
    "KeywordAssigner",
    "LlmEvalAssigner",
    "compare",
    "format_report",
    "load_set",
    "read_report",
    "run_domain_eval",
    "write_report",
]

DEFAULT_SET: Final = Path("eval/domains/set.json")
DEFAULT_BASELINE: Final = Path("eval/domains/baseline-keyword.json")
DEFAULT_MARGIN: Final = 0.02
DEFAULT_ASSIGNER: Final = "keyword"
DECIDED_AT: Final = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
"""The time every item of the set is said: the labels do not depend on it."""


class DomainEvalError(ValueError):
    """A set or a report that is not valid, or an assigner that cannot run."""


# --- The set -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainCase:
    id: str
    kind: str
    statement: str
    said: tuple[SaidPart, ...]
    project: str | None
    language: str
    expected: frozenset[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class DomainSet:
    name: str
    choices: tuple[DomainChoice, ...]
    cases: tuple[DomainCase, ...]

    def question(self, case: DomainCase) -> DomainQuestion:
        """What the job would ask about ``case``: the domains of its project when it has one."""
        return DomainQuestion(
            kind=case.kind,
            statement=case.statement,
            decided_at=DECIDED_AT,
            said=case.said,
            choices=tuple(
                c for c in self.choices if case.project is None or c.project == case.project
            ),
        )


def _text(value: object, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DomainEvalError(f"{where}: a non-empty string")
    return value


def _list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise DomainEvalError(f"{where}: a list")
    items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
    return items


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DomainEvalError(f"{where}: an object")
    found: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
    return found


def load_set(path: Path) -> DomainSet:
    """The set in ``path``, checked.

    Raises:
        DomainEvalError: the file cannot be read, or holds something that is not a set.
    """
    try:
        data = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, json.JSONDecodeError) as error:
        raise DomainEvalError(f"{path}: {error}") from None
    choices: list[DomainChoice] = []
    for p, project in enumerate(_list(data.get("catalog"), "catalog")):
        entry = _object(project, f"catalog[{p}]")
        name = _text(entry.get("project"), f"catalog[{p}].project")
        title = _text(entry.get("title", ""), f"catalog[{p}].title", empty=True)
        for d, domain in enumerate(_list(entry.get("domains"), f"catalog[{p}].domains")):
            where = f"catalog[{p}].domains[{d}]"
            each = _object(domain, where)
            choices.append(
                DomainChoice(
                    project=name,
                    project_title=title,
                    domain=_text(each.get("domain"), f"{where}.domain"),
                    title=_text(each.get("title", ""), f"{where}.title", empty=True),
                    description=_text(
                        each.get("description", ""), f"{where}.description", empty=True
                    ),
                )
            )
    known = {choice.key for choice in choices}
    if len(known) != len(choices):
        raise DomainEvalError("catalog: a domain is listed twice")
    cases: list[DomainCase] = []
    for n, raw in enumerate(_list(data.get("items"), "items")):
        where = f"items[{n}]"
        item = _object(raw, where)
        expected: set[tuple[str, str]] = set()
        for e, label in enumerate(_list(item.get("expected"), f"{where}.expected")):
            project, colon, address = _text(label, f"{where}.expected[{e}]").partition(":")
            if not colon or (project, address) not in known:
                raise DomainEvalError(f"{where}.expected[{e}]: project:domain of the catalog")
            expected.add((project, address))
        project = item.get("project")
        if project is not None and project not in {c.project for c in choices}:
            raise DomainEvalError(f"{where}.project: a project of the catalog")
        if project is not None and any(p != project for p, _ in expected):
            raise DomainEvalError(f"{where}.expected: domains of the item's project")
        said: list[SaidPart] = []
        for s, raw_part in enumerate(_list(item.get("said", []), f"{where}.said")):
            part = _object(raw_part, f"{where}.said[{s}]")
            who = part.get("from")
            said.append(
                SaidPart(
                    text=_text(part.get("text"), f"{where}.said[{s}].text"),
                    who=None if who is None else _text(who, f"{where}.said[{s}].from"),
                )
            )
        kind = item.get("kind", "decision")
        if kind not in ("decision", "open_question"):
            raise DomainEvalError(f"{where}.kind: decision or open_question")
        cases.append(
            DomainCase(
                id=_text(item.get("id"), f"{where}.id"),
                kind=kind,
                statement=_text(item.get("statement"), f"{where}.statement"),
                said=tuple(said),
                project=project,
                language=_text(item.get("language", "en"), f"{where}.language"),
                expected=frozenset(expected),
            )
        )
    if len({case.id for case in cases}) != len(cases):
        raise DomainEvalError("items: an id is used twice")
    if not cases:
        raise DomainEvalError("items: at least one")
    return DomainSet(
        name=_text(data.get("name"), "name"), choices=tuple(choices), cases=tuple(cases)
    )


# --- Assigners -----------------------------------------------------------------------------------


class EvalAssigner(Protocol):
    """What the eval measures: an assigner with a name, and an end."""

    @property
    def name(self) -> str: ...

    async def assign(self, question: DomainQuestion) -> tuple[DomainChoice, ...]: ...

    async def aclose(self) -> None: ...


_WORD: Final = re.compile(r"\w+")
_STEM: Final = 4
_STOPWORDS: Final = frozenset(
    {"what", "with", "from", "that", "this", "every", "each", "their", "them", "they", "have",
     "harbour", "ship", "ships", "team", "after", "before", "until", "ahead", "instead", "only",
     "does", "should", "still", "less", "more", "than", "over", "into", "who", "whom"}
)  # fmt: skip


def _stems(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return {
        word[:_STEM]
        for word in _WORD.findall(folded)
        if len(word) >= _STEM and word not in _STOPWORDS and not word.isdigit()
    }


@dataclass(slots=True)
class KeywordAssigner:
    """The stub: the domains whose address, title and description share the most word stems with
    the item's statement and cited parts; none when no domain shares one. Deterministic, no
    model."""

    name: str = "keyword-stub"

    async def assign(self, question: DomainQuestion) -> tuple[DomainChoice, ...]:
        said = " ".join(part.text for part in question.said)
        words = _stems(f"{question.statement} {said}")
        scores: list[tuple[int, DomainChoice]] = []
        for choice in question.choices:
            own = _stems(f"{choice.domain.replace('/', ' ')} {choice.title} {choice.description}")
            scores.append((len(words & own), choice))
        best = max((score for score, _ in scores), default=0)
        if best == 0:
            return ()
        return tuple(choice for score, choice in scores if score == best)[:3]

    async def aclose(self) -> None:
        return None


PRINCIPAL: Final = "eval"
_WORKSPACE: Final = "domains"
_SOURCE: Final = "notes"


class LlmEvalAssigner:
    """The job's call (:class:`~memini.decisions.assign.LlmAssigner`) through the gate, on the
    local provider of the settings; see the module."""

    def __init__(
        self, settings: Settings, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        local = configured_providers(settings).get("local")
        if local is None:
            raise DomainEvalError(
                "the llm assigner runs on the local LLM provider: set MEMINI_LLM_LOCAL_URL and"
                " MEMINI_LLM_LOCAL_MODEL"
            )
        self.name = assigner_name(local.model)
        self._gate = create_llm_gate(settings, transport=transport)
        self._assigner = LlmAssigner(self._gate)
        self._server_url = settings.database_url.get_secret_value()
        self._stack = AsyncExitStack()
        self._conn: AsyncConnection[Any] | None = None
        self._scope: tuple[UUID, UUID] | None = None
        self.output_errors = 0

    async def _ready(self) -> tuple[AsyncConnection[Any], UUID, UUID]:
        if self._conn is None or self._scope is None:
            _, url = await self._stack.enter_async_context(scratch_database(self._server_url))
            await migrate(url)
            conn = await self._stack.enter_async_context(connect(url, autocommit=True))
            owner = await ensure_principal(conn, PRINCIPAL)
            workspace = await create_workspace(conn, owner_id=owner, name=_WORKSPACE)
            source = await create_push_source(
                conn, WriteAccess.for_owner(owner), _WORKSPACE, NewPushSource(name=_SOURCE)
            )
            self._conn, self._scope = conn, (workspace.id, source.id)
        return self._conn, *self._scope

    async def assign(self, question: DomainQuestion) -> tuple[DomainChoice, ...]:
        conn, workspace_id, source_id = await self._ready()
        try:
            chosen, _ = await self._assigner.assign(
                conn, workspace_id=workspace_id, source_ids={source_id}, question=question
            )
        except LlmOutputError:
            self.output_errors += 1
            return ()
        return chosen

    def summary(self) -> str:
        a = self._assigner
        return (
            f"{self.name}: {a.calls} calls, {a.input_tokens} input and {a.output_tokens} output"
            f" tokens; {self.output_errors} replies that did not match the schema"
        )

    async def aclose(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            await self._gate.aclose()


ASSIGNERS: Final[Mapping[str, Callable[[Settings | None], EvalAssigner]]] = {
    "keyword": lambda _settings: KeywordAssigner(),
    "llm": lambda settings: _llm(settings),
}
"""The assigners by name, each made from the settings (the stub needs none)."""


def _llm(settings: Settings | None) -> EvalAssigner:
    if settings is None:
        raise DomainEvalError("the llm assigner needs the service's settings")
    return LlmEvalAssigner(settings)


# --- The run and the report ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    language: str
    expected: tuple[str, ...]
    answered: tuple[str, ...]

    @property
    def correct(self) -> bool:
        return self.expected == self.answered


@dataclass(slots=True)
class DomainReport:
    assigner: str
    set: str
    items: int
    accuracy: float
    none_rate: float
    expected_none_rate: float
    false_none: int
    false_domain: int
    by_language: dict[str, float] = field(default_factory=dict[str, float])
    """Accuracy by language."""
    cases: list[CaseResult] = field(default_factory=list[CaseResult])


def _labels(found: Sequence[tuple[str, str]] | frozenset[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(sorted(f"{project}:{domain}" for project, domain in found))


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


async def run_domain_eval(data: DomainSet, assigner: EvalAssigner) -> DomainReport:
    """Ask ``assigner`` about every item of ``data`` and measure the answers."""
    results: list[CaseResult] = []
    for case in data.cases:
        answer = await assigner.assign(data.question(case))
        results.append(
            CaseResult(
                id=case.id,
                language=case.language,
                expected=_labels(case.expected),
                answered=_labels([choice.key for choice in answer]),
            )
        )
    total = len(results)
    languages = sorted({result.language for result in results})
    return DomainReport(
        assigner=assigner.name,
        set=data.name,
        items=total,
        accuracy=_rate(sum(r.correct for r in results), total),
        none_rate=_rate(sum(not r.answered for r in results), total),
        expected_none_rate=_rate(sum(not r.expected for r in results), total),
        false_none=sum(bool(r.expected) and not r.answered for r in results),
        false_domain=sum(not r.expected and bool(r.answered) for r in results),
        by_language={
            language: _rate(
                sum(r.correct for r in results if r.language == language),
                sum(r.language == language for r in results),
            )
            for language in languages
        },
        cases=results,
    )


def format_report(report: DomainReport) -> str:
    lines = [
        f"domain eval of {report.items} items of set {report.set} with {report.assigner}",
        f"accuracy          {report.accuracy:.4f}",
        f"none rate         {report.none_rate:.4f}"
        f" (labelled none: {report.expected_none_rate:.4f})",
        f"false none        {report.false_none}",
        f"false domain      {report.false_domain}",
    ]
    lines += [
        f"accuracy ({language})  {value:.4f}" for language, value in report.by_language.items()
    ]
    wrong = [result for result in report.cases if not result.correct]
    if wrong:
        lines.append("wrong:")
        lines += [
            f"  {r.id}: expected {', '.join(r.expected) or 'none'};"
            f" answered {', '.join(r.answered) or 'none'}"
            for r in wrong
        ]
    return "\n".join(lines)


def write_report(path: Path, report: DomainReport) -> None:
    text = json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n"
    if str(path) == "-":
        print(text, end="")
        return
    path.write_text(text, encoding="utf-8")


def read_report(path: Path) -> DomainReport:
    """A report written by :func:`write_report`.

    Raises:
        DomainEvalError: it cannot be read or is not a report.
    """
    try:
        data = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
        cases = [
            CaseResult(
                id=str(each["id"]),
                language=str(each["language"]),
                expected=tuple(each["expected"]),
                answered=tuple(each["answered"]),
            )
            for each in _list(data.get("cases", []), "cases")
        ]
        return DomainReport(
            assigner=str(data["assigner"]),
            set=str(data["set"]),
            items=int(data["items"]),
            accuracy=float(data["accuracy"]),
            none_rate=float(data["none_rate"]),
            expected_none_rate=float(data["expected_none_rate"]),
            false_none=int(data["false_none"]),
            false_domain=int(data["false_domain"]),
            by_language={str(k): float(v) for k, v in _object(data["by_language"], "x").items()},
            cases=cases,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise DomainEvalError(f"{path}: not a domain eval report ({error})") from None


def compare(current: DomainReport, baseline: DomainReport, *, margin: float) -> list[str]:
    """What fell by more than ``margin`` against ``baseline``; empty for no regression."""
    regressions: list[str] = []
    if current.accuracy < baseline.accuracy - margin:
        regressions.append(f"accuracy {current.accuracy:.4f} < {baseline.accuracy:.4f}")
    drift = abs(current.none_rate - current.expected_none_rate)
    before = abs(baseline.none_rate - baseline.expected_none_rate)
    if drift > before + margin:
        regressions.append(
            f"none rate {current.none_rate:.4f} is {drift:.4f} from the labelled"
            f" {current.expected_none_rate:.4f} (baseline: {before:.4f})"
        )
    return regressions
