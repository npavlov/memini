"""Public operational routes: liveness, readiness and version. None of them needs a token.

- ``GET /healthz``: liveness. Answers 200 while the process can serve HTTP. It touches nothing else,
  so a database outage does not get the process restarted.
- ``GET /readyz``: readiness. Runs every readiness check at once, each under a timeout. 200 when all
  pass, 503 otherwise. The body is the same report either way and names each check with ``ok``,
  ``failed`` or ``timeout``; the reason for a failure goes to the server log as the exception type
  only, because exception text can hold a connection URL with a password.
- ``GET /version``: the package version.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Final, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from memini import __version__

__all__ = [
    "DEFAULT_READINESS_TIMEOUT",
    "CheckResult",
    "CheckStatus",
    "Liveness",
    "Readiness",
    "ReadinessCheck",
    "VersionInfo",
    "build_health_router",
    "run_readiness_checks",
]

ReadinessCheck = Callable[[], Awaitable[None]]
"""A readiness check: returns when the dependency is usable, raises when it is not."""

CheckStatus = Literal["ok", "failed", "timeout"]

DEFAULT_READINESS_TIMEOUT: Final = 2.0
"""Seconds each readiness check may take before it counts as ``timeout``."""

_log = logging.getLogger(__name__)


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class CheckResult(BaseModel):
    status: CheckStatus = Field(description="ok, failed, or timeout.")


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"] = Field(
        description="ready when every check is ok, else not_ready."
    )
    checks: dict[str, CheckResult] = Field(description="Result of each check, by name.")


class VersionInfo(BaseModel):
    version: str = Field(description="The Memini package version.")


def _validate_timeout(timeout: float) -> float:
    """``timeout`` if it is a finite number of seconds above zero; raises ``ValueError`` if not."""
    if not (math.isfinite(timeout) and timeout > 0):
        raise ValueError("the readiness timeout must be a finite number of seconds above zero")
    return timeout


async def _run_check(name: str, check: ReadinessCheck, timeout: float) -> CheckStatus:
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            await check()
    except Exception as exc:
        status: CheckStatus = "timeout" if deadline.expired() else "failed"
        # The type only: the message can carry a connection URL with a password.
        _log.warning("readiness check %s: %s (%s)", name, status, type(exc).__name__)
        return status
    return "ok"


async def run_readiness_checks(checks: Mapping[str, ReadinessCheck], timeout: float) -> Readiness:
    """Run ``checks`` concurrently, each limited to ``timeout`` seconds, and report on each."""
    names = list(checks)
    statuses = await asyncio.gather(*(_run_check(name, checks[name], timeout) for name in names))
    ready = all(status == "ok" for status in statuses)
    return Readiness(
        status="ready" if ready else "not_ready",
        checks={
            name: CheckResult(status=status) for name, status in zip(names, statuses, strict=True)
        },
    )


def build_health_router(
    readiness_checks: Mapping[str, ReadinessCheck],
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
) -> APIRouter:
    """The public operational routes, with the readiness checks they run."""
    checks = dict(readiness_checks)
    timeout = _validate_timeout(readiness_timeout)
    router = APIRouter(tags=["health"])

    @router.get("/healthz", summary="Liveness")
    async def liveness() -> Liveness:
        """200 while the process can serve HTTP. Checks no dependency."""
        return Liveness()

    @router.get(
        "/readyz",
        summary="Readiness",
        responses={503: {"model": Readiness, "description": "Not ready: a check failed."}},
    )
    async def readiness(response: Response) -> Readiness:
        """200 when every readiness check passes, 503 otherwise."""
        report = await run_readiness_checks(checks, timeout)
        if report.status != "ready":
            response.status_code = 503
        return report

    @router.get("/version", summary="Version")
    async def version() -> VersionInfo:
        """The Memini package version."""
        return VersionInfo(version=__version__)

    return router
