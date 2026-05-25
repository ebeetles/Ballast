"""Parse natural-language day/time preferences for slot finding."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings

# Local-hour windows (start inclusive, end exclusive)
TIME_OF_DAY_WINDOWS: dict[str, tuple[int, int]] = {
    "morning": (7, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 23),
    "tonight": (19, 23),
}

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def effective_timezone_name(tz_name: str | None) -> str:
    """Return the IANA timezone name to use for scheduling.

    Users created before timezone onboarding have ``UTC`` in the DB; that makes
    "2 PM" become 14:00 UTC (7 AM on a Pacific calendar). Replace bare UTC with
    the configured default.
    """
    name = (tz_name or "").strip()
    if not name or name.upper() == "UTC":
        return settings.default_user_timezone
    return name


def user_timezone(tz_name: str | None) -> ZoneInfo:
    """Resolve a user timezone string, falling back to configured default."""
    name = effective_timezone_name(tz_name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            return ZoneInfo(settings.default_user_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")


def parse_wall_clock_time(raw: str | None) -> time | None:
    """Parse an explicit local time like '14:00', '2pm', or '2:30 PM'."""
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip().lower().replace(".", "")

    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3) == "pm" and hour != 12:
            hour += 12
        elif m.group(3) == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)

    return None


def ensure_user_timezone(dt: datetime, tz: ZoneInfo) -> datetime:
    """Return a timezone-aware datetime for calendar writes in ``tz``.

    Naive datetimes are wall-clock in ``tz``. Legacy rows stored as UTC with
    hour/minute meaning local time are reinterpreted (see ``wall_clock_in_tz``).
    """
    return wall_clock_in_tz(dt, tz)


def wall_clock_in_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    """Build a datetime whose hour/minute are wall-clock in ``tz``.

    Ballast stores intended local times. Values incorrectly tagged as UTC
    (e.g. 14:00 UTC meaning "2 PM local") must not be converted via astimezone.
    """
    from datetime import timezone as std_tz

    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    if dt.tzinfo == std_tz.utc:
        return datetime.combine(dt.date(), dt.time(), tzinfo=tz)
    local = dt.astimezone(tz)
    return datetime.combine(local.date(), local.time(), tzinfo=tz)


def slot_at_wall_time(
    tz: ZoneInfo,
    duration_mins: int,
    wall_time: time,
    *,
    target_date: date | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Build a slot at an explicit local wall-clock time on the target day."""
    ref = now or datetime.now(tz)
    base_date = target_date or ref.date()
    start = datetime.combine(base_date, wall_time, tzinfo=tz)
    if start <= ref:
        start += timedelta(days=1)
    end = start + timedelta(minutes=duration_mins)
    return start, end


def normalize_time_of_day(raw: str | None) -> str | None:
    """Map free-text time hints to a canonical window key."""
    if not raw or not str(raw).strip():
        return None
    if parse_wall_clock_time(raw) is not None:
        return None
    text = str(raw).lower().strip()
    if text in TIME_OF_DAY_WINDOWS:
        return text
    if "tonight" in text or text == "night":
        return "night"
    if "evening" in text:
        return "evening"
    if "afternoon" in text:
        return "afternoon"
    if "morning" in text:
        return "morning"
    return None


def time_of_day_window(time_of_day: str | None) -> tuple[int, int] | None:
    """Return local-hour (start, end) for a canonical time-of-day key."""
    key = normalize_time_of_day(time_of_day)
    if key is None:
        return None
    return TIME_OF_DAY_WINDOWS[key]


def parse_target_date(day: str | None, tz: ZoneInfo, *, now: datetime | None = None) -> date | None:
    """Parse day strings like 'tomorrow' or 'friday' into a calendar date in the user's TZ."""
    if not day or not str(day).strip():
        return None

    text = str(day).lower().strip()
    ref = now or datetime.now(tz)
    today = ref.date()

    if text in ("today", "tonight"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    )
    if match:
        target_weekday = _WEEKDAYS[match.group(1)]
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0 and text != "today":
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    return None


def slot_matches_window(slot_start: datetime, window: tuple[int, int], tz: ZoneInfo) -> bool:
    """True when slot_start falls in the local-hour window."""
    local = slot_start.astimezone(tz) if slot_start.tzinfo else slot_start.replace(tzinfo=tz)
    lo, hi = window
    return lo <= local.hour < hi


def preferred_fallback_start(
    tz: ZoneInfo,
    duration_mins: int,
    *,
    target_date: date | None = None,
    time_of_day: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Pick a fallback slot when GCal is unavailable, honoring day/time preferences."""
    ref = now or datetime.now(tz)
    wall_time = parse_wall_clock_time(time_of_day)
    if wall_time is not None:
        return slot_at_wall_time(
            tz, duration_mins, wall_time, target_date=target_date, now=ref
        )

    window = time_of_day_window(time_of_day)
    base_date = target_date or ref.date()

    if window is not None:
        start_hour = window[0]
        candidate = datetime.combine(base_date, time(start_hour, 0), tzinfo=tz)
        if candidate <= ref:
            candidate += timedelta(days=1)
        end = candidate + timedelta(minutes=duration_mins)
        return candidate, end

    start = ref.replace(second=0, microsecond=0) + timedelta(hours=1)
    end = start + timedelta(minutes=duration_mins)
    return start, end
