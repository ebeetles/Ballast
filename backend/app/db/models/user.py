"""User model with onboarding_status, onboarding_step, and goals fields."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    onboarding_status: Mapped[str] = mapped_column(String(32), default="incomplete")
    onboarding_step: Mapped[str] = mapped_column(String(64), default="welcome")
    goals: Mapped[str | None] = mapped_column(String, nullable=True)
    preferences: Mapped[str | None] = mapped_column(String, nullable=True)
