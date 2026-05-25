"""Handler for pending confirmation state: routes yes/no to commit or cancel."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.webhook import TelegramMessage
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import message_service, schedule_service, task_service
from app.services.schedule_service import BatchScheduleProposal, ScheduleProposal
from app.services.scheduling_prefs import user_timezone
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
        proposal_action = raw.get("action")
        await user_crud.update(session, user, pending_confirmation=None)

        try:
            if proposal_action == "batch_add":
                batch = BatchScheduleProposal.model_validate(raw)
                tasks = await schedule_service.commit_batch_tasks(batch, session, user)
                tz = user_timezone(user.timezone)
                first_start = min(p.proposed_start for p in batch.sessions)
                local_first = first_start.astimezone(tz)
                first_str = local_first.strftime("%a %b %-d at %-I:%M %p")
                total_hours = round(batch.duration_mins * batch.total_sessions / 60, 1)
                reply = (
                    f"Done — {batch.title} is locked in {batch.days_label} "
                    f"for {batch.weeks} weeks. "
                    f"First session {first_str}. "
                    f"That's {total_hours}h of work — let's go."
                )
            elif proposal_action == "delete_task":
                title = raw.get("title", "the task")
                task_ids: list[str] = raw.get("task_ids") or (
                    [raw["task_id"]] if raw.get("task_id") else []
                )
                errors: list[str] = []
                for tid in task_ids:
                    res = await task_service.delete_task(session, user, task_id_str=tid)
                    if res.get("error"):
                        errors.append(tid)
                if errors and len(errors) == len(task_ids):
                    reply = f"Couldn't find '{title}' — it may have already been removed."
                else:
                    reply = f"Deleted '{title}'."
            elif proposal_action == "reschedule":
                proposal = ScheduleProposal.model_validate(raw)
                await schedule_service.commit_reschedule(proposal, session, user)
                new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
                reply = f"Done — '{proposal.title}' moved to {new_time}."
            else:
                proposal = ScheduleProposal.model_validate(raw)
                task = await schedule_service.commit_new_task(proposal, session, user)
                new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
                reply = f"Done — added '{task.title}' to your schedule on {new_time}."
        except Exception:
            logger.exception("confirmation_commit_failed chat_id=%s action=%s", chat_id, raw.get("action"))
            reply = "Something went wrong committing that. Please try again."

        await message_service.save_message(session, user.id, "user", message.text or "")
        await message_service.save_message(session, user.id, "assistant", reply)
        await session.flush()
        await telegram_client.send_message(chat_id, reply)

    elif text in _NO_WORDS:
        await user_crud.update(session, user, pending_confirmation=None)
        cancel_reply = "No problem, keeping it as is."
        await message_service.save_message(session, user.id, "user", message.text or "")
        await message_service.save_message(session, user.id, "assistant", cancel_reply)
        await session.flush()
        await telegram_client.send_message(chat_id, cancel_reply)

    else:
        proposal_action = raw.get("action")
        if proposal_action == "batch_add":
            batch = BatchScheduleProposal.model_validate(raw)
            prompt = (
                f"Still waiting — {batch.title} every {batch.days_label} "
                f"for {batch.weeks} weeks ({batch.total_sessions} sessions)? (yes/no)"
            )
        elif proposal_action == "delete_task":
            title = raw.get("title", "the task")
            prompt = f"Still waiting — delete '{title}'? This can't be undone. (yes/no)"
        else:
            proposal = ScheduleProposal.model_validate(raw)
            new_time = proposal.proposed_start.strftime("%a %b %-d at %-I:%M %p")
            if proposal_action == "reschedule":
                prompt = f"Still waiting — move '{proposal.title}' to {new_time}? (yes/no)"
            else:
                prompt = f"Still waiting — add '{proposal.title}' on {new_time}? (yes/no)"
        await message_service.save_message(session, user.id, "user", message.text or "")
        await message_service.save_message(session, user.id, "assistant", prompt)
        await session.flush()
        await telegram_client.send_message(chat_id, prompt)

    logger.info(
        "confirmation_handled chat_id=%s response=%r action=%s",
        chat_id,
        text,
        raw.get("action"),
    )
