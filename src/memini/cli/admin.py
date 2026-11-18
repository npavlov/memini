"""Operator commands on the database: ``migrate``, ``bootstrap`` and ``workspace llm``.

They run where the service's settings are, inside its container, because they come before the API
can be used or stand above it: the database must be migrated before the service starts, the first
token cannot come from an API that asks for one, and a workspace's opt-in to a cloud LLM is the
operator's alone, which no token may give (principle 8). Everything else the CLI does goes through
the API, but for ``reindex``, ``eval`` and ``credentials``, operator commands of their own modules.

``bootstrap`` acts as the principal named by the ``principal`` setting (``MEMINI_PRINCIPAL``,
default ``owner``), created on first use. v1 has one user: that principal owns the first admin
token, and so every workspace, source and token made with it.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Final

from memini.auth import Scope, TokenInfo, WriteAccess, create_token, list_tokens
from memini.cli.arguments import parse_workspace_name
from memini.cli.common import (
    EXIT_OK,
    CommandError,
    format_time,
    message,
    principal_session,
)
from memini.config import Settings
from memini.db import migrate
from memini.llm import (
    LLM_POLICIES,
    LlmPolicy,
    configured_providers,
    get_workspace_llm_policy,
    set_workspace_llm_policy,
)
from memini.workspaces import NotFoundError

__all__ = [
    "BOOTSTRAP_SCOPES",
    "DEFAULT_BOOTSTRAP_NAME",
    "WORKSPACE_LLM_DESCRIPTION",
    "WORKSPACE_LLM_HELP",
    "bootstrap_command",
    "configure_workspace_llm",
    "migrate_command",
    "parse_workspace_name",
    "workspace_llm_command",
]

BOOTSTRAP_SCOPES: Final[tuple[Scope, ...]] = ("read", "admin")
"""The scopes of the first token: what the client commands need, to set up and to look."""

DEFAULT_BOOTSTRAP_NAME: Final = "bootstrap"


# --- migrate -------------------------------------------------------------------------------------


async def migrate_command(args: argparse.Namespace, settings: Settings) -> int:
    """Apply pending migrations and print each one applied."""
    applied = await migrate(settings.database_url.get_secret_value())
    if not applied:
        print("no pending migrations; the database is up to date")
    for migration in applied:
        print(f"applied {migration.file_name}")
    return EXIT_OK


# --- bootstrap -----------------------------------------------------------------------------------


def _admin_for_everything(token: TokenInfo, now: datetime) -> bool:
    """An active admin token that is not limited to one workspace: one that can do all setup."""
    return token.status(now) == "active" and "admin" in token.scopes and token.workspace_id is None


async def bootstrap_command(args: argparse.Namespace, settings: Settings) -> int:
    """Create the first admin token and print it, alone, on stdout. Everything else goes to stderr.

    Refused while an active admin token for every workspace exists, unless ``--force``: the
    command is how an operator gets in, not a way around the API's token management.
    """
    force: bool = args.force
    name: str = args.name
    expires_in: timedelta | None = args.expires_in

    async with principal_session(settings) as (conn, owner_id):
        # Two bootstraps at once see each other's token: the second waits for the first.
        await conn.execute("SELECT 1 FROM principal WHERE id = %s FOR UPDATE", (owner_id,))
        cur = await conn.execute("SELECT now()")
        row = await cur.fetchone()
        now: datetime = datetime.now(UTC) if row is None else row[0]
        existing = [
            t for t in await list_tokens(conn, owner_id=owner_id) if _admin_for_everything(t, now)
        ]
        if existing and not force:
            prefixes = ", ".join(t.prefix for t in existing)
            raise CommandError(
                f"an active admin token exists ({prefixes}); manage tokens with `memini token`"
                " through the API, or pass --force to create another one"
            )
        new = await create_token(
            conn,
            owner_id=owner_id,
            name=name,
            scopes=BOOTSTRAP_SCOPES,
            expires_at=None if expires_in is None else now + expires_in,
        )

    info = new.info
    message(
        f"created token {info.id} named {info.name!r} for principal {settings.principal}:"
        f" prefix {info.prefix}, scopes {', '.join(info.scopes)}, every workspace,"
        f" expires {format_time(info.expires_at)}"
    )
    message("the token is printed below, on stdout, this once; it cannot be shown again")
    print(new.secret.get_secret_value(), file=sys.stdout, flush=True)
    return EXIT_OK


# --- workspace llm -------------------------------------------------------------------------------

WORKSPACE_LLM_HELP: Final = "show or set which LLM may read a workspace's content"
WORKSPACE_LLM_DESCRIPTION: Final = (
    "Without a policy, show the workspace's llm_policy and the configured providers. With one,"
    " set it: none (no LLM), local (the local provider only; the default) or cloud (opt in to the"
    " cloud provider for sources whose llm_policy is cloud). A source's own llm_policy can narrow"
    " it, never widen it. The HTTP API cannot set it, so no token can opt a workspace in."
)


def configure_workspace_llm(parser: argparse.ArgumentParser) -> None:
    """The arguments of ``workspace llm``, on the parser given."""
    parser.add_argument("name", type=parse_workspace_name, help="the workspace")
    parser.add_argument(
        "policy",
        nargs="?",
        choices=LLM_POLICIES,
        help="none, local or cloud; leave out to show the current one",
    )
    parser.set_defaults(command=workspace_llm_command)


async def workspace_llm_command(args: argparse.Namespace, settings: Settings) -> int:
    """Show the ``llm_policy`` of a workspace and the providers it reaches, or set it.

    ``cloud`` opts the workspace in to the cloud provider (principle 8). It is set here only: the
    HTTP API has no route that changes it.
    """
    name: str = args.name
    policy: LlmPolicy | None = args.policy
    providers = configured_providers(settings)
    async with principal_session(settings) as (conn, owner_id):
        try:
            if policy is None:
                current = await get_workspace_llm_policy(conn, owner_id=owner_id, name=name)
            else:
                access = WriteAccess.for_owner(owner_id)
                current = await set_workspace_llm_policy(conn, access, name, policy)
        except NotFoundError:
            raise CommandError(f"no workspace named {name}; see `memini workspace list`") from None
    if policy is None:
        print(f"workspace {name}: llm_policy {current}")
        for provider_class in ("local", "cloud"):
            info = providers.get(provider_class)
            shown = "not configured" if info is None else info.describe()
            print(f"  {provider_class} provider: {shown}")
        return EXIT_OK

    print(f"workspace {name}: llm_policy {current} -> {policy}")
    cloud, local = providers.get("cloud"), providers.get("local")
    no_local = "no local provider is configured (MEMINI_LLM_LOCAL_URL)"
    if policy == "none":
        message(f"no LLM reads content of {name} from now on")
    elif policy == "local" and local is None:
        message(f"{no_local}; until one is, no LLM reads content of {name}")
    elif policy == "cloud":
        if cloud is None:
            message(
                "no cloud provider is configured (MEMINI_LLM_CLOUD_URL); until one is, content"
                f" of {name} goes to the local provider only"
            )
        else:
            message(
                f"content of {name} whose sources all have llm_policy cloud may now go to the"
                f" cloud provider, {cloud.describe()}"
            )
        if local is None:
            message(f"{no_local}; no LLM reads content of {name} that may not go to the cloud")
    return EXIT_OK
