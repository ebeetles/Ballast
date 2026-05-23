"""Unit tests for debt_service."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.services.debt_service import add_debt, get_total_debt, subtract_debt


@pytest.mark.asyncio
async def test_add_debt_creates_positive_entry(session: AsyncSession, user: User) -> None:
    entry = await add_debt(session, user.id, None, 2.0, "test push")
    assert entry.hours_added == 2.0
    assert entry.user_id == user.id


@pytest.mark.asyncio
async def test_subtract_debt_creates_negative_entry(session: AsyncSession, user: User) -> None:
    entry = await subtract_debt(session, user.id, None, 1.5, "completed task")
    assert entry.hours_added == -1.5


@pytest.mark.asyncio
async def test_get_total_debt_sums_all_entries(session: AsyncSession, user: User) -> None:
    await add_debt(session, user.id, None, 3.0, "push 1")
    await add_debt(session, user.id, None, 2.0, "push 2")
    await subtract_debt(session, user.id, None, 1.0, "complete")
    total = await get_total_debt(session, user.id)
    assert total == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_get_total_debt_zero_when_no_entries(session: AsyncSession, user: User) -> None:
    total = await get_total_debt(session, user.id)
    assert total == 0.0


@pytest.mark.asyncio
async def test_add_debt_with_task_id(session: AsyncSession, user: User, task) -> None:
    entry = await add_debt(session, user.id, task.id, 1.0, "push with task")
    assert entry.task_id == task.id


@pytest.mark.asyncio
async def test_subtract_debt_always_stores_negative(session: AsyncSession, user: User) -> None:
    entry = await subtract_debt(session, user.id, None, 5.0, "subtracting 5")
    assert entry.hours_added == -5.0
