"""Structured logs: the JSON shape, context fields, routing and redaction. Every value is fake."""

import ast
import inspect
import io
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

import memini.logs
from memini.api.request_id import request_id_var
from memini.logs import (
    REDACTED,
    ROUTED_LOGGERS,
    configure_logging,
    current_run_id,
    redact,
    register_secret,
    register_url_secrets,
    run_id_var,
)

LOGGER = "memini.test"

# Obviously fake secrets; each is distinct, so a leak names its case.
CANARY_MEMINI = "memini_FAKEtokenFORtestsONLY0000"
CANARY_IN_URL = "fake-url-password-0001"
CANARY_BEARER = "fake-bearer-credential-0002"
CANARY_BASIC = "ZmFrZTpmYWtlLWJhc2ljLTAwMDM="
CANARY_REGISTERED = "fake-registered-secret-0004"
CANARY_IN_EXCEPTION = "fake-exception-password-0005"
CANARY_CONNINFO = "fake-conninfo-password-0006"
CANARY_IN_EXTRA = "fake-extra-password-0007"
CANARY_SECRETSTR = "fake-secretstr-value-0008"
CANARY_API_KEY = "fake-api-key-0009"


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    """Each line of `stream` parsed as one JSON object."""
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def log() -> logging.Logger:
    return logging.getLogger(LOGGER)


# --- Shape ---------------------------------------------------------------------------------------


def test_a_line_is_one_json_object_with_time_level_logger_and_message(
    log_output: io.StringIO,
) -> None:
    before = datetime.now(UTC)
    log().info("stored %d records of %s", 3, "chat")

    (entry,) = records(log_output)
    assert set(entry) == {"timestamp", "level", "logger", "message"}
    assert entry["level"] == "INFO"
    assert entry["logger"] == LOGGER
    assert entry["message"] == "stored 3 records of chat"
    assert entry["timestamp"].endswith("Z")
    moment = datetime.fromisoformat(entry["timestamp"])
    assert moment.utcoffset() is not None
    assert moment.utcoffset() == UTC.utcoffset(None)
    assert before.replace(microsecond=0) <= moment <= datetime.now(UTC)


def test_line_breaks_stay_inside_one_line(log_output: io.StringIO) -> None:
    log().warning("first\nsecond\r\nthird")

    lines = log_output.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "first\nsecond\r\nthird"


def test_request_id_and_run_id_appear_while_set(log_output: io.StringIO) -> None:
    run_id = uuid.uuid4()
    request_token = request_id_var.set("req-0001")
    run_token = run_id_var.set(run_id)
    try:
        assert current_run_id() == run_id
        log().info("inside")
    finally:
        run_id_var.reset(run_token)
        request_id_var.reset(request_token)
    log().info("outside")

    inside, outside = records(log_output)
    assert inside["request_id"] == "req-0001"
    assert inside["run_id"] == str(run_id)
    assert "request_id" not in outside
    assert "run_id" not in outside
    assert current_run_id() is None


def test_extra_fields_are_kept_under_extra(log_output: io.StringIO) -> None:
    ident = uuid.UUID("00000000-0000-4000-8000-000000000001")
    log().info(
        "synced",
        extra={
            "records": 12,
            "ratio": 0.5,
            "complete": True,
            "missing": None,
            "ids": (ident,),
            "nested": {"stream": "general", "lag_s": 4},
            "not_a_number": float("nan"),
            "color_message": "dropped: uvicorn sets it",
        },
    )

    (entry,) = records(log_output)
    assert entry["extra"] == {
        "records": 12,
        "ratio": 0.5,
        "complete": True,
        "missing": None,
        "ids": [str(ident)],
        "nested": {"stream": "general", "lag_s": 4},
        "not_a_number": "nan",
    }


def test_an_exception_is_a_structured_field(log_output: io.StringIO) -> None:
    try:
        raise LookupError("no such stream")
    except LookupError:
        log().exception("sync failed")

    (entry,) = records(log_output)
    assert entry["message"] == "sync failed"
    assert entry["level"] == "ERROR"
    exception = entry["exception"]
    assert exception["type"] == "LookupError"
    assert exception["message"] == "no such stream"
    assert exception["traceback"].startswith("Traceback (most recent call last):")
    assert exception["traceback"].endswith("LookupError: no such stream")


def test_the_exception_type_names_its_module(log_output: io.StringIO) -> None:
    try:
        json.loads("{")
    except json.JSONDecodeError:
        log().exception("bad")

    (entry,) = records(log_output)
    assert entry["exception"]["type"] == "json.decoder.JSONDecodeError"


def test_stack_info_is_a_field(log_output: io.StringIO) -> None:
    log().info("where", stack_info=True)

    (entry,) = records(log_output)
    assert entry["stack"].startswith("Stack (most recent call last):")


def test_the_text_mode_writes_a_line_for_people(log_output: io.StringIO) -> None:
    configure_logging("INFO", json=False, stream=log_output)
    token = request_id_var.set("req-0002")
    try:
        log().info("stored %d", 5, extra={"stream": "general"})
    finally:
        request_id_var.reset(token)

    (line,) = log_output.getvalue().splitlines()
    assert line.endswith(' INFO     memini.test: stored 5 request_id="req-0002" stream="general"')


# --- Setup ---------------------------------------------------------------------------------------


def our_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler.formatter, memini.logs.JsonFormatter | memini.logs.TextFormatter)
    ]


def test_configure_logging_is_idempotent(log_output: io.StringIO) -> None:
    configure_logging("DEBUG", stream=log_output)
    configure_logging("DEBUG", stream=log_output)

    log().info("once")

    assert len(our_handlers()) == 1
    assert len(records(log_output)) == 1


def test_a_second_call_replaces_level_format_and_stream(log_output: io.StringIO) -> None:
    other = io.StringIO()
    configure_logging("WARNING", json=False, stream=other)

    log().info("below the level")
    log().warning("written")

    assert log_output.getvalue() == ""
    (line,) = other.getvalue().splitlines()
    assert "WARNING  memini.test: written" in line
    assert len(our_handlers()) == 1


def test_library_loggers_write_through_the_one_handler(log_output: io.StringIO) -> None:
    stray = io.StringIO()
    for name in ("uvicorn.error", "uvicorn.access", "psycopg.pool"):
        logger = logging.getLogger(name)
        logger.addHandler(logging.StreamHandler(stray))
        logger.propagate = False

    configure_logging("DEBUG", stream=log_output)
    logging.getLogger("uvicorn.error").info("server started")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/healthz", "1.1", 200
    )
    logging.getLogger("psycopg.pool").warning("pool is short of connections")

    assert stray.getvalue() == ""
    assert [entry["logger"] for entry in records(log_output)] == [
        "uvicorn.error",
        "uvicorn.access",
        "psycopg.pool",
    ]
    for name in ("uvicorn.error", "uvicorn.access", "psycopg.pool"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate


def pool_lends_a_connection() -> None:
    """What psycopg.pool logs for one connection it lends, as psycopg_pool does."""
    pool = logging.getLogger("psycopg.pool")
    pool.info("connection requested from %r", "pool-1")
    pool.info("connection given by %r", "pool-1")
    pool.info("returning connection to %r", "pool-1")
    pool.warning("discarding closed connection: %s", "<fake connection>")


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_the_pool_logs_warnings_only_above_debug(log_output: io.StringIO, level: str) -> None:
    configure_logging(level, stream=log_output)

    pool_lends_a_connection()
    log().info("own info")

    entries = [(entry["logger"], entry["level"]) for entry in records(log_output)]
    expected = [("psycopg.pool", "WARNING")]
    if level == "INFO":
        expected.append((LOGGER, "INFO"))
    assert entries == expected
    pool = logging.getLogger("psycopg.pool")
    assert pool.handlers == []
    assert pool.propagate


def test_the_pool_logs_nothing_below_a_higher_level(log_output: io.StringIO) -> None:
    configure_logging("ERROR", stream=log_output)

    pool_lends_a_connection()
    logging.getLogger("psycopg.pool").error("pool is broken")

    assert [(entry["logger"], entry["level"]) for entry in records(log_output)] == [
        ("psycopg.pool", "ERROR")
    ]


def test_the_pool_logs_everything_at_debug(log_output: io.StringIO) -> None:
    configure_logging("WARNING", stream=log_output)
    configure_logging("DEBUG", stream=log_output)

    pool_lends_a_connection()
    logging.getLogger("psycopg.pool").debug("pool closed")

    assert [entry["level"] for entry in records(log_output)] == [
        "INFO",
        "INFO",
        "INFO",
        "WARNING",
        "DEBUG",
    ]


def test_quiet_loggers_are_routed() -> None:
    assert set(memini.logs.QUIET_LOGGERS) <= set(ROUTED_LOGGERS)


def test_an_unknown_level_is_refused(log_output: io.StringIO) -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("LOUD", stream=log_output)


def test_a_lost_record_is_reported_without_its_content(
    log_output: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken(io.StringIO):
        def write(self, s: str, /) -> int:
            raise OSError("disk full")

    configure_logging("INFO", stream=Broken())
    log().info("carrying %s", CANARY_MEMINI)

    err = capsys.readouterr().err
    assert "memini: a log record from memini.test was lost (OSError)" in err
    assert CANARY_MEMINI not in err
    assert "carrying" not in err


def test_the_logging_module_never_unwraps_a_secret() -> None:
    tree = ast.parse(inspect.getsource(memini.logs))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ("get_secret_value", "_secret_value")
    ]
    assert calls == []


# --- Redaction -----------------------------------------------------------------------------------


def raise_and_log(message: str) -> None:
    try:
        raise RuntimeError(message)
    except RuntimeError:
        log().exception("the job failed")


# (case, how it is logged, the secret that must not appear)
LEAKS: list[tuple[str, Callable[[], object], str]] = [
    ("token in message", lambda: log().info(f"got {CANARY_MEMINI}"), CANARY_MEMINI),
    ("token in args", lambda: log().info("got %s", CANARY_MEMINI), CANARY_MEMINI),
    ("token in extra", lambda: log().info("got", extra={"header": CANARY_MEMINI}), CANARY_MEMINI),
    (
        "url password",
        lambda: log().info("dsn postgresql://memini:%s@db.invalid:5432/x", CANARY_IN_URL),
        CANARY_IN_URL,
    ),
    (
        "url password in nested extra",
        lambda: log().info(
            "dsn",
            extra={"target": {"urls": [f"https://someone:{CANARY_IN_URL}@git.invalid/r"]}},
        ),
        CANARY_IN_URL,
    ),
    (
        "authorization header, bearer",
        lambda: log().info("headers: {'Authorization': 'Bearer %s'}", CANARY_BEARER),
        CANARY_BEARER,
    ),
    (
        "authorization header, basic",
        lambda: log().info("Authorization: Basic %s", CANARY_BASIC),
        CANARY_BASIC,
    ),
    (
        "raw asgi headers",
        lambda: log().debug("scope headers %s", [(b"authorization", f"Bearer {CANARY_BEARER}")]),
        CANARY_BEARER,
    ),
    (
        "registered secret",
        lambda: (register_secret(CANARY_REGISTERED), log().info("value %s", CANARY_REGISTERED)),
        CANARY_REGISTERED,
    ),
    (
        "exception message and traceback",
        lambda: raise_and_log(f"cannot connect: password={CANARY_IN_EXCEPTION}"),
        CANARY_IN_EXCEPTION,
    ),
    (
        "exception carrying a url",
        lambda: raise_and_log(f"postgresql://memini:{CANARY_IN_URL}@db.invalid/x"),
        CANARY_IN_URL,
    ),
    (
        "libpq connection string",
        lambda: log().info("conninfo host=db.invalid password=%s", CANARY_CONNINFO),
        CANARY_CONNINFO,
    ),
    (
        "extra keyed as a secret",
        lambda: log().info("login", extra={"db_password": CANARY_IN_EXTRA}),
        CANARY_IN_EXTRA,
    ),
    (
        "nested extra keyed as a secret",
        lambda: log().info("login", extra={"provider": {"api_key": CANARY_API_KEY}}),
        CANARY_API_KEY,
    ),
    (
        "secretstr in extra",
        lambda: log().info("cfg", extra={"database_url": SecretStr(CANARY_SECRETSTR)}),
        CANARY_SECRETSTR,
    ),
    (
        "secretstr in args",
        lambda: log().info("cfg %s %r", SecretStr(CANARY_SECRETSTR), SecretStr(CANARY_SECRETSTR)),
        CANARY_SECRETSTR,
    ),
    (
        "api key header",
        lambda: log().info("sent X-Api-Key: %s", CANARY_API_KEY),
        CANARY_API_KEY,
    ),
]


@pytest.mark.parametrize("json_lines", [True, False], ids=["json", "text"])
@pytest.mark.parametrize(
    ("emit", "secret"), [(e, s) for _, e, s in LEAKS], ids=[c for c, _, _ in LEAKS]
)
def test_a_secret_never_reaches_a_line(
    log_output: io.StringIO, json_lines: bool, emit: Callable[[], object], secret: str
) -> None:
    configure_logging("DEBUG", json=json_lines, stream=log_output)

    emit()

    text = log_output.getvalue()
    assert text
    assert secret not in text
    assert REDACTED in text or "**********" in text
    if json_lines:
        records(log_output)  # still valid JSON


def test_a_registered_url_password_is_masked_wherever_it_appears(log_output: io.StringIO) -> None:
    url = f"postgresql://memini:{CANARY_IN_URL}%40x@db.invalid/memini"

    assert register_url_secrets(url)
    log().info("the password alone: %s and decoded: %s@x", f"{CANARY_IN_URL}%40x", CANARY_IN_URL)

    assert CANARY_IN_URL not in log_output.getvalue()


def test_short_or_missing_secrets_are_not_registered(log_output: io.StringIO) -> None:
    assert not register_secret("short")
    assert not register_url_secrets("postgresql://memini@db.invalid/memini")
    assert not register_url_secrets("not a url")

    log().info("short words stay")

    assert records(log_output)[0]["message"] == "short words stay"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"use {CANARY_MEMINI} now", f"use {REDACTED} now"),
        (
            f"postgresql://memini:{CANARY_IN_URL}@db.invalid:5432/memini",
            f"postgresql://memini:{REDACTED}@db.invalid:5432/memini",
        ),
        # An unencoded "@" or "/" in the password leaves no part of it behind.
        (
            "https://someone:fa/ke@pass@git.invalid/repo",
            f"https://someone:{REDACTED}@git.invalid/repo",
        ),
        ("redis://:fake-only-password@cache.invalid", f"redis://:{REDACTED}@cache.invalid"),
        (f"Authorization: Bearer {CANARY_BEARER}", f"Authorization: Bearer {REDACTED}"),
        (f"authorization={CANARY_BEARER}", f"authorization={REDACTED}"),
        (f"sent Bearer {CANARY_BEARER}", f"sent Bearer {REDACTED}"),
        (
            f"user=memini password={CANARY_CONNINFO} host=x",
            f"user=memini password={REDACTED} host=x",
        ),
        (f'{{"client_secret": "{CANARY_API_KEY}"}}', f'{{"client_secret": "{REDACTED}"}}'),
        # Ordinary text is left alone.
        ("missing bearer token", "missing bearer token"),
        ("invalid token: expired", "invalid token: expired"),
        ("http://127.0.0.1:6364/healthz", "http://127.0.0.1:6364/healthz"),
        ("postgresql://memini@db.invalid/memini", "postgresql://memini@db.invalid/memini"),
        ("memini_test_0123456789ab", "memini_test_0123456789ab"),
        ("MEMINI_DATABASE_URL_FILE is set", "MEMINI_DATABASE_URL_FILE is set"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert redact(text) == expected
