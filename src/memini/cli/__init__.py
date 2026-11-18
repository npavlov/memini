"""The ``memini`` command: a client of the running service, and the operator commands below it.

Usage::

    memini [--url URL] [--token-file FILE] [--config FILE] [--debug] <command> ...
    memini --version

Client commands are HTTP clients of the running service (requirements, section 7): they need its
URL and a token, and nothing else. The service checks scopes and workspace limits, touches the
mirrors and writes run reports; no database credentials leave it::

    memini workspace create <name> | list
    memini source add <workspace> <name> --location URL_OR_PATH --ref REF [--ref ...]
                      [--schedule DURATION] [--include GLOB ...] [--exclude GLOB ...]
                      [--no-builtin-excludes]
    memini source add <workspace> <name> --kind push [--allow-channel GLOB ...]
                      [--allow-domain DOMAIN ...] [--exclude-author GLOB ...]
                      [--exclude-title GLOB ...] [--exclude-automated]
    memini source list <workspace> | show|edit|remove|prune <workspace> <source>
    memini streams <workspace> <source>
    memini sync <workspace> <source> [--stream KEY ...] [--wait]
    memini runs <workspace> [--source NAME] [--stream KEY] [--limit N] [--before CURSOR]
    memini run <run-id>
    memini status
    memini token create --scope {read,ingest,admin} [--scope ...] [--workspace NAME]
                        [--name LABEL] [--expires-in DURATION]
    memini token list | revoke <id-or-prefix>
    memini search <query> [--code] [--mode MODE] [--workspace NAME ...] ...
    memini forget <workspace> (--record ID | --source NAME --external-id ID [--kind KIND])

They read ``MEMINI_URL`` (default ``http://127.0.0.1:6364``) and the token from ``MEMINI_TOKEN`` or
``MEMINI_TOKEN_FILE`` (:mod:`memini.cli.client`); ``--url`` and ``--token-file`` override them.
No option takes the token itself. Read commands take ``--json`` for the API's answer as it is.

Operator commands work on the database directly, with the service's settings, inside the service's
container (``docker compose exec memini memini <command>``): they run before a token exists, or
below the API::

    memini serve [--migrate]
    memini migrate
    memini bootstrap [--name LABEL] [--expires-in DURATION] [--force]
    memini workspace llm <name> [none|local|cloud]
    memini reindex --workspace <name>
    memini eval [--suite PATH] [--baseline PATH [--write-baseline]] [--margin M] [--k 1,5,10]
                [--modes MODE,...] [--fake-embeddings] [--lexical NAME] [--reranker NAME]
                [--json PATH] [--keep]
    memini credentials set --workspace <name> --source <name> --kind {api_key,oauth}  < secret
    memini credentials list [--workspace <name>]
    memini credentials delete --workspace <name> --source <name>
    memini credentials rotate-key [--batch-size N]

``--config FILE`` sets ``MEMINI_CONFIG_FILE`` for an operator command; every other setting comes
from the environment and that file, as for the service (:mod:`memini.config`). ``bootstrap``
creates the first admin token (:mod:`memini.cli.admin`).

``workspace llm`` is an operator command in the ``workspace`` group, whose other commands are
clients: it shows a workspace's ``llm_policy`` and the LLM providers the settings configure, or
sets it: ``none`` (no LLM reads anything in it), ``local`` (the default) or ``cloud``, the
workspace's opt-in to the cloud provider for sources whose own ``llm_policy`` is ``cloud``
(:mod:`memini.llm`). Only this command sets it; the HTTP API cannot, so no token can opt a
workspace in.

``reindex`` rebuilds a workspace's index from raw records under the configured embedding model and
the chunker of this build (:func:`memini.index.reindex`); it is what a refused search names, and
needs an embedding provider.

``eval`` runs the retrieval eval (:mod:`memini.cli.evaluate`, :mod:`memini.evaluation`): it indexes
the suite's fixture repositories into a scratch database it creates on the configured server and
drops afterwards, prints recall@k and MRR per mode, kind and language, and compares them with a
baseline report. It never reads or writes the configured database itself; the role needs CREATEDB.

``credentials`` stores the API keys and OAuth tokens of sources, encrypted with
``MEMINI_CREDENTIAL_KEY``, and re-encrypts them under a new key (:mod:`memini.cli.credentials`).
A secret is read from stdin, never from an argument, and never printed. ``rotate-key`` exits 1
while credentials are left under another key; without ``MEMINI_CREDENTIAL_KEY`` the commands that
need it exit 2.

Results go to stdout, messages to stderr. ``bootstrap`` and ``token create`` print the token alone
on stdout, so ``TOKEN=$(memini token create --scope read)`` captures it. It is shown this once:
only its digest is stored. No other output holds a token.

An error is one line on stderr, ``memini: ...``, without a traceback; ``--debug`` adds it. No output
holds the database URL, its password or the token; the database is named as ``host:port``.

Exit codes:

- 0: done; also ``serve`` stopped by SIGINT or SIGTERM.
- 1: the command failed: no such workspace, source, run or token, a name already taken, a request
  the service refused (an error of the API other than those below), a sync waited for that did not
  succeed, a migration that failed or is pending, a server that did not start; for ``reindex`` also
  a rebuild that is running already, raw content that cannot be read, an embedding model that
  failed, or an index built with another fingerprint; for ``eval`` a search that failed, or an
  expected result of the suite not in the index.
- 2: wrong usage or invalid configuration: arguments, the service's settings, the service URL, no
  token or a malformed one; also no embedding provider for ``reindex`` and ``eval`` (without
  ``--fake-embeddings``), and a suite or baseline file of ``eval`` that is not valid.
- 3: unreachable: the service (client commands) did not answer, or answered that it is unavailable;
  the database (operator commands) cannot be reached.
- 4: ``eval`` only: a tracked metric fell below the baseline by more than the margin.
- 5: client commands: the service refused the token: unknown, revoked or expired (401), or without
  the scope or the workspace the command needs (403).
- 130: interrupted by Ctrl-C.
"""

import argparse
import asyncio
import os
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import httpx2
import psycopg

from memini import __version__
from memini.cli import (
    admin,
    credentials,
    decisions,
    evaluate,
    forget,
    index,
    recipes,
    runs,
    search,
    serve,
    sources,
    status,
    tokens,
)
from memini.cli.arguments import (
    HELP_WIDTH,
    ClientCommand,
    Commands,
    OfflineCommand,
    OperatorCommand,
    Parser,
    parse_lifetime,
    parse_token_name,
    parse_workspace_name,
)
from memini.cli.client import (
    TOKEN_FILE_VAR,
    URL_VAR,
    Api,
    ClientConfigError,
    load_client_settings,
    quiet_http_logs,
)
from memini.cli.common import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    CommandError,
    DatabaseUrl,
    format_traceback,
    message,
    printable,
)
from memini.config import CONFIG_FILE_VAR, ConfigError, load_settings
from memini.db import MigrationError
from memini.embeddings import EmbeddingError, EmbeddingsNotConfiguredError
from memini.evaluation import (
    DEFAULT_KS,
    DEFAULT_LEXICAL,
    DEFAULT_RERANKER,
    LEXICAL_VARIANTS,
    RERANKERS,
)
from memini.index import IndexMismatchError

__all__ = ["build_parser", "main"]


def main(
    argv: Sequence[str] | None = None, *, transport: httpx2.BaseTransport | None = None
) -> int:
    """Run the ``memini`` command with ``argv`` (default ``sys.argv[1:]``); return the exit code.

    ``transport`` replaces the HTTP transport of client commands, for tests.
    """
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:  # --help, --version, or a usage error argparse has printed
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    client: ClientCommand | None = getattr(args, "client", None)
    if client is not None:
        return _run_client(args, client, transport)
    offline: OfflineCommand | None = getattr(args, "offline", None)
    if offline is not None:
        return _run_offline(args, offline)
    command: OperatorCommand = args.command
    return _run_operator(args, command)


def _run_offline(args: argparse.Namespace, command: OfflineCommand) -> int:
    if args.url is not None or args.token_file is not None or args.config is not None:
        message("--url, --token-file and --config are not used by an offline command")
        return EXIT_USAGE
    try:
        return asyncio.run(command(args))
    except KeyboardInterrupt:
        message("interrupted")
        return EXIT_INTERRUPTED


def _run_client(
    args: argparse.Namespace, command: ClientCommand, transport: httpx2.BaseTransport | None
) -> int:
    debug: bool = args.debug
    if args.config is not None:
        message(
            "--config is for operator commands; a client command reads"
            f" {URL_VAR} and the token from the environment"
        )
        return EXIT_USAGE
    try:
        settings = load_client_settings(os.environ, url=args.url, token_file=args.token_file)
    except ClientConfigError as error:  # its text names settings, never values
        message(str(error))
        return EXIT_USAGE
    quiet_http_logs()
    try:
        with Api(settings, transport=transport) as api:
            return command(args, api)
    except KeyboardInterrupt:
        message("interrupted")
        return EXIT_INTERRUPTED
    except CommandError as error:
        message(error.text)
        return error.exit_code
    except Exception as error:
        message(
            f"unexpected error: {type(error).__name__}: {printable(settings.redact(str(error)))}"
            " (run with --debug for the traceback)"
        )
        if debug:
            print(format_traceback(error, settings.redact), file=sys.stderr, end="", flush=True)
        return EXIT_FAILURE


def _run_operator(args: argparse.Namespace, command: OperatorCommand) -> int:
    debug: bool = args.debug
    if args.url is not None or args.token_file is not None:
        message(
            "--url and --token-file are for client commands; an operator command works on the"
            " database with the service's settings"
        )
        return EXIT_USAGE
    environ = dict(os.environ)
    if args.config is not None:
        environ[CONFIG_FILE_VAR] = args.config
    try:
        settings = load_settings(environ)
    except ConfigError as error:  # its text names keys, never values
        message(str(error))
        return EXIT_USAGE

    database = DatabaseUrl(settings)
    try:
        database.check()
        return asyncio.run(command(args, settings))
    except KeyboardInterrupt:
        message("interrupted")
        return EXIT_INTERRUPTED
    except Exception as error:
        code, text = _describe(error, database)
        message(text)
        if debug:
            print(database.format_traceback(error), file=sys.stderr, end="", flush=True)
        return code


def _describe(error: Exception, database: DatabaseUrl) -> tuple[int, str]:
    """Exit code and one line for an error; any text that is not the CLI's own is redacted."""
    match error:
        case CommandError():
            return error.exit_code, error.text
        case psycopg.OperationalError():  # also psycopg_pool.PoolTimeout
            return EXIT_UNAVAILABLE, database.unavailable(error)
        case MigrationError():
            return EXIT_FAILURE, f"migration failed: {database.redact(str(error))}"
        case EmbeddingsNotConfiguredError():  # names settings, never values
            return EXIT_USAGE, str(error)
        case EmbeddingError():  # names the provider, the model and the cause; never a key or text
            return EXIT_FAILURE, f"embedding failed: {database.redact(str(error))}"
        case IndexMismatchError():  # names the workspace, both fingerprints and `memini reindex`
            return EXIT_FAILURE, str(error)
        case psycopg.Error():
            primary = error.diag.message_primary or type(error).__name__
            return EXIT_FAILURE, f"database error: {database.redact(primary)}"
        case _:
            return EXIT_FAILURE, (
                f"unexpected error: {type(error).__name__}: {database.redact(str(error))}"
                " (run with --debug for the traceback)"
            )


EXIT_CODES = (
    "exit codes: 0 done; 1 failed; 2 wrong usage or configuration; 3 the service or the database"
    " cannot be reached; 4 eval: a metric fell below the baseline; 5 the service refused the"
    " token; 130 interrupted"
)
DESCRIPTION = (
    "Memini: a self-hosted knowledge service. Client commands call the HTTP API of a running"
    " service; operator commands work on its database, inside its container."
)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="memini",
        usage="memini [options] <command> ...",
        description=textwrap.fill(DESCRIPTION, HELP_WIDTH),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"memini {__version__}")
    parser.add_argument(
        "--url",
        metavar="URL",
        help=f"client commands: the service's URL; default: {URL_VAR}, or http://127.0.0.1:6364",
    )
    parser.add_argument(
        "--token-file",
        metavar="FILE",
        type=Path,
        help=f"client commands: a file that holds the token; default: {TOKEN_FILE_VAR} or"
        " MEMINI_TOKEN. No option takes the token itself.",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help=f"operator commands: a TOML settings file; sets {CONFIG_FILE_VAR} for this run",
    )
    parser.add_argument(
        "--debug", action="store_true", help="show the traceback when a command fails"
    )
    subparsers = parser.add_subparsers(
        title="commands",
        metavar="<command>",
        prog="memini",
        required=True,
        help=argparse.SUPPRESS,
    )
    commands = Commands(subparsers.add_parser)

    # --- Client commands: HTTP clients of the running service ------------------------------------
    sources.add_commands(commands)
    runs.add_commands(commands)
    status.add_commands(commands)
    tokens.add_commands(commands)
    search.add_commands(commands)
    decisions.add_commands(commands)
    forget.add_commands(commands)

    # --- Operator commands: on the database, inside the service's container ----------------------
    serve_parser = commands.add_parser(
        "serve",
        help="run the service until SIGINT or SIGTERM",
        description="Run the HTTP API on MEMINI_HTTP_HOST:MEMINI_HTTP_PORT. Refuses to start"
        " while migrations are pending.",
    )
    serve_parser.add_argument(
        "--migrate", action="store_true", help="apply pending migrations first"
    )
    serve_parser.set_defaults(command=serve.serve_command)

    migrate_parser = commands.add_parser(
        "migrate",
        help="apply pending database migrations",
        description="Apply pending migrations and print each one. Nothing to do is not an error.",
    )
    migrate_parser.set_defaults(command=admin.migrate_command)

    bootstrap = commands.add_parser(
        "bootstrap",
        help="create the first admin token and print it once",
        description="Create a token with the read and admin scopes for every workspace, for the"
        " principal MEMINI_PRINCIPAL names, and print it alone on stdout, this once. Refused while"
        " that principal has an active admin token for every workspace, unless --force.",
    )
    bootstrap.add_argument(
        "--name",
        metavar="LABEL",
        type=parse_token_name,
        default=admin.DEFAULT_BOOTSTRAP_NAME,
        help=f"what the token is for (default: {admin.DEFAULT_BOOTSTRAP_NAME})",
    )
    bootstrap.add_argument(
        "--expires-in",
        metavar="DURATION",
        type=parse_lifetime,
        help="e.g. 90d, 12h or 1w2d (units: s, m, h, d, w); default: never expires",
    )
    bootstrap.add_argument(
        "--force",
        action="store_true",
        help="create the token even though an active admin token exists",
    )
    bootstrap.set_defaults(command=admin.bootstrap_command)

    reindex = commands.add_parser(
        "reindex",
        help="rebuild a workspace's index from raw records",
        description="Rebuild the chunks, vectors and symbols of a workspace from its raw records"
        " with the configured embedding model and this build's chunker. Unchanged chunks are"
        " kept and not embedded again. Search in the workspace is refused until the rebuild"
        " finishes when the model or the chunker changed. Needs an embedding provider.",
    )
    reindex.add_argument(
        "--workspace",
        metavar="NAME",
        required=True,
        type=parse_workspace_name,
        help="the workspace whose index to rebuild",
    )
    reindex.set_defaults(command=index.reindex_command)

    evaluation = commands.add_parser(
        "eval",
        help="run the retrieval eval on the fixture repositories",
        description="Index the suite's fixture repositories into a scratch database on the"
        " configured Postgres server (created and dropped by the command; the role needs"
        " CREATEDB), run every question in every mode, print recall@k and MRR per kind and"
        " language, and compare with a baseline. Uses the configured embedding provider, or the"
        " FakeEmbedder with --fake-embeddings. Exit codes: 0 no regression, 1 a search failed or"
        " an expected result is not in the index, 2 an invalid suite or baseline,"
        f" {evaluate.EXIT_REGRESSION} a metric fell below the baseline by more than the margin.",
    )
    evaluation.add_argument(
        "--suite",
        metavar="PATH",
        type=Path,
        default=evaluate.DEFAULT_SUITE,
        help=f"the suite file (default: {evaluate.DEFAULT_SUITE}, from the repository root)",
    )
    evaluation.add_argument(
        "--baseline",
        metavar="PATH",
        type=Path,
        help="a report to compare with; with --write-baseline, where to write this run's",
    )
    evaluation.add_argument(
        "--write-baseline",
        action="store_true",
        help="write this run's report to --baseline instead of comparing with it",
    )
    evaluation.add_argument(
        "--margin",
        type=evaluate.parse_margin,
        default=evaluate.DEFAULT_MARGIN,
        help=f"how far a metric may fall below the baseline (default: {evaluate.DEFAULT_MARGIN})",
    )
    evaluation.add_argument(
        "--k",
        dest="ks",
        type=evaluate.parse_ks,
        default=DEFAULT_KS,
        metavar="K,...",
        help=f"recall cutoffs (default: {','.join(map(str, DEFAULT_KS))})",
    )
    evaluation.add_argument(
        "--modes",
        type=evaluate.parse_modes,
        metavar="MODE,...",
        help="search modes to measure, or all (default: every mode the suite uses; with"
        f" --fake-embeddings, {','.join(evaluate.FAKE_MODES)})",
    )
    evaluation.add_argument(
        "--fake-embeddings",
        action="store_true",
        help="use the deterministic FakeEmbedder instead of the configured model: dense and"
        " hybrid results then carry no meaning",
    )
    evaluation.add_argument(
        "--lexical",
        choices=sorted(LEXICAL_VARIANTS),
        default=DEFAULT_LEXICAL,
        help=f"the lexical retriever to measure (default: {DEFAULT_LEXICAL}, the service's)",
    )
    evaluation.add_argument(
        "--reranker",
        choices=sorted(RERANKERS),
        default=DEFAULT_RERANKER,
        help=f"the reranker to measure (default: {DEFAULT_RERANKER})",
    )
    evaluation.add_argument(
        "--json",
        metavar="PATH",
        type=Path,
        help="also write the report as JSON to PATH (- for stdout, instead of the table)",
    )
    evaluation.add_argument(
        "--keep",
        action="store_true",
        help="keep the scratch database and the temporary repositories, for inspection",
    )
    evaluation.set_defaults(command=evaluate.eval_command)
    credentials.configure_parser(
        commands.add_parser(
            "credentials", help=credentials.HELP, description=credentials.DESCRIPTION
        )
    )

    # --- Offline commands: files only, no service and no database ---------------------------------
    recipes.configure_parser(
        commands.add_offline("recipe", help=recipes.HELP, description=recipes.DESCRIPTION)
    )

    parser.epilog = f"{commands.listing()}\n\n{textwrap.fill(EXIT_CODES, HELP_WIDTH)}"
    return parser
