"""Unit tests for agent/response_formatter.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.agent.context_assembler import TaskSummary, TimeDebtSummary
from app.agent.response_formatter import (
    _FALLBACK,
    format_debt_summary,
    format_for_telegram,
    format_proposal,
    format_task_list,
)
from app.services.schedule_service import ScheduleProposal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 24, 14, 0, tzinfo=timezone.utc)
_END = datetime(2026, 5, 24, 14, 45, tzinfo=timezone.utc)


def _make_proposal(
    title: str = "Write report",
    duration_mins: int = 60,
    debt_delta: float = 0.0,
    start: datetime = _NOW,
    end: datetime = _END,
) -> ScheduleProposal:
    return ScheduleProposal(
        action="add_task",
        title=title,
        duration_mins=duration_mins,
        proposed_start=start,
        proposed_end=end,
        debt_delta=debt_delta,
    )


def _make_debt(total: float, limit: float) -> TimeDebtSummary:
    pct = (total / limit) if limit > 0 else 0.0
    return TimeDebtSummary(total_hours=total, max_debt_limit=limit, percentage=pct)


def _make_task(
    title: str = "Task A",
    status: str = "pending",
    deadline: datetime | None = None,
) -> TaskSummary:
    return TaskSummary(
        id=uuid.uuid4(),
        title=title,
        duration_mins=30,
        deadline_at=deadline,
        status=status,
        is_fixed=False,
        requires_proof=False,
    )


# ---------------------------------------------------------------------------
# format_for_telegram
# ---------------------------------------------------------------------------


def test_truncates_at_4096_on_sentence_boundary() -> None:
    """A long response is truncated at the last sentence boundary before 4096 chars."""
    # Build a string with clearly placed sentence boundaries
    sentence = "This is a sentence. "
    long_text = sentence * 300  # well over 4096 chars
    result = format_for_telegram(long_text)
    assert len(result) <= 4096
    assert result.endswith("…")


def test_no_truncation_under_limit() -> None:
    """A short response is returned without truncation."""
    text = "Stay focused"
    result = format_for_telegram(text)
    assert "…" not in result
    assert len(result) <= 4096


def test_header_converted_to_bold() -> None:
    """## and ### headers are converted to MarkdownV2 *bold*."""
    result = format_for_telegram("## My Header")
    assert "*My Header*" in result

    result2 = format_for_telegram("### Sub Header")
    assert "*Sub Header*" in result2


def test_bold_converted() -> None:
    """**bold** markdown is converted to MarkdownV2 *bold*."""
    result = format_for_telegram("This is **important** text")
    assert "*important*" in result
    # Original ** markers should not appear (they were stripped)
    assert "**important**" not in result


def test_bullet_converted() -> None:
    """Bullet list items are converted to the • character."""
    result = format_for_telegram("- Task one\n- Task two")
    assert "• Task one" in result
    assert "• Task two" in result
    # Original dash should not remain as a list marker
    assert "- Task" not in result


def test_xml_stripped() -> None:
    """XML-like tags and their content are removed from the output."""
    result = format_for_telegram("<think>internal reasoning</think>Real answer here")
    assert "<think>" not in result
    assert "internal reasoning" not in result
    assert "Real answer" in result


def test_empty_input_returns_fallback() -> None:
    """An empty or whitespace-only input returns the fallback message, not an empty string."""
    for bad_input in ("", "   ", "\n\t"):
        result = format_for_telegram(bad_input)
        assert result
        assert result == _FALLBACK


def test_special_chars_escaped() -> None:
    """MarkdownV2 special characters in plain text are escaped with backslashes."""
    result = format_for_telegram("Use a.b.c and (parens) here!")
    assert "a\\.b\\.c" in result
    assert "\\(parens\\)" in result
    assert "\\!" in result


def test_bold_inner_text_escaped_but_markers_intact() -> None:
    """Bold span inner text has special chars escaped; the outer * markers are not escaped."""
    result = format_for_telegram("**Bold.text**")
    # Should produce *Bold\.text* — markers intact, dot escaped
    assert result == "*Bold\\.text*"


# ---------------------------------------------------------------------------
# format_proposal
# ---------------------------------------------------------------------------


def test_format_proposal_all_fields() -> None:
    """format_proposal renders the expected fields in the output string."""
    proposal = _make_proposal(title="Finish slides", duration_mins=45, debt_delta=0.75)
    result = format_proposal(proposal, current_debt=2.0, max_debt=8.0)

    assert "Finish slides" in result
    assert "45" in result
    assert "Confirm?" in result
    # Debt delta and totals appear somewhere in the output
    assert "0" in result  # 0.75h rendered
    assert "8" in result  # max debt


def test_format_proposal_confirm_line_present() -> None:
    """The confirmation prompt is always present in a proposal."""
    result = format_proposal(_make_proposal())
    assert "Confirm?" in result
    assert "yes or no" in result


def test_format_proposal_debt_calculation() -> None:
    """Total debt shown is current_debt + proposal.debt_delta."""
    proposal = _make_proposal(debt_delta=1.5)
    result = format_proposal(proposal, current_debt=3.0, max_debt=8.0)
    # new_total = 3.0 + 1.5 = 4.5; the dot is MarkdownV2-escaped to \.
    assert r"4\.5" in result


def test_format_proposal_title_special_chars_escaped() -> None:
    """Task titles containing MarkdownV2 special chars are escaped in the output."""
    proposal = _make_proposal(title="Fix bug #42 (urgent)")
    result = format_proposal(proposal)
    assert "\\#42" in result
    assert "\\(urgent\\)" in result


# ---------------------------------------------------------------------------
# format_task_list
# ---------------------------------------------------------------------------


def test_format_task_list_empty() -> None:
    """An empty task list returns a non-empty 'no tasks' message."""
    result = format_task_list([])
    assert result
    assert "no active tasks" in result.lower()


def test_format_task_list_populated() -> None:
    """Each task's title and status appear in the formatted list."""
    tasks = [
        _make_task("Write report", "pending"),
        _make_task("Review PR", "pushed"),
    ]
    result = format_task_list(tasks)
    assert "Write report" in result
    assert "Review PR" in result
    assert "pending" in result
    assert "pushed" in result
    assert result.startswith("Your active tasks:")


def test_format_task_list_with_deadline() -> None:
    """Tasks with deadlines show the formatted deadline date."""
    deadline = datetime(2026, 5, 30, tzinfo=timezone.utc)
    tasks = [_make_task("Submit form", deadline=deadline)]
    result = format_task_list(tasks)
    assert "May" in result
    assert "30" in result


def test_format_task_list_no_deadline() -> None:
    """Tasks without a deadline show 'no deadline' in the output."""
    tasks = [_make_task("Open-ended task", deadline=None)]
    result = format_task_list(tasks)
    assert "no deadline" in result


# ---------------------------------------------------------------------------
# format_debt_summary
# ---------------------------------------------------------------------------


def test_format_debt_summary_under_50_pct() -> None:
    """Below 50% debt shows the 'in good shape' message."""
    debt = _make_debt(total=2.0, limit=8.0)  # 25%
    result = format_debt_summary(debt)
    assert "in good shape" in result.lower()


def test_format_debt_summary_50_to_75_pct() -> None:
    """50–75% debt shows the 'starting to build up' message."""
    debt = _make_debt(total=5.0, limit=8.0)  # 62.5%
    result = format_debt_summary(debt)
    assert "starting to build up" in result.lower()


def test_format_debt_summary_75_to_100_pct() -> None:
    """75–100% debt shows the 'getting serious' message."""
    debt = _make_debt(total=7.0, limit=8.0)  # 87.5%
    result = format_debt_summary(debt)
    assert "getting serious" in result.lower()


def test_format_debt_summary_over_100_pct() -> None:
    """Over 100% debt shows the 'over your limit' message."""
    debt = _make_debt(total=9.0, limit=8.0)  # 112.5%
    result = format_debt_summary(debt)
    assert "over your limit" in result.lower()


def test_format_debt_summary_shows_totals() -> None:
    """The formatted output contains the numeric total and max values."""
    debt = _make_debt(total=3.0, limit=8.0)
    result = format_debt_summary(debt)
    assert "3" in result
    assert "8" in result


def test_format_debt_summary_zero_limit() -> None:
    """A zero debt limit does not raise and returns a valid string."""
    debt = TimeDebtSummary(total_hours=0.0, max_debt_limit=0.0, percentage=0.0)
    result = format_debt_summary(debt)
    assert isinstance(result, str)
    assert result


def test_format_debt_summary_exactly_100_pct() -> None:
    """Exactly 100% debt falls in the 'getting serious' bucket (≤ 100%, not > 100%)."""
    debt = _make_debt(total=8.0, limit=8.0)  # 100%
    result = format_debt_summary(debt)
    assert "getting serious" in result.lower()
