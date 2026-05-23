"""Multi-turn onboarding; uses onboarding.txt and onboarding_service."""

from __future__ import annotations

import json
import re
from pathlib import Path

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas.webhook import TelegramMessage
from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.models.user import User
from app.services import onboarding_service
from app.telegram.client import telegram_client

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "agent" / "prompts" / "onboarding.txt"
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _parse_llm_json(raw: str) -> dict:
    text = _JSON_FENCE_RE.sub("", raw.strip())
    if not text:
        raise ValueError("empty model response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


async def _refine_goal(raw_goal: str) -> dict:
    """Call the LLM to refine a raw goal into a concrete, measurable version."""
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.llm_model,
        max_tokens=256,
        system=system_prompt,
        messages=[{"role": "user", "content": raw_goal}],
    )
    parts = [block.text for block in response.content if block.type == "text"]
    raw = "\n".join(parts).strip()
    return _parse_llm_json(raw)


def _build_confirmation_summary(data: dict) -> str:
    lines = ["Here's what I've got — does this look right?\n"]
    if goal := data.get("goal_refined") or data.get("goal_raw"):
        lines.append(f"Goal: {goal}")
    if target := data.get("goal_target_date"):
        lines.append(f"Target date: {target}")
    if schedule := data.get("fixed_commitments"):
        lines.append(f"Fixed commitments: {schedule}")
    if deadline := data.get("deadline"):
        lines.append(f"Hard deadline: {deadline}")
    if pref := data.get("work_preference"):
        lines.append(f"Best work time: {pref}")
    style_map = {"A": "Gentle", "B": "Firm", "C": "Brutal"}
    if style := data.get("accountability_style"):
        lines.append(f"Accountability: {style_map.get(style.upper(), style)}")
    lines.append("\nReply yes to confirm, or tell me what to fix.")
    return "\n".join(lines)


async def handle_onboarding(
    session: AsyncSession, user: User, message: TelegramMessage
) -> None:
    """Route an incoming message to the correct onboarding step handler."""
    chat_id = message.chat.id
    text = (message.text or "").strip()
    step = user.onboarding_step

    logger.info("onboarding_step chat_id=%s step=%s", chat_id, step)

    match step:
        case "welcome":
            await user_crud.update(session, user, onboarding_step="goal_input")
            await telegram_client.send_message(
                chat_id,
                "Hey! I'm Ballast — your personal accountability agent.\n\n"
                "Before we get started, I want to learn about you.\n\n"
                "What's one goal you're working toward right now?",
            )

        case "goal_input":
            await onboarding_service.save_onboarding_answer(session, user, "goal_raw", text)
            try:
                refined = await _refine_goal(text)
                refined_goal = refined.get("refined_goal", text)
                target_date = refined.get("target_date")
            except Exception:
                logger.exception("goal_refinement_failed chat_id=%s", chat_id)
                refined_goal = text
                target_date = None

            await onboarding_service.save_onboarding_answer(
                session, user, "goal_refined", refined_goal
            )
            if target_date:
                await onboarding_service.save_onboarding_answer(
                    session, user, "goal_target_date", target_date
                )
            await user_crud.update(session, user, onboarding_step="goal_confirm")

            date_note = f" by {target_date}" if target_date else ""
            await telegram_client.send_message(
                chat_id,
                f"Got it — so: {refined_goal}{date_note}?\n\n"
                "Does that capture it, or is the target different?",
            )

        case "goal_confirm":
            positive = text.lower() in {"yes", "y", "yeah", "yep", "correct", "right", "sure", "ok", "okay"}
            if not positive:
                await onboarding_service.save_onboarding_answer(
                    session, user, "goal_refined", text
                )
                await onboarding_service.save_onboarding_answer(
                    session, user, "goal_target_date", ""
                )
            await user_crud.update(session, user, onboarding_step="fixed_commitments")
            await telegram_client.send_message(
                chat_id,
                "What does your weekly schedule look like? Tell me about any fixed "
                "commitments — classes, training, work hours, anything that can't move.",
            )

        case "fixed_commitments":
            await onboarding_service.save_onboarding_answer(
                session, user, "fixed_commitments", text
            )
            await user_crud.update(session, user, onboarding_step="deadline")
            await telegram_client.send_message(
                chat_id,
                "Is there a hard deadline I should know about? Like an application "
                "date, exam, or specific target date?",
            )

        case "deadline":
            await onboarding_service.save_onboarding_answer(session, user, "deadline", text)
            await user_crud.update(session, user, onboarding_step="work_preference")
            await telegram_client.send_message(
                chat_id,
                "When do you do your best focused work? Morning, afternoon, or evening?",
            )

        case "work_preference":
            await onboarding_service.save_onboarding_answer(
                session, user, "work_preference", text
            )
            await user_crud.update(session, user, onboarding_step="accountability_style")
            await telegram_client.send_message(
                chat_id,
                "Last one — how hard should I push you?\n\n"
                "(A) Gentle — nudges and encouragement\n"
                "(B) Firm — I'll push back when you slip\n"
                "(C) Brutal — no excuses, strict consequences",
            )

        case "accountability_style":
            style = text.strip().upper()
            if style not in {"A", "B", "C"}:
                await telegram_client.send_message(
                    chat_id, "Please reply with A, B, or C."
                )
                return
            await onboarding_service.save_onboarding_answer(
                session, user, "accountability_style", style
            )
            await user_crud.update(session, user, onboarding_step="confirm")
            summary = _build_confirmation_summary(user.onboarding_data)
            await telegram_client.send_message(chat_id, summary)

        case "confirm":
            positive = text.lower() in {"yes", "y", "yeah", "yep", "correct", "right", "sure", "ok", "okay"}
            if not positive:
                await telegram_client.send_message(
                    chat_id,
                    "No problem — what needs to change? (Onboarding restart coming soon; "
                    "for now reply yes when you're ready.)",
                )
                return
            await onboarding_service.complete_onboarding(session, user)
            await telegram_client.send_message(chat_id, "You're all set. Let's get to work.")

        case _:
            logger.warning("unknown_onboarding_step chat_id=%s step=%s", chat_id, step)
            await telegram_client.send_message(
                chat_id, "Something went wrong with onboarding. Please try again later."
            )
