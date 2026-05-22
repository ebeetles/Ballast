"""Integration tests for the Telegram dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage, TelegramUpdate, TelegramUser
from app.db.crud import user_crud
from app.telegram import dispatcher


def _make_update(
    chat_id: int = 42,
    text: str | None = "hello",
    update_id: int = 1,
) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(
            message_id=1,
            **{"from": TelegramUser(id=chat_id, first_name="User")},
            chat=TelegramChat(id=chat_id, type="private"),
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_dispatcher_onboarding_gate_blocks_pending_user(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """Pending users hit the onboarding gate; no reply is sent."""
    existing = await user_crud.create(session, telegram_chat_id=42, onboarding_status="pending")
    await session.commit()

    with patch("app.telegram.dispatcher.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=42, text="hello"))

    mock_send_message.assert_not_awaited()

    result = await user_crud.list(session)
    assert len(result) == 1
    assert result[0].id == existing.id


@pytest.mark.asyncio
async def test_dispatcher_creates_user_when_unknown(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """New users are created with pending status; onboarding gate stops processing."""
    with patch("app.telegram.dispatcher.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=99, text="hi"))

    users = await user_crud.list(session)
    assert len(users) == 1
    assert users[0].telegram_chat_id == 99
    assert users[0].onboarding_status == "pending"
    mock_send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_routes_intent_for_complete_user(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """Complete users get their message routed and receive a debug intent reply."""
    await user_crud.create(session, telegram_chat_id=55, onboarding_status="complete")
    await session.commit()

    intent_result = IntentResult(
        intent=Intent.push_task,
        confidence=0.95,
        extracted_params={"task": "leetcode"},
    )

    with (
        patch("app.telegram.dispatcher.async_session_factory") as mock_factory,
        patch("app.telegram.dispatcher.classify_intent", return_value=intent_result) as mock_classify,
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=55, text="can't do leetcode tonight"))

    mock_classify.assert_awaited_once()
    sent_text = mock_send_message.call_args[0][1]
    assert "push_task" in sent_text
    assert "0.95" in sent_text


@pytest.mark.asyncio
async def test_dispatcher_no_echo_when_no_text(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    with patch("app.telegram.dispatcher.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=77, text=None))

    mock_send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_no_op_when_no_message(mock_send_message: AsyncMock) -> None:
    update = TelegramUpdate(update_id=5, message=None)
    await dispatcher.handle_update(update)
    mock_send_message.assert_not_awaited()
