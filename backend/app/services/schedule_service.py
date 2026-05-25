"""Domain schedule proposal and commit; wraps calendar/ and DB layers."""

from __future__ import annotations

import re as _re
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    # TimeSlot is only referenced in type annotations; the from __future__ import
    # annotations pragma makes all annotations lazy strings at runtime, so this
    # import is never executed at module load time and the circular import chain
    # (schedule_service → api.v1 __init__ → webhook → dispatcher → confirmation
    # → schedule_service) is broken.
    from app.api.v1.schemas.calendar import TimeSlot

from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services import debt_service
from app.services.scheduling_prefs import (
    effective_timezone_name,
    ensure_user_timezone,
    parse_target_date,
    parse_wall_clock_time,
    preferred_fallback_start,
    slot_at_wall_time,
    slot_matches_window,
    time_of_day_window,
    user_timezone,
)

logger = get_logger(__name__)

# Weekday name → Python weekday number (Monday=0)
_WEEKDAY_MAP: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_DAY_ABBREVS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Default fallback hours for time-of-day names
_TIME_OF_DAY_DEFAULTS: dict[str, int] = {
    "morning": 9,
    "afternoon": 14,
    "evening": 18,
    "night": 21,
    "tonight": 20,
}


def _parse_recurrence_time(raw: str | None) -> time:
    """Parse a time string like '17:30', '2 PM', or 'afternoon' into a time object."""
    explicit = parse_wall_clock_time(raw)
    if explicit is not None:
        return explicit
    text = (raw or "").strip().lower()
    m = _re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        return time(int(m.group(1)), int(m.group(2)))
    for name, hour in _TIME_OF_DAY_DEFAULTS.items():
        if name in text:
            return time(hour, 0)
    return time(9, 0)


def _build_days_label(weekday_nums: list[int]) -> str:
    """Build a human-readable label for a set of weekday numbers."""
    sorted_days = sorted(weekday_nums)
    if sorted_days == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if sorted_days == [5, 6]:
        return "Sat–Sun"
    if sorted_days == list(range(7)):
        return "every day"
    return ", ".join(_DAY_ABBREVS[d] for d in sorted_days)


class ScheduleProposal(BaseModel):
    """JSON-serializable proposal stored in users.pending_confirmation."""

    action: Literal["reschedule", "add_task"]
    task_id: str | None = None
    title: str
    duration_mins: int
    proposed_start: datetime
    proposed_end: datetime
    debt_delta: float = 0.0
    deadline_at: datetime | None = None
    requires_proof: bool = False
    is_fixed: bool = False


class BatchScheduleProposal(BaseModel):
    """A group of recurring sessions proposed for one confirmation."""

    action: Literal["batch_add"] = "batch_add"
    sessions: list[ScheduleProposal]
    title: str
    duration_mins: int
    days_label: str   # e.g. "Mon–Fri"
    time_label: str   # e.g. "5:30 PM"
    weeks: int
    total_sessions: int


def propose_batch_slots(
    title: str,
    duration_mins: int,
    recurrence: dict[str, Any],
    user: User,
    *,
    deadline_at: datetime | None = None,
    requires_proof: bool = False,
) -> BatchScheduleProposal:
    """Build a BatchScheduleProposal from a recurrence spec dict.

    recurrence keys:
      days  – list of day-name strings (e.g. ["monday", "tuesday"])
      weeks – int, number of weeks to schedule (default 4)
      time  – "HH:MM" or time-of-day name (e.g. "17:30" or "evening")

    Uses the specified time directly (no free-slot search). Sessions are sorted
    chronologically; sessions that fall in the past are dropped.
    """
    tz = user_timezone(user.timezone)
    now = datetime.now(tz)

    days_raw: list = recurrence.get("days") or []
    weeks_count = max(1, int(recurrence.get("weeks") or 4))
    slot_time = _parse_recurrence_time(recurrence.get("time"))

    # Normalise day names → sorted unique weekday numbers
    weekday_nums: list[int] = sorted({
        _WEEKDAY_MAP[d.strip().lower()]
        for d in days_raw
        if d.strip().lower() in _WEEKDAY_MAP
    })
    if not weekday_nums:
        raise ValueError(f"No valid day names in recurrence.days: {days_raw!r}")

    sessions: list[ScheduleProposal] = []
    for wd in weekday_nums:
        days_ahead = (wd - now.weekday()) % 7
        if days_ahead == 0:
            # Today is the right weekday — check whether the time has passed
            candidate = datetime.combine(now.date(), slot_time, tzinfo=tz)
            if candidate <= now:
                days_ahead = 7
        first_date = now.date() + timedelta(days=days_ahead)
        for week in range(weeks_count):
            session_date = first_date + timedelta(weeks=week)
            start_dt = datetime.combine(session_date, slot_time, tzinfo=tz)
            end_dt = start_dt + timedelta(minutes=duration_mins)
            sessions.append(
                ScheduleProposal(
                    action="add_task",
                    title=title,
                    duration_mins=duration_mins,
                    proposed_start=start_dt,
                    proposed_end=end_dt,
                    deadline_at=deadline_at,
                    requires_proof=requires_proof,
                )
            )

    sessions.sort(key=lambda s: s.proposed_start)

    time_label = datetime.combine(date.today(), slot_time).strftime("%-I:%M %p")
    days_label = _build_days_label(weekday_nums)

    return BatchScheduleProposal(
        sessions=sessions,
        title=title,
        duration_mins=duration_mins,
        days_label=days_label,
        time_label=time_label,
        weeks=weeks_count,
        total_sessions=len(sessions),
    )


async def commit_batch_tasks(
    batch: BatchScheduleProposal,
    session: AsyncSession,
    user: User,
) -> list[Task]:
    """Create all tasks and GCal events for a batch proposal atomically.

    GCal pre-flight: all events are created first. If any fails, all
    already-created GCal events are deleted and the exception is re-raised
    so the DB session is not committed.
    """
    gcal = _get_gcal_client()
    event_ids: list[str | None]

    tz = user_timezone(user.timezone)
    if gcal is not None:
        created_event_ids: list[str] = []
        try:
            for proposal in batch.sessions:
                start = ensure_user_timezone(proposal.proposed_start, tz)
                end = ensure_user_timezone(proposal.proposed_end, tz)
                event = await gcal.create_event(
                    title=proposal.title,
                    start=start,
                    end=end,
                    description=f"Ballast task — {proposal.title} #ballast",
                    tz_name=effective_timezone_name(user.timezone),
                )
                created_event_ids.append(event.id)
        except Exception as exc:
            # Roll back every GCal event that was already created
            for eid in created_event_ids:
                try:
                    await gcal.delete_event(eid)
                except Exception:
                    logger.exception("batch_gcal_rollback_failed event_id=%s", eid)
            logger.exception(
                "batch_gcal_create_failed title=%r session=%d/%d error=%s",
                batch.title,
                len(created_event_ids),
                batch.total_sessions,
                exc,
            )
            raise
        event_ids = created_event_ids
    else:
        event_ids = [None] * len(batch.sessions)

    # All GCal events confirmed — create DB rows
    tasks: list[Task] = []
    for proposal, event_id in zip(batch.sessions, event_ids):
        task = await task_crud.create(
            session,
            user_id=user.id,
            title=proposal.title,
            duration_mins=proposal.duration_mins,
            is_fixed=proposal.is_fixed,
            deadline_at=proposal.deadline_at,
            requires_proof=proposal.requires_proof,
            scheduled_at=proposal.proposed_start,
        )
        if event_id:
            await task_crud.update(session, task, gcal_event_id=event_id)
        tasks.append(task)

    logger.info(
        "batch_tasks_committed title=%r sessions=%d",
        batch.title,
        len(tasks),
    )
    return tasks


def _get_gcal_client():
    """Return a GoogleCalendarClient instance or None if GCal is not configured."""
    if not settings.google_calendar_credentials_file:
        return None
    try:
        from app.calendar.gcal_client import GoogleCalendarClient
        return GoogleCalendarClient()
    except Exception:
        logger.exception("gcal_client_init_failed")
        return None


def _pick_best_slot(
    slots: list[TimeSlot],
    tz,
    window: tuple[int, int] | None,
) -> TimeSlot | None:
    """Choose the best slot, preferring the requested time-of-day window when set."""
    if not slots:
        return None

    if window is not None:
        in_window = [s for s in slots if slot_matches_window(s.start, window, tz)]
        if in_window:
            return in_window[0]

    return slots[0]


async def _find_next_slot(
    duration_mins: int,
    user: User,
    *,
    target_date: date | None = None,
    time_of_day: str | None = None,
) -> tuple[datetime, datetime]:
    """Find the next available slot via GCal, honoring day and time-of-day preferences."""
    tz = user_timezone(user.timezone)
    window = time_of_day_window(time_of_day)
    wall_time = parse_wall_clock_time(time_of_day)
    now = datetime.now(tz)

    gcal = _get_gcal_client()
    if gcal is None:
        return preferred_fallback_start(
            tz,
            duration_mins,
            target_date=target_date,
            time_of_day=time_of_day,
            now=now,
        )

    # Explicit local time (e.g. "2 PM", "14:00") — search for that wall-clock slot first
    if wall_time is not None:
        dates_to_search: list[date]
        if target_date is not None:
            dates_to_search = [target_date]
            if target_date < now.date():
                dates_to_search.append(target_date + timedelta(days=7))
        else:
            dates_to_search = [now.date() + timedelta(days=i) for i in range(7)]

        for day in dates_to_search:
            try:
                slots = await gcal.find_free_slots(day, duration_mins, tz=tz)
            except Exception:
                logger.exception("find_free_slots_failed date=%s", day)
                continue
            for slot in slots:
                local = slot.start.astimezone(tz)
                if local.hour == wall_time.hour and local.minute == wall_time.minute:
                    return (
                        ensure_user_timezone(slot.start, tz),
                        ensure_user_timezone(slot.end, tz),
                    )

        return slot_at_wall_time(
            tz,
            duration_mins,
            wall_time,
            target_date=target_date,
            now=now,
        )

    dates_to_search: list[date]
    if target_date is not None:
        dates_to_search = [target_date]
        if target_date < now.date():
            dates_to_search.append(target_date + timedelta(days=7))
    else:
        dates_to_search = [now.date() + timedelta(days=i) for i in range(7)]

    for day in dates_to_search:
        try:
            slots = await gcal.find_free_slots(day, duration_mins, tz=tz)
        except Exception:
            logger.exception("find_free_slots_failed date=%s", day)
            continue

        best = _pick_best_slot(slots, tz, window)
        if best is not None:
            return (
                ensure_user_timezone(best.start, tz),
                ensure_user_timezone(best.end, tz),
            )

    return preferred_fallback_start(
        tz,
        duration_mins,
        target_date=target_date,
        time_of_day=time_of_day,
        now=now,
    )


async def propose_reschedule(
    task: Task,
    user: User,
    *,
    time_of_day: str | None = None,
) -> ScheduleProposal:
    """Find next available slot and return an unconfirmed reschedule proposal.

    Debt delta is task.duration_mins / 60 hours added (pushing incurs debt).
    """
    proposed_start, proposed_end = await _find_next_slot(
        task.duration_mins, user, time_of_day=time_of_day
    )
    debt_delta = round(task.duration_mins / 60, 2)

    return ScheduleProposal(
        action="reschedule",
        task_id=str(task.id),
        title=task.title,
        duration_mins=task.duration_mins,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        debt_delta=debt_delta,
    )


async def commit_reschedule(
    proposal: ScheduleProposal,
    session: AsyncSession,
    user: User,
) -> None:
    """Apply a confirmed reschedule: update task, move GCal event, log debt."""
    if proposal.task_id is None:
        raise ValueError("commit_reschedule requires a task_id")

    task = await task_crud.get(session, UUID(proposal.task_id))
    if task is None:
        raise ValueError(f"Task {proposal.task_id} not found")

    gcal_event_id = task.gcal_event_id

    gcal = _get_gcal_client()
    if gcal is not None and gcal_event_id:
        tz = user_timezone(user.timezone)
        try:
            await gcal.update_event(
                gcal_event_id,
                start=ensure_user_timezone(proposal.proposed_start, tz),
                end=ensure_user_timezone(proposal.proposed_end, tz),
                tz_name=effective_timezone_name(user.timezone),
            )
        except Exception:
            logger.exception("gcal_update_event_failed event_id=%s", gcal_event_id)

    await task_crud.update(
        session,
        task,
        status=TaskStatus.PUSHED.value,
        scheduled_at=proposal.proposed_start,
    )

    await debt_service.add_debt(
        session,
        user_id=user.id,
        task_id=task.id,
        hours=proposal.debt_delta,
        reason=f"Pushed '{task.title}'",
    )


async def propose_slot(
    title: str,
    duration_mins: int,
    user: User,
    deadline_at: datetime | None = None,
    requires_proof: bool = False,
    is_fixed: bool = False,
    *,
    day: str | None = None,
    time_of_day: str | None = None,
) -> ScheduleProposal:
    """Find the best available slot for a new task and return a proposal."""
    tz = user_timezone(user.timezone)
    target_date = parse_target_date(day, tz)
    proposed_start, proposed_end = await _find_next_slot(
        duration_mins,
        user,
        target_date=target_date,
        time_of_day=time_of_day,
    )

    return ScheduleProposal(
        action="add_task",
        task_id=None,
        title=title,
        duration_mins=duration_mins,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        debt_delta=0.0,
        deadline_at=deadline_at,
        requires_proof=requires_proof,
        is_fixed=is_fixed,
    )


async def commit_new_task(
    proposal: ScheduleProposal,
    session: AsyncSession,
    user: User,
) -> Task:
    """Create the task in DB and a corresponding GCal event."""
    task = await task_crud.create(
        session,
        user_id=user.id,
        title=proposal.title,
        duration_mins=proposal.duration_mins,
        is_fixed=proposal.is_fixed,
        deadline_at=proposal.deadline_at,
        requires_proof=proposal.requires_proof,
        scheduled_at=proposal.proposed_start,
    )

    gcal = _get_gcal_client()
    if gcal is not None:
        tz = user_timezone(user.timezone)
        start = ensure_user_timezone(proposal.proposed_start, tz)
        end = ensure_user_timezone(proposal.proposed_end, tz)
        try:
            event = await gcal.create_event(
                title=proposal.title,
                start=start,
                end=end,
                description=f"Ballast task — {proposal.title} #ballast",
                tz_name=effective_timezone_name(user.timezone),
            )
            await task_crud.update(session, task, gcal_event_id=event.id)
        except Exception as exc:
            logger.exception(
                "gcal_create_event_failed title=%r start=%s error=%s",
                proposal.title,
                start.isoformat(),
                exc,
            )

    return task
