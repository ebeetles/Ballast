"""Unit tests for confirmation handler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage
from app.db.crud import message_crud, task_crud, user_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services.schedule_service import ScheduleProposal
from app.telegram.handlers.confirmation import handle_confirmation


def _make_message(text: str, chat_id: int = 123) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


def _reschedule_proposal(task: Task) -> dict:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    return ScheduleProposal(
        action="reschedule",
        task_id=str(task.id),
        title=task.title,
        duration_mins=task.duration_mins,
        proposed_start=start,
        proposed_end=start + timedelta(minutes=task.duration_mins),
        debt_delta=1.0,
    ).model_dump(mode="json")


def _add_task_proposal() -> dict:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    return ScheduleProposal(
        action="add_task",
        title="New task",
        duration_mins=45,
        proposed_start=start,
        proposed_end=start + timedelta(minutes=45),
    ).model_dump(mode="json")


async def _set_confirmation(session: AsyncSession, user: User, proposal: dict) -> User:
    return await user_crud.update(session, user, pending_confirmation=proposal)


# ---------------------------------------------------------------------------
# Yes — reschedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yes_calls_commit_reschedule(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))

    commit_mock = AsyncMock()
    with patch(
        "app.telegram.handlers.confirmation.schedule_service.commit_reschedule",
        new=commit_mock,
    ):
        await handle_confirmation(session, user, _make_message("yes"))

    commit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_yes_clears_pending_confirmation(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))

    with patch(
        "app.telegram.handlers.confirmation.schedule_service.commit_reschedule",
        new=AsyncMock(),
    ):
        await handle_confirmation(session, user, _make_message("yes"))

    await session.refresh(user)
    assert user.pending_confirmation is None


@pytest.mark.asyncio
async def test_yes_calls_commit_new_task(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    await _set_confirmation(session, user, _add_task_proposal())

    created_task = await task_crud.create(
        session,
        user_id=user.id,
        title="New task",
        duration_mins=45,
        status=TaskStatus.PENDING.value,
    )
    commit_mock = AsyncMock(return_value=created_task)

    with patch(
        "app.telegram.handlers.confirmation.schedule_service.commit_new_task",
        new=commit_mock,
    ):
        await handle_confirmation(session, user, _make_message("yep"))

    commit_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# No — cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_clears_pending_confirmation(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))
    await handle_confirmation(session, user, _make_message("no"))

    await session.refresh(user)
    assert user.pending_confirmation is None


@pytest.mark.asyncio
async def test_no_sends_cancel_message(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))
    await handle_confirmation(session, user, _make_message("nope"))

    reply = mock_send_message.call_args[0][1]
    assert "keeping it as is" in reply


# ---------------------------------------------------------------------------
# Unrecognized input — re-prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrecognized_input_keeps_confirmation_and_reprompts(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    proposal = _reschedule_proposal(task)
    await _set_confirmation(session, user, proposal)

    await handle_confirmation(session, user, _make_message("maybe later"))

    await session.refresh(user)
    assert user.pending_confirmation is not None
    reply = mock_send_message.call_args[0][1]
    assert "yes/no" in reply


# ---------------------------------------------------------------------------
# Variants of yes/no
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["yes", "y", "yeah", "yup", "ok", "sure", "confirm"])
@pytest.mark.asyncio
async def test_yes_variants_accepted(
    word: str,
    session: AsyncSession,
    user: User,
    task: Task,
    mock_send_message: AsyncMock,
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))

    with patch(
        "app.telegram.handlers.confirmation.schedule_service.commit_reschedule",
        new=AsyncMock(),
    ):
        await handle_confirmation(session, user, _make_message(word))

    await session.refresh(user)
    assert user.pending_confirmation is None


@pytest.mark.parametrize("word", ["no", "n", "nope", "cancel"])
@pytest.mark.asyncio
async def test_no_variants_accepted(
    word: str,
    session: AsyncSession,
    user: User,
    task: Task,
    mock_send_message: AsyncMock,
) -> None:
    await _set_confirmation(session, user, _reschedule_proposal(task))
    await handle_confirmation(session, user, _make_message(word))

    await session.refresh(user)
    assert user.pending_confirmation is None


# ---------------------------------------------------------------------------
# Conversation history persistence
# ---------------------------------------------------------------------------


async def _messages_for(session: AsyncSession, user: User) -> list[tuple[str, str]]:
    rows = await message_crud.list(session, limit=100)
    return [(m.role, m.content) for m in rows if m.user_id == user.id]


@pytest.mark.asyncio
async def test_yes_persists_messages(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    """Yes branch saves both the user reply and the bot's 'Done' message."""
    await _set_confirmation(session, user, _reschedule_proposal(task))

    with patch(
        "app.telegram.handlers.confirmation.schedule_service.commit_reschedule",
        new=AsyncMock(),
    ):
        await handle_confirmation(session, user, _make_message("yes"))

    history = await _messages_for(session, user)
    roles = [r for r, _ in history]
    assert "user" in roles
    assert "assistant" in roles
    assistant_replies = [c for r, c in history if r == "assistant"]
    assert any("Done" in c for c in assistant_replies)


@pytest.mark.asyncio
async def test_no_persists_messages(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    """No branch saves both the user 'no' and the cancellation reply."""
    await _set_confirmation(session, user, _reschedule_proposal(task))
    await handle_confirmation(session, user, _make_message("no"))

    history = await _messages_for(session, user)
    assistant_replies = [c for r, c in history if r == "assistant"]
    user_msgs = [c for r, c in history if r == "user"]
    assert "no" in user_msgs
    assert any("keeping it as is" in c for c in assistant_replies)


@pytest.mark.asyncio
async def test_reprompt_persists_messages(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    """Unrecognized input still persists both the user reply and the reprompt."""
    await _set_confirmation(session, user, _reschedule_proposal(task))
    await handle_confirmation(session, user, _make_message("maybe later"))

    history = await _messages_for(session, user)
    assistant_replies = [c for r, c in history if r == "assistant"]
    user_msgs = [c for r, c in history if r == "user"]
    assert "maybe later" in user_msgs
    assert any("yes/no" in c for c in assistant_replies)
