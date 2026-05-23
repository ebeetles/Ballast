"""Unit tests for memory/north_star.py."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.memory.north_star import NorthStar, read_goals


@pytest.fixture
async def complete_user(session: AsyncSession) -> User:
    user = User(
        telegram_chat_id=888_000_001,
        onboarding_status="complete",
        onboarding_data={
            "goal_raw": "finish leetcode",
            "goal_refined": "Complete all 150 NeetCode problems by August 1st",
            "goal_target_date": "2026-08-01",
            "work_preference": "morning",
            "accountability_style": "B",
        },
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


@pytest.fixture
async def incomplete_user(session: AsyncSession) -> User:
    user = User(telegram_chat_id=888_000_002, onboarding_status="pending")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# read_goals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_goals_returns_none_when_user_not_found(session: AsyncSession) -> None:
    import uuid
    result = await read_goals(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_read_goals_returns_none_when_incomplete(
    session: AsyncSession, incomplete_user: User
) -> None:
    result = await read_goals(session, incomplete_user.id)
    assert result is None


@pytest.mark.asyncio
async def test_read_goals_returns_north_star_after_onboarding(
    session: AsyncSession, complete_user: User
) -> None:
    result = await read_goals(session, complete_user.id)
    assert result is not None
    assert isinstance(result, NorthStar)


@pytest.mark.asyncio
async def test_read_goals_populates_goals_list(
    session: AsyncSession, complete_user: User
) -> None:
    result = await read_goals(session, complete_user.id)
    assert result is not None
    assert len(result.goals) == 1
    assert "NeetCode" in result.goals[0]


@pytest.mark.asyncio
async def test_read_goals_populates_deadline(
    session: AsyncSession, complete_user: User
) -> None:
    result = await read_goals(session, complete_user.id)
    assert result is not None
    goal = result.goals[0]
    assert goal in result.deadlines
    assert result.deadlines[goal] == "2026-08-01"


@pytest.mark.asyncio
async def test_read_goals_populates_preferences(
    session: AsyncSession, complete_user: User
) -> None:
    result = await read_goals(session, complete_user.id)
    assert result is not None
    assert result.preferences.get("work_preference") == "morning"
    assert result.preferences.get("accountability_style") == "B"


@pytest.mark.asyncio
async def test_read_goals_uses_goal_raw_when_refined_missing(session: AsyncSession) -> None:
    user = User(
        telegram_chat_id=888_000_003,
        onboarding_status="complete",
        onboarding_data={"goal_raw": "run a marathon"},
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)

    result = await read_goals(session, user.id)
    assert result is not None
    assert result.goals == ["run a marathon"]


# ---------------------------------------------------------------------------
# NorthStar.format_for_prompt
# ---------------------------------------------------------------------------


def test_north_star_format_for_prompt_includes_goal() -> None:
    ns = NorthStar(
        goals=["Complete NeetCode 150 by August 1st"],
        deadlines={"Complete NeetCode 150 by August 1st": "2026-08-01"},
        preferences={"work_preference": "morning"},
    )
    text = ns.format_for_prompt()
    assert "Complete NeetCode 150" in text
    assert "2026-08-01" in text
    assert "morning" in text


def test_north_star_format_for_prompt_empty_goals() -> None:
    ns = NorthStar()
    text = ns.format_for_prompt()
    assert "none set" in text.lower()


def test_north_star_format_for_prompt_no_deadline() -> None:
    ns = NorthStar(goals=["Launch the app"], deadlines={}, preferences={})
    text = ns.format_for_prompt()
    assert "Launch the app" in text
    assert "by" not in text
