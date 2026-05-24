"""ReAct reasoning loop for general_chat — assembles context, calls LLM, persists conversation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import anthropic

from app.agent.context_assembler import assemble_context, to_prompt_string
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.services import message_service

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "cognitive_loop.txt"


def _extract_response_text(response: anthropic.types.Message) -> str:
    """Collect text blocks from an Anthropic response, skipping tool/thinking blocks."""
    parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


async def run(user_id: UUID, message: str) -> str:
    """Run the cognitive loop for a single user message.

    Assembles full context, calls the LLM with conversation history, persists
    both the user message and assistant response, and returns the response text.
    Never raises — returns a graceful fallback on any failure.
    """
    try:
        async with async_session_factory() as session:
            context = await assemble_context(session, user_id)
            context_str = to_prompt_string(context)
            system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").replace("{context}", context_str)

            messages: list[dict[str, str]] = [
                {"role": m.role, "content": m.content}
                for m in context.recent_messages
            ]
            messages.append({"role": "user", "content": message})

            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            response = await client.messages.create(
                model=settings.llm_model,
                max_tokens=1024,
                system=system_prompt,
                messages=messages,
            )
            assistant_text = _extract_response_text(response)

            await message_service.save_message(session, user_id, "user", message)
            await message_service.save_message(session, user_id, "assistant", assistant_text)
            await session.commit()

            return assistant_text
    except Exception:
        logger.exception("cognitive_loop_failed user_id=%s", user_id)
        return "I'm having trouble thinking right now. Try again in a moment."
