"""The ``memini`` command: start the service, migrate the database, manage workspaces and tokens.

Usage::

    memini [--config FILE] [--debug] <command> ...
    memini --version

    memini serve [--migrate]
    memini migrate
    memini workspace create <name>
    memini workspace list
    memini workspace llm <name> [none|local|cloud]
    memini token create --scope {read,ingest,admin} [--scope ...] [--workspace <name>]
                        [--name <label>] [--expires-in <duration>]
    memini token list
    memini token revoke <id-or-prefix>
    memini reindex --workspace <name>
    memini eval [--suite PATH] [--baseline PATH [--write-baseline]] [--margin M] [--k 1,5,10]
                [--modes MODE,...] [--fake-embeddings] [--lexical NAME] [--reranker NAME]
                [--json PATH] [--keep]
    memini credentials set --workspace <name> --source <name> --kind {api_key,oauth}  < secret
    memini credentials list [--workspace <name>]
    memini credentials delete --workspace <name> --source <name>
    memini credentials rotate-key [--batch-size N]

Global options go before the command. ``--config FILE`` sets ``MEMINI_CONFIG_FILE`` for this run;
every other setting comes from the environment and that file, as for the service
(:mod:`memini.config`). ``--debug`` adds the traceback to an error.

The commands work on the database directly, not through the HTTP API: the first token cannot come
from an API that asks for one. They act as the principal named by the ``principal`` setting
(``MEMINI_PRINCIPAL``, default ``owner``), which is created on first use. v1 has one user, and that
principal owns every workspace and token the CLI creates.

Results go to stdout, messages to stderr. ``token create`` prints the token alone on stdout, so
``TOKEN=$(memini token create --scope read)`` captures it. It is shown this once: only its digest
is stored.

An error is one line on stderr, ``memini: ...``, without a traceback. No output holds the database
URL or its password; the server is named as ``host:port``.

``workspace llm`` shows a workspace's ``llm_policy`` and the LLM providers the settings configure,
or sets it: ``none`` (no LLM reads anything in it), ``local`` (the default) or ``cloud``, the
workspace's opt-in to the cloud provider for sources whose own ``llm_policy`` is ``cloud``
(:mod:`memini.llm`). Only this command sets it; the HTTP API cannot.

``reindex`` rebuilds a workspace's index from raw records under the configured embedding model and
the chunker of this build (:func:`memini.index.reindex`); it is what a refused search names. It
needs an embedding provider (``MEMINI_EMBEDDING_PROVIDER``).

``eval`` runs the retrieval eval (:mod:`memini.cli.evaluate`, :mod:`memini.evaluation`): it indexes
the suite's fixture repositories into a scratch database it creates on the configured server and
drops afterwards, prints recall@k and MRR per mode, kind and language, and compares them with a
baseline report. It never reads or writes the configured database itself; the role needs CREATEDB.

``credentials`` stores the API keys and OAuth tokens of sources, encrypted with
``MEMINI_CREDENTIAL_KEY``, and re-encrypts them under a new key (:mod:`memini.cli.credentials`).
A secret is read from stdin, never from an argument, and never printed. ``rotate-key`` exits 1
while credentials are left under another key; without ``MEMINI_CREDENTIAL_KEY`` the commands that
need it exit 2.

Exit codes:

- 0: done; also ``serve`` stopped by SIGINT or SIGTERM.
- 1: the command failed: no such workspace or token, a name already taken, a migration that failed
  or is pending, a server that did not start; for ``reindex`` also a rebuild of the workspace that
  is running already, raw content that cannot be read, an embedding model that failed, or an index
  built with another fingerprint.
  For ``eval``: a search that failed, or an expected result of the suite not in the index.
- 2: wrong usage or invalid configuration, including no embedding provider for ``reindex`` and
  ``eval`` (without ``--fake-embeddings``), and a suite or baseline file of ``eval`` that is not
  valid.
- 3: the database cannot be reached.
- 4: ``eval`` only: a tracked metric fell below the baseline by more than the margin.
- 130: interrupted by Ctrl-C.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import psycopg

from memini import __version__
from memini.auth import SCOPES
from memini.cli import admin, credentials, evaluate, index, serve
from memini.cli.common import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    CommandError,
    DatabaseUrl,
    message,
)
from memini.config import CONFIG_FILE_VAR, ConfigError, Settings, load_settings
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
from memini.llm import LLM_POLICIES

__all__ = ["build_parser", "main"]

type Command = Callable[[argparse.Namespace, Settings], Coroutine[Any, Any, int]]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``memini`` command with ``argv`` (default ``sys.argv[1:]``); return the exit code."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:  # --help, --version, or a usage error argparse has printed
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    command: Command = args.command
    debug: bool = args.debug

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memini",
        description="Memini: a self-hosted knowledge service. Settings come from MEMINI_*"
        " environment variables and an optional TOML file.",
    )
    parser.add_argument("--version", action="version", version=f"memini {__version__}")
    parser.add_argument(
        "--config",
        metavar="FILE",
        help=f"a TOML settings file; sets {CONFIG_FILE_VAR} for this run",
    )
    parser.add_argument(
        "--debug", action="store_true", help="show the traceback when a command fails"
    )
    commands = parser.add_subparsers(title="commands", metavar="<command>", required=True)

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

    workspace = commands.add_parser("workspace", help="create and list workspaces")
    workspace_commands = workspace.add_subparsers(metavar="<command>", required=True)
    create_workspace = workspace_commands.add_parser("create", help="create a workspace")
    create_workspace.add_argument(
        "name",
        type=admin.parse_workspace_name,
        help="1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit",
    )
    create_workspace.set_defaults(command=admin.create_workspace_command)
    list_workspaces = workspace_commands.add_parser("list", help="list workspaces")
    list_workspaces.set_defaults(command=admin.list_workspaces_command)
    workspace_llm = workspace_commands.add_parser(
        "llm",
        help="show or set which LLM may read a workspace's content",
        description="Without a policy, show the workspace's llm_policy and the configured"
        " providers. With one, set it: none (no LLM), local (the local provider only; the"
        " default) or cloud (opt in to the cloud provider for sources whose llm_policy is"
        " cloud). A source's own llm_policy can narrow it, never widen it.",
    )
    workspace_llm.add_argument(
        "name",
        type=admin.parse_workspace_name,
        help="the workspace",
    )
    workspace_llm.add_argument(
        "policy",
        nargs="?",
        choices=LLM_POLICIES,
        help="none, local or cloud; leave out to show the current one",
    )
    workspace_llm.set_defaults(command=admin.workspace_llm_command)

    token = commands.add_parser("token", help="create, list and revoke bearer tokens")
    token_commands = token.add_subparsers(metavar="<command>", required=True)
    create_token = token_commands.add_parser(
        "create",
        help="create a token and print it once",
        description="Create a bearer token. The token alone goes to stdout, and is shown only"
        " this once; details go to stderr.",
    )
    create_token.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=SCOPES,
        help="what the token may do; repeat for more than one",
    )
    create_token.add_argument(
        "--workspace",
        metavar="NAME",
        type=admin.parse_workspace_name,
        help="limit the token to this workspace (default: every workspace)",
    )
    create_token.add_argument(
        "--name",
        metavar="LABEL",
        type=admin.parse_token_name,
        default=admin.DEFAULT_TOKEN_NAME,
        help=f"what the token is for, e.g. 'laptop agent' (default: {admin.DEFAULT_TOKEN_NAME})",
    )
    create_token.add_argument(
        "--expires-in",
        metavar="DURATION",
        type=admin.parse_duration,
        help="e.g. 90d, 12h or 1w2d (units: s, m, h, d, w); default: never expires",
    )
    create_token.set_defaults(command=admin.create_token_command)
    list_tokens = token_commands.add_parser("list", help="list tokens, without secrets")
    list_tokens.set_defaults(command=admin.list_tokens_command)
    revoke_token = token_commands.add_parser("revoke", help="revoke a token")
    revoke_token.add_argument(
        "token", metavar="id-or-prefix", help="the token's id or prefix, as `token list` shows"
    )
    revoke_token.set_defaults(command=admin.revoke_token_command)

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
        type=admin.parse_workspace_name,
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

    return parser
