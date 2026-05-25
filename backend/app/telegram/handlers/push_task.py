"""Handler for push_task intent: proposes rescheduling an existing task."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.response_formatter import format_proposal
from app.agent.router import IntentResult
from app.api.v1.schemas.webhook import TelegramMessage
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import debt_service, schedule_service, task_service
from app.services.user_service import resolve_timezone
from app.telegram.client import telegram_client

logger = get_logger(__name__)


async def handle(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
    intent_result: IntentResult,
) -> None:
    """Find the named task, propose a reschedule slot, and await confirmation."""
    chat_id = message.chat.id
    task_name: str = intent_result.extracted_params.get("task", "").strip()

    if not task_name:
        await telegram_client.send_message(
            chat_id, "Which task do you want to push? Just tell me the name."
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
            reply = f"I couldn't find a task matching '{task_name}' and you have no active tasks."
        await telegram_client.send_message(chat_id, reply)
        return

    time_of_day = intent_result.extracted_params.get("time_of_day")
    if isinstance(time_of_day, str):
        time_of_day = time_of_day.strip() or None
    else:
        time_of_day = None

    proposal = await schedule_service.propose_reschedule(
        task, user, time_of_day=time_of_day
    )
    total_debt = await debt_service.get_total_debt(session, user.id)

    reply = format_proposal(
        proposal,
        current_debt=total_debt,
        max_debt=user.max_debt_limit,
        user_timezone=resolve_timezone(user),
    )

    await user_crud.update(
        session,
        user,
        pending_confirmation=proposal.model_dump(mode="json"),
    )
    await session.flush()

    logger.info(
        "push_task_proposed chat_id=%s task=%r proposed_start=%s",
        chat_id,
        task.title,
        proposal.proposed_start,
    )
    await telegram_client.send_message(chat_id, reply, parse_mode="MarkdownV2")
