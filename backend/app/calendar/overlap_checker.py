"""Calendar conflict detection."""

from __future__ import annotations

from datetime import datetime, timezone

from app.api.v1.schemas.calendar import Event


def _to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to UTC-aware for comparison.

    Naive datetimes are assumed to already represent UTC (GCal returns
    aware datetimes in production; naive ones only appear in tests).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def check_overlap(start: datetime, end: datetime, existing_events: list[Event]) -> bool:
    """Return True if [start, end] conflicts with any existing event.

    Zero-gap (abutting) boundaries are treated as overlapping:
    a proposed slot from 9:00–10:00 conflicts with an event from 10:00–11:00.

    Both the proposed window and existing events are normalised to UTC so that
    naive (test) datetimes and timezone-aware (production) datetimes can be
    compared without raising ``TypeError``.
    """
    start_utc = _to_utc(start)
    end_utc = _to_utc(end)
    for event in existing_events:
        ev_start = _to_utc(event.start)
        ev_end = _to_utc(event.end)
        if ev_start <= end_utc and ev_end >= start_utc:
            return True
    return False
