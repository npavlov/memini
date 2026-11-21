"""Render the versioned Markdown as a static site for GitHub Pages. Requires Pandoc."""

import argparse
import html
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def render(page: Path, revision: str, pandoc: str) -> str:
    stylesheet = os.path.relpath(DOCS / "assets" / "guide.css", page.parent)
    result = subprocess.run(  # noqa: S603 -- local executable, fixed options and docs input
        [
            pandoc,
            "--from=gfm",
            "--to=html5",
            "--standalone",
            "--css",
            stylesheet,
            "--metadata=lang:en",
            "--metadata",
            f"pagetitle={page.read_text().splitlines()[0].removeprefix('# ')}",
            str(page),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    def link(match: re.Match[str]) -> str:
        url = urlsplit(html.unescape(match[1]))
        if url.scheme or url.netloc or not url.path:
            return match[0]
        path = (page.parent / unquote(url.path)).resolve()
        if not path.exists() or not path.is_relative_to(ROOT):
            raise ValueError(f"{page.relative_to(ROOT)}: missing link {url.path}")
        if path.is_relative_to(DOCS) and path.is_file():
            target = path.with_suffix(".html") if path.suffix == ".md" else path
            href = quote(os.path.relpath(target, page.parent))
        else:
            view = "tree" if path.is_dir() else "blob"
            repo_path = quote(path.relative_to(ROOT).as_posix())
            href = f"https://github.com/npavlov/memini/{view}/{revision}/{repo_path}"
        return f'href="{html.escape(urlunsplit(("", "", href, url.query, url.fragment)))}"'

    rendered = re.sub(r'href="([^"]*)"', link, result.stdout)
    head, body = rendered.split("<body>", 1)
    article, end = body.split("</body>", 1)
    index = os.path.relpath(DOCS / "index.html", page.parent)
    menu: list[str] = []
    for target, label in (
        ("install.md", "Install"),
        ("intake.md", "Intake"),
        ("mcp/README.md", "MCP tools"),
        ("operations/runbook.md", "Operations"),
    ):
        href = os.path.relpath((DOCS / target).with_suffix(".html"), page.parent)
        current = ' aria-current="page"' if page == DOCS / target else ""
        menu.append(f'<a href="{href}"{current}>{label}</a>')
    header = (
        '<a class="skip" href="#guide-content">Skip to content</a>'
        '<header class="masthead"><div class="masthead-inner">'
        f'<a class="brand" href="{index}">Memini <span>Docs</span></a>'
        f'<nav aria-label="Documentation">{"".join(menu)}</nav></div></header>'
    )
    headings = re.findall(r'<h2\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</h2>', article, re.DOTALL)
    sidebar = ""
    if len(headings) > 2:
        links: list[str] = []
        for anchor, title in headings:
            label = re.sub(r"<[^>]*>", "", title)
            links.append(f'<li><a href="#{anchor}">{label}</a></li>')
        contents = f"<ul>{''.join(links)}</ul>"
        sidebar = (
            '<aside class="contents" aria-label="Page sections">'
            f"<details><summary>On this page</summary>{contents}</details>"
            f'<nav aria-label="On this page"><strong>On this page</strong>{contents}</nav></aside>'
        )
    article = article.replace("<table", '<div class="table-scroll"><table').replace(
        "</table>", "</table></div>"
    )
    return (
        f'{head}<body>{header}<div class="page">{sidebar}'
        f'<main id="guide-content">{article}</main></div></body>{end}'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new or empty output directory")
    output = parser.parse_args().output.resolve()
    if output.is_relative_to(DOCS) or (output.exists() and any(output.iterdir())):
        parser.error("choose a new or empty directory outside docs/")
    pandoc, git = shutil.which("pandoc"), shutil.which("git")
    if pandoc is None or git is None:
        parser.error("install Pandoc 3 and Git to build the guide")
    revision = subprocess.check_output(  # noqa: S603 -- read the current checkout revision
        [git, "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    output.mkdir(parents=True, exist_ok=True)
    for source in sorted(DOCS.rglob("*")):
        if not source.is_file():
            continue
        asset = source.is_relative_to(DOCS / "assets") and source.suffix in {".css", ".png"}
        if source.suffix not in {".md", ".json"} and not asset:
            continue
        dest = output / source.relative_to(DOCS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".md":
            dest.with_suffix(".html").write_text(render(source, revision, pandoc))
        else:
            shutil.copyfile(source, dest)
    (output / ".nojekyll").touch()


if __name__ == "__main__":
    main()
