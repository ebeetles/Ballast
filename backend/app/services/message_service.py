"""Persist and retrieve conversation messages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.message import Message


async def save_message(
    session: AsyncSession,
    user_id: UUID,
    role: str,
    content: str,
) -> Message:
    """Persist a single message (user or assistant) to the messages table."""
    message = Message(user_id=user_id, role=role, content=content)
    session.add(message)
    await session.flush()
    await session.refresh(message)
    return message


async def get_recent_messages(
    session: AsyncSession,
    user_id: UUID,
    limit: int = 10,
) -> list[Message]:
    """Return the most recent messages for a user, oldest-first."""
    result = await session.execute(
        select(Message)
        .where(Message.user_id == user_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    return list(reversed(rows))
