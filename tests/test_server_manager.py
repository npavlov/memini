import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/memini-server.py"


def load_manager(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("memini_server", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(monkeypatch: pytest.MonkeyPatch, module: ModuleType, state: Path, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--state-dir", str(state), *args])
    assert module.main() == 0


def test_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    launcher = package / SCRIPT.name
    shutil.copyfile(SCRIPT, launcher)
    module = load_manager(launcher)
    # Unit fixtures stay independent of development-branch Compose pins and shallow Git clones.
    contents = b"services: {}\n"
    for name in ("compose.yaml", "compose.release.yaml"):
        (package / name).write_bytes(contents)
        monkeypatch.setitem(module.COMPOSE_FILES, name, hashlib.sha256(contents).hexdigest())
    state = tmp_path / "new server"
    calls: list[tuple[list[str], dict[str, str]]] = []
    cached = False

    def docker(
        argv: list[str], *, env: dict[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal cached
        calls.append((argv, dict(env)))
        response = ""
        if argv[-1] == "list" and cached:
            response = "NAME ID SIZE\nqwen3-embedding:0.6b c690 639MB\n"
        if argv[-2] == "pull":
            cached = True
        if argv[-3:] == ["port", "memini", "6364"]:
            response = "127.0.0.1:36364\n"
        return subprocess.CompletedProcess(argv, 0, response)

    monkeypatch.setattr(subprocess, "run", docker)

    def locate(_name: str) -> str:
        return "/fake/docker"

    monkeypatch.setattr(shutil, "which", locate)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "some-other-project")
    monkeypatch.setenv("MEMINI_DB_PASSWORD", "ambient")
    monkeypatch.delenv("MEMINI_TOKEN", raising=False)
    monkeypatch.delenv("MEMINI_TOKEN_FILE", raising=False)
    invoke(monkeypatch, module, state, "status")
    invoke(monkeypatch, module, state, "stop")
    assert not calls
    assert not state.exists()

    invoke(monkeypatch, module, state, "start", "--http-port", "0")
    settings = (state / ".env").read_bytes()
    assert b"MEMINI_HTTP_PORT=0" in settings
    assert b"ambient" not in settings
    if os.name != "nt":
        assert (state / ".env").stat().st_mode & 0o077 == 0
    invoke(monkeypatch, module, state, "stop")
    assert calls[-1][0][-1] == "stop"
    invoke(monkeypatch, module, state, "start")
    assert sum("pull" in args for args, _ in calls) == 1
    assert (state / ".env").read_bytes() == settings
    assert len({args[args.index("--project-name") + 1] for args, _ in calls}) == 1
    assert all(
        "COMPOSE_PROJECT_NAME" not in env and "MEMINI_DB_PASSWORD" not in env for _, env in calls
    )
    assert all("@sha256:" in env["MEMINI_RELEASE_IMAGE"] for _, env in calls)
    with pytest.raises(ValueError, match="update MEMINI_HTTP_PORT"):
        invoke(monkeypatch, module, state, "start", "--http-port", "12345")

    token = tmp_path / "fixture.token"
    token.write_text("fixture-value\r\n", encoding="utf-16")
    monkeypatch.setenv("MEMINI_TOKEN_FILE", str(token))
    invoke(monkeypatch, module, state, "cli", "--version")
    argv, env = calls[-1]
    assert argv[-5:] == ["-e", "MEMINI_TOKEN", "memini", "memini", "--version"]
    assert env["MEMINI_TOKEN"] == token.read_text(encoding="utf-16").strip()
    assert "fixture-value" not in argv
    assert "MEMINI_TOKEN_FILE" not in env


def test_existing_directory_is_untouched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = load_manager(SCRIPT)
    original = tmp_path / ".env"
    original.write_text("existing project")
    for action in ("start", "stop", "status", "cli"):
        with pytest.raises(ValueError, match="no managed server"):
            invoke(monkeypatch, module, tmp_path, action)
    assert list(tmp_path.iterdir()) == [original]
    assert original.read_text() == "existing project"


def test_packaged_assets_verified_before_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packaged = tmp_path / "distribution"
    packaged.mkdir()
    launcher = packaged / SCRIPT.name
    shutil.copyfile(SCRIPT, launcher)
    (packaged / "compose.yaml").write_text("wrong release")
    state = tmp_path / "state"
    module = load_manager(launcher)
    invoke(monkeypatch, module, state, "path")
    assert capsys.readouterr().out.strip() == str(packaged)
    with pytest.raises(ValueError, match="checksum mismatch"):
        invoke(monkeypatch, module, state, "start")
    assert not state.exists()
