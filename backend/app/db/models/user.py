"""User model with onboarding and debt limit fields."""

from __future__ import annotations

import uuid
from datetime import datetime

from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    max_debt_limit: Mapped[float] = mapped_column(Float, default=0.0)
    onboarding_status: Mapped[str] = mapped_column(String(32), default="incomplete")
    onboarding_step: Mapped[str] = mapped_column(String(64), default="welcome")
    onboarding_data: Mapped[dict] = mapped_column(JSON, default=dict)
    pending_confirmation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
