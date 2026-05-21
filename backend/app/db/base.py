"""Declarative base and generic async CRUD classes."""

from __future__ import annotations

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

ModelT = TypeVar("ModelT", bound="Base")


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


class CRUDBase(Generic[ModelT]):
    """Generic async CRUD operations for a SQLAlchemy model."""

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    async def create(self, session: AsyncSession, **fields: Any) -> ModelT:
        db_obj = self.model(**fields)
        session.add(db_obj)
        await session.flush()
        await session.refresh(db_obj)
        return db_obj

    async def get(self, session: AsyncSession, id: UUID) -> ModelT | None:
        return await session.get(self.model, id)

    async def list(
        self,
        session: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ModelT]:
        result = await session.execute(
            select(self.model).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def update(self, session: AsyncSession, db_obj: ModelT, **fields: Any) -> ModelT:
        for key, value in fields.items():
            setattr(db_obj, key, value)
        session.add(db_obj)
        await session.flush()
        await session.refresh(db_obj)
        return db_obj

    async def delete(self, session: AsyncSession, db_obj: ModelT) -> None:
        await session.delete(db_obj)
        await session.flush()


class AppendOnlyCRUDBase(Generic[ModelT]):
    """Append-only CRUD: create, get, and list only — no update or delete."""

    def __init__(self, model: type[ModelT]) -> None:
        self.model = model

    async def create(self, session: AsyncSession, **fields: Any) -> ModelT:
        db_obj = self.model(**fields)
        session.add(db_obj)
        await session.flush()
        await session.refresh(db_obj)
        return db_obj

    async def get(self, session: AsyncSession, id: UUID) -> ModelT | None:
        return await session.get(self.model, id)

    async def list(
        self,
        session: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ModelT]:
        result = await session.execute(
            select(self.model).offset(skip).limit(limit)
        )
        return list(result.scalars().all())
