"""Formats agent output for Telegram MarkdownV2 delivery."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.context_assembler import TaskSummary, TimeDebtSummary
from app.services.schedule_service import BatchScheduleProposal, ScheduleProposal

_MAX_LEN = 4096

# MarkdownV2 special chars: \ _ * [ ] ( ) ~ ` > # + - = | { } . !
_MDV2_ESCAPE_RE = re.compile(r"([\\*_\[\]()~`>#+\-=|{}.!])")

# Matches a *bold span* produced by our conversion pipeline (no nested * or newlines)
_BOLD_SPAN_RE = re.compile(r"\*([^*\n]+)\*")


def _escape_mdv2(text: str) -> str:
    """Escape all MarkdownV2 special characters in a plain text segment."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


# Pre-escaped fallback messages computed once at import time
_FALLBACK = _escape_mdv2("Something went wrong — please try again.")
_ERROR_FALLBACK = _escape_mdv2("I'm having trouble thinking right now. Try again in a moment.")


def _escape_with_bold_preserved(text: str) -> str:
    """Escape for MarkdownV2 while preserving *bold* spans inserted by the pipeline.

    Plain segments (outside *...*) have all special chars escaped including *.
    Bold span inner text is escaped but the outer * markers are kept intact.
    """
    parts: list[str] = []
    last = 0
    for m in _BOLD_SPAN_RE.finditer(text):
        parts.append(_escape_mdv2(text[last : m.start()]))
        parts.append("*" + _escape_mdv2(m.group(1)) + "*")
        last = m.end()
    parts.append(_escape_mdv2(text[last:]))
    return "".join(parts)


def _strip_artifacts(text: str) -> str:
    """Remove XML tags and obvious JSON structural artifacts leaked from LLM output."""
    # Remove XML/HTML-like tag pairs with their content (e.g. <think>…</think>)
    text = re.sub(
        r"<[a-zA-Z][a-zA-Z0-9_]*>.*?</[a-zA-Z][a-zA-Z0-9_]*>", "", text, flags=re.DOTALL
    )
    # Remove remaining open/close/self-closing tags
    text = re.sub(r"<[^>]{1,100}>", "", text)
    # Remove lines that are pure JSON structural characters
    text = re.sub(r"^\s*[{}\[\],]\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def format_for_telegram(response: str) -> str:
    """Format an LLM response string for Telegram MarkdownV2 delivery.

    Pipeline:
    1. Strip XML tags and JSON artifacts.
    2. Convert ## / ### headers to *bold*.
    3. Convert **bold** to *bold*.
    4. Convert bullet list items (- / *) to the • character.
    5. Escape all MarkdownV2 special characters, preserving bold spans.
    6. Truncate to 4096 characters at the last sentence boundary.

    Never returns an empty string — falls back to a safe error message.
    """
    if not response or not response.strip():
        return _FALLBACK

    text = response

    # 1. Strip artifacts
    text = _strip_artifacts(text)

    # 2. ## / ### headers → *bold*
    text = re.sub(r"^#{2,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 3. **bold** → *bold*
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)

    # 4. Bullet lines → •
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # 5. Escape for MarkdownV2, preserving *bold* spans
    text = _escape_with_bold_preserved(text)

    # 6. Truncate on the final escaped string
    if len(text) > _MAX_LEN:
        # Leave room for the ellipsis character (1 char)
        truncated = text[: _MAX_LEN - 1]
        # In MarkdownV2, '.' is '\.' — look for that 2-char sequence as a boundary
        last_break = max(
            truncated.rfind("\\."),
            truncated.rfind("\\!"),
            truncated.rfind("\\?"),
        )
        if last_break > _MAX_LEN // 2:
            text = truncated[: last_break + 2] + "…"
        else:
            text = truncated + "…"

    if not text.strip():
        return _FALLBACK

    return text


def _localize_dt(dt: datetime, tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    if dt.tzinfo is None:
        # Naive datetimes are wall-clock in the user's timezone, not UTC
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def format_proposal(
    proposal: ScheduleProposal,
    current_debt: float = 0.0,
    max_debt: float = 0.0,
    user_timezone: str = "UTC",
) -> str:
    """Format a ScheduleProposal as a Telegram MarkdownV2 confirmation message.

    The ``current_debt`` and ``max_debt`` parameters supplement the proposal with
    the user's current debt context, which is not stored on ScheduleProposal itself.
    Times are shown in the user's local timezone.
    """
    local_start = _localize_dt(proposal.proposed_start, user_timezone)
    date_str = local_start.strftime("%a %b %-d")
    time_str = local_start.strftime("%-I:%M %p")
    new_total = round(current_debt + proposal.debt_delta, 2)

    title_esc = _escape_mdv2(proposal.title)
    date_esc = _escape_mdv2(date_str)
    time_esc = _escape_mdv2(time_str)
    delta_esc = _escape_mdv2(str(proposal.debt_delta))
    total_esc = _escape_mdv2(str(new_total))
    max_esc = _escape_mdv2(str(max_debt))

    return (
        f"📅 '{title_esc}' → {date_esc} at {time_esc} "
        f"\\({proposal.duration_mins}min\\)\n"
        f"⏱ Time debt: \\+{delta_esc}h "
        f"\\({total_esc}h / {max_esc}h\\)\n"
        f"Confirm? Reply yes or no\\."
    )


def format_batch_proposal(
    batch: BatchScheduleProposal,
    user_timezone: str = "UTC",
) -> str:
    """Format a BatchScheduleProposal as a Telegram MarkdownV2 confirmation message.

    Shows the task name, schedule pattern, session count, and first session date.
    """
    first = min(batch.sessions, key=lambda s: s.proposed_start)
    local_first = _localize_dt(first.proposed_start, user_timezone)
    first_str = local_first.strftime("%a %b %-d at %-I:%M %p")

    title_esc = _escape_mdv2(batch.title)
    days_esc = _escape_mdv2(batch.days_label)
    time_esc = _escape_mdv2(batch.time_label)
    dur_esc = _escape_mdv2(str(batch.duration_mins))
    sessions_esc = _escape_mdv2(str(batch.total_sessions))
    weeks_esc = _escape_mdv2(str(batch.weeks))
    first_esc = _escape_mdv2(first_str)

    return (
        f"📅 *{title_esc}* every {days_esc} for {weeks_esc} weeks:\n"
        f"   {time_esc}, {dur_esc} min each\n"
        f"   {sessions_esc} sessions — first up {first_esc}\n"
        f"Confirm? Reply yes or no\\."
    )


def format_task_list(tasks: list[TaskSummary]) -> str:
    """Format a list of active TaskSummary objects for Telegram MarkdownV2.

    Returns a non-empty fallback string when the task list is empty.
    """
    if not tasks:
        return "You have no active tasks right now\\."

    lines = ["Your active tasks:"]
    for t in tasks:
        deadline = (
            _escape_mdv2(t.deadline_at.strftime("%b %-d")) if t.deadline_at else "no deadline"
        )
        title_esc = _escape_mdv2(t.title)
        status_esc = _escape_mdv2(t.status)
        lines.append(f"• {title_esc} — due {deadline} \\({status_esc}\\)")

    return "\n".join(lines)


def format_debt_summary(debt: TimeDebtSummary) -> str:
    """Format a TimeDebtSummary as a Telegram MarkdownV2 status message.

    ``debt.percentage`` is a ratio (1.0 = 100%).  Status messages are calibrated
    to the four thresholds defined in the agent prompt.
    """
    pct = round(debt.percentage * 100, 1)
    total_esc = _escape_mdv2(str(round(debt.total_hours, 1)))
    max_esc = _escape_mdv2(str(round(debt.max_debt_limit, 1)))
    pct_esc = _escape_mdv2(str(pct))

    if debt.percentage > 1.0:
        status_msg = "You're over your limit\\. No more pushes until you catch up\\."
    elif debt.percentage >= 0.75:
        status_msg = "Getting serious — try not to push more tasks\\."
    elif debt.percentage >= 0.5:
        status_msg = "Starting to build up — keep an eye on this\\."
    else:
        status_msg = "You're in good shape\\."

    return f"⚡ Time debt: {total_esc}h of {max_esc}h \\({pct_esc}%\\)\n{status_msg}"
