"""Onboarding persistence: goals, preferences, initial tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.crud import task_crud, user_crud
from app.db.models.user import User
from app.services.scheduling_prefs import effective_timezone_name


_DEBT_LIMIT_BY_STYLE: dict[str, float] = {
    "A": 6.0,
    "B": 4.0,
    "C": 2.0,
}


@dataclass
class OnboardingState:
    step: str
    data: dict


def get_onboarding_state(user: User) -> OnboardingState:
    """Return the current onboarding step and accumulated answers for user."""
    return OnboardingState(step=user.onboarding_step, data=dict(user.onboarding_data))


async def save_onboarding_answer(
    session: AsyncSession, user: User, step: str, answer: str
) -> None:
    """Persist a single onboarding answer into users.onboarding_data.

    Uses full dict reassignment so SQLAlchemy detects the change.
    """
    updated_data = {**user.onboarding_data, step: answer}
    await user_crud.update(session, user, onboarding_data=updated_data)


async def complete_onboarding(session: AsyncSession, user: User) -> None:
    """Finalise onboarding: set status, preferences, and create initial task.

    Caller is responsible for committing the session.
    """
    data = user.onboarding_data
    style = data.get("accountability_style", "B").upper()
    max_debt = _DEBT_LIMIT_BY_STYLE.get(style, 4.0)

    deadline_str: Optional[str] = data.get("goal_target_date")
    deadline_at: Optional[datetime] = None
    if deadline_str:
        try:
            deadline_at = datetime.fromisoformat(deadline_str)
        except ValueError:
            deadline_at = None

    tz = settings.default_user_timezone
    if user.timezone and user.timezone not in ("", "UTC"):
        tz = user.timezone

    await user_crud.update(
        session,
        user,
        onboarding_status="complete",
        max_debt_limit=max_debt,
        timezone=tz,
    )

    refined_goal: str = data.get("goal_refined") or data.get("goal_raw", "Primary goal")
    await task_crud.create(
        session,
        user_id=user.id,
        title=refined_goal,
        duration_mins=60,
        is_fixed=False,
        deadline_at=deadline_at,
    )
