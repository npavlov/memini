"""The preview of what a git source indexes, by extension and folder (memini.gitsync.preview).

Pure: the entries are made here, as git's tree listing gives them. Paths are synthetic.
"""

from dataclasses import dataclass

from memini.gitsync.preview import OTHER, PreviewGroup, summarize
from memini.gitsync.rules import FileRules
from memini.sources import GitConfig

FILE = "100644"


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str = FILE
    size: int | None = 10


def rules(**config: object) -> FileRules:
    return FileRules(GitConfig(location="/srv/repos/harbour", **config), max_size=100)  # type: ignore[arg-type]


def test_files_are_grouped_by_extension_and_top_folder() -> None:
    entries = [
        Entry("README.md"),
        Entry("Makefile"),
        Entry(".gitignore"),
        Entry("berths/Berth.GO"),
        Entry("berths/sub/plan.go"),
    ]

    preview = summarize(entries, rules(), ref="refs/heads/main", commit="c0ffee")

    assert (preview.tracked, preview.indexed) == (5, 5)
    # Equal counts go by key: "(none)" sorts before ".go".
    assert preview.by_extension == (
        PreviewGroup("(none)", 2, 2),
        PreviewGroup(".go", 2, 2),
        PreviewGroup(".md", 1, 1),
    )
    assert preview.by_folder == (PreviewGroup("(root)", 3, 3), PreviewGroup("berths", 2, 2))


def test_left_out_files_are_tracked_but_not_indexed_with_their_reason() -> None:
    entries = [
        Entry("berths/berth.go"),
        Entry("berths/berth_test.go"),
        Entry("web/node_modules/lib/index.js"),
        Entry("assets/map.png", size=1000),
        Entry("vendor", mode="160000", size=None),  # a submodule is not a file
    ]

    preview = summarize(
        entries, rules(exclude=("**/*_test.go",)), ref="refs/heads/main", commit="c0ffee"
    )

    assert (preview.tracked, preview.indexed) == (4, 1)
    assert preview.skipped == {"excluded": 2, "too_large": 1, "not_a_file": 1}
    assert PreviewGroup("berths", 2, 1) in preview.by_folder


def test_groups_past_the_limit_are_summed_into_one() -> None:
    entries = [Entry(f"f{n}.e{n % 4}") for n in range(10)]

    preview = summarize(entries, rules(), ref="refs/heads/main", commit="c0ffee", limit=2)

    assert preview.by_extension == (
        PreviewGroup(".e0", 3, 3),
        PreviewGroup(".e1", 3, 3),
        PreviewGroup(OTHER, 4, 4),
    )
