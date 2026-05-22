"""Calendar conflict detection."""

from __future__ import annotations

from datetime import datetime

from app.api.v1.schemas.calendar import Event


def check_overlap(start: datetime, end: datetime, existing_events: list[Event]) -> bool:
    """Return True if [start, end] conflicts with any existing event.

    Zero-gap (abutting) boundaries are treated as overlapping:
    a proposed slot from 9:00–10:00 conflicts with an event from 10:00–11:00.
    """
    for event in existing_events:
        if event.start <= end and event.end >= start:
            return True
    return False
