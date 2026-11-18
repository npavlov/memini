"""Source filters without a database: globs, addresses, the checks of the rules, what the rules make
of each kind of record, and the filter step of an ingest batch. Every name, address and text here is
made up; addresses are under example.test."""

import json
import time
from dataclasses import replace
from typing import Any

import pytest

from ingest_helpers import HOUR, T0, iso
from memini.filters import (
    MAX_FILTER_PATTERNS,
    FilterError,
    SourceFilters,
    address_of,
    check_filters,
    glob_matches,
)
from memini.ingest import (
    DEFAULT_LIMITS,
    Batch,
    PreparedTombstone,
    RecordIn,
    filter_batch,
    prepare_batch,
    stored_form,
)
from redaction_helpers import GITHUB

BOT = "Deploy Bot <deploy-bot@example.test>"
ANN = "Ann Example <ann@example.test>"
BO = "bo@partner.example.test"


def message(author: str, minute: int, text: str = "Looks good.", **extra: Any) -> dict[str, Any]:
    return {"author": author, "sent_at": iso(T0 + minute * HOUR / 60), "text": text, **extra}


def thread(*messages: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"channel": "release", "title": "Friday release", **fields, "messages": list(messages)}


# --- Globs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "value", "matches"),
    [
        ("*bot*", "Deploy Bot <deploy-bot@example.test>", True),
        ("*bot*", "Ann Example", False),
        ("ann example", "Ann Example", True),
        ("Ann", "Ann Example", False),
        ("eng-*", "eng-backend", True),
        ("eng-*", "sales-eng", False),
        ("*-alerts", "ops-alerts", True),
        ("ops-??", "ops-eu", True),
        ("ops-??", "ops-emea", False),
        ("[ab]-team", "B-team", True),
        ("[!ab]-team", "c-team", True),
        ("[!ab]-team", "a-team", False),
        ("v[0-9]*", "v2 notes", True),
        ("[*]urgent*", "*urgent* fix", True),
        ("[*]urgent*", "urgent fix", False),
        ("*", "", True),
        ("a*b*c", "abc", True),
        ("a*b*c", "aXbYbZc", True),
        ("a*b*c", "acb", False),
        ("*ab*ab", "abab", True),
        ("*ab*ab", "aab", False),
        ("[unclosed", "[unclosed", True),
        ("Ελένη*", "ελένη παράδειγμα", True),
    ],
)
def test_globs_match_the_whole_value_ignoring_case(pattern: str, value: str, matches: bool) -> None:
    assert glob_matches(pattern, value) is matches


def test_a_glob_with_many_stars_does_not_backtrack() -> None:
    pattern = "*a" * 20 + "*b"
    value = "a" * 20_000

    start = time.monotonic()
    assert not glob_matches(pattern, value)
    assert glob_matches(pattern, value + "b")
    assert time.monotonic() - start < 1.0


# --- Addresses -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "address"),
    [
        ("ann@example.test", "ann@example.test"),
        ("  ann@example.test ", "ann@example.test"),
        ("Ann Example <ann@example.test>", "ann@example.test"),
        ('"Example, Ann" < ann@example.test >', "ann@example.test"),
        ("Ann Example", None),
        ("@ann", None),
        ("ann at example.test", None),
    ],
)
def test_the_address_in_a_name(name: str, address: str | None) -> None:
    assert address_of(name) == address


# --- Checks --------------------------------------------------------------------------------------


def test_rules_are_stored_without_duplicates_and_domains_in_lower_case() -> None:
    checked = check_filters(
        SourceFilters(
            allow_channels=("eng-*", "eng-*", "release"),
            allow_domains=("Example.TEST", "example.test.", "partner.example.test"),
            exclude_authors=("*bot*",),
        )
    )

    assert checked.allow_channels == ("eng-*", "release")
    assert checked.allow_domains == ("example.test", "partner.example.test")
    assert checked.to_json() == {
        "allow_channels": ["eng-*", "release"],
        "allow_domains": ["example.test", "partner.example.test"],
        "exclude_authors": ["*bot*"],
    }
    assert SourceFilters.from_json(checked.to_json()) == checked
    assert SourceFilters().to_json() == {}
    assert SourceFilters.from_json({"exclude_titles": "not a list", "other": 1}).empty


@pytest.mark.parametrize(
    ("filters", "setting"),
    [
        (SourceFilters(allow_channels=("",)), "allow_channels"),
        (SourceFilters(exclude_authors=("  ",)), "exclude_authors"),
        (SourceFilters(exclude_titles=("tab\there",)), "exclude_titles"),
        (SourceFilters(exclude_titles=("x" * 257,)), "exclude_titles"),
        (SourceFilters(exclude_authors=("[z-a]*",)), "exclude_authors"),
        (SourceFilters(allow_domains=("@example.test",)), "allow_domains"),
        (SourceFilters(allow_domains=("example..test",)), "allow_domains"),
        (SourceFilters(allow_domains=("*.example.test",)), "allow_domains"),
        (SourceFilters(allow_domains=("-example.test",)), "allow_domains"),
        (
            SourceFilters(
                exclude_authors=tuple(f"bot-{n}" for n in range(MAX_FILTER_PATTERNS + 1))
            ),
            "exclude_authors",
        ),
    ],
)
def test_a_rule_that_is_not_valid_is_refused_by_name(filters: SourceFilters, setting: str) -> None:
    with pytest.raises(FilterError) as raised:
        check_filters(filters)

    assert raised.value.setting == setting
    assert "\t" not in str(raised.value)
    assert "z-a" not in str(raised.value)
    assert "@" not in str(raised.value)


# --- Verdicts ------------------------------------------------------------------------------------


def test_no_rule_keeps_everything() -> None:
    verdict = SourceFilters().apply("thread", thread(message(BOT, 1)))

    assert (verdict.keep, verdict.payload, verdict.messages_excluded) == (True, None, 0)


def test_an_excluded_author_leaves_the_thread_without_that_message() -> None:
    payload = thread(message(ANN, 1, "Ship it?"), message(BOT, 2, "Deployed."), message(BO, 3))
    rules = SourceFilters(exclude_authors=("*bot*",))

    verdict = rules.apply("thread", payload)

    assert verdict.keep
    assert verdict.messages_excluded == 1
    assert verdict.payload == {
        **payload,
        "messages": [payload["messages"][0], payload["messages"][2]],
    }
    # The address alone matches too, and a thread of nothing but the bot is not stored.
    by_address = SourceFilters(exclude_authors=("deploy-bot@*",))
    assert by_address.apply("thread", payload).messages_excluded == 1
    alone = rules.apply("thread", thread(message(BOT, 1), message(BOT, 2)))
    assert (alone.keep, alone.messages_excluded) == (False, 2)


def test_a_channel_allowlist_admits_only_its_channels() -> None:
    rules = SourceFilters(allow_channels=("eng-*",))

    assert rules.apply("thread", thread(message(ANN, 1), channel="eng-backend")).keep
    assert not rules.apply("thread", thread(message(ANN, 1), channel="random")).keep
    without = thread(message(ANN, 1))
    del without["channel"]
    assert not rules.apply("thread", without).keep
    assert not rules.apply("thread", thread(message(ANN, 1), channel="  ")).keep
    meeting = {"title": "Planning", "started_at": iso(T0)}
    assert rules.apply("meeting", meeting).keep


def test_a_domain_allowlist_keeps_messages_from_or_to_its_domains() -> None:
    rules = SourceFilters(allow_domains=("example.test",))
    outside = "Cy Placeholder <cy@elsewhere.test>"
    payload = thread(
        message(ANN, 1),  # from the domain
        message(BO, 2),  # from a subdomain
        message(outside, 3, recipients=[ANN]),  # to the domain
        message(outside, 4, recipients=["dee@elsewhere.test"]),  # neither
        message("Ann Example", 5),  # no address at all
    )

    verdict = rules.apply("thread", payload)

    assert verdict.messages_excluded == 2
    assert verdict.payload is not None
    assert [m["sent_at"] for m in verdict.payload["messages"]] == [
        m["sent_at"] for m in payload["messages"][:3]
    ]
    assert (
        not SourceFilters(allow_domains=("example.test",))
        .apply("thread", thread(message("Ann Example", 1)))
        .keep
    )
    assert (
        not SourceFilters(allow_domains=("ample.test",))
        .apply("thread", thread(message(ANN, 1)))
        .keep
    )


@pytest.mark.parametrize(
    ("author", "headers", "automated"),
    [
        ("Shop <noreply@shop.example.test>", None, True),
        ("no-reply@example.test", None, True),
        ("Billing <Do_Not_Reply@example.test>", None, True),
        ("notifications-noreply+abc@example.test", None, True),
        ("MAILER-DAEMON@example.test", None, True),
        (ANN, {"Auto-Submitted": "auto-generated"}, True),
        (ANN, {"auto-submitted": "auto-replied; owner-email=ann@example.test"}, True),
        (ANN, {"Auto-Submitted": "no"}, False),
        (ANN, {"Precedence": ["bulk"]}, True),
        (ANN, {"Precedence": "list"}, False),
        (ANN, {"X-Mailer": "Example Mail"}, False),
        (ANN, "not an object", False),
        ("Noreply Fan <ann@example.test>", None, False),
    ],
)
def test_automated_mail(author: str, headers: object, automated: bool) -> None:
    extra: dict[str, Any] = {} if headers is None else {"headers": headers}
    payload = thread(message(BO, 1), message(author, 2, **extra))

    verdict = SourceFilters(exclude_automated=True).apply("thread", payload)

    assert verdict.messages_excluded == (1 if automated else 0)


def test_an_excluded_title_drops_the_record_of_any_kind() -> None:
    rules = SourceFilters(exclude_titles=("daily stand-up*",))
    meeting = {"title": "Daily stand-up (EU)", "started_at": iso(T0)}
    document = {"title": "Daily Stand-up notes", "text": "Nothing new."}

    assert not rules.apply("meeting", meeting).keep
    assert not rules.apply("document", document).keep
    assert not rules.apply("thread", thread(message(ANN, 1), title="Daily stand-up")).keep
    assert rules.apply("meeting", {**meeting, "title": "Retro"}).keep
    assert rules.apply("thread", thread(message(ANN, 1), title=None)).keep


# --- The filter step of a batch ------------------------------------------------------------------


def record(external_id: str, payload: dict[str, Any], kind: str = "thread") -> RecordIn:
    return RecordIn(kind=kind, external_id=external_id, occurred_at=T0 + HOUR, payload=payload)


def test_a_trimmed_thread_takes_the_stored_form_ingest_gives_its_payload() -> None:
    full = thread(message(ANN, 1, f"token {GITHUB}"), message(BOT, 2), message(BO, 3))
    trimmed = {**full, "messages": [full["messages"][0], full["messages"][2]]}
    rules = SourceFilters(exclude_authors=("*bot*",))

    filtered = filter_batch(
        prepare_batch(Batch(records=[record("t-1", full)]), DEFAULT_LIMITS), rules
    )
    direct = prepare_batch(Batch(records=[record("t-1", trimmed)]), DEFAULT_LIMITS)

    assert filtered.records == direct.records
    assert filtered.filtered == {"records_filtered": 0, "messages_filtered": 1}
    assert GITHUB.encode() not in filtered.records[0].payload
    assert stored_form("thread", json.loads(direct.records[0].payload)) == (
        direct.records[0].payload,
        direct.records[0].content_hash,
        direct.records[0].normalized_text,
    )


def test_an_excluded_record_becomes_a_tombstone_of_itself() -> None:
    batch = Batch(
        records=[
            record("t-1", thread(message(BOT, 1))),
            record("t-2", thread(message(ANN, 1))),
            RecordIn(kind="thread", external_id="t-3", deleted=True),
            record("m-1", {"title": "Daily stand-up", "started_at": iso(T0)}, "meeting"),
        ]
    )
    rules = SourceFilters(exclude_authors=("*bot*",), exclude_titles=("daily*",))

    filtered = filter_batch(prepare_batch(batch, DEFAULT_LIMITS), rules)

    assert [item.external_id for item in filtered.records] == ["t-2"]
    assert filtered.tombstones == (
        PreparedTombstone(0, "thread", "t-1"),
        PreparedTombstone(2, "thread", "t-3"),
        PreparedTombstone(3, "meeting", "m-1"),
    )
    assert filtered.filtered == {"records_filtered": 2, "messages_filtered": 1}


def test_a_source_without_rules_leaves_the_batch_as_it_is() -> None:
    prepared = prepare_batch(
        Batch(records=[record("t-1", thread(message(BOT, 1)))]), DEFAULT_LIMITS
    )

    assert filter_batch(prepared, SourceFilters()) is prepared
    assert filter_batch(prepared, replace(SourceFilters(), exclude_authors=("ann*",))).filtered == {
        "records_filtered": 0,
        "messages_filtered": 0,
    }
