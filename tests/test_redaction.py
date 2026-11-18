"""Redaction of secrets and card numbers (memini.redaction), and where ingest applies it.

Every secret is fake (tests/redaction_helpers.py)."""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest

from memini.ingest import Batch, InvalidBatchError, RecordIn, WindowClaim, prepare_batch
from memini.ingest.model import DEFAULT_LIMITS
from memini.redaction import (
    REDACTION_KINDS,
    RedactionCollisionError,
    RedactionKind,
    add_counts,
    is_card_number,
    is_secret_key,
    luhn_valid,
    marker,
    redact_json,
    redact_text,
)
from redaction_helpers import (
    AMEX,
    ANTHROPIC,
    AWS_KEY_ID,
    AWS_SECRET,
    DISCOVER,
    GITHUB,
    GITHUB_PAT,
    GITLAB,
    GOOGLE,
    JCB,
    JWT,
    MASTERCARD,
    MASTERCARD_2,
    MEMINI,
    NPM,
    OPENAI,
    OPENSSH,
    PEM,
    PEM_BODY,
    PEM_ESCAPED,
    SLACK,
    SLACK_HOOK,
    STRIPE,
    STRIPE_TEST,
    VISA,
    WEBHOOK_SECRET,
)

M = marker


def red(text: str) -> str:
    return redact_text(text).value


def counts(text: str) -> dict[RedactionKind, int]:
    return dict(redact_text(text).counts)


# --- Detectors: what they find -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        (PEM, M("private_key") + "\n\n\n", "private_key"),
        (OPENSSH, M("private_key") + "\n\n\n", "private_key"),
        # As it stands in code or JSON, with escaped line breaks.
        (f'KEY = "{PEM_ESCAPED}"', f'KEY = "{M("private_key")}"', "private_key"),
        (
            "postgresql://app:Fake-Pass-1@db.example.invalid:5432/app",
            "postgresql://REDACTED@db.example.invalid:5432/app",
            "url_credentials",
        ),
        (f"post to {SLACK_HOOK} now", f"post to {M('slack_webhook')} now", "slack_webhook"),
        (f"token {MEMINI}", f"token {M('memini_token')}", "memini_token"),
        (f"git push with {GITHUB}", f"git push with {M('github_token')}", "github_token"),
        (GITHUB_PAT, M("github_token"), "github_token"),
        (GITLAB, M("gitlab_token"), "gitlab_token"),
        (f"bot {SLACK}.", f"bot {M('slack_token')}.", "slack_token"),
        (STRIPE, M("stripe_key"), "stripe_key"),
        (STRIPE_TEST, M("stripe_key"), "stripe_key"),
        (WEBHOOK_SECRET, M("stripe_key"), "stripe_key"),
        (f"id {AWS_KEY_ID}", f"id {M('aws_access_key')}", "aws_access_key"),
        (GOOGLE, M("gcp_api_key"), "gcp_api_key"),
        (OPENAI, M("api_key"), "api_key"),
        (ANTHROPIC, M("api_key"), "api_key"),
        (NPM, M("api_key"), "api_key"),
        (f"cookie {JWT};", f"cookie {M('jwt')};", "jwt"),
        (
            f"aws_secret_access_key = {AWS_SECRET}",
            f"aws_secret_access_key = {M('aws_secret_key')}",
            "aws_secret_key",
        ),
        (
            "Authorization: Basic ZmFrZTpmYWtlLXBhc3M=",
            f"Authorization: Basic {M('authorization')}",
            "authorization",
        ),
        (
            "curl -H 'Authorization: token Fake1Fake2Fake3'",
            f"curl -H 'Authorization: token {M('authorization')}'",
            "authorization",
        ),
        (
            "use bearer FakeOpaque1Token2Value3",
            f"use bearer {M('authorization')}",
            "authorization",
        ),
        ('password = "hunter22"', f'password = "{M("secret")}"', "secret"),
        ("DB_PASSWORD=hunter22", f"DB_PASSWORD={M('secret')}", "secret"),
        ('{"client_secret": "fake-secret"}', f'{{"client_secret": "{M("secret")}"}}', "secret"),
        ("api-key: Fake-Value-1", f"api-key: {M('secret')}", "secret"),
        ("Password := 'fake99'", f"Password := '{M('secret')}'", "secret"),
        ("secret => fake-value!", f"secret => {M('secret')}", "secret"),
        (
            "https://example.invalid/cb?access_token=fake123&state=1",
            f"https://example.invalid/cb?access_token={M('secret')}&state=1",
            "secret",
        ),
        (f"card {VISA}, thanks", f"card {M('card')}, thanks", "card"),
        (f"({MASTERCARD})", f"({M('card')})", "card"),
        (f"amex {AMEX}.", f"amex {M('card')}.", "card"),
        (f"card:{DISCOVER}", f"card:{M('card')}", "card"),
        (JCB, M("card"), "card"),
        (MASTERCARD_2, M("card"), "card"),
    ],
)
def test_each_detector_replaces_what_it_finds_with_its_marker(
    text: str, expected: str, kind: RedactionKind
) -> None:
    result = redact_text(text)

    assert result.value == expected
    assert result.counts == {kind: 1}
    assert result.total == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://reader:Fake-Pass-1@chat.example.invalid/a?b=1",
            "https://REDACTED@chat.example.invalid/a?b=1",
        ),
        (
            f"https://{GITHUB}@git.example.invalid/r.git",
            "https://REDACTED@git.example.invalid/r.git",
        ),
        (
            f"https://x-access-token:{GITHUB}@git.example.invalid/r",
            "https://REDACTED@git.example.invalid/r",
        ),
        ("ssh://git@git.example.invalid/r.git", "ssh://git@git.example.invalid/r.git"),
        ("https://REDACTED@chat.example.invalid/", "https://REDACTED@chat.example.invalid/"),
    ],
)
def test_the_credentials_of_a_url_go_and_the_url_still_parses(url: str, expected: str) -> None:
    redacted = red(url)

    assert redacted == expected
    parts = urlsplit(redacted)  # a URL parser accepts it: no brackets in the user information
    assert parts.hostname is not None
    assert parts.password is None


def test_a_pair_of_aws_keys_is_redacted_together() -> None:
    text = f"export AWS_ACCESS_KEY_ID={AWS_KEY_ID}\nthe other one: {AWS_SECRET}\n"

    assert red(text) == (
        f"export AWS_ACCESS_KEY_ID={M('aws_access_key')}\nthe other one: {M('aws_secret_key')}\n"
    )
    # Without its access key id, a 40-character run is not taken for a secret key.
    assert red(f"the other one: {AWS_SECRET}") == f"the other one: {AWS_SECRET}"


def test_a_private_key_cut_short_is_still_redacted() -> None:
    cut = PEM.split("-----END")[0]

    assert red(f"{cut}and then the text goes on") == (
        M("private_key") + "\n\n\nand then the text goes on"
    )


def test_redaction_keeps_the_lines_of_the_text() -> None:
    code = f"import os\n\nKEY = '''\n{PEM}\n'''\n\n\ndef main():\n    return 1\n"

    redacted = red(code)

    assert redacted.count("\n") == code.count("\n")
    line = code.splitlines().index("def main():")
    assert redacted.splitlines()[line] == "def main():"
    assert PEM_BODY[0] not in redacted


def test_a_token_in_an_authorization_header_is_counted_once() -> None:
    assert counts(f"Authorization: Bearer {JWT}") == {"jwt": 1}
    assert counts(f"Authorization: Bearer {GITHUB}") == {"github_token": 1}
    assert counts(f'password = "{GITHUB}"') == {"github_token": 1}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{VISA} 12/28 737", f"{M('card')} 12/28 737"),  # with its expiry and CVV
        (f"{VISA} 737", f"{M('card')} 737"),  # a CVV is a group of the run, not of the card
        (f"{VISA} {AMEX}", f"{M('card')} {M('card')}"),
        (f"ref 1234 {VISA}", f"ref 1234 {M('card')}"),
        (f"{MASTERCARD}-1234", f"{M('card')}-1234"),
        ("4111111111111111 12/28", f"{M('card')} 12/28"),
    ],
)
def test_a_card_is_found_in_a_longer_run_of_digit_groups(text: str, expected: str) -> None:
    assert red(text) == expected


def test_several_kinds_in_one_text_are_counted_by_kind() -> None:
    text = f"{VISA} {AMEX} {GITHUB} {STRIPE} password=hunter22"

    assert counts(text) == {"card": 2, "github_token": 1, "stripe_key": 1, "secret": 1}


ALL_SECRETS = (
    PEM,
    OPENSSH,
    AWS_KEY_ID,
    GITHUB,
    GITHUB_PAT,
    GITLAB,
    SLACK,
    SLACK_HOOK,
    STRIPE,
    WEBHOOK_SECRET,
    GOOGLE,
    OPENAI,
    ANTHROPIC,
    NPM,
    JWT,
    MEMINI,
    VISA,
    MASTERCARD,
    AMEX,
    DISCOVER,
    f"aws_secret_access_key={AWS_SECRET}",
    "Authorization: Bearer FakeOpaque1Token2Value3",
    'password = "hunter22"',
    "postgresql://app:Fake-Pass-1@db.example.invalid/app",
)


@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_redacting_again_changes_nothing(secret: str) -> None:
    once = redact_text(f"before {secret} after")

    assert once.total >= 1
    twice = redact_text(once.value)
    assert twice.value == once.value
    assert twice.total == 0


def test_skipped_kinds_are_not_looked_for() -> None:
    assert redact_text(VISA, skip=frozenset({"card"})).value == VISA
    assert redact_text(GITHUB, skip=frozenset({"card"})).counts == {"github_token": 1}


def test_markers_name_every_kind() -> None:
    assert marker("card") == "[REDACTED:card]"
    assert len(set(REDACTION_KINDS)) == len(REDACTION_KINDS)


# --- Detectors: what they leave alone ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Long numbers that are not cards.
        "1772323200000",  # a timestamp in milliseconds
        "slack ts 1772323200.000100",
        "snowflake 1255338432189399040",  # starts with 1: no card does
        "isbn 9780306406157 and 978-0-306-40615-7",
        "not Luhn: 4111 1111 1111 1112",
        "id ord_4111111111111111 and https://shop.example.invalid/orders/4111111111111111/x",
        "a decimal 4111111111111111.5 and a version v4111111111111111",
        "41111111111111111111 has twenty digits",
        "uuid 123e4567-e89b-12d3-a456-426614174000",
        "a hash 4111111111111111111111111111111111111111",
        # Phone numbers and dates.
        "call +1 415 555 0100 or +44 20 7946 0958 or 8 800 555 35 35",
        "or +7 (916) 123-45-67, or +4411111111111111",
        "on 2026-03-01 09:30:00, 01.03.2026, 20260301, 2026 03 01",
        "a total of 4 111 111 111 111 111 grains",
        # Words, names and code that look like secrets.
        "password=password and api_key = settings.api_key",
        "password: str = Field(min_length=12)",
        "secret: SecretStr",
        'label = "password"; password: "Enter your password"',
        "password = ${DB_PASSWORD} and secret: {{ vault.secret }} and api_key: <your key>",
        "password: '****' and password=xxxxxxxx",
        "password_hint: hunter22 and max_api_key_retries: 3",
        "token: page-2 and next_token=abc123",
        'headers["Authorization"] = f"Bearer {token}"',
        '"Authorization": "Bearer " + token',
        "authorization=AUTH_HEADER",
        "the bearer of good tidings uses authentication-mechanisms",
        "pip install sk-learn-contrib-imbalanced",
        "memini_test_database_url",
        "ssh://git@github.example.invalid/org/repo.git and git@github.example.invalid:org/repo",
        "http://example.invalid:8080/path?email=a@b.example",
        "a git commit 3f2c8e1a9b7d6c5e4f3a2b1c0d9e8f7a6b5c4d3e near no key id",
        "PRIVATE KEY is mentioned, BEGIN too, but no block",
    ],
)
def test_ordinary_text_is_left_as_it_is(text: str) -> None:
    result = redact_text(text)

    assert result.value == text
    assert result.counts == {}


def test_a_git_hash_is_not_an_aws_secret_key_even_next_to_a_key_id() -> None:
    sha = "3f2c8e1a9b7d6c5e4f3a2b1c0d9e8f7a6b5c4d3e"

    assert red(f"{AWS_KEY_ID} at {sha}") == f"{M('aws_access_key')} at {sha}"


# --- Checks --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("digits", "valid"),
    [
        ("4111111111111111", True),
        ("5555555555554444", True),
        ("378282246310005", True),
        ("79927398713", True),
        ("4111111111111112", False),
        ("0", True),
        ("18", True),
        ("19", False),
    ],
)
def test_the_luhn_check(digits: str, valid: bool) -> None:
    assert luhn_valid(digits) is valid


@pytest.mark.parametrize(
    ("text", "card"),
    [
        (VISA, True),
        (MASTERCARD, True),
        (AMEX, True),
        ("4111111111111", False),  # 13 digits, but no valid check digit
        ("4222222222222", True),  # the 13-digit Visa test number
        ("1234567812345670", False),  # Luhn-valid, but no issuer starts with 1
        ("9780306406157", False),  # an ISBN: no issuer starts with 9
        ("411111111111", False),  # 12 digits
        ("41111111111111111111", False),  # 20 digits
        ("4111 1111 1111 111a", False),
    ],
)
def test_a_card_number_needs_its_length_an_issuer_and_the_luhn_check(text: str, card: bool) -> None:
    assert is_card_number(text) is card


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("password", True),
        ("db_password", True),
        ("DB_PASSWORD", True),
        ("passwd", True),
        ("passphrase", True),
        ("secret", True),
        ("client_secret", True),
        ("clientSecret", True),
        ("api_key", True),
        ("apiKey", True),
        ("apikey", True),
        ("x-api-key", True),
        ("private_key", True),
        ("SecretAccessKey", True),
        ("access_token", True),
        ("refreshToken", True),
        ("authorization", True),
        ("Proxy-Authorization", True),
        ("token", False),
        ("next_page_token", False),
        ("secret_name", False),
        ("password_hint", False),
        ("private_key_id", False),
        ("key", False),
        ("tokens", False),
        ("author", False),
        ("", False),
    ],
)
def test_a_key_names_a_secret_by_its_last_word(name: str, secret: bool) -> None:
    assert is_secret_key(name) is secret


# --- JSON ----------------------------------------------------------------------------------------


def test_every_string_of_a_json_value_is_redacted_keys_included() -> None:
    value: dict[str, Any] = {
        "messages": [
            {"author": "Ann Example", "text": f"my card is {VISA}"},
            {"author": "Bo Sample", "text": f"and the token {GITHUB}", "reactions": 3},
        ],
        "extra": {"nested": [[f"deep {AMEX}"]], AWS_KEY_ID: "a key as a key"},
        "flags": [True, None, 1.5],
    }

    result = redact_json(value)

    assert result.value == {
        "messages": [
            {"author": "Ann Example", "text": f"my card is {M('card')}"},
            {"author": "Bo Sample", "text": f"and the token {M('github_token')}", "reactions": 3},
        ],
        "extra": {"nested": [[f"deep {M('card')}"]], M("aws_access_key"): "a key as a key"},
        "flags": [True, None, 1.5],
    }
    assert result.counts == {"card": 2, "github_token": 1, "aws_access_key": 1}
    assert value["messages"][0]["text"] == f"my card is {VISA}"  # the input is not changed


def test_a_string_under_a_key_that_names_a_secret_is_replaced_whole() -> None:
    value = {
        "password": "correct horse battery staple",
        "auth": {"apiKey": "abc", "client_secret": "fake-value"},
        "headers": {"Authorization": "Basic short"},
        "token": "page-2",
        "placeholders": {"password": "****", "secret": "${SECRET}", "api_key": ""},
        "password_length": 12,
    }

    result = redact_json(value)

    assert result.value == {
        "password": M("secret"),
        "auth": {"apiKey": M("secret"), "client_secret": M("secret")},
        "headers": {"Authorization": M("secret")},
        "token": "page-2",
        "placeholders": {"password": "****", "secret": "${SECRET}", "api_key": ""},
        "password_length": 12,
    }
    assert result.counts == {"secret": 4}


def test_a_card_number_as_a_json_integer_is_redacted() -> None:
    value = {"card": 4111111111111111, "id": 1255338432189399040, "count": -4111111111111111}

    result = redact_json(value)

    assert result.value == {
        "card": M("card"),
        "id": 1255338432189399040,
        "count": -4111111111111111,
    }
    assert result.counts == {"card": 1}


def test_an_id_is_not_looked_at_as_a_card_number_but_for_secrets_it_is() -> None:
    value = {
        "id": "4111111111111111",
        "message_id": 5555555555554444,
        "segmentIds": ["4111111111111111", f"seg-{GITHUB}"],
        "text": "4111111111111111",
    }

    result = redact_json(value)

    assert result.value == {
        "id": "4111111111111111",
        "message_id": 5555555555554444,
        "segmentIds": ["4111111111111111", f"seg-{M('github_token')}"],
        "text": M("card"),
    }


def test_the_items_of_a_list_are_under_its_key() -> None:
    result = redact_json({"password": ["hunter22", "", "****"], "notes": ["hunter22"]})

    assert result.value == {"password": [M("secret"), "", "****"], "notes": ["hunter22"]}


def test_two_keys_redacted_to_one_are_refused() -> None:
    with pytest.raises(RedactionCollisionError):
        redact_json({VISA: 1, MASTERCARD: 2})


def test_a_redacted_json_value_redacted_again_is_the_same() -> None:
    once = redact_json({"text": f"{VISA} {JWT}", "password": "hunter22", "n": 4111111111111111})

    twice = redact_json(once.value)

    assert twice.value == once.value
    assert twice.counts == {}


def test_counts_add_up_by_kind() -> None:
    assert add_counts({"card": 1}, {}, {"card": 2, "jwt": 1}) == {"card": 3, "jwt": 1}


# --- Ingest: prepare_batch -----------------------------------------------------------------------

T0 = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def thread(text: str, *, external_id: str = "general/0001", **extra: Any) -> RecordIn:
    payload = {
        "messages": [{"author": "Ann Example", "sent_at": "2026-03-01T09:30:00Z", "text": text}],
        **extra,
    }
    return RecordIn(kind="thread", external_id=external_id, occurred_at=T0, payload=payload)


def test_a_batch_is_prepared_with_its_payload_redacted_and_hashed_as_stored() -> None:
    plain = thread("Shall we move the release to Friday?")
    secret = thread(f"card {VISA}, key {GITHUB}", external_id="general/0002")
    batch = Batch(records=[plain, secret])

    prepared = prepare_batch(batch, DEFAULT_LIMITS)

    stored = prepared.records[1].payload
    assert VISA.encode() not in stored
    assert GITHUB.encode() not in stored
    assert b"[REDACTED:card]" in stored
    assert b"[REDACTED:github_token]" in stored
    assert prepared.redactions == {"card": 1, "github_token": 1}
    # The same record sent again, or sent already redacted, is the same content.
    again = prepare_batch(batch, DEFAULT_LIMITS)
    assert again.records[1].content_hash == prepared.records[1].content_hash
    assert secret.payload is not None
    redacted = redact_json(secret.payload).value
    resent = prepare_batch(
        Batch(
            records=[
                RecordIn(
                    kind="thread", external_id="general/0002", occurred_at=T0, payload=redacted
                )
            ]
        ),
        DEFAULT_LIMITS,
    )
    assert resent.records[0].content_hash == prepared.records[1].content_hash
    assert resent.redactions == {}


def test_the_url_and_the_cursor_of_a_batch_are_redacted() -> None:
    record = RecordIn(
        kind="document",
        external_id="handbook",
        occurred_at=T0,
        payload={"title": "Handbook", "text": "Water the plants."},
        external_url="https://reader:Fake-Pass-1@wiki.example.invalid/handbook",
    )
    batch = Batch(
        stream="wiki",
        window=WindowClaim(
            start=T0,
            end=T0.replace(hour=10),
            complete=True,
            cursor={"page": 2, "access_token": "fake-cursor-token"},
        ),
        records=[record],
    )

    prepared = prepare_batch(batch, DEFAULT_LIMITS)

    assert prepared.records[0].external_url == "https://REDACTED@wiki.example.invalid/handbook"
    assert prepared.cursor == {"page": 2, "access_token": M("secret")}
    assert prepared.window is not None
    assert prepared.window.cursor == prepared.cursor
    assert prepared.redactions == {"url_credentials": 1, "secret": 1}


def test_the_parser_and_the_normalized_text_read_the_redacted_payload() -> None:
    record = RecordIn(
        kind="thread",
        external_id="general/0003",
        occurred_at=T0,
        payload={
            "messages": [
                {
                    "id": "4111111111111111",
                    "author": "Ann Example",
                    "sent_at": "2026-03-01T09:30:00Z",
                    "text": f"pay with {VISA}",
                    "url": "https://reader:Fake-Pass-1@chat.example.invalid/m/1",
                },
                {
                    "id": "5555555555554444",
                    "author": "Bo Sample",
                    "sent_at": "2026-03-01T09:31:00Z",
                    "text": f"token {GITHUB}",
                },
            ]
        },
    )

    prepared = prepare_batch(Batch(records=[record]), DEFAULT_LIMITS)

    (stored,) = prepared.records
    text = stored.normalized_text
    assert VISA not in text
    assert GITHUB not in text
    assert "Fake-Pass-1" not in text
    assert M("card") in text
    assert M("github_token") in text
    assert b'"url":"https://REDACTED@chat.example.invalid/m/1"' in stored.payload
    assert b'"id":"4111111111111111"' in stored.payload  # an id, kept
    assert prepared.redactions == {"card": 1, "github_token": 1, "url_credentials": 1}


def test_the_kind_checks_read_the_redacted_payload() -> None:
    record = RecordIn(
        kind="document",
        external_id="keys",
        occurred_at=T0,
        payload={"title": GITHUB, "text": PEM},
    )

    prepared = prepare_batch(Batch(records=[record]), DEFAULT_LIMITS)

    assert prepared.records[0].payload == (
        b'{"text":"[REDACTED:private_key]\\n\\n\\n","title":"[REDACTED:github_token]"}'
    )


@pytest.mark.parametrize(
    ("batch", "field"),
    [
        (Batch(records=[thread("hi", external_id=f"thread-{GITHUB}")]), "records[0].external_id"),
        (
            Batch(records=[RecordIn(kind="thread", external_id=f"t?token={JWT}", deleted=True)]),
            "records[0].external_id",
        ),
        (Batch(stream=f"chat?api_key={STRIPE}", records=[thread("hi")]), "stream"),
    ],
)
def test_an_id_that_holds_a_secret_is_refused_without_repeating_it(
    batch: Batch, field: str
) -> None:
    with pytest.raises(InvalidBatchError) as refused:
        prepare_batch(batch, DEFAULT_LIMITS)

    assert refused.value.field == field
    assert "secret" in refused.value.reason
    for value in (GITHUB, JWT, STRIPE):
        assert value not in str(refused.value)


def test_an_id_of_digits_is_kept_even_when_it_looks_like_a_card() -> None:
    prepared = prepare_batch(Batch(records=[thread("hi", external_id=DISCOVER)]), DEFAULT_LIMITS)

    assert prepared.records[0].external_id == DISCOVER


def test_keys_of_a_payload_redacted_to_one_are_refused() -> None:
    record = thread("hi", cards={VISA: "a", MASTERCARD: "b"})

    with pytest.raises(InvalidBatchError) as refused:
        prepare_batch(Batch(records=[record]), DEFAULT_LIMITS)

    assert refused.value.field == "records[0].payload"
    assert VISA not in str(refused.value)
