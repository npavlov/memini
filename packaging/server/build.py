"""Bundle the server manager with configuration and examples from its matching release tag."""

import argparse
import hashlib
import json
import runpy
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = "v0.2.0-rc.1"
VERSION = "0.2.0-rc.1"
# The package revision: raised when the launcher changes between releases of the service, so the
# archive of an earlier revision, which users have installed, is never replaced. Back to 1 with a
# new VERSION.
REVISION = 2
ARCHIVE = f"memini-server-{VERSION}-{REVISION}.zip"
URL = f"https://github.com/npavlov/memini/releases/download/{TAG}/{ARCHIVE}"
FILES = [
    "LICENSE",
    "compose.yaml",
    "compose.release.yaml",
    "secrets/unset",
    "docs/examples/mail-thread.json",
    "docs/examples/thread.json",
    "docs/examples/meeting.json",
    "docs/examples/document.json",
]


def build(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    git = shutil.which("git")
    if not git:
        raise SystemExit("Git is required to read release resources")
    contents = {
        name: subprocess.check_output(  # noqa: S603 -- fixed tag and tracked release paths
            [git, "-C", str(ROOT), "show", f"{TAG}:{name}"]
        )
        for name in FILES
    }
    pins: dict[str, str] = runpy.run_path(str(ROOT / "scripts/memini-server.py"))["COMPOSE_FILES"]
    for name, expected in pins.items():
        if hashlib.sha256(contents[name]).hexdigest() != expected:
            raise ValueError(f"{TAG}:{name} does not match the server manager's pinned release")
    contents["memini-server.py"] = (
        (ROOT / "scripts/memini-server.py").read_text(encoding="utf-8").encode("utf-8")
    )
    contents["memini-server.cmd"] = b'@echo off\r\npython "%~dp0memini-server.py" %*\r\n'
    archive = destination / ARCHIVE
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(f"memini-server/{name}")
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    formula = (Path(__file__).with_name("memini-server.rb.in")).read_text()
    (destination / "memini-server.rb").write_text(
        formula.replace("@URL@", URL)
        .replace("@SHA@", digest)
        .replace("@VERSION@", VERSION)
        # Homebrew upgrades an installed formula of the same version only for a higher revision.
        .replace("@REVISION@", f"  revision {REVISION - 1}\n" if REVISION > 1 else "")
    )
    manifest = {
        "version": VERSION,
        "description": "Manage a local Memini server with Docker Compose",
        "homepage": "https://github.com/npavlov/memini",
        "license": "Apache-2.0",
        "depends": "main/python",
        "url": URL,
        "hash": digest,
        "extract_dir": "memini-server",
        "bin": "memini-server.cmd",
    }
    (destination / "memini-server.json").write_text(json.dumps(manifest, indent=4) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
