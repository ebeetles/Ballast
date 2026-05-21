"""Telegram webhook endpoint. Delegates to dispatcher only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.v1.schemas.webhook import TelegramUpdate
from app.telegram import dispatcher
from app.telegram.client import telegram_client

router = APIRouter()


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, str]:
    body = await request.body()
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not telegram_client.verify_webhook_signature(body, secret_token):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    update = TelegramUpdate.model_validate_json(body)
    await dispatcher.handle_update(update)
    return {"status": "ok"}
