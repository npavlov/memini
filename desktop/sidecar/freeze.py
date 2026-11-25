"""Freeze the launcher into the one program the desktop app carries with it.

The app does not need Homebrew, Scoop or a Python on the user's computer: it runs
``memini-server`` as a Tauri sidecar. This script makes that sidecar from the launcher itself,
``scripts/memini-server.py``, so there is one implementation of it. It takes the launcher and
the files it ships with from ``packaging/server/build.py``, the recipe of the Homebrew and Scoop
packages (compose files of the pinned release, checked against the launcher's own checksums),
and freezes them into one executable with PyInstaller:

    desktop/src-tauri/binaries/memini-server-<target triple>[.exe]

which is where ``bundle.externalBin`` of ``tauri.conf.json`` looks. Run it with PyInstaller on
the path of the interpreter, for the platform it runs on (PyInstaller does not cross-compile):

    uv run --no-project --python 3.12 --with pyinstaller==6.22.3 python desktop/sidecar/freeze.py

It ends by running the frozen program and the script side by side, without Docker, and fails
unless both print the same JSON.
"""

import json
import os
import platform
import runpy
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BINARIES = ROOT / "desktop/src-tauri/binaries"
NAME = "memini-server"

# The targets the desktop workflow builds: (system, machine) as Python names them.
TRIPLES = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
}


def target_triple() -> str:
    """The Rust target of this machine, which names the sidecar for Tauri."""
    key = (platform.system(), platform.machine())
    if key not in TRIPLES:
        raise SystemExit(f"no sidecar target is known for {key[0]} on {key[1]}")
    return TRIPLES[key]


def stage(directory: Path) -> Path:
    """The launcher and its files as the packages ship them, unpacked into ``directory``."""
    build = runpy.run_path(str(ROOT / "packaging/server/build.py"))["build"]
    build(directory / "package")
    (archive,) = (directory / "package").glob("*.zip")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory)
    return directory / NAME


def freeze(staged: Path, work: Path) -> Path:
    """One executable of the staged launcher, with its files inside."""
    data = [
        (staged / "compose.yaml", "."),
        (staged / "compose.release.yaml", "."),
        (staged / "LICENSE", "."),
        (staged / "secrets/unset", "secrets"),
        (staged / "docs/examples", "docs/examples"),
    ]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--log-level=WARN",
        f"--name={NAME}",
        f"--distpath={work / 'dist'}",
        f"--workpath={work / 'build'}",
        f"--specpath={work}",
    ]
    for source, target in data:
        command.append(f"--add-data={source}{os.pathsep}{target}")
    command.append(str(staged / f"{NAME}.py"))
    subprocess.run(command, check=True)  # noqa: S603 - this interpreter, a fixed argument vector
    suffix = ".exe" if platform.system() == "Windows" else ""
    return work / "dist" / f"{NAME}{suffix}"


def status_without_docker(program: list[str], state: Path, empty: Path) -> object:
    """What ``status --json`` prints where there is no server and no ``docker`` to be found."""
    environment = {**os.environ, "PATH": str(empty)}
    result = subprocess.run(  # noqa: S603 - the program just built, a fixed argument vector
        [*program, "--state-dir", str(state), "status", "--json"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    BINARIES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        staged = stage(work / "staged")
        frozen = freeze(staged, work)
        empty = work / "empty"
        empty.mkdir()
        script = [sys.executable, str(staged / f"{NAME}.py")]
        expected = status_without_docker(script, work / "no-server", empty)
        printed = status_without_docker([str(frozen)], work / "no-server", empty)
        if printed != expected:
            raise SystemExit(f"the frozen launcher printed {printed}, the script {expected}")
        target = BINARIES / f"{NAME}-{target_triple()}{frozen.suffix}"
        shutil.copy2(frozen, target)
    print(f"{target.relative_to(ROOT)}: {target.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
