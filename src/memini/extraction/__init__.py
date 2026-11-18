"""Decision extraction: the interface between records and the knowledge journal.

Requirements, section 5 (Decision extraction, Supersession). This package holds what an extractor
reads and returns and the two protocols it implements (:mod:`memini.extraction.model`), and how a
stored thread or meeting becomes its input (:func:`record_input`). The decision eval
(:mod:`memini.evaluation.decisions`, ``memini eval decisions``) measures implementations of the
protocols; the LLM pipeline implements them through :class:`memini.llm.LlmGate` and stores what
they return with :func:`memini.knowledge.record_extraction`.
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
