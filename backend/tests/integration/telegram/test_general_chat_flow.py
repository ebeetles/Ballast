"""Integration tests for the complete general_chat message lifecycle.

Flow under test:
  webhook POST → dispatcher → classify_intent → general_chat handler →
  cognitive_loop → format_for_telegram → send_message(parse_mode="MarkdownV2")

All external I/O (Anthropic API, Telegram API) is mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context_assembler import AgentContext, TimeDebtSummary
from app.agent.response_formatter import _ERROR_FALLBACK, format_for_telegram
from app.agent.router import Intent, IntentResult
from app.api.v1.schemas.webhook import TelegramChat, TelegramMessage, TelegramUpdate, TelegramUser
from app.db.crud import user_crud
from app.db.models.message import Message
from app.memory.north_star import NorthStar
from app.telegram import dispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(chat_id: int = 1001, text: str = "How am I doing?") -> TelegramUpdate:
    return TelegramUpdate(
        update_id=1,
        message=TelegramMessage(
            message_id=1,
            **{"from": TelegramUser(id=chat_id, first_name="Test")},
            chat=TelegramChat(id=chat_id, type="private"),
            text=text,
        ),
    )


def _make_agent_context() -> AgentContext:
    now = datetime.now(timezone.utc)
    return AgentContext(
        north_star=NorthStar(goals=["Ship Ballast"], deadlines={}, preferences={}),
        active_tasks=[],
        time_debt=TimeDebtSummary(total_hours=1.0, max_debt_limit=8.0, percentage=0.125),
        insights=[],
        recent_messages=[],
        current_time=now,
        user_timezone="UTC",
    )


def _make_llm_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    return resp


_GENERAL_CHAT_INTENT = IntentResult(
    intent=Intent.general_chat, confidence=0.95, extracted_params={}
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_general_chat_sends_markdownv2(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """send_message is called with parse_mode='MarkdownV2' for general_chat responses."""
    chat_id = 2001
    await user_crud.create(session, telegram_chat_id=chat_id, onboarding_status="complete")
    await session.commit()

    llm_reply = "You're doing well. Keep pushing."
    expected_text = format_for_telegram(llm_reply)

    with (
        patch("app.telegram.dispatcher.async_session_factory") as mock_factory,
        patch("app.telegram.dispatcher.classify_intent", new_callable=AsyncMock,
              return_value=_GENERAL_CHAT_INTENT),
        patch("app.agent.cognitive_loop.async_session_factory") as mock_loop_factory,
        patch("app.agent.cognitive_loop.assemble_context", new_callable=AsyncMock,
              return_value=_make_agent_context()),
        patch("app.agent.cognitive_loop.user_crud.get", new_callable=AsyncMock,
              return_value=MagicMock(id="fake-id", max_debt_limit=8.0)),
        patch("app.agent.cognitive_loop.anthropic.AsyncAnthropic") as mock_anthropic,
        patch("app.agent.cognitive_loop.message_service.save_message", new_callable=AsyncMock),
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_loop_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_loop_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm_client = MagicMock()
        llm_client.messages.create = AsyncMock(return_value=_make_llm_text_response(llm_reply))
        mock_anthropic.return_value = llm_client

        await dispatcher.handle_update(_make_update(chat_id=chat_id))

    mock_send_message.assert_awaited_once()
    call_args = mock_send_message.call_args
    sent_text = call_args.args[1]
    sent_parse_mode = call_args.kwargs.get("parse_mode")

    assert sent_parse_mode == "MarkdownV2"
    assert sent_text == expected_text
    assert isinstance(sent_text, str)
    assert len(sent_text) <= 4096


@pytest.mark.asyncio
async def test_general_chat_response_is_nonempty(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """The response sent to Telegram is always non-empty, even for terse LLM replies."""
    chat_id = 2002
    await user_crud.create(session, telegram_chat_id=chat_id, onboarding_status="complete")
    await session.commit()

    with (
        patch("app.telegram.dispatcher.async_session_factory") as mock_factory,
        patch("app.telegram.dispatcher.classify_intent", new_callable=AsyncMock,
              return_value=_GENERAL_CHAT_INTENT),
        patch("app.agent.cognitive_loop.async_session_factory") as mock_loop_factory,
        patch("app.agent.cognitive_loop.assemble_context", new_callable=AsyncMock,
              return_value=_make_agent_context()),
        patch("app.agent.cognitive_loop.user_crud.get", new_callable=AsyncMock,
              return_value=MagicMock(id="fake-id", max_debt_limit=8.0)),
        patch("app.agent.cognitive_loop.anthropic.AsyncAnthropic") as mock_anthropic,
        patch("app.agent.cognitive_loop.message_service.save_message", new_callable=AsyncMock),
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_loop_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_loop_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm_client = MagicMock()
        llm_client.messages.create = AsyncMock(return_value=_make_llm_text_response("Ok"))
        mock_anthropic.return_value = llm_client

        await dispatcher.handle_update(_make_update(chat_id=chat_id, text="k"))

    mock_send_message.assert_awaited_once()
    sent_text = mock_send_message.call_args.args[1]
    assert sent_text


@pytest.mark.asyncio
async def test_general_chat_messages_persisted_to_db(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """Both the user message and assistant response are persisted to the messages table."""
    chat_id = 2003
    user = await user_crud.create(
        session, telegram_chat_id=chat_id, onboarding_status="complete"
    )
    await session.commit()

    user_text = "How's my progress?"
    llm_reply = "You're on track."

    with (
        patch("app.telegram.dispatcher.async_session_factory") as mock_factory,
        patch("app.telegram.dispatcher.classify_intent", new_callable=AsyncMock,
              return_value=_GENERAL_CHAT_INTENT),
        patch("app.agent.cognitive_loop.async_session_factory") as mock_loop_factory,
        patch("app.agent.cognitive_loop.assemble_context", new_callable=AsyncMock,
              return_value=_make_agent_context()),
        patch("app.agent.cognitive_loop.user_crud.get", new_callable=AsyncMock,
              return_value=MagicMock(id=user.id, max_debt_limit=8.0)),
        patch("app.agent.cognitive_loop.anthropic.AsyncAnthropic") as mock_anthropic,
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_loop_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_loop_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm_client = MagicMock()
        llm_client.messages.create = AsyncMock(return_value=_make_llm_text_response(llm_reply))
        mock_anthropic.return_value = llm_client

        await dispatcher.handle_update(_make_update(chat_id=chat_id, text=user_text))

    result = await session.execute(
        select(Message).where(Message.user_id == user.id)
    )
    messages = list(result.scalars().all())

    assert len(messages) == 2
    roles = {m.role for m in messages}
    assert "user" in roles
    assert "assistant" in roles

    user_msg = next(m for m in messages if m.role == "user")
    assistant_msg = next(m for m in messages if m.role == "assistant")

    assert user_msg.content == user_text
    # DB stores the raw LLM text before MarkdownV2 escaping
    assert assistant_msg.content == llm_reply


@pytest.mark.asyncio
async def test_general_chat_llm_failure_sends_fallback(
    session: AsyncSession,
    mock_send_message: AsyncMock,
) -> None:
    """When the LLM call fails, a safe MarkdownV2-escaped fallback is sent."""
    chat_id = 2004
    await user_crud.create(session, telegram_chat_id=chat_id, onboarding_status="complete")
    await session.commit()

    with (
        patch("app.telegram.dispatcher.async_session_factory") as mock_factory,
        patch("app.telegram.dispatcher.classify_intent", new_callable=AsyncMock,
              return_value=_GENERAL_CHAT_INTENT),
        patch("app.agent.cognitive_loop.async_session_factory") as mock_loop_factory,
        patch("app.agent.cognitive_loop.assemble_context", new_callable=AsyncMock,
              return_value=_make_agent_context()),
        patch("app.agent.cognitive_loop.user_crud.get", new_callable=AsyncMock,
              return_value=MagicMock(id="fake-id", max_debt_limit=8.0)),
        patch("app.agent.cognitive_loop.anthropic.AsyncAnthropic") as mock_anthropic,
        patch("app.agent.cognitive_loop.message_service.save_message", new_callable=AsyncMock),
    ):
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_loop_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_loop_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        llm_client = MagicMock()
        llm_client.messages.create = AsyncMock(side_effect=Exception("timeout"))
        mock_anthropic.return_value = llm_client

        await dispatcher.handle_update(_make_update(chat_id=chat_id))

    mock_send_message.assert_awaited_once()
    sent_text = mock_send_message.call_args.args[1]
    assert sent_text == _ERROR_FALLBACK
