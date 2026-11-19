"""The injection corpus of ``eval/injection`` for the tests: its batches, its attempts and its
code, the texts that carry an attempt, and a reader of the fenced blocks of a tool's text content.

Everything there is invented (``eval/injection/SYNTHETIC.md``); ``eval/injection/README.md``
lists the attempts. The batches are push API bodies, one per file, in the layout of
``eval/comms``: ``batches/<source>/NN-<stream>.json``.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, cast

from memini.api.ingest import BatchBody
from memini.ingest import Batch

__all__ = [
    "CODE",
    "CORPUS",
    "INSERTS",
    "MARKERS",
    "SOURCES",
    "TEMPLATE_TOKENS",
    "Delivery",
    "attempts",
    "code_files",
    "deliveries",
    "fences",
    "records",
    "texts_of",
]

type Json = dict[str, Any]

CORPUS: Final = Path(__file__).resolve().parents[1] / "eval" / "injection"
INSERTS: Final = CORPUS / "inserts.json"
CODE: Final = CORPUS / "code"
SOURCES: Final = ("chat", "mail", "meetings", "docs")

TEMPLATE_TOKENS: Final = (
    "<|im_start|>",
    "<|im_end|>",
    "<|eot_id|>",
    "<|start_header_id|>",
    "<start_of_turn>",
    "<end_of_turn>",
    "[INST]",
    "[/INST]",
)
"""Chat-template tokens the corpus holds: none may reach a model as such."""
MARKERS: Final = ("<<<END DATA", "<<<BEGIN DATA")
"""The shapes of the data block's marker lines, which the corpus forges."""


@dataclass(frozen=True, slots=True)
class Delivery:
    source: str
    name: str
    body: Json

    @property
    def batch(self) -> Batch:
        return BatchBody.model_validate(self.body).to_batch()


@cache
def deliveries() -> tuple[Delivery, ...]:
    """Every batch of the corpus, source by source, in the order of the file names."""
    found: list[Delivery] = []
    for source in SOURCES:
        for path in sorted((CORPUS / "batches" / source).glob("*.json")):
            body = cast(Json, json.loads(path.read_text(encoding="utf-8")))
            found.append(Delivery(source, f"{source}/{path.name}", body))
    return tuple(found)


def records() -> Iterator[tuple[str, Json]]:
    """Every record of the batches with its source."""
    for each in deliveries():
        for record in cast("list[Json]", each.body["records"]):
            yield each.source, record


def texts_of(record: Json) -> list[str]:
    """The texts a record holds, as the sources wrote them: messages, a meeting's summary,
    action items and segments, a document's title and text."""
    payload = cast(Json, record["payload"])
    found: list[str] = []
    for key in ("title", "summary", "text"):
        if isinstance(payload.get(key), str):
            found.append(cast(str, payload[key]))
    for key in ("messages", "transcript", "action_items"):
        for part in cast("list[Json]", payload.get(key) or []):
            found.append(cast(str, part["text"]))
    return found


@cache
def attempts() -> tuple[str, ...]:
    """The texts of the inserts file: one injection attempt each."""
    data = cast(Json, json.loads(INSERTS.read_text(encoding="utf-8")))
    return tuple(cast(str, each["text"]) for each in cast("list[Json]", data["inserts"]))


def code_files() -> dict[str, str]:
    """The files of the corpus's code, by path below ``code/``."""
    return {
        path.relative_to(CODE).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(CODE.rglob("*"))
        if path.is_file()
    }


_OPEN: Final = re.compile(r" {0,3}(`{3,})([^`]*)")
_CLOSE: Final = re.compile(r" {0,3}(`{3,})[ \t]*")


def fences(text: str) -> tuple[list[str], list[str]]:
    """The fenced blocks of ``text`` (backtick fences as CommonMark reads them, at the top
    level) and the lines outside them. Fails on a block that is never closed."""
    blocks: list[str] = []
    outside: list[str] = []
    opened: int | None = None
    current: list[str] = []
    for line in text.split("\n"):
        if opened is None:
            found = _OPEN.fullmatch(line)
            if found is None:
                outside.append(line)
            else:
                opened, current = len(found.group(1)), []
            continue
        closing = _CLOSE.fullmatch(line)
        if closing is not None and len(closing.group(1)) >= opened:
            blocks.append("\n".join(current))
            opened = None
        else:
            current.append(line)
    assert opened is None, "a fenced block is never closed"
    return blocks, outside
