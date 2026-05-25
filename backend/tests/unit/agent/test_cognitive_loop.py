"""Tests for agent/cognitive_loop.py — all Anthropic API and DB calls are mocked."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.agent.cognitive_loop import MAX_TOOL_ITERATIONS, run
from app.agent.context_assembler import AgentContext, MessageSummary, TimeDebtSummary
from app.agent.response_formatter import _ERROR_FALLBACK, format_for_telegram, format_proposal
from app.memory.north_star import NorthStar
from app.services.schedule_service import ScheduleProposal


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
    """Happy path: run() returns a non-empty MarkdownV2-formatted string from the LLM."""
    llm_reply = "You pushed this task three days in a row. What's actually going on?"
    patch_anthropic.return_value = _make_anthropic_client(_make_anthropic_response(llm_reply))

    result = await run(user_id, "I don't feel like doing it today.")

    assert isinstance(result, str)
    assert result == format_for_telegram(llm_reply)


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

    assert result == _ERROR_FALLBACK


# ---------------------------------------------------------------------------
# Tool-use tests
# ---------------------------------------------------------------------------


def _make_tool_use_response(tool_name: str = "get_tasks", tool_id: str = "tu_abc") -> MagicMock:
    """Build a fake Anthropic response whose stop_reason is tool_use."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = tool_id
    tool_block.name = tool_name
    tool_block.input = {}

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [tool_block]
    return response


def _make_text_response(text: str) -> MagicMock:
    """Build a fake Anthropic response with a single text block."""
    return _make_anthropic_response(text)


@pytest.fixture
def patch_execute_tool():
    """Patch execute_tool in cognitive_loop so we can inspect calls."""
    with patch(
        "app.agent.cognitive_loop.execute_tool",
        new_callable=AsyncMock,
        return_value={"tasks": []},
    ) as mock:
        yield mock


@pytest.fixture
def patch_user_crud():
    """Patch user_crud.get so the loop can fetch a user object without a real DB."""
    mock_user = MagicMock()
    mock_user.id = uuid.uuid4()
    mock_user.max_debt_limit = 8.0
    with patch(
        "app.agent.cognitive_loop.user_crud.get",
        new_callable=AsyncMock,
        return_value=mock_user,
    ) as mock:
        yield mock, mock_user


async def test_tool_use_response_triggers_execution(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    patch_execute_tool,
    patch_user_crud,
) -> None:
    """When the first response has stop_reason=tool_use, the tool is executed and a
    follow-up call is made to get the final text response."""
    tool_response = _make_tool_use_response("get_tasks", "tu_001")
    final_text = "You have two active tasks."
    text_response = _make_text_response(final_text)

    client_mock = _make_anthropic_client(tool_response)
    client_mock.messages.create = AsyncMock(side_effect=[tool_response, text_response])
    patch_anthropic.return_value = client_mock

    result = await run(user_id, "What tasks do I have?")

    assert result == format_for_telegram(final_text)
    patch_execute_tool.assert_awaited_once()
    call_args = patch_execute_tool.await_args
    assert call_args.args[0] == "get_tasks"
    assert client_mock.messages.create.await_count == 2


async def test_max_iterations_prevents_infinite_loop(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    patch_execute_tool,
    patch_user_crud,
) -> None:
    """The loop terminates after MAX_TOOL_ITERATIONS even if every response is tool_use."""
    always_tool = _make_tool_use_response("get_tasks")
    client_mock = _make_anthropic_client(always_tool)
    client_mock.messages.create = AsyncMock(return_value=always_tool)
    patch_anthropic.return_value = client_mock

    result = await run(user_id, "What are my tasks?")

    assert isinstance(result, str)
    assert client_mock.messages.create.await_count == MAX_TOOL_ITERATIONS


async def test_create_task_tool_sets_pending_confirmation(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    patch_user_crud,
    mock_context: AgentContext,
) -> None:
    """When create_task fires and returns a proposal, run() short-circuits and returns
    the formatted proposal via format_proposal() — no second LLM call is made."""
    tool_response = _make_tool_use_response("create_task", "tu_002")
    proposal_result = {
        "action": "add_task",
        "title": "Finish slides",
        "duration_mins": 45,
        "proposed_start": "2026-05-24T14:00:00+00:00",
        "proposed_end": "2026-05-24T14:45:00+00:00",
        "debt_delta": 0.0,
        "status": "proposal_pending_confirmation",
    }

    with patch(
        "app.agent.cognitive_loop.execute_tool",
        new_callable=AsyncMock,
        return_value=proposal_result,
    ) as mock_execute:
        client_mock = _make_anthropic_client(tool_response)
        # Only one LLM call — the proposal short-circuits the loop
        client_mock.messages.create = AsyncMock(return_value=tool_response)
        patch_anthropic.return_value = client_mock

        result = await run(user_id, "Add a task to finish my slides")

    expected = format_proposal(
        ScheduleProposal(
            action="add_task",
            title="Finish slides",
            duration_mins=45,
            proposed_start=datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc),
            proposed_end=datetime(2026, 5, 24, 14, 45, tzinfo=timezone.utc),
            debt_delta=0.0,
        ),
        current_debt=mock_context.time_debt.total_hours,
        max_debt=mock_context.time_debt.max_debt_limit,
        user_timezone=mock_context.user_timezone,
    )

    assert result == expected
    mock_execute.assert_awaited_once()
    assert mock_execute.await_args.args[0] == "create_task"
    assert client_mock.messages.create.await_count == 1


async def test_debug_logging_dumps_messages_array(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    caplog,
) -> None:
    """At DEBUG level, run() logs the messages array sent to Anthropic per API call."""
    import logging

    patch_anthropic.return_value = _make_anthropic_client(_make_anthropic_response("ok"))

    caplog.set_level(logging.DEBUG, logger="app.agent.cognitive_loop")
    await run(user_id, "what's going on?")

    api_call_logs = [
        record for record in caplog.records
        if record.message.startswith("cognitive_loop_api_call")
    ]
    assert api_call_logs, "expected at least one cognitive_loop_api_call log entry"
    assert any("iteration=0" in r.message for r in api_call_logs)
    assert any("messages=" in r.message for r in api_call_logs)


async def test_text_only_response_unchanged(
    user_id: uuid.UUID,
    patch_session,
    patch_assemble,
    patch_save,
    patch_anthropic,
    patch_user_crud,
) -> None:
    """When the LLM returns a plain text response (no tools), behaviour is identical
    to the pre-tool-use path — one API call, correct text returned."""
    expected = "You pushed this task three days in a row."
    client_mock = _make_anthropic_client(_make_text_response(expected))
    patch_anthropic.return_value = client_mock

    with patch("app.agent.cognitive_loop.execute_tool", new_callable=AsyncMock) as mock_execute:
        result = await run(user_id, "I don't feel like doing it today.")

    assert result == format_for_telegram(expected)
    client_mock.messages.create.assert_awaited_once()
    mock_execute.assert_not_awaited()
