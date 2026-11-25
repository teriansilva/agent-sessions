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


# ---- Claude `AskUserQuestion` (a held callback the operator answers) --------------------------
#
# Claude Code sends it as `can_use_tool` with `requires_user_interaction: true`, in every permission
# mode, and takes the answer as `allow` with the original input plus `answers: {<question text>:
# <label>[, <label>…] | <own words>}` (captured, tests/fixtures/claude_ask_user_question.*). The
# map is keyed by the question's OWN text, so it is used exactly as sent: a question set whose texts
# would need cleaning, or that repeats one, is not offered as choices at all.

ASK_TOOL = "AskUserQuestion"
CLAUDE_MAX_QUESTIONS = 4
OTHER_MAX = 2000
#: Bounds on a header and an option's description for an EXACT answer (Hermes #6477). The card
#: shows text whole; a longer question is still answerable, as a message (#1463 follow-up).
HEADER_MAX = 64
DESCRIPTION_MAX = 500


def _shown_length(text: str) -> int:
    """Length as the card counts it: JavaScript slices UTF-16 units, so a character outside the
    BMP (an emoji) is two of them, not one code point (independent review of #1487)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


#: Claude Code's own picker joins a multi-select answer the same way.
JOIN = ", "


def _exact(value: Any, cap: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == _clean(value, cap)


def claude_questions(tool_input: Any) -> list[dict] | None:
    """The questions of an ``AskUserQuestion`` input, exactly as sent, or ``None`` when they can't
    be answered safely as choices (malformed, oversized, or repeated questions or labels)."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not 1 <= len(questions) <= CLAUDE_MAX_QUESTIONS:
        return None
    seen: set[str] = set()
    for q in questions:
        if not isinstance(q, dict) or not _exact(q.get("question"), TITLE_MAX):
            return None
        if q["question"] in seen:
            return None
        seen.add(q["question"])
        # Header and description are display text only (cleaned where shown), never a key, and
        # shown whole: nothing the operator answers may sit past the card's cut.
        header = q.get("header", "")
        if not isinstance(header, str) or _shown_length(header) > HEADER_MAX:
            return None
        if not isinstance(q.get("multiSelect", False), bool):
            return None
        options = q.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
            return None
        labels: set[str] = set()
        for o in options:
            if not isinstance(o, dict) or not _exact(o.get("label"), OPTION_MAX):
                return None
            description = o.get("description", "")
            if not isinstance(description, str) or _shown_length(description) > DESCRIPTION_MAX:
                return None
            if o["label"] in labels:
                return None
            labels.add(o["label"])
    return questions


def answer_shape(value: Any) -> list[dict]:
    """The decision's ``answers``, by shape alone: one ``{"selected": [label, …]}`` or
    ``{"other": text}`` per question. Which labels are offered is checked against the request."""
    if not isinstance(value, list) or not 1 <= len(value) <= CLAUDE_MAX_QUESTIONS:
        raise QuestionError("answers must give one answer per question")
    for entry in value:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise QuestionError("each answer is either selected options or the operator's words")
        if "selected" in entry:
            selected = entry["selected"]
            if (
                not isinstance(selected, list)
                or not 1 <= len(selected) <= MAX_OPTIONS
                or any(not isinstance(s, str) or len(s) > OPTION_MAX for s in selected)
                or len(set(selected)) != len(selected)
            ):
                raise QuestionError("selected must list distinct options")
        elif "other" in entry:
            other = entry["other"]
            if not isinstance(other, str) or not other.strip() or len(other) > OTHER_MAX:
                raise QuestionError("an own answer must be 1-2000 characters")
            if _CTRL.sub("", other) != other:
                raise QuestionError("an own answer must not carry control characters")
        else:
            raise QuestionError("each answer is either selected options or the operator's words")
    return value


def claude_answers(tool_input: Any, answers: Any) -> dict[str, str]:
    """Claude's ``answers`` map for this request, or :class:`QuestionError`. Labels must be the
    request's own, exactly one unless the question is multi-select."""
    questions = claude_questions(tool_input)
    if questions is None:
        raise QuestionError("this question can't be answered as choices")
    answer_shape(answers)
    if len(answers) != len(questions):
        raise QuestionError("answers must give one answer per question")
    out: dict[str, str] = {}
    for q, a in zip(questions, answers, strict=True):
        if "other" in a:
            out[q["question"]] = a["other"]
            continue
        offered = [o["label"] for o in q["options"]]
        if any(label not in offered for label in a["selected"]):
            raise QuestionError("that option was not offered")
        if len(a["selected"]) != 1 and not q.get("multiSelect", False):
            raise QuestionError("that question takes one option")
        # Offered order, so the same choices always make the same answer.
        out[q["question"]] = JOIN.join(label for label in offered if label in a["selected"])
    return out
