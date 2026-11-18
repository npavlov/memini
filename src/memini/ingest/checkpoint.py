"""How a window claim moves a stream's checked range: the checkpoint forward, the frontier back.

A stream's checked range is ``[checked_from, checked_until)``: everything of the stream in it is
stored. ``checked_until`` is the checkpoint, where the next forward window starts (less an overlap
margin); ``checked_from`` is the history frontier, where the next step of older history ends
(:mod:`memini.intake`). Only a complete window moves either, and only so that the range stays one
interval with nothing unchecked in it:

- The first complete window of a stream sets the range to the window.
- A complete window that starts after the checkpoint would count the gap as checked: refused
  (:class:`~memini.ingest.WindowGapError`). One that ends after it moves the checkpoint to its end,
  with the claim's cursor.
- A complete window that ends at or after the frontier moves the frontier back to its start, if
  that is earlier. One that ends before the frontier leaves a gap: it stores its records and
  leaves the frontier where it was. The cursor belongs to the checkpoint and does not change here.
- Neither bound ever moves the other way, and an incomplete window moves nothing.

A stream whose start was never recorded (``checked_from`` NULL with a checkpoint: a checkpoint set
before the frontier existed) is taken to start at its checkpoint, so the first complete window
that reaches the checkpoint records its start.

The ingest transaction (:mod:`memini.ingest.store`) applies the result with a compare-and-set on
the stream's row, together with the records of the window (principle 7).
"""

from collections.abc import Mapping
from typing import Any

from memini.ingest.model import Checkpoint, WindowClaim, WindowGapError

__all__ = ["advance_checkpoint"]


def advance_checkpoint(
    before: Checkpoint, window: WindowClaim, cursor: Mapping[str, Any] | None
) -> Checkpoint | None:
    """The stream's checkpoint after it stores ``window``, or ``None`` when nothing moves.

    ``before`` is the stream's checkpoint as its locked row holds it, ``cursor`` the claim's cursor
    as it is stored. See the module for the rule.

    Raises:
        WindowGapError: ``window`` is complete and starts after ``before.checked_until``.
    """
    if not window.complete:
        return None
    until = before.checked_until
    if until is None:
        return Checkpoint(checked_until=window.end, cursor=cursor, checked_from=window.start)
    if window.start > until:
        raise WindowGapError(until)

    checked_until, moved_cursor = until, before.cursor
    if window.end > until:
        checked_until, moved_cursor = window.end, cursor
    frontier = until if before.checked_from is None else before.checked_from
    checked_from = before.checked_from
    if window.end >= frontier:
        checked_from = min(window.start, frontier)

    if checked_until == until and checked_from == before.checked_from:
        return None
    return Checkpoint(checked_until=checked_until, cursor=moved_cursor, checked_from=checked_from)
