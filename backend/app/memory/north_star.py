"""Layer 1 goal context via user_service."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


@dataclass
class NorthStar:
    goals: list[str] = field(default_factory=list)
    deadlines: dict[str, str] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)

    def format_for_prompt(self) -> str:
        """Return a human-readable summary suitable for prompt injection."""
        lines: list[str] = ["## User Goals (North Star)"]

        if self.goals:
            lines.append("Goals:")
            for goal in self.goals:
                deadline = self.deadlines.get(goal)
                suffix = f" (by {deadline})" if deadline else ""
                lines.append(f"  - {goal}{suffix}")
        else:
            lines.append("Goals: (none set)")

        if self.preferences:
            lines.append("Preferences:")
            for key, value in self.preferences.items():
                lines.append(f"  - {key}: {value}")

        return "\n".join(lines)


async def read_goals(session: AsyncSession, user_id: UUID) -> NorthStar | None:
    """Return the NorthStar for user_id, or None if onboarding is not complete.

    Reads onboarding_data from the users table.
    """
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or user.onboarding_status != "complete":
        return None

    data: dict = user.onboarding_data or {}

    refined_goal: str = data.get("goal_refined") or data.get("goal_raw", "")
    goals = [refined_goal] if refined_goal else []

    deadlines: dict[str, str] = {}
    target_date = data.get("goal_target_date")
    if refined_goal and target_date:
        deadlines[refined_goal] = target_date

    preferences: dict[str, str] = {}
    for key in ("work_preference", "accountability_style", "timezone"):
        value = data.get(key) or (user.timezone if key == "timezone" else None)
        if value:
            preferences[key] = value

    return NorthStar(goals=goals, deadlines=deadlines, preferences=preferences)
