"""Unit tests for complete_task handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage
from app.db.crud import task_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.telegram.handlers.complete_task import handle


def _make_message(text: str, chat_id: int = 123) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


def _make_intent(task_name: str) -> IntentResult:
    return IntentResult(
        intent=Intent.complete_task,
        confidence=0.95,
        extracted_params={"task": task_name},
    )


@pytest.mark.asyncio
async def test_complete_task_sets_completed_status(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    with patch(
        "app.telegram.handlers.complete_task.task_service.find_task_by_name",
        new=AsyncMock(return_value=task),
    ):
        await handle(session, user, _make_message("done"), _make_intent("report"))

    await session.refresh(task)
    assert task.status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_complete_task_sends_confirmation_message(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    with patch(
        "app.telegram.handlers.complete_task.task_service.find_task_by_name",
        new=AsyncMock(return_value=task),
    ):
        await handle(session, user, _make_message("done"), _make_intent("report"))

    mock_send_message.assert_awaited_once()
    reply = mock_send_message.call_args[0][1]
    assert "complete" in reply.lower()
    assert task.title in reply


@pytest.mark.asyncio
async def test_complete_task_requires_proof_sets_awaiting_proof(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    proof_task = await task_crud.create(
        session,
        user_id=user.id,
        title="Proof task",
        duration_mins=60,
        requires_proof=True,
        status=TaskStatus.PENDING.value,
    )

    with patch(
        "app.telegram.handlers.complete_task.task_service.find_task_by_name",
        new=AsyncMock(return_value=proof_task),
    ):
        await handle(session, user, _make_message("done"), _make_intent("proof task"))

    await session.refresh(proof_task)
    assert proof_task.status == TaskStatus.AWAITING_PROOF.value
    reply = mock_send_message.call_args[0][1]
    assert "proof" in reply.lower()


@pytest.mark.asyncio
async def test_complete_task_subtracts_debt_when_was_pushed(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    pushed_task = await task_crud.create(
        session,
        user_id=user.id,
        title="Pushed task",
        duration_mins=60,
        status=TaskStatus.PUSHED.value,
    )

    subtract_mock = AsyncMock()
    with (
        patch(
            "app.telegram.handlers.complete_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=pushed_task),
        ),
        patch(
            "app.telegram.handlers.complete_task.debt_service.subtract_debt",
            new=subtract_mock,
        ),
    ):
        await handle(session, user, _make_message("done"), _make_intent("pushed task"))

    subtract_mock.assert_awaited_once()
    kwargs = subtract_mock.call_args
    assert kwargs[1]["hours"] == pytest.approx(60 / 60)


@pytest.mark.asyncio
async def test_complete_task_no_debt_subtraction_for_pending(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    subtract_mock = AsyncMock()
    with (
        patch(
            "app.telegram.handlers.complete_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=task),
        ),
        patch(
            "app.telegram.handlers.complete_task.debt_service.subtract_debt",
            new=subtract_mock,
        ),
    ):
        await handle(session, user, _make_message("done"), _make_intent("report"))

    subtract_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_task_not_found_sends_list(
    session: AsyncSession, user: User, task: Task, mock_send_message: AsyncMock
) -> None:
    with (
        patch(
            "app.telegram.handlers.complete_task.task_service.find_task_by_name",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.telegram.handlers.complete_task.task_service.get_user_tasks",
            new=AsyncMock(return_value=[task]),
        ),
    ):
        await handle(session, user, _make_message("done xyz"), _make_intent("xyz"))

    reply = mock_send_message.call_args[0][1]
    assert "couldn't find" in reply
    assert task.title in reply


@pytest.mark.asyncio
async def test_complete_task_empty_name_prompts(
    session: AsyncSession, user: User, mock_send_message: AsyncMock
) -> None:
    await handle(session, user, _make_message("done"), _make_intent(""))
    reply = mock_send_message.call_args[0][1]
    assert "Which task" in reply
