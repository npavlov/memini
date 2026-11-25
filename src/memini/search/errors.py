"""Why a search did not run. :class:`memini.index.IndexMismatchError` is the other refusal: the
index was built with another fingerprint (principle 9)."""

from typing import Literal

__all__ = [
    "InvalidSearchError",
    "SearchError",
    "SearchTimeoutError",
    "UnknownNameError",
    "UnsupportedFilterError",
]


class SearchError(Exception):
    """A search could not run as asked. The message says why; it never quotes stored content."""


class InvalidSearchError(SearchError, ValueError):
    """The query or a filter is not valid: an empty text, an unknown kind, a bad regular
    expression. The caller's mistake, to be reported back to it."""


class UnsupportedFilterError(InvalidSearchError):
    """A filter the query type accepts but this version cannot apply yet."""


class UnknownNameError(InvalidSearchError):
    """A ``projects``, ``domains`` or ``services`` filter names something the catalog of the
    workspaces searched does not hold.

    An error, not an empty answer: a search of a domain that does not exist would read as "this
    domain has no such code". The message says which filter and which of its values (by
    position, never the value), and where the names are listed.
    """

    def __init__(self, kind: Literal["project", "domain", "service"], position: int) -> None:
        within = "" if kind == "project" else " in the projects searched"
        super().__init__(
            f"value {position} of the {kind}s filter names no {kind} of the catalog{within};"
            " get_catalog lists the names"
        )
        self.kind = kind
        self.position = position


class SearchTimeoutError(SearchError):
    """A statement of the search ran longer than the limit and was stopped.

    Usually a regular expression that backtracks without end, or a pattern too short for the
    trigram index on a large store. Nothing is returned; a narrower query may succeed.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__(
            f"the search was stopped after {timeout:g} s (search_timeout); narrow it with filters"
            " or a more specific pattern"
        )
        self.timeout = timeout
