"""Routes updates: onboarding gate, then agent/router, then handler."""

from __future__ import annotations

from app.agent.router import classify_intent
from app.api.v1.schemas.webhook import TelegramUpdate
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services import user_service
from app.telegram.client import telegram_client
from app.telegram.handlers import onboarding as onboarding_handler

logger = get_logger(__name__)


async def handle_update(update: TelegramUpdate) -> None:
    """Entry point from webhook. Checks onboarding, then routes intent."""
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

    result = await classify_intent(message.text, {})

    logger.info(
        "intent_classified chat_id=%s intent=%s confidence=%.2f params=%s",
        chat_id,
        result.intent.value,
        result.confidence,
        result.extracted_params,
    )

    reply = f"Detected: {result.intent.value} ({result.confidence:.2f})\nParams: {result.extracted_params}"
    try:
        await telegram_client.send_message(chat_id, reply)
    except Exception:
        logger.exception("failed to send telegram reply chat_id=%s", chat_id)
