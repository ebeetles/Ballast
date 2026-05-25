"""Intent classification only."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "intent_router.txt"
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


class Intent(str, Enum):
    push_task = "push_task"
    complete_task = "complete_task"
    add_task = "add_task"
    delete_task = "delete_task"
    general_chat = "general_chat"
    unknown = "unknown"


class IntentResult(BaseModel):
    intent: Intent
    confidence: float
    extracted_params: dict[str, Any]


_FALLBACK = IntentResult(intent=Intent.general_chat, confidence=0.0, extracted_params={})


def _extract_response_text(response: anthropic.types.Message) -> str:
    """Collect assistant text blocks (skips thinking/tool blocks)."""
    parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse JSON from model output, tolerating markdown code fences."""
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


async def classify_intent(message: str, user_context: dict[str, Any]) -> IntentResult:
    """Classify the intent of a raw message. Never raises to the caller."""
    try:
        system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=settings.router_model,
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        raw = _extract_response_text(response)
        data = _parse_llm_json(raw)
        return IntentResult(**data)
    except Exception:
        logger.exception("intent_classification_failed message=%r", message)
        return _FALLBACK
