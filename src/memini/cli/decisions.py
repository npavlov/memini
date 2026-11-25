"""Client commands for the knowledge journal: ``memini decision record|supersede|close|retract``.

Each is one call to a decision route (:mod:`memini.api.decisions`), the HTTP face of the MCP write
tools ``record_decision``, ``supersede_decision``, ``close_decision`` and ``retract_decision``,
with the same arguments; they need a token with the ``ingest`` scope. Every item recorded so is
manual: extraction never supersedes or answers it. Usage::

    memini decision record <workspace> --statement TEXT [--kind decision|open_question]
                           [--occurred-at TIME] --evidence REFERENCE [--evidence ...]
                           [--participant PERSON_ID ...] [--supersedes ITEM_ID[:RELATION] ...]
    memini decision supersede <workspace> <id> --supersedes ITEM_ID[:RELATION] [...]
    memini decision close <workspace> <id>
    memini decision retract <workspace> <id>

A piece of evidence names a record and where in it: ``RECORD_ID[#ANCHOR]``, the record's
64-character id as results give it. The first ``#`` starts the anchor, which may hold more of them
(``<record id>#<commit>#L12-L30``). ``--supersedes`` takes an earlier item's id, with
``:replaces`` (the default: it becomes superseded) or ``:answers`` (a decision answers an open
question, which becomes closed). ``--occurred-at``, when it was said or asked, is ISO 8601 with a
zone offset or ``Z``; it defaults to now.

Each command prints the item as the service answers it: its fields, evidence, participants and
links; the statement is data, printed with control characters escaped. ``--json`` prints the
API's answer as it is. A refusal of the append-only journal answers 409 and its message names the
database constraint (``not_active``, ``supersession_refused``, ``journal_refused``); it is printed
as one line and the command exits 1.
"""

import argparse
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from memini.cli.arguments import Commands, parse_workspace_name
from memini.cli.client import Api, Json, segment
from memini.cli.common import EXIT_OK
from memini.cli.output import NONE, as_list, as_mapping, print_fields, print_json, text, timestamp
from memini.knowledge import ITEM_KINDS, RELATIONS

__all__ = [
    "add_commands",
    "decisions_path",
    "item_path",
    "parse_evidence",
    "parse_supersedes",
    "parse_time",
    "print_item",
]

_RECORD_ID: Final = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_FORM: Final = (
    "evidence is a RECORD_ID (64 hex characters), optionally followed by #ANCHOR"
)


def decisions_path(workspace: str) -> str:
    """The API path of a workspace's decisions."""
    return f"v1/workspaces/{segment(workspace)}/decisions"


def item_path(workspace: str, item_id: UUID, action: str) -> str:
    """The API path of an action on one item: ``supersede``, ``close`` or ``retract``."""
    return f"{decisions_path(workspace)}/{item_id}/{action}"


# --- Argument types ------------------------------------------------------------------------------


def parse_evidence(value: str) -> Json:
    """``RECORD_ID[#ANCHOR]`` as an evidence argument of the API."""
    reference, has_anchor, anchor = value.partition("#")
    if has_anchor and not anchor:
        raise argparse.ArgumentTypeError("an anchor after '#' is not empty")
    if not _RECORD_ID.fullmatch(reference.lower()):
        raise argparse.ArgumentTypeError(_EVIDENCE_FORM)
    evidence: Json = {"record_id": reference.lower()}
    if has_anchor:
        evidence["anchor"] = anchor
    return evidence


def parse_supersedes(value: str) -> Json:
    """``ITEM_ID[:replaces|answers]`` as a supersedes argument of the API."""
    item, has_relation, relation = value.partition(":")
    try:
        item_id = UUID(item)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "an earlier item is its id, optionally followed by :replaces or :answers"
        ) from None
    if not has_relation:
        relation = "replaces"
    elif relation not in RELATIONS:
        raise argparse.ArgumentTypeError(f"a relation is {' or '.join(RELATIONS)}")
    return {"item_id": str(item_id), "relation": relation}


def parse_time(value: str) -> datetime:
    """An ISO 8601 time with a zone offset or ``Z``, e.g. ``2026-03-02T10:00:00Z``."""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "a time is ISO 8601 with a zone offset or Z, e.g. 2026-03-02T10:00:00Z"
        )
    return moment


# --- Commands ------------------------------------------------------------------------------------


def record_command(args: argparse.Namespace, api: Api) -> int:
    occurred_at: datetime = args.occurred_at or datetime.now(UTC)
    participants: list[UUID] = args.participant or []
    body: Json = {
        "kind": args.kind,
        "statement": args.statement,
        "occurred_at": occurred_at.isoformat(),
        "evidence": args.evidence,
        "participants": [str(person) for person in participants],
        "supersedes": args.supersedes or [],
    }
    return _show(args, api.post(decisions_path(args.workspace), body))


def supersede_command(args: argparse.Namespace, api: Api) -> int:
    body: Json = {"supersedes": args.supersedes}
    return _show(args, api.post(item_path(args.workspace, args.id, "supersede"), body))


def close_command(args: argparse.Namespace, api: Api) -> int:
    return _show(args, api.post(item_path(args.workspace, args.id, "close")))


def retract_command(args: argparse.Namespace, api: Api) -> int:
    return _show(args, api.post(item_path(args.workspace, args.id, "retract")))


def _show(args: argparse.Namespace, item: Json) -> int:
    if args.json:
        print_json(item)
    else:
        print_item(item)
    return EXIT_OK


# --- Output --------------------------------------------------------------------------------------


def print_item(item: Mapping[str, Any]) -> None:
    """Print a knowledge item readably: its fields, then one line per piece of evidence,
    participant and link."""
    fields = [
        ("id", text(item.get("id"))),
        ("workspace", text(item.get("workspace"))),
        ("kind", text(item.get("kind"))),
        ("status", text(item.get("status"))),
        ("statement", text(item.get("statement"))),
        ("occurred", timestamp(item.get("occurred_at"))),
        ("created", timestamp(item.get("created_at"))),
    ]
    if item.get("invalidated_at") is not None:
        fields.append(("invalidated", timestamp(item.get("invalidated_at"))))
    fields.append(("extractor", text(item.get("extractor"))))
    fields += _lines("evidence", [_evidence(as_mapping(e)) for e in as_list(item.get("evidence"))])
    fields += _lines(
        "participants",
        [
            f"{text(person.get('display_name'))} ({text(person.get('person_id'))})"
            for person in map(as_mapping, as_list(item.get("participants")))
        ],
    )
    fields += _lines(
        "supersedes",
        [
            f"{text(link.get('supersedes_id'))} ({text(link.get('relation'))})"
            for link in map(as_mapping, as_list(item.get("supersedes")))
        ],
    )
    by = as_mapping(item.get("superseded_by"))
    if by:
        fields.append(("superseded by", f"{text(by.get('item_id'))} ({text(by.get('relation'))})"))
    print_fields(fields)


def _evidence(evidence: Mapping[str, Any]) -> str:
    """A piece of evidence in the form ``--evidence`` takes, with the record's kind, source,
    external id and state."""
    reference = text(evidence.get("record_id"))
    anchor = evidence.get("anchor")
    if anchor is not None:
        reference += f"#{text(anchor)}"
    source = evidence.get("source")
    where = "" if source is None else f"{text(source)}:{text(evidence.get('external_id'))}, "
    return (
        f"{reference}  ({where}{text(evidence.get('record_kind'))},"
        f" {text(evidence.get('record_state'))})"
    )


def _lines(name: str, values: Sequence[str]) -> list[tuple[str, str]]:
    """``name`` on the first value's line only; :data:`NONE` for no values."""
    if not values:
        return [(name, NONE)]
    return [(name, values[0]), *(("", value) for value in values[1:])]


# --- The parser ----------------------------------------------------------------------------------


def _item_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    parser.add_argument("id", metavar="id", type=UUID, help="the item's id")


def _supersedes_option(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--supersedes",
        action="append",
        required=required,
        type=parse_supersedes,
        metavar="ITEM_ID[:RELATION]",
        help="an active earlier item of the workspace, said no later than this one, that it"
        " replaces (:replaces, the default: it becomes superseded) or, for a decision, answers"
        " (:answers, an open question: it becomes closed); repeat for more",
    )


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the API's JSON answer")


def add_commands(commands: Commands) -> None:
    """Add ``decision record|supersede|close|retract``."""
    decision = commands.add_client(
        "decision",
        help="record, supersede, close and retract decisions (ingest)",
        description="Record decisions and open questions by hand, link them to earlier items they"
        " replace or answer, close or retract them: the manual operations on the knowledge"
        " journal, which is append-only. Items recorded here are manual: extraction never"
        " supersedes or answers them. Needs a token with the ingest scope.",
    )
    decision_commands = decision.add_subparsers(metavar="<command>", required=True)

    record = decision_commands.add_parser(
        "record",
        help="record a decision or an open question with its evidence",
        description="Record a decision or an open question with the records it comes from, the"
        " persons involved and the active items it replaces or answers. Evidence is"
        " SOURCE:EXTERNAL_ID[#ANCHOR], the source by name and the record's id in it as search"
        " results give them, or RECORD_ID[#ANCHOR]; the first # starts the anchor. A thread or a"
        " meeting needs an anchor (a message id, a segment, a time offset), a code file its"
        " commit (<sha>, <sha>#Symbol or <sha>#L12-L30); a document without one is cited whole.",
    )
    record.add_argument("workspace", type=parse_workspace_name, help="the workspace's name")
    record.add_argument(
        "--statement",
        required=True,
        metavar="TEXT",
        help="one or two sentences: what was decided or asked",
    )
    record.add_argument(
        "--kind",
        choices=ITEM_KINDS,
        default="decision",
        help="decision (the default) or open_question",
    )
    record.add_argument(
        "--occurred-at",
        type=parse_time,
        metavar="TIME",
        help="when it was said or asked, as the evidence shows: ISO 8601 with a zone offset or Z,"
        " e.g. 2026-03-02T10:00:00Z; default: now",
    )
    record.add_argument(
        "--evidence",
        action="append",
        required=True,
        type=parse_evidence,
        metavar="REFERENCE",
        help="RECORD_ID[#ANCHOR]: a record of the workspace the item comes from, by the id"
        " results give it; repeat for more",
    )
    record.add_argument(
        "--participant",
        action="append",
        type=UUID,
        metavar="PERSON_ID",
        help="a person involved, by id, in the workspace; repeat for more",
    )
    _supersedes_option(record, required=False)
    _json_option(record)
    record.set_defaults(client=record_command)

    supersede = decision_commands.add_parser(
        "supersede",
        help="link an item to earlier items it replaces or answers",
        description="Link an active item to earlier active items it replaces (they become"
        " superseded) or, for a decision, open questions it answers (they become closed). The"
        " item stays active. Refused for a cycle, the wrong relation, an earlier item said later,"
        " or an extracted item over one recorded by hand.",
    )
    _item_arguments(supersede)
    _supersedes_option(supersede, required=True)
    _json_option(supersede)
    supersede.set_defaults(client=supersede_command)

    close = decision_commands.add_parser(
        "close",
        help="close an item without a replacement",
        description="Close an active item: an open question no longer asked, a decision no"
        " longer in force.",
    )
    _item_arguments(close)
    _json_option(close)
    close.set_defaults(client=close_command)

    retract = decision_commands.add_parser(
        "retract",
        help="retract a wrong item",
        description="Retract an active item that was wrong: a misread extraction, a mistake. It"
        " stays in the history, marked retracted.",
    )
    _item_arguments(retract)
    _json_option(retract)
    retract.set_defaults(client=retract_command)
