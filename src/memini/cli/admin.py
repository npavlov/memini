"""Setup commands: ``migrate``, ``workspace create|list`` and ``token create|list|revoke``.

Each runs in one transaction as the principal the CLI acts as (``settings.principal``) and prints
its result only after the transaction has committed.
"""

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from psycopg import errors

from memini.auth import (
    Scope,
    TokenInfo,
    create_token,
    is_well_formed_token,
    list_tokens,
    revoke_token,
)
from memini.auth.tokens import display_prefix
from memini.cli.common import (
    EXIT_OK,
    CommandError,
    format_time,
    message,
    principal_session,
    print_table,
)
from memini.config import Settings
from memini.db import migrate
from memini.workspaces import check_handle, create_workspace, list_owned_workspaces

__all__ = [
    "DEFAULT_TOKEN_NAME",
    "MAX_TOKEN_LIFETIME",
    "create_token_command",
    "create_workspace_command",
    "list_tokens_command",
    "list_workspaces_command",
    "migrate_command",
    "parse_duration",
    "parse_token_name",
    "parse_workspace_name",
    "revoke_token_command",
]

DEFAULT_TOKEN_NAME: Final = "cli"  # noqa: S105 - a label, not a secret
MAX_TOKEN_LIFETIME: Final = timedelta(days=36525)
"""About a hundred years; for longer, create a token that does not expire."""

ALL_WORKSPACES: Final = "*"
"""Shown in ``token list`` for a token that is not limited to one workspace. Not a valid handle,
so it cannot be taken for a workspace name."""

_UNITS: Final = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION: Final = re.compile(r"(?:\d+[smhdw])+")
_DURATION_PART: Final = re.compile(r"(\d+)([smhdw])")


# --- Argument types ------------------------------------------------------------------------------


def parse_duration(value: str) -> timedelta:
    """A duration such as ``30d``, ``12h`` or ``1w2d``: numbers with units s, m, h, d, w."""
    text = value.strip().lower()
    if _DURATION.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            "a duration is a number and a unit, such as 90d, 12h or 1w2d (units: s, m, h, d, w)"
        )
    try:
        total = sum(
            (
                timedelta(**{_UNITS[unit]: int(number)})
                for number, unit in _DURATION_PART.findall(text)
            ),
            timedelta(),
        )
    except OverflowError:
        total = MAX_TOKEN_LIFETIME + timedelta(days=1)
    if total <= timedelta():
        raise argparse.ArgumentTypeError("a duration must be longer than zero")
    if total > MAX_TOKEN_LIFETIME:
        raise argparse.ArgumentTypeError(
            "a duration is at most 36525d; leave out --expires-in for a token that does not expire"
        )
    return total


def parse_workspace_name(value: str) -> str:
    try:
        return check_handle("workspace", value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parse_token_name(value: str) -> str:
    name = value.strip()
    if not name or not name.isprintable():
        raise argparse.ArgumentTypeError("a token name is printable text and not empty")
    return name


# --- migrate -------------------------------------------------------------------------------------


async def migrate_command(args: argparse.Namespace, settings: Settings) -> int:
    """Apply pending migrations and print each one applied."""
    applied = await migrate(settings.database_url.get_secret_value())
    if not applied:
        print("no pending migrations; the database is up to date")
    for migration in applied:
        print(f"applied {migration.file_name}")
    return EXIT_OK


# --- workspace -----------------------------------------------------------------------------------


async def create_workspace_command(args: argparse.Namespace, settings: Settings) -> int:
    name: str = args.name
    async with principal_session(settings) as (conn, owner_id):
        try:
            workspace = await create_workspace(conn, owner_id=owner_id, name=name)
        except errors.UniqueViolation:
            raise CommandError(f"a workspace named {name} exists") from None
    print(f"created workspace {workspace.name} ({workspace.id})")
    return EXIT_OK


async def list_workspaces_command(args: argparse.Namespace, settings: Settings) -> int:
    async with principal_session(settings) as (conn, owner_id):
        found = await list_owned_workspaces(conn, owner_id=owner_id)
    if not found:
        message("no workspaces yet; create one with `memini workspace create <name>`")
        return EXIT_OK
    print_table(
        ("NAME", "CREATED", "ID"),
        ((w.name, format_time(w.created_at), str(w.id)) for w in found),
    )
    return EXIT_OK


# --- token ---------------------------------------------------------------------------------------


async def create_token_command(args: argparse.Namespace, settings: Settings) -> int:
    """Create a token and print it, alone, on stdout. Everything else goes to stderr."""
    scopes: list[Scope] = args.scope
    workspace_name: str | None = args.workspace
    name: str = args.name
    expires_in: timedelta | None = args.expires_in

    async with principal_session(settings) as (conn, owner_id):
        workspace_id: UUID | None = None
        if workspace_name is not None:
            owned = await list_owned_workspaces(conn, owner_id=owner_id)
            matches = [w.id for w in owned if w.name == workspace_name]
            if not matches:
                raise CommandError(
                    f"no workspace named {workspace_name}; see `memini workspace list`"
                )
            workspace_id = matches[0]
        expires_at = None if expires_in is None else datetime.now(UTC) + expires_in
        try:
            new = await create_token(
                conn,
                owner_id=owner_id,
                name=name,
                scopes=scopes,
                workspace_id=workspace_id,
                expires_at=expires_at,
            )
        except errors.CheckViolation:
            # token_expires_at_check: the expiry is not after the database's clock.
            raise CommandError("the expiry time is not in the future") from None

    info = new.info
    message(
        f"created token {info.id} named {info.name!r}: prefix {info.prefix},"
        f" scopes {', '.join(info.scopes)},"
        f" {'workspace ' + workspace_name if workspace_name else 'every workspace'},"
        f" expires {format_time(info.expires_at)}"
    )
    message("the token is printed below, on stdout, this once; it cannot be shown again")
    print(new.secret.get_secret_value(), file=sys.stdout, flush=True)
    return EXIT_OK


async def list_tokens_command(args: argparse.Namespace, settings: Settings) -> int:
    """Every token of the principal, without secrets: a token is never stored, only its digest."""
    async with principal_session(settings) as (conn, owner_id):
        tokens = await list_tokens(conn, owner_id=owner_id)
        names = {w.id: w.name for w in await list_owned_workspaces(conn, owner_id=owner_id)}
    if not tokens:
        message("no tokens yet; create one with `memini token create --scope read`")
        return EXIT_OK
    now = datetime.now(UTC)
    print_table(
        (
            "ID",
            "PREFIX",
            "NAME",
            "SCOPES",
            "WORKSPACE",
            "STATUS",
            "CREATED",
            "EXPIRES",
            "LAST USED",
        ),
        (
            (
                str(t.id),
                t.prefix,
                t.name,
                ",".join(t.scopes),
                ALL_WORKSPACES if t.workspace_id is None else names.get(t.workspace_id, "?"),
                t.status(now),
                format_time(t.created_at),
                format_time(t.expires_at),
                format_time(t.last_used_at),
            )
            for t in tokens
        ),
    )
    return EXIT_OK


async def revoke_token_command(args: argparse.Namespace, settings: Settings) -> int:
    """Revoke a token given by its id or its prefix, as ``token list`` shows them."""
    reference: str = args.token
    # A whole token pasted by mistake is matched by its prefix and never repeated in output.
    if is_well_formed_token(reference):
        reference = display_prefix(reference)
    async with principal_session(settings) as (conn, owner_id):
        matches = [t for t in await list_tokens(conn, owner_id=owner_id) if _is(t, reference)]
        if not matches:
            raise CommandError(f"no token {reference}; see `memini token list`")
        if len(matches) > 1:
            raise CommandError(f"{reference} matches {len(matches)} tokens; give the token id")
        before = matches[0]
        info = await revoke_token(conn, owner_id=owner_id, token_id=before.id)
    if before.revoked_at is not None:
        revoked_at = format_time(info.revoked_at)
        print(f"token {info.id} ({info.prefix}) was revoked already, at {revoked_at}")
    else:
        print(f"revoked token {info.id} ({info.prefix}, {info.name!r})")
    return EXIT_OK


def _is(token: TokenInfo, reference: str) -> bool:
    try:
        return token.id == UUID(reference)
    except ValueError:
        return token.prefix == reference
