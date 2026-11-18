"""From the errors of the administration library to the API's error shape.

Wrap a call into :mod:`memini.workspaces`, :mod:`memini.sources` or the run reads of
:mod:`memini.runs` in :func:`admin_errors`. What each error becomes:

- :class:`~memini.workspaces.NotFoundError`: 404 ``not_found``. A workspace, source, stream or run
  the token may not see is reported like one that does not exist.
- :class:`~memini.auth.WorkspaceLimitError`: 403 ``workspace_limited``: the token is limited to
  one workspace and the write would reach beyond it.
- :class:`~memini.workspaces.WorkspaceExistsError`, :class:`~memini.sources.SourceExistsError`:
  409 ``conflict``.
- :class:`~memini.sources.SourceBusyError`: 409 ``source_busy``: a job of the source runs; try
  again when it is done.
- :class:`~memini.sources.NotSyncableError`: 409 ``not_syncable``.
- :class:`~memini.sources.InvalidSourceError`: 422 ``invalid_source``. Its message names the
  setting and the reason and never the value, so it is passed on: a location can hold a
  credential, and it is not repeated.
"""

from collections.abc import Generator
from contextlib import contextmanager

from memini.api.errors import ApiError
from memini.auth import WorkspaceLimitError
from memini.sources import (
    InvalidSourceError,
    NotSyncableError,
    SourceBusyError,
    SourceExistsError,
)
from memini.workspaces import NotFoundError, WorkspaceExistsError

__all__ = ["admin_errors"]


@contextmanager
def admin_errors() -> Generator[None]:
    """Raise the :class:`ApiError` for an error of the administration library; see the module."""
    try:
        yield
    except NotFoundError as error:
        raise ApiError(404, "not_found", f"No such {error.what}.") from None
    except WorkspaceLimitError:
        raise ApiError(
            403,
            "workspace_limited",
            "The token is limited to one workspace; this needs a token that is not.",
        ) from None
    except WorkspaceExistsError:
        raise ApiError(409, "conflict", "A workspace of that name exists.") from None
    except SourceExistsError:
        raise ApiError(409, "conflict", "The workspace has a source of that name.") from None
    except SourceBusyError:
        raise ApiError(
            409,
            "source_busy",
            "A job of the source is running; try again when it is done.",
        ) from None
    except NotSyncableError:
        raise ApiError(
            409, "not_syncable", "Sources of this kind are not synced by the service."
        ) from None
    except InvalidSourceError as error:
        raise ApiError(
            422,
            "invalid_source",
            f"The source setting {error.setting} is not valid: {error.reason}.",
        ) from None
