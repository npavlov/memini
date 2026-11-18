"""The git connector's raw store: a bare mirror of every git source, and reads from it.

- :class:`MirrorManager` creates (:meth:`~MirrorManager.ensure_mirror`), fetches
  (:meth:`~MirrorManager.fetch`, :meth:`~MirrorManager.sync`) and removes mirrors, and reads them:
  refs, :meth:`~MirrorManager.resolve`, :meth:`~MirrorManager.ls_tree`,
  :meth:`~MirrorManager.read_blob`, :meth:`~MirrorManager.read_blobs`,
  :meth:`~MirrorManager.diff_names`. :mod:`memini.git.mirror` describes the layout, the atomic
  fetch and the locking.
- :func:`parse_remote` checks a source location against the allowlist of transports before any
  process starts (:mod:`memini.git.remote`).
- :class:`Git` runs the git CLI hardened, with credentials kept out of arguments, URLs and files
  (:mod:`memini.git.command`).
- :class:`GitOptions` holds the ``git_*`` settings.

Every error is a :class:`GitError`; none carries a credential.
"""

from memini.git.command import MIN_GIT_VERSION, Command, Git, Output, SizeLimit, directory_size
from memini.git.errors import (
    BlobTooLarge,
    GitCommandError,
    GitError,
    GitTimeout,
    GitUnavailable,
    InvalidPath,
    InvalidRevision,
    MirrorBusy,
    MirrorNotFound,
    MirrorTooLarge,
    NotABlob,
    OutputTooLarge,
    PathNotFound,
    RemoteNotAllowed,
    RevisionNotFound,
)
from memini.git.mirror import (
    INCOMING_NAMESPACE,
    FetchResult,
    FileChange,
    Mirror,
    MirrorManager,
    RefUpdate,
    TreeEntry,
    check_path,
)
from memini.git.options import GitOptions, default_mirror_dir
from memini.git.remote import Remote, Transport, parse_remote

__all__ = [
    "INCOMING_NAMESPACE",
    "MIN_GIT_VERSION",
    "BlobTooLarge",
    "Command",
    "FetchResult",
    "FileChange",
    "Git",
    "GitCommandError",
    "GitError",
    "GitOptions",
    "GitTimeout",
    "GitUnavailable",
    "InvalidPath",
    "InvalidRevision",
    "Mirror",
    "MirrorBusy",
    "MirrorManager",
    "MirrorNotFound",
    "MirrorTooLarge",
    "NotABlob",
    "Output",
    "OutputTooLarge",
    "PathNotFound",
    "RefUpdate",
    "Remote",
    "RemoteNotAllowed",
    "RevisionNotFound",
    "SizeLimit",
    "Transport",
    "TreeEntry",
    "check_path",
    "default_mirror_dir",
    "directory_size",
    "parse_remote",
]
