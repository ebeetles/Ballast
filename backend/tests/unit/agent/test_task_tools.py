"""Tests for agent/tools/task_tools.py — all service and DB calls are mocked."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.tools.task_tools import (
    TOOL_DEFINITIONS,
    execute_analyze_schedule_for_goal,
    execute_check_calendar,
    execute_complete_task,
    execute_create_task,
    execute_delete_all_tasks,
    execute_get_tasks,
    execute_get_time_debt,
    execute_tool,
)
from app.db.models.task import TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(max_debt_limit: float = 8.0) -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.max_debt_limit = max_debt_limit
    user.pending_confirmation = None
    return user


def _make_session() -> AsyncMock:
    return AsyncMock()


def _make_task(
    title: str = "Write report",
    duration_mins: int = 60,
    status: str = TaskStatus.PENDING.value,
) -> MagicMock:
    task = MagicMock()
    task.id = uuid.uuid4()
    task.title = title
    task.duration_mins = duration_mins
    task.status = status
    task.deadline_at = None
    task.requires_proof = False
    return task


# ---------------------------------------------------------------------------
# TOOL_DEFINITIONS schema
# ---------------------------------------------------------------------------


def test_tool_definitions_schema() -> None:
    """Each entry must have name, description, and input_schema with type=object."""
    assert len(TOOL_DEFINITIONS) == 8
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {
        "create_task",
        "delete_task",
        "delete_all_tasks",
        "complete_task",
        "get_tasks",
        "get_time_debt",
        "check_calendar",
        "analyze_schedule_for_goal",
    }

    for tool in TOOL_DEFINITIONS:
        assert "name" in tool
        assert "description" in tool
        assert isinstance(tool["description"], str) and tool["description"]
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"
        assert "properties" in tool["input_schema"]


def test_create_task_schema_required_fields() -> None:
    """create_task should only require title; the executor fills sensible defaults."""
    schema = next(t for t in TOOL_DEFINITIONS if t["name"] == "create_task")
    assert schema["input_schema"]["required"] == ["title"]
    assert "duration_mins" in schema["input_schema"]["properties"]
    assert "recurrence" in schema["input_schema"]["properties"]


def test_complete_task_schema_allows_title_or_id() -> None:
    """complete_task must accept task_id or title; neither is strictly required."""
    schema = next(t for t in TOOL_DEFINITIONS if t["name"] == "complete_task")
    assert schema["input_schema"].get("required", []) == []
    assert "task_id" in schema["input_schema"]["properties"]
    assert "title" in schema["input_schema"]["properties"]


def test_check_calendar_schema_required_fields() -> None:
    """check_calendar must require date."""
    schema = next(t for t in TOOL_DEFINITIONS if t["name"] == "check_calendar")
    assert "date" in schema["input_schema"]["required"]


# ---------------------------------------------------------------------------
# execute_create_task
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_proposal() -> MagicMock:
    proposal = MagicMock()
    proposal.title = "Finish slides"
    proposal.duration_mins = 45
    proposal.proposed_start = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
    proposal.proposed_end = datetime(2026, 5, 24, 14, 45, tzinfo=timezone.utc)
    proposal.deadline_at = None
    proposal.requires_proof = False
    proposal.model_dump = MagicMock(return_value={
        "action": "add_task",
        "title": "Finish slides",
        "duration_mins": 45,
    })
    return proposal


async def test_execute_create_task_returns_proposal(mock_proposal) -> None:
    """execute_create_task returns a proposal dict and stores pending_confirmation."""
    session = _make_session()
    user = _make_user()

    with (
        patch("app.agent.tools.task_tools.schedule_service.propose_slot", new_callable=AsyncMock, return_value=mock_proposal) as mock_propose,
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock) as mock_update,
    ):
        result = await execute_create_task(
            {"title": "Finish slides", "duration_mins": 45},
            session,
            user,
        )

    mock_propose.assert_awaited_once()
    call_kwargs = mock_propose.await_args.kwargs
    assert call_kwargs["title"] == "Finish slides"
    assert call_kwargs["duration_mins"] == 45

    mock_update.assert_awaited_once()
    update_kwargs = mock_update.await_args.kwargs
    assert "pending_confirmation" in update_kwargs

    assert result["action"] == "add_task"
    assert result["title"] == "Finish slides"
    assert result["status"] == "proposal_pending_confirmation"


async def test_execute_create_task_does_not_commit(mock_proposal) -> None:
    """execute_create_task must not call session.commit — the caller owns the transaction."""
    session = _make_session()
    user = _make_user()

    with (
        patch("app.agent.tools.task_tools.schedule_service.propose_slot", new_callable=AsyncMock, return_value=mock_proposal),
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock),
    ):
        await execute_create_task(
            {"title": "Finish slides", "duration_mins": 45},
            session,
            user,
        )

    session.commit.assert_not_awaited()


async def test_execute_create_task_parses_deadline(mock_proposal) -> None:
    """execute_create_task forwards a valid deadline_at to propose_slot."""
    session = _make_session()
    user = _make_user()

    with (
        patch("app.agent.tools.task_tools.schedule_service.propose_slot", new_callable=AsyncMock, return_value=mock_proposal) as mock_propose,
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock),
    ):
        await execute_create_task(
            {
                "title": "Finish slides",
                "duration_mins": 45,
                "deadline_at": "2026-05-30T12:00:00",
            },
            session,
            user,
        )

    _, call_kwargs = mock_propose.await_args
    assert call_kwargs["deadline_at"] is not None


# ---------------------------------------------------------------------------
# execute_complete_task
# ---------------------------------------------------------------------------


async def test_execute_complete_task_calls_service() -> None:
    """execute_complete_task delegates to task_service.complete_task."""
    session = _make_session()
    user = _make_user()
    task_id = str(uuid.uuid4())
    expected_result = {"title": "Write report", "status": "completed", "debt_reduced_hours": 0.0}

    with patch(
        "app.agent.tools.task_tools.task_service.complete_task",
        new_callable=AsyncMock,
        return_value=expected_result,
    ) as mock_complete:
        result = await execute_complete_task({"task_id": task_id}, session, user)

    mock_complete.assert_awaited_once_with(session, task_id, user)
    assert result == expected_result


async def test_execute_complete_task_resolves_title() -> None:
    """execute_complete_task fuzzy-matches by title when no task_id is provided."""
    session = _make_session()
    user = _make_user()
    task = _make_task("Write report")
    expected_result = {"title": "Write report", "status": "completed", "debt_reduced_hours": 0.0}

    with (
        patch(
            "app.agent.tools.task_tools.task_service.find_task_by_name",
            new_callable=AsyncMock,
            return_value=task,
        ) as mock_find,
        patch(
            "app.agent.tools.task_tools.task_service.complete_task",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_complete,
    ):
        result = await execute_complete_task({"title": "report"}, session, user)

    mock_find.assert_awaited_once_with(session, user.id, "report")
    mock_complete.assert_awaited_once_with(session, str(task.id), user)
    assert result == expected_result


async def test_execute_complete_task_missing_inputs_returns_error() -> None:
    """execute_complete_task returns a clean error when neither id nor title is given."""
    session = _make_session()
    user = _make_user()

    result = await execute_complete_task({}, session, user)

    assert result == {"error": "provide task_id or title"}


async def test_execute_create_task_defaults_duration(mock_proposal) -> None:
    """execute_create_task uses 60-minute default when duration_mins is missing."""
    session = _make_session()
    user = _make_user()

    with (
        patch(
            "app.agent.tools.task_tools.schedule_service.propose_slot",
            new_callable=AsyncMock,
            return_value=mock_proposal,
        ) as mock_propose,
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock),
    ):
        result = await execute_create_task({"title": "LeetCode"}, session, user)

    assert mock_propose.await_args.kwargs["duration_mins"] == 60
    assert result["status"] == "proposal_pending_confirmation"


async def test_execute_create_task_missing_title_returns_error() -> None:
    """execute_create_task returns a clean error when no title is given."""
    session = _make_session()
    user = _make_user()

    result = await execute_create_task({}, session, user)

    assert result == {"error": "title is required to create a task"}


async def test_execute_create_task_string_recurrence_falls_through(mock_proposal) -> None:
    """A legacy string recurrence is ignored and falls through to the single-task path."""
    session = _make_session()
    user = _make_user()

    with (
        patch(
            "app.agent.tools.task_tools.schedule_service.propose_slot",
            new_callable=AsyncMock,
            return_value=mock_proposal,
        ) as mock_propose,
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock),
    ):
        await execute_create_task(
            {"title": "LeetCode", "duration_mins": 60, "recurrence": "weekdays"},
            session,
            user,
        )

    # String recurrence is no longer a valid batch spec — title stays unchanged
    assert mock_propose.await_args.kwargs["title"] == "LeetCode"


async def test_execute_create_task_batch_recurrence(mock_proposal) -> None:
    """A dict recurrence with 'days' triggers the batch path and returns batch status."""
    from unittest.mock import MagicMock
    from datetime import datetime, timezone, timedelta
    from app.services.schedule_service import BatchScheduleProposal, ScheduleProposal

    session = _make_session()
    user = _make_user()

    fake_batch = BatchScheduleProposal(
        sessions=[
            ScheduleProposal(
                action="add_task",
                title="LeetCode",
                duration_mins=90,
                proposed_start=datetime(2026, 5, 26, 17, 30, tzinfo=timezone.utc),
                proposed_end=datetime(2026, 5, 26, 19, 0, tzinfo=timezone.utc),
            )
        ],
        title="LeetCode",
        duration_mins=90,
        days_label="Mon–Fri",
        time_label="5:30 PM",
        weeks=1,
        total_sessions=1,
    )

    with (
        patch(
            "app.agent.tools.task_tools.schedule_service.propose_batch_slots",
            return_value=fake_batch,
        ),
        patch("app.agent.tools.task_tools.user_crud.update", new_callable=AsyncMock),
    ):
        result = await execute_create_task(
            {
                "title": "LeetCode",
                "duration_mins": 90,
                "recurrence": {"days": ["monday", "tuesday", "wednesday", "thursday", "friday"], "weeks": 1, "time": "17:30"},
            },
            session,
            user,
        )

    assert result["status"] == "batch_proposal_pending_confirmation"
    assert result["action"] == "batch_add"
    assert result["total_sessions"] == 1
    assert result["days_label"] == "Mon–Fri"


# ---------------------------------------------------------------------------
# execute_get_tasks
# ---------------------------------------------------------------------------


async def test_execute_get_tasks_returns_list() -> None:
    """execute_get_tasks returns tasks serialised as dicts."""
    session = _make_session()
    user = _make_user()
    task = _make_task("Write report")

    with patch(
        "app.agent.tools.task_tools.task_service.get_user_tasks",
        new_callable=AsyncMock,
        return_value=[task],
    ) as mock_get:
        result = await execute_get_tasks(session, user)

    mock_get.assert_awaited_once_with(session, user.id)
    assert "tasks" in result
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["title"] == "Write report"
    assert "id" in result["tasks"][0]


async def test_execute_get_tasks_empty_list() -> None:
    """execute_get_tasks returns an empty tasks list when there are no active tasks."""
    session = _make_session()
    user = _make_user()

    with patch(
        "app.agent.tools.task_tools.task_service.get_user_tasks",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await execute_get_tasks(session, user)

    assert result == {"tasks": []}


# ---------------------------------------------------------------------------
# execute_get_time_debt
# ---------------------------------------------------------------------------


async def test_execute_get_time_debt_computes_percentage() -> None:
    """execute_get_time_debt computes percentage from total_hours / max_debt_limit."""
    session = _make_session()
    user = _make_user(max_debt_limit=8.0)

    with patch(
        "app.agent.tools.task_tools.debt_service.get_total_debt",
        new_callable=AsyncMock,
        return_value=4.0,
    ) as mock_debt:
        result = await execute_get_time_debt(session, user)

    mock_debt.assert_awaited_once_with(session, user.id)
    assert result["total_hours"] == 4.0
    assert result["max_debt_limit"] == 8.0
    assert result["percentage"] == 50.0


async def test_execute_get_time_debt_zero_limit() -> None:
    """When max_debt_limit is 0, percentage is reported as 0 to avoid division by zero."""
    session = _make_session()
    user = _make_user(max_debt_limit=0.0)

    with patch(
        "app.agent.tools.task_tools.debt_service.get_total_debt",
        new_callable=AsyncMock,
        return_value=2.0,
    ):
        result = await execute_get_time_debt(session, user)

    assert result["percentage"] == 0.0


# ---------------------------------------------------------------------------
# execute_check_calendar
# ---------------------------------------------------------------------------


async def test_execute_check_calendar_no_gcal() -> None:
    """When GCal is not configured, returns calendar_not_configured status."""
    with patch("app.agent.tools.task_tools._get_gcal_client", return_value=None):
        result = await execute_check_calendar({"date": "2026-05-24"})

    assert result["status"] == "calendar_not_configured"
    assert result["events"] == []


async def test_execute_check_calendar_invalid_date() -> None:
    """Returns an error dict for unparseable date strings."""
    result = await execute_check_calendar({"date": "not-a-date"})
    assert "error" in result


async def test_execute_check_calendar_returns_events() -> None:
    """When GCal returns events, they are serialised as dicts."""
    mock_event = MagicMock()
    mock_event.title = "Team standup"
    mock_event.start = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    mock_event.end = datetime(2026, 5, 24, 9, 30, tzinfo=timezone.utc)
    mock_event.description = ""

    mock_gcal = AsyncMock()
    mock_gcal.get_events = AsyncMock(return_value=[mock_event])

    with patch("app.agent.tools.task_tools._get_gcal_client", return_value=mock_gcal):
        result = await execute_check_calendar({"date": "2026-05-24"})

    assert result["date"] == "2026-05-24"
    assert len(result["events"]) == 1
    assert result["events"][0]["title"] == "Team standup"


# ---------------------------------------------------------------------------
# execute_tool dispatcher
# ---------------------------------------------------------------------------


async def test_execute_tool_dispatches_correctly() -> None:
    """execute_tool routes each known name to the correct executor."""
    session = _make_session()
    user = _make_user()

    with (
        patch("app.agent.tools.task_tools.execute_create_task", new_callable=AsyncMock, return_value={"ok": True}) as m_create,
        patch("app.agent.tools.task_tools.execute_complete_task", new_callable=AsyncMock, return_value={"ok": True}) as m_complete,
        patch("app.agent.tools.task_tools.execute_get_tasks", new_callable=AsyncMock, return_value={"tasks": []}) as m_get,
        patch("app.agent.tools.task_tools.execute_get_time_debt", new_callable=AsyncMock, return_value={}) as m_debt,
        patch("app.agent.tools.task_tools.execute_check_calendar", new_callable=AsyncMock, return_value={}) as m_cal,
    ):
        await execute_tool("create_task", {"title": "x", "duration_mins": 30}, session, user)
        await execute_tool("complete_task", {"task_id": str(uuid.uuid4())}, session, user)
        await execute_tool("get_tasks", {}, session, user)
        await execute_tool("get_time_debt", {}, session, user)
        await execute_tool("check_calendar", {"date": "2026-05-24"}, session, user)

    m_create.assert_awaited_once()
    m_complete.assert_awaited_once()
    m_get.assert_awaited_once()
    m_debt.assert_awaited_once()
    m_cal.assert_awaited_once()


async def test_execute_delete_all_tasks_requires_confirm() -> None:
    """Bulk delete refuses to run unless confirm=True."""
    session = _make_session()
    user = _make_user()
    task = _make_task()

    with patch(
        "app.agent.tools.task_tools.task_service.get_user_tasks",
        new_callable=AsyncMock,
        return_value=[task, task],
    ):
        result = await execute_delete_all_tasks({"confirm": False}, session, user)

    assert "error" in result
    assert result["task_count"] == 2


async def test_execute_delete_all_tasks_deletes_everything() -> None:
    """Bulk delete removes all tasks and returns deleted_count."""
    session = _make_session()
    user = _make_user()
    expected = {"deleted_count": 60, "gcal_deleted_count": 55, "status": "all_tasks_deleted"}

    with patch(
        "app.agent.tools.task_tools.task_service.delete_all_tasks",
        new_callable=AsyncMock,
        return_value=expected,
    ) as mock_delete:
        result = await execute_delete_all_tasks({"confirm": True}, session, user)

    mock_delete.assert_awaited_once_with(session, user)
    assert result["deleted_count"] == 60
    assert result["status"] == "all_tasks_deleted"


async def test_execute_tool_unknown_name() -> None:
    """execute_tool returns an error dict for unknown tool names and does not raise."""
    session = _make_session()
    user = _make_user()
    result = await execute_tool("totally_unknown", {}, session, user)
    assert "error" in result
    assert "totally_unknown" in result["error"]


# ---------------------------------------------------------------------------
# execute_analyze_schedule_for_goal
# ---------------------------------------------------------------------------


async def test_execute_analyze_schedule_for_goal_computes_math() -> None:
    """Core math: sessions_needed = ceil(units / units_per_session) is computed correctly."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from unittest.mock import AsyncMock, MagicMock, patch

    session = _make_session()
    user = _make_user()
    user.onboarding_data = {
        "work_preference": "evening",
        "fixed_commitments": "Jiu Jitsu Monday, Wednesday, Friday evenings",
    }

    # Mock DB queries to return empty (no fixed tasks, no completed tasks)
    async_result = MagicMock()
    async_result.scalars.return_value.all.return_value = []
    async_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=async_result)

    result = await execute_analyze_schedule_for_goal(
        {
            "goal_description": "NeetCode 150",
            "deadline": "August 14, 2026",
            "total_units": 150,
            "unit_name": "problems",
            "units_per_session": 2.0,
            "session_duration_mins": 90,
        },
        session,
        user,
    )

    assert result["total_sessions_needed"] == 75  # ceil(150/2)
    assert result["work_preference"] == "evening"
    assert result["recommended_session_duration_mins"] == 90
    assert result["blocked_weekdays"] == []
    assert "reasoning" in result
    assert result["weeks_remaining"] is not None


async def test_execute_analyze_schedule_for_goal_blocked_days() -> None:
    """Fixed tasks contribute their weekday to blocked_weekdays."""
    from unittest.mock import AsyncMock, MagicMock

    session = _make_session()
    user = _make_user()
    user.onboarding_data = {"work_preference": "evening", "fixed_commitments": ""}

    # First call → fixed tasks (Monday = weekday 0)
    monday_task = _make_task("Jiu Jitsu")
    monday_task.is_fixed = True
    monday_task.scheduled_at = datetime(2026, 5, 25, 18, 0, tzinfo=timezone.utc)  # Monday

    call_count = 0

    async def _fake_execute(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        r = MagicMock()
        if call_count == 1:
            r.scalars.return_value.all.return_value = [monday_task]
        else:
            r.scalar_one_or_none.return_value = None
        return r

    session.execute = _fake_execute

    result = await execute_analyze_schedule_for_goal(
        {
            "goal_description": "NeetCode 150",
            "deadline": "August 14, 2026",
            "total_units": 30,
            "unit_name": "problems",
        },
        session,
        user,
    )

    assert "monday" in result["blocked_weekdays"]


async def test_execute_analyze_schedule_for_goal_unparseable_deadline() -> None:
    """When the deadline string can't be parsed, weeks_remaining is None."""
    from unittest.mock import AsyncMock, MagicMock

    session = _make_session()
    user = _make_user()
    user.onboarding_data = {}

    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    r.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=r)

    result = await execute_analyze_schedule_for_goal(
        {
            "goal_description": "Some goal",
            "deadline": "sometime next year",
            "total_units": 50,
            "unit_name": "tasks",
        },
        session,
        user,
    )

    assert result["weeks_remaining"] is None
    assert result["total_sessions_needed"] == 25  # ceil(50/2)
