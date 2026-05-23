"""Domain schedule proposal and commit; wraps calendar/ and DB layers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services import debt_service

logger = get_logger(__name__)


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


def _fallback_slot(duration_mins: int) -> tuple[datetime, datetime]:
    """Return now+1h as a default slot when GCal is unavailable."""
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(minutes=duration_mins)
    return start, end


async def _find_next_slot(duration_mins: int, user: User) -> tuple[datetime, datetime]:
    """Find the next available slot via GCal, or fall back to now+1h."""
    gcal = _get_gcal_client()
    if gcal is None:
        return _fallback_slot(duration_mins)

    today = datetime.now(tz=timezone.utc).date()
    for days_ahead in range(7):
        target_date = today + timedelta(days=days_ahead)
        try:
            slots = await gcal.find_free_slots(target_date, duration_mins)
        except Exception:
            logger.exception("find_free_slots_failed date=%s", target_date)
            continue
        if slots:
            best = slots[0]
            return best.start, best.end

    return _fallback_slot(duration_mins)


async def propose_reschedule(task: Task, user: User) -> ScheduleProposal:
    """Find next available slot and return an unconfirmed reschedule proposal.

    Debt delta is task.duration_mins / 60 hours added (pushing incurs debt).
    """
    proposed_start, proposed_end = await _find_next_slot(task.duration_mins, user)
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
        try:
            await gcal.update_event(
                gcal_event_id,
                start=proposal.proposed_start,
                end=proposal.proposed_end,
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
) -> ScheduleProposal:
    """Find the best available slot for a new task and return a proposal."""
    proposed_start, proposed_end = await _find_next_slot(duration_mins, user)

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
        try:
            event = await gcal.create_event(
                title=proposal.title,
                start=proposal.proposed_start,
                end=proposal.proposed_end,
                description=f"Ballast task — {proposal.title} #ballast",
            )
            await task_crud.update(session, task, gcal_event_id=event.id)
        except Exception:
            logger.exception("gcal_create_event_failed title=%r", proposal.title)

    return task
