"""Routes updates: onboarding gate, then agent/router, then handler."""

from __future__ import annotations

from app.api.v1.schemas.webhook import TelegramUpdate
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services import user_service
from app.telegram.client import telegram_client

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

    if message.text:
        try:
            await telegram_client.send_message(chat_id, message.text)
        except Exception:
            logger.exception("failed to send telegram reply chat_id=%s", chat_id)
