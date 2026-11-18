"""Checks of the knowledge layer's input that run before the database: no database needed.

Every statement and id here is synthetic.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from memini.knowledge import (
    MANUAL,
    MAX_EVIDENCE,
    MAX_STATEMENT_LENGTH,
    EvidenceRef,
    InvalidItemError,
    ItemFilters,
    NewItem,
    SupersedesRef,
    check_extractor,
)
from memini.knowledge.model import (
    check_anchor,
    check_content_hash,
    check_new_item,
    check_record_id,
    check_supersedes,
)

MONDAY = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
RECORD = "ab" * 32
OTHER_RECORD = "cd" * 32
COMMIT = "4f" * 20
ITEM = UUID("00000000-0000-7000-8000-000000000001")
PERSON = UUID("00000000-0000-7000-8000-000000000002")


def new_item(**changes: object) -> NewItem:
    fields: dict[str, object] = {
        "kind": "decision",
        "statement": "The pump runs in the morning.",
        "decided_at": MONDAY,
        "evidence": (EvidenceRef(RECORD, "m1"),),
    }
    fields.update(changes)
    return NewItem(**fields)  # pyright: ignore[reportArgumentType]


def test_a_new_item_is_trimmed_and_its_references_deduplicated() -> None:
    checked = check_new_item(
        new_item(
            statement="  The pump runs in the morning.\n",
            evidence=[
                EvidenceRef(RECORD, " m1 "),
                EvidenceRef(RECORD, "m1"),
                EvidenceRef(OTHER_RECORD),
            ],
            participants=[PERSON, PERSON],
            supersedes=[SupersedesRef(ITEM), SupersedesRef(ITEM)],
        )
    )

    assert checked.statement == "The pump runs in the morning."
    assert checked.evidence == (EvidenceRef(RECORD, "m1"), EvidenceRef(OTHER_RECORD))
    assert checked.participants == (PERSON,)
    assert checked.supersedes == (SupersedesRef(ITEM, "replaces"),)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        pytest.param({"kind": "rumour"}, "kind", id="kind"),
        pytest.param({"statement": " \n "}, "statement", id="empty-statement"),
        pytest.param(
            {"statement": "x" * (MAX_STATEMENT_LENGTH + 1)}, "statement", id="long-statement"
        ),
        pytest.param({"statement": "Water\x00 at six."}, "statement", id="nul-in-statement"),
        pytest.param({"decided_at": datetime(2026, 3, 2, 10, 0)}, "decided_at", id="naive-time"),
        pytest.param({"evidence": ()}, "evidence", id="no-evidence"),
        pytest.param(
            {"evidence": [EvidenceRef(RECORD, f"m{n}") for n in range(MAX_EVIDENCE + 1)]},
            "evidence",
            id="too-much-evidence",
        ),
        pytest.param({"evidence": [EvidenceRef("ABC", "m1")]}, "evidence", id="record-id"),
        pytest.param({"evidence": [EvidenceRef(RECORD, "  ")]}, "evidence", id="blank-anchor"),
        pytest.param({"evidence": [EvidenceRef(RECORD, "m\n1")]}, "evidence", id="control-anchor"),
        pytest.param(
            {"supersedes": [SupersedesRef(ITEM, "replaces"), SupersedesRef(ITEM, "answers")]},
            "supersedes",
            id="two-relations",
        ),
        pytest.param(
            {"supersedes": [SupersedesRef(ITEM, "undoes")]},  # pyright: ignore[reportArgumentType]
            "supersedes",
            id="relation",
        ),
    ],
)
def test_a_new_item_is_refused_with_the_field_named(changes: dict[str, object], field: str) -> None:
    with pytest.raises(InvalidItemError) as refused:
        check_new_item(new_item(**changes))

    assert refused.value.field == field


def test_a_refusal_never_repeats_the_value() -> None:
    value = "not-a-record-id-0000"
    with pytest.raises(InvalidItemError) as refused:
        check_new_item(new_item(evidence=[EvidenceRef(value)]))

    assert value not in str(refused.value)


@pytest.mark.parametrize(
    ("kind", "anchor", "ok"),
    [
        ("thread", "1700000000.000100", True),
        ("thread", None, False),
        ("meeting", "00:12:30", True),
        ("meeting", None, False),
        ("code_file", COMMIT, True),
        ("code_file", f"{COMMIT}#Pump.speed", True),
        ("code_file", f"{'ab' * 32}#L12-L30", True),
        ("code_file", "Pump.speed", False),
        ("code_file", f"{COMMIT}#", False),
        ("code_file", None, False),
        ("document", None, True),
        ("document", "Pump > Speed", True),
    ],
)
def test_anchors_fit_the_kind_of_record(kind: str, anchor: str | None, ok: bool) -> None:
    if ok:
        check_anchor(kind, anchor)  # pyright: ignore[reportArgumentType]
        return
    with pytest.raises(InvalidItemError):
        check_anchor(kind, anchor)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "value", ["qwen3-8b@decisions-v1", "ollama/qwen3:8b+prompt=v2", "m", "a" * 200]
)
def test_extractor_names(value: str) -> None:
    assert check_extractor(value) == value


@pytest.mark.parametrize("value", [MANUAL, "", " model", "model v1", "-model", "a" * 201, "mödel"])
def test_extractor_names_refused(value: str) -> None:
    with pytest.raises(InvalidItemError) as refused:
        check_extractor(value)

    assert refused.value.field == "extractor"


def test_record_ids_and_content_hashes() -> None:
    assert check_record_id(RECORD) == RECORD
    with pytest.raises(InvalidItemError):
        check_record_id(RECORD.upper())
    assert check_content_hash("c0" * 32) == "c0" * 32
    for bad in ("", "x" * 201, "a\nb"):
        with pytest.raises(InvalidItemError):
            check_content_hash(bad)


def test_supersedes_keeps_one_relation_per_item() -> None:
    assert check_supersedes([SupersedesRef(ITEM, "answers"), SupersedesRef(ITEM, "answers")]) == (
        SupersedesRef(ITEM, "answers"),
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        pytest.param({"limit": 0}, "limit", id="limit-low"),
        pytest.param({"limit": 501}, "limit", id="limit-high"),
        pytest.param({"statuses": ()}, "statuses", id="no-status"),
        pytest.param({"statuses": ("done",)}, "statuses", id="status"),
        pytest.param({"kinds": ()}, "kinds", id="no-kind"),
        pytest.param({"record_ids": ("x",)}, "record_ids", id="record-id"),
        pytest.param({"decided_from": datetime(2026, 1, 1)}, "decided_from", id="naive-from"),
    ],
)
def test_filters_are_checked(changes: dict[str, object], field: str) -> None:
    with pytest.raises(InvalidItemError) as refused:
        ItemFilters(**changes)  # pyright: ignore[reportArgumentType]

    assert refused.value.field == field


def test_the_default_filter_is_the_active_view() -> None:
    assert ItemFilters().statuses == ("active",)
