"""Render the tap formula from verified platform archives."""

import argparse
import hashlib
import platform
import re
from pathlib import Path

TARGETS = ("macos-arm64", "linux-x86_64")


def release_tag(version):
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)(?:rc([0-9]+))?", version)
    if not match:
        raise ValueError("expected X.Y.Z or X.Y.ZrcN")
    base, rc = match.groups()
    return f"v{base}" + (f"-rc.{rc}" if rc is not None else "")


def render(version, tag, directory, local=False, revision=0):
    if release_tag(version) != tag:
        raise ValueError("PEP 440 version and Git tag disagree")
    template = (Path(__file__).parent / "memini.rb.in").read_text()
    for target in TARGETS:
        artifact = directory / f"memini-{version}-{target}.tar.gz"
        if local and not artifact.exists():
            native = (
                ("macos" if platform.system() == "Darwin" else "linux") + "-" + platform.machine()
            )
            if target == native:
                raise ValueError(f"missing native archive: {artifact}")
            # The smoke test stages only its native archive and formula stanza.
            template = re.sub(
                rf"  on_{'macos' if target.startswith('macos') else 'linux'} do\n.*?  end\n\n",
                "",
                template,
                flags=re.S,
            )
            continue
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        url = (
            artifact.resolve().as_uri()
            if local
            else (f"https://github.com/npavlov/memini/releases/download/{tag}/{artifact.name}")
        )
        template = template.replace(f"@URL_{target}@", url).replace(f"@SHA_{target}@", digest)
    return template.replace("@VERSION@", version).replace(
        "@REVISION@", f"  revision {revision}\n" if revision else ""
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("tag")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--revision", choices=(0, 1), type=int, default=0)
    args = parser.parse_args()
    print(render(args.version, args.tag, args.directory, args.local, args.revision), end="")
