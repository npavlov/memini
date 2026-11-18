"""The call budget of a recipe's ``[rate]``, and how long a failed call waits before it is tried
again.

A run has one :class:`Budget` per recipe ``[rate]`` (docs/recipes.md, ``[rate]``): at most
``max_calls`` calls in the run, at most ``calls_per_minute`` in any 60 seconds, and a pause that a
rate-limit answer of the server sets for every call. ``concurrency`` and ``timeout`` are applied by
the executor around each call. Every attempt is a call: a retry spends the budget as the first
attempt did, so a server that keeps failing cannot make a run call it without end.

:class:`RetryPolicy` says which attempt a call is at most tried (``mcp_call_attempts``) and how long
it waits before the next: exponential backoff from ``base`` up to ``cap``, or what the server's
``Retry-After`` asked, up to ``max_retry_after``.

Time comes from ``clock`` and waits go through ``sleep``, so tests run them without waiting.
"""

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import anyio

from memini.recipes.model import Rate

__all__ = ["DEFAULT_RETRY", "MINUTE", "Budget", "RetryPolicy"]

MINUTE: Final = 60.0


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    """How often one call is tried and how long it waits in between."""

    attempts: int = 3
    """Tries of one call, the first included (``mcp_call_attempts``)."""
    base: float = 1.0
    """Seconds before the second try; doubled for each later one."""
    cap: float = 60.0
    """The longest wait of the backoff."""
    max_retry_after: float = 300.0
    """The longest wait a ``Retry-After`` of the server is followed for; a longer one is cut."""

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base < 0 or self.cap < self.base or self.max_retry_after < 0:
            raise ValueError("a retry policy needs attempts >= 1 and 0 <= base <= cap")

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after try number ``attempt`` (1 for the first) failed: the server's
        ``retry_after`` if it gave one, else the backoff."""
        if retry_after is not None:
            return min(retry_after, self.max_retry_after)
        return min(self.cap, self.base * 2 ** max(attempt - 1, 0))


DEFAULT_RETRY: Final = RetryPolicy()


class Budget:
    """The calls a run may still make; see the module."""

    def __init__(
        self,
        rate: Rate,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._rate = rate
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()
        self._paused_until = 0.0
        self._lock = anyio.Lock()
        self.calls = 0
        """Calls taken so far."""

    @property
    def spent(self) -> bool:
        """Whether the run has made ``max_calls`` calls."""
        return self.calls >= self._rate.max_calls

    def pause(self, seconds: float) -> None:
        """Make no call for ``seconds`` from now: the server asked to slow down."""
        if seconds > 0:
            self._paused_until = max(self._paused_until, self._clock() + seconds)

    async def take(self) -> bool:
        """Wait until a call may be made and count it; ``False``, without waiting, once the run
        has made ``max_calls`` calls."""
        async with self._lock:
            if self.spent:
                return False
            while True:
                now = self._clock()
                while self._recent and self._recent[0] <= now - MINUTE:
                    self._recent.popleft()
                wait = self._paused_until - now
                if len(self._recent) >= self._rate.calls_per_minute:
                    wait = max(wait, self._recent[0] + MINUTE - now)
                if wait <= 0:
                    break
                await self._sleep(wait)
            self._recent.append(now)
            self.calls += 1
            return True
