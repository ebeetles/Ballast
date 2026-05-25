"""Routes updates: onboarding gate, confirmation state, then agent/router, then handler."""

from __future__ import annotations

from app.agent.router import Intent, classify_intent
from app.api.v1.schemas.webhook import TelegramUpdate
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.session import async_session_factory
from app.services import user_service
from app.telegram.client import telegram_client
from app.telegram.handlers import onboarding as onboarding_handler
from app.telegram.handlers import confirmation as confirmation_handler
from app.telegram.handlers import push_task as push_task_handler
from app.telegram.handlers import complete_task as complete_task_handler
from app.telegram.handlers import add_task as add_task_handler
from app.telegram.handlers import delete_task as delete_task_handler
from app.telegram.handlers import general_chat as general_chat_handler
from app.telegram.handlers.add_task import AWAITING_TITLE_ACTION

_YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it",
              "confirm", "go ahead", "sounds good", "alright", "y"}
_NO_WORDS = {"no", "nope", "nah", "cancel", "nevermind", "never mind",
             "stop", "don't", "n"}

logger = get_logger(__name__)


async def handle_update(update: TelegramUpdate) -> None:
    """Entry point from webhook. Checks onboarding, confirmation state, then routes intent."""
    if update.message is None:
        return

    message = update.message
    chat_id = message.chat.id

    async with async_session_factory() as session:
        user, created = await user_service.get_or_create_user(session, chat_id)
        if created:
            await session.commit()
            logger.info("new_user chat_id=%s onboarding_status=%s", chat_id, user.onboarding_status)

    logger.info(
        "incoming_message chat_id=%s update_id=%s text=%r",
        chat_id,
        update.update_id,
        message.text,
    )

    if not message.text:
        return

    if user.onboarding_status == "pending":
        async with async_session_factory() as session:
            user, _ = await user_service.get_or_create_user(session, chat_id)
            await onboarding_handler.handle_onboarding(session, user, message)
            await session.commit()
        return

    async with async_session_factory() as session:
        user, _ = await user_service.get_or_create_user(session, chat_id)

        if user.pending_confirmation is not None:
            pending_action = user.pending_confirmation.get("action")
            if pending_action == AWAITING_TITLE_ACTION:
                await add_task_handler.handle_awaiting_title(session, user, message)
                await session.commit()
                return

            text_lower = (message.text or "").strip().lower()
            if text_lower in _YES_WORDS or text_lower in _NO_WORDS:
                await confirmation_handler.handle_confirmation(session, user, message)
                await session.commit()
                return

            # Not a clear yes/no — escape if the message is clearly something else
            word_count = len(text_lower.split())
            is_question = "?" in text_lower
            if word_count > 6 or is_question:
                await user_crud.update(session, user, pending_confirmation=None)
                await session.flush()
                result = await classify_intent(message.text, {})
                await general_chat_handler.handle(session, user, message, result)
                await session.commit()
                return

            # Short non-yes/no — run the router to decide
            result = await classify_intent(message.text, {})
            if result.intent not in (Intent.unknown, Intent.general_chat):
                # Clearly a different action — clear confirmation and fall through to route normally
                await user_crud.update(session, user, pending_confirmation=None)
                await session.flush()
            else:
                # Ambiguous short text — keep confirmation active, re-prompt
                await confirmation_handler.handle_confirmation(session, user, message)
                await session.commit()
                return

        result = await classify_intent(message.text, {})

        logger.info(
            "intent_classified chat_id=%s intent=%s confidence=%.2f params=%s",
            chat_id,
            result.intent.value,
            result.confidence,
            result.extracted_params,
        )

        match result.intent:
            case Intent.push_task:
                await push_task_handler.handle(session, user, message, result)
            case Intent.complete_task:
                await complete_task_handler.handle(session, user, message, result)
            case Intent.add_task:
                await add_task_handler.handle(session, user, message, result)
            case Intent.delete_task:
                await delete_task_handler.handle(session, user, message, result)
            case Intent.general_chat:
                await general_chat_handler.handle(session, user, message, result)
            case _:
                await general_chat_handler.handle(session, user, message, result)

        await session.commit()
