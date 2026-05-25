"""Tests for scheduling_prefs day/time parsing."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.services.scheduling_prefs import (
    effective_timezone_name,
    parse_target_date,
    parse_wall_clock_time,
    preferred_fallback_start,
    slot_matches_window,
    time_of_day_window,
    user_timezone,
    wall_clock_in_tz,
)


def test_time_of_day_afternoon_window() -> None:
    assert time_of_day_window("afternoon") == (12, 17)
    assert time_of_day_window("tonight") == (19, 23)


def test_parse_tomorrow() -> None:
    tz = ZoneInfo("UTC")
    now = datetime(2026, 5, 23, 10, 0, tzinfo=tz)
    assert parse_target_date("tomorrow", tz, now=now) == date(2026, 5, 24)


def test_slot_matches_afternoon() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    slot = datetime(2026, 5, 24, 20, 0, tzinfo=ZoneInfo("UTC"))  # 1pm Pacific
    assert slot_matches_window(slot, (12, 17), tz)


def test_fallback_afternoon_not_7am() -> None:
    tz = ZoneInfo("UTC")
    now = datetime(2026, 5, 24, 8, 0, tzinfo=tz)
    start, _ = preferred_fallback_start(
        tz, 30, target_date=date(2026, 5, 24), time_of_day="afternoon", now=now
    )
    assert start.hour == 12


def test_parse_wall_clock_time_variants() -> None:
    assert parse_wall_clock_time("14:00") == time(14, 0)
    assert parse_wall_clock_time("2 PM") == time(14, 0)
    assert parse_wall_clock_time("2pm") == time(14, 0)
    assert parse_wall_clock_time("9:30am") == time(9, 30)


def test_effective_timezone_replaces_utc() -> None:
    assert effective_timezone_name("UTC") == "America/Los_Angeles"
    assert effective_timezone_name(None) == "America/Los_Angeles"
    assert effective_timezone_name("America/New_York") == "America/New_York"


def test_wall_clock_in_tz_reinterprets_utc_as_local() -> None:
    """14:00 UTC stored by mistake must become 14:00 Pacific, not 07:00."""
    from datetime import datetime, timezone as std_tz

    pdt = ZoneInfo("America/Los_Angeles")
    wrong = datetime(2026, 5, 25, 14, 0, tzinfo=std_tz.utc)
    fixed = wall_clock_in_tz(wrong, pdt)
    assert fixed.hour == 14
    assert fixed.minute == 0
    assert str(fixed.tzinfo) == "America/Los_Angeles"


def test_fallback_explicit_2pm_pdt() -> None:
    """Explicit '2 PM' must schedule at 14:00 local, not 14:00 UTC."""
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 5, 24, 8, 0, tzinfo=tz)
    start, _ = preferred_fallback_start(
        tz, 60, target_date=date(2026, 5, 24), time_of_day="2 PM", now=now
    )
    assert start.hour == 14
    assert start.minute == 0
    assert start.tzinfo == user_timezone("America/Los_Angeles")
