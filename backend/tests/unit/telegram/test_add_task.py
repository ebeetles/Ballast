"""Unit tests for add_task handler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage
from app.db.models.user import User
from app.services.schedule_service import ScheduleProposal
from app.telegram.handlers.add_task import (
    AWAITING_TITLE_ACTION,
    _parse_duration_mins,
    handle,
    handle_awaiting_title,
)


def _make_message(text: str, chat_id: int = 123) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


def _make_intent(title: str = "New task", duration_mins: int = 60, **extra) -> IntentResult:
    params = {"title": title, "duration_mins": duration_mins, **extra}
    return IntentResult(
        intent=Intent.add_task,
        confidence=0.95,
        extracted_params=params,
    )


def _make_proposal(title: str = "New task", duration_mins: int = 60) -> ScheduleProposal:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    return ScheduleProposal(
        action="add_task",
        title=title,
        duration_mins=duration_mins,
        proposed_start=start,
        proposed_end=start + timedelta(minutes=duration_mins),
    )


@pytest.mark.asyncio
async def test_add_task_sets_pending_confirmation(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proposal = _make_proposal()
    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=AsyncMock(return_value=proposal),
    ):
        await handle(session, user, _make_message("add task"), _make_intent())

    await session.refresh(user)
    assert user.pending_confirmation is not None
    assert user.pending_confirmation["action"] == "add_task"
    assert user.pending_confirmation["title"] == "New task"


@pytest.mark.asyncio
async def test_add_task_sends_proposal_message(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proposal = _make_proposal(title="Read paper", duration_mins=30)
    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=AsyncMock(return_value=proposal),
    ):
        await handle(session, user, _make_message("add task"), _make_intent("Read paper", 30))

    mock_send_message.assert_awaited_once()
    reply = mock_send_message.call_args[0][1]
    assert "Read paper" in reply
    assert "30min" in reply
    assert "Confirm?" in reply


@pytest.mark.asyncio
async def test_add_task_empty_title_prompts_user(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    await handle(session, user, _make_message("add task"), _make_intent(title=""))
    reply = mock_send_message.call_args[0][1]
    assert "name of the task" in reply
    await session.refresh(user)
    assert user.pending_confirmation is not None
    assert user.pending_confirmation["action"] == AWAITING_TITLE_ACTION


@pytest.mark.asyncio
async def test_add_task_reads_task_param_from_router(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proposal = _make_proposal(title="study block", duration_mins=120)
    propose_mock = AsyncMock(return_value=proposal)

    intent = IntentResult(
        intent=Intent.add_task,
        confidence=0.98,
        extracted_params={
            "task": "study block",
            "duration": "2 hours",
            "day": "friday",
        },
    )

    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=propose_mock,
    ):
        await handle(session, user, _make_message("add a 2 hour study block friday"), intent)

    propose_mock.assert_awaited_once()
    assert propose_mock.call_args[1]["title"] == "study block"
    assert propose_mock.call_args[1]["duration_mins"] == 120


@pytest.mark.asyncio
async def test_parse_duration_mins_from_natural_language() -> None:
    assert _parse_duration_mins({"duration": "2 hours"}) == 120
    assert _parse_duration_mins({"duration": "30 minutes"}) == 30
    assert _parse_duration_mins({"duration": "1 hour"}) == 60
    assert _parse_duration_mins({}) == 60


@pytest.mark.asyncio
async def test_handle_awaiting_title_completes_flow(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    from app.db.crud import user_crud

    await user_crud.update(
        session,
        user,
        pending_confirmation={
            "action": AWAITING_TITLE_ACTION,
            "duration_mins": 60,
            "deadline_at": None,
            "requires_proof": False,
        },
    )
    proposal = _make_proposal(title="Morning run", duration_mins=60)

    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=AsyncMock(return_value=proposal),
    ):
        await handle_awaiting_title(session, user, _make_message("Morning run"))

    await session.refresh(user)
    assert user.pending_confirmation["action"] == "add_task"
    assert user.pending_confirmation["title"] == "Morning run"
    reply = mock_send_message.call_args[0][1]
    assert "Confirm?" in reply


@pytest.mark.asyncio
async def test_add_task_calls_propose_slot_with_correct_params(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proposal = _make_proposal()
    propose_mock = AsyncMock(return_value=proposal)

    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=propose_mock,
    ):
        await handle(
            session,
            user,
            _make_message("add task"),
            _make_intent("Study session", 90),
        )

    propose_mock.assert_awaited_once()
    args, kwargs = propose_mock.call_args
    assert kwargs["title"] == "Study session"
    assert kwargs["duration_mins"] == 90


@pytest.mark.asyncio
async def test_add_task_parses_deadline_from_params(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proposal = _make_proposal()
    propose_mock = AsyncMock(return_value=proposal)
    deadline_str = "2026-07-01T00:00:00"

    with patch(
        "app.telegram.handlers.add_task.schedule_service.propose_slot",
        new=propose_mock,
    ):
        await handle(
            session,
            user,
            _make_message("add task"),
            _make_intent("Exam prep", 120, deadline=deadline_str),
        )

    _, kwargs = propose_mock.call_args
    assert kwargs["deadline_at"] == datetime.fromisoformat(deadline_str)
