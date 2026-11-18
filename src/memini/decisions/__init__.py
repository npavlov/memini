"""The decision tools: manual operations on the knowledge journal.

The library behind the MCP write tools (:mod:`memini.mcp.decisions`) and their HTTP routes
(:mod:`memini.api.decisions`), over :mod:`memini.knowledge`:

- :mod:`memini.decisions.model`: the arguments and results, the same for MCP and HTTP.
- :mod:`memini.decisions.service`: one function per operation (record, supersede, close,
  retract); :class:`DecisionError`, every refusal with an HTTP status, a code and a safe message.

A person records a decision or an open question with its evidence, links an item to earlier
ones it replaces or answers, closes an item, or retracts a wrong one (requirements, sections 3, 5
and 6). Every item recorded here is manual: extraction never supersedes or answers it, so a
correction survives a re-extraction of the same record. The writes need the ``ingest`` scope and
keep a token limited to one workspace inside it.
"""

from memini.decisions.model import (
    DecisionItem,
    EvidenceArgument,
    EvidenceInfo,
    ItemArguments,
    LinkInfo,
    NewDecision,
    ParticipantInfo,
    RecordDecisionArguments,
    SupersedeArguments,
    SupersedeBody,
    SupersedesArgument,
)
from memini.decisions.service import (
    NOT_ACTIVE_CONSTRAINT,
    SUPERSESSION_CONSTRAINTS,
    DecisionError,
    decision_errors,
    run_close,
    run_record_decision,
    run_retract,
    run_supersede,
)

__all__ = [
    "NOT_ACTIVE_CONSTRAINT",
    "SUPERSESSION_CONSTRAINTS",
    "DecisionError",
    "DecisionItem",
    "EvidenceArgument",
    "EvidenceInfo",
    "ItemArguments",
    "LinkInfo",
    "NewDecision",
    "ParticipantInfo",
    "RecordDecisionArguments",
    "SupersedeArguments",
    "SupersedeBody",
    "SupersedesArgument",
    "decision_errors",
    "run_close",
    "run_record_decision",
    "run_retract",
    "run_supersede",
]
