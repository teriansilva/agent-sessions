"""Read an agent's TOOL-PERMISSION dialog off its screen — for the operator only (#1213).

`screen_menus` deliberately refuses permission dialogs (#1060): a one-tap "don't ask again" widens
what an agent may do, and no autonomous path may ever produce that tap. That boundary stands. This
module is the separate, OPERATOR-ONLY reader: its output is shown on a decision card and answered by
the operator's own `/choose`. Nothing that decides on its own — the orchestrator pass, a mission's
auto-choose — imports it; `actuator.render` refuses a permission recipe for any other origin.

Like `screen_menus` it is **stricter than any classifier, per kind, and empty on doubt**. Each kind
recognises only a rendering captured from the engine's real TUI (``tests/fixtures/*permission*``),
anchored on that dialog's own chrome at the BOTTOM of the live screen. ``None`` means no buttons —
today's text-only escalation — so a wrong "no" costs a trip to the terminal and a wrong "yes" is
what every rule below is there to prevent.

**What it returns is display text plus one number.** Labels, the tool line and the command are the
agent's words, cleaned of control bytes and capped; they never reach a PTY. The number is where the
dialog's cursor is (``selected``), which the key recipe needs. What a tap sends is built by
:func:`keys` from the kind, the option number and that cursor — never from anything a client sent.

Kinds are selected by the manifest's ``terminal.permission`` (#853: consumers ask the manifest and
never name an engine). Adding one is a new entry in ``_KINDS`` plus a real capture, never a looser
pattern here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import TypedDict

from .vtscreen import Cells

#: Display caps. The command is the thing the operator is deciding about, so it gets the most room.
TITLE_MAX = 200
DETAIL_MAX = 4000
QUESTION_MAX = 200
LABEL_MAX = 200
OPTIONS_MAX = 9

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class PermissionOption(TypedDict):
    n: int
    label: str
    selected: bool
    #: Choosing it grants more than this one call ("don't ask again", "Allow always", a confirmed
    #: pattern). Shown as a warning; never a reason to refuse — the operator decides.
    persistent: bool


class Permission(TypedDict):
    engine: str
    kind: str  # always "permission" — the card's discriminator beside a `menu`
    parser: str  # the manifest kind that read it
    heading: str  # the dialog's own heading ("Permission required", "Always allow", "Bash command")
    title: str  # the tool line ("# Shell command", 'Grep "(?i)mission"', "Create file")
    detail: str  # the command / pattern / path, newline-joined as drawn
    question: str
    options: list[PermissionOption]
    #: A hash of the dialog EXACTLY as drawn — every character of its rows, the cursor mark
    #: excluded. The display fields above are cleaned and capped for a card; this is what an answer
    #: is bound to, so two commands that display alike (collapsed spaces, a shared long prefix)
    #: can never stand in for each other (#1218 review).
    identity: str


def _identity(parser: str, rows: Sequence[str]) -> str:
    raw = parser + "\x00" + "\n".join(r.rstrip() for r in rows)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _fits(text: str, cap: int) -> bool:
    """Would the card show ALL of it? A dialog whose words would be cut is not offered buttons:
    the operator must be able to read everything they are granting."""
    return len(" ".join(_CTRL.sub("", text).split())) <= cap


def _clean(text: str, cap: int) -> str:
    return " ".join(_CTRL.sub("", text).split())[:cap]


def _clean_block(lines: Sequence[str], cap: int) -> str:
    out = [" ".join(_CTRL.sub("", ln).split()) for ln in lines]
    return "\n".join(ln for ln in out if ln)[:cap]


_PERSISTENT = re.compile(
    r"don['’]t ask again|always|accept edits|for this session|until .* restart", re.IGNORECASE
)


# ---- claude-permission ------------------------------------------------------------------------
#
# Captured from claude 2.1.284 in a private PTY (tests/fixtures/claude_permission_*.screen.txt):
#
#     ────────────────────────────────────────…   <- the dialog's top rule, full width
#      Bash command                               <- heading
#                                                 <- blank
#        mkdir -p probe-dir-1213 && touch …       <- body: the command, its description
#        Create probe-dir-1213 directory …
#                                                 <- blank
#      Do you want to proceed?                    <- the question
#      ❯ 1. Yes                                   <- options: " ❯ N." or "   N." (indent 1 / 3)
#        2. Yes, and don't ask again for mkdir …
#           in /home/…                            <- a wrapped label: indent 6
#        3. No
#                                                 <- blank
#      Esc to cancel · Tab to amend               <- the anchor: last line of the screen
#
# A Write shows ` Create file` / ` notes.txt` / a `╌╌╌` framed preview / ` Do you want to
# create …?`. The cursor is the `❯`. Keys (verified 2026-09-28): the digit alone selects AND
# submits (`1` ran the command, `3` answered No) — so the recipe is the digit, and the cursor
# does not matter.

_CL_RULE = re.compile(r"^─{8,}$")
_CL_DASHED = re.compile(r"^╌{8,}$")
_CL_OPTION = re.compile(r"^ (?:(?P<sel>❯) |  )(?P<n>\d)\. (?P<label>\S.*)$")
_CL_CONT = re.compile(r"^ {6}\S")
_CL_FOOTER = "Esc to cancel"


def _claude(lines: list[str], _cells: list[Cells] | None) -> Permission | None:
    if not lines or not lines[-1].strip().startswith(_CL_FOOTER):
        return None
    rule_at = None
    for i in range(len(lines) - 2, -1, -1):
        if _CL_RULE.match(lines[i].strip()):
            rule_at = i
            break
    if rule_at is None:
        return None
    body = lines[rule_at + 1 : -1]
    # The options: a contiguous run (with wrapped continuations) ending before the footer's blank.
    first_opt = next((i for i, ln in enumerate(body) if _CL_OPTION.match(ln)), None)
    if first_opt is None or first_opt == 0:
        return None
    question = body[first_opt - 1].strip()
    if not question.lower().startswith("do you want to"):
        return None
    options: list[dict] = []
    for ln in body[first_opt:]:
        m = _CL_OPTION.match(ln)
        if m:
            options.append(
                {"n": int(m["n"]), "label": m["label"].strip(), "selected": m["sel"] is not None}
            )
        elif _CL_CONT.match(ln) and options:
            options[-1]["label"] += " " + ln.strip()
        elif ln.strip():
            return None  # anything else after the options is not this rendering
    numbers = [o["n"] for o in options]
    if not (2 <= len(numbers) <= OPTIONS_MAX) or numbers != list(range(1, len(numbers) + 1)):
        return None
    if sum(o["selected"] for o in options) != 1:
        return None
    # Its first option is the plain yes, its last the no: the shape every capture shows. A dialog
    # whose ends read otherwise is a rendering this parser has never seen.
    if options[0]["label"] != "Yes" or not options[-1]["label"].startswith("No"):
        return None
    head = [ln for ln in body[: first_opt - 1]]
    content = [ln for ln in head if ln.strip() and not _CL_DASHED.match(ln.strip())]
    if not content:
        return None
    heading = content[0].strip()
    detail_lines = content[1:]
    if not _fits("\n".join(detail_lines), DETAIL_MAX) or not _fits(heading, TITLE_MAX):
        return None
    if not _fits(question, QUESTION_MAX) or any(not _fits(o["label"], LABEL_MAX) for o in options):
        return None
    # The identity is every row of the dialog but the footer (which changes with the cursor), with
    # the cursor mark itself blanked: the same dialog with the cursor elsewhere is the same dialog.
    rows = [
        (" " + "  " + ln[3:]) if _CL_OPTION.match(ln) and ln.startswith(" ❯ ") else ln
        for ln in lines[rule_at:-1]
    ]
    return {
        "engine": "",
        "kind": "permission",
        "parser": "claude-permission",
        "identity": _identity("claude-permission", rows),
        "heading": _clean(heading, TITLE_MAX),
        "title": _clean(heading, TITLE_MAX),
        "detail": _clean_block(detail_lines, DETAIL_MAX),
        "question": _clean(question, QUESTION_MAX),
        "options": [
            {
                "n": o["n"],
                "label": _clean(o["label"], LABEL_MAX),
                "selected": o["selected"],
                "persistent": o["n"] != 1
                and o["n"] != len(options)
                and bool(_PERSISTENT.search(o["label"])),
            }
            for o in options
        ],
    }


# ---- opencode-permission ----------------------------------------------------------------------
#
# Captured from opencode 1.18.33 (the #1213 incident's own ring bytes, and fresh captures in a
# private PTY; tests/fixtures/opencode_permission_*.raw). Under 80 columns the panel is:
#
#       ┃                                          <- the panel's left border, in the warning colour
#       ┃  △ Permission required                   <- heading ("△ Always allow" on the 2nd stage)
#       ┃    # Shell command                       <- the tool line (absent on the 2nd stage)
#       ┃
#       ┃  $ git log --oneline -15 && git log …    <- body: the command / "Pattern: …" / patterns
#       ┃
#       ┃   Allow once   Allow always   Reject     <- options (or "Confirm   Cancel")
#       ┃
#       ┃  ctrl+f fullscreen  ⇆ select  enter confirm   <- the anchor ("⇆ select  enter confirm")
#       ┃
#
# At 80 columns and wider the options and the footer share one row. THE CURSOR IS ONLY A COLOUR:
# the selected option's box is filled with the same warning colour the border `┃` is drawn in, and
# the others are not — so the parse needs the coloured frame, and without it there is no cursor and
# no answer. Keys (verified 2026-09-28): `→` moves the cursor one option right, wrapping; `\r`
# confirms. "Allow always" opens the second stage, which lists the patterns it would grant.

_OC_BORDER = "┃"
_OC_FOOTER = ("⇆ select", "enter confirm")
_OC_HEADINGS = {"△ Permission required": "Permission required", "△ Always allow": "Always allow"}
_OC_OPTION_SETS: dict[tuple[str, ...], tuple[bool, ...]] = {
    ("Allow once", "Allow always", "Reject"): (False, True, False),
    ("Confirm", "Cancel"): (True, False),
}
_OC_QUESTION = {"Permission required": "Allow this?", "Always allow": "Grant these patterns?"}


def _oc_content(line: str) -> str | None:
    """The text inside the panel on this row, or None when the row is not inside the panel."""
    stripped = line.lstrip(" ")
    if not stripped.startswith(_OC_BORDER):
        return None
    rest = stripped[len(_OC_BORDER) :]
    if rest and not rest.startswith(" "):
        return None
    return rest


def _oc_options(text: str) -> tuple[list[tuple[str, int]], tuple[bool, ...]] | None:
    """The option labels on one row with their columns inside ``text``, or None."""
    head = text
    for mark in ("ctrl+f", "⇆ select"):
        at = head.find(mark)
        if at >= 0:
            head = head[:at]
    parts = [p for p in re.split(r"\s{2,}", head.strip()) if p]
    persist = _OC_OPTION_SETS.get(tuple(parts))
    if persist is None:
        return None
    found: list[tuple[str, int]] = []
    pos = 0
    for label in parts:
        at = text.find(label, pos)
        if at < 0:
            return None
        found.append((label, at))
        pos = at + len(label)
    return found, persist


def _opencode(lines: list[str], cells: list[Cells] | None) -> Permission | None:
    # The panel runs to the bottom of the screen; its last rows may be border-only padding.
    end = len(lines) - 1
    while end >= 0 and (_oc_content(lines[end]) or "").strip() == "":
        if _oc_content(lines[end]) is None:
            return None
        end -= 1
    if end < 0:
        return None
    footer = _oc_content(lines[end]) or ""
    if not all(m in footer for m in _OC_FOOTER):
        return None
    # The options: on the footer row (wide layout) or the nearest non-blank panel row above it.
    opt_at = end
    parsed = _oc_options(footer)
    if parsed is None:
        opt_at = end - 1
        while opt_at >= 0 and (_oc_content(lines[opt_at]) or "").strip() == "":
            if _oc_content(lines[opt_at]) is None:
                return None
            opt_at -= 1
        if opt_at < 0:
            return None
        parsed = _oc_options(_oc_content(lines[opt_at]) or "")
        if parsed is None:
            return None
    found, persist = parsed
    # The heading: the nearest panel row above the options that is one of the dialog's own.
    head_at = None
    heading = ""
    for i in range(opt_at - 1, -1, -1):
        content = _oc_content(lines[i])
        if content is None:
            return None  # left the panel before finding its heading
        h = _OC_HEADINGS.get(content.strip())
        if h:
            head_at, heading = i, h
            break
    if head_at is None:
        return None
    body = [(_oc_content(lines[i]) or "") for i in range(head_at + 1, opt_at)]
    if heading == "Permission required":
        # The tool line sits directly under the heading, indented two columns deeper.
        first = next((j for j, b in enumerate(body) if b.strip()), None)
        if first is None:
            return None
        title = body[first].strip()
        detail_lines = body[first + 1 :]
    else:
        title = heading
        detail_lines = body
    labels = [lbl for lbl, _ in found]
    if heading == "Always allow" and labels != ["Confirm", "Cancel"]:
        return None
    if heading == "Permission required" and labels[0] != "Allow once":
        return None
    if not _fits("\n".join(detail_lines), DETAIL_MAX) or not _fits(title, TITLE_MAX):
        return None
    selected = _oc_cursor(lines[opt_at], found, cells, len(lines) - 1 - opt_at)
    options: list[PermissionOption] = [
        {
            "n": i + 1,
            "label": lbl,
            "selected": selected == i,
            "persistent": persist[i],
        }
        for i, lbl in enumerate(labels)
    ]
    return {
        "engine": "",
        "kind": "permission",
        "parser": "opencode-permission",
        # Every row from the heading to the footer as drawn; opencode's cursor is colour only.
        "identity": _identity("opencode-permission", lines[head_at : end + 1]),
        "heading": heading,
        "title": _clean(title, TITLE_MAX),
        "detail": _clean_block(detail_lines, DETAIL_MAX),
        "question": _OC_QUESTION[heading],
        "options": options,
    }


def _oc_cursor(
    line: str, found: list[tuple[str, int]], cells: list[Cells] | None, from_bottom: int
) -> int | None:
    """Which option the cursor is on: the ONE whose background is the border's colour. None when
    there are no colours, the row does not line up with them, or not exactly one option matches."""
    if not cells or from_bottom >= len(cells):
        return None
    row = cells[len(cells) - 1 - from_bottom]
    if row.text != line:
        return None
    border_at = row.text.find(_OC_BORDER)
    if border_at < 0 or border_at >= len(row.fg):
        return None
    ink = row.fg[border_at]
    if not ink:
        return None
    # Columns in `found` are relative to the panel content; map them onto the row.
    offset = border_at + len(_OC_BORDER)
    hits = []
    for i, (label, col) in enumerate(found):
        start = offset + col
        span = row.bg[start : start + len(label)]
        if len(span) != len(label):
            return None
        if all(b == ink for b in span):
            hits.append(i)
        elif any(b == ink for b in span):
            return None  # half-highlighted: a repaint in flight, not a state
    return hits[0] if len(hits) == 1 else None


# ---- the kinds --------------------------------------------------------------------------------

#: Parsers by the manifest's ``terminal.permission`` kind.
_KINDS = {"claude-permission": _claude, "opencode-permission": _opencode}


def _kind_for(engine: str) -> str | None:
    from . import engines

    term = engines.terminal_of(engine)
    kind = getattr(term, "permission", "none") if term is not None else "none"
    return kind if kind in _KINDS else None


def parse(screen: str, engine: str, cells: list[Cells] | None = None) -> Permission | None:
    """The engine's permission dialog at the bottom of ``screen``, or ``None``.

    ``screen`` is the frame `scrollback.live_tail_text` / `live_tail_frame` returns and ``cells``
    the coloured rows from the same read. A dialog is only returned with its cursor known —
    exactly one option ``selected`` — because the key recipe depends on it; without ``cells`` a
    kind whose cursor is a colour yields ``None``. Use :func:`recognises` to ask "is this a
    permission dialog" without needing the cursor.
    """
    p = _parse_any(screen, engine, cells)
    if p is None or sum(o["selected"] for o in p["options"]) != 1:
        return None
    return p


def _parse_any(screen: str, engine: str, cells: list[Cells] | None) -> Permission | None:
    kind = _kind_for(engine)
    if kind is None or not isinstance(screen, str) or not screen.strip():
        return None
    lines = [ln.rstrip() for ln in screen.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if cells is not None:
        cells = list(cells)
        while cells and not cells[-1].text.rstrip():
            cells.pop()
    try:
        p = _KINDS[kind](lines, cells)
    except Exception:  # noqa: BLE001 — a parser fault is "no dialog", never an error on a card
        return None
    if p is not None:
        p["engine"] = engine
    return p


def recognises(screen: str) -> bool:
    """Does ANY engine's captured permission dialog sit at the bottom of ``screen``? Cursor not
    required. Engine-agnostic like `screen_menus.recognises` — each kind anchors on its own
    chrome."""
    from . import engines

    return any(
        _parse_any(screen, eid, None) is not None
        for eid in engines.ids_where(lambda m: getattr(m.terminal, "permission", "none") != "none")
    )


def digest(p: Permission | None) -> str:
    """A stable hash of everything the operator decided on — heading, tool, detail, question, every
    option label, AND the cursor. Pinned into a `choose` precondition and re-checked inside the
    first-byte fence, so a changed command under identical labels, or a cursor moved by hand, is
    refused with nothing sent."""
    if p is None:
        return ""
    body = {
        "identity": p.get("identity"),
        "parser": p.get("parser"),
        "heading": p.get("heading"),
        "title": p.get("title"),
        "detail": p.get("detail"),
        "question": p.get("question"),
        "options": [[o.get("n"), o.get("label"), bool(o.get("selected"))] for o in p["options"]],
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def same_prompt(a: Permission | None, b: Permission | None) -> bool:
    """The same dialog, cursor aside: what the operator read is still what is showing."""
    if a is None or b is None:
        return False

    if not a.get("identity") or a.get("identity") != b.get("identity"):
        return False

    def shape(p: Permission) -> tuple:
        return (
            p.get("parser"),
            p.get("heading"),
            p.get("title"),
            p.get("detail"),
            p.get("question"),
            tuple((o.get("n"), o.get("label")) for o in p["options"]),
        )

    return shape(a) == shape(b)


def selected_of(p: Permission) -> int | None:
    """The option number the cursor is on."""
    hits = [o["n"] for o in p["options"] if o.get("selected")]
    return hits[0] if len(hits) == 1 else None


def keys(parser: str, option: int, from_selected: int, count: int) -> bytes:
    """The keystrokes that choose ``option`` in a dialog of ``count`` options whose cursor is on
    ``from_selected`` — per kind, as verified against the real CLI. Raises ``ValueError`` on
    anything out of range; never reached with client bytes."""
    for v in (option, from_selected, count):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("permission keys need whole numbers")
    if not (2 <= count <= OPTIONS_MAX) or not (1 <= option <= count):
        raise ValueError("option out of range")
    if not (1 <= from_selected <= count):
        raise ValueError("cursor out of range")
    if parser == "claude-permission":
        return str(option).encode()  # the digit alone selects and submits
    if parser == "opencode-permission":
        steps = (option - from_selected) % count
        return b"\x1b[C" * steps + b"\r"
    raise ValueError(f"no key recipe for {parser!r}")


def summary(p: Permission) -> str:
    """One line naming the prompt, for an escalation's text and the mission chat."""
    engine = p.get("engine") or "the agent"
    what = p.get("title") or p.get("heading") or "a tool"
    detail = " ".join((p.get("detail") or "").split())
    opts = " · ".join(o["label"] for o in p["options"])
    if p.get("heading") == "Always allow":
        line = (
            f"{engine} asks to confirm Always allow: {detail}"
            if detail
            else f"{engine} asks to confirm Always allow"
        )
    else:
        line = f"{engine} asks permission — {what}" + (f": {detail}" if detail else "")
    return f"{line} ({opts})"


__all__ = [
    "Permission",
    "PermissionOption",
    "digest",
    "keys",
    "parse",
    "recognises",
    "same_prompt",
    "selected_of",
    "summary",
]
