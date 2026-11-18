"""Job settings, keys and error text (memini.jobs). No database needed."""

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from memini.db import Pool
from memini.jobs import (
    SOURCE_LOCK_NAMESPACE,
    Job,
    RetryPolicy,
    Scheduler,
    Worker,
    describe_error,
    source_lock_key,
    stream_dedupe_key,
)
from memini.jobs.queue import check_kind

NO_POOL = cast(Pool, None)
"""The constructors only validate; nothing here connects."""


async def _noop(job: Job) -> None:
    pass


def test_backoff_grows_by_the_factor_up_to_the_cap() -> None:
    policy = RetryPolicy(base=timedelta(seconds=30), factor=2, cap=timedelta(minutes=3))

    assert [policy.delay(n).total_seconds() for n in range(1, 6)] == [30, 60, 120, 180, 180]


@pytest.mark.parametrize(
    "options",
    [
        {"base": timedelta(seconds=-1)},
        {"base": timedelta(hours=2), "cap": timedelta(hours=1)},
        {"factor": 0.5},
    ],
)
def test_a_retry_policy_refuses_nonsense(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="retry policy"):
        RetryPolicy(**options)


@pytest.mark.parametrize("kind", ["git_sync", "retention", "a", "reindex2"])
def test_job_kinds_have_the_form_of_run_kinds(kind: str) -> None:
    assert check_kind(kind) == kind


@pytest.mark.parametrize("kind", ["", "Git", "git-sync", "2sync", "_sync", "git sync"])
def test_bad_job_kinds_are_refused(kind: str) -> None:
    with pytest.raises(ValueError, match="job kind"):
        check_kind(kind)


def test_the_source_lock_is_a_fixed_pair_of_int4_keys() -> None:
    source = uuid.UUID("01a1ef28-e2a4-7ff3-8264-ecb1df0d8fd4")

    namespace, key = source_lock_key(source)
    assert namespace == SOURCE_LOCK_NAMESPACE == int.from_bytes(b"msrc", "big")
    assert source_lock_key(source) == (namespace, key)
    assert -(2**31) <= key < 2**31
    assert source_lock_key(uuid.UUID(int=source.int + 1)) != (namespace, key)


def test_the_dedupe_key_of_a_stream_is_its_id_as_postgres_prints_it() -> None:
    stream = uuid.UUID("01A1EF28-E2A4-7FF3-8264-ECB1DF0D8FD4")

    assert stream_dedupe_key(stream) == "01a1ef28-e2a4-7ff3-8264-ecb1df0d8fd4"


def test_error_text_masks_credentials_in_urls_and_is_cut_short() -> None:
    error = RuntimeError(
        "fetch https://user:hunter2@git.example.test/r and ssh://deploy@git.example.test/r failed"
    )

    assert describe_error(error) == (
        "RuntimeError: fetch https://***@git.example.test/r and ssh://***@git.example.test/r failed"
    )
    assert describe_error(TimeoutError()) == "TimeoutError"
    assert len(describe_error(ValueError("x" * 5000))) == 2000


@pytest.mark.parametrize(
    ("handlers", "options", "message"),
    [
        ({}, {}, "at least one handler"),
        ({"Git sync": _noop}, {}, "job kind"),
        ({"git_sync": _noop}, {"concurrency": 0}, "concurrency"),
        ({"git_sync": _noop}, {"heartbeat": timedelta(seconds=60)}, "heartbeat"),
        ({"git_sync": _noop}, {"poll_interval": timedelta(0)}, "poll_interval"),
    ],
)
def test_a_worker_refuses_bad_settings(
    handlers: dict[str, Any], options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Worker(NO_POOL, handlers, **options)


def test_a_worker_is_named_after_its_host_and_process_by_default() -> None:
    assert ":" in Worker(NO_POOL, {"git_sync": _noop}).name
    assert Worker(NO_POOL, {"git_sync": _noop}, name="w1").name == "w1"


def test_a_scheduler_refuses_bad_settings() -> None:
    with pytest.raises(ValueError, match="job kind"):
        Scheduler(NO_POOL, sync_kinds={"git": "Git sync"})
    with pytest.raises(ValueError, match="interval"):
        Scheduler(NO_POOL, interval=timedelta(0))
