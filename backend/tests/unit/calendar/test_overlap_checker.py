"""Tests for calendar/overlap_checker.py."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.api.v1.schemas.calendar import Event
from app.calendar.overlap_checker import check_overlap


def _event(start: str, end: str) -> Event:
    return Event(
        id="evt",
        title="Existing",
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
    )


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_no_overlap_before():
    events = [_event("2024-01-10T10:00:00", "2024-01-10T11:00:00")]
    assert check_overlap(_dt("2024-01-10T08:00:00"), _dt("2024-01-10T09:00:00"), events) is False


def test_no_overlap_after():
    events = [_event("2024-01-10T10:00:00", "2024-01-10T11:00:00")]
    assert check_overlap(_dt("2024-01-10T12:00:00"), _dt("2024-01-10T13:00:00"), events) is False


def test_partial_overlap_start():
    # Proposed slot starts before event and ends inside it
    events = [_event("2024-01-10T10:00:00", "2024-01-10T12:00:00")]
    assert check_overlap(_dt("2024-01-10T09:00:00"), _dt("2024-01-10T11:00:00"), events) is True


def test_partial_overlap_end():
    # Proposed slot starts inside event and ends after it
    events = [_event("2024-01-10T10:00:00", "2024-01-10T12:00:00")]
    assert check_overlap(_dt("2024-01-10T11:00:00"), _dt("2024-01-10T13:00:00"), events) is True


def test_zero_gap_abutting_end():
    # Proposed slot ends exactly when event starts — zero-gap = overlap
    events = [_event("2024-01-10T11:00:00", "2024-01-10T12:00:00")]
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), events) is True


def test_zero_gap_abutting_start():
    # Proposed slot starts exactly when event ends — zero-gap = overlap
    events = [_event("2024-01-10T09:00:00", "2024-01-10T10:00:00")]
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), events) is True


def test_slot_contained_in_event():
    events = [_event("2024-01-10T08:00:00", "2024-01-10T18:00:00")]
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), events) is True


def test_event_contained_in_slot():
    events = [_event("2024-01-10T10:30:00", "2024-01-10T10:45:00")]
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), events) is True


def test_empty_events():
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), []) is False


def test_multiple_events_only_one_conflicts():
    events = [
        _event("2024-01-10T08:00:00", "2024-01-10T09:00:00"),
        _event("2024-01-10T11:00:00", "2024-01-10T12:00:00"),
    ]
    # Slot 10:00–11:00 abuts the second event at 11:00 → overlap
    assert check_overlap(_dt("2024-01-10T10:00:00"), _dt("2024-01-10T11:00:00"), events) is True
