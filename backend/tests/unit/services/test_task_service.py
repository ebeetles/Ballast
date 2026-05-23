"""Unit tests for task_service."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services.task_service import find_task_by_name, get_user_tasks


@pytest.fixture
async def user_with_tasks(session: AsyncSession, user: User) -> list[Task]:
    t1 = await task_crud.create(
        session,
        user_id=user.id,
        title="Write report",
        duration_mins=60,
        status=TaskStatus.PENDING.value,
        deadline_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )
    t2 = await task_crud.create(
        session,
        user_id=user.id,
        title="Write blog post",
        duration_mins=45,
        status=TaskStatus.PENDING.value,
        deadline_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
    )
    t3 = await task_crud.create(
        session,
        user_id=user.id,
        title="Send email",
        duration_mins=15,
        status=TaskStatus.PENDING.value,
    )
    return [t1, t2, t3]


# ---------------------------------------------------------------------------
# find_task_by_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_task_exact_match(session: AsyncSession, user: User, user_with_tasks) -> None:
    result = await find_task_by_name(session, user.id, "Send email")
    assert result is not None
    assert result.title == "Send email"


@pytest.mark.asyncio
async def test_find_task_case_insensitive(session: AsyncSession, user: User, user_with_tasks) -> None:
    result = await find_task_by_name(session, user.id, "SEND EMAIL")
    assert result is not None
    assert result.title == "Send email"


@pytest.mark.asyncio
async def test_find_task_partial_match(session: AsyncSession, user: User, user_with_tasks) -> None:
    result = await find_task_by_name(session, user.id, "email")
    assert result is not None
    assert "email" in result.title.lower()


@pytest.mark.asyncio
async def test_find_task_multi_match_returns_nearest_deadline(
    session: AsyncSession, user: User, user_with_tasks
) -> None:
    # "write" matches both "Write report" (deadline Jun 10) and "Write blog post" (deadline Jun 5)
    result = await find_task_by_name(session, user.id, "write")
    assert result is not None
    # blog post has the earlier deadline so it should be returned
    assert result.title == "Write blog post"


@pytest.mark.asyncio
async def test_find_task_no_match_returns_none(
    session: AsyncSession, user: User, user_with_tasks
) -> None:
    result = await find_task_by_name(session, user.id, "nonexistent xyz")
    assert result is None


@pytest.mark.asyncio
async def test_find_task_ignores_completed(session: AsyncSession, user: User) -> None:
    await task_crud.create(
        session,
        user_id=user.id,
        title="Old task",
        duration_mins=30,
        status=TaskStatus.COMPLETED.value,
    )
    result = await find_task_by_name(session, user.id, "old task")
    assert result is None


@pytest.mark.asyncio
async def test_find_task_includes_pushed_status(session: AsyncSession, user: User) -> None:
    pushed = await task_crud.create(
        session,
        user_id=user.id,
        title="Pushed task",
        duration_mins=30,
        status=TaskStatus.PUSHED.value,
    )
    result = await find_task_by_name(session, user.id, "pushed task")
    assert result is not None
    assert result.id == pushed.id


# ---------------------------------------------------------------------------
# get_user_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_tasks_all(session: AsyncSession, user: User, user_with_tasks) -> None:
    tasks = await get_user_tasks(session, user.id)
    assert len(tasks) == 3


@pytest.mark.asyncio
async def test_get_user_tasks_filtered_by_status(
    session: AsyncSession, user: User, user_with_tasks
) -> None:
    tasks = await get_user_tasks(session, user.id, status=TaskStatus.PENDING.value)
    assert all(t.status == TaskStatus.PENDING.value for t in tasks)


@pytest.mark.asyncio
async def test_get_user_tasks_empty_when_no_tasks(session: AsyncSession, user: User) -> None:
    tasks = await get_user_tasks(session, user.id)
    assert tasks == []
