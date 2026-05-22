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
