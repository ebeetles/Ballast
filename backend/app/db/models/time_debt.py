"""Time debt model."""

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TimeDebt(Base):
    __tablename__ = "time_debts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    minutes: Mapped[int] = mapped_column(Integer, default=0)
