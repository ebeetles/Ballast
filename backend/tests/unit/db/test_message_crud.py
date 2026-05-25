"""Tests for Message model defaults — microsecond ORM-side timestamps."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import message_crud
from app.db.models.message import Message
from app.db.models.user import User


@pytest.mark.asyncio
async def test_back_to_back_messages_have_distinct_timestamps(
    session: AsyncSession, user: User
) -> None:
    """ORM-side `default` gives messages saved milliseconds apart unique timestamps."""
    await message_crud.create(session, user_id=user.id, role="user", content="first")
    await message_crud.create(session, user_id=user.id, role="assistant", content="second")
    await message_crud.create(session, user_id=user.id, role="user", content="third")

    result = await session.execute(
        select(Message).where(Message.user_id == user.id).order_by(Message.created_at.asc())
    )
    rows = list(result.scalars().all())
    timestamps = [m.created_at for m in rows]
    contents = [m.content for m in rows]

    assert contents == ["first", "second", "third"]
    assert len(set(timestamps)) == 3, f"expected unique timestamps, got {timestamps}"
