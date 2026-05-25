"""Task lookup, fuzzy-match, and lifecycle helpers."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User


def _nearest_deadline(tasks: list[Task]) -> Task:
    """Return the task with the nearest deadline (nulls last)."""
    tasks_sorted = sorted(tasks, key=lambda t: (t.deadline_at is None, t.deadline_at or ""))
    return tasks_sorted[0]


async def find_task_by_name(session: AsyncSession, user_id: UUID, query: str) -> Task | None:
    """Return the best matching active task using a 4-tier priority system.

    Tier 1: Exact match (case-insensitive)
    Tier 2: Query contained in task title
    Tier 3: Task title contained in query
    Tier 4: Word overlap (words > 3 chars)

    When multiple tasks match at the same tier, returns the one with the nearest deadline.
    """
    result = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.status.in_([TaskStatus.PENDING.value, TaskStatus.PUSHED.value]),
        )
    )
    tasks = list(result.scalars().all())
    if not tasks:
        return None

    needle = query.lower().strip()

    # Tier 1: exact match
    for t in tasks:
        if t.title.lower() == needle:
            return t

    # Tier 2: query contained in title
    tier2 = [t for t in tasks if needle in t.title.lower()]
    if tier2:
        return _nearest_deadline(tier2)

    # Tier 3: title contained in query
    tier3 = [t for t in tasks if t.title.lower() in needle]
    if tier3:
        return _nearest_deadline(tier3)

    # Tier 4: word overlap (words > 3 chars)
    needle_words = {w for w in needle.split() if len(w) > 3}
    if needle_words:
        def _word_overlap(task: Task) -> int:
            title_words = {w for w in task.title.lower().split() if len(w) > 3}
            return len(needle_words & title_words)

        tier4 = [(t, _word_overlap(t)) for t in tasks if _word_overlap(t) > 0]
        if tier4:
            best_score = max(score for _, score in tier4)
            best_tasks = [t for t, score in tier4 if score == best_score]
            return _nearest_deadline(best_tasks)

    return None


async def find_tasks_by_compound_name(
    session: AsyncSession,
    user_id: UUID,
    query: str,
) -> list[tuple[Task | None, str]]:
    """Return (task_or_None, query_part) tuples for compound 'X and Y' queries."""
    parts = [p.strip() for p in re.split(r"\band\b", query, flags=re.IGNORECASE)]
    results: list[tuple[Task | None, str]] = []
    for part in parts:
        if part:
            results.append((await find_task_by_name(session, user_id, part), part))
    return results


async def get_user_tasks(
    session: AsyncSession,
    user_id: UUID,
    status: str | None = None,
) -> list[Task]:
    """Return tasks for a user, optionally filtered by status string."""
    stmt = select(Task).where(Task.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def complete_task(
    session: AsyncSession,
    task_id_str: str,
    user: User,
) -> dict[str, Any]:
    """Mark a task as completed and reduce debt if it was previously pushed.

    Returns a result dict describing what happened. Callers must commit the
    session after calling this function.
    """
    from app.services import debt_service

    task = await task_crud.get(session, UUID(task_id_str))
    if task is None or task.user_id != user.id:
        return {"error": "task not found"}

    was_pushed = task.status == TaskStatus.PUSHED.value
    await task_crud.update(session, task, status=TaskStatus.COMPLETED.value)

    debt_reduced = 0.0
    if was_pushed:
        hours = round(task.duration_mins / 60, 2)
        await debt_service.subtract_debt(
            session,
            user_id=user.id,
            task_id=task.id,
            hours=hours,
            reason=f"Completed pushed task '{task.title}'",
        )
        debt_reduced = hours

    return {
        "title": task.title,
        "status": "completed",
        "debt_reduced_hours": debt_reduced,
    }


async def delete_task(
    session: AsyncSession,
    user: User,
    *,
    task_name: str | None = None,
    task_id_str: str | None = None,
) -> dict[str, Any]:
    """Public alias for cancel_task used by the delete confirmation flow."""
    return await cancel_task(session, user, task_name=task_name, task_id_str=task_id_str)


async def cancel_task(
    session: AsyncSession,
    user: User,
    *,
    task_name: str | None = None,
    task_id_str: str | None = None,
) -> dict[str, Any]:
    """Cancel/remove an active task and delete its calendar event if present.

    Marks the task as forfeit. If the task was pushed, subtracts the associated
    time debt. Returns a result dict; callers must commit the session.
    """
    from app.core.config import settings
    from app.services import debt_service

    task: Task | None = None
    if task_id_str:
        task = await task_crud.get(session, UUID(task_id_str))
        if task is not None and task.user_id != user.id:
            task = None
    elif task_name:
        task = await find_task_by_name(session, user.id, task_name)

    if task is None:
        return {"error": "task not found"}

    was_pushed = task.status == TaskStatus.PUSHED.value

    if settings.google_calendar_credentials_file and task.gcal_event_id:
        try:
            from app.calendar.gcal_client import GoogleCalendarClient

            gcal = GoogleCalendarClient()
            await gcal.delete_event(task.gcal_event_id)
        except Exception:
            pass

    await task_crud.update(session, task, status=TaskStatus.FORFEIT.value, gcal_event_id=None)

    debt_reduced = 0.0
    if was_pushed:
        hours = round(task.duration_mins / 60, 2)
        await debt_service.subtract_debt(
            session,
            user_id=user.id,
            task_id=task.id,
            hours=hours,
            reason=f"Removed pushed task '{task.title}'",
        )
        debt_reduced = hours

    return {
        "title": task.title,
        "status": "removed",
        "debt_reduced_hours": debt_reduced,
    }


async def delete_all_tasks(
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Delete every task row for the user and remove associated GCal events.

    Returns counts of deleted tasks and calendar events. Callers must commit
    the session after calling this function.
    """
    from app.core.config import settings
    from app.core.logging import get_logger

    log = get_logger(__name__)
    tasks = await get_user_tasks(session, user.id)
    if not tasks:
        return {"deleted_count": 0, "gcal_deleted_count": 0}

    gcal = None
    if settings.google_calendar_credentials_file:
        try:
            from app.calendar.gcal_client import GoogleCalendarClient

            gcal = GoogleCalendarClient()
        except Exception:
            log.exception("gcal_client_init_failed")

    gcal_deleted = 0
    if gcal is not None:
        for task in tasks:
            if task.gcal_event_id:
                try:
                    await gcal.delete_event(task.gcal_event_id)
                    gcal_deleted += 1
                except Exception:
                    log.exception(
                        "delete_all_gcal_failed event_id=%s",
                        task.gcal_event_id,
                    )

    for task in tasks:
        await task_crud.delete(session, task)

    return {
        "deleted_count": len(tasks),
        "gcal_deleted_count": gcal_deleted,
        "status": "all_tasks_deleted",
    }
