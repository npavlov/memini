"""Encryption of credentials: AES-256-GCM from ``cryptography``, keys from the settings.

A :class:`Keyring` holds the current key (``credential_key``), which encrypts, and the old keys
(``credential_old_keys``), which only decrypt, so that rows written under an old key stay readable
while a rotation re-encrypts them. Each key is known by its key id, 16 hex digits of
HMAC-SHA256(key, ``memini.credential.key-id.v1``): stored with every ciphertext, it tells which key
to decrypt with and which rows a rotation still has to re-encrypt, without revealing the key.

Why AES-256-GCM and not Fernet: GCM authenticates associated data, and the store binds every
ciphertext to its row with it (:func:`associated_data`), so a ciphertext copied into another row
fails to decrypt. Fernet (AES-128-CBC with HMAC) takes no associated data; the binding would have
to be built on top of it by hand. A nonce is 96 random bits per encryption: with far fewer than
2^32 encryptions under one key, a repeated nonce is not a practical risk.
"""

import hmac
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, override

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from memini.config import Settings, decode_credential_key
from memini.credentials.errors import (
    CredentialDecryptionError,
    CredentialKeyNotConfiguredError,
    CredentialKeyUnavailableError,
)
from memini.logs import register_secret

__all__ = [
    "KEY_ID_LENGTH",
    "NONCE_LENGTH",
    "CredentialKey",
    "Keyring",
    "Sealed",
    "associated_data",
    "key_id",
]

NONCE_LENGTH: Final = 12
"""Bytes of an AES-GCM nonce."""

KEY_ID_LENGTH: Final = 16
"""Hex digits of a key id."""

_KEY_ID_LABEL: Final = b"memini.credential.key-id.v1"
_AAD_DOMAIN: Final = b"memini.credential.v1"


def key_id(key: bytes) -> str:
    """The id of ``key``: 16 hex digits of an HMAC under the key. Not secret."""
    return hmac.new(key, _KEY_ID_LABEL, "sha256").hexdigest()[:KEY_ID_LENGTH]


def associated_data(
    *, credential_id: uuid.UUID, workspace_id: uuid.UUID, source_id: uuid.UUID, kind: str
) -> bytes:
    """What a ciphertext is bound to: its row's id, workspace, source and kind.

    Each field is preceded by its length as 8 bytes, big-endian, as in :mod:`memini.ids`, so no
    choice of values can make two rows encode alike.
    """
    fields = (
        _AAD_DOMAIN,
        credential_id.bytes,
        workspace_id.bytes,
        source_id.bytes,
        kind.encode("utf-8"),
    )
    return b"".join(len(field).to_bytes(8, "big") + field for field in fields)


@dataclass(frozen=True, slots=True)
class Sealed:
    """A ciphertext as it is stored: the key id, the nonce, and the ciphertext with its tag."""

    key_id: str
    nonce: bytes
    ciphertext: bytes


class CredentialKey:
    """One key. Its ``repr`` shows the key id only."""

    __slots__ = ("_aead", "id")

    def __init__(self, key: bytes) -> None:
        self.id: str = key_id(key)
        self._aead = AESGCM(key)

    @classmethod
    def from_text(cls, text: str) -> "CredentialKey":
        """A key from its base64 text, as the settings hold it.

        Raises:
            ValueError: not base64 of 32 bytes; the message never holds the text.
        """
        return cls(decode_credential_key(text))

    def seal(self, plaintext: bytes, aad: bytes) -> Sealed:
        nonce = os.urandom(NONCE_LENGTH)
        return Sealed(self.id, nonce, self._aead.encrypt(nonce, plaintext, aad))

    def open(self, sealed: Sealed, aad: bytes) -> bytes:
        """The plaintext of ``sealed``.

        Raises:
            cryptography.exceptions.InvalidTag: the ciphertext, nonce or associated data differ
                from what was sealed, or the key is another one.
        """
        return self._aead.decrypt(sealed.nonce, sealed.ciphertext, aad)

    @override
    def __repr__(self) -> str:
        return f"CredentialKey(id={self.id!r})"


class Keyring:
    """The current key, which encrypts, and old keys, which only decrypt.

    Without a current key the keyring still exists, so the service can start; :meth:`seal`, and
    :meth:`open` of a row no key here can read, raise an error that names the setting.
    """

    __slots__ = ("_current", "_keys")

    def __init__(self, current: CredentialKey | None, old: Iterable[CredentialKey] = ()) -> None:
        self._current = current
        keys = {key.id: key for key in old}
        if current is not None:
            keys[current.id] = current
        self._keys: dict[str, CredentialKey] = keys

    @classmethod
    def from_settings(cls, settings: Settings) -> "Keyring":
        """The keyring of ``credential_key`` and ``credential_old_keys``.

        The key texts are registered with :func:`memini.logs.register_secret`, so a log line that
        held one by mistake shows it masked.
        """
        old = [secret.get_secret_value() for secret in settings.credential_old_keys]
        current = settings.credential_key
        for text in old if current is None else [*old, current.get_secret_value()]:
            register_secret(text)
        return cls(
            None if current is None else CredentialKey.from_text(current.get_secret_value()),
            [CredentialKey.from_text(text) for text in old],
        )

    @property
    def configured(self) -> bool:
        """Whether there is a current key, so credentials can be stored."""
        return self._current is not None

    @property
    def current(self) -> CredentialKey:
        """The key new ciphertexts are made with.

        Raises:
            CredentialKeyNotConfiguredError: ``credential_key`` is not set.
        """
        if self._current is None:
            raise CredentialKeyNotConfiguredError
        return self._current

    @property
    def key_ids(self) -> frozenset[str]:
        """The ids of every key that can decrypt: the current one and the old ones."""
        return frozenset(self._keys)

    def seal(self, plaintext: bytes, aad: bytes) -> Sealed:
        """Encrypt under the current key.

        Raises:
            CredentialKeyNotConfiguredError: ``credential_key`` is not set.
        """
        return self.current.seal(plaintext, aad)

    def open(self, sealed: Sealed, aad: bytes, *, credential_id: uuid.UUID) -> bytes:
        """Decrypt with the key ``sealed`` names. ``credential_id`` is for error messages.

        Raises:
            CredentialKeyNotConfiguredError: no key is configured at all.
            CredentialKeyUnavailableError: the row's key is neither the current key nor an old one.
            CredentialDecryptionError: the ciphertext does not belong to this row, or was altered.
        """
        key = self._keys.get(sealed.key_id)
        if key is None:
            if not self._keys:
                raise CredentialKeyNotConfiguredError
            raise CredentialKeyUnavailableError(credential_id, sealed.key_id)
        try:
            return key.open(sealed, aad)
        except InvalidTag:
            raise CredentialDecryptionError(credential_id) from None

    @override
    def __repr__(self) -> str:
        current = None if self._current is None else self._current.id
        old = sorted(self._keys.keys() - {current})
        return f"Keyring(current={current!r}, old={old!r})"
