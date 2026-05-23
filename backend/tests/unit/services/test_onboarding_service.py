"""Unit tests for onboarding_service."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from app.db.models.user import User
from app.services.onboarding_service import (
    OnboardingState,
    complete_onboarding,
    get_onboarding_state,
    save_onboarding_answer,
)


@pytest.fixture
async def pending_user(session: AsyncSession) -> User:
    user = User(telegram_chat_id=999_000_001, onboarding_status="pending")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# get_onboarding_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_onboarding_state_returns_dataclass(pending_user: User) -> None:
    state = get_onboarding_state(pending_user)
    assert isinstance(state, OnboardingState)
    assert state.step == "welcome"
    assert state.data == {}


@pytest.mark.asyncio
async def test_get_onboarding_state_reflects_existing_data(session: AsyncSession) -> None:
    user = User(
        telegram_chat_id=999_000_002,
        onboarding_status="pending",
        onboarding_step="goal_input",
        onboarding_data={"goal_raw": "finish leetcode"},
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)

    state = get_onboarding_state(user)
    assert state.step == "goal_input"
    assert state.data["goal_raw"] == "finish leetcode"


# ---------------------------------------------------------------------------
# save_onboarding_answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_answer_persists_to_onboarding_data(
    session: AsyncSession, pending_user: User
) -> None:
    await save_onboarding_answer(session, pending_user, "goal_raw", "finish leetcode")

    result = await session.execute(
        select(User).where(User.id == pending_user.id)
    )
    refreshed = result.scalar_one()
    assert refreshed.onboarding_data["goal_raw"] == "finish leetcode"


@pytest.mark.asyncio
async def test_save_answer_does_not_overwrite_other_keys(
    session: AsyncSession, pending_user: User
) -> None:
    await save_onboarding_answer(session, pending_user, "goal_raw", "finish leetcode")
    await save_onboarding_answer(session, pending_user, "goal_refined", "Complete NeetCode 150")

    result = await session.execute(select(User).where(User.id == pending_user.id))
    refreshed = result.scalar_one()
    assert refreshed.onboarding_data["goal_raw"] == "finish leetcode"
    assert refreshed.onboarding_data["goal_refined"] == "Complete NeetCode 150"


# ---------------------------------------------------------------------------
# complete_onboarding — status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_onboarding_sets_status_complete(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {"accountability_style": "B", "goal_refined": "Ship the app"}
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    refreshed = result.scalar_one()
    assert refreshed.onboarding_status == "complete"


# ---------------------------------------------------------------------------
# complete_onboarding — max_debt_limit by accountability style
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_onboarding_gentle_sets_debt_limit_6(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {"accountability_style": "A", "goal_refined": "Run a marathon"}
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().max_debt_limit == 6.0


@pytest.mark.asyncio
async def test_complete_onboarding_firm_sets_debt_limit_4(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {"accountability_style": "B", "goal_refined": "Run a marathon"}
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().max_debt_limit == 4.0


@pytest.mark.asyncio
async def test_complete_onboarding_brutal_sets_debt_limit_2(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {"accountability_style": "C", "goal_refined": "Run a marathon"}
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().max_debt_limit == 2.0


@pytest.mark.asyncio
async def test_complete_onboarding_unknown_style_defaults_firm(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {"goal_refined": "Run a marathon"}
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().max_debt_limit == 4.0


# ---------------------------------------------------------------------------
# complete_onboarding — initial task creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_onboarding_creates_initial_task(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {
        "accountability_style": "B",
        "goal_refined": "Complete NeetCode 150 by August 1st",
    }
    await complete_onboarding(session, pending_user)

    result = await session.execute(
        select(Task).where(Task.user_id == pending_user.id)
    )
    tasks = result.scalars().all()
    assert len(tasks) == 1
    assert "NeetCode" in tasks[0].title


@pytest.mark.asyncio
async def test_complete_onboarding_uses_goal_raw_when_refined_missing(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {
        "accountability_style": "A",
        "goal_raw": "learn piano",
    }
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(Task).where(Task.user_id == pending_user.id))
    tasks = result.scalars().all()
    assert tasks[0].title == "learn piano"


@pytest.mark.asyncio
async def test_complete_onboarding_sets_deadline_on_task(
    session: AsyncSession, pending_user: User
) -> None:
    pending_user.onboarding_data = {
        "accountability_style": "B",
        "goal_refined": "Pass the bar exam",
        "goal_target_date": "2027-02-01",
    }
    await complete_onboarding(session, pending_user)

    result = await session.execute(select(Task).where(Task.user_id == pending_user.id))
    task = result.scalars().first()
    assert task is not None
    assert task.deadline_at is not None
