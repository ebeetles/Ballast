"""Telegram bot API wrapper and signature verification."""

from __future__ import annotations

import hmac

import httpx

from app.core.config import settings
from app.core.exceptions import ValidationError

_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramClient:
    """Async wrapper around the Telegram Bot API."""

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        """Send a text message to the given chat.

        Args:
            chat_id: Telegram chat identifier.
            text: Message text.  When ``parse_mode`` is ``"MarkdownV2"`` the text
                must already be escaped for Telegram MarkdownV2.
            parse_mode: Optional Telegram parse mode (e.g. ``"MarkdownV2"``).
        """
        token = settings.telegram_bot_token.strip()
        if not token:
            raise ValidationError(
                "TELEGRAM_BOT_TOKEN is not set. Add it to Ballast/.env or backend/.env "
                "and restart uvicorn."
            )
        url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

    def verify_webhook_signature(self, body: bytes, secret_token: str) -> bool:
        """Verify the X-Telegram-Bot-Api-Secret-Token header value.

        Returns True when no secret is configured (development / unprotected).
        Uses constant-time comparison to prevent timing attacks.

        Args:
            body: Raw request body bytes (reserved for future HMAC support).
            secret_token: Value of X-Telegram-Bot-Api-Secret-Token header.
        """
        expected = settings.telegram_webhook_secret
        if not expected:
            return True
        return hmac.compare_digest(secret_token, expected)


telegram_client = TelegramClient()
