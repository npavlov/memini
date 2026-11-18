"""Incremental code sync: from the mirror of a git source to code records and their chunks.

The git native connector (requirements, sections 4 and 9). Each tracked ref of a git source is a
stream with a pinned commit. A sync fetches the mirror, compares the commit the ref names now with
the pin, writes the records and chunks of the files that changed, turns deleted and deselected
files into tombstones, and moves the pin in the transaction that commits the last of them
(principle 7).

- :mod:`memini.gitsync.model`: the identity of a code record (``refs/heads/main:src/app.py``) and
  the pin in ``stream.cursor``; :func:`stream_pin` for readers of code at the pinned commit.
- :mod:`memini.gitsync.rules`: which files are indexed (globs, built-in exclusions, type, size).
- :mod:`memini.gitsync.sync`: :func:`sync_stream`, the algorithm and its transactions.
- :mod:`memini.gitsync.handler`: the ``git_sync`` job handler.
- :mod:`memini.gitsync.content`: :class:`MirrorContent`, the raw content of code records for
  ``memini reindex``.
"""

from memini.gitsync.content import MirrorContent
from memini.gitsync.handler import GIT_SYNC, git_sync_handler, mirrors_unusable, run_git_sync
from memini.gitsync.model import (
    Pin,
    code_external_id,
    code_record_id,
    read_pin,
    split_code_external_id,
    stream_pin,
)
from memini.gitsync.rules import BUILTIN_EXCLUDE, FileRules, SkipReason
from memini.gitsync.sync import (
    DEFAULT_LIMITS,
    GitSource,
    GitStream,
    StreamChangedError,
    StreamSyncResult,
    SyncLimits,
    sync_stream,
)

__all__ = [
    "BUILTIN_EXCLUDE",
    "DEFAULT_LIMITS",
    "GIT_SYNC",
    "FileRules",
    "GitSource",
    "GitStream",
    "MirrorContent",
    "Pin",
    "SkipReason",
    "StreamChangedError",
    "StreamSyncResult",
    "SyncLimits",
    "code_external_id",
    "code_record_id",
    "git_sync_handler",
    "mirrors_unusable",
    "read_pin",
    "run_git_sync",
    "split_code_external_id",
    "stream_pin",
    "sync_stream",
]
