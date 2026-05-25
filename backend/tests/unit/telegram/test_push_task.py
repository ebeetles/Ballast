"""Unit tests for push_task handler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage
from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services.schedule_service import ScheduleProposal
from app.telegram.handlers.push_task import handle


def _make_message(text: str, chat_id: int = 123) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


def _make_intent(task_name: str) -> IntentResult:
    return IntentResult(
        intent=Intent.push_task,
        confidence=0.95,
        extracted_params={"task": task_name},
    )


def _proposal(task: Task) -> ScheduleProposal:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    return ScheduleProposal(
        action="reschedule",
        task_id=str(task.id),
        title=task.title,
        duration_mins=task.duration_mins,
        proposed_start=start,
        proposed_end=start + timedelta(minutes=task.duration_mins),
        debt_delta=1.0,
    )


@pytest.mark.asyncio
async def test_push_task_found_sets_pending_confirmation(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    proposal = _proposal(task)

    with (
        patch(
            "app.telegram.handlers.push_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=task),
        ),
        patch(
            "app.telegram.handlers.push_task.schedule_service.propose_reschedule",
            new=AsyncMock(return_value=proposal),
        ),
        patch(
            "app.telegram.handlers.push_task.debt_service.get_total_debt",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        await handle(session, user, _make_message("push report"), _make_intent("report"))

    await session.refresh(user)
    assert user.pending_confirmation is not None
    assert user.pending_confirmation["action"] == "reschedule"
    mock_send_message.assert_awaited_once()
    call_text = mock_send_message.call_args[0][1]
    assert "Confirm?" in call_text
    assert task.title in call_text


@pytest.mark.asyncio
async def test_push_task_not_found_sends_task_list(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    with (
        patch(
            "app.telegram.handlers.push_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.telegram.handlers.push_task.task_service.get_user_tasks",
            new=AsyncMock(return_value=[task]),
        ),
    ):
        await handle(session, user, _make_message("push xyz"), _make_intent("xyz"))

    mock_send_message.assert_awaited_once()
    reply = mock_send_message.call_args[0][1]
    assert "couldn't find" in reply
    assert task.title in reply


@pytest.mark.asyncio
async def test_push_task_not_found_no_tasks_sends_simple_reply(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    with (
        patch(
            "app.telegram.handlers.push_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.telegram.handlers.push_task.task_service.get_user_tasks",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await handle(session, user, _make_message("push xyz"), _make_intent("xyz"))

    reply = mock_send_message.call_args[0][1]
    assert "no active tasks" in reply


@pytest.mark.asyncio
async def test_push_task_empty_name_prompts_user(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    await handle(session, user, _make_message("push"), _make_intent(""))
    mock_send_message.assert_awaited_once()
    reply = mock_send_message.call_args[0][1]
    assert "Which task" in reply


@pytest.mark.asyncio
async def test_push_task_proposal_includes_debt_info(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    proposal = _proposal(task)

    with (
        patch(
            "app.telegram.handlers.push_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=task),
        ),
        patch(
            "app.telegram.handlers.push_task.schedule_service.propose_reschedule",
            new=AsyncMock(return_value=proposal),
        ),
        patch(
            "app.telegram.handlers.push_task.debt_service.get_total_debt",
            new=AsyncMock(return_value=3.0),
        ),
    ):
        await handle(session, user, _make_message("push report"), _make_intent("report"))

    reply = mock_send_message.call_args[0][1]
    assert "Time debt" in reply
    # new_total = 3.0 (current) + 1.0 (delta) = 4.0; dot is MarkdownV2-escaped
    assert r"4\.0" in reply
