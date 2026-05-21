"""Telegram webhook endpoint. Delegates to dispatcher only."""


from fastapi import APIRouter, Request

from app.telegram.dispatcher import handle_update

router = APIRouter()


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> dict[str, str]:
    update = await request.json()
    await handle_update(update)
    return {"status": "ok"}
