"""Build and exercise the per-user x64 installer on windows-2022 with CPython 3.13."""

# ruff: noqa: S603 -- release tools and installed binaries are invoked with argument vectors.

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON_URL = "https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip"
PYTHON_SHA = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
DISTLIB_URL = (
    "https://files.pythonhosted.org/packages/02/08/"
    "9c41fb51ab5b43eb21674aff13df270e8ba6c4b29c8624e328dc7a9482af/"
    "distlib-0.4.3-py2.py3-none-any.whl"
)
DISTLIB_SHA = "4b0ce306c966eb73bc3a7b6abad017c556dadd92c44701562cd528ac7fde4d5b"
SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\NikPavlov.Memini_is1"


def versions(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)(?:(?:-rc\.|rc)(\d+))?", value)
    if not match:
        raise ValueError("expected X.Y.Z or X.Y.Z-rc.N")
    base, rc = match.groups()
    return (base, base) if rc is None else (f"{base}-rc.{rc}", f"{base}rc{rc}")


def unpack(url: str, checksum: str, directory: Path, archive: Path) -> None:
    with urllib.request.urlopen(url, timeout=90) as response, archive.open("wb") as file:  # noqa: S310 - pinned HTTPS artifacts
        shutil.copyfileobj(response, file)
    with archive.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != checksum:
            raise ValueError(f"unexpected SHA-256 for {archive.name}")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(directory)


def verify_wheels(report: dict, lock: dict) -> None:
    def identity(name: str, version: str, url: str) -> tuple[str, str, str]:
        filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
        return re.sub(r"[-_.]+", "-", name).lower(), version, filename

    approved = {
        identity(pkg["name"], pkg["version"], wheel["url"]): wheel["hash"]
        for pkg in lock["package"]
        for wheel in pkg.get("wheels", [])
    }
    for entry in report["install"]:
        metadata, source = entry["metadata"], entry["download_info"]
        if metadata["name"].lower() == "memini":
            continue
        key = identity(metadata["name"], metadata["version"], source["url"])
        if approved.get(key) != "sha256:" + source["archive_info"]["hashes"]["sha256"]:
            raise ValueError(f"dependency wheel does not match uv.lock: {key[2]}")


# How long a release's installer build waits for PyPI's index to list the version the release run
# has just published: the simple index can lag the upload by a minute or more.
PYPI_WAIT_SECONDS = 600


def wait_for_pypi(package: str) -> None:
    """Return once PyPI's simple index lists memini ``package``; fail after the wait."""
    url = "https://pypi.org/simple/memini/"
    deadline = time.monotonic() + PYPI_WAIT_SECONDS
    while True:
        request = urllib.request.Request(
            url, headers={"Accept": "application/vnd.pypi.simple.v1+json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
                if package in json.load(answer).get("versions", []):
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"memini {package} did not appear on PyPI in {PYPI_WAIT_SECONDS}s")
        time.sleep(15)


def payload(temp: Path, package: str, wheel: Path | None) -> Path:
    root = temp / "payload"
    runtime = root / "runtime"
    library = runtime / "Lib" / "site-packages"
    unpack(PYTHON_URL, PYTHON_SHA, runtime, temp / "python.zip")
    (runtime / "python313._pth").write_text(
        "python313.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii"
    )
    lock = tomllib.loads((HERE.parents[1] / "uv.lock").read_text(encoding="utf-8"))
    constraints = temp / "constraints.txt"
    constraints.write_text(
        "".join(f"{p['name']}=={p['version']}\n" for p in lock["package"] if p["name"] != "memini"),
        encoding="utf-8",
    )
    if not wheel:
        wait_for_pypi(package)
    # PyPI's index pages are cached apart from its JSON API: pip can still miss a version the
    # API lists, so the install is tried again until the wait is over.
    deadline = time.monotonic() + PYPI_WAIT_SECONDS
    while True:
        attempt = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--isolated",
                "install",
                "--index-url",
                "https://pypi.org/simple",
                "--only-binary=:all:",
                "--no-compile",
                "--target",
                library,
                "-c",
                constraints,
                "--report",
                temp / "pip-report.json",
                wheel.resolve() if wheel else f"memini=={package}",
            ],
            check=False,
        )
        if attempt.returncode == 0:
            break
        if wheel or time.monotonic() > deadline:
            raise subprocess.CalledProcessError(attempt.returncode, attempt.args)
        time.sleep(30)
    report = json.loads((temp / "pip-report.json").read_text(encoding="utf-8"))
    verify_wheels(report, lock)
    selected = next(p for p in report["install"] if p["metadata"]["name"].lower() == "memini")
    if selected["metadata"]["version"] != package:
        raise ValueError("wheel version does not match the requested release")
    source = selected["download_info"]
    provenance = {
        "package": "memini",
        "version": package,
        "source": "checkout-built wheel" if wheel else "PyPI",
        "wheel": Path(urllib.parse.urlsplit(source["url"]).path).name,
        "wheel_sha256": source["archive_info"]["hashes"]["sha256"],
        "packaging_commit": subprocess.check_output(
            [shutil.which("git") or "git", "rev-parse", "HEAD"], cwd=HERE, text=True
        ).strip(),
        "python": PYTHON_URL,
        "python_sha256": PYTHON_SHA,
    }
    (runtime / "windows-build.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    unpack(DISTLIB_URL, DISTLIB_SHA, temp / "tools", temp / "distlib.whl")
    sys.path.insert(0, str(temp / "tools"))
    from distlib.scripts import ScriptMaker

    bin_dir = root / "bin"
    bin_dir.mkdir()
    maker = ScriptMaker(None, str(bin_dir))
    maker.variants = {""}
    maker.executable = r"<launcher_dir>\..\runtime\python.exe"
    maker.make("memini = memini.cli:main", options={"interpreter_args": ["-B"]})
    return root


def manifest(release: str, installer: Path) -> str:
    with installer.open("rb") as file:
        checksum = hashlib.file_digest(file, "sha256").hexdigest().upper()
    return f"""PackageIdentifier: NikPavlov.Memini
PackageVersion: {release}
PackageLocale: en-US
Publisher: Nik Pavlov
PackageName: Memini CLI
License: Apache-2.0
LicenseUrl: https://github.com/npavlov/memini/blob/v{release}/LICENSE
ShortDescription: Command-line client for the Memini knowledge service.
PackageUrl: https://github.com/npavlov/memini
InstallerType: inno
Scope: user
Installers:
  - Architecture: x64
    InstallerUrl: https://github.com/npavlov/memini/releases/download/v{release}/{installer.name}
    InstallerSha256: {checksum}
    ProductCode: NikPavlov.Memini_is1
    AppsAndFeaturesEntries:
      - DisplayName: Memini CLI
        DisplayVersion: {versions(release)[1]}
ManifestType: singleton
ManifestVersion: 1.9.0
"""


def smoke(bin_dir: Path, package: str, cwd: Path) -> None:
    api_token = "memini_" + secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if (
                self.path != "/v1/workspaces"
                or self.headers.get("Authorization") != f"Bearer {api_token}"
            ):
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "items": [
                            {
                                "name": "install-smoke",
                                "created_at": "2026-01-01T00:00:00Z",
                                "id": "smoke",
                            }
                        ]
                    }
                ).encode()
            )

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MEMINI_", "PYTHON"))}
    # Only the installed CLI and OS tools are discoverable during the smoke check.
    env["PATH"] = os.pathsep.join((str(bin_dir), str(Path(env["SYSTEMROOT"]) / "System32")))
    cli = str(bin_dir / "memini.exe")
    version = subprocess.check_output([cli, "--version"], cwd=cwd, env=env, text=True).strip()
    if version != f"memini {package}":
        raise RuntimeError(f"unexpected installed version: {version}")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env.update(
                MEMINI_URL=f"http://127.0.0.1:{server.server_port}",
                MEMINI_TOKEN=api_token,
                NO_PROXY="127.0.0.1",
            )
            rows = subprocess.check_output(
                [cli, "workspace", "list"], env=env, cwd=cwd, text=True, timeout=60
            )
            if "install-smoke" not in rows:
                raise RuntimeError("installed CLI did not return the smoke workspace")
        finally:
            server.shutdown()
            thread.join()


def user_value(key: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return str(winreg.QueryValueEx(handle, name)[0])
    except FileNotFoundError:
        return None


def compile_setup(iscc: Path, root: Path, version: str, dest: Path, filename: str) -> Path:
    subprocess.run(
        [
            iscc,
            f"/DPayload={root}",
            f"/DPackageVersion={version}",
            f"/DInstallerName={filename}",
            f"/O{dest}",
            HERE / "memini.iss",
        ],
        check=True,
    )
    return dest / f"{filename}.exe"


def exercise(setup: Path, prior: Path, version: str, temp: Path) -> None:
    home = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Memini"
    bin_dir = home / "bin"
    if home.exists() or user_value(UNINSTALL_KEY, "DisplayName"):
        raise RuntimeError(
            "installer test requires a fresh user without an existing Memini installation"
        )
    collision = temp / "existing" / "runtime"
    collision.mkdir(parents=True)
    note = collision / "unrelated.txt"
    note.write_text("preserve")
    collision_result = subprocess.run([setup, *SILENT, f"/DIR={collision.parent}"], check=False)
    if not collision_result.returncode or note.read_text() != "preserve":
        raise RuntimeError("installer did not preserve an unowned runtime")
    original = user_value("Environment", "Path") or ""
    try:
        subprocess.run([prior, *SILENT], check=True)
        if not (home / "runtime" / "old-package.txt").is_file():
            raise RuntimeError("upgrade fixture failed to install")
        subprocess.run([setup, *SILENT], check=True)
        if (home / "runtime" / "old-package.txt").exists() or user_value(
            UNINSTALL_KEY, "DisplayVersion"
        ) != version:
            raise RuntimeError("upgrade left a stale runtime or registration")
        paths = (user_value("Environment", "Path") or "").lower().split(";")
        if paths.count(str(bin_dir).lower()) != 1:
            raise RuntimeError("missing or duplicate user PATH entry after upgrade")
        smoke(bin_dir, version, temp)
    finally:
        uninstaller = home / "unins000.exe"
        if uninstaller.exists():
            subprocess.run([uninstaller, *SILENT], check=True)
    for _ in range(100):
        if not home.exists():
            break
        time.sleep(0.1)
    if home.exists() or user_value(UNINSTALL_KEY, "DisplayName"):
        raise RuntimeError("uninstall left files or registration")
    if (user_value("Environment", "Path") or "").rstrip(";") != original.rstrip(";"):
        raise RuntimeError("uninstall changed unrelated user PATH entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--wheel", type=Path, help="Explicit checkout-built compatibility wheel for an existing RC"
    )
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 13):
        parser.error("run with x64 CPython 3.13 on a clean Windows runner")
    release, package = versions(args.version)
    iscc = shutil.which("iscc") or str(
        Path(os.environ["PROGRAMFILES(X86)"]) / "Inno Setup 6" / "ISCC.exe"
    )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        temp = Path(scratch)
        root = payload(temp, package, args.wheel)
        installer = compile_setup(
            Path(iscc), root, package, output, f"memini-{release}-windows-x64"
        )
        previous = temp / "previous"
        (previous / "runtime").mkdir(parents=True)
        (previous / "runtime" / "old-package.txt").write_text("upgrade fixture")
        shutil.copytree(root / "bin", previous / "bin")
        prior = compile_setup(Path(iscc), previous, "0.0.0", temp, "upgrade-fixture")
        exercise(installer, prior, package, temp)
        shutil.copyfile(root / "runtime" / "windows-build.json", output / "windows-build.json")
        (output / "NikPavlov.Memini.yaml").write_text(
            manifest(release, installer), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
