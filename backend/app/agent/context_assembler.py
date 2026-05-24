"""Builds LLM prompt context from all data sources (pure DB reads, no LLM calls)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass
class TaskSummary:
    id: UUID
    title: str
    duration_mins: int
    deadline_at: datetime | None
    status: str
    is_fixed: bool
    requires_proof: bool


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


async def assemble_context(session: AsyncSession, user_id: UUID) -> AgentContext:
    """Assemble all agent context for a user from the database.

    Executes six targeted queries and returns a fully populated AgentContext.
    No LLM calls are made here.
    """
    # 1. User row (timezone + debt limit)
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    user_timezone = user.timezone if user else "UTC"
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
        .order_by(Message.created_at.desc())
        .limit(10)
    )
    recent_messages = [
        MessageSummary(role=m.role, content=m.content, created_at=m.created_at)
        for m in reversed(messages_result.scalars().all())
    ]

    return AgentContext(
        north_star=north_star,
        active_tasks=active_tasks,
        time_debt=time_debt,
        insights=insights,
        recent_messages=recent_messages,
        current_time=datetime.now(timezone.utc),
        user_timezone=user_timezone,
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
