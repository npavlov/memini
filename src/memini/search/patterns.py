"""What a query's text becomes in SQL: path globs, LIKE patterns, line-mode regexes, lexical terms.

Pure functions, no database. Every value they produce is sent as a query parameter, never spliced
into SQL text; the escaping here is about the pattern languages (regular expressions, LIKE), so a
glob or a substring means what it says.
"""

import re
from typing import Final

from memini.search.errors import InvalidSearchError

__all__ = [
    "MAX_GLOB_CHARS",
    "MAX_LEXICAL_TERMS",
    "glob_regex",
    "globs_regex",
    "lexical_terms",
    "like_pattern",
    "line_mode",
]

MAX_GLOB_CHARS: Final = 512
MAX_LEXICAL_TERMS: Final = 32
"""Words of a query the lexical retriever looks for; the rest are ignored."""


def _literal(char: str) -> str:
    """``char`` as a Postgres regular expression that matches exactly it.

    ASCII letters and digits stand for themselves. Anything else is written as its code point
    (``\\u002e``, ``\\U0001f600``), which an ARE reads as that one character whatever it is, so
    no character of a glob can act as regex syntax.
    """
    if char.isascii() and char.isalnum():
        return char
    point = ord(char)
    return f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}"


def glob_regex(glob: str) -> str:
    """A path glob as an anchored Postgres regular expression over a path.

    The rules, close to those of ``.gitignore``:

    - ``*`` matches any run of characters but ``/``; ``?`` one character but ``/``.
    - ``**`` matches any run of characters, ``/`` included. As a whole segment, ``**/`` matches
      zero or more directories: ``src/**/test_*.py`` matches ``src/test_a.py`` and
      ``src/a/b/test_c.py``.
    - A glob with a ``/`` at its start or in its middle is matched from the root of the
      repository: ``src/*.py`` matches ``src/a.py``, not ``lib/src/a.py``. A glob with no ``/``
      but at its end matches at any depth: ``*.py`` matches ``a.py`` and ``src/a.py``.
    - A glob that matches a directory matches everything below it: ``src/memini`` and
      ``src/memini/`` match ``src/memini/search/model.py``; ``tests`` matches every path with a
      directory named ``tests``.
    - Every other character stands for itself, ``[``, ``]``, ``{`` and ``}`` included; matching is
      case-sensitive. For alternatives, give several globs.

    Raises:
        InvalidSearchError: the glob is empty, only slashes, longer than :data:`MAX_GLOB_CHARS`,
            or holds a NUL or a line break.
    """
    if not glob:
        raise InvalidSearchError("a path glob is a non-empty string")
    if len(glob) > MAX_GLOB_CHARS:
        raise InvalidSearchError(f"a path glob is at most {MAX_GLOB_CHARS} characters")
    if any(char in glob for char in "\x00\n\r"):
        raise InvalidSearchError("a path glob holds a NUL or a line break")
    body = glob.strip("/")
    if not body:
        raise InvalidSearchError("a path glob names no path")
    rooted = glob.startswith("/") or "/" in body
    parts: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "*":
            end = i
            while end < len(body) and body[end] == "*":
                end += 1
            if end - i == 1:
                parts.append("[^/]*")
            elif end < len(body) and body[end] == "/" and (i == 0 or body[i - 1] == "/"):
                parts.append("(?:.*/)?")  # "**/" as a whole segment: zero or more directories
                end += 1
            else:
                parts.append(".*")
            i = end
            continue
        if char == "?":
            parts.append("[^/]")
        elif char == "/":
            parts.append("/")
        else:
            parts.append(_literal(char))
        i += 1
    return ("^" if rooted else "(?:^|/)") + "".join(parts) + "(?:/.*)?$"


def globs_regex(globs: tuple[str, ...]) -> str:
    """One regular expression that matches a path when any of ``globs`` does."""
    if len(globs) == 1:
        return glob_regex(globs[0])
    return "|".join(f"(?:{glob_regex(glob)})" for glob in globs)


def like_pattern(text: str) -> str:
    """A ``LIKE`` pattern that matches content holding ``text`` anywhere, as a substring.

    ``%``, ``_`` and the escape character ``\\`` in ``text`` are escaped, so they match
    themselves.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_EMBEDDED_OPTIONS: Final = re.compile(r"\(\?([bceimnpqstwx]*)\)")


def line_mode(pattern: str) -> str:
    """``pattern`` with Postgres's newline-sensitive option (``n``) in front, as grep reads one.

    Then ``^`` and ``$`` match at the start and end of every line, and ``.`` and a bracket
    expression with ``^`` do not match a line break (``\\n`` in the pattern still does). A
    pattern that starts with embedded options, ``(?i)`` say, keeps them, after the ``n``, so it
    may turn line mode off again with ``(?s)``. A pattern that starts with a director (``***=``,
    ``***:``) is left as it is.
    """
    if pattern.startswith("***"):
        return pattern
    options = _EMBEDDED_OPTIONS.match(pattern)
    if options is not None:
        return f"(?n{options.group(1)}){pattern[options.end() :]}"
    return f"(?n){pattern}"


def lexical_terms(text: str) -> list[str]:
    """The words of ``text`` the lexical retriever looks for: split on white space, each once,
    in order, at most :data:`MAX_LEXICAL_TERMS`.

    Each word is parsed by Postgres again (``plainto_tsquery``), so ``water_level`` looks for
    both its parts, together, as the stored text of that identifier holds them. The words are
    alternatives: a chunk needs only one of them to be found, and ranks higher the more it holds.
    """
    return list(dict.fromkeys(text.split()))[:MAX_LEXICAL_TERMS]
