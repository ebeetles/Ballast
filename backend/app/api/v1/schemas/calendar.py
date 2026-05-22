"""Pydantic schemas for calendar events and time slots."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

_BALLAST_TAG = "#ballast"


class Event(BaseModel):
    """A Google Calendar event, possibly created by Ballast."""

    id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    is_ballast_event: bool = Field(
        default=False,
        description="True when Ballast created this event (description contains #ballast).",
    )

    @classmethod
    def from_gcal(cls, raw: dict) -> "Event":
        """Build an Event from a raw Google Calendar API event resource."""
        start_raw = raw.get("start", {})
        end_raw = raw.get("end", {})
        start = datetime.fromisoformat(
            start_raw.get("dateTime") or f"{start_raw.get('date')}T00:00:00"
        )
        end = datetime.fromisoformat(
            end_raw.get("dateTime") or f"{end_raw.get('date')}T00:00:00"
        )
        description = raw.get("description") or ""
        return cls(
            id=raw.get("id", ""),
            title=raw.get("summary") or "",
            start=start,
            end=end,
            description=description,
            is_ballast_event=_BALLAST_TAG in description,
        )

    def to_gcal_body(self) -> dict:
        """Serialize to a Google Calendar API event resource dict."""
        return {
            "summary": self.title,
            "description": self.description,
            "start": {"dateTime": self.start.isoformat()},
            "end": {"dateTime": self.end.isoformat()},
        }


class TimeSlot(BaseModel):
    """A candidate free time slot on a given day with a preference score."""

    start: datetime
    end: datetime
    preference_score: float = Field(ge=0.0, description="Higher is better.")
