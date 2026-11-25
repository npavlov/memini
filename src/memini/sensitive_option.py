"""The ``include_sensitive`` argument of the reads that can reach records marked sensitive
(:mod:`memini.sensitivity`). One definition, so every tool and route says the same."""

from typing import Any

from pydantic import Field

__all__ = ["INCLUDE_SENSITIVE_DESCRIPTION", "include_sensitive_field"]

INCLUDE_SENSITIVE_DESCRIPTION = (
    "Also read records marked sensitive (a salary, a diagnosis), which are left out by default."
    " Needs a token with the sensitive scope; without it the request is refused."
)


def include_sensitive_field() -> Any:
    """The field: false by default."""
    return Field(default=False, strict=False, description=INCLUDE_SENSITIVE_DESCRIPTION)
