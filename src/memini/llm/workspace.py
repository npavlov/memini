"""The LLM policy of a workspace: read it, and change it.

The workspace's ``llm_policy`` (migration 0007) is the most any LLM may read in it; a source's
policy can narrow it further, never widen it. ``cloud`` is the workspace's opt-in to the cloud
provider (principle 8). Only the CLI changes it (``memini workspace llm``), as the operator working
on the database directly; the HTTP API has no route for it, so a token, even one with the
``admin`` scope, cannot opt a workspace in.
"""

from typing import Any, Final
from uuid import UUID

from psycopg import AsyncConnection, sql

from memini.auth import WriteAccess
from memini.llm.model import LLM_POLICIES, LlmPolicy
from memini.workspaces import NotFoundError

__all__ = ["get_workspace_llm_policy", "set_workspace_llm_policy"]

_FIND: Final = sql.SQL(
    "SELECT w.id, w.llm_policy FROM workspace w WHERE w.name = %s AND {} FOR UPDATE"
)


async def get_workspace_llm_policy(
    conn: AsyncConnection[Any], *, owner_id: UUID, name: str
) -> LlmPolicy:
    """The ``llm_policy`` of the workspace ``name`` that ``owner_id`` owns. For the CLI.

    Raises:
        NotFoundError: ``owner_id`` owns no workspace of that name.
    """
    cur = await conn.execute(
        "SELECT llm_policy FROM workspace WHERE name = %s AND owner_id = %s", (name, owner_id)
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError("workspace")
    return _policy(row[0])


async def set_workspace_llm_policy(
    conn: AsyncConnection[Any], access: WriteAccess, name: str, policy: LlmPolicy
) -> LlmPolicy:
    """Set the ``llm_policy`` of the workspace ``name``; return the one it had.

    Runs in a transaction of its own, or a savepoint of the caller's.

    Raises:
        ValueError: ``policy`` is not ``none``, ``local`` or ``cloud``.
        NotFoundError: no such workspace that ``access`` may change.
    """
    if policy not in LLM_POLICIES:
        raise ValueError("an llm_policy is none, local or cloud")
    condition = access.where(
        workspace_id=sql.Identifier("w", "id"), owner_id=sql.Identifier("w", "owner_id")
    )
    async with conn.transaction():
        cur = await conn.execute(_FIND.format(condition), (name,))
        row = await cur.fetchone()
        if row is None:
            raise NotFoundError("workspace")
        workspace_id: UUID = row[0]
        await conn.execute(
            "UPDATE workspace SET llm_policy = %s WHERE id = %s", (policy, workspace_id)
        )
    return _policy(row[1])


def _policy(value: object) -> LlmPolicy:
    if value not in LLM_POLICIES:  # the schema's check makes this unreachable
        raise ValueError("the workspace has an llm_policy that is not none, local or cloud")
    return value
