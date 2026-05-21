"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base
from app.db.crud import insight_crud, ledger_crud, task_crud, user_crud
from app.db.models.task import TaskStatus
from app.db.models.user import User
from app.main import app


@pytest.fixture
async def engine():
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture
async def user(session: AsyncSession) -> User:
    return await user_crud.create(
        session,
        telegram_chat_id=123456789,
        timezone="America/New_York",
        max_debt_limit=8.0,
    )


@pytest.fixture
async def task(session: AsyncSession, user: User):
    return await task_crud.create(
        session,
        user_id=user.id,
        title="Write report",
        duration_mins=60,
        status=TaskStatus.PENDING.value,
    )


@pytest.fixture
async def ledger_entry(session: AsyncSession, user: User, task):
    return await ledger_crud.create(
        session,
        user_id=user.id,
        task_id=task.id,
        hours_added=1.5,
        reason="Task pushed",
    )


@pytest.fixture
async def insight(session: AsyncSession, user: User):
    return await insight_crud.create(
        session,
        user_id=user.id,
        category="focus",
        insight="Works best in morning blocks",
        strength=8,
    )


@pytest.fixture
async def http_client() -> AsyncIterator[AsyncClient]:
    """AsyncClient wired to the FastAPI app under test."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def mock_send_message():
    """Patch telegram_client.send_message with an AsyncMock."""
    with patch(
        "app.telegram.client.telegram_client.send_message",
        new_callable=AsyncMock,
    ) as mock:
        yield mock
