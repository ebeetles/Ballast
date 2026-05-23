"""Task lookup and fuzzy-match helpers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task, TaskStatus


async def find_task_by_name(session: AsyncSession, user_id: UUID, query: str) -> Task | None:
    """Return the best matching active task for user by case-insensitive substring.

    If multiple tasks match, returns the one with the nearest deadline.
    Tasks without a deadline are sorted last.
    """
    result = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.status.in_([TaskStatus.PENDING.value, TaskStatus.PUSHED.value]),
        )
    )
    tasks = list(result.scalars().all())

    needle = query.lower()
    matches = [t for t in tasks if needle in t.title.lower()]

    if not matches:
        return None

    matches.sort(
        key=lambda t: (t.deadline_at is None, t.deadline_at or "")
    )
    return matches[0]


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
