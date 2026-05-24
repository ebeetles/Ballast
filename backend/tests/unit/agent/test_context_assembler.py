"""Tests for agent/context_assembler.py — all DB calls are mocked."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.context_assembler import (
    AgentContext,
    InsightSummary,
    MessageSummary,
    NorthStar,
    TaskSummary,
    TimeDebtSummary,
    assemble_context,
    to_prompt_string,
)
from app.db.models.task import TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scalar_result(value):
    """Mock a result whose .scalar_one_or_none() returns value."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _scalars_result(rows):
    """Mock a result whose .scalars().all() returns rows."""
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    return result


def _make_user(*, timezone_str="UTC", max_debt_limit=8.0):
    user = MagicMock()
    user.timezone = timezone_str
    user.max_debt_limit = max_debt_limit
    user.onboarding_status = "complete"
    user.onboarding_data = {}
    return user


def _make_task(*, title="Write report", status=TaskStatus.PENDING.value, deadline_at=None):
    task = MagicMock()
    task.id = uuid.uuid4()
    task.title = title
    task.duration_mins = 60
    task.deadline_at = deadline_at
    task.status = status
    task.is_fixed = False
    task.requires_proof = False
    return task


def _make_insight(*, category="focus", insight="Morning blocks are best", strength=9):
    ins = MagicMock()
    ins.category = category
    ins.insight = insight
    ins.strength = strength
    return ins


def _make_message(*, role="user", content="Hello"):
    msg = MagicMock()
    msg.role = role
    msg.content = content
    msg.created_at = datetime(2026, 5, 23, 10, 0, 0, tzinfo=timezone.utc)
    return msg


def _make_session(user=None, tasks=None, debt_sum=None, insights=None, messages=None):
    """Return an AsyncMock session whose execute() yields results in call order."""
    session = AsyncMock()
    results = [
        # 1. User query → scalar_one_or_none
        _scalar_result(user),
        # 2. Tasks query → scalars().all()
        _scalars_result(tasks or []),
        # 3. Debt sum query → scalar_one_or_none
        _scalar_result(debt_sum),
        # 4. Insights query → scalars().all()
        _scalars_result(insights or []),
        # 5. Messages query → scalars().all()
        _scalars_result(messages or []),
    ]
    session.execute = AsyncMock(side_effect=results)
    return session


# ---------------------------------------------------------------------------
# test_assemble_context_shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assemble_context_shape():
    """assemble_context returns a fully populated AgentContext with correct types."""
    user = _make_user(timezone_str="America/New_York", max_debt_limit=8.0)
    task = _make_task(title="Leetcode session", status=TaskStatus.PENDING.value)
    insight = _make_insight(category="focus", insight="Best in morning", strength=9)
    message = _make_message(role="user", content="Hi there")
    session = _make_session(
        user=user,
        tasks=[task],
        debt_sum=4.0,
        insights=[insight],
        messages=[message],
    )
    north_star = NorthStar(goals=["Get a job"], deadlines={"Get a job": "2026-12-31"})

    with patch(
        "app.agent.context_assembler.read_goals",
        new=AsyncMock(return_value=north_star),
    ):
        ctx = await assemble_context(session, uuid.uuid4())

    assert isinstance(ctx, AgentContext)
    assert ctx.north_star is north_star
    assert ctx.user_timezone == "America/New_York"

    assert len(ctx.active_tasks) == 1
    t = ctx.active_tasks[0]
    assert isinstance(t, TaskSummary)
    assert t.title == "Leetcode session"
    assert t.status == TaskStatus.PENDING.value

    assert isinstance(ctx.time_debt, TimeDebtSummary)
    assert ctx.time_debt.total_hours == 4.0
    assert ctx.time_debt.max_debt_limit == 8.0

    assert len(ctx.insights) == 1
    assert isinstance(ctx.insights[0], InsightSummary)
    assert ctx.insights[0].strength == 9

    assert len(ctx.recent_messages) == 1
    assert isinstance(ctx.recent_messages[0], MessageSummary)
    assert ctx.recent_messages[0].role == "user"

    assert isinstance(ctx.current_time, datetime)
    assert ctx.current_time.tzinfo is not None


# ---------------------------------------------------------------------------
# test_to_prompt_string_sections
# ---------------------------------------------------------------------------

def _build_full_context() -> AgentContext:
    deadline = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return AgentContext(
        north_star=NorthStar(
            goals=["Land a SWE job"],
            deadlines={"Land a SWE job": "2026-12-31"},
            preferences={"work_preference": "mornings"},
        ),
        active_tasks=[
            TaskSummary(
                id=uuid.uuid4(),
                title="Leetcode hard",
                duration_mins=90,
                deadline_at=deadline,
                status="pending",
                is_fixed=False,
                requires_proof=True,
            )
        ],
        time_debt=TimeDebtSummary(total_hours=4.0, max_debt_limit=8.0, percentage=0.5),
        insights=[InsightSummary(category="focus", insight="Best in mornings", strength=9)],
        recent_messages=[
            MessageSummary(role="user", content="Hello", created_at=datetime.now(timezone.utc)),
            MessageSummary(role="assistant", content="Hi!", created_at=datetime.now(timezone.utc)),
        ],
        current_time=datetime(2026, 5, 23, 18, 0, 0, tzinfo=timezone.utc),
        user_timezone="UTC",
    )


def test_to_prompt_string_sections():
    """All six section headers appear in the output."""
    ctx = _build_full_context()
    output = to_prompt_string(ctx)

    assert "=== NORTH STAR ===" in output
    assert "=== ACTIVE TASKS ===" in output
    assert "=== TIME DEBT ===" in output
    assert "=== BEHAVIORAL INSIGHTS ===" in output
    assert "=== RECENT CONVERSATION ===" in output
    assert "=== CURRENT TIME ===" in output


def test_to_prompt_string_content():
    """Key values appear in the formatted output."""
    ctx = _build_full_context()
    output = to_prompt_string(ctx)

    assert "Land a SWE job" in output
    assert "2026-12-31" in output
    assert "Leetcode hard" in output
    assert "proof required" in output
    assert "4.0h of 8.0h limit (50.0%)" in output
    assert "Best in mornings" in output
    assert "user: Hello" in output
    assert "assistant: Hi!" in output


# ---------------------------------------------------------------------------
# test_empty_states
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_states_no_errors():
    """assemble_context with no tasks/insights/messages returns gracefully."""
    user = _make_user()
    session = _make_session(user=user, tasks=[], debt_sum=None, insights=[], messages=[])

    with patch(
        "app.agent.context_assembler.read_goals",
        new=AsyncMock(return_value=None),
    ):
        ctx = await assemble_context(session, uuid.uuid4())

    assert ctx.active_tasks == []
    assert ctx.insights == []
    assert ctx.recent_messages == []
    assert ctx.time_debt.total_hours == 0.0
    assert isinstance(ctx.north_star, NorthStar)


def test_to_prompt_string_empty_states():
    """to_prompt_string handles all-empty context without errors."""
    ctx = AgentContext(
        north_star=NorthStar(),
        active_tasks=[],
        time_debt=TimeDebtSummary(total_hours=0.0, max_debt_limit=0.0, percentage=0.0),
        insights=[],
        recent_messages=[],
        current_time=datetime.now(timezone.utc),
        user_timezone="UTC",
    )
    output = to_prompt_string(ctx)

    assert "=== NORTH STAR ===" in output
    assert "(no goals set)" in output
    assert "(none)" in output
    assert "0.0h of 0.0h limit (0.0%)" in output
    assert "(none)" in output
    assert "(no history)" in output


# ---------------------------------------------------------------------------
# test_time_debt_percentage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_debt_percentage_half():
    """4h debt against 8h limit yields 0.5 (50%)."""
    session = _make_session(user=_make_user(max_debt_limit=8.0), debt_sum=4.0)

    with patch("app.agent.context_assembler.read_goals", new=AsyncMock(return_value=None)):
        ctx = await assemble_context(session, uuid.uuid4())

    assert ctx.time_debt.total_hours == 4.0
    assert ctx.time_debt.max_debt_limit == 8.0
    assert ctx.time_debt.percentage == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_time_debt_percentage_zero_limit():
    """Zero max_debt_limit yields 0.0 percentage (no division by zero)."""
    session = _make_session(user=_make_user(max_debt_limit=0.0), debt_sum=3.0)

    with patch("app.agent.context_assembler.read_goals", new=AsyncMock(return_value=None)):
        ctx = await assemble_context(session, uuid.uuid4())

    assert ctx.time_debt.percentage == 0.0


@pytest.mark.asyncio
async def test_time_debt_percentage_full():
    """8h debt against 8h limit yields 1.0 (100%)."""
    session = _make_session(user=_make_user(max_debt_limit=8.0), debt_sum=8.0)

    with patch("app.agent.context_assembler.read_goals", new=AsyncMock(return_value=None)):
        ctx = await assemble_context(session, uuid.uuid4())

    assert ctx.time_debt.percentage == pytest.approx(1.0)


def test_time_debt_prompt_format():
    """Time debt section renders with correct formatting."""
    ctx = AgentContext(
        north_star=NorthStar(),
        time_debt=TimeDebtSummary(total_hours=3.5, max_debt_limit=10.0, percentage=0.35),
        current_time=datetime.now(timezone.utc),
        user_timezone="UTC",
    )
    output = to_prompt_string(ctx)
    assert "3.5h of 10.0h limit (35.0%)" in output
