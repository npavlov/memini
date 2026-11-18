"""Client commands for tokens: ``memini token create|list|revoke``, through ``/v1/tokens``.

They need a token with the ``admin`` scope; the rules (a token limited to one workspace manages
only tokens limited to it) are the service's, :mod:`memini.token_admin`. The first admin token
comes from ``memini bootstrap`` inside the service's container.

``token create`` prints the new token alone on stdout, so ``TOKEN=$(memini token create …)``
captures it, and everything else on stderr. It is shown this once: only its digest is stored. No
other output holds a token: listings show its prefix.
"""

import argparse
import sys
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from memini.auth import SCOPES, is_well_formed_token
from memini.auth.tokens import display_prefix
from memini.cli.arguments import (
    Commands,
    parse_lifetime,
    parse_token_name,
    parse_workspace_name,
)
from memini.cli.client import Api, Json
from memini.cli.common import EXIT_OK, CommandError, message, print_table
from memini.cli.output import as_list, as_mapping, print_json, text, when

__all__ = ["ALL_WORKSPACES", "DEFAULT_TOKEN_NAME", "add_commands"]

DEFAULT_TOKEN_NAME: Final = "cli"  # noqa: S105 - a label, not a secret

ALL_WORKSPACES: Final = "*"
"""Shown in ``token list`` for a token that is not limited to one workspace. Not a valid handle,
so it cannot be taken for a workspace name."""


def create_token_command(args: argparse.Namespace, api: Api) -> int:
    """Create a token and print it, alone, on stdout. Everything else goes to stderr."""
    expires_in: timedelta | None = args.expires_in
    body: Json = {
        "scopes": args.scope,
        "name": args.name,
        "workspace": args.workspace,
        "expires_in_seconds": None if expires_in is None else int(expires_in.total_seconds()),
    }
    created = api.post("v1/tokens", body)
    token = created.get("token")
    if not isinstance(token, str) or not is_well_formed_token(token):
        raise CommandError("the service answered without a token; nothing was printed")
    workspace = created.get("workspace")
    message(
        f"created token {text(created.get('id'))} named {text(created.get('name'))!r}:"
        f" prefix {text(created.get('prefix'))}, scopes {text(created.get('scopes'))},"
        f" {'every workspace' if workspace is None else 'workspace ' + text(workspace)},"
        f" expires {when(created.get('expires_at'), 'never')}"
    )
    message("the token is printed below, on stdout, this once; it cannot be shown again")
    print(token, file=sys.stdout, flush=True)
    return EXIT_OK


def list_tokens_command(args: argparse.Namespace, api: Api) -> int:
    """The tokens the caller may see, without secrets: a token is never stored, only its digest."""
    body = api.get("v1/tokens")
    if args.json:
        print_json(body)
        return EXIT_OK
    tokens = [as_mapping(t) for t in as_list(body.get("tokens"))]
    if not tokens:
        message("no tokens yet; create one with `memini token create --scope read`")
        return EXIT_OK
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
                text(t.get("id")),
                text(t.get("prefix")),
                text(t.get("name")),
                ",".join(text(scope) for scope in as_list(t.get("scopes"))),
                text(t.get("workspace"), ALL_WORKSPACES),
                text(t.get("status")),
                when(t.get("created_at")),
                when(t.get("expires_at"), "never"),
                when(t.get("last_used_at"), "never"),
            )
            for t in tokens
        ),
    )
    return EXIT_OK


def revoke_token_command(args: argparse.Namespace, api: Api) -> int:
    """Revoke a token given by its id or its prefix, as ``token list`` shows them."""
    reference: str = args.token
    # A whole token pasted by mistake is matched by its prefix and never repeated in output.
    if is_well_formed_token(reference):
        reference = display_prefix(reference)
    tokens = [as_mapping(t) for t in as_list(api.get("v1/tokens").get("tokens"))]
    matches = [t for t in tokens if _is(t, reference)]
    if not matches:
        raise CommandError(f"no token {text(reference)}; see `memini token list`")
    if len(matches) > 1:
        raise CommandError(f"{text(reference)} matches {len(matches)} tokens; give the token id")
    before = matches[0]
    revoked = api.delete(f"v1/tokens/{UUID(str(before.get('id')))}") or {}
    token_id, prefix = text(revoked.get("id")), text(revoked.get("prefix"))
    if before.get("revoked_at") is not None:
        print(
            f"token {token_id} ({prefix}) was revoked already, at {when(revoked.get('revoked_at'))}"
        )
    else:
        print(f"revoked token {token_id} ({prefix}, {text(revoked.get('name'))!r})")
    return EXIT_OK


def _is(token: Mapping[str, Any], reference: str) -> bool:
    try:
        return str(token.get("id")) == str(UUID(reference))
    except ValueError:
        return token.get("prefix") == reference


def add_commands(commands: Commands) -> None:
    """Add ``token create|list|revoke``."""
    token = commands.add_client("token", help="create, list and revoke tokens (admin)")
    token_commands = token.add_subparsers(metavar="<command>", required=True)
    create = token_commands.add_parser(
        "create",
        help="create a token and print it once",
        description="Create a bearer token. The token alone goes to stdout, and is shown only"
        " this once; details go to stderr. A token limited to one workspace may only create"
        " tokens limited to it.",
    )
    create.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=SCOPES,
        help="what the token may do; repeat for more than one",
    )
    create.add_argument(
        "--workspace",
        metavar="NAME",
        type=parse_workspace_name,
        help="limit the token to this workspace (default: every workspace)",
    )
    create.add_argument(
        "--name",
        metavar="LABEL",
        type=parse_token_name,
        default=DEFAULT_TOKEN_NAME,
        help=f"what the token is for, e.g. 'laptop agent' (default: {DEFAULT_TOKEN_NAME})",
    )
    create.add_argument(
        "--expires-in",
        metavar="DURATION",
        type=parse_lifetime,
        help="e.g. 90d, 12h or 1w2d (units: s, m, h, d, w); default: never expires",
    )
    create.set_defaults(client=create_token_command)

    listing = token_commands.add_parser(
        "list", help="list tokens, without secrets", description="List tokens, without secrets."
    )
    listing.add_argument("--json", action="store_true", help="print the API's JSON answer")
    listing.set_defaults(client=list_tokens_command)

    revoke = token_commands.add_parser(
        "revoke", help="revoke a token", description="Revoke a token from now on."
    )
    revoke.add_argument(
        "token", metavar="id-or-prefix", help="the token's id or prefix, as `token list` shows"
    )
    revoke.set_defaults(client=revoke_token_command)
