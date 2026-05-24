"""Delegates general conversation to the cognitive loop."""

from __future__ import annotations

from app.agent import cognitive_loop
from app.agent.router import IntentResult
from app.core.logging import get_logger
from app.db.models.user import User
from app.telegram.client import telegram_client

logger = get_logger(__name__)


async def handle(session, user: User, message, result: IntentResult) -> None:
    """Route a general_chat message through the cognitive loop and send the response."""
    response = await cognitive_loop.run(user.id, message.text)
    try:
        await telegram_client.send_message(user.telegram_chat_id, response)
    except Exception:
        logger.exception("failed_to_send_general_chat chat_id=%s", user.telegram_chat_id)
