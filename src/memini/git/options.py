"""What the git connector needs from the settings, as one value it can be built and tested with."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Self

from pydantic import SecretStr

from memini.config import Settings

__all__ = ["DEFAULT_COMMAND_TIMEOUT", "GitOptions", "default_mirror_dir"]

DEFAULT_COMMAND_TIMEOUT: Final = 120.0
"""Seconds a local git command (rev-parse, ls-tree, cat-file, update-ref …) may take."""


def default_mirror_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Where mirrors live when ``git_mirror_dir`` is not set: the user's data directory.

    ``$XDG_DATA_HOME/memini/mirrors`` if that variable holds an absolute path, otherwise
    ``~/.local/share/memini/mirrors``. The image sets ``MEMINI_GIT_MIRROR_DIR`` to
    ``/var/lib/memini/mirrors``, a volume, so this applies to a service run from source.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    data_home = env.get("XDG_DATA_HOME", "")
    base = Path(data_home) if os.path.isabs(data_home) else Path.home() / ".local" / "share"
    return base / "memini" / "mirrors"


@dataclass(frozen=True, slots=True, kw_only=True)
class GitOptions:
    """Settings of the git connector. Build it with :meth:`from_settings`; tests build it directly.

    The ``Settings`` fields of the same names (with the ``git_`` prefix) document each one.
    """

    mirror_dir: Path
    fetch_timeout: float = 600.0
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    max_mirror_size: int = 5 * 1024**3
    max_blob_size: int = 1024**2
    allow_http: bool = False
    local_roots: tuple[Path, ...] = ()
    https_token: SecretStr | None = field(default=None, repr=False)
    https_username: str = "x-access-token"
    https_token_hosts: frozenset[str] = frozenset()
    ssh_key_path: Path | None = None
    ssh_known_hosts_path: Path | None = None
    git: str = "git"
    """The git executable, looked up on ``PATH`` unless it is a path."""
    size_check_interval: float = 1.0
    """Seconds between two checks of a mirror's size while a fetch runs."""

    def __post_init__(self) -> None:
        if not self.mirror_dir.is_absolute():
            raise ValueError("the mirror directory must be an absolute path")
        if any(not root.is_absolute() for root in self.local_roots):
            raise ValueError("local roots must be absolute paths")
        if self.https_token is not None and not self.https_token_hosts:
            raise ValueError("an https token needs the hosts it may be sent to")

    @classmethod
    def from_settings(cls, settings: Settings, environ: Mapping[str, str] | None = None) -> Self:
        """The options ``settings`` describe; ``environ`` only serves :func:`default_mirror_dir`."""
        return cls(
            mirror_dir=settings.git_mirror_dir or default_mirror_dir(environ),
            fetch_timeout=float(settings.git_fetch_timeout),
            max_mirror_size=settings.git_max_mirror_size,
            max_blob_size=settings.git_max_blob_size,
            allow_http=settings.git_allow_http,
            local_roots=settings.git_local_roots,
            https_token=settings.git_https_token,
            https_username=settings.git_https_username,
            https_token_hosts=frozenset(settings.git_https_token_hosts),
            ssh_key_path=settings.git_ssh_key_path,
            ssh_known_hosts_path=settings.git_ssh_known_hosts_path,
        )
