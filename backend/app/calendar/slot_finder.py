"""Free slot discovery with user preference scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from app.api.v1.schemas.calendar import Event, TimeSlot
from app.calendar.overlap_checker import check_overlap


@dataclass
class UserPreferences:
    """User-specific scheduling preferences."""

    sleep_start: time = field(default_factory=lambda: time(23, 0))
    sleep_end: time = field(default_factory=lambda: time(7, 0))
    deep_work_start: time | None = None
    deep_work_end: time | None = None


def _minutes_from_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _in_sleep_block(slot_start: datetime, slot_end: datetime, prefs: UserPreferences) -> bool:
    """Return True if the slot overlaps the overnight sleep block.

    The default sleep block (23:00–07:00) crosses midnight, so it has two
    portions on any given day: a morning tail (00:00–sleep_end) and an evening
    head (sleep_start–24:00).  A non-crossing block (sleep_start < sleep_end)
    is handled as a single contiguous daytime window.
    """
    day = slot_start.date()
    tzinfo = slot_start.tzinfo

    if prefs.sleep_end <= prefs.sleep_start:
        # Overnight block: covers [00:00, sleep_end) and [sleep_start, next midnight)
        morning_end = datetime.combine(day, prefs.sleep_end, tzinfo=tzinfo)
        evening_start = datetime.combine(day, prefs.sleep_start, tzinfo=tzinfo)
        in_morning = slot_start < morning_end
        in_evening = slot_end > evening_start
        return in_morning or in_evening
    else:
        # Daytime block (unusual but supported)
        sleep_start_dt = datetime.combine(day, prefs.sleep_start, tzinfo=tzinfo)
        sleep_end_dt = datetime.combine(day, prefs.sleep_end, tzinfo=tzinfo)
        return slot_start < sleep_end_dt and slot_end > sleep_start_dt


def _in_deep_work_window(slot_start: datetime, slot_end: datetime, prefs: UserPreferences) -> bool:
    """Return True if the slot is fully contained within the deep work window."""
    if prefs.deep_work_start is None or prefs.deep_work_end is None:
        return False
    day = slot_start.date()
    tzinfo = slot_start.tzinfo
    dw_start = datetime.combine(day, prefs.deep_work_start, tzinfo=tzinfo)
    dw_end = datetime.combine(day, prefs.deep_work_end, tzinfo=tzinfo)
    return slot_start >= dw_start and slot_end <= dw_end


def find_slots_on_day(
    target_date: date,
    duration_mins: int,
    existing_events: list[Event],
    user_preferences: UserPreferences | None = None,
) -> list[TimeSlot]:
    """Return free time slots on target_date sorted best-first by preference score.

    Sweeps the full day in duration_mins increments, discarding slots that fall
    in the sleep block or conflict with existing_events, then scores and sorts.
    """
    prefs = user_preferences or UserPreferences()
    step = timedelta(minutes=duration_mins)
    duration = timedelta(minutes=duration_mins)

    # Use naive datetimes to match whatever tzinfo the existing events carry.
    # If events are timezone-aware, combine accordingly.
    sample_tz = existing_events[0].start.tzinfo if existing_events else None

    day_start = datetime.combine(target_date, time(0, 0), tzinfo=sample_tz)
    day_end = datetime.combine(target_date, time(23, 59), tzinfo=sample_tz)

    slots: list[TimeSlot] = []
    cursor = day_start
    while cursor + duration <= day_end + timedelta(minutes=1):
        slot_end = cursor + duration

        if not _in_sleep_block(cursor, slot_end, prefs):
            if not check_overlap(cursor, slot_end, existing_events):
                score = _score_slot(cursor, slot_end, prefs)
                slots.append(TimeSlot(start=cursor, end=slot_end, preference_score=score))

        cursor += step

    slots.sort(key=lambda s: s.preference_score, reverse=True)
    return slots


def _score_slot(slot_start: datetime, slot_end: datetime, prefs: UserPreferences) -> float:
    score = 1.0
    if _in_deep_work_window(slot_start, slot_end, prefs):
        score += 0.5
    # Gentle morning bias: later slots score progressively lower; clamped to 0
    score -= 0.001 * _minutes_from_midnight(slot_start)
    return round(max(0.0, score), 4)
