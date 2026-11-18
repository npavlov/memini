"""Retries of calls to the tide service, with exponential backoff and jitter."""

import random
import time
from collections.abc import Callable

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 30.0


class TideServiceUnavailable(Exception):
    """The tide service did not answer after every attempt."""


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Seconds to wait before attempt number ``attempt`` (from 1): doubling from the base delay,
    capped, with full jitter so that clients which failed together do not retry together."""
    ceiling = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
    return (rng or random).uniform(0, ceiling)


def with_retries[T](call: Callable[[], T], sleep: Callable[[float], None] = time.sleep) -> T:
    """Run ``call`` until it succeeds, at most MAX_ATTEMPTS times."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except ConnectionError:
            if attempt == MAX_ATTEMPTS:
                raise TideServiceUnavailable("the tide service did not answer") from None
            sleep(backoff_delay(attempt))
    raise AssertionError("unreachable")
