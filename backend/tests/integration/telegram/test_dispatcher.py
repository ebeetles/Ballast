"""Integration tests for the Telegram dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
async def test_dispatcher_echoes_message_to_known_user(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    existing = await user_crud.create(session, telegram_chat_id=42, onboarding_status="pending")
    await session.commit()

    with patch("app.telegram.dispatcher.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=42, text="hello"))

    mock_send_message.assert_awaited_once_with(42, "hello")

    result = await user_crud.list(session)
    assert len(result) == 1
    assert result[0].id == existing.id


@pytest.mark.asyncio
async def test_dispatcher_creates_user_when_unknown(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    with patch("app.telegram.dispatcher.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        await dispatcher.handle_update(_make_update(chat_id=99, text="hi"))

    users = await user_crud.list(session)
    assert len(users) == 1
    assert users[0].telegram_chat_id == 99
    assert users[0].onboarding_status == "pending"

    mock_send_message.assert_awaited_once_with(99, "hi")


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
