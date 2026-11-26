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
unless both print the same JSON and the frozen program names the image it should.

The launcher in the repository pins the image of the release before the one being tagged: the pin
moves in a change after the tag (docs/releasing.md, step 3). A release build passes the image it
has just published, with its digest, so that the app of release X installs the server of X:

    python desktop/sidecar/freeze.py --release-image ghcr.io/npavlov/memini:X@sha256:<digest>

The copy of the launcher being frozen then pins that image, and carries the compose files,
licence and examples of the tag vX instead of the pinned release's, with their checksums. The
image must be this repository's, of a release version, with a digest; nothing else is accepted.
Without the option the launcher is frozen as scripts/memini-server.py pins it.
"""

import argparse
import hashlib
import json
import os
import platform
import re
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
# The only image a release build may pin: this repository's, a release version, a digest.
RELEASE_IMAGE = re.compile(
    r"ghcr\.io/npavlov/memini:"
    r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-rc\.(?:0|[1-9][0-9]*))?)"
    r"@sha256:[0-9a-f]{64}"
)
# The pins of the launcher that a release build rewrites, each one block of its source.
IMAGE_PIN = re.compile(r"^IMAGE = \(\n.*?\n\)\n", re.MULTILINE | re.DOTALL)
COMPOSE_PIN = re.compile(r"^COMPOSE_FILES = \{\n.*?\n\}\n", re.MULTILINE | re.DOTALL)

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


def release_tag(image: str) -> str:
    """The tag of a release image, ``vX.Y.Z[-rc.N]``; refuses any other image."""
    match = RELEASE_IMAGE.fullmatch(image)
    if match is None:
        raise ValueError(
            f"not a release image: {image!r}; expected"
            " ghcr.io/npavlov/memini:<version>@sha256:<64 hex digits>"
        )
    return f"v{match.group(1)}"


def pin_release(source: str, image: str, compose: dict[str, bytes]) -> str:
    """The launcher's ``source`` with ``image`` and the checksums of ``compose`` as its pins."""
    release_tag(image)
    name, digest = image.split("@")
    image_block = f'IMAGE = (\n    "{name}"\n    "@{digest}"\n)\n'
    lines = "".join(
        f'    "{filename}": "{hashlib.sha256(data).hexdigest()}",\n'
        for filename, data in compose.items()
    )
    compose_block = f"COMPOSE_FILES = {{\n{lines}}}\n"
    for pattern, block in ((IMAGE_PIN, image_block), (COMPOSE_PIN, compose_block)):
        matches = list(pattern.finditer(source))
        if len(matches) != 1:
            raise ValueError(f"the launcher has no single pin matching {pattern.pattern!r}")
        start, end = matches[0].span()
        source = source[:start] + block + source[end:]
    return source


def stage_release(staged: Path, image: str) -> None:
    """Make the staged launcher the one of the release ``image``: its pins, its tag's files."""
    tag = release_tag(image)
    git = shutil.which("git")
    if not git:
        raise SystemExit("Git is required to read the release's files")
    files: list[str] = runpy.run_path(str(ROOT / "packaging/server/build.py"))["FILES"]
    contents = {
        name: subprocess.check_output(  # noqa: S603 - a checked tag and the recipe's paths
            [git, "-C", str(ROOT), "show", f"{tag}:{name}"]
        )
        for name in files
    }
    for name, data in contents.items():
        (staged / name).parent.mkdir(parents=True, exist_ok=True)
        (staged / name).write_bytes(data)
    launcher = staged / f"{NAME}.py"
    pins: dict[str, str] = runpy.run_path(str(launcher))["COMPOSE_FILES"]
    compose = {name: contents[name] for name in pins}
    launcher.write_text(
        pin_release(launcher.read_text(encoding="utf-8"), image, compose), encoding="utf-8"
    )


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


def installed_state(directory: Path) -> Path:
    """A state directory that looks installed, so that ``status --json`` names the image."""
    directory.mkdir()
    (directory / ".memini-server-v1").write_text("0" * 32, encoding="ascii")
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the launcher into the app's sidecar.")
    parser.add_argument(
        "--release-image",
        help="the image a release just published: ghcr.io/npavlov/memini:<version>@sha256:<digest>",
    )
    release_image: str | None = parser.parse_args().release_image
    if release_image is not None:
        try:
            release_tag(release_image)
        except ValueError as error:
            raise SystemExit(str(error)) from None
    BINARIES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        staged = stage(work / "staged")
        if release_image is not None:
            stage_release(staged, release_image)
        image = release_image or runpy.run_path(str(staged / f"{NAME}.py"))["IMAGE"]
        frozen = freeze(staged, work)
        empty = work / "empty"
        empty.mkdir()
        script = [sys.executable, str(staged / f"{NAME}.py")]
        printed: object = None
        for state in (work / "no-server", installed_state(work / "installed")):
            expected = status_without_docker(script, state, empty)
            printed = status_without_docker([str(frozen)], state, empty)
            if printed != expected:
                raise SystemExit(f"the frozen launcher printed {printed}, the script {expected}")
        # The last state looks installed: status names the image a start would run.
        if not isinstance(printed, dict) or printed.get("image") != image:
            raise SystemExit(f"the frozen launcher printed {printed}, not the image {image}")
        print(f"the frozen launcher pins {image}")
        target = BINARIES / f"{NAME}-{target_triple()}{frozen.suffix}"
        shutil.copy2(frozen, target)
    print(f"{target.relative_to(ROOT)}: {target.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
