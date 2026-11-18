"""Key rotation: re-encrypt every credential under the current key.

How to rotate, with the service running:

1. Make a new key (``openssl rand -base64 32``). Set it as ``MEMINI_CREDENTIAL_KEY`` and move the
   old one to ``MEMINI_CREDENTIAL_OLD_KEYS``; restart the service. It now writes with the new key
   and reads with either.
2. Run ``memini credentials rotate-key`` with the same settings. It re-encrypts, batch by batch,
   every credential that is not under the new key.
3. When it reports nothing left, remove ``MEMINI_CREDENTIAL_OLD_KEYS`` and restart.

:func:`rotate_key` is idempotent and resumable: a row is re-encrypted in the transaction of its
batch, so a run that stops keeps the batches it committed, and the next run takes the rest. Before
it changes anything it checks that the keyring can read every row; rows under a key it lacks stop
it (:class:`RotationBlockedError`). A rotation leaves the secret, its generation and its
``updated_at`` as they were. It waits for a refresh that holds a row and then finds the row under
the new key already, as the refresh wrote it with the current key of its process.
"""

import uuid
from dataclasses import dataclass
from typing import Final

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from memini.credentials.crypto import Keyring, Sealed, associated_data
from memini.credentials.errors import (
    CredentialDecryptionError,
    CredentialKeyUnavailableError,
    RotationBlockedError,
)

__all__ = ["DEFAULT_ROTATION_BATCH", "RotationResult", "rotate_key"]

type Conn = AsyncConnection[TupleRow]

DEFAULT_ROTATION_BATCH: Final = 100
"""Rows re-encrypted per transaction."""


@dataclass(frozen=True, slots=True)
class RotationResult:
    """What :func:`rotate_key` did."""

    key_id: str
    """The current key, which every row should be under."""
    rotated: int
    """Rows re-encrypted under it by this run."""
    already: int
    """Rows that were under it when the run started."""
    unreadable: tuple[uuid.UUID, ...]
    """Credentials whose ciphertext fails to decrypt: altered, or moved from another row. They
    stay as they are; set them again."""
    remaining: int
    """Rows not under the current key when the run ended: the unreadable ones, and rows written
    meanwhile by a process that still encrypts with an old key."""

    @property
    def complete(self) -> bool:
        """Whether every row is under the current key, so the old keys are no longer needed."""
        return self.remaining == 0


async def rotate_key(
    conn: Conn, keyring: Keyring, *, batch_size: int = DEFAULT_ROTATION_BATCH
) -> RotationResult:
    """Re-encrypt every credential that is not under the current key of ``keyring``.

    Raises:
        ValueError: ``conn`` is inside a transaction (each batch commits), or ``batch_size`` is
            below 1.
        CredentialKeyNotConfiguredError: ``credential_key`` is not set.
        RotationBlockedError: rows are under keys ``keyring`` lacks; nothing was changed.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError(
            "rotate_key commits batch by batch and needs a connection outside a transaction"
        )
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    key = keyring.current

    # Every statement runs in a transaction block: on a connection without autocommit, a bare
    # statement would open a transaction that turns the batches below into savepoints.
    async with conn.transaction():
        cur = await conn.execute("SELECT key_id, count(*) FROM credential GROUP BY key_id")
        counts: dict[str, int] = {key_id: count for key_id, count in await cur.fetchall()}
    missing = {key_id: n for key_id, n in counts.items() if key_id not in keyring.key_ids}
    if missing:
        raise RotationBlockedError(missing)

    rotated = 0
    unreadable: list[uuid.UUID] = []
    after = uuid.UUID(int=0)
    while True:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, workspace_id, source_id, kind, key_id, nonce, ciphertext"
                " FROM credential WHERE key_id <> %s AND id > %s"
                " ORDER BY id LIMIT %s FOR NO KEY UPDATE",
                (key.id, after, batch_size),
            )
            rows = await cur.fetchall()
            for credential_id, workspace_id, source_id, kind, key_id, nonce, ciphertext in rows:
                aad = associated_data(
                    credential_id=credential_id,
                    workspace_id=workspace_id,
                    source_id=source_id,
                    kind=kind,
                )
                sealed = Sealed(key_id, bytes(nonce), bytes(ciphertext))
                try:
                    plaintext = keyring.open(sealed, aad, credential_id=credential_id)
                except (CredentialDecryptionError, CredentialKeyUnavailableError):
                    unreadable.append(credential_id)
                    continue
                fresh = key.seal(plaintext, aad)
                await conn.execute(
                    "UPDATE credential SET key_id = %s, nonce = %s, ciphertext = %s WHERE id = %s",
                    (fresh.key_id, fresh.nonce, fresh.ciphertext, credential_id),
                )
                rotated += 1
        if not rows:
            break
        after = rows[-1][0]

    async with conn.transaction():
        cur = await conn.execute("SELECT count(*) FROM credential WHERE key_id <> %s", (key.id,))
        row = await cur.fetchone()
    remaining: int = 0 if row is None else row[0]
    return RotationResult(
        key_id=key.id,
        rotated=rotated,
        already=counts.get(key.id, 0),
        unreadable=tuple(unreadable),
        remaining=remaining,
    )
