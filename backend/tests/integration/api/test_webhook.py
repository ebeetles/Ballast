"""Integration tests for the Telegram webhook endpoint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

VALID_PAYLOAD = {
    "update_id": 1,
    "message": {
        "message_id": 10,
        "from": {"id": 999, "is_bot": False, "first_name": "Test"},
        "chat": {"id": 999, "type": "private"},
        "text": "hello",
    },
}

WEBHOOK_URL = "/api/v1/webhook/telegram"


@pytest.mark.asyncio
async def test_webhook_missing_secret_returns_401(http_client: AsyncClient) -> None:
    """No secret header when TELEGRAM_WEBHOOK_SECRET is set → 401."""
    with patch("app.core.config.settings.telegram_webhook_secret", "mysecret"):
        response = await http_client.post(
            WEBHOOK_URL,
            content=json.dumps(VALID_PAYLOAD),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_wrong_secret_returns_401(http_client: AsyncClient) -> None:
    with patch("app.core.config.settings.telegram_webhook_secret", "mysecret"):
        response = await http_client.post(
            WEBHOOK_URL,
            content=json.dumps(VALID_PAYLOAD),
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "wrongsecret",
            },
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_valid_secret_calls_dispatcher(http_client: AsyncClient) -> None:
    with (
        patch("app.core.config.settings.telegram_webhook_secret", "mysecret"),
        patch(
            "app.telegram.dispatcher.handle_update", new_callable=AsyncMock
        ) as mock_handle,
        patch("app.telegram.client.telegram_client.send_message", new_callable=AsyncMock),
    ):
        response = await http_client.post(
            WEBHOOK_URL,
            content=json.dumps(VALID_PAYLOAD),
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "mysecret",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_no_secret_configured_accepts_any_token(
    http_client: AsyncClient,
) -> None:
    """When telegram_webhook_secret is empty (dev mode), all requests pass."""
    with (
        patch("app.core.config.settings.telegram_webhook_secret", ""),
        patch(
            "app.telegram.dispatcher.handle_update", new_callable=AsyncMock
        ),
        patch("app.telegram.client.telegram_client.send_message", new_callable=AsyncMock),
    ):
        response = await http_client.post(
            WEBHOOK_URL,
            content=json.dumps(VALID_PAYLOAD),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 200
