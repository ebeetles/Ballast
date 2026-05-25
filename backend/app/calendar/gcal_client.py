"""Google Calendar API client backed by a service account."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.api.v1.schemas.calendar import Event, TimeSlot
from app.calendar.slot_finder import UserPreferences, find_slots_on_day
from app.core.config import settings
from app.core.exceptions import CalendarError
from app.core.logging import get_logger
from app.services.scheduling_prefs import ensure_user_timezone

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_logger = get_logger(__name__)


def _dt_to_gcal_entry(dt: datetime, tz_name: str | None = None) -> dict:
    """Serialize a datetime for the Google Calendar API.

    Uses local wall-clock ``dateTime`` plus IANA ``timeZone`` when possible — the
    format Google recommends.  Naive datetimes are interpreted as wall-clock in
    ``tz_name``, never as UTC.
    """
    iana: str | None = None
    if hasattr(dt.tzinfo, "key"):
        iana = dt.tzinfo.key  # type: ignore[union-attr]
    elif tz_name:
        iana = tz_name

    if iana:
        try:
            zi = ZoneInfo(iana)
        except Exception:
            zi = None
        if zi is not None:
            local = ensure_user_timezone(dt, zi)
            return {
                "dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": iana,
            }

    if dt.tzinfo is not None:
        return {"dateTime": dt.isoformat()}

    if tz_name:
        zi = ZoneInfo(tz_name)
        local = dt.replace(tzinfo=zi)
        return {
            "dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": tz_name,
        }

    return {"dateTime": dt.isoformat()}


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

    async def find_free_slots(
        self,
        target_date: date,
        duration_mins: int,
        tz: ZoneInfo | None = None,
    ) -> list[TimeSlot]:
        """Return free slots on target_date that fit duration_mins, best-first.

        ``tz`` must be the user's IANA timezone (e.g. ``ZoneInfo("America/Los_Angeles")``).
        It is used to generate slots in local time so that the returned datetimes
        serialize to correct RFC 3339 offsets for the Google Calendar API.
        """
        from datetime import timezone as _tz
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=_tz.utc)
        day_end = datetime.combine(target_date, datetime.max.time().replace(microsecond=0), tzinfo=_tz.utc)
        existing = await self.get_events(day_start, day_end)
        return find_slots_on_day(
            target_date=target_date,
            duration_mins=duration_mins,
            existing_events=existing,
            user_preferences=UserPreferences(),
            user_tz=tz,
        )

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        *,
        tz_name: str | None = None,
    ) -> Event:
        """Insert a new event and return it as an Event model."""
        body = Event(
            id="",
            title=title,
            start=start,
            end=end,
            description=description,
        ).to_gcal_body(tz_name=tz_name)
        try:
            raw = await asyncio.to_thread(self._insert_event, body)
        except HttpError as exc:
            raise CalendarError(f"create_event failed: {exc}") from exc
        return Event.from_gcal(raw)

    async def update_event(self, event_id: str, **kwargs: object) -> Event:
        """Patch an existing event with the supplied keyword arguments and return it."""
        tz_name = kwargs.pop("tz_name", None)  # type: ignore[misc]
        patch_body: dict = {}
        if "title" in kwargs:
            patch_body["summary"] = kwargs["title"]
        if "description" in kwargs:
            patch_body["description"] = kwargs["description"]
        if "start" in kwargs:
            patch_body["start"] = _dt_to_gcal_entry(kwargs["start"], tz_name=tz_name)  # type: ignore[arg-type]
        if "end" in kwargs:
            patch_body["end"] = _dt_to_gcal_entry(kwargs["end"], tz_name=tz_name)  # type: ignore[arg-type]
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
