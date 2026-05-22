"""Google Calendar API client backed by a service account."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.api.v1.schemas.calendar import Event, TimeSlot
from app.calendar.slot_finder import UserPreferences, find_slots_on_day
from app.core.config import settings
from app.core.exceptions import CalendarError
from app.core.logging import get_logger

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_logger = get_logger(__name__)


class GoogleCalendarClient:
    """Async wrapper around the synchronous Google Calendar v3 API.

    All blocking API calls are dispatched via asyncio.to_thread so the event
    loop is never blocked.
    """

    def __init__(self) -> None:
        credentials = service_account.Credentials.from_service_account_file(
            settings.google_calendar_credentials_file,
            scopes=_SCOPES,
        )
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._calendar_id = settings.google_calendar_id

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def get_events(self, start: datetime, end: datetime) -> list[Event]:
        """Return all events in the calendar between start and end (inclusive)."""
        try:
            result = await asyncio.to_thread(self._fetch_events, start, end)
        except HttpError as exc:
            raise CalendarError(f"get_events failed: {exc}") from exc
        items = result.get("items", [])
        return [Event.from_gcal(item) for item in items]

    async def find_free_slots(self, target_date: date, duration_mins: int) -> list[TimeSlot]:
        """Return free slots on target_date that fit duration_mins, best-first."""
        day_start = datetime.combine(target_date, datetime.min.time())
        day_end = datetime.combine(target_date, datetime.max.time().replace(microsecond=0))
        existing = await self.get_events(day_start, day_end)
        return find_slots_on_day(
            target_date=target_date,
            duration_mins=duration_mins,
            existing_events=existing,
            user_preferences=UserPreferences(),
        )

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
    ) -> Event:
        """Insert a new event and return it as an Event model."""
        body = Event(
            id="",
            title=title,
            start=start,
            end=end,
            description=description,
        ).to_gcal_body()
        try:
            raw = await asyncio.to_thread(self._insert_event, body)
        except HttpError as exc:
            raise CalendarError(f"create_event failed: {exc}") from exc
        return Event.from_gcal(raw)

    async def update_event(self, event_id: str, **kwargs: object) -> Event:
        """Patch an existing event with the supplied keyword arguments and return it."""
        patch_body: dict = {}
        if "title" in kwargs:
            patch_body["summary"] = kwargs["title"]
        if "description" in kwargs:
            patch_body["description"] = kwargs["description"]
        if "start" in kwargs:
            patch_body["start"] = {"dateTime": kwargs["start"].isoformat()}  # type: ignore[union-attr]
        if "end" in kwargs:
            patch_body["end"] = {"dateTime": kwargs["end"].isoformat()}  # type: ignore[union-attr]
        try:
            raw = await asyncio.to_thread(self._patch_event, event_id, patch_body)
        except HttpError as exc:
            raise CalendarError(f"update_event failed: {exc}") from exc
        return Event.from_gcal(raw)

    async def delete_event(self, event_id: str) -> None:
        """Delete an event by id."""
        try:
            await asyncio.to_thread(self._delete_event, event_id)
        except HttpError as exc:
            raise CalendarError(f"delete_event failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Synchronous helpers (run inside to_thread)
    # ------------------------------------------------------------------

    def _fetch_events(self, start: datetime, end: datetime) -> dict:
        return (
            self._service.events()
            .list(
                calendarId=self._calendar_id,
                timeMin=start.isoformat() + ("Z" if start.tzinfo is None else ""),
                timeMax=end.isoformat() + ("Z" if end.tzinfo is None else ""),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

    def _insert_event(self, body: dict) -> dict:
        return self._service.events().insert(calendarId=self._calendar_id, body=body).execute()

    def _patch_event(self, event_id: str, body: dict) -> dict:
        return (
            self._service.events()
            .patch(calendarId=self._calendar_id, eventId=event_id, body=body)
            .execute()
        )

    def _delete_event(self, event_id: str) -> None:
        self._service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()
