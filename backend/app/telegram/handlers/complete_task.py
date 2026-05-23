"""Handler for complete_task intent: marks a task done or requests proof."""

from __future__ import annotations

import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import IntentResult
from app.api.v1.schemas.webhook import TelegramMessage
from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import task_crud
from app.db.models.task import TaskStatus
from app.db.models.user import User
from app.services import debt_service, task_service
from app.telegram.client import telegram_client

logger = get_logger(__name__)

_ENCOURAGEMENT = [
    "Keep the momentum going!",
    "One down. What's next?",
    "Nice — that's how it's done.",
    "Every completed task is progress.",
]


def _get_gcal_client():
    """Return GoogleCalendarClient or None if GCal is not configured."""
    if not settings.google_calendar_credentials_file:
        return None
    try:
        from app.calendar.gcal_client import GoogleCalendarClient
        return GoogleCalendarClient()
    except Exception:
        logger.exception("gcal_client_init_failed")
        return None


async def handle(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
    intent_result: IntentResult,
) -> None:
    """Complete a task, handling proof requirements and debt adjustments."""
    chat_id = message.chat.id
    task_name: str = intent_result.extracted_params.get("task", "").strip()

    if not task_name:
        await telegram_client.send_message(
            chat_id, "Which task did you complete? Just tell me the name."
        )
        return

    task = await task_service.find_task_by_name(session, user.id, task_name)

    if task is None:
        active_tasks = await task_service.get_user_tasks(session, user.id)
        if active_tasks:
            task_list = "\n".join(f"• {t.title}" for t in active_tasks)
            reply = (
                f"I couldn't find a task matching '{task_name}'. "
                f"Here are your active tasks:\n{task_list}"
            )
        else:
            reply = f"I couldn't find a task matching '{task_name}'."
        await telegram_client.send_message(chat_id, reply)
        return

    if task.requires_proof:
        await task_crud.update(session, task, status=TaskStatus.AWAITING_PROOF.value)
        await session.flush()
        await telegram_client.send_message(
            chat_id,
            f"Nice work! Send me your proof to mark '{task.title}' complete "
            "(screenshot, link, etc.)",
        )
        logger.info(
            "complete_task_awaiting_proof chat_id=%s task=%r", chat_id, task.title
        )
        return

    was_pushed = task.status == TaskStatus.PUSHED.value

    gcal = _get_gcal_client()
    if gcal is not None and task.gcal_event_id:
        try:
            await gcal.delete_event(task.gcal_event_id)
        except Exception:
            logger.exception(
                "gcal_delete_event_failed event_id=%s", task.gcal_event_id
            )

    await task_crud.update(session, task, status=TaskStatus.COMPLETED.value)

    if was_pushed:
        await debt_service.subtract_debt(
            session,
            user_id=user.id,
            task_id=task.id,
            hours=task.duration_mins / 60,
            reason=f"Completed pushed task '{task.title}'",
        )

    await session.flush()

    encouragement = random.choice(_ENCOURAGEMENT)
    await telegram_client.send_message(
        chat_id, f"Marked '{task.title}' as complete. {encouragement}"
    )
    logger.info("complete_task_done chat_id=%s task=%r", chat_id, task.title)
