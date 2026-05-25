"""ReAct reasoning loop for general_chat — assembles context, calls LLM, persists conversation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import anthropic

from app.agent.context_assembler import AgentContext, assemble_context, to_prompt_string
from app.agent.response_formatter import (
    _ERROR_FALLBACK,
    format_batch_proposal,
    format_for_telegram,
    format_proposal,
)
from app.agent.tools.task_tools import TOOL_DEFINITIONS, execute_tool
from app.core.config import settings
from app.core.logging import get_logger
from app.db.crud import user_crud
from app.db.session import async_session_factory
from app.services import message_service
from app.services.schedule_service import BatchScheduleProposal, ScheduleProposal

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "cognitive_loop.txt"
MAX_TOOL_ITERATIONS = 5


def _extract_response_text(response: anthropic.types.Message) -> str:
    """Collect text blocks from an Anthropic response, skipping tool/thinking blocks."""
    parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _safe_dump_messages(messages: list[dict[str, Any]], snippet_chars: int = 200) -> list[dict]:
    """Render the API messages array into a compact, log-safe form.

    Each entry keeps the role and a truncated text rendition of the content so
    debug logs stay readable when conversation history is long.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "?")
                    if btype == "text":
                        chunks.append(f"[text] {block.get('text', '')}")
                    elif btype == "tool_use":
                        chunks.append(
                            f"[tool_use:{block.get('name', '?')} input={block.get('input', {})}]"
                        )
                    elif btype == "tool_result":
                        result_content = block.get("content", "")
                        chunks.append(f"[tool_result] {result_content}")
                    else:
                        chunks.append(f"[{btype}]")
                else:
                    chunks.append(repr(block)[:80])
            text = " | ".join(chunks)
        else:
            text = repr(content)
        if len(text) > snippet_chars:
            text = text[: snippet_chars - 1] + "…"
        out.append({"role": role, "content": text})
    return out


def _try_format_batch_proposal(result: dict, context: AgentContext) -> str | None:
    """Reconstruct a BatchScheduleProposal from a tool result dict and format it."""
    try:
        fields = {k: v for k, v in result.items() if k != "status"}
        batch = BatchScheduleProposal.model_validate(fields)
        return format_batch_proposal(batch, user_timezone=context.user_timezone)
    except Exception:
        logger.warning("batch_proposal_format_failed")
        return None


def _try_format_proposal(result: dict, context: AgentContext) -> str | None:
    """Reconstruct a ScheduleProposal from a tool result dict and format it.

    Returns the formatted MarkdownV2 proposal string, or None if reconstruction
    fails (in which case the caller falls through to a regular LLM response).
    """
    try:
        fields = {k: v for k, v in result.items() if k != "status"}
        proposal = ScheduleProposal(**fields)
        return format_proposal(
            proposal,
            current_debt=context.time_debt.total_hours,
            max_debt=context.time_debt.max_debt_limit,
            user_timezone=context.user_timezone,
        )
    except Exception:
        logger.warning("proposal_format_failed")
        return None


async def run(user_id: UUID, message: str) -> str:
    """Run the cognitive loop for a single user message.

    Assembles full context, calls the LLM with conversation history and tool
    definitions, handles any tool-use turns (up to MAX_TOOL_ITERATIONS), persists
    the original user message and final assistant response, and returns the
    formatted response text. Never raises — returns a graceful fallback on any failure.
    """
    try:
        async with async_session_factory() as session:
            user = await user_crud.get(session, user_id)

            context = await assemble_context(session, user_id)
            context_str = to_prompt_string(context)
            system_prompt = _PROMPT_PATH.read_text(encoding="utf-8").replace("{context}", context_str)

            messages: list[dict] = [
                {"role": m.role, "content": m.content}
                for m in context.recent_messages
            ]
            messages.append({"role": "user", "content": message})

            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

            response: anthropic.types.Message | None = None

            for iteration in range(MAX_TOOL_ITERATIONS):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "cognitive_loop_api_call user_id=%s iteration=%d messages=%s",
                        user_id,
                        iteration,
                        json.dumps(_safe_dump_messages(messages), default=str),
                    )

                response = await client.messages.create(
                    model=settings.cognitive_model,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                )

                if response.stop_reason != "tool_use":
                    break

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                tool_results = []
                for block in tool_use_blocks:
                    result = await execute_tool(block.name, block.input, session, user)

                    if isinstance(result, dict):
                        status = result.get("status")
                        if status == "batch_proposal_pending_confirmation":
                            proposal_text = _try_format_batch_proposal(result, context)
                        elif status == "proposal_pending_confirmation":
                            proposal_text = _try_format_proposal(result, context)
                        else:
                            proposal_text = None
                        if proposal_text is not None:
                            await message_service.save_message(session, user_id, "user", message)
                            await message_service.save_message(
                                session, user_id, "assistant", proposal_text
                            )
                            await session.commit()
                            return proposal_text

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            if response is None:
                return _ERROR_FALLBACK

            assistant_text = _extract_response_text(response)

            # Save raw text to DB so conversation history stays human-readable
            await message_service.save_message(session, user_id, "user", message)
            await message_service.save_message(session, user_id, "assistant", assistant_text)
            await session.commit()

            return format_for_telegram(assistant_text)

    except Exception:
        logger.exception("cognitive_loop_failed user_id=%s", user_id)
        return _ERROR_FALLBACK
