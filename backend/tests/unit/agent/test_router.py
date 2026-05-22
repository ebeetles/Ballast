"""Tests for agent/router.py — all LLM calls are mocked."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.router import Intent, IntentResult, classify_intent


def _make_response(payload: dict | str) -> MagicMock:
    """Build a fake anthropic Messages response containing JSON text."""
    text = json.dumps(payload) if isinstance(payload, dict) else payload
    content_block = MagicMock()
    content_block.type = "text"
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


def _mock_client(response: MagicMock) -> MagicMock:
    """Return a patched AsyncAnthropic whose messages.create returns response."""
    messages_mock = MagicMock()
    messages_mock.create = AsyncMock(return_value=response)
    client_instance = MagicMock()
    client_instance.messages = messages_mock
    return client_instance


@pytest.fixture
def patch_anthropic():
    """Yield a factory; tests call it with the desired response payload."""
    with patch("app.agent.router.anthropic.AsyncAnthropic") as mock_cls:
        yield mock_cls


async def test_push_task_intent(patch_anthropic):
    payload = {"intent": "push_task", "confidence": 0.95, "extracted_params": {"task": "leetcode"}}
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("can't do leetcode tonight, push it", {})

    assert result.intent == Intent.push_task
    assert result.confidence == 0.95
    assert result.extracted_params["task"] == "leetcode"


async def test_complete_task_intent(patch_anthropic):
    payload = {
        "intent": "complete_task",
        "confidence": 0.98,
        "extracted_params": {"task": "neetcode session"},
    }
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("just finished my neetcode session", {})

    assert result.intent == Intent.complete_task
    assert result.confidence == 0.98


async def test_add_task_intent(patch_anthropic):
    payload = {
        "intent": "add_task",
        "confidence": 0.97,
        "extracted_params": {"task": "study block", "duration": "2 hours", "day": "friday"},
    }
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("add a 2 hour study block friday afternoon", {})

    assert result.intent == Intent.add_task
    assert "duration" in result.extracted_params
    assert "day" in result.extracted_params


async def test_general_chat_intent(patch_anthropic):
    payload = {"intent": "general_chat", "confidence": 0.93, "extracted_params": {}}
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("feeling burnt out, not sure I can keep this pace", {})

    assert result.intent == Intent.general_chat


async def test_unknown_intent(patch_anthropic):
    payload = {"intent": "unknown", "confidence": 0.99, "extracted_params": {}}
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("asdfgh", {})

    assert result.intent == Intent.unknown
    assert result.confidence == 0.99


async def test_malformed_llm_response(patch_anthropic):
    patch_anthropic.return_value = _mock_client(_make_response("not json at all"))

    result = await classify_intent("some message", {})

    assert result.intent == Intent.unknown
    assert result.confidence == 0.0
    assert result.extracted_params == {}


async def test_api_failure(patch_anthropic):
    client_instance = MagicMock()
    client_instance.messages.create = AsyncMock(side_effect=Exception("network error"))
    patch_anthropic.return_value = client_instance

    result = await classify_intent("push my run", {})

    assert result.intent == Intent.unknown
    assert result.confidence == 0.0
    assert result.extracted_params == {}


async def test_markdown_fenced_json_response(patch_anthropic):
    fenced = (
        '```json\n'
        '{"intent": "complete_task", "confidence": 0.98, "extracted_params": {"task": "neetcode"}}\n'
        "```"
    )
    patch_anthropic.return_value = _mock_client(_make_response(fenced))

    result = await classify_intent("Just finished neetcode", {})

    assert result.intent == Intent.complete_task
    assert result.extracted_params["task"] == "neetcode"


async def test_extracted_params_populated(patch_anthropic):
    payload = {
        "intent": "push_task",
        "confidence": 0.94,
        "extracted_params": {"task": "leetcode", "reason": "too tired"},
    }
    patch_anthropic.return_value = _mock_client(_make_response(payload))

    result = await classify_intent("can't do leetcode tonight, push it", {})

    assert result.extracted_params["task"] == "leetcode"
    assert result.extracted_params["reason"] == "too tired"
