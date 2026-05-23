"""Integration tests for the multi-turn onboarding flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage
from app.db.models.task import Task
from app.db.models.user import User
from app.telegram.handlers.onboarding import handle_onboarding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(chat_id: int, text: str) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


def _mock_refine(refined_goal: str = "Complete NeetCode 150 by August 1st", target_date: str = "2026-08-01"):
    """Return a coroutine that simulates a successful LLM goal-refinement call."""
    async def _fake_refine(raw_goal: str) -> dict:
        return {"refined_goal": refined_goal, "target_date": target_date}

    return _fake_refine


CHAT_ID = 777_000_001


@pytest.fixture
async def pending_user(session: AsyncSession) -> User:
    user = User(
        telegram_chat_id=CHAT_ID,
        onboarding_status="pending",
        onboarding_step="welcome",
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Step: welcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_welcome_step_sends_intro_and_advances_step(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    msg = _make_message(CHAT_ID, "hi")
    await handle_onboarding(session, pending_user, msg)

    mock_send_message.assert_called_once()
    sent_text: str = mock_send_message.call_args[0][1]
    assert "Ballast" in sent_text
    assert "goal" in sent_text.lower()

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().onboarding_step == "goal_input"


# ---------------------------------------------------------------------------
# Step: goal_input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_input_calls_llm_and_shows_refinement(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "goal_input"

    with patch(
        "app.telegram.handlers.onboarding._refine_goal",
        side_effect=_mock_refine(),
    ):
        msg = _make_message(CHAT_ID, "I want to do neetcode")
        await handle_onboarding(session, pending_user, msg)

    mock_send_message.assert_called_once()
    sent_text: str = mock_send_message.call_args[0][1]
    assert "NeetCode 150" in sent_text

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "goal_confirm"
    assert user.onboarding_data.get("goal_raw") == "I want to do neetcode"
    assert user.onboarding_data.get("goal_refined") == "Complete NeetCode 150 by August 1st"


@pytest.mark.asyncio
async def test_goal_input_falls_back_gracefully_on_llm_error(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "goal_input"

    async def _bad_refine(_: str) -> dict:
        raise RuntimeError("LLM down")

    with patch("app.telegram.handlers.onboarding._refine_goal", side_effect=_bad_refine):
        msg = _make_message(CHAT_ID, "finish my app")
        await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "goal_confirm"
    assert user.onboarding_data.get("goal_refined") == "finish my app"


# ---------------------------------------------------------------------------
# Step: goal_confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_confirm_yes_advances_to_fixed_commitments(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "goal_confirm"
    pending_user.onboarding_data = {"goal_refined": "Complete NeetCode 150 by August 1st"}

    msg = _make_message(CHAT_ID, "yes")
    await handle_onboarding(session, pending_user, msg)

    mock_send_message.assert_called_once()
    sent_text: str = mock_send_message.call_args[0][1]
    assert "schedule" in sent_text.lower() or "commitment" in sent_text.lower()

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().onboarding_step == "fixed_commitments"


@pytest.mark.asyncio
async def test_goal_confirm_no_stores_correction_and_advances(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "goal_confirm"
    pending_user.onboarding_data = {"goal_refined": "Complete NeetCode 150 by August 1st"}

    msg = _make_message(CHAT_ID, "Actually I want to finish by July 1st")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "fixed_commitments"
    assert user.onboarding_data["goal_refined"] == "Actually I want to finish by July 1st"


# ---------------------------------------------------------------------------
# Steps: fixed_commitments → deadline → work_preference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_commitments_advances_to_deadline(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "fixed_commitments"
    msg = _make_message(CHAT_ID, "MWF classes 9-11am, gym Tue/Thu evenings")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "deadline"
    assert "MWF" in user.onboarding_data.get("fixed_commitments", "")


@pytest.mark.asyncio
async def test_deadline_advances_to_work_preference(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "deadline"
    msg = _make_message(CHAT_ID, "August 1st — that's when I want to start applying")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "work_preference"
    assert user.onboarding_data.get("deadline") != ""


@pytest.mark.asyncio
async def test_work_preference_advances_to_accountability_style(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "work_preference"
    msg = _make_message(CHAT_ID, "morning")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_step == "accountability_style"
    assert user.onboarding_data.get("work_preference") == "morning"


# ---------------------------------------------------------------------------
# Step: accountability_style
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "style_input,expected_style,expected_limit",
    [
        ("A", "A", 6.0),
        ("a", "A", 6.0),
        ("B", "B", 4.0),
        ("C", "C", 2.0),
    ],
)
@pytest.mark.asyncio
async def test_accountability_style_sets_correct_debt_limit(
    session: AsyncSession,
    mock_send_message: AsyncMock,
    style_input: str,
    expected_style: str,
    expected_limit: float,
) -> None:
    user = User(
        telegram_chat_id=CHAT_ID + hash(style_input) % 1000,
        onboarding_status="pending",
        onboarding_step="accountability_style",
        onboarding_data={"goal_refined": "Complete NeetCode 150"},
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)

    msg = _make_message(user.telegram_chat_id, style_input)
    await handle_onboarding(session, user, msg)

    result = await session.execute(select(User).where(User.id == user.id))
    updated = result.scalar_one()
    assert updated.onboarding_step == "confirm"
    assert updated.onboarding_data.get("accountability_style") == expected_style


@pytest.mark.asyncio
async def test_accountability_style_invalid_input_asks_again(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "accountability_style"
    pending_user.onboarding_data = {"goal_refined": "Complete NeetCode 150"}

    msg = _make_message(CHAT_ID, "maybe")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().onboarding_step == "accountability_style"
    sent_text: str = mock_send_message.call_args[0][1]
    assert "A, B, or C" in sent_text


# ---------------------------------------------------------------------------
# Step: confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_yes_completes_onboarding(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "confirm"
    pending_user.onboarding_data = {
        "goal_refined": "Complete NeetCode 150 by August 1st",
        "accountability_style": "B",
    }

    msg = _make_message(CHAT_ID, "yes")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    user = result.scalar_one()
    assert user.onboarding_status == "complete"
    mock_send_message.assert_called_once()
    assert "all set" in mock_send_message.call_args[0][1].lower()


@pytest.mark.asyncio
async def test_confirm_no_stays_at_confirm(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    pending_user.onboarding_step = "confirm"
    pending_user.onboarding_data = {
        "goal_refined": "Complete NeetCode 150",
        "accountability_style": "B",
    }

    msg = _make_message(CHAT_ID, "no, change the goal")
    await handle_onboarding(session, pending_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    assert result.scalar_one().onboarding_status == "pending"


# ---------------------------------------------------------------------------
# Full happy-path end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_onboarding_happy_path(
    session: AsyncSession, pending_user: User, mock_send_message: AsyncMock
) -> None:
    """Drive a user through all 7 onboarding steps in sequence."""
    steps = [
        ("welcome", "hi"),
        ("goal_input", "finish neetcode 150 by august"),
        ("goal_confirm", "yes"),
        ("fixed_commitments", "Work Mon-Fri 9-5, gym evenings"),
        ("deadline", "August 1st"),
        ("work_preference", "morning"),
        ("accountability_style", "B"),
        ("confirm", "yes"),
    ]

    with patch(
        "app.telegram.handlers.onboarding._refine_goal",
        side_effect=_mock_refine(),
    ):
        for step, text in steps:
            result = await session.execute(select(User).where(User.id == pending_user.id))
            current_user = result.scalar_one()
            assert current_user.onboarding_step == step, (
                f"Expected step={step!r}, got {current_user.onboarding_step!r}"
            )
            msg = _make_message(CHAT_ID, text)
            await handle_onboarding(session, current_user, msg)

    result = await session.execute(select(User).where(User.id == pending_user.id))
    final_user = result.scalar_one()
    assert final_user.onboarding_status == "complete"
    assert final_user.max_debt_limit == 4.0

    tasks_result = await session.execute(select(Task).where(Task.user_id == pending_user.id))
    tasks = tasks_result.scalars().all()
    assert len(tasks) == 1
    assert "NeetCode" in tasks[0].title
