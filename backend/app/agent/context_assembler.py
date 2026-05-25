"""Builds LLM prompt context from all data sources (pure DB reads, no LLM calls)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import UUID

from zoneinfo import ZoneInfoNotFoundError, ZoneInfo
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.insight import UserProfileInsight
from app.db.models.message import Message
from app.db.models.task import Task, TaskStatus
from app.db.models.time_debt import TimeDebtLedger
from app.db.models.user import User
from app.memory.north_star import NorthStar, read_goals
from app.services.scheduling_prefs import effective_timezone_name, user_timezone

# Month abbreviations used in natural-language deadline strings like "August 14, 2026"
_MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _parse_deadline_string(raw: str | None) -> date | None:
    """Best-effort parse of a deadline string into a date.

    Tries, in order:
    1. ISO format (YYYY-MM-DD)
    2. Natural-language "Month Day, Year" or "Month Day Year"
    3. Returns None if unparseable.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # ISO
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    # Natural language: "August 14, 2026" or "August 14 2026"
    m = re.search(
        r"\b([a-zA-Z]+)\s+(\d{1,2})[,\s]+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year = int(m.group(3))
        month = _MONTH_NAMES.get(month_name)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                pass
    # "Month Day" with no year — assume current or next year
    m2 = re.search(r"\b([a-zA-Z]+)\s+(\d{1,2})\b", text, re.IGNORECASE)
    if m2:
        month_name = m2.group(1).lower()
        day = int(m2.group(2))
        month = _MONTH_NAMES.get(month_name)
        if month:
            today = date.today()
            year = today.year
            try:
                candidate = date(year, month, day)
                if candidate < today:
                    candidate = date(year + 1, month, day)
                return candidate
            except ValueError:
                pass
    return None


@dataclass
class TaskSummary:
    id: UUID
    title: str
    duration_mins: int
    deadline_at: datetime | None
    status: str
    is_fixed: bool
    requires_proof: bool
    scheduled_at: datetime | None = None


@dataclass
class TimeDebtSummary:
    total_hours: float
    max_debt_limit: float
    percentage: float


@dataclass
class InsightSummary:
    category: str
    insight: str
    strength: int


@dataclass
class MessageSummary:
    role: str
    content: str
    created_at: datetime


@dataclass
class AgentContext:
    north_star: NorthStar
    active_tasks: list[TaskSummary] = field(default_factory=list)
    time_debt: TimeDebtSummary = field(
        default_factory=lambda: TimeDebtSummary(
            total_hours=0.0, max_debt_limit=0.0, percentage=0.0
        )
    )
    insights: list[InsightSummary] = field(default_factory=list)
    recent_messages: list[MessageSummary] = field(default_factory=list)
    current_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_timezone: str = "UTC"
    pending_confirmation: dict | None = None
    # --- planning context ---
    fixed_commitments_text: str = ""
    weeks_until_deadlines: dict[str, float | None] = field(default_factory=dict)
    suggested_session_length: int | None = None


async def assemble_context(session: AsyncSession, user_id: UUID) -> AgentContext:
    """Assemble all agent context for a user from the database.

    Executes six targeted queries and returns a fully populated AgentContext.
    No LLM calls are made here.
    """
    # 1. User row (timezone + debt limit)
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    user_tz_name = effective_timezone_name(user.timezone if user else None)
    max_debt_limit = user.max_debt_limit if user else 0.0

    # 2. North Star (goals from onboarding data)
    north_star = await read_goals(session, user_id)
    if north_star is None:
        north_star = NorthStar()

    # 3. Active tasks (pending or pushed), ordered by deadline ascending (nulls last)
    tasks_result = await session.execute(
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.status.in_([TaskStatus.PENDING.value, TaskStatus.PUSHED.value]),
        )
        .order_by(Task.deadline_at.asc().nulls_last())
    )
    active_tasks = [
        TaskSummary(
            id=t.id,
            title=t.title,
            duration_mins=t.duration_mins,
            deadline_at=t.deadline_at,
            status=t.status,
            is_fixed=t.is_fixed,
            requires_proof=t.requires_proof,
            scheduled_at=t.scheduled_at,
        )
        for t in tasks_result.scalars().all()
    ]

    # 4. Time debt (sum of ledger entries)
    debt_result = await session.execute(
        select(func.sum(TimeDebtLedger.hours_added)).where(
            TimeDebtLedger.user_id == user_id
        )
    )
    total_hours: float = debt_result.scalar_one_or_none() or 0.0
    percentage = (total_hours / max_debt_limit) if max_debt_limit > 0 else 0.0
    time_debt = TimeDebtSummary(
        total_hours=total_hours,
        max_debt_limit=max_debt_limit,
        percentage=percentage,
    )

    # 5. Behavioral insights (top 5 by strength)
    insights_result = await session.execute(
        select(UserProfileInsight)
        .where(UserProfileInsight.user_id == user_id)
        .order_by(UserProfileInsight.strength.desc())
        .limit(5)
    )
    insights = [
        InsightSummary(
            category=i.category,
            insight=i.insight,
            strength=i.strength,
        )
        for i in insights_result.scalars().all()
    ]

    # 6. Recent messages (last 10, reversed to chronological order)
    messages_result = await session.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(10)
    )
    recent_messages = [
        MessageSummary(role=m.role, content=m.content, created_at=m.created_at)
        for m in reversed(messages_result.scalars().all())
    ]

    # 7. Planning context — no extra DB queries except session length avg
    onboarding_data: dict = (user.onboarding_data or {}) if user else {}
    fixed_commitments_text: str = onboarding_data.get("fixed_commitments", "") or ""

    today = datetime.now(timezone.utc).date()
    weeks_until_deadlines: dict[str, float | None] = {}
    for goal, deadline_str in north_star.deadlines.items():
        deadline_date = _parse_deadline_string(deadline_str)
        if deadline_date is not None:
            weeks_until_deadlines[goal] = round((deadline_date - today).days / 7, 1)
        else:
            weeks_until_deadlines[goal] = None

    session_length_result = await session.execute(
        select(func.avg(Task.duration_mins)).where(
            Task.user_id == user_id,
            Task.status == TaskStatus.COMPLETED.value,
        )
    )
    avg_duration = session_length_result.scalar_one_or_none()
    suggested_session_length: int | None = int(round(avg_duration)) if avg_duration else None

    # Build current_time in the user's local timezone so the agent reasons about
    # "now" in the user's frame of reference rather than UTC.
    local_tz = user_timezone(user_tz_name)
    local_now = datetime.now(local_tz)

    return AgentContext(
        north_star=north_star,
        active_tasks=active_tasks,
        time_debt=time_debt,
        insights=insights,
        recent_messages=recent_messages,
        current_time=local_now,
        user_timezone=user_tz_name,
        pending_confirmation=user.pending_confirmation if user else None,
        fixed_commitments_text=fixed_commitments_text,
        weeks_until_deadlines=weeks_until_deadlines,
        suggested_session_length=suggested_session_length,
    )


def to_prompt_string(context: AgentContext) -> str:
    """Format an AgentContext into a prompt-injectable string."""
    sections: list[str] = []

    # --- NORTH STAR ---
    ns = context.north_star
    ns_lines: list[str] = []
    if ns.goals:
        for goal in ns.goals:
            deadline = ns.deadlines.get(goal)
            suffix = f" (by {deadline})" if deadline else ""
            ns_lines.append(f"- {goal}{suffix}")
    else:
        ns_lines.append("(no goals set)")
    if ns.preferences:
        for key, value in ns.preferences.items():
            ns_lines.append(f"  {key}: {value}")
    sections.append("=== NORTH STAR ===\n" + "\n".join(ns_lines))

    # --- PLANNING CONTEXT ---
    planning_lines: list[str] = []
    if context.weeks_until_deadlines:
        for goal, weeks in context.weeks_until_deadlines.items():
            if weeks is not None:
                planning_lines.append(f"Weeks until deadline ({goal[:60]}): {weeks}")
            else:
                planning_lines.append(f"Weeks until deadline ({goal[:60]}): (unparseable)")
    if context.fixed_commitments_text:
        planning_lines.append(
            f'Fixed commitments (from onboarding): "{context.fixed_commitments_text}"'
        )
    fixed_tasks = [t for t in context.active_tasks if t.is_fixed]
    if fixed_tasks:
        fixed_lines = []
        for t in fixed_tasks:
            sched = t.scheduled_at or t.deadline_at
            scheduled_str = sched.strftime("%a %H:%M") if sched else "unscheduled"
            fixed_lines.append(f"  - {t.title} ({t.duration_mins}min, {scheduled_str}) [fixed]")
        planning_lines.append("Fixed tasks in schedule:\n" + "\n".join(fixed_lines))
    if context.suggested_session_length is not None:
        planning_lines.append(
            f"Avg completed session length: {context.suggested_session_length} min"
        )
    if planning_lines:
        sections.append("=== PLANNING CONTEXT ===\n" + "\n".join(planning_lines))

    # --- ACTIVE TASKS ---
    if context.active_tasks:
        task_lines = []
        for t in context.active_tasks:
            deadline_str = (
                t.deadline_at.strftime("%Y-%m-%d %H:%M UTC")
                if t.deadline_at
                else "no deadline"
            )
            flags = []
            if t.is_fixed:
                flags.append("fixed")
            if t.requires_proof:
                flags.append("proof required")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            task_lines.append(
                f"- [{t.status}] {t.title} ({t.duration_mins}min, due {deadline_str}){flag_str}"
            )
        sections.append("=== ACTIVE TASKS ===\n" + "\n".join(task_lines))
    else:
        sections.append("=== ACTIVE TASKS ===\n(none)")

    # --- PENDING CONFIRMATION ---
    if context.pending_confirmation:
        action = context.pending_confirmation.get("action", "unknown")
        title = context.pending_confirmation.get("title", "a task")
        proposed_start = context.pending_confirmation.get("proposed_start", "")
        if action == "reschedule":
            summary = f"Reschedule '{title}' to {proposed_start}"
        elif action == "add_task":
            summary = f"Add '{title}' at {proposed_start}"
        elif action == "delete_task":
            summary = f"Delete '{title}'"
        else:
            summary = f"Pending action on '{title}'"
        sections.append(
            "=== PENDING CONFIRMATION ===\n"
            f"There is a proposal waiting for the user's yes/no: {summary}.\n"
            "Do not re-propose this. If the user seems to be responding to it, "
            "remind them to reply yes or no."
        )

    # --- TIME DEBT ---
    td = context.time_debt
    pct = round(td.percentage * 100, 1)
    sections.append(
        f"=== TIME DEBT ===\n{td.total_hours:.1f}h of {td.max_debt_limit:.1f}h limit ({pct}%)"
    )

    # --- BEHAVIORAL INSIGHTS ---
    if context.insights:
        insight_lines = [
            f"- [{i.category}] {i.insight} (strength: {i.strength})"
            for i in context.insights
        ]
        sections.append("=== BEHAVIORAL INSIGHTS ===\n" + "\n".join(insight_lines))
    else:
        sections.append("=== BEHAVIORAL INSIGHTS ===\n(none)")

    # --- RECENT CONVERSATION ---
    if context.recent_messages:
        msg_lines = [f"{m.role}: {m.content}" for m in context.recent_messages]
        sections.append("=== RECENT CONVERSATION ===\n" + "\n".join(msg_lines))
    else:
        sections.append("=== RECENT CONVERSATION ===\n(no history)")

    # --- CURRENT TIME ---
    try:
        tz = ZoneInfo(context.user_timezone)
        local_time = context.current_time.astimezone(tz)
        time_str = local_time.strftime("%Y-%m-%d %H:%M %Z")
    except (ZoneInfoNotFoundError, Exception):
        time_str = context.current_time.strftime("%Y-%m-%d %H:%M UTC")
    sections.append(f"=== CURRENT TIME ===\n{time_str}")

    return "\n\n".join(sections)
