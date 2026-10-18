"""An API agent's structured question and its options (#1463).

The options are the agent's words, so they are DISPLAY TEXT: cleaned of control bytes, capped,
and only ever returned to the agent that wrote them. Buttons are built from these fields alone,
never parsed from prose: an agent that writes a list in its reply gets no buttons for it.

Codex's async question (``agentMessage`` with ``delivery: "async"``) is observation only. Codex
is not waiting: the operator's next ordinary message is the answer, so nothing here writes.
"""

from __future__ import annotations

import re
from typing import Any

MAX_QUESTIONS = 8
MAX_OPTIONS = 12
TITLE_MAX = 1000
OPTION_MAX = 200

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class QuestionError(ValueError):
    """A question set that does not have the reviewed shape."""


def _clean(value: Any, cap: int) -> str:
    if not isinstance(value, str):
        return ""
    # Cap BEFORE trimming, so a cut that lands on a space still yields a value this function
    # maps to itself: the IPC/journal boundary re-cleans and compares (Hermes #6464).
    return _CTRL.sub("", value)[:cap].strip()


def async_questions(value: Any) -> list[dict] | None:
    """Codex's ``questions`` (``[{title, options: [str]}]``) as display text, else ``None``.

    A malformed set is dropped whole, never in part: a missing question would make the operator
    answer a different set than the agent asked. Empty and repeated options are skipped.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        return None
    out = []
    for question in value:
        if not isinstance(question, dict):
            return None
        title = _clean(question.get("title"), TITLE_MAX)
        raw = question.get("options")
        if not title or not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
            return None
        options: list[str] = []
        for option in raw:
            label = _clean(option, OPTION_MAX)
            if label and label not in options:
                options.append(label)
        if not options:
            return None
        out.append({"title": title, "options": options})
    return out


def validate_async(value: Any) -> list[dict]:
    """The journal/IPC boundary: exactly what ``async_questions`` produces, or refused."""
    if async_questions(value) != value:
        raise QuestionError("invalid native questions")
    return value
