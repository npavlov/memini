"""Fixture data is marked synthetic, and its addresses and links use reserved names only.

Acceptance of memini-3va: a test fails when a fixture directory lacks the synthetic marker. A
fixture set is every directory directly under ``eval/`` and ``tests/fixtures/``; its root holds a
``SYNTHETIC.md`` that starts with ``# SYNTHETIC DATA`` and says the content is invented, and covers
the subdirectories. A data file outside a set fails too, so new fixtures land in a marked set.

A cheap lint for real data on top: every e-mail address and every link host in a fixture uses a
reserved name (RFC 2606, RFC 6761) or a private or loopback address. The secret scan
(.gitleaks.toml, test_secret_scan.py) covers keys and tokens."""

import ipaddress
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASES = (ROOT / "eval", ROOT / "tests" / "fixtures")
MARKER = "SYNTHETIC.md"


def is_data(path: Path) -> bool:
    """A file of fixture data: not the marker, not an editor's or a tool's leftover."""
    rel = path.relative_to(ROOT)
    hidden = any(part.startswith(".") or part == "__pycache__" for part in rel.parts)
    return path.is_file() and path.name != MARKER and not hidden


def fixture_sets() -> list[Path]:
    return sorted(
        path
        for base in BASES
        for path in base.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    )


def data_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if is_data(path))


def name(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_fixture_sets_are_found() -> None:
    names = {name(path) for path in fixture_sets()}
    assert {"eval/code", "eval/comms", "tests/fixtures/chunking", "tests/fixtures/recipes"} <= names
    assert all(data_files(path) for path in fixture_sets())


@pytest.mark.parametrize("fixture_set", fixture_sets(), ids=name)
def test_every_fixture_set_is_marked_synthetic(fixture_set: Path) -> None:
    marker = fixture_set / MARKER
    assert marker.is_file(), f"{name(fixture_set)} has no {MARKER}: add one, saying it is invented"
    text = marker.read_text(encoding="utf-8")
    assert text.startswith("# SYNTHETIC DATA\n"), f"{name(marker)} does not start with the title"
    assert "invented" in text, f"{name(marker)} does not say the content is invented"


def test_no_data_file_lies_outside_a_fixture_set() -> None:
    loose = [name(path) for base in BASES for path in base.iterdir() if is_data(path)]
    assert loose == [], "put fixture data into a directory with a SYNTHETIC.md marker"


# --- Addresses and links -------------------------------------------------------------------------

ADDRESS = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[A-Za-z][\w-]*)")
LINK = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^\s/@\"'<>]*@)?(\[[0-9a-fA-F:.]+\]|[\w.-]+)")
RESERVED_TLDS = ("test", "example", "invalid", "localhost")
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")


def is_reserved(host: str) -> bool:
    host = host.lower().rstrip(".").strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_private or address.is_loopback
    labels = host.split(".")
    if host == "localhost" or labels[-1] in RESERVED_TLDS:
        return True
    return any(host == domain or host.endswith(f".{domain}") for domain in RESERVED_DOMAINS)


@pytest.mark.parametrize(
    ("host", "reserved"),
    [
        ("saltreed.example.test", True),
        ("hearth.example", True),
        ("chat.example.invalid", True),
        ("example.org", True),
        ("wiki.example.com", True),
        ("localhost", True),
        ("127.0.0.1", True),
        ("[::1]", True),
        ("10.0.0.7", True),
        ("github.com", False),
        ("example.com.evil.org", False),
        ("notexample.com", False),
        ("mail.example.co", False),
        ("8.8.8.8", False),
    ],
)
def test_reserved_names(host: str, reserved: bool) -> None:
    assert is_reserved(host) is reserved


def hosts_in(text: str) -> set[str]:
    return set(ADDRESS.findall(text)) | set(LINK.findall(text))


def test_hosts_are_found_in_addresses_and_links() -> None:
    text = (
        'mail ana@harbour.example.test, see https://user@wiki.example.org:8443/x and "ssh://[::1]"'
    )
    assert hosts_in(text) == {"harbour.example.test", "wiki.example.org", "[::1]"}


@pytest.mark.parametrize("fixture_set", fixture_sets(), ids=name)
def test_addresses_and_links_use_reserved_names_only(fixture_set: Path) -> None:
    found: dict[str, set[str]] = {}
    for path in data_files(fixture_set):
        text = path.read_bytes().decode("utf-8", errors="replace")
        for host in hosts_in(text):
            if not is_reserved(host):
                found.setdefault(host, set()).add(name(path))
    assert found == {}, "use example.test, example.org or another reserved name (RFC 2606)"
