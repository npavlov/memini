"""The synthetic comms fixtures (``eval/comms``) against their own labels, without a database.
Acceptance of memini-dh2: the fixtures live in one directory with a marker that they are
synthetic, and every reference of the answer keys matches the records: it resolves, its anchor
exists, and what the key says about the metadata (who attended, whom a message is to, when a
record expires, which records are bots') holds. Ingest itself: test_comms_fixtures_db.py."""

import re
from datetime import datetime, timedelta
from typing import Any, cast

from comms_fixtures import (
    COMMS,
    SOURCES,
    FinalRecord,
    RecordRef,
    anchor_time,
    anchors_of,
    attended,
    checked_ranges,
    decision_labels,
    deliveries,
    delivery,
    final_records,
    me,
    messages_to,
    names_of,
    parse_time,
    people,
    person_of,
    query_labels,
    second_sync,
    timeline_labels,
)
from memini.api.ingest import BatchBody
from memini.ingest import DEFAULT_LIMITS, prepare_batch

type Json = dict[str, Any]


def live(ref: RecordRef) -> FinalRecord:
    record = final_records().get(ref)
    assert record is not None, f"no record {ref}"
    assert not record.deleted, f"{ref} is deleted"
    return record


def refs(entries: list[Json]) -> list[RecordRef]:
    return [RecordRef.of(entry) for entry in entries]


# --- The directory -------------------------------------------------------------------------------


def test_the_directory_is_marked_synthetic() -> None:
    marker = (COMMS / "SYNTHETIC.md").read_text(encoding="utf-8")
    assert marker.startswith("# SYNTHETIC DATA\n")
    assert "invented" in marker
    assert people()["synthetic"] is True
    for labels in (decision_labels(), query_labels(), timeline_labels()):
        assert labels["labels"] is True
        assert labels["workspace"] == "harbor"
    assert sorted(path.name for path in (COMMS / "batches").iterdir()) == sorted(SOURCES)


ADDRESS = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
LINK = re.compile(r"https?://([\w.-]+)")


def test_addresses_and_links_use_reserved_domains_only() -> None:
    domains: set[str] = set()
    for path in COMMS.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            domains |= set(ADDRESS.findall(text)) | set(LINK.findall(text))
    assert domains
    assert {d for d in domains if not d.endswith((".example.test", ".example.org"))} == set()


# --- The batches ---------------------------------------------------------------------------------


def test_every_batch_is_a_request_body_the_contract_takes() -> None:
    assert len(deliveries()) == 21
    for each in deliveries():
        BatchBody.model_validate(each.body)
        prepare_batch(each.batch, DEFAULT_LIMITS)
        stream = each.name.split("/")[1].split("-", 1)[1].removesuffix(".json")
        if each.source == "docs":
            assert each.stream is None, each.name
            assert each.window is None, each.name
        else:
            assert each.stream == stream, each.name
            assert each.window is not None, each.name


def test_windows_chain_without_a_gap_to_the_final_checkpoints() -> None:
    expected = {
        (s["source"], s["stream"]): (parse_time(s["checked_from"]), parse_time(s["checked_until"]))
        for s in cast("list[Json]", timeline_labels()["final"]["streams"])
    }
    assert checked_ranges() == expected


def test_the_final_state_is_the_one_the_timeline_labels_give() -> None:
    final = timeline_labels()["final"]
    counts = {source: 0 for source in SOURCES}
    for record in final_records().values():
        if not record.deleted:
            counts[record.ref.source] += 1
    assert counts == final["live"]
    deleted = {ref for ref, record in final_records().items() if record.deleted}
    assert deleted == set(refs(final["deleted"]))


def test_the_timeline_holds_each_case_of_the_ingest_contract() -> None:
    cases = timeline_labels()["scenarios"]

    late = cases["late_reply"]
    ref = RecordRef.of(late["record"])
    first, again = delivery(late["first"]).item(ref), delivery(late["again"]).item(ref)
    assert first is not None
    assert again is not None
    first_ids = [m["id"] for m in first["payload"]["messages"]]
    again_ids = [m["id"] for m in again["payload"]["messages"]]
    assert again_ids == first_ids + late["new_messages"]
    assert again["occurred_at"] == again["payload"]["messages"][-1]["sent_at"]
    assert again["occurred_at"] > first["occurred_at"]

    edit = cases["edited_message"]
    ref = RecordRef.of(edit["record"])
    first, again = delivery(edit["first"]).item(ref), delivery(edit["again"]).item(ref)
    assert first is not None
    assert again is not None
    before = {m["id"]: m["text"] for m in first["payload"]["messages"]}
    after = {m["id"]: m["text"] for m in again["payload"]["messages"]}
    assert before.keys() == after.keys()
    assert [key for key in before if before[key] != after[key]] == [edit["message"]]
    assert first["occurred_at"] == again["occurred_at"]

    gone = cases["deleted_thread"]
    ref = RecordRef.of(gone["record"])
    assert delivery(gone["first"]).item(ref) is not None
    assert delivery(gone["tombstone"]).item(ref) == {
        "kind": ref.kind,
        "external_id": ref.external_id,
        "deleted": True,
    }

    grows = cases["growing_mail_thread"]
    ref = RecordRef.of(grows["record"])
    versions = [delivery(name).item(ref) for name in grows["deliveries"]]
    sizes = [len(cast("Json", v)["payload"]["messages"]) for v in versions]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]
    assert versions[-1] == versions[-2]

    partial = cases["incomplete_window"]
    window = delivery(partial["delivery"]).window
    retry = delivery(partial["retried_by"]).window
    assert window is not None
    assert retry is not None
    assert window["complete"] is False
    assert retry["complete"] is True
    assert retry["from"] == window["from"]
    stream = ("mail", "inbox")
    previous = deliveries()[[d.name for d in deliveries()].index(partial["delivery"]) - 1].name
    assert checked_ranges(partial["delivery"])[stream] == checked_ranges(previous)[stream]
    assert checked_ranges(partial["retried_by"])[stream][1] == parse_time(retry["to"])

    snap = cases["snapshot_deletion"]
    ref = RecordRef.of(snap["record"])
    first = delivery(snap["first"]).item(ref)
    window = delivery(snap["snapshot"]).window
    assert first is not None
    assert window is not None
    assert window["snapshot"] is True
    assert delivery(snap["snapshot"]).item(ref) is None
    assert parse_time(window["from"]) <= parse_time(first["occurred_at"]) < parse_time(window["to"])
    assert final_records()[ref].deleted


def test_the_second_sync_holds_every_live_record_once() -> None:
    sent = [
        RecordRef(source, record.kind, record.external_id)
        for source, batch in second_sync()
        for record in batch.records
    ]
    assert len(sent) == len(set(sent))
    assert set(sent) == {ref for ref, record in final_records().items() if not record.deleted}
    for _, batch in second_sync():
        prepare_batch(batch, DEFAULT_LIMITS)


# --- The cast ------------------------------------------------------------------------------------


def test_every_name_in_the_batches_is_an_alias_of_the_cast() -> None:
    cast_people = cast("list[Json]", people()["people"])
    assert [p["key"] for p in cast_people if p.get("me")] == [me()] == ["mira"]
    for each in deliveries():
        for item in each.items:
            if item.get("deleted"):
                continue
            for name in names_of(item["kind"], item["payload"]):
                assert person_of(name) is not None, f"{each.name}: {name!r} is nobody"


def test_every_person_of_the_cast_appears_in_the_batches() -> None:
    seen = {
        person_of(name)
        for record in final_records().values()
        for name in names_of(record.ref.kind, record.payload)
    }
    keys = {str(p["key"]) for p in cast("list[Json]", people()["people"])}
    assert keys <= seen


# --- The decision labels -------------------------------------------------------------------------


def test_decision_labels_point_at_records_and_agree_with_them() -> None:
    labels = decision_labels()
    keys = {str(p["key"]) for p in cast("list[Json]", people()["people"])}
    items = {item["id"]: item for item in cast("list[Json]", labels["items"])}
    assert len(items) == len(labels["items"])

    for item in items.values():
        assert item["kind"] in ("decision", "open_question")
        assert set(item["participants"]) <= keys, item["id"]
        times: list[datetime | None] = []
        for evidence in item["evidence"]:
            record = live(RecordRef.of(evidence))
            assert evidence["anchor"] in anchors_of(record), item["id"]
            times.append(anchor_time(record, evidence["anchor"]))
        # decided_at is the time of the first anchor: the message or segment that says it.
        assert times[0] == parse_time(item["decided_at"]), item["id"]

    status = {key: "active" for key in items}
    for link in labels["links"]:
        new, old = items[link["from"]], items[link["to"]]
        assert parse_time(new["decided_at"]) >= parse_time(old["decided_at"])
        if link["relation"] == "replaces":
            status[old["id"]] = "superseded"
        else:
            assert link["relation"] == "answers"
            assert (new["kind"], old["kind"]) == ("decision", "open_question")
            status[old["id"]] = "closed"
    assert status == {key: item["status"] for key, item in items.items()}

    labelled: set[RecordRef] = {
        RecordRef.of(evidence) for item in items.values() for evidence in item["evidence"]
    }
    for entry in [*labels["restatements"], *labels["traps"]]:
        record = live(RecordRef.of(entry["record"]))
        assert set(entry["anchors"]) <= anchors_of(record)
        labelled.add(record.ref)
    for entry in labels["restatements"]:
        assert entry["item"] in items

    correction = labels["corrections"][0]
    trap_ids = {trap["id"] for trap in labels["traps"]}
    assert correction["corrects"] in trap_ids
    body = correction["body"]
    assert [s["label"] for s in body["supersedes"]] == [correction["corrects"]]
    assert set(body["participants"]) <= keys
    for evidence in body["evidence"]:
        assert evidence["anchor"] in anchors_of(live(RecordRef.of(evidence)))

    nothing = refs(labels["no_items"])
    assert len(nothing) == len(set(nothing))
    for ref in nothing:
        live(ref)
    assert labelled.isdisjoint(nothing)
    extractable = {
        ref
        for ref, record in final_records().items()
        if ref.kind in ("thread", "meeting") and not record.deleted
    }
    assert labelled | set(nothing) == extractable


# --- The query keys ------------------------------------------------------------------------------


def test_meetings_i_attended_agree_with_the_participants() -> None:
    keys = query_labels()
    assert keys["me"] == me()
    for query in keys["meetings_attended"]:
        for ref in refs(query["expect"]):
            assert ref.kind == "meeting"
            assert attended(live(ref), me()), (query["id"], ref)
        for entry in query["not_expected"]:
            ref, why = RecordRef.of(entry), entry["why"]
            if why == "deleted":
                assert final_records()[ref].deleted, (query["id"], ref)
            elif why == "absent":
                assert not attended(live(ref), me()), (query["id"], ref)
            else:
                assert why == "off_topic"
                assert attended(live(ref), me()), (query["id"], ref)


def test_messages_to_me_agree_with_recipients_and_mentions() -> None:
    for query in query_labels()["messages_to_me"]:
        for entry in query["expect"]:
            record = live(RecordRef.of(entry))
            assert entry["messages"]
            assert set(entry["messages"]) <= set(messages_to(record, me())), query["id"]
        for entry in query["not_expected"]:
            ref, why = RecordRef.of(entry), entry["why"]
            if why == "deleted":
                record = final_records()[ref]
                assert record.deleted, (query["id"], ref)
                assert messages_to(record, me()), (query["id"], ref)
            elif why == "not_to_me":
                assert messages_to(live(ref), me()) == [], (query["id"], ref)
            else:
                assert why == "off_topic"
                assert messages_to(live(ref), me()), (query["id"], ref)


def test_retention_expires_the_records_before_the_cutoff() -> None:
    key = query_labels()["retention"]
    cutoff = parse_time(key["as_of"]) - timedelta(days=key["retention_days"])
    expired = {
        ref
        for ref, record in final_records().items()
        if not record.deleted and ref.source in key["sources"] and record.occurred_at < cutoff
    }
    assert set(refs(key["expired"])) == expired
    for entry in key["kept_at_the_edge"]:
        ref = RecordRef.of(entry)
        assert ref not in expired
        live(ref)


def test_the_bot_records_are_exactly_those_of_bot_authors() -> None:
    key = query_labels()["bots"]
    bots = {str(p["key"]) for p in cast("list[Json]", people()["people"]) if p.get("bot")}
    authors = {
        name for names in cast("dict[str, list[str]]", key["authors"]).values() for name in names
    }
    assert {person_of(name) for name in authors} == bots
    by_bots: set[RecordRef] = set()
    for ref, record in final_records().items():
        if ref.kind != "thread" or record.deleted:
            continue
        written = {person_of(str(m["author"])) for m in record.payload["messages"]}
        if written & bots:
            assert written <= bots, f"{ref} mixes bots and people"
            by_bots.add(ref)
    assert by_bots == set(refs(key["records"]))
