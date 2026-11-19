"""How onnxruntime runs: its telemetry off, its threads within the CPU limit of a cgroup, and
what its native code writes past the JSON log given to the log.

The cgroup files are written into a temporary root; nothing reads the machine's own.
"""

import io
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memini.embeddings import runtime
from memini.embeddings.runtime import (
    NATIVE_LOGGER,
    TELEMETRY_VAR,
    cpu_limit,
    model_threads,
    native_output_logged,
    quiet_runtime,
    session_options,
)


def test_telemetry_is_off_unless_turned_on_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TELEMETRY_VAR, "unset by the test")
    monkeypatch.delenv(TELEMETRY_VAR)  # restored, that is removed, after the test

    quiet_runtime()
    assert os.environ[TELEMETRY_VAR] == "1"

    monkeypatch.setenv(TELEMETRY_VAR, "0")
    quiet_runtime()
    assert os.environ[TELEMETRY_VAR] == "0"


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


@pytest.fixture
def cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process may run on 16 CPUs, wherever the test runs."""
    monkeypatch.setattr(runtime, "_available_cpus", lambda: 16)


@pytest.mark.usefixtures("cpus")
@pytest.mark.parametrize(
    ("quota", "expected"),
    [("200000 100000", 2), ("150000 100000", 2), ("50000 100000", 1), ("100000 100000", 1)],
)
def test_a_container_limit_bounds_the_threads(tmp_path: Path, quota: str, expected: int) -> None:
    write(tmp_path, "proc/self/cgroup", "0::/\n")
    write(tmp_path, "sys/fs/cgroup/cpu.max", f"{quota}\n")

    assert cpu_limit(tmp_path) == expected


@pytest.mark.usefixtures("cpus")
def test_the_smallest_limit_of_the_cgroup_and_its_parents_counts(tmp_path: Path) -> None:
    write(tmp_path, "proc/self/cgroup", "0::/system.slice/memini.service\n")
    write(tmp_path, "sys/fs/cgroup/system.slice/memini.service/cpu.max", "max 100000\n")
    write(tmp_path, "sys/fs/cgroup/system.slice/cpu.max", "400000 100000\n")

    assert cpu_limit(tmp_path) == 4


@pytest.mark.usefixtures("cpus")
@pytest.mark.parametrize(
    "files",
    [
        {},  # no cgroups: macOS, Windows
        {"proc/self/cgroup": "0::/\n", "sys/fs/cgroup/cpu.max": "max 100000\n"},
        {"proc/self/cgroup": "0::/\n", "sys/fs/cgroup/cpu.max": "3200000 100000\n"},  # 32 > 16
        {"proc/self/cgroup": "0::/\n", "sys/fs/cgroup/cpu.max": "garbage\n"},
        {"proc/self/cgroup": "0::/\n"},
    ],
)
def test_without_a_limit_below_the_cpus_onnxruntime_chooses(
    tmp_path: Path, files: dict[str, str]
) -> None:
    for path, text in files.items():
        write(tmp_path, path, text)

    assert cpu_limit(tmp_path) is None


@pytest.mark.usefixtures("cpus")
def test_cgroup_v1_quota(tmp_path: Path) -> None:
    write(tmp_path, "proc/self/cgroup", "12:cpu,cpuacct:/docker/abc\n")
    write(tmp_path, "sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us", "300000\n")
    write(tmp_path, "sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us", "100000\n")
    assert cpu_limit(tmp_path) == 3

    write(tmp_path, "sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us", "-1\n")
    assert cpu_limit(tmp_path) is None


@pytest.mark.usefixtures("cpus")
def test_a_model_takes_the_limit_or_the_cpus_up_to_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "cpu_limit", lambda: None)
    assert model_threads(4) == 4  # 16 CPUs, no limit
    assert model_threads(32) == 16

    monkeypatch.setattr(runtime, "cpu_limit", lambda: 2)
    assert model_threads(4) == 2


def test_session_options_take_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    class Options(SimpleNamespace):
        pass

    fake = SimpleNamespace(SessionOptions=Options)

    monkeypatch.setattr(runtime, "cpu_limit", lambda: 2)
    limited = session_options(fake)
    assert limited.intra_op_num_threads == 2
    assert limited.log_severity_level == 3

    monkeypatch.setattr(runtime, "cpu_limit", lambda: None)
    assert not hasattr(session_options(fake), "intra_op_num_threads")
    assert session_options(fake, 3).intra_op_num_threads == 3


NATIVE_WARNING = (
    "2026-11-19 01:38:22.231 [W:onnxruntime:Default, device_discovery.cc:95 GetPciBusId]"
    " Skipping pci_bus_id for PCI path"
)


def test_native_output_of_the_import_becomes_a_log_record(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """What native code writes to the descriptor of stderr while onnxruntime is first imported is
    a warning of the logger onnxruntime; a JSON log line another thread wrote meanwhile is given
    back as it was; nothing else reaches stderr."""
    record = json.dumps({"level": "info", "logger": "memini.other", "message": "meanwhile"})
    with caplog.at_level(logging.WARNING, logger=NATIVE_LOGGER), native_output_logged():
        fd = sys.stderr.fileno()
        os.write(fd, f"{NATIVE_WARNING}\n".encode())
        os.write(fd, f"{record}\n\n".encode())

    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        (NATIVE_LOGGER, logging.WARNING, NATIVE_WARNING)
    ]
    err = capfd.readouterr().err
    assert record in err.splitlines()
    assert "GetPciBusId" not in err


def test_without_a_descriptor_the_block_runs_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()  # no fileno, as a captured stream of a test
    monkeypatch.setattr(sys, "stderr", stream)
    ran: list[bool] = []
    with native_output_logged():
        ran.append(True)
    assert ran == [True]
    assert stream.getvalue() == ""
