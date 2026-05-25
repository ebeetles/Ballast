"""Tests for delete_task handler (confirmation-based deletion)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage, TelegramUser
from app.db.models.task import TaskStatus
from app.db.models.user import User
from app.telegram.handlers.delete_task import handle


def _make_message(text: str = "delete test task", chat_id: int = 1) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        **{"from": TelegramUser(id=chat_id, first_name="User")},
        chat=TelegramChat(id=chat_id, type="private"),
        text=text,
    )


@pytest.mark.asyncio
async def test_delete_task_sets_pending_confirmation(
    session: AsyncSession,
    user: User,
    task,
    mock_send_message: AsyncMock,
) -> None:
    """Handler should set pending_confirmation and NOT delete immediately."""
    intent = IntentResult(
        intent=Intent.delete_task, confidence=0.95, extracted_params={"task": "report"}
    )

    await handle(session, user, _make_message(), intent)
    await session.flush()
    await session.refresh(user)

    # Task should still be pending — not deleted yet
    from app.db.crud import task_crud
    updated = await task_crud.get(session, task.id)
    assert updated is not None
    assert updated.status == TaskStatus.PENDING.value

    # Confirmation should be set
    assert user.pending_confirmation is not None
    assert user.pending_confirmation["action"] == "delete_task"
    assert user.pending_confirmation["task_id"] == str(task.id)

    # Message should ask for confirmation
    mock_send_message.assert_awaited_once()
    sent_text = mock_send_message.call_args.args[1]
    assert "Delete" in sent_text
    assert "yes or no" in sent_text
