"""Decision extraction: the interface between records and the knowledge journal.

Requirements, section 5 (Decision extraction, Supersession). This package holds what an extractor
reads and returns and the two protocols it implements (:mod:`memini.extraction.model`), and how a
stored thread or meeting becomes its input (:func:`record_input`). The decision eval
(:mod:`memini.evaluation.decisions`, ``memini eval decisions``) measures implementations of the
protocols. The LLM pipeline: :mod:`memini.extraction.llm`, the prompts and the two calls through
:class:`memini.llm.LlmGate`; :mod:`memini.extraction.job`, the ``extract`` job, which finds the
candidates of a supersession check and stores what the calls return with
:func:`memini.knowledge.record_extraction`. Neither is imported here: they need the database and
the LLM, and the model of this package does not.
"""

from memini.extraction.inputs import ACTION_ITEMS_ANCHOR, SUMMARY_ANCHOR, record_input
from memini.extraction.model import (
    EXTRACTED_KINDS,
    PART_KINDS,
    VERDICT_LABELS,
    Candidate,
    CandidateEvidence,
    ExtractedKind,
    Extractor,
    Part,
    PartKind,
    ProposedItem,
    RecordInput,
    RecordRef,
    Supersedes,
    SupersessionClassifier,
    Verdict,
    VerdictLabel,
)

__all__ = [
    "ACTION_ITEMS_ANCHOR",
    "EXTRACTED_KINDS",
    "PART_KINDS",
    "SUMMARY_ANCHOR",
    "VERDICT_LABELS",
    "Candidate",
    "CandidateEvidence",
    "ExtractedKind",
    "Extractor",
    "Part",
    "PartKind",
    "ProposedItem",
    "RecordInput",
    "RecordRef",
    "Supersedes",
    "SupersessionClassifier",
    "Verdict",
    "VerdictLabel",
    "record_input",
]
