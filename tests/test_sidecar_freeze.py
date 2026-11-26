"""The release option of the desktop app's sidecar freeze (desktop/sidecar/freeze.py)."""

import hashlib
import importlib.util
import runpy
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "ab12" * 16
IMAGE = f"ghcr.io/npavlov/memini:0.2.0-rc.5@{DIGEST}"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("freeze", ROOT / "desktop/sidecar/freeze.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_release_image_names_its_tag() -> None:
    freeze = load()
    assert freeze.release_tag(IMAGE) == "v0.2.0-rc.5"
    assert freeze.release_tag(f"ghcr.io/npavlov/memini:1.0.0@{DIGEST}") == "v1.0.0"


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/npavlov/memini:0.2.0-rc.5",
        f"ghcr.io/someone/memini:0.2.0-rc.5@{DIGEST}",
        f"docker.io/npavlov/memini:0.2.0-rc.5@{DIGEST}",
        f"ghcr.io/npavlov/memini:latest@{DIGEST}",
        f"ghcr.io/npavlov/memini:0.2.0rc5@{DIGEST}",
        f"ghcr.io/npavlov/memini:0.2.0-rc.5@{DIGEST.upper()}",
        f"ghcr.io/npavlov/memini:0.2.0-rc.5@{DIGEST[:-1]}",
        f"ghcr.io/npavlov/memini:0.2.0-rc.5@{DIGEST}\n",
        f"ghcr.io/npavlov/memini:0.2.0-rc.5@{DIGEST} --pull",
        "",
    ],
)
def test_any_other_image_is_refused(image: str) -> None:
    freeze = load()
    with pytest.raises(ValueError, match="not a release image"):
        freeze.release_tag(image)
    with pytest.raises(ValueError, match="not a release image"):
        freeze.pin_release("", image, {})


def test_the_release_copy_of_the_launcher_pins_the_image_and_its_compose_files(
    tmp_path: Path,
) -> None:
    freeze = load()
    source = (ROOT / "scripts/memini-server.py").read_text(encoding="utf-8")
    pinned = runpy.run_path(str(ROOT / "scripts/memini-server.py"))
    compose = {name: f"# {name} of the release\n".encode() for name in pinned["COMPOSE_FILES"]}

    rewritten = freeze.pin_release(source, IMAGE, compose)
    copy = tmp_path / "memini-server.py"
    copy.write_text(rewritten, encoding="utf-8")
    released = runpy.run_path(str(copy))

    assert released["IMAGE"] == IMAGE
    assert released["COMPOSE_FILES"] == {
        name: hashlib.sha256(data).hexdigest() for name, data in compose.items()
    }
    # Nothing else of the launcher changes.
    assert released["MODEL"] == pinned["MODEL"]
    assert len(rewritten.splitlines()) == len(source.splitlines())
    assert rewritten.replace(IMAGE.split("@")[0], "").count("ghcr.io/npavlov/memini") == 0


def test_a_launcher_without_one_pin_of_each_is_refused() -> None:
    freeze = load()
    with pytest.raises(ValueError, match="no single pin"):
        freeze.pin_release('IMAGE = "x"\n', IMAGE, {})
