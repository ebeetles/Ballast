"""Task and calendar tools invoked by the cognitive loop via Anthropic tool-use."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context_assembler import _parse_deadline_string
from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.task import Task, TaskStatus
from app.db.models.user import User
from app.services import debt_service, schedule_service, task_service

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool schema definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "create_task",
        "description": (
            "Create a task. Infer title, duration, day, and time_of_day from the "
            "conversation; never ask the user to fill these in. Returns a scheduling "
            "proposal — does NOT commit immediately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Name of the task (infer from conversation)",
                },
                "duration_mins": {
                    "type": "integer",
                    "description": "Length in minutes. Default 60 if unclear.",
                },
                "deadline_at": {
                    "type": "string",
                    "description": "ISO 8601 datetime string for the deadline (optional)",
                },
                "requires_proof": {
                    "type": "boolean",
                    "description": "Whether the user must submit proof of completion",
                },
                "day": {
                    "type": "string",
                    "description": "Preferred day, e.g. tomorrow, friday, tonight",
                },
                "time_of_day": {
                    "type": "string",
                    "description": (
                        "Preferred time: morning, afternoon, evening, night, "
                        "or explicit local time like '14:00' or '2 PM'"
                    ),
                },
                "recurrence": {
                    "type": "object",
                    "description": (
                        "Provide this whenever the user wants the same task on "
                        "multiple days or across multiple weeks. Never create "
                        "recurring tasks one at a time."
                    ),
                    "properties": {
                        "days": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Days of the week, e.g. ['monday','tuesday',"
                                "'wednesday','thursday','friday']"
                            ),
                        },
                        "weeks": {
                            "type": "integer",
                            "description": "Number of weeks to schedule. Default 4.",
                        },
                        "time": {
                            "type": "string",
                            "description": (
                                "Session time as HH:MM (e.g. '17:30') or "
                                "time-of-day name (e.g. 'evening'). "
                                "Infer from user preferences or conversation."
                            ),
                        },
                    },
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "delete_task",
        "description": (
            "Remove/cancel an active task and delete its calendar event. "
            "Use when the user wants to delete, cancel, or remove a task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "UUID of the task to remove",
                },
                "title": {
                    "type": "string",
                    "description": "Task name if UUID is unknown (fuzzy match)",
                },
            },
        },
    },
    {
        "name": "delete_all_tasks",
        "description": (
            "Delete ALL of the user's tasks and their associated Google Calendar "
            "events in one go. Only use when the user explicitly asks to wipe "
            "everything or start fresh."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": (
                        "Must be true to execute. Set only after the user has "
                        "confirmed they want to delete everything."
                    ),
                },
            },
            "required": ["confirm"],
        },
    },
    {
        "name": "complete_task",
        "description": (
            "Mark a task as complete. Only call when the user explicitly says "
            "something is done. Provide task_id when known, or title for fuzzy match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "UUID of the task to complete (preferred when known)",
                },
                "title": {
                    "type": "string",
                    "description": "Task title for fuzzy match when task_id is unavailable",
                },
            },
        },
    },
    {
        "name": "get_tasks",
        "description": "Get the user's current active tasks (pending and pushed).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_time_debt",
        "description": "Get the user's current time debt status.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_calendar",
        "description": "Check what's on the user's calendar for a given date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to check in YYYY-MM-DD format",
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "analyze_schedule_for_goal",
        "description": (
            "Compute how many sessions are needed to hit a goal by its deadline, "
            "and identify the user's blocked days and preferred times. "
            "Call this BEFORE proposing any multi-session recurring plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_description": {
                    "type": "string",
                    "description": "The goal being planned, e.g. 'NeetCode 150'",
                },
                "deadline": {
                    "type": "string",
                    "description": "Deadline string from context, e.g. 'August 14, 2026'",
                },
                "total_units": {
                    "type": "integer",
                    "description": "Total countable units to complete, e.g. 150 for NeetCode 150",
                },
                "unit_name": {
                    "type": "string",
                    "description": "Name for one unit, e.g. 'problems', 'chapters', 'sessions'",
                },
                "units_per_session": {
                    "type": "number",
                    "description": (
                        "Estimated units completable per session. "
                        "Infer from context or default to 2."
                    ),
                },
                "session_duration_mins": {
                    "type": "integer",
                    "description": (
                        "Planned session length in minutes. "
                        "Use user history from context, or default to 90."
                    ),
                },
            },
            "required": ["goal_description", "total_units", "unit_name"],
        },
    },
]

# ---------------------------------------------------------------------------
# GCal helper (mirrors pattern from schedule_service and complete_task handler)
# ---------------------------------------------------------------------------


def _get_gcal_client():
    if not settings.google_calendar_credentials_file:
        return None
    try:
        from app.calendar.gcal_client import GoogleCalendarClient
        return GoogleCalendarClient()
    except Exception:
        logger.exception("gcal_client_init_failed")
        return None


# ---------------------------------------------------------------------------
# Individual executor functions
# ---------------------------------------------------------------------------


async def execute_create_task(
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Propose a slot for a new task and store the proposal in pending_confirmation.

    Does NOT commit the task to the DB — returns the proposal so the LLM can
    present it and the confirmation handler can commit on user approval.
    """
    raw_title = inputs.get("title")
    title = (raw_title or "").strip() if isinstance(raw_title, str) else ""
    if not title:
        return {"error": "title is required to create a task"}

    try:
        duration_mins = int(inputs.get("duration_mins") or 60)
    except (TypeError, ValueError):
        duration_mins = 60
    duration_mins = max(1, duration_mins)

    requires_proof: bool = bool(inputs.get("requires_proof", False))

    deadline_at: datetime | None = None
    raw_deadline = inputs.get("deadline_at")
    if raw_deadline:
        try:
            deadline_at = datetime.fromisoformat(raw_deadline)
        except ValueError:
            logger.warning("create_task_tool_invalid_deadline value=%r", raw_deadline)

    day = inputs.get("day")
    time_of_day = inputs.get("time_of_day")
    if isinstance(day, str):
        day = day.strip() or None
    if isinstance(time_of_day, str):
        time_of_day = time_of_day.strip() or None

    recurrence = inputs.get("recurrence")

    # --- Batch path: structured recurrence object with days list ---
    if isinstance(recurrence, dict) and recurrence.get("days"):
        try:
            batch = schedule_service.propose_batch_slots(
                title=title,
                duration_mins=duration_mins,
                recurrence=recurrence,
                user=user,
                deadline_at=deadline_at,
                requires_proof=requires_proof,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        await user_crud.update(
            session,
            user,
            pending_confirmation=batch.model_dump(mode="json"),
        )

        return {
            "status": "batch_proposal_pending_confirmation",
            "action": "batch_add",
            "title": batch.title,
            "days_label": batch.days_label,
            "time_label": batch.time_label,
            "weeks": batch.weeks,
            "total_sessions": batch.total_sessions,
            "duration_mins": batch.duration_mins,
            "sessions": [s.model_dump(mode="json") for s in batch.sessions],
        }

    proposal = await schedule_service.propose_slot(
        title=title,
        duration_mins=duration_mins,
        user=user,
        deadline_at=deadline_at,
        requires_proof=requires_proof,
        day=day,
        time_of_day=time_of_day,
    )

    await user_crud.update(
        session,
        user,
        pending_confirmation=proposal.model_dump(mode="json"),
    )

    return {
        "action": "add_task",
        "title": proposal.title,
        "duration_mins": proposal.duration_mins,
        "proposed_start": proposal.proposed_start.isoformat(),
        "proposed_end": proposal.proposed_end.isoformat(),
        "debt_delta": 0.0,
        "deadline_at": proposal.deadline_at.isoformat() if proposal.deadline_at else None,
        "requires_proof": proposal.requires_proof,
        "status": "proposal_pending_confirmation",
    }


async def execute_delete_task(
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Remove an active task by id or fuzzy title match."""
    task_id = inputs.get("task_id")
    title = inputs.get("title")
    if task_id:
        return await task_service.cancel_task(session, user, task_id_str=str(task_id))
    if title:
        return await task_service.cancel_task(session, user, task_name=str(title))
    return {"error": "provide task_id or title"}


async def execute_delete_all_tasks(
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Delete every task and GCal event for the user when confirm=True."""
    if inputs.get("confirm") is not True:
        tasks = await task_service.get_user_tasks(session, user.id)
        return {
            "error": "confirm must be true to execute bulk delete",
            "task_count": len(tasks),
            "message": (
                "Ask the user to confirm first, then call again with confirm=True."
            ),
        }
    return await task_service.delete_all_tasks(session, user)


async def execute_complete_task(
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Complete a task by ID, or by fuzzy title match if no ID is provided."""
    task_id = inputs.get("task_id")
    title = inputs.get("title")

    if isinstance(task_id, str) and task_id.strip():
        return await task_service.complete_task(session, task_id.strip(), user)

    if isinstance(title, str) and title.strip():
        task = await task_service.find_task_by_name(session, user.id, title.strip())
        if task is None:
            return {"error": "task not found"}
        return await task_service.complete_task(session, str(task.id), user)

    return {"error": "provide task_id or title"}


async def execute_get_tasks(
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Return the user's active tasks as serialisable dicts."""
    tasks = await task_service.get_user_tasks(session, user.id)
    return {
        "tasks": [
            {
                "id": str(t.id),
                "title": t.title,
                "duration_mins": t.duration_mins,
                "status": t.status,
                "deadline_at": t.deadline_at.isoformat() if t.deadline_at else None,
                "requires_proof": t.requires_proof,
            }
            for t in tasks
        ]
    }


async def execute_get_time_debt(
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Return the user's current time debt summary."""
    total_hours = await debt_service.get_total_debt(session, user.id)
    max_limit = user.max_debt_limit or 0.0
    percentage = round((total_hours / max_limit) * 100, 1) if max_limit > 0 else 0.0
    return {
        "total_hours": round(total_hours, 2),
        "max_debt_limit": max_limit,
        "percentage": percentage,
    }


async def execute_check_calendar(inputs: dict[str, Any]) -> dict[str, Any]:
    """Return events from GCal for the requested date."""
    date_str: str = inputs["date"]
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return {"error": f"invalid date format: {date_str!r} — expected YYYY-MM-DD"}

    gcal = _get_gcal_client()
    if gcal is None:
        return {"status": "calendar_not_configured", "events": []}

    day_start = datetime.combine(target, time.min).replace(tzinfo=timezone.utc)
    day_end = datetime.combine(target, time.max.replace(microsecond=0)).replace(tzinfo=timezone.utc)

    try:
        events = await gcal.get_events(day_start, day_end)
    except Exception:
        logger.exception("check_calendar_tool_failed date=%s", date_str)
        return {"error": "failed to fetch calendar events"}

    return {
        "date": date_str,
        "events": [
            {
                "title": e.title,
                "start": e.start.isoformat(),
                "end": e.end.isoformat(),
                "description": e.description or "",
            }
            for e in events
        ],
    }


_WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


async def execute_analyze_schedule_for_goal(
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Compute planning math for a goal and surface scheduling constraints.

    Returns sessions needed, sessions/week, blocked weekdays, work preference,
    and suggested session duration so the LLM can propose a concrete schedule.
    """
    goal_description: str = inputs.get("goal_description", "")
    deadline_str: str = inputs.get("deadline", "")
    total_units: int = max(1, int(inputs.get("total_units") or 1))
    unit_name: str = inputs.get("unit_name", "sessions")
    units_per_session: float = float(inputs.get("units_per_session") or 2)
    session_duration_mins: int = int(inputs.get("session_duration_mins") or 90)

    # --- Weeks remaining until deadline ---
    today = datetime.now(timezone.utc).date()
    deadline_date = _parse_deadline_string(deadline_str)
    if deadline_date is not None:
        days_remaining = (deadline_date - today).days
        weeks_remaining: float | None = round(days_remaining / 7, 1)
    else:
        days_remaining = None
        weeks_remaining = None

    # --- Sessions math ---
    total_sessions_needed = math.ceil(total_units / max(0.01, units_per_session))
    if weeks_remaining and weeks_remaining > 0:
        sessions_per_week_needed = math.ceil(total_sessions_needed / weeks_remaining)
    else:
        sessions_per_week_needed = None

    # --- Blocked weekdays from is_fixed active tasks ---
    from sqlalchemy import select as _select
    fixed_result = await session.execute(
        _select(Task).where(
            Task.user_id == user.id,
            Task.is_fixed.is_(True),
            Task.status.in_(["pending", "pushed"]),
        )
    )
    fixed_tasks = list(fixed_result.scalars().all())
    blocked_weekday_nums = sorted({
        t.scheduled_at.weekday()
        for t in fixed_tasks
        if t.scheduled_at is not None
    })
    blocked_weekdays = [_WEEKDAY_NAMES[n] for n in blocked_weekday_nums]

    # --- Average completed session duration (history-based) ---
    avg_result = await session.execute(
        select(func.avg(Task.duration_mins)).where(
            Task.user_id == user.id,
            Task.status == TaskStatus.COMPLETED.value,
        )
    )
    avg_completed = avg_result.scalar_one_or_none()
    if avg_completed and session_duration_mins == 90:
        session_duration_mins = int(round(avg_completed))

    # --- Work preference and fixed commitments text ---
    onboarding_data: dict = user.onboarding_data or {}
    work_preference: str = onboarding_data.get("work_preference", "evening") or "evening"
    fixed_commitments_text: str = onboarding_data.get("fixed_commitments", "") or ""

    # --- Reasoning string ---
    if weeks_remaining is not None and sessions_per_week_needed is not None:
        reasoning = (
            f"At {units_per_session} {unit_name}/session you need "
            f"{total_sessions_needed} sessions over {weeks_remaining} weeks "
            f"— roughly {sessions_per_week_needed}/week."
        )
    elif weeks_remaining is None:
        reasoning = (
            f"Couldn't parse the deadline '{deadline_str}'. "
            f"You still need {total_sessions_needed} sessions at "
            f"{units_per_session} {unit_name}/session."
        )
    else:
        reasoning = f"You need {total_sessions_needed} sessions total."

    return {
        "goal_description": goal_description,
        "weeks_remaining": weeks_remaining,
        "days_remaining": days_remaining,
        "total_sessions_needed": total_sessions_needed,
        "sessions_per_week_needed": sessions_per_week_needed,
        "recommended_session_duration_mins": session_duration_mins,
        "blocked_weekdays": blocked_weekdays,
        "fixed_commitments_text": fixed_commitments_text,
        "work_preference": work_preference,
        "reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


async def execute_tool(
    name: str,
    inputs: dict[str, Any],
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Route a tool call by name to its executor. Returns a JSON-serialisable dict."""
    match name:
        case "create_task":
            return await execute_create_task(inputs, session, user)
        case "delete_task":
            return await execute_delete_task(inputs, session, user)
        case "delete_all_tasks":
            return await execute_delete_all_tasks(inputs, session, user)
        case "complete_task":
            return await execute_complete_task(inputs, session, user)
        case "get_tasks":
            return await execute_get_tasks(session, user)
        case "get_time_debt":
            return await execute_get_time_debt(session, user)
        case "check_calendar":
            return await execute_check_calendar(inputs)
        case "analyze_schedule_for_goal":
            return await execute_analyze_schedule_for_goal(inputs, session, user)
        case _:
            logger.warning("execute_tool_unknown_name name=%r", name)
            return {"error": f"unknown tool: {name}"}
