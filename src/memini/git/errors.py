"""Errors of the git connector. Every message is safe to log: no credential is ever part of one."""

__all__ = [
    "BlobTooLarge",
    "GitCommandError",
    "GitError",
    "GitTimeout",
    "GitUnavailable",
    "InvalidPath",
    "InvalidRevision",
    "MirrorBusy",
    "MirrorNotFound",
    "MirrorTooLarge",
    "NotABlob",
    "OutputTooLarge",
    "PathNotFound",
    "RemoteNotAllowed",
    "RevisionNotFound",
]


class GitError(Exception):
    """Base class of every error the git connector raises."""


class GitUnavailable(GitError):
    """The git binary is missing or older than the connector needs."""


class RemoteNotAllowed(GitError):
    """A source location was refused before any process started: a scheme or form that is not
    allowed, credentials in the URL, a local path outside the allowed roots."""


class GitCommandError(GitError):
    """A git command exited non-zero.

    ``stderr`` is the end of what git printed, with registered secrets masked.
    """

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        detail = f": {stderr}" if stderr else ""
        super().__init__(f"git {command} failed (exit {returncode}){detail}")


class GitTimeout(GitError):
    """A git command ran longer than its timeout; its process group was killed."""

    def __init__(self, command: str, timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        super().__init__(f"git {command} ran longer than {timeout:g} s and was killed")


class OutputTooLarge(GitError):
    """A git command printed more than the caller accepts; its process group was killed."""


class MirrorTooLarge(GitError):
    """A mirror grew past ``git_max_mirror_size``. The refs readers see were left as they were."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(
            f"the mirror would hold at least {size} bytes, over the limit of {limit} bytes"
            " (git_max_mirror_size); the previous state is kept"
        )


class MirrorBusy(GitError):
    """Another writer holds the mirror of this source."""


class MirrorNotFound(GitError):
    """The source has no mirror yet."""


class InvalidRevision(GitError):
    """A ref name or object id is malformed or uses revision syntax that is not accepted."""


class RevisionNotFound(GitError):
    """A ref or commit does not exist in the mirror."""


class InvalidPath(GitError):
    """A path is not a plain repository-relative path: empty, absolute, with ``.`` or ``..``
    components, empty components, a trailing slash or control characters."""


class PathNotFound(GitError):
    """No entry at that path in that commit."""


class NotABlob(GitError):
    """The entry at that path is a directory or a submodule, not a file."""


class BlobTooLarge(GitError):
    """A file is larger than the read limit (``git_max_blob_size``)."""

    def __init__(self, path: str, size: int, limit: int) -> None:
        self.path = path
        self.size = size
        self.limit = limit
        super().__init__(f"{path} holds {size} bytes, over the read limit of {limit} bytes")
