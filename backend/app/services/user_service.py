"""User management; goals and preferences for north_star."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


async def get_or_create_user(
    session: AsyncSession, chat_id: int
) -> tuple[User, bool]:
    """Return the User for chat_id, creating one if it does not exist yet.

    Returns:
        (user, created) where created is True when a new row was inserted.
    """
    result = await session.execute(
        select(User).where(User.telegram_chat_id == chat_id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user, False

    user = User(telegram_chat_id=chat_id, onboarding_status="pending")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user, True
