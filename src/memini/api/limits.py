"""Limits of every HTTP request: rate limits, the size of the body, the time of database work.

:class:`RequestLimitsMiddleware` wraps every route, the MCP endpoint included. For each request:

1. **Database time limits.** Every pooled connection the request borrows, on whichever path and
   in whichever task, gets ``http_statement_timeout`` per statement and
   ``http_idle_in_transaction_timeout`` per idle transaction (:mod:`memini.db.limits`). Search sets
   its own, ``search_timeout``. A statement or session stopped by its limit answers 503
   ``query_timeout`` (:mod:`memini.api.errors`). Jobs and the CLI run outside requests and keep the
   database's defaults.
2. **Rate limits**, for ``/v1`` and the MCP endpoint; the public routes (``/healthz``, ``/readyz``,
   ``/version``, ``/openapi.json``) are not counted. Counted in this process, in memory: one service
   process (principle 12), so the budgets start full again after a restart. A token has two
   budgets, keyed by the token's id: reads (``GET``, ``HEAD``, ``OPTIONS``, and every MCP request:
   the MCP tools read) and writes (every other method). A request whose token is not known to be
   valid yet (no token, a malformed one, or one not seen to authenticate) is counted against its
   client address instead (``http_rate_limit_unauthenticated``), before the token is looked up, so
   guessing tokens is slow and costs no database work once the budget is spent. A token becomes
   known when a request with it authenticates, and is forgotten when one answers 401 or after
   :data:`KNOWN_TOKEN_TTL` idle. Over budget: 429 ``rate_limited`` with ``Retry-After`` in whole
   seconds, before anything else is done. ``http_rate_limit = false`` turns rate limits off.
3. **Body size**: at most ``http_max_body_size``, refused by its ``Content-Length`` before it is
   read, or as soon as more of it arrives: 413 ``content_too_large``. The ingest route has its own
   limit (``ingest_max_body_size``) and is left to it.

Each budget is a token bucket that holds a minute's worth of requests and refills steadily, so a
burst of up to that many passes at once. A bucket that has refilled is forgotten, since a new one
would be the same; at most :data:`MAX_KEYS` are kept per budget, the least recently used dropped
first, which only ever makes a limit more lenient. The client address is the one the ASGI server
reports: behind a proxy it is the proxy's, unless the server is told to trust its forwarding
headers.
"""

import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memini.api.errors import ApiError, error_response
from memini.auth import AuthenticationError, Principal, bearer_token, hash_token
from memini.config import Settings
from memini.db import SessionLimits, session_limits
from memini.mcp import MCP_PATH

__all__ = [
    "KNOWN_TOKEN_TTL",
    "MAX_KEYS",
    "RateLimiter",
    "Refusal",
    "RequestKind",
    "RequestLimitsMiddleware",
]

MAX_KEYS: Final = 10_000
"""Keys kept per budget, and tokens known to be valid."""

KNOWN_TOKEN_TTL: Final = 900.0
"""Seconds a token stays known to be valid after its last request."""

RequestKind = Literal["read", "write"]
"""Which budget of its token a request is counted against."""

Budget = Literal["read", "write", "unauthenticated"]

_V1_PREFIX: Final = "/v1"  # memini.api.app.V1_PREFIX, which imports this module
_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_OWN_BODY_LIMIT: Final = re.compile(r"/v1/workspaces/[^/]+/sources/[^/]+/records")
"""The ingest route (``POST``), which reads its body up to ``ingest_max_body_size`` itself."""

_log = logging.getLogger(__name__)


# --- Rate limits ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float
    refusing: bool = False


class _Budget:
    """Token buckets by key: ``per_minute`` requests a minute, as many at once."""

    def __init__(self, per_minute: int, max_keys: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.max_keys = max_keys
        self._buckets: OrderedDict[Hashable, _Bucket] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def take(self, key: Hashable, now: float) -> tuple[float, bool]:
        """Take one request for ``key``. Returns the seconds to wait (0: taken) and whether this
        is the first refusal since the key was last admitted."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(self.capacity, now)
        else:
            bucket.tokens = self._level(bucket, now)
            bucket.updated = now
            self._buckets.move_to_end(key)
        first = False
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            bucket.refusing = False
            wait = 0.0
        else:
            first = not bucket.refusing
            bucket.refusing = True
            wait = (1 - bucket.tokens) / self.rate
        self._evict(now)
        return wait, first

    def _level(self, bucket: _Bucket, now: float) -> float:
        return min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)

    def _evict(self, now: float) -> None:
        # Least recently used first: drop buckets that have refilled, and any beyond max_keys.
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            if len(self._buckets) <= self.max_keys and self._level(bucket, now) < self.capacity:
                return
            del self._buckets[key]


class _KnownTokens:
    """Digests of tokens seen to authenticate, with their ids; idle ones expire."""

    def __init__(self, ttl: float, max_keys: int) -> None:
        self.ttl = ttl
        self.max_keys = max_keys
        self._entries: OrderedDict[bytes, tuple[UUID, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, digest: bytes, now: float) -> UUID | None:
        entry = self._entries.get(digest)
        if entry is None:
            return None
        token_id, seen = entry
        if now - seen > self.ttl:
            del self._entries[digest]
            return None
        return token_id

    def remember(self, digest: bytes, token_id: UUID, now: float) -> None:
        self._entries[digest] = (token_id, now)
        self._entries.move_to_end(digest)
        while self._entries:
            oldest, (_, seen) = next(iter(self._entries.items()))
            if len(self._entries) <= self.max_keys and now - seen <= self.ttl:
                return
            del self._entries[oldest]

    def forget(self, digest: bytes) -> None:
        self._entries.pop(digest, None)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A request over its budget."""

    budget: Budget
    retry_after: int
    """Whole seconds until the budget has room for it, at least 1."""
    first: bool
    """Whether it is the first refusal since that budget last admitted the key."""
    subject: str
    """Whose budget, for the log: ``token <id>`` or ``address <host>``."""


class RateLimiter:
    """Per-token budgets of reads and writes, and per-address budgets for requests whose token is
    not known to be valid. In memory, for one process; see the module."""

    def __init__(
        self,
        *,
        reads: int,
        writes: int,
        unauthenticated: int,
        max_keys: int = MAX_KEYS,
        known_ttl: float = KNOWN_TOKEN_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``reads``, ``writes`` and ``unauthenticated`` are requests per minute."""
        if min(reads, writes, unauthenticated, max_keys) < 1 or known_ttl <= 0:
            raise ValueError("a budget, max_keys and known_ttl must be positive")
        self._budgets: dict[Budget, _Budget] = {
            "read": _Budget(reads, max_keys),
            "write": _Budget(writes, max_keys),
            "unauthenticated": _Budget(unauthenticated, max_keys),
        }
        self._known = _KnownTokens(known_ttl, max_keys)
        self._clock = clock

    @classmethod
    def from_settings(
        cls, settings: Settings, *, clock: Callable[[], float] = time.monotonic
    ) -> "RateLimiter | None":
        """The limiter ``settings`` describe; ``None`` when ``http_rate_limit`` is off."""
        if not settings.http_rate_limit:
            return None
        return cls(
            reads=settings.http_rate_limit_reads,
            writes=settings.http_rate_limit_writes,
            unauthenticated=settings.http_rate_limit_unauthenticated,
            clock=clock,
        )

    def keys(self, budget: Budget) -> int:
        """How many keys ``budget`` keeps now."""
        return len(self._budgets[budget])

    def known_tokens(self) -> int:
        """How many tokens are known to be valid now."""
        return len(self._known)

    def admit(self, kind: RequestKind, token_digest: bytes | None, client: str) -> Refusal | None:
        """Count one request; ``None`` if its budget has room, else why not.

        ``token_digest`` is :func:`~memini.auth.hash_token` of the request's bearer token, ``None``
        without a well-formed one; ``client`` is the client address.
        """
        now = self._clock()
        token_id = None if token_digest is None else self._known.get(token_digest, now)
        budget: Budget
        key: Hashable
        if token_id is None:
            budget, key, subject = "unauthenticated", client, f"address {client}"
        else:
            budget, key, subject = kind, token_id, f"token {token_id}"
        wait, first = self._budgets[budget].take(key, now)
        if wait <= 0:
            return None
        return Refusal(budget, max(1, math.ceil(wait)), first, subject)

    def authenticated(self, token_digest: bytes, token_id: UUID) -> None:
        """A request with the token authenticated: count its next ones against the token."""
        self._known.remember(token_digest, token_id, self._clock())

    def refused(self, token_digest: bytes) -> None:
        """A request with the token answered 401: count its next ones against the address."""
        self._known.forget(token_digest)


# --- The middleware ------------------------------------------------------------------------------


_RATE_MESSAGES: Final[dict[Budget, str]] = {
    "read": "Too many reads for this token",
    "write": "Too many writes for this token",
    "unauthenticated": "Too many requests without a valid token from this address",
}


def _too_many_requests(refusal: Refusal) -> ApiError:
    seconds = refusal.retry_after
    return ApiError(
        429,
        "rate_limited",
        f"{_RATE_MESSAGES[refusal.budget]}; retry after {seconds} s.",
        headers={"Retry-After": str(seconds)},
    )


def _content_too_large(limit: int) -> ApiError:
    return ApiError(413, "content_too_large", f"The body is larger than {limit} bytes.")


async def _answer(error: ApiError, scope: Scope, receive: Receive, send: Send) -> None:
    response = error_response(error.status_code, error.code, error.detail, headers=error.headers)
    await response(scope, receive, send)


def _log_refusal(refusal: Refusal) -> None:
    _log.info("requests refused: the %s budget of %s is spent", refusal.budget, refusal.subject)


def _is_counted(path: str) -> bool:
    return path in (MCP_PATH, _V1_PREFIX) or path.startswith(_V1_PREFIX + "/")


def _kind(method: str, path: str) -> RequestKind:
    return "read" if path == MCP_PATH or method in _READ_METHODS else "write"


def _token_digest(headers: Headers) -> bytes | None:
    try:
        return hash_token(bearer_token(headers.get("Authorization")))
    except AuthenticationError:
        return None


def _client(scope: Scope) -> str:
    client: Any = scope.get("client")
    if isinstance(client, tuple | list) and client and isinstance(client[0], str):
        return client[0]
    return "unknown"


def _declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length", "")
    return int(value) if value.isascii() and value.isdigit() else None


class RequestLimitsMiddleware:
    """ASGI middleware that applies the request limits of ``settings``; see the module.

    Register it with ``app.add_middleware(RequestLimitsMiddleware, settings=settings)``: inside the
    request-id middleware, so its errors carry the request id. Its rate limits live as long as the
    application. ``clock`` is for tests.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.max_body_size: int = settings.http_max_body_size
        self.session_limits = SessionLimits(
            statement_timeout=settings.http_statement_timeout,
            idle_in_transaction_timeout=settings.http_idle_in_transaction_timeout,
        )
        self.limiter = RateLimiter.from_settings(settings, clock=clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with session_limits(self.session_limits):
            await self._serve(scope, receive, send)

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope["path"]
        method: str = scope["method"]
        headers = Headers(scope=scope)

        digest: bytes | None = None
        limiter = self.limiter if _is_counted(path) else None
        if limiter is not None:
            digest = _token_digest(headers)
            refusal = limiter.admit(_kind(method, path), digest, _client(scope))
            if refusal is not None:
                if refusal.first:  # once per spent budget, not once per request
                    _log_refusal(refusal)
                await _answer(_too_many_requests(refusal), scope, receive, send)
                return

        limit = None if method == "POST" and _OWN_BODY_LIMIT.fullmatch(path) else self.max_body_size
        if limit is not None:
            declared = _declared_length(headers)
            if declared is not None and declared > limit:
                await _answer(_content_too_large(limit), scope, receive, send)
                return

        # The authentication layers keep the principal here (request.state.principal).
        state: dict[str, Any] = scope.setdefault("state", {})
        received = 0
        too_large = False
        started = False
        replaced = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if limit is not None and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    # A route re-raises it as it is; any other reader fails, and the answer
                    # below replaces whatever it sends.
                    raise _content_too_large(limit)
            return message

        async def watched_send(message: Message) -> None:
            nonlocal started, replaced
            if replaced:
                return
            if message["type"] == "http.response.start":
                started = True
                if too_large:
                    replaced = True
                    await _answer(_content_too_large(limit or 0), scope, receive, send)
                    return
                if limiter is not None and digest is not None:
                    principal = state.get("principal")
                    if isinstance(principal, Principal):
                        limiter.authenticated(digest, principal.token_id)
                    elif message["status"] == 401:
                        limiter.refused(digest)
            await send(message)

        try:
            await self.app(scope, limited_receive, watched_send)
        except Exception:
            # Only the failure the refused body caused is answered here; the 413 may be out.
            if not too_large or (started and not replaced):
                raise
            if not replaced:
                await _answer(_content_too_large(limit or 0), scope, receive, send)
