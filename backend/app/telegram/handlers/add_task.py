"""Handler for add_task intent: proposes a slot for a new task."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.router import IntentResult
from app.api.v1.schemas.webhook import TelegramMessage
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import schedule_service
from app.telegram.client import telegram_client

logger = get_logger(__name__)

AWAITING_TITLE_ACTION = "add_task_awaiting_title"


def _parse_deadline(raw: str | None) -> datetime | None:
    """Best-effort ISO datetime parse; returns None on any failure."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _extract_title(params: dict[str, Any]) -> str:
    """Title from router params (intent prompt uses 'task', not 'title')."""
    for key in ("title", "task", "name"):
        value = params.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_duration_mins(params: dict[str, Any]) -> int:
    """Parse duration from duration_mins int or natural-language duration string."""
    if "duration_mins" in params:
        try:
            return max(1, int(params["duration_mins"]))
        except (TypeError, ValueError):
            pass

    raw = params.get("duration")
    if raw is None:
        return 60
    if isinstance(raw, (int, float)):
        return max(1, int(raw))

    text = str(raw).lower().strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(h(?:our)?s?|m(?:in(?:ute)?s?)?)\b", text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)
        if unit.startswith("h"):
            return max(1, int(amount * 60))
        return max(1, int(amount))

    digits = re.search(r"(\d+)", text)
    if digits:
        return max(1, int(digits.group(1)))
    return 60


def _awaiting_title_payload(
    duration_mins: int,
    deadline_at: datetime | None,
    requires_proof: bool,
) -> dict[str, Any]:
    return {
        "action": AWAITING_TITLE_ACTION,
        "duration_mins": duration_mins,
        "deadline_at": deadline_at.isoformat() if deadline_at else None,
        "requires_proof": requires_proof,
    }


async def _send_proposal(
    session: AsyncSession,
    user: User,
    chat_id: int,
    title: str,
    duration_mins: int,
    deadline_at: datetime | None,
    requires_proof: bool,
) -> None:
    """Find a slot, store confirmation state, and send the proposal message."""
    proposal = await schedule_service.propose_slot(
        title=title,
        duration_mins=duration_mins,
        user=user,
        deadline_at=deadline_at,
        requires_proof=requires_proof,
    )

    date_str = proposal.proposed_start.strftime("%a %b %-d")
    time_str = proposal.proposed_start.strftime("%-I:%M %p")

    reply = (
        f"Adding '{title}' ({duration_mins}min) on {date_str} at {time_str}.\n"
        "Confirm? (yes/no)"
    )

    await user_crud.update(
        session,
        user,
        pending_confirmation=proposal.model_dump(mode="json"),
    )
    await session.flush()

    logger.info(
        "add_task_proposed chat_id=%s title=%r slot=%s",
        chat_id,
        title,
        proposal.proposed_start,
    )
    await telegram_client.send_message(chat_id, reply)


async def handle(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
    intent_result: IntentResult,
) -> None:
    """Propose a time slot for a new task and store confirmation state."""
    chat_id = message.chat.id
    params = intent_result.extracted_params

    title = _extract_title(params)
    duration_mins = _parse_duration_mins(params)
    deadline_at = _parse_deadline(params.get("deadline"))
    requires_proof = bool(params.get("requires_proof", False))

    if not title:
        await user_crud.update(
            session,
            user,
            pending_confirmation=_awaiting_title_payload(
                duration_mins, deadline_at, requires_proof
            ),
        )
        await session.flush()
        await telegram_client.send_message(
            chat_id, "What's the name of the task you want to add?"
        )
        return

    await _send_proposal(
        session, user, chat_id, title, duration_mins, deadline_at, requires_proof
    )


async def handle_awaiting_title(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
) -> None:
    """Complete add_task flow when the user replies with a task name."""
    chat_id = message.chat.id
    title = (message.text or "").strip()
    raw = user.pending_confirmation or {}

    duration_mins = int(raw.get("duration_mins", 60))
    deadline_at = _parse_deadline(raw.get("deadline_at"))
    requires_proof = bool(raw.get("requires_proof", False))

    if not title:
        await telegram_client.send_message(
            chat_id, "What's the name of the task you want to add?"
        )
        return

    await _send_proposal(
        session, user, chat_id, title, duration_mins, deadline_at, requires_proof
    )
