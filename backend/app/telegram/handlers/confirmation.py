"""Handler for pending confirmation state: routes yes/no to commit or cancel."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.webhook import TelegramMessage
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import schedule_service
from app.services.schedule_service import ScheduleProposal
from app.telegram.client import telegram_client

logger = get_logger(__name__)

_YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "do it"}
_NO_WORDS = {"no", "n", "nope", "nah", "cancel", "skip", "never mind", "nevermind"}


async def handle_confirmation(
    session: AsyncSession,
    user: User,
    message: TelegramMessage,
) -> None:
    """Check user response and commit or cancel the pending confirmation."""
    chat_id = message.chat.id
    text = (message.text or "").strip().lower()

    raw = user.pending_confirmation
    if raw is None:
        return

    if text in _YES_WORDS:
        proposal = ScheduleProposal.model_validate(raw)

        await user_crud.update(session, user, pending_confirmation=None)

        try:
            if proposal.action == "reschedule":
                await schedule_service.commit_reschedule(proposal, session, user)
                new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
                reply = f"Done — '{proposal.title}' moved to {new_time}."
            else:
                task = await schedule_service.commit_new_task(proposal, session, user)
                new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
                reply = f"Done — added '{task.title}' to your schedule on {new_time}."
        except Exception:
            logger.exception("confirmation_commit_failed chat_id=%s action=%s", chat_id, raw.get("action"))
            reply = "Something went wrong committing that. Please try again."

        await session.flush()
        await telegram_client.send_message(chat_id, reply)

    elif text in _NO_WORDS:
        await user_crud.update(session, user, pending_confirmation=None)
        await session.flush()
        await telegram_client.send_message(chat_id, "No problem, keeping it as is.")

    else:
        proposal = ScheduleProposal.model_validate(raw)
        new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
        if proposal.action == "reschedule":
            prompt = (
                f"Still waiting — move '{proposal.title}' to {new_time}? (yes/no)"
            )
        else:
            prompt = (
                f"Still waiting — add '{proposal.title}' on {new_time}? (yes/no)"
            )
        await telegram_client.send_message(chat_id, prompt)

    logger.info(
        "confirmation_handled chat_id=%s response=%r action=%s",
        chat_id,
        text,
        raw.get("action"),
    )
