"""The credential store without a database: keys, encryption, settings, and what never shows a
secret. Every key and secret here is synthetic."""

import base64
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from memini.config import (
    CONFIG_FILE_VAR,
    ConfigError,
    decode_credential_key,
    environment_variables,
    load_settings,
)
from memini.credentials import (
    KEY_ID_LENGTH,
    ApiKeyCredential,
    CredentialDecryptionError,
    CredentialInfo,
    CredentialKey,
    CredentialKeyNotConfiguredError,
    CredentialKeyUnavailableError,
    InvalidCredentialError,
    Keyring,
    OAuthClient,
    OAuthTokens,
    RefreshNotSavedError,
    RotationBlockedError,
    associated_data,
    key_id,
)
from memini.logs import REDACTED, clear_secrets, redact

FAKE_URL = "postgresql://fake:fake@db.invalid:5432/memini"
KEY_A = bytes(range(32))
KEY_B = bytes(range(100, 132))
TEXT_A = base64.b64encode(KEY_A).decode()
TEXT_B = base64.urlsafe_b64encode(KEY_B).decode().rstrip("=")
CANARY = "fake-secret-c4n4ry-0000"

CREDENTIAL_ID = uuid.UUID(int=1)


def row_aad(
    *,
    credential_id: uuid.UUID = CREDENTIAL_ID,
    workspace_id: uuid.UUID = uuid.UUID(int=2),  # noqa: B008 - immutable
    source_id: uuid.UUID = uuid.UUID(int=3),  # noqa: B008 - immutable
    kind: str = "api_key",
) -> bytes:
    """The associated data of a synthetic row, with one field changed if asked."""
    return associated_data(
        credential_id=credential_id, workspace_id=workspace_id, source_id=source_id, kind=kind
    )


@pytest.fixture(autouse=True)
def _forget_secrets() -> None:
    clear_secrets()


# --- Keys ----------------------------------------------------------------------------------------


def test_a_key_is_32_bytes_of_base64_in_either_alphabet() -> None:
    assert decode_credential_key(TEXT_A) == KEY_A
    assert decode_credential_key(TEXT_B) == KEY_B  # URL-safe, without padding
    assert decode_credential_key(base64.urlsafe_b64encode(KEY_B).decode()) == KEY_B


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not base64 at all!",
        base64.b64encode(bytes(16)).decode(),
        base64.b64encode(bytes(33)).decode(),
        TEXT_A[:20] + " " + TEXT_A[20:],
        TEXT_A + "\n",
    ],
)
def test_anything_else_is_refused_without_echoing_it(text: str) -> None:
    with pytest.raises(ValueError, match=r"^not (base64|32 bytes)$") as caught:
        decode_credential_key(text)
    assert text.strip() == "" or text.strip() not in str(caught.value)


def test_a_key_id_is_16_hex_digits_that_name_the_key_without_revealing_it() -> None:
    first, second = key_id(KEY_A), key_id(KEY_B)

    assert len(first) == KEY_ID_LENGTH
    assert int(first, 16) >= 0
    assert first == key_id(KEY_A)
    assert first != second
    assert first not in KEY_A.hex()
    assert CredentialKey(KEY_A).id == first


def test_the_keys_come_from_the_environment_or_a_file(tmp_path: Path) -> None:
    key_file = tmp_path / "credential_key"
    key_file.write_text(TEXT_A + "\n")

    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_CREDENTIAL_KEY_FILE": str(key_file),
            "MEMINI_CREDENTIAL_OLD_KEYS": f"{TEXT_B}, {TEXT_A}",
        }
    )

    assert settings.credential_key is not None
    assert settings.credential_key.get_secret_value() == TEXT_A
    assert [key.get_secret_value() for key in settings.credential_old_keys] == [TEXT_B, TEXT_A]
    assert TEXT_A not in repr(settings)
    assert TEXT_B not in settings.model_dump_json()
    assert {
        "MEMINI_CREDENTIAL_KEY",
        "MEMINI_CREDENTIAL_KEY_FILE",
        "MEMINI_CREDENTIAL_OLD_KEYS",
        "MEMINI_CREDENTIAL_OLD_KEYS_FILE",
    } <= set(environment_variables())


def test_the_service_starts_without_a_key() -> None:
    settings = load_settings({"MEMINI_DATABASE_URL": FAKE_URL})

    keyring = Keyring.from_settings(settings)

    assert settings.credential_key is None
    assert not keyring.configured
    assert keyring.key_ids == frozenset()


@pytest.mark.parametrize(
    ("env", "key"),
    [
        ({"MEMINI_CREDENTIAL_KEY": CANARY}, "MEMINI_CREDENTIAL_KEY"),
        (
            {"MEMINI_CREDENTIAL_KEY": TEXT_A, "MEMINI_CREDENTIAL_OLD_KEYS": f"{TEXT_B},{CANARY}"},
            "MEMINI_CREDENTIAL_OLD_KEYS",
        ),
        ({"MEMINI_CREDENTIAL_OLD_KEYS": TEXT_B}, "MEMINI_CREDENTIAL_OLD_KEYS"),
    ],
)
def test_bad_key_settings_are_named_and_never_echoed(env: dict[str, str], key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({"MEMINI_DATABASE_URL": FAKE_URL, **env})

    assert [problem.split(":")[0] for problem in caught.value.problems] == [key]
    text = "".join(traceback.format_exception(caught.value))
    for value in (CANARY, TEXT_A, TEXT_B):
        assert value not in text


def test_old_keys_need_a_key_and_the_message_says_so() -> None:
    with pytest.raises(ConfigError, match="only together with credential_key"):
        load_settings({"MEMINI_DATABASE_URL": FAKE_URL, "MEMINI_CREDENTIAL_OLD_KEYS": TEXT_B})


def test_a_key_is_never_read_from_the_config_file(tmp_path: Path) -> None:
    config = tmp_path / "memini.toml"
    config.write_text(f'credential_key = "{TEXT_A}"\ncredential_old_keys = ["{TEXT_B}"]\n')

    with pytest.raises(ConfigError) as caught:
        load_settings({"MEMINI_DATABASE_URL": FAKE_URL, CONFIG_FILE_VAR: str(config)})

    assert [problem.split(" ")[0] for problem in caught.value.problems] == [
        "credential_key",
        "credential_old_keys",
    ]
    assert TEXT_A not in str(caught.value)


def test_the_keys_are_masked_in_logs_once_loaded() -> None:
    settings = load_settings(
        {
            "MEMINI_DATABASE_URL": FAKE_URL,
            "MEMINI_CREDENTIAL_KEY": TEXT_A,
            "MEMINI_CREDENTIAL_OLD_KEYS": TEXT_B,
        }
    )

    keyring = Keyring.from_settings(settings)

    assert keyring.current.id == key_id(KEY_A)
    assert keyring.key_ids == {key_id(KEY_A), key_id(KEY_B)}
    assert redact(f"key={TEXT_A} old {TEXT_B}") == f"key={REDACTED} old {REDACTED}"


# --- Encryption ----------------------------------------------------------------------------------


def test_a_sealed_secret_opens_with_its_key_and_row() -> None:
    keyring = Keyring(CredentialKey(KEY_A))
    aad = row_aad()

    first = keyring.seal(CANARY.encode(), aad)
    second = keyring.seal(CANARY.encode(), aad)

    assert first.key_id == key_id(KEY_A)
    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert CANARY.encode() not in first.ciphertext
    assert keyring.open(first, aad, credential_id=CREDENTIAL_ID) == CANARY.encode()


OTHER = uuid.UUID(int=99)


@pytest.mark.parametrize(
    "other",
    [
        row_aad(credential_id=OTHER),
        row_aad(workspace_id=OTHER),
        row_aad(source_id=OTHER),
        row_aad(kind="oauth"),
    ],
)
def test_a_ciphertext_does_not_open_for_another_row(other: bytes) -> None:
    keyring = Keyring(CredentialKey(KEY_A))
    sealed = keyring.seal(CANARY.encode(), row_aad())

    with pytest.raises(CredentialDecryptionError) as caught:
        keyring.open(sealed, other, credential_id=CREDENTIAL_ID)

    assert str(CREDENTIAL_ID) in str(caught.value)
    assert CANARY not in str(caught.value)


def test_an_altered_ciphertext_does_not_open() -> None:
    keyring = Keyring(CredentialKey(KEY_A))
    aad = row_aad()
    sealed = keyring.seal(CANARY.encode(), aad)
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]

    with pytest.raises(CredentialDecryptionError):
        keyring.open(
            type(sealed)(sealed.key_id, sealed.nonce, flipped), aad, credential_id=uuid.UUID(int=1)
        )


def test_another_key_does_not_open_it() -> None:
    aad = row_aad()
    sealed = Keyring(CredentialKey(KEY_A)).seal(CANARY.encode(), aad)
    other = Keyring(CredentialKey(KEY_B))

    with pytest.raises(CredentialKeyUnavailableError) as caught:
        other.open(sealed, aad, credential_id=CREDENTIAL_ID)
    assert sealed.key_id in str(caught.value)
    assert "MEMINI_CREDENTIAL_OLD_KEYS" in str(caught.value)

    # The wrong key under the right id: authentication fails.
    forged = type(sealed)(key_id(KEY_B), sealed.nonce, sealed.ciphertext)
    with pytest.raises(CredentialDecryptionError):
        other.open(forged, aad, credential_id=CREDENTIAL_ID)


def test_old_keys_open_and_the_current_key_seals() -> None:
    aad = row_aad()
    old = Keyring(CredentialKey(KEY_A)).seal(CANARY.encode(), aad)
    keyring = Keyring(CredentialKey(KEY_B), [CredentialKey(KEY_A)])

    assert keyring.open(old, aad, credential_id=CREDENTIAL_ID) == CANARY.encode()
    assert keyring.seal(b"x", aad).key_id == key_id(KEY_B)


def test_without_a_key_nothing_is_sealed_or_opened_and_the_setting_is_named() -> None:
    keyring = Keyring(None)
    aad = row_aad()
    sealed = Keyring(CredentialKey(KEY_A)).seal(b"x", aad)

    with pytest.raises(CredentialKeyNotConfiguredError, match="MEMINI_CREDENTIAL_KEY"):
        keyring.seal(b"x", aad)
    with pytest.raises(CredentialKeyNotConfiguredError, match="MEMINI_CREDENTIAL_KEY_FILE"):
        keyring.open(sealed, aad, credential_id=uuid.UUID(int=1))


def test_associated_data_is_unambiguous() -> None:
    assert row_aad() != row_aad(kind="oauth")
    assert row_aad() == row_aad()
    # Length prefixes: moving a byte from one field to the next changes the encoding.
    assert row_aad(kind="ab") != row_aad(kind="a")


# --- What never shows a secret -------------------------------------------------------------------


def _info() -> CredentialInfo:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    return CredentialInfo(
        id=uuid.UUID(int=1),
        workspace_id=uuid.UUID(int=2),
        workspace="acme",
        owner_id=uuid.UUID(int=4),
        source_id=uuid.UUID(int=3),
        source="chat",
        kind="api_key",
        key_id=key_id(KEY_A),
        generation=1,
        expires_at=None,
        token_endpoint=None,
        client_id=None,
        scope=None,
        refreshable=False,
        created_at=now,
        updated_at=now,
        refreshed_at=None,
    )


def test_reprs_hide_keys_and_secrets() -> None:
    tokens = OAuthTokens(SecretStr(CANARY + "-a"), SecretStr(CANARY + "-r"))
    client = OAuthClient("https://auth.example.test/token", "client-1", SecretStr(CANARY + "-c"))
    keyring = Keyring(CredentialKey(KEY_A), [CredentialKey(KEY_B)])

    shown = " ".join(
        repr(value)
        for value in (
            tokens,
            client,
            keyring,
            CredentialKey(KEY_A),
            ApiKeyCredential(_info(), SecretStr(CANARY)),
        )
    )

    assert CANARY not in shown
    assert TEXT_A not in shown
    assert KEY_A.hex() not in shown
    assert key_id(KEY_A) in shown


def test_error_texts_hold_no_secret() -> None:
    tokens = OAuthTokens(SecretStr(CANARY + "-a"), SecretStr(CANARY + "-r"))
    errors: list[Exception] = [
        RefreshNotSavedError(uuid.UUID(int=3), 4, tokens),
        RotationBlockedError({key_id(KEY_A): 2}),
        CredentialKeyNotConfiguredError(),
    ]

    for error in errors:
        assert CANARY not in "".join(traceback.format_exception(error))
        assert CANARY not in repr(error)


@pytest.mark.parametrize(
    ("make", "field"),
    [
        (lambda: OAuthTokens(SecretStr("")), "access_token"),
        (lambda: OAuthTokens(SecretStr(f"{CANARY} x")), "access_token"),
        (lambda: OAuthTokens(SecretStr("a"), SecretStr(f"{CANARY}\n")), "refresh_token"),
        (lambda: OAuthTokens(SecretStr("a"), None, datetime(2026, 1, 2)), "expires_at"),
        (lambda: OAuthClient(f"https://user:{CANARY}@auth.example.test/token"), "token_endpoint"),
        (lambda: OAuthClient("ftp://auth.example.test/token"), "token_endpoint"),
        (lambda: OAuthClient(client_secret=SecretStr(f"{CANARY}\t")), "client_secret"),
        (lambda: OAuthClient(client_id=""), "client_id"),
    ],
)
def test_invalid_fields_are_named_and_never_echoed(make: Callable[[], object], field: str) -> None:
    with pytest.raises(InvalidCredentialError) as caught:
        make()

    assert caught.value.field == field
    assert str(caught.value).startswith(f"{field}: ")
    assert CANARY not in str(caught.value)
