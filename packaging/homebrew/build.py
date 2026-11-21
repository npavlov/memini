# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["packaging==26.3"]
# ///
"""Build a deterministic wheelhouse from a public release and the tag's uv.lock."""

import argparse
import gzip
import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = {"macos-arm64": ("darwin", "arm64"), "linux-x86_64": ("linux", "x86_64")}


def public_bytes(url):
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in (
        "pypi.org",
        "files.pythonhosted.org",
    ):
        raise ValueError(f"unexpected package source {url}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - checked PyPI origins
        return response.read()


def wheel_info(wheel):
    filename = unquote(urlparse(wheel["url"]).path.rsplit("/", 1)[1])
    return filename, wheel["url"], wheel["hash"].removeprefix("sha256:")


def target_tags(target):
    if target == "macos-arm64":
        platforms = list(mac_platforms((14, 0), "arm64"))
    else:
        platforms = [f"manylinux_2_{v}_x86_64" for v in range(28, 4, -1)]
        platforms += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
    tags = [
        *cpython_tags((3, 12), ["cp312"], platforms),
        *compatible_tags((3, 12), "cp312", platforms),
    ]
    return {tag: order for order, tag in enumerate(tags)}


def select_wheels(version, target):
    published = json.loads(public_bytes(f"https://pypi.org/pypi/memini/{version}/json"))
    if published["info"]["project_urls"].get("Repository") != "https://github.com/npavlov/memini":
        raise ValueError("unexpected PyPI project repository")
    (app,) = [
        f
        for f in published["urls"]
        if f["filename"] == f"memini-{version}-py3-none-any.whl" and not f["yanked"]
    ]
    wheels = [(app["filename"], app["url"], app["digests"]["sha256"])]
    distro = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    by_version = {(pkg["name"], pkg["version"]): pkg for pkg in distro}
    exported = subprocess.check_output(
        [  # noqa: S607
            "uv",
            "export",
            "--offline",
            "--frozen",
            "--no-default-groups",
            "--no-emit-project",
            "--no-hashes",
            "--no-annotate",
            "--no-header",
        ],
        cwd=ROOT,
        text=True,
    )
    environment = default_environment()
    system, machine = PLATFORMS[target]
    environment.update(
        sys_platform=system,
        platform_system="Darwin" if system == "darwin" else "Linux",
        platform_machine=machine,
        python_version="3.12",
        python_full_version="3.12.0",
    )
    supported = target_tags(target)
    for line in exported.splitlines():
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        (spec,) = requirement.specifier
        if spec.operator != "==":
            raise ValueError(f"unlocked package: {requirement}")
        candidates = []
        for entry in by_version[requirement.name, spec.version]["wheels"]:
            info = wheel_info(entry)
            tags = parse_wheel_filename(info[0])[3] & supported.keys()
            if tags:
                candidates.append((min(supported[tag] for tag in tags), info))
        if not candidates:
            raise ValueError(f"no compatible wheel for {requirement} on {target}")
        wheels.append(min(candidates)[1])
    return sorted(wheels)


def build(version, target, output):
    root = f"memini-{version}-{target}"
    wheels = select_wheels(version, target)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as folder:
        stage = Path(folder)
        (stage / "wheels").mkdir()

        def fetch(wheel):
            name, url, digest = wheel
            data = public_bytes(url)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"SHA-256 mismatch for {name}")
            (stage / "wheels" / name).write_bytes(data)

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(fetch, wheels))
        requirements = []
        for name, _, digest in wheels:
            package, number, _, _ = parse_wheel_filename(name)
            requirements.append(f"{package}=={number} --hash=sha256:{digest}")
        (stage / "requirements.txt").write_text("\n".join(requirements) + "\n")
        dest = stage / f"{root}.tar.gz"
        with (
            dest.open("wb") as stream,
            gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=0) as gz,
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as archive,
        ):
            for file in [stage / "requirements.txt", *sorted((stage / "wheels").iterdir())]:
                info = tarfile.TarInfo(f"{root}/{file.relative_to(stage)}")
                info.size = file.stat().st_size
                with file.open("rb") as content:
                    archive.addfile(info, content)
        os.replace(dest, output / dest.name)
    print(output / f"{root}.tar.gz")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", choices=PLATFORMS)
    args = parser.parse_args()
    if (
        str(Version(args.version)) != args.version
        or tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        != args.version
    ):
        parser.error("version must match project.version in the release checkout")
    native = {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), "unsupported")
    target = args.target or f"{native}-{platform.machine()}"
    if target not in PLATFORMS:
        parser.error(f"unsupported target {target}")
    build(args.version, target, args.output)
