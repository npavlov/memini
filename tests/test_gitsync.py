"""Unit tests of the git connector's sync (memini.gitsync) that need no database: which files are
indexed, record identity, the pin, and which handlers ``serve`` registers. Values are synthetic."""

import io
import uuid
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from memini.cli.serve import job_handlers
from memini.config import Settings
from memini.db import Pool
from memini.embeddings import EmbeddingError
from memini.git import GitOptions
from memini.gitsync import (
    BUILTIN_EXCLUDE,
    DOCUMENT_SUFFIXES,
    GIT_SYNC,
    FileKind,
    FileRules,
    Pin,
    SyncLimits,
    code_external_id,
    code_record_id,
    file_kind,
    file_record_id,
    mirrors_unusable,
    read_pin,
    split_code_external_id,
)
from memini.gitsync import rules as file_rules
from memini.ids import hash_fields, record_id
from memini.removal import RETENTION
from memini.sources import GitConfig

LIMIT = 1000
COMMIT = "a" * 40


def rules(include: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> FileRules:
    return FileRules(GitConfig(location="", include=include, exclude=exclude), LIMIT)


@pytest.mark.parametrize(
    ("path", "mode", "size", "reason"),
    [
        ("src/app.py", "100644", 10, None),
        ("bin/tool", "100755", 10, None),
        ("src/app.py", "100644", LIMIT, None),
        ("src/app.py", "100644", LIMIT + 1, "too_large"),
        ("link.py", "120000", 10, "not_a_file"),
        ("vendor/lib", "160000", None, "not_a_file"),
        ("caf\udce9.py", "100644", 10, "bad_path"),
        ("package-lock.json", "100644", 10, "excluded"),
        ("web/yarn.lock", "100644", 10, "excluded"),
        ("node_modules/left/index.js", "100644", 10, "excluded"),
        ("web/node_modules/left/index.js", "100644", 10, "excluded"),
        ("static/app.min.js", "100644", 10, "excluded"),
        ("static/app.js.map", "100644", 10, "excluded"),
        ("go.sum", "100644", 10, "excluded"),
        ("static/app.js", "100644", 10, None),
        ("docs/lockfile.md", "100644", 10, None),
    ],
)
def test_skip_reason_by_type_path_and_size(
    path: str, mode: str, size: int | None, reason: str | None
) -> None:
    assert rules().skip_reason(path, mode, size) == reason


def test_the_source_globs_select_files() -> None:
    selected = rules(include=("src/**", "*.md"), exclude=("**/test_*.py",))

    assert selected.skip_reason("src/app.py", "100644", 1) is None
    assert selected.skip_reason("README.md", "100644", 1) is None
    assert selected.skip_reason("src/test_app.py", "100644", 1) == "excluded"
    assert selected.skip_reason("scripts/run.py", "100644", 1) == "excluded"
    assert selected.skip_reason("docs/guide.md", "100644", 1) == "excluded"


def test_builtin_excludes_can_be_switched_off_per_source() -> None:
    off = FileRules(GitConfig(location="", builtin_excludes=False), LIMIT)
    own = FileRules(
        GitConfig(location="", exclude=("**/yarn.lock",), builtin_excludes=False), LIMIT
    )

    assert off.skip_reason("package-lock.json", "100644", 10) is None
    assert off.skip_reason("web/node_modules/left/index.js", "100644", 10) is None
    assert off.skip_reason("static/app.min.js", "100644", 10) is None
    assert own.skip_reason("web/yarn.lock", "100644", 10) == "excluded"  # the source's own
    assert own.skip_reason("go.sum", "100644", 10) is None
    assert off.digest != rules().digest
    assert off.digest == FileRules(GitConfig(location="/b", builtin_excludes=False), LIMIT).digest


def test_a_source_that_keeps_the_builtin_excludes_keeps_its_rules_digest() -> None:
    # The digest as it was before the switch existed: no full listing after an upgrade.
    def counted(values: tuple[str, ...]) -> list[bytes]:
        return [str(len(values)).encode(), *(value.encode() for value in values)]

    before = hash_fields(
        file_rules.RULES_DOMAIN,
        str(LIMIT).encode(),
        *counted(()),
        *counted(()),
        *counted(BUILTIN_EXCLUDE),
        *counted(DOCUMENT_SUFFIXES),
    )

    assert rules().digest == before


def test_the_rules_digest_changes_with_every_rule() -> None:
    digests = {
        rules().digest,
        rules(include=("src/**",)).digest,
        rules(exclude=("src/**",)).digest,
        rules(include=("a", "b")).digest,
        rules(include=("a",), exclude=("b",)).digest,
        FileRules(GitConfig(location=""), LIMIT + 1).digest,
    }

    assert len(digests) == 6
    assert rules(include=("src/**",)).digest == rules(include=("src/**",)).digest
    # The location is not a rule: a source's files do not depend on where it is fetched from.
    assert FileRules(GitConfig(location="/a"), LIMIT).digest == rules().digest
    assert len(BUILTIN_EXCLUDE) == len(set(BUILTIN_EXCLUDE))


def test_the_rules_digest_names_the_kinds_of_record(monkeypatch: pytest.MonkeyPatch) -> None:
    current = rules().digest
    # Before Markdown files were documents: a pin of then lists the ref again, once.
    monkeypatch.setattr(file_rules, "DOCUMENT_SUFFIXES", ())

    assert rules().digest != current
    assert file_kind("README.md") == "code_file"


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("README.md", "document"),
        ("docs/guide/install.md", "document"),
        ("CHANGES.markdown", "document"),
        ("site/docs/intro.mdx", "document"),
        ("NOTES.MD", "document"),
        ("docs/Setup.Markdown", "document"),
        ("src/app.py", "code_file"),
        ("README", "code_file"),
        ("README.txt", "code_file"),
        ("docs/guide.rst", "code_file"),
        ("md/readme", "code_file"),
        ("docs.md/index.html", "code_file"),
    ],
)
def test_markdown_files_are_documents(path: str, kind: FileKind) -> None:
    assert file_kind(path) == kind
    assert all(suffix == suffix.lower() for suffix in DOCUMENT_SUFFIXES)


def test_a_code_record_is_named_by_its_ref_and_path() -> None:
    source = uuid.UUID("00000000-0000-7000-8000-000000000001")

    assert code_external_id("refs/heads/main", "src/app.py") == "refs/heads/main:src/app.py"
    assert split_code_external_id("refs/tags/v1:src/a:b.py") == ("refs/tags/v1", "src/a:b.py")
    assert code_record_id(source, "refs/heads/main", "src/app.py") == record_id(
        source, "code_file", "refs/heads/main:src/app.py"
    )
    assert code_record_id(source, "refs/heads/main", "a") != code_record_id(
        source, "refs/heads/dev", "a"
    )
    # A file's record is of the kind the file becomes; the external id is the same for both.
    assert file_record_id(source, "refs/heads/main", "src/app.py") == code_record_id(
        source, "refs/heads/main", "src/app.py"
    )
    assert file_record_id(source, "refs/heads/main", "README.md") == record_id(
        source, "document", "refs/heads/main:README.md"
    )
    assert file_record_id(source, "refs/heads/main", "README.md") != code_record_id(
        source, "refs/heads/main", "README.md"
    )
    for ref, path in (("main", "a"), ("refs/heads/a:b", "c"), ("refs/heads/main", "")):
        with pytest.raises(ValueError, match="a code record needs"):
            code_external_id(ref, path)
    for external_id in ("main:src/app.py", "refs/heads/main", "refs/heads/main:", "note-1"):
        with pytest.raises(ValueError, match="not the external id of a code record"):
            split_code_external_id(external_id)


def test_the_pin_reads_back_what_it_writes_and_ignores_anything_else() -> None:
    pin = Pin(COMMIT, "r" * 64)

    assert pin.to_json() == {"commit": COMMIT, "rules": "r" * 64}
    assert read_pin(pin.to_json()) == pin
    partial = Pin(COMMIT, "r", partial=True)
    assert partial.to_json() == {"commit": COMMIT, "rules": "r", "partial": True}
    assert read_pin(partial.to_json()) == partial
    cursors: list[object] = [
        None,
        "a" * 40,
        ["commit"],
        {},
        {"commit": COMMIT},
        {"commit": "main", "rules": "r"},
        {"commit": COMMIT.upper(), "rules": "r"},
        {"commit": COMMIT, "rules": 1},
    ]
    for cursor in cursors:
        assert read_pin(cursor) is None


def test_sync_limits_need_at_least_one_file_and_byte() -> None:
    with pytest.raises(ValueError, match="at least one file"):
        SyncLimits(batch_files=0)
    with pytest.raises(ValueError, match="at least one file"):
        SyncLimits(batch_bytes=0)


# --- Registering the handler ---------------------------------------------------------------------


def test_mirrors_are_usable_with_git_and_a_writable_directory(tmp_path: Path) -> None:
    options = GitOptions(mirror_dir=tmp_path / "deep" / "mirrors")

    assert mirrors_unusable(options) is None
    assert options.mirror_dir.is_dir()

    no_git = GitOptions(mirror_dir=tmp_path / "m", git="/nonexistent/git")
    problem = mirrors_unusable(no_git)
    assert problem is not None
    assert "git executable" in problem

    (tmp_path / "file").write_text("not a directory")
    problem = mirrors_unusable(GitOptions(mirror_dir=tmp_path / "file" / "mirrors"))
    assert problem is not None
    assert "cannot be created" in problem


NO_POOL = cast(Pool, object())  # the handlers only keep it; nothing here connects


def embedding_settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "database_url": SecretStr("postgresql://fake:fake@db.invalid:5432/memini"),
        "embedding_provider": "openai_compatible",
        "embedding_model": "fake-embedder",
        "embedding_dimension": 8,
        "embedding_url": "http://127.0.0.1:9/v1",
        **changes,
    }
    return Settings.model_validate(values)


@pytest.mark.anyio
async def test_serve_registers_git_sync_with_embeddings_and_usable_mirrors(
    tmp_path: Path,
) -> None:
    settings = embedding_settings(git_mirror_dir=tmp_path / "mirrors")

    async with job_handlers(settings, NO_POOL) as handlers:
        assert set(handlers) == {GIT_SYNC, RETENTION}


@pytest.mark.anyio
async def test_serve_does_not_sync_git_without_embeddings(
    settings: Settings, log_output: io.StringIO
) -> None:
    async with job_handlers(settings, NO_POOL) as handlers:
        assert set(handlers) == {RETENTION}  # retention needs no embeddings

    assert "git sources are not synced: no embedding provider is configured" in (
        log_output.getvalue()
    )


@pytest.mark.anyio
async def test_serve_does_not_sync_git_when_mirrors_cannot_be_kept(
    tmp_path: Path, log_output: io.StringIO
) -> None:
    (tmp_path / "file").write_text("not a directory")
    settings = embedding_settings(git_mirror_dir=tmp_path / "file" / "mirrors")

    async with job_handlers(settings, NO_POOL) as handlers:
        assert set(handlers) == {RETENTION}

    assert "git sources are not synced: the mirror directory" in log_output.getvalue()


@pytest.mark.anyio
async def test_serve_refuses_embedding_settings_it_cannot_build(tmp_path: Path) -> None:
    settings = embedding_settings(git_mirror_dir=tmp_path / "mirrors").model_copy(
        update={"embedding_url": None}
    )

    with pytest.raises(EmbeddingError, match="embedding_url"):
        async with job_handlers(settings, NO_POOL):
            pass
