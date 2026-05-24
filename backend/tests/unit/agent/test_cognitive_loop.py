"""Tests for agent/cognitive_loop.py — all Anthropic API and DB calls are mocked."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.cognitive_loop import run
from app.agent.context_assembler import AgentContext, MessageSummary
from app.memory.north_star import NorthStar
from app.agent.context_assembler import TimeDebtSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_anthropic_response(text: str) -> MagicMock:
    """Build a fake Anthropic Messages response with a single text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


def _make_anthropic_client(response: MagicMock) -> MagicMock:
    """Return a mock AsyncAnthropic instance whose messages.create returns response."""
    messages_mock = MagicMock()
    messages_mock.create = AsyncMock(return_value=response)
    client = MagicMock()
    client.messages = messages_mock
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def mock_context() -> AgentContext:
    """AgentContext with two prior conversation messages and no tasks/debt."""
    now = datetime.now(timezone.utc)
    return AgentContext(
        north_star=NorthStar(goals=["Get a software engineering job"], deadlines={}, preferences={}),
        active_tasks=[],
        time_debt=TimeDebtSummary(total_hours=0.0, max_debt_limit=8.0, percentage=0.0),
        insights=[],
        recent_messages=[
            MessageSummary(role="user", content="I'm struggling to focus today.", created_at=now),
            MessageSummary(role="assistant", content="That's understandable. What's pulling your attention?", created_at=now),
        ],
        current_time=now,
        user_timezone="UTC",
    )


@pytest.fixture
def patch_session():
    """Patch async_session_factory so no real DB connection is made."""
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=mock_session)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    with patch("app.agent.cognitive_loop.async_session_factory", return_value=ctx_manager):
        yield mock_session


@pytest.fixture
def patch_assemble(mock_context: AgentContext):
    """Patch assemble_context to return mock_context without hitting the DB."""
    with patch(
        "app.agent.cognitive_loop.assemble_context",
        new_callable=AsyncMock,
        return_value=mock_context,
    ) as mock:
        yield mock


@pytest.fixture
def patch_save():
    """Patch message_service.save_message to capture calls without hitting the DB."""
    with patch(
        "app.agent.cognitive_loop.message_service.save_message",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


@pytest.fixture
def patch_anthropic():
    """Patch anthropic.AsyncAnthropic in the cognitive_loop module."""
    with patch("app.agent.cognitive_loop.anthropic.AsyncAnthropic") as mock_cls:
        yield mock_cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_run_returns_string(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
) -> None:
    """Happy path: run() returns a non-empty string from the LLM."""
    llm_reply = "You pushed this task three days in a row. What's actually going on?"
    patch_anthropic.return_value = _make_anthropic_client(_make_anthropic_response(llm_reply))

    result = await run(user_id, "I don't feel like doing it today.")

    assert isinstance(result, str)
    assert result == llm_reply


async def test_conversation_history_included_in_llm_call(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    mock_context: AgentContext,
) -> None:
    """The messages array passed to the LLM contains prior history before the current message."""
    client_mock = _make_anthropic_client(_make_anthropic_response("Got it."))
    patch_anthropic.return_value = client_mock

    current_message = "Just checking in."
    await run(user_id, current_message)

    call_kwargs = client_mock.messages.create.call_args
    messages_sent = call_kwargs.kwargs.get("messages") or call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["messages"]

    # The two history messages appear before the current user message
    assert len(messages_sent) == 3
    assert messages_sent[0]["role"] == "user"
    assert messages_sent[0]["content"] == mock_context.recent_messages[0].content
    assert messages_sent[1]["role"] == "assistant"
    assert messages_sent[1]["content"] == mock_context.recent_messages[1].content
    assert messages_sent[2]["role"] == "user"
    assert messages_sent[2]["content"] == current_message


async def test_user_and_assistant_messages_saved(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
) -> None:
    """Both the user message and the assistant response are persisted via message_service."""
    llm_reply = "You've been consistent this week — keep it up."
    patch_anthropic.return_value = _make_anthropic_client(_make_anthropic_response(llm_reply))

    user_message = "I finished the neetcode session."
    await run(user_id, user_message)

    assert patch_save.await_count == 2

    calls = patch_save.await_args_list
    roles_saved = {call.args[2] for call in calls}
    contents_saved = {call.args[3] for call in calls}

    assert "user" in roles_saved
    assert "assistant" in roles_saved
    assert user_message in contents_saved
    assert llm_reply in contents_saved


async def test_llm_failure_returns_fallback(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
) -> None:
    """When the LLM call raises, run() returns the graceful fallback message without raising."""
    client_mock = MagicMock()
    client_mock.messages.create = AsyncMock(side_effect=Exception("API timeout"))
    patch_anthropic.return_value = client_mock

    result = await run(user_id, "Hey, what should I work on?")

    assert result == "I'm having trouble thinking right now. Try again in a moment."
