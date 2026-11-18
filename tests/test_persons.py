"""Persons without a database: normalization of aliases, identities in payloads, the cursor of a
listing. Every name, address and id here is made up."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memini.payloads import Contact, PayloadError, Thread, parse_payload
from memini.persons import (
    MAX_VALUE_LENGTH,
    Identity,
    InvalidAliasError,
    MergeError,
    Occurrence,
    PersonCursor,
    clean_display_name,
    extract_occurrences,
    identity_of,
    normalize_alias,
    normalize_chat_user_id,
    normalize_email,
    normalize_name,
)

T0 = datetime(2026, 3, 1, 9, tzinfo=UTC)


# --- Normalization -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("ann@example.test", "ann@example.test"),
        ("  Ann.Example@Example.TEST ", "ann.example@example.test"),
        ("mailto:ann@example.test", "ann@example.test"),
        ("MAILTO:Ann@example.test", "ann@example.test"),
        ("ann+notes@example.test", "ann+notes@example.test"),
        ("ann@localhost", "ann@localhost"),
        ("ann@example.test\n", "ann@example.test"),
    ],
)
def test_a_mail_address_is_trimmed_and_lowercased(value: str, key: str) -> None:
    assert normalize_email(value) == key


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Ann Example",
        "ann@",
        "@example.test",
        "ann@bo@example.test",
        "ann @example.test",
        "Ann <ann@example.test>",
        "ann\x00@example.test",
        "a" * MAX_VALUE_LENGTH + "@example.test",
        None,
        42,
    ],
)
def test_what_is_not_a_mail_address_is_no_key(value: object) -> None:
    assert normalize_email(value) is None


def test_a_chat_user_id_is_kept_as_given_but_trimmed() -> None:
    assert normalize_chat_user_id(" U012AB ") == "U012AB"
    assert normalize_chat_user_id("teamchat:U012ab") == "teamchat:U012ab"
    assert normalize_chat_user_id(1234567) == "1234567"
    assert normalize_chat_user_id(True) is None
    assert normalize_chat_user_id("") is None
    assert normalize_chat_user_id("U1\tU2") is None


def test_a_name_is_case_folded_with_its_spaces_collapsed() -> None:
    assert normalize_name("  Ann   EXAMPLE ") == "ann example"
    assert normalize_name("Ann\N{NO-BREAK SPACE}Example") == "ann example"
    assert normalize_name("\N{FULLWIDTH LATIN CAPITAL LETTER A}nn Example") == "ann example"
    assert normalize_name("Grosse Strasse") == normalize_name("GROSSE STRAßE")
    assert normalize_name("A. Example") != normalize_name("Ann Example")
    assert normalize_name(" ") is None
    assert normalize_name("Ann\x07") is None


def test_a_display_name_keeps_its_case() -> None:
    assert clean_display_name("  Ann   Example ") == "Ann Example"
    assert clean_display_name("") is None
    assert clean_display_name(["Ann"]) is None


def test_an_alias_given_by_a_user_is_checked_by_its_kind() -> None:
    assert normalize_alias("email", " Ann@Example.test ") == (
        "Ann@Example.test",
        "ann@example.test",
    )
    assert normalize_alias("chat_user_id", "U012AB") == ("U012AB", "U012AB")
    assert normalize_alias("display_name", " Ann  Example ") == ("Ann Example", "ann example")
    assert normalize_alias("git_author", "Ann Example") == ("Ann Example", "ann example")

    with pytest.raises(InvalidAliasError) as refused:
        normalize_alias("email", "secret-looking value")
    assert refused.value.field == "value"
    assert "secret-looking" not in str(refused.value)
    with pytest.raises(InvalidAliasError):
        normalize_alias("display_name", "x" * (MAX_VALUE_LENGTH + 1))


# --- Identities ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "identity"),
    [
        ("ann@example.test", Identity(email="ann@example.test")),
        (
            "Ann Example <Ann@Example.test>",
            Identity(name="Ann Example", email="ann@example.test"),
        ),
        (
            '"Example, Ann" <ann@example.test>',
            Identity(name="Example, Ann", email="ann@example.test"),
        ),
        ("<ann@example.test>", Identity(email="ann@example.test")),
        ("Ann  Example", Identity(name="Ann Example")),
        ("Ann <not an address>", Identity(name="Ann <not an address>")),
        ("", None),
        ("Ann\x07", None),
        ("x" * (MAX_VALUE_LENGTH + 1), None),
    ],
)
def test_a_name_on_its_own_is_an_address_or_a_display_name(
    value: str, identity: Identity | None
) -> None:
    assert identity_of(value) == identity


def test_an_identity_names_its_keys_in_resolution_order() -> None:
    both = Identity(name="Ann", email="ann@example.test", chat_user_id="U-ANN")
    assert both.deterministic_keys() == (("email", "ann@example.test"), ("chat_user_id", "U-ANN"))
    assert both.label == "Ann"
    assert Identity(name="Ann  ").name_key() == "ann"
    assert Identity(chat_user_id="U-ANN").label == "U-ANN"
    assert Identity(name="Ann").deterministic_keys() == ()


def iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def occurrences(kind: str, payload: dict[str, Any]) -> list[Occurrence]:
    return extract_occurrences(parse_payload(kind, payload))


def test_a_thread_names_authors_recipients_and_mentions_per_message() -> None:
    payload: dict[str, Any] = {
        "people": [
            {"name": "Ann Example", "email": "Ann@Example.test", "chat_user_id": "U-ANN"},
            {"name": " Bo Sample ", "chat_user_id": "U-BO"},
            {"name": "Cy Placeholder", "email": "not an address"},
        ],
        "messages": [
            {
                "id": "m1",
                "author": "Ann Example",
                "sent_at": iso(T0),
                "text": "Shall we move the release?",
                "recipients": ["Di Stand-in <di@example.test>", "Cy Placeholder"],
                "cc": ["ed@example.test"],
                "mentions": ["Bo Sample", " "],
            },
            {
                "author": "bo@example.test",
                "sent_at": iso(T0 + timedelta(minutes=5)),
                "text": "Yes.",
            },
        ],
    }

    found = occurrences("thread", payload)

    second = parse_payload("thread", payload)
    assert isinstance(second, Thread)
    derived = second.messages[1].id
    assert found == [
        Occurrence(
            Identity(name="Ann Example", email="ann@example.test", chat_user_id="U-ANN"),
            "author",
            "m1",
            T0,
        ),
        Occurrence(Identity(name="Di Stand-in", email="di@example.test"), "recipient", "m1", T0),
        Occurrence(Identity(name="Cy Placeholder"), "recipient", "m1", T0),
        Occurrence(Identity(email="ed@example.test"), "recipient", "m1", T0),
        Occurrence(Identity(name="Bo Sample", chat_user_id="U-BO"), "mention", "m1", T0),
        Occurrence(Identity(email="bo@example.test"), "author", derived, T0 + timedelta(minutes=5)),
    ]


def test_a_meeting_names_participants_speakers_and_owners_at_its_start() -> None:
    payload = {
        "title": "Release planning",
        "started_at": iso(T0),
        "participants": ["Ann Example", "bo@example.test"],
        "transcript": [{"speaker": "Ann Example", "text": "Let us start."}, {"text": "(silence)"}],
        "action_items": ["Book a room", {"text": "Write the notes", "owner": "Cy Placeholder"}],
        "people": [{"name": "Ann Example", "chat_user_id": "U-ANN"}],
    }

    ann = Identity(name="Ann Example", chat_user_id="U-ANN")
    assert occurrences("meeting", payload) == [
        Occurrence(ann, "participant", None, T0),
        Occurrence(Identity(email="bo@example.test"), "participant", None, T0),
        Occurrence(ann, "participant", None, T0),
        Occurrence(Identity(name="Cy Placeholder"), "mention", None, T0),
    ]


def test_a_document_names_no_one() -> None:
    assert occurrences("document", {"title": "Greenhouse handbook", "text": "Water at six."}) == []


def test_people_is_a_list_of_names_each_once() -> None:
    message = {"author": "Ann Example", "sent_at": iso(T0), "text": "Hi."}
    parsed = parse_payload(
        "thread",
        {"messages": [message], "people": [{"name": "Ann Example", "email": " "}]},
    )
    assert isinstance(parsed, Thread)
    assert parsed.people == (Contact(name="Ann Example"),)
    for people, field in [
        ("Ann", "payload.people"),
        (["Ann"], "payload.people[0]"),
        ([{"email": "ann@example.test"}], "payload.people[0].name"),
        ([{"name": "Ann", "chat_user_id": 7}], "payload.people[0].chat_user_id"),
        ([{"name": "Ann"}, {"name": " Ann "}], "payload.people[1].name"),
    ]:
        with pytest.raises(PayloadError) as refused:
            parse_payload("thread", {"messages": [message], "people": people})
        assert refused.value.field == field


# --- Values --------------------------------------------------------------------------------------


def test_a_person_cursor_survives_encoding() -> None:
    cursor = PersonCursor(display_name="Example | Ann", id=uuid.uuid4())
    assert PersonCursor.decode(cursor.encode()) == cursor


@pytest.mark.parametrize("value", ["", "not base64!", "bm90IGEgY3Vyc29y", "fA"])
def test_a_person_cursor_refuses_what_it_did_not_make(value: str) -> None:
    with pytest.raises(ValueError, match="not a cursor of a person listing"):
        PersonCursor.decode(value)


def test_a_merge_refusal_says_why() -> None:
    assert MergeError("already_merged").refusal == "already_merged"
    assert "split it first" in str(MergeError("already_merged"))
    assert str(MergeError("not_merged")) == "the person is not merged into another"
