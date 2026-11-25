"""Read an engine's own numbered menu off a session's screen — or nothing (#1060).

`orchestrator._prompt_class` is deliberately coarse: it only says a screen *might* be a choice,
from substrings like ``1.`` anywhere in the tail. That is fine for a precondition and useless as a
source of buttons, because a numbered list is not evidence of a menu. Agents print numbered lists
constantly — in prose, in plans, in logs, and on purpose.

So this module is **stricter than the classifier, per engine, and empty on doubt**. It recognises
only a menu matching a rendering captured from the engine's real TUI (see the fixtures under
``tests/fixtures/*_select_menu.*``), anchored on that engine's own chrome at the **bottom** of the
live screen, and returns ``None`` for anything else. ``None`` means no buttons, which is exactly the
behaviour before this module existed — so a wrong "no" costs a tap in the terminal,
and a wrong "yes" is what every rule below is there to prevent.

What it returns is **display text only**. The option labels are the agent's words: they are cleaned
of control bytes, capped, and shown to the operator as text. They never reach a PTY — what a tap
sends is the option NUMBER, rendered server-side by ``actuator.render`` and bounds-checked there.

An engine with no captured rendering parses to ``None``. Adding one is a new entry in
``_PARSERS`` plus a real fixture, never a looser pattern here.
"""

from __future__ import annotations

import re
from typing import TypedDict

#: The option numbers a menu may carry. Mirrors ``orchestrator.OPTION_MIN/MAX``, which bounds the
#: digit ``choose`` delivers; a menu numbered outside it could not be answered anyway.
OPTION_MIN, OPTION_MAX = 1, 20
#: Caps on display text. The question is one line on a card; a label is one button.
QUESTION_MAX = 400
LABEL_MAX = 160

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class MenuOption(TypedDict):
    n: int
    label: str
    selected: bool


class Menu(TypedDict):
    engine: str
    question: str
    options: list[MenuOption]


def _clean(text: str, cap: int) -> str:
    return " ".join(_CTRL.sub("", text).split())[:cap]


# ---- claude ------------------------------------------------------------------------------------
#
# The `AskUserQuestion` select list, as `vtscreen.render` draws it (tests/fixtures/
# claude_select_menu.screen.txt):
#
#      ☐ Release scope                       <- title: exactly one ☐, nothing else tab-like
#                                            <- blank
#     │ Is that hold lifted now?             <- the question, one or more lines — drawn WITH a │
#                                               gutter in one capture and WITHOUT in another, same
#                                               version (claude_select_menu_v2_1_280.screen.txt)
#                                            <- blank
#     ❯ 1. Merge the two ready branches      <- an option: indent 0 with ❯, or indent 2
#          Merge both reviewed branches …    <- its description: indent 5, may wrap
#       2. Keep the hold
#       …
#       5. Type something.                   <- opens a free-text box: NOT an answer
#     ───────────────────────────────…       <- separator
#       6. Chat about this                   <- opens a conversation: NOT an answer
#                                            <- blank
#     Enter to select · ↑/↓ to navigate · Esc to cancel   <- the anchor, last line of the screen

_CLAUDE_FOOTER = "Enter to select · ↑/↓ to navigate · Esc to cancel"
# An option line: `❯ N. label` at column 0, or `  N. label` at column 2 — never deeper. A
# description that happens to start "2." sits at column 5 and is not an option.
_CLAUDE_OPTION = re.compile(r"^(?:(?P<sel>❯) |  )(?P<n>\d{1,2})\. (?P<label>\S.*)$")
_CLAUDE_DESCRIPTION = re.compile(r"^ {5}\S")
_CLAUDE_SEPARATOR = re.compile(r"^─{8,}$")
_CLAUDE_NON_ANSWERS = frozenset({"Type something.", "Chat about this"})
# A PERMISSION DIALOG IS NEVER A MENU (#1060 issue review). claude's tool-permission prompt ("Do you
# want to proceed? ❯ 1. Yes / 2. Yes, and don't ask again / 3. No") is a numbered list too, and a
# one-tap "don't ask again" is exactly the button this module must never produce: it widens what the
# agent may do without the operator having decided it here. Sessions BattleLab launches run with
# permissions bypassed, and so does a mission's own dispatch unless the operator's bypass default is
# off (#1215) — then it does not, and the prompt is reachable. No real
# capture exists on the host for that reason, so this is refused by its TEXT, anywhere between the
# title and the footer, whatever chrome it is drawn in. The OPERATOR can answer one (#1213), through
# `permission_prompts` — a separate reader no autonomous path imports — never through this module.
_CLAUDE_PERMISSION_MARKS = ("do you want to proceed", "don't ask again", "don’t ask again")
# A multi-question prompt draws its questions as tabs on the title line (☐ … ☐ … ✔ Submit). What a
# digit does there is not established by any capture, so it is refused rather than guessed.
_TAB_MARKS = ("☐", "☒", "✔", "✓")


def _claude(lines: list[str]) -> Menu | None:
    if not lines or lines[-1].strip() != _CLAUDE_FOOTER:
        return None
    # The title: the nearest line above the footer starting with ☐, with exactly one tab mark.
    title_at = None
    for i in range(len(lines) - 2, -1, -1):
        if lines[i].lstrip().startswith("☐"):
            title_at = i
            break
    if title_at is None:
        return None
    if sum(lines[title_at].count(m) for m in _TAB_MARKS) != 1:
        return None

    region = "\n".join(lines[title_at:-1]).lower()
    if any(mark in region for mark in _CLAUDE_PERMISSION_MARKS):
        return None

    question: list[str] = []
    options: list[MenuOption] = []
    seen_option = False
    for line in lines[title_at + 1 : -1]:
        if not line.strip():
            continue
        m = _CLAUDE_OPTION.match(line)
        if not seen_option and not m:
            # Everything between the title and the first option is the question, with or
            # without the `│` gutter. The option regex runs first, so a numbered line is never
            # question text.
            question.append(line.removeprefix("│").strip())
            continue
        if m:
            seen_option = True
            options.append(
                {"n": int(m["n"]), "label": m["label"].strip(), "selected": m["sel"] is not None}
            )
            continue
        if seen_option and (_CLAUDE_DESCRIPTION.match(line) or _CLAUDE_SEPARATOR.match(line)):
            continue
        # Anything else between the title and the footer is not this rendering.
        return None

    if not question or not options:
        return None
    # The numbering must be the menu's own: 1..k, each once, no gaps. A disagreement means the parse
    # and the digit it would send could disagree, and that is the one thing this cannot risk.
    numbers = [o["n"] for o in options]
    if numbers != list(range(1, len(numbers) + 1)) or numbers[-1] > OPTION_MAX:
        return None
    if sum(o["selected"] for o in options) != 1:
        return None
    answers = [
        {"n": o["n"], "label": _clean(o["label"], LABEL_MAX), "selected": o["selected"]}
        for o in options
        if o["label"].strip() not in _CLAUDE_NON_ANSWERS
    ]
    answers = [o for o in answers if o["label"]]
    if not answers:
        return None
    text = _clean(" ".join(question), QUESTION_MAX)
    if not text:
        return None
    return {"engine": "", "question": text, "options": answers}  # type: ignore[typeddict-item]


#: Menu parsers, by the manifest's `terminal.menu` KIND (#853 P3).
_PARSERS = {"claude-numbered": _claude}


def _parser_for(engine: str):
    from . import engines

    term = engines.terminal_of(engine)
    return _PARSERS.get(term.menu) if term is not None else None


def parse(screen: str, engine: str) -> Menu | None:
    """The engine's own numbered menu at the bottom of ``screen``, or ``None``.

    ``screen`` is the rendered frame ``scrollback.live_tail_text`` returns. Trailing blank lines are
    ignored; nothing else is forgiven. An engine with no captured rendering always yields ``None``.
    """
    fn = _parser_for(engine)
    if fn is None or not isinstance(screen, str) or not screen.strip():
        return None
    lines = [line.rstrip() for line in screen.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    menu = fn(lines)
    if menu is not None:
        menu["engine"] = engine  # the parser is a kind; which engine it read for is the caller's
    return menu


def recognises(screen: str) -> bool:
    """Does ANY engine's captured menu rendering match the bottom of ``screen``?

    Engine-agnostic on purpose: `orchestrator._prompt_class` is called without an engine, and every
    parser anchors on its own engine's chrome, so asking all of them cannot turn one engine's output
    into another's menu.
    """
    from . import engines

    return any(
        parse(screen, eid) is not None
        for eid in engines.ids_where(lambda m: m.runtime == "pty" and m.terminal.menu != "none")
    )


def engine_of(session_key: str) -> str:
    """``claude:<uuid>`` → ``claude``. Empty for anything without a prefix."""
    head, sep, _ = (session_key or "").partition(":")
    return head if sep else ""


__all__ = ["LABEL_MAX", "Menu", "MenuOption", "QUESTION_MAX", "engine_of", "parse", "recognises"]
