"""``memini credentials``: set, list and delete the credentials of sources; rotate the key.

- ``set --workspace W --source S --kind api_key`` reads the key from stdin (from a terminal without
  echo), never from an argument, which other users of the machine could see in the process list.
- ``set ... --kind oauth`` reads a JSON object from stdin: the token response of the provider
  (``access_token``, ``refresh_token``, ``expires_in`` or ``expires_at``) and the client it was
  issued to (``token_endpoint``, ``client_id``, ``client_secret``, ``scope``). Only
  ``access_token`` is required.
- ``list`` shows what is stored without secrets; ``delete`` removes a source's credential.
- ``rotate-key`` re-encrypts every credential under ``MEMINI_CREDENTIAL_KEY``, reading the old ones
  with ``MEMINI_CREDENTIAL_OLD_KEYS`` (:mod:`memini.credentials.rotation` has the procedure). It
  exits 1 while credentials are left under another key.

No output holds a secret: commands print ids, names, kinds, key ids and times.
"""

import argparse
import getpass
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

from pydantic import SecretStr

from memini.auth import WriteAccess
from memini.cli.admin import parse_workspace_name
from memini.cli.common import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    CommandError,
    Conn,
    format_time,
    message,
    principal_session,
    print_table,
    require_migrated,
)
from memini.config import Settings
from memini.credentials import (
    DEFAULT_ROTATION_BATCH,
    CredentialError,
    CredentialInfo,
    CredentialKeyNotConfiguredError,
    InvalidCredentialError,
    Keyring,
    OAuthClient,
    OAuthTokens,
    delete_credential,
    list_credentials,
    put_api_key,
    put_oauth,
    resolve_source,
    rotate_key,
)
from memini.db import connect
from memini.workspaces import NotFoundError, check_handle, list_owned_workspaces

__all__ = [
    "DESCRIPTION",
    "HELP",
    "OAUTH_FIELDS",
    "configure_parser",
    "delete_command",
    "list_command",
    "rotate_key_command",
    "set_command",
]

OAUTH_FIELDS: Final = (
    "access_token",
    "refresh_token",
    "expires_in",
    "expires_at",
    "token_type",
    "token_endpoint",
    "client_id",
    "client_secret",
    "scope",
)
"""The fields ``set --kind oauth`` reads. ``token_type`` is accepted, as a token response holds
it, and ignored: the service sends the access token as a bearer token."""

_STRING_FIELDS: Final = (
    "access_token",
    "refresh_token",
    "client_secret",
    "token_endpoint",
    "client_id",
    "scope",
    "expires_at",
)
_MAX_INPUT: Final = 65536


HELP: Final = "set, list and delete the credentials of sources; rotate the key"
DESCRIPTION: Final = (
    "Credentials are stored encrypted with MEMINI_CREDENTIAL_KEY and are never printed."
)


def configure_parser(group: argparse.ArgumentParser) -> None:
    """Add the commands of ``memini credentials`` to its parser, ``group``."""
    subcommands = group.add_subparsers(metavar="<command>", required=True)

    set_parser = subcommands.add_parser(
        "set",
        help="store a source's credential, read from stdin",
        description="Store the credential of a source, replacing the one it has. An API key is"
        " read from stdin, without echo on a terminal. OAuth tokens are a JSON object on stdin"
        f" with the fields {', '.join(OAUTH_FIELDS)}; access_token is required.",
    )
    _source_arguments(set_parser)
    set_parser.add_argument("--kind", required=True, choices=("api_key", "oauth"))
    set_parser.set_defaults(command=set_command)

    list_parser = subcommands.add_parser("list", help="list credentials, without secrets")
    list_parser.add_argument(
        "--workspace", metavar="NAME", type=parse_workspace_name, help="only this workspace"
    )
    list_parser.set_defaults(command=list_command)

    delete_parser = subcommands.add_parser("delete", help="delete a source's credential")
    _source_arguments(delete_parser)
    delete_parser.set_defaults(command=delete_command)

    rotate = subcommands.add_parser(
        "rotate-key",
        help="re-encrypt every credential under MEMINI_CREDENTIAL_KEY",
        description="Re-encrypt every credential that is not under MEMINI_CREDENTIAL_KEY, reading"
        " it with a key of MEMINI_CREDENTIAL_OLD_KEYS, in batches that commit one by one. Safe to"
        " run again; exits 1 while credentials are left under another key.",
    )
    rotate.add_argument(
        "--batch-size",
        metavar="N",
        type=_positive,
        default=DEFAULT_ROTATION_BATCH,
        help=f"credentials per transaction (default: {DEFAULT_ROTATION_BATCH})",
    )
    rotate.set_defaults(command=rotate_key_command)


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        metavar="NAME",
        required=True,
        type=parse_workspace_name,
        help="its workspace",
    )
    parser.add_argument(
        "--source", metavar="NAME", required=True, type=_source_name, help="the source"
    )


def _source_name(value: str) -> str:
    try:
        return check_handle("source", value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= 100000:
        raise argparse.ArgumentTypeError("a whole number from 1 to 100000")
    return number


def _keyring(settings: Settings) -> Keyring:
    keyring = Keyring.from_settings(settings)
    if not keyring.configured:
        raise CommandError(str(CredentialKeyNotConfiguredError()), EXIT_USAGE)
    return keyring


# --- set -----------------------------------------------------------------------------------------


def _read_stdin(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    text = sys.stdin.read(_MAX_INPUT + 1)
    if len(text) > _MAX_INPUT:
        raise CommandError(f"stdin holds more than {_MAX_INPUT} characters", EXIT_USAGE)
    return text


def _oauth_input(text: str, now: datetime) -> tuple[OAuthTokens, OAuthClient]:
    """Tokens and client from the JSON object on stdin. Errors name fields, never values."""
    try:
        loaded: object = json.loads(text)
    except ValueError:
        raise CommandError("stdin is not a JSON object", EXIT_USAGE) from None
    if not isinstance(loaded, dict):
        raise CommandError("stdin is not a JSON object", EXIT_USAGE)
    data = cast(dict[str, Any], loaded)
    unknown = [name for name in data if name not in OAUTH_FIELDS]
    if unknown:  # not named: a misplaced token could be a key
        raise CommandError(
            f"stdin holds {len(unknown)} unknown fields; the fields are {', '.join(OAUTH_FIELDS)}",
            EXIT_USAGE,
        )
    for name in _STRING_FIELDS:
        if name in data and data[name] is not None and not isinstance(data[name], str):
            raise CommandError(f"{name}: must be a string", EXIT_USAGE)
    if "access_token" not in data or data["access_token"] is None:
        raise CommandError("access_token: required", EXIT_USAGE)
    tokens = OAuthTokens(
        access_token=SecretStr(data["access_token"]),
        refresh_token=_secret(data, "refresh_token"),
        expires_at=_expires_at(data, now),
    )
    client = OAuthClient(
        token_endpoint=data.get("token_endpoint"),
        client_id=data.get("client_id"),
        client_secret=_secret(data, "client_secret"),
        scope=data.get("scope"),
    )
    return tokens, client


def _secret(data: Mapping[str, Any], name: str) -> SecretStr | None:
    value = data.get(name)
    return None if value is None else SecretStr(value)


def _expires_at(data: Mapping[str, Any], now: datetime) -> datetime | None:
    expires_in, expires_at = data.get("expires_in"), data.get("expires_at")
    if expires_in is not None and expires_at is not None:
        raise CommandError("expires_in, expires_at: give one of them", EXIT_USAGE)
    if expires_in is not None:
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in < 0:
            raise CommandError("expires_in: must be a whole number of seconds", EXIT_USAGE)
        try:
            return now + timedelta(seconds=expires_in)
        except OverflowError:
            raise CommandError("expires_in: too large", EXIT_USAGE) from None
    if expires_at is not None:
        try:
            moment = datetime.fromisoformat(cast(str, expires_at))
        except ValueError:
            raise CommandError("expires_at: must be an ISO 8601 time", EXIT_USAGE) from None
        if moment.utcoffset() is None:
            raise CommandError("expires_at: must carry a time zone", EXIT_USAGE)
        return moment
    return None


async def set_command(args: argparse.Namespace, settings: Settings) -> int:
    workspace: str = args.workspace
    source_name: str = args.source
    kind: str = args.kind
    keyring = _keyring(settings)
    try:
        if kind == "api_key":
            api_key = SecretStr(_read_stdin("API key: ").rstrip("\r\n"))

            async def put(conn: Conn, source_id: UUID) -> CredentialInfo:
                return await put_api_key(conn, keyring, source_id=source_id, api_key=api_key)

        else:
            tokens, client = _oauth_input(_read_stdin("OAuth token JSON: "), datetime.now(UTC))

            async def put(conn: Conn, source_id: UUID) -> CredentialInfo:
                return await put_oauth(
                    conn, keyring, source_id=source_id, tokens=tokens, client=client
                )

        async with principal_session(settings) as (conn, owner_id):
            source = await resolve_source(
                conn, WriteAccess.for_owner(owner_id), workspace, source_name
            )
            info = await put(conn, source.id)
    except InvalidCredentialError as error:
        raise CommandError(str(error), EXIT_USAGE) from None
    except NotFoundError as error:
        raise CommandError(_not_found(error, workspace, source_name)) from None
    except CredentialError as error:
        raise CommandError(str(error)) from None
    print(
        f"stored the {info.kind} credential of source {info.source} in workspace {info.workspace}"
        f" (generation {info.generation}, key {info.key_id})"
    )
    return EXIT_OK


def _not_found(error: NotFoundError, workspace: str, source: str) -> str:
    if error.what == "workspace":
        return f"no workspace named {workspace}; see `memini workspace list`"
    return f"no source named {source} in workspace {workspace}"


# --- list, delete --------------------------------------------------------------------------------


async def list_command(args: argparse.Namespace, settings: Settings) -> int:
    workspace: str | None = args.workspace
    async with principal_session(settings) as (conn, owner_id):
        workspace_id = None
        if workspace is not None:
            owned = await list_owned_workspaces(conn, owner_id=owner_id)
            matches = [w.id for w in owned if w.name == workspace]
            if not matches:
                raise CommandError(f"no workspace named {workspace}; see `memini workspace list`")
            workspace_id = matches[0]
        found = await list_credentials(conn, workspace_id=workspace_id)
    if not found:
        message("no credentials yet; store one with `memini credentials set`")
        return EXIT_OK
    print_table(
        ("WORKSPACE", "SOURCE", "KIND", "GENERATION", "KEY", "EXPIRES", "UPDATED", "REFRESHED"),
        (
            (
                c.workspace,
                c.source,
                c.kind,
                str(c.generation),
                c.key_id,
                format_time(c.expires_at, "-"),
                format_time(c.updated_at),
                format_time(c.refreshed_at),
            )
            for c in found
        ),
    )
    return EXIT_OK


async def delete_command(args: argparse.Namespace, settings: Settings) -> int:
    workspace: str = args.workspace
    source_name: str = args.source
    try:
        async with principal_session(settings) as (conn, owner_id):
            source = await resolve_source(
                conn, WriteAccess.for_owner(owner_id), workspace, source_name
            )
            deleted = await delete_credential(conn, source_id=source.id)
    except NotFoundError as error:
        raise CommandError(_not_found(error, workspace, source_name)) from None
    if not deleted:
        raise CommandError(f"source {source_name} in workspace {workspace} has no credential")
    print(f"deleted the credential of source {source_name} in workspace {workspace}")
    return EXIT_OK


# --- rotate-key ----------------------------------------------------------------------------------


async def rotate_key_command(args: argparse.Namespace, settings: Settings) -> int:
    batch_size: int = args.batch_size
    keyring = _keyring(settings)
    async with connect(settings.database_url.get_secret_value(), autocommit=True) as conn:
        await require_migrated(conn, "run `memini migrate` first")
        try:
            result = await rotate_key(conn, keyring, batch_size=batch_size)
        except CredentialError as error:
            raise CommandError(str(error)) from None
    print(
        f"re-encrypted {result.rotated} credentials under key {result.key_id};"
        f" {result.already} were under it already"
    )
    if result.unreadable:
        ids = ", ".join(str(credential_id) for credential_id in result.unreadable)
        message(
            f"{len(result.unreadable)} credentials cannot be decrypted and stay as they are"
            f" (altered, or moved from another row): {ids}; set them again"
        )
    left = result.remaining - len(result.unreadable)
    if left > 0:
        message(
            f"{left} credentials were written under another key while the rotation ran; is every"
            " process using the new MEMINI_CREDENTIAL_KEY? Run rotate-key again"
        )
    if not result.complete:
        return EXIT_FAILURE
    message("every credential is under the current key; MEMINI_CREDENTIAL_OLD_KEYS can be removed")
    return EXIT_OK
