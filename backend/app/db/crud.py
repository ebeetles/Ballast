"""Per-model CRUD instances."""

from app.db.base import AppendOnlyCRUDBase, CRUDBase
from app.db.models.insight import UserProfileInsight
from app.db.models.task import Task
from app.db.models.time_debt import TimeDebtLedger
from app.db.models.user import User

user_crud = CRUDBase(User)
task_crud = CRUDBase(Task)
ledger_crud = AppendOnlyCRUDBase(TimeDebtLedger)
insight_crud = CRUDBase(UserProfileInsight)
