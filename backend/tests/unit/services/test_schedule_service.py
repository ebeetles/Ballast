"""Unit tests for schedule_service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services.schedule_service import (
    ScheduleProposal,
    commit_new_task,
    commit_reschedule,
    propose_reschedule,
    propose_slot,
)


def _make_slot(hours_from_now: int = 1, duration_mins: int = 60):
    from app.api.v1.schemas.calendar import TimeSlot
    start = datetime.now(tz=timezone.utc) + timedelta(hours=hours_from_now)
    end = start + timedelta(minutes=duration_mins)
    return TimeSlot(start=start, end=end, preference_score=1.0)


# ---------------------------------------------------------------------------
# propose_reschedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_reschedule_returns_proposal(user: User, task: Task) -> None:
    slot = _make_slot(duration_mins=task.duration_mins)
    with patch(
        "app.services.schedule_service._get_gcal_client",
        return_value=None,
    ):
        proposal = await propose_reschedule(task, user)

    assert proposal.action == "reschedule"
    assert proposal.task_id == str(task.id)
    assert proposal.title == task.title
    assert proposal.duration_mins == task.duration_mins
    assert proposal.debt_delta == pytest.approx(task.duration_mins / 60)


@pytest.mark.asyncio
async def test_propose_reschedule_uses_gcal_when_available(user: User, task: Task) -> None:
    slot = _make_slot(duration_mins=task.duration_mins)
    mock_gcal = MagicMock()
    mock_gcal.find_free_slots = AsyncMock(return_value=[slot])

    with patch("app.services.schedule_service._get_gcal_client", return_value=mock_gcal):
        proposal = await propose_reschedule(task, user)

    assert proposal.proposed_start == slot.start
    assert proposal.proposed_end == slot.end


# ---------------------------------------------------------------------------
# commit_reschedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_reschedule_updates_task_status(
    session: AsyncSession, user: User, task: Task
) -> None:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    end = start + timedelta(minutes=task.duration_mins)
    proposal = ScheduleProposal(
        action="reschedule",
        task_id=str(task.id),
        title=task.title,
        duration_mins=task.duration_mins,
        proposed_start=start,
        proposed_end=end,
        debt_delta=1.0,
    )

    with patch("app.services.schedule_service._get_gcal_client", return_value=None):
        await commit_reschedule(proposal, session, user)

    await session.refresh(task)
    assert task.status == TaskStatus.PUSHED.value
    assert task.scheduled_at is not None


@pytest.mark.asyncio
async def test_commit_reschedule_logs_debt(
    session: AsyncSession, user: User, task: Task
) -> None:
    from app.services.debt_service import get_total_debt

    start = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    end = start + timedelta(minutes=60)
    proposal = ScheduleProposal(
        action="reschedule",
        task_id=str(task.id),
        title=task.title,
        duration_mins=60,
        proposed_start=start,
        proposed_end=end,
        debt_delta=2.5,
    )

    with patch("app.services.schedule_service._get_gcal_client", return_value=None):
        await commit_reschedule(proposal, session, user)

    total = await get_total_debt(session, user.id)
    assert total == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# propose_slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_slot_returns_add_task_proposal(user: User) -> None:
    with patch("app.services.schedule_service._get_gcal_client", return_value=None):
        proposal = await propose_slot("Read paper", 30, user)

    assert proposal.action == "add_task"
    assert proposal.title == "Read paper"
    assert proposal.duration_mins == 30
    assert proposal.task_id is None


@pytest.mark.asyncio
async def test_propose_slot_preserves_deadline(user: User) -> None:
    dl = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with patch("app.services.schedule_service._get_gcal_client", return_value=None):
        proposal = await propose_slot("Exam prep", 120, user, deadline_at=dl)

    assert proposal.deadline_at == dl


# ---------------------------------------------------------------------------
# commit_new_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_new_task_creates_task_in_db(
    session: AsyncSession, user: User
) -> None:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    end = start + timedelta(minutes=45)
    proposal = ScheduleProposal(
        action="add_task",
        title="New task",
        duration_mins=45,
        proposed_start=start,
        proposed_end=end,
    )

    with patch("app.services.schedule_service._get_gcal_client", return_value=None):
        task = await commit_new_task(proposal, session, user)

    assert task.title == "New task"
    assert task.duration_mins == 45
    assert task.user_id == user.id


@pytest.mark.asyncio
async def test_commit_new_task_stores_gcal_event_id(
    session: AsyncSession, user: User
) -> None:
    from app.api.v1.schemas.calendar import Event

    start = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    end = start + timedelta(minutes=30)
    proposal = ScheduleProposal(
        action="add_task",
        title="GCal task",
        duration_mins=30,
        proposed_start=start,
        proposed_end=end,
    )

    fake_event = Event(id="gcal-event-123", title="GCal task", start=start, end=end)
    mock_gcal = MagicMock()
    mock_gcal.create_event = AsyncMock(return_value=fake_event)

    with patch("app.services.schedule_service._get_gcal_client", return_value=mock_gcal):
        task = await commit_new_task(proposal, session, user)

    await session.refresh(task)
    assert task.gcal_event_id == "gcal-event-123"
