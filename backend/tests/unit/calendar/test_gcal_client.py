"""Tests for calendar/gcal_client.py — Google API is fully mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from app.api.v1.schemas.calendar import Event
from app.core.exceptions import CalendarError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_event(
    event_id: str = "abc123",
    summary: str = "Team sync",
    start: str = "2024-01-10T09:00:00",
    end: str = "2024-01-10T10:00:00",
    description: str = "",
) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "description": description,
    }


def _make_http_error() -> HttpError:
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return HttpError(resp=resp, content=b"Forbidden")


@pytest.fixture
def mock_gcal_client():
    """Return a GoogleCalendarClient with all Google library calls mocked out."""
    with (
        patch(
            "app.calendar.gcal_client.service_account.Credentials.from_service_account_file"
        ) as mock_creds,
        patch("app.calendar.gcal_client.build") as mock_build,
        patch("app.calendar.gcal_client.settings") as mock_settings,
    ):
        mock_settings.google_calendar_credentials_file = "/fake/creds.json"
        mock_settings.google_calendar_id = "primary"

        mock_creds.return_value = MagicMock()
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        from app.calendar.gcal_client import GoogleCalendarClient

        client = GoogleCalendarClient()
        client._service = mock_service
        yield client, mock_service


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------

async def test_get_events_returns_event_models(mock_gcal_client):
    client, mock_service = mock_gcal_client
    raw = _raw_event(summary="Team sync", description="")
    mock_service.events.return_value.list.return_value.execute.return_value = {"items": [raw]}

    events = await client.get_events(
        datetime(2024, 1, 10, 0, 0),
        datetime(2024, 1, 10, 23, 59),
    )

    assert len(events) == 1
    assert isinstance(events[0], Event)
    assert events[0].id == "abc123"
    assert events[0].title == "Team sync"
    assert events[0].start == datetime(2024, 1, 10, 9, 0)
    assert events[0].end == datetime(2024, 1, 10, 10, 0)


def test_event_from_gcal_is_ballast_event_false():
    raw = _raw_event(description="Just a normal meeting")
    event = Event.from_gcal(raw)
    assert event.is_ballast_event is False


def test_event_from_gcal_is_ballast_event_true():
    raw = _raw_event(description="Focus block #ballast")
    event = Event.from_gcal(raw)
    assert event.is_ballast_event is True


def test_event_from_gcal_empty_description():
    raw = _raw_event(description="")
    event = Event.from_gcal(raw)
    assert event.is_ballast_event is False
    assert event.description == ""


async def test_get_events_http_error_raises_calendar_error(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.list.return_value.execute.side_effect = _make_http_error()

    with pytest.raises(CalendarError):
        await client.get_events(datetime(2024, 1, 10, 0, 0), datetime(2024, 1, 10, 23, 59))


async def test_get_events_returns_empty_list_when_no_items(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.list.return_value.execute.return_value = {"items": []}

    events = await client.get_events(datetime(2024, 1, 10, 0, 0), datetime(2024, 1, 10, 23, 59))
    assert events == []


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------

async def test_create_event_returns_event_model(mock_gcal_client):
    client, mock_service = mock_gcal_client
    raw = _raw_event(event_id="new-1", summary="Deep work", description="#ballast")
    mock_service.events.return_value.insert.return_value.execute.return_value = raw

    event = await client.create_event(
        title="Deep work",
        start=datetime(2024, 1, 10, 9, 0),
        end=datetime(2024, 1, 10, 11, 0),
        description="#ballast",
    )

    assert event.id == "new-1"
    assert event.title == "Deep work"
    assert event.is_ballast_event is True


async def test_create_event_http_error_raises_calendar_error(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.insert.return_value.execute.side_effect = _make_http_error()

    with pytest.raises(CalendarError):
        await client.create_event("Test", datetime(2024, 1, 10, 9, 0), datetime(2024, 1, 10, 10, 0))


# ---------------------------------------------------------------------------
# update_event
# ---------------------------------------------------------------------------

async def test_update_event_returns_updated_model(mock_gcal_client):
    client, mock_service = mock_gcal_client
    raw = _raw_event(event_id="evt-2", summary="Updated title")
    mock_service.events.return_value.patch.return_value.execute.return_value = raw

    event = await client.update_event("evt-2", title="Updated title")

    assert event.title == "Updated title"


async def test_update_event_http_error_raises_calendar_error(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.patch.return_value.execute.side_effect = _make_http_error()

    with pytest.raises(CalendarError):
        await client.update_event("evt-2", title="New name")


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------

async def test_delete_event_calls_api(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.delete.return_value.execute.return_value = None

    await client.delete_event("evt-3")

    mock_service.events.return_value.delete.assert_called_once_with(
        calendarId="primary", eventId="evt-3"
    )


async def test_delete_event_http_error_raises_calendar_error(mock_gcal_client):
    client, mock_service = mock_gcal_client
    mock_service.events.return_value.delete.return_value.execute.side_effect = _make_http_error()

    with pytest.raises(CalendarError):
        await client.delete_event("evt-3")


# ---------------------------------------------------------------------------
# Timezone serialization correctness
# ---------------------------------------------------------------------------


def test_2pm_pdt_serializes_with_correct_offset():
    """A 2 PM PDT datetime must serialize to an ISO string with -07:00, not +00:00.

    Bug regression test: events were being created at 7 AM on GCal because the
    local time was serialized as if it were UTC (2 PM UTC = 7 AM PDT).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.calendar.gcal_client import _dt_to_gcal_entry

    pdt = ZoneInfo("America/Los_Angeles")
    # 14 May 2026 is during Pacific Daylight Time (UTC-7)
    start = datetime(2026, 5, 25, 14, 0, 0, tzinfo=pdt)

    entry = _dt_to_gcal_entry(start)

    assert entry["dateTime"] == "2026-05-25T14:00:00", (
        f"Expected local wall-clock 14:00:00, got: {entry['dateTime']}"
    )
    assert entry.get("timeZone") == "America/Los_Angeles", (
        f"Expected IANA timeZone key, got: {entry.get('timeZone')}"
    )


def test_to_gcal_body_includes_timezone_for_zoneinfo():
    """Event.to_gcal_body() includes timeZone when datetimes carry a ZoneInfo tzinfo."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.api.v1.schemas.calendar import Event

    pdt = ZoneInfo("America/Los_Angeles")
    start = datetime(2026, 5, 25, 14, 0, 0, tzinfo=pdt)
    end = datetime(2026, 5, 25, 15, 30, 0, tzinfo=pdt)
    event = Event(id="", title="Deep work", start=start, end=end)

    body = event.to_gcal_body()

    assert body["start"]["dateTime"] == "2026-05-25T14:00:00"
    assert body["start"]["timeZone"] == "America/Los_Angeles"
    assert body["end"]["dateTime"] == "2026-05-25T15:30:00"
    assert body["end"]["timeZone"] == "America/Los_Angeles"


def test_to_gcal_body_fixed_offset_uses_passed_tz_name():
    """Reloaded JSON datetimes (fixed offset) still get IANA timeZone when tz_name passed."""
    from datetime import datetime, timezone, timedelta
    from app.api.v1.schemas.calendar import Event

    pdt_fixed = timezone(timedelta(hours=-7))
    start = datetime(2026, 5, 25, 14, 0, 0, tzinfo=pdt_fixed)
    end = datetime(2026, 5, 25, 15, 30, 0, tzinfo=pdt_fixed)
    event = Event(id="", title="Deep work", start=start, end=end)

    body = event.to_gcal_body(tz_name="America/Los_Angeles")

    assert body["start"]["dateTime"] == "2026-05-25T14:00:00"
    assert body["start"]["timeZone"] == "America/Los_Angeles"


def test_slots_with_utc_events_still_use_user_tz():
    """When GCal returns UTC events, generated slots must still be in user local TZ."""
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo
    from app.api.v1.schemas.calendar import Event
    from app.calendar.slot_finder import find_slots_on_day

    pdt = ZoneInfo("America/Los_Angeles")
    # Busy block at 10:00–11:00 UTC (3–4 AM PDT) — should not force slots into UTC wall clock
    utc_events = [
        Event(
            id="e1",
            title="Busy",
            start=datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 25, 11, 0, tzinfo=timezone.utc),
        )
    ]
    slots = find_slots_on_day(
        target_date=date(2026, 5, 25),
        duration_mins=60,
        existing_events=utc_events,
        user_tz=pdt,
    )
    assert slots, "Expected free slots on the day"
    for slot in slots:
        assert slot.start.tzinfo == pdt
        assert "-07:00" in slot.start.isoformat()
    # 2 PM PDT must appear as hour 14 local, not hour 14 UTC (which would be 7 AM PDT)
    two_pm = [s for s in slots if s.start.astimezone(pdt).hour == 14]
    assert two_pm, "Expected a 2 PM PDT slot when calendar uses user_tz"


def test_slots_on_empty_day_use_user_tz_not_utc():
    """find_slots_on_day with no existing events must use user_tz, not UTC.

    Regression test: when a day had no GCal events, slots were generated in UTC.
    For a PDT user, a '14:00 slot' would be 14:00 UTC = 7:00 AM PDT on GCal.
    """
    from datetime import date
    from zoneinfo import ZoneInfo
    from app.calendar.slot_finder import find_slots_on_day

    pdt = ZoneInfo("America/Los_Angeles")
    slots = find_slots_on_day(
        target_date=date(2026, 5, 25),
        duration_mins=60,
        existing_events=[],
        user_tz=pdt,
    )

    assert slots, "Should return slots for a free day"
    for slot in slots:
        # All slot datetimes must carry the PDT timezone, not UTC
        assert slot.start.tzinfo is pdt, (
            f"Expected ZoneInfo('America/Los_Angeles') tzinfo, got {slot.start.tzinfo!r}"
        )
        # When serialized, must include PDT offset (-07:00), not UTC (+00:00)
        iso = slot.start.isoformat()
        assert "-07:00" in iso, (
            f"Slot datetime {iso!r} does not contain PDT offset; would appear as UTC on GCal"
        )
