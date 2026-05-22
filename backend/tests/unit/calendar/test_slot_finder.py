"""Tests for calendar/slot_finder.py."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.api.v1.schemas.calendar import Event
from app.calendar.slot_finder import UserPreferences, find_slots_on_day


def _event(start: str, end: str) -> Event:
    return Event(id="evt", title="Busy", start=datetime.fromisoformat(start), end=datetime.fromisoformat(end))


def test_sleep_block_excluded_by_default():
    # Default sleep block: 11pm–7am. Slots at midnight should not appear.
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=[],
        user_preferences=UserPreferences(),
    )
    starts = [s.start.hour for s in slots]
    # No slot should start between 23:00 and 06:00 (would overlap sleep block)
    assert not any(h >= 23 or h < 6 for h in starts), f"Sleep-block slot found: {starts}"


def test_all_slots_within_awake_hours():
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=30,
        existing_events=[],
        user_preferences=UserPreferences(),
    )
    assert len(slots) > 0
    for slot in slots:
        # All slots must start at 07:00 or later and end no later than 23:00
        assert slot.start.hour >= 7, f"Slot starts too early (sleep block): {slot.start}"
        assert slot.end <= slot.start.replace(hour=23, minute=0, second=0), (
            f"Slot ends inside sleep block: {slot.end}"
        )


def test_overlapping_event_filtered_out():
    # An event from 09:00–10:00 should block the 09:00 slot
    events = [_event("2024-01-10T09:00:00", "2024-01-10T10:00:00")]
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=events,
        user_preferences=UserPreferences(),
    )
    for slot in slots:
        assert not (slot.start.hour == 9 and slot.start.minute == 0), "09:00 slot should be blocked"


def test_deep_work_slots_score_higher():
    prefs = UserPreferences(
        deep_work_start=time(9, 0),
        deep_work_end=time(12, 0),
    )
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=[],
        user_preferences=prefs,
    )
    deep_work_slots = [s for s in slots if s.start.hour >= 9 and s.end.hour <= 12]
    other_slots = [s for s in slots if s.start.hour >= 13]

    assert deep_work_slots, "Should have deep work slots"
    assert other_slots, "Should have non-deep-work slots"

    best_deep = max(s.preference_score for s in deep_work_slots)
    best_other = max(s.preference_score for s in other_slots)
    assert best_deep > best_other, "Deep work slots should outscore afternoon slots"


def test_result_sorted_by_preference_score_descending():
    prefs = UserPreferences(
        deep_work_start=time(9, 0),
        deep_work_end=time(11, 0),
    )
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=[],
        user_preferences=prefs,
    )
    scores = [s.preference_score for s in slots]
    assert scores == sorted(scores, reverse=True), "Slots must be sorted best-first"


def test_no_slots_when_day_fully_blocked():
    # Block 7am–11pm so nothing fits a 60-min slot
    events = [_event("2024-01-10T07:00:00", "2024-01-10T23:00:00")]
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=events,
        user_preferences=UserPreferences(),
    )
    assert slots == []


def test_morning_slots_score_higher_than_afternoon():
    prefs = UserPreferences()  # no deep work preference
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=[],
        user_preferences=prefs,
    )
    morning = [s for s in slots if s.start.hour == 7]
    afternoon = [s for s in slots if s.start.hour == 15]
    assert morning and afternoon
    assert morning[0].preference_score > afternoon[0].preference_score


def test_none_preferences_uses_defaults():
    # Should not raise when user_preferences=None
    slots = find_slots_on_day(
        target_date=date(2024, 1, 10),
        duration_mins=60,
        existing_events=[],
        user_preferences=None,
    )
    assert isinstance(slots, list)
