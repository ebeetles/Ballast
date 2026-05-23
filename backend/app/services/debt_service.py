"""All time debt mutations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import ledger_crud
from app.db.models.time_debt import TimeDebtLedger


async def add_debt(
    session: AsyncSession,
    user_id: UUID,
    task_id: UUID | None,
    hours: float,
    reason: str,
) -> TimeDebtLedger:
    """Append a positive debt entry to the ledger."""
    return await ledger_crud.create(
        session,
        user_id=user_id,
        task_id=task_id,
        hours_added=hours,
        reason=reason,
    )


async def subtract_debt(
    session: AsyncSession,
    user_id: UUID,
    task_id: UUID | None,
    hours: float,
    reason: str,
) -> TimeDebtLedger:
    """Append a negative debt entry to the ledger (debt reduction)."""
    return await ledger_crud.create(
        session,
        user_id=user_id,
        task_id=task_id,
        hours_added=-abs(hours),
        reason=reason,
    )


async def get_total_debt(session: AsyncSession, user_id: UUID) -> float:
    """Return the sum of all hours_added entries for a user (can be negative)."""
    result = await session.execute(
        select(TimeDebtLedger).where(TimeDebtLedger.user_id == user_id)
    )
    entries = result.scalars().all()
    return sum(e.hours_added for e in entries)
