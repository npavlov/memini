"""How onnxruntime runs in the process: without telemetry, and within the CPUs it is given.

Both local models, the embedding provider and the reranker (:mod:`memini.rerank`), import the
runtime after :func:`quiet_runtime` and open their sessions with :func:`session_options`.

- Telemetry. onnxruntime's official builds send usage events to Microsoft unless
  ``ORT_DISABLE_TELEMETRY`` is set when it starts (its ``Privacy.md``). Memini runs locally
  (principle 8) and sends nothing anywhere it was not told to: the variable is set to ``1`` before
  the runtime is imported, unless it is set already, so an explicit ``ORT_DISABLE_TELEMETRY=0`` is
  the only way to turn the events on.
- Threads. onnxruntime starts one thread per core of the machine for each run, and every step of
  a run waits for the slowest of them. Under a CPU limit of a container (``docker run --cpus``,
  ``cpus`` in compose) those threads share the time of fewer CPUs: on 2 CPUs of a 16-core machine
  a search's reranking took 11 times as long as with 2 threads (docs/requirements.md, section 9,
  Reranker). So a session gets no more threads than the CPU limit allows, rounded up
  (:func:`cpu_limit`). Without a limit the embedding provider leaves the choice to onnxruntime;
  the reranker takes at most 4 (:func:`model_threads`, :data:`memini.rerank.local.THREADS`).
"""

import math
import os
import sys
from pathlib import Path
from typing import Any, Final

__all__ = ["TELEMETRY_VAR", "cpu_limit", "model_threads", "quiet_runtime", "session_options"]

TELEMETRY_VAR: Final = "ORT_DISABLE_TELEMETRY"
"""Keeps onnxruntime from sending usage events to Microsoft; read once, when it starts."""

_CGROUP_V1_QUOTAS: Final = ("sys/fs/cgroup/cpu", "sys/fs/cgroup/cpu,cpuacct")
"""Where cgroup v1 keeps ``cpu.cfs_quota_us`` and ``cpu.cfs_period_us``, inside a container."""


def quiet_runtime() -> None:
    """Keep onnxruntime from sending usage events: :data:`TELEMETRY_VAR` set to ``1`` unless it is
    set already. Call it before onnxruntime is imported; once it has started, the variable is not
    read again."""
    os.environ.setdefault(TELEMETRY_VAR, "1")


def cpu_limit(root: Path = Path("/")) -> int | None:
    """The CPUs this process's cgroup may use, rounded up, when that is fewer than the CPUs it may
    run on; ``None`` without such a limit, or where there are no cgroups. ``root`` is for tests."""
    quota = _cgroup_cpus(root)
    if quota is None:
        return None
    available = _available_cpus()
    limit = max(1, math.ceil(quota))
    return limit if available is None or limit < available else None


def model_threads(cap: int) -> int:
    """Threads for one run of a model: as many as the CPU limit allows, or the CPUs the process may
    run on, but at most ``cap``."""
    return max(1, min(cap, cpu_limit() or _available_cpus() or 1))


def session_options(onnxruntime: Any, threads: int | None = None) -> Any:
    """``onnxruntime.SessionOptions`` for one model of the process: errors only in its log, and
    ``threads`` threads for a run, or else at most :func:`cpu_limit`."""
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3  # errors only; warnings about the graph are noise here
    if threads is None:
        threads = cpu_limit()
    if threads is not None:
        options.intra_op_num_threads = threads
    return options


def _available_cpus() -> int | None:
    """The CPUs the process may run on: its affinity on Linux, else the machine's."""
    if sys.platform == "linux":
        return len(os.sched_getaffinity(0))
    return os.cpu_count()


def _cgroup_cpus(root: Path) -> float | None:
    """The smallest CPU quota of this process's cgroup and its parents, in CPUs (cgroup v2), or of
    the container's cgroup (v1); ``None`` without one."""
    try:
        lines = (root / "proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    own = next((line.removeprefix("0::") for line in lines if line.startswith("0::")), None)
    if own is None:
        return _v1_cpus(root)
    base = root / "sys/fs/cgroup"
    found: float | None = None
    current = base / own.strip("/")
    while True:
        cpus = _v2_cpus(current / "cpu.max")
        if cpus is not None:
            found = cpus if found is None else min(found, cpus)
        if current == base or base not in current.parents:
            return found
        current = current.parent


def _v2_cpus(path: Path) -> float | None:
    """``cpu.max`` of cgroup v2: ``<quota> <period>`` in microseconds, or ``max <period>``."""
    try:
        quota, period = path.read_text(encoding="utf-8").split()
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _v1_cpus(root: Path) -> float | None:
    for directory in _CGROUP_V1_QUOTAS:
        try:
            quota = int((root / directory / "cpu.cfs_quota_us").read_text(encoding="utf-8"))
            period = int((root / directory / "cpu.cfs_period_us").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return quota / period if quota > 0 and period > 0 else None
    return None
