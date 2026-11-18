"""Migration files on disk: naming and numbering. No database needed."""

from pathlib import Path

import pytest

from memini.db import MigrationError, load_migrations


def test_shipped_migrations_are_numbered_from_one() -> None:
    migrations = load_migrations()

    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].file_name == "0001_extensions.sql"
    assert all(m.sql.strip() for m in migrations)


def test_files_load_in_version_order(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;\n")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "README.txt").write_text("not a migration\n")

    assert [m.file_name for m in load_migrations(tmp_path)] == ["0001_first.sql", "0002_second.sql"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (["0001_a.sql", "0003_c.sql"], "0003_c.sql: expected version 0002"),
        (["0001_a.sql", "0001_b.sql"], "expected version 0002"),
        (["0002_b.sql"], "expected version 0001"),
        (["1_a.sql"], "named NNNN_name.sql"),
        (["0001-Extensions.sql"], "named NNNN_name.sql"),
    ],
)
def test_bad_numbering_or_names_are_refused(tmp_path: Path, files: list[str], message: str) -> None:
    for name in files:
        (tmp_path / name).write_text("SELECT 1;\n")

    with pytest.raises(MigrationError, match=message):
        load_migrations(tmp_path)
