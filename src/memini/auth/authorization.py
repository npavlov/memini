"""Authorization: what a principal may do, the one filter every read goes through, and its
counterpart for writes.

Three separate questions:

- **May this token do this kind of operation?** Scopes, checked per route or tool before any work:
  :func:`ensure_scope` here, ``memini.api.auth.require_scope`` in the HTTP API.
- **Which rows may it read?** :func:`authorize_read`, the single authorization filter of
  requirements section 8. Every read goes through it: a read function takes the
  :class:`ReadFilter` it returns and puts :meth:`ReadFilter.where` into its query, so a read cannot
  be written without one.
- **Which rows may it change?** :func:`authorize_write`, the same question for writes. A write
  function takes the :class:`WriteAccess` it returns and puts :meth:`WriteAccess.where` into the
  query that finds what it changes, so a write cannot be written without one either.

In v1 the filter lets everything through except what the token itself limits. There is one
principal, and every row and every source is theirs, so ownership and source visibility narrow
nothing. A token limited to one workspace reads only that workspace: that limit is part of the
token, not a permission between users, but it is a limit on reads, and the filter is the one place
that every read already passes. Putting it anywhere else would make each route remember it.

Writes are limited the same way: a token limited to one workspace changes nothing outside it, and
creates no workspace. A row it may not change looks like a row that does not exist, as for reads.

Growing to several users (section 8) fills :meth:`ReadFilter.where` and :meth:`WriteAccess.where`
in, not their callers: they already receive the owner column and, on rows that belong to a source,
the source column, so they can add "owned by the principal, or in a source with
``visibility = 'workspace'`` of a workspace they belong to" as SQL. They decide in SQL, never in
Python, so callers need nothing else.
"""

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from psycopg import sql

from memini.auth.principal import Principal, Scope

__all__ = [
    "InsufficientScopeError",
    "ReadFilter",
    "WorkspaceLimitError",
    "WriteAccess",
    "authorize_read",
    "authorize_write",
    "ensure_scope",
]


class InsufficientScopeError(Exception):
    """The token lacks the scope an operation needs."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(f"the token lacks the {scope} scope")
        self.scope: Scope = scope


def ensure_scope(principal: Principal, scope: Scope) -> None:
    """Raise :class:`InsufficientScopeError` unless the principal's token has ``scope``."""
    if not principal.has_scope(scope):
        raise InsufficientScopeError(scope)


_TRUE = sql.SQL("TRUE")
_FALSE = sql.SQL("FALSE")


@dataclass(frozen=True, slots=True)
class ReadFilter:
    """The rows a principal may read: what :func:`authorize_read` decided.

    Use :meth:`where` in queries. The fields are the v1 form of the decision and may change.
    """

    principal: Principal
    workspace_ids: frozenset[UUID] | None
    """The workspaces reads are limited to; ``None`` for no limit by workspace."""

    def where(
        self,
        *,
        workspace_id: sql.Composable,
        owner_id: sql.Composable,
        source_id: sql.Composable | None = None,
    ) -> sql.Composable:
        """A SQL condition that holds for exactly the rows the principal may read.

        Pass the columns that scope the row: its workspace, its owner and, for a row that belongs
        to a source (a record, a stream, a run), its source. For the ``workspace`` table itself,
        the workspace column is ``id``. Example::

            access.where(workspace_id=sql.Identifier("r", "workspace_id"),
                         owner_id=sql.Identifier("r", "owner_id"),
                         source_id=sql.Identifier("r", "source_id"))

        Values are embedded as SQL literals, so the condition needs no query parameters. They are
        uuids only, which hold no ``%`` that could be taken for a placeholder.
        """
        # v1: one principal owns every row and may read every source, so owner_id and source_id
        # narrow nothing yet. They are taken now so that callers need no change when they do.
        del owner_id, source_id
        if self.workspace_ids is None:
            return _TRUE
        if not self.workspace_ids:
            return _FALSE
        return sql.SQL("{} = ANY({})").format(workspace_id, sql.Literal(sorted(self.workspace_ids)))


def authorize_read(
    principal: Principal, *, workspaces: Collection[UUID] | None = None
) -> ReadFilter:
    """The authorization filter: what ``principal`` may read, as a :class:`ReadFilter`.

    Every read goes through this function (requirements, section 8). It does not check scopes;
    the caller checks the ``read`` scope first.

    Args:
        principal: the authenticated caller.
        workspaces: the workspaces the request asks for, e.g. a search filter; ``None`` for all.
            The result never reaches beyond them; asking for a workspace the principal may not read
            gives no rows from it, not an error, so a caller cannot tell it from a missing one.
    """
    allowed: frozenset[UUID] | None = None
    if principal.workspace_id is not None:
        allowed = frozenset({principal.workspace_id})
    if workspaces is not None:
        requested = frozenset(workspaces)
        allowed = requested if allowed is None else allowed & requested
    return ReadFilter(principal=principal, workspace_ids=allowed)


class WorkspaceLimitError(Exception):
    """The token is limited to one workspace, and the write would reach beyond it: creating a
    workspace, for example."""

    def __init__(self) -> None:
        super().__init__("the token is limited to one workspace")


@dataclass(frozen=True, slots=True)
class WriteAccess:
    """What a principal may change: what :func:`authorize_write` decided.

    Write functions take one and use :meth:`where` in the query that finds the rows they change,
    so a row outside the access cannot be found, let alone changed. Code that acts for a principal
    with the database's own credentials and holds no token, such as an operator command or a test,
    builds one with :meth:`for_owner`.
    """

    owner_id: UUID
    """The principal the writes act as. Rows they create are owned by it."""
    workspace_ids: frozenset[UUID] | None
    """The workspaces writes are limited to; ``None`` for no limit by workspace."""

    @classmethod
    def for_owner(cls, owner_id: UUID) -> "WriteAccess":
        """Unlimited writes as ``owner_id``: for code that acts without a token."""
        return cls(owner_id=owner_id, workspace_ids=None)

    def allows(self, workspace_id: UUID) -> bool:
        """Whether rows in ``workspace_id`` may be changed."""
        return self.workspace_ids is None or workspace_id in self.workspace_ids

    def ensure_unlimited(self) -> None:
        """Raise :class:`WorkspaceLimitError` if the access is limited to some workspaces.

        For writes that are not inside one existing workspace, such as creating a workspace.
        """
        if self.workspace_ids is not None:
            raise WorkspaceLimitError

    def where(
        self,
        *,
        workspace_id: sql.Composable,
        owner_id: sql.Composable,
        source_id: sql.Composable | None = None,
    ) -> sql.Composable:
        """A SQL condition that holds for exactly the rows the principal may change.

        Takes the same columns as :meth:`ReadFilter.where`, for the same reason: with several
        users, ownership and sources will narrow writes too, and callers need no change then.
        """
        del owner_id, source_id  # v1: one principal owns every row
        if self.workspace_ids is None:
            return _TRUE
        if not self.workspace_ids:
            return _FALSE
        return sql.SQL("{} = ANY({})").format(workspace_id, sql.Literal(sorted(self.workspace_ids)))


def authorize_write(principal: Principal, scope: Scope) -> WriteAccess:
    """What ``principal`` may change with ``scope`` (``admin`` or ``ingest``), as a
    :class:`WriteAccess`.

    Every write goes through this function. Unlike :func:`authorize_read`, it checks the scope: a
    write has no other gate.

    Raises:
        InsufficientScopeError: the token lacks ``scope``.
    """
    ensure_scope(principal, scope)
    allowed = None if principal.workspace_id is None else frozenset({principal.workspace_id})
    return WriteAccess(owner_id=principal.principal_id, workspace_ids=allowed)
