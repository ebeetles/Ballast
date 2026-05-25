"""Handler for delete_task intent: asks for confirmation before deleting a task."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import IntentResult
from app.api.v1.schemas.webhook import TelegramMessage
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import task_service
from app.telegram.client import telegram_client

logger = get_logger(__name__)


async def handle(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
    intent_result: IntentResult,
) -> None:
    """Find the task(s) and propose deletion with a confirmation prompt."""
    chat_id = message.chat.id
    task_name: str = intent_result.extracted_params.get("task", "").strip()

    if not task_name:
        await telegram_client.send_message(
            chat_id,
            "Which task do you want to delete? Tell me the name.",
        )
        return

    # Handle compound "X and Y" queries
    if " and " in task_name.lower():
        pairs = await task_service.find_tasks_by_compound_name(session, user.id, task_name)
        found = [(t, part) for t, part in pairs if t is not None]
        missing = [part for t, part in pairs if t is None]

        if not found:
            active_tasks = await task_service.get_user_tasks(session, user.id)
            if active_tasks:
                task_list = "\n".join(f"• {t.title}" for t in active_tasks)
                reply = (
                    f"I couldn't find any tasks matching '{task_name}'. "
                    f"Here are your active tasks:\n{task_list}"
                )
            else:
                reply = f"I couldn't find any tasks matching '{task_name}'."
            await telegram_client.send_message(chat_id, reply)
            return

        titles = [t.title for t, _ in found]
        task_ids = [str(t.id) for t, _ in found]
        titles_str = "' and '".join(titles)

        await user_crud.update(
            session,
            user,
            pending_confirmation={
                "action": "delete_task",
                "task_ids": task_ids,
                "title": titles_str,
            },
        )
        await session.flush()

        prompt = f"Delete '{titles_str}'? This can't be undone. Reply yes or no."
        if missing:
            prompt += f" (Couldn't find: {', '.join(missing)})"
        await telegram_client.send_message(chat_id, prompt)
        logger.info("delete_task_proposed chat_id=%s tasks=%r", chat_id, titles)
        return

    # Single task
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

    await user_crud.update(
        session,
        user,
        pending_confirmation={
            "action": "delete_task",
            "task_id": str(task.id),
            "title": task.title,
        },
    )
    await session.flush()

    await telegram_client.send_message(
        chat_id,
        f"Delete '{task.title}'? This can't be undone. Reply yes or no.",
    )
    logger.info("delete_task_proposed chat_id=%s task=%r", chat_id, task.title)
