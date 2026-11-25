"""#1213 — an agent's TOOL-PERMISSION dialog, read off its screen for the operator only.

Every fixture is a real capture (`tests/fixtures/permission_prompts.PROVENANCE.md`): the #1213
incident's own opencode ring, and fresh opencode / claude sessions in a private PTY. The parse must
match those renderings exactly and be empty on anything else; the key recipes are the ones proven
against the real CLIs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_sessions import (
    actuator,
    orchestrator,
    permission_prompts,
    screen_menus,
    vtscreen,
)

FIX = Path(__file__).parent / "fixtures"
OC = (46, 65)  # rows, cols the opencode captures were authored at
CL = (40, 100)


def _raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _frame(name: str, geom: tuple[int, int]) -> tuple[str, list[vtscreen.Cells]]:
    cells = vtscreen.render_cells(_raw(name), *geom)
    return "\n".join(c.text for c in cells), cells


ALL = {
    "opencode_permission_grep.raw": ("opencode", OC),
    "opencode_permission_shell_truncated.raw": ("opencode", OC),
    "opencode_permission_shell.raw": ("opencode", OC),
    "opencode_permission_shell_cursor2.raw": ("opencode", OC),
    "opencode_permission_always_stage.raw": ("opencode", OC),
    "claude_permission_bash.raw": ("claude", CL),
    "claude_permission_bash_cursor2.raw": ("claude", CL),
    "claude_permission_write.raw": ("claude", CL),
    "claude_permission_answered.raw": ("claude", CL),
}


# --- vtscreen: the colour layer is opt-in and changes no text ------------------------------------


@pytest.mark.parametrize("name", sorted(ALL))
def test_render_cells_is_render_with_colours(name):
    """Same frame, same trimming — for the whole capture and for cut points in it. The one
    difference is deliberate: an empty current grid is `[]`, never the pre-erase frame."""
    raw = _raw(name)
    rows, cols = ALL[name][1]
    for cut in (len(raw), len(raw) // 2, len(raw) // 3):
        cells = vtscreen.render_cells(raw[:cut], rows, cols)
        if cells:
            assert "\n".join(c.text for c in cells) == vtscreen.render(raw[:cut], rows, cols)
        for c in cells:
            assert len(c.fg) == len(c.bg) == len(c.text)


def test_a_cleared_screen_is_no_dialog_to_answer():
    """`render` shows the frame from before a trailing erase (a reviewer nicety); the coloured
    read an ANSWER is authorised against must not (#1218 review, finding 2)."""
    raw = _raw("opencode_permission_shell.raw") + b"\x1b[2J"
    assert vtscreen.render(raw, *OC)  # the display fallback still shows the old dialog
    assert vtscreen.render_cells(raw, *OC) == []


def test_the_colour_layer_reads_sgr_truecolour_256_and_basic():
    cells = vtscreen.render_cells(
        b"\x1b[38;2;1;2;3m\x1b[48;5;208mA\x1b[0mB\x1b[31;44mC\x1b[39;49mD", 2, 10
    )
    (row,) = cells
    assert row.text == "ABCD"
    assert row.fg == ("2;1;2;3", "", "31", "")
    assert row.bg == ("5;208", "", "44", "")


# --- the parse ------------------------------------------------------------------------------------


def _parse(name: str):
    engine, geom = ALL[name]
    text, cells = _frame(name, geom)
    return permission_prompts.parse(text, engine, cells)


def test_the_incident_grep_dialog():
    p = _parse("opencode_permission_grep.raw")
    assert p is not None
    assert p["engine"] == "opencode" and p["parser"] == "opencode-permission"
    assert p["heading"] == "Permission required"
    assert p["title"] == '✱ Grep "(?i)mission"'
    assert p["detail"] == "Pattern: (?i)mission"
    assert [(o["n"], o["label"], o["selected"], o["persistent"]) for o in p["options"]] == [
        (1, "Allow once", True, False),
        (2, "Allow always", False, True),
        (3, "Reject", False, False),
    ]


def test_a_half_drawn_dialog_is_no_dialog():
    """The incident ring ends partway through drawing the Shell dialog: no options, no footer."""
    assert _parse("opencode_permission_shell_truncated.raw") is None
    text, _ = _frame("opencode_permission_shell_truncated.raw", OC)
    assert not permission_prompts.recognises(text)


def test_opencode_cursor_is_read_from_colour_only():
    """The two captures have IDENTICAL text; only the highlighted option differs."""
    a_text, _ = _frame("opencode_permission_shell.raw", OC)
    b_text, _ = _frame("opencode_permission_shell_cursor2.raw", OC)
    assert a_text == b_text
    a, b = _parse("opencode_permission_shell.raw"), _parse("opencode_permission_shell_cursor2.raw")
    assert permission_prompts.selected_of(a) == 1
    assert permission_prompts.selected_of(b) == 2
    assert a["title"] == "# Shell command" and a["detail"] == "$ mkdir -p probe-dir-1213"
    # Same dialog, different cursor: `same_prompt` agrees, the digest does not.
    assert permission_prompts.same_prompt(a, b)
    assert permission_prompts.digest(a) != permission_prompts.digest(b)
    # …and the text fingerprint cannot tell them apart, which is why the digest exists.
    assert orchestrator._screen_fingerprint(a_text) == orchestrator._screen_fingerprint(b_text)


def test_without_colours_an_opencode_dialog_is_recognised_but_not_answerable():
    text, _ = _frame("opencode_permission_shell.raw", OC)
    assert permission_prompts.parse(text, "opencode", None) is None
    assert permission_prompts.recognises(text)


def test_opencode_always_allow_stage_shows_what_it_grants():
    p = _parse("opencode_permission_always_stage.raw")
    assert p is not None and p["heading"] == "Always allow"
    assert "- mkdir *" in p["detail"]
    assert [(o["label"], o["selected"], o["persistent"]) for o in p["options"]] == [
        ("Confirm", True, True),
        ("Cancel", False, False),
    ]


def test_claude_bash_and_write_dialogs():
    bash = _parse("claude_permission_bash.raw")
    assert bash is not None and bash["parser"] == "claude-permission"
    assert bash["title"] == "Bash command"
    assert bash["detail"].startswith("mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt")
    assert bash["question"] == "Do you want to proceed?"
    assert [o["n"] for o in bash["options"]] == [1, 2, 3]
    assert bash["options"][0]["selected"] and bash["options"][0]["label"] == "Yes"
    assert bash["options"][1]["persistent"]  # "Yes, and don't ask again for …"
    assert "operator-01" in bash["options"][1]["label"]  # the wrapped continuation is joined
    assert not bash["options"][2]["persistent"] and bash["options"][2]["label"] == "No"

    moved = _parse("claude_permission_bash_cursor2.raw")
    assert permission_prompts.selected_of(moved) == 2

    write = _parse("claude_permission_write.raw")
    assert write is not None and write["title"] == "Create file"
    assert write["question"] == "Do you want to create notes-1213.txt?"
    assert write["options"][1]["persistent"]  # "switch to accept edits"


def test_an_answered_claude_dialog_is_gone():
    assert _parse("claude_permission_answered.raw") is None


@pytest.mark.parametrize(
    "screen",
    [
        # The words, as prose — no rule, no option shape.
        "I will ask: Do you want to proceed?\n1. Yes\n2. No\nEsc to cancel",
        # A rule and options, but the footer is not last.
        "─" * 40 + "\n Bash command\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n"
        " Esc to cancel\nmore output",
        # Two cursors.
        "─" * 40
        + "\n Bash command\n\n Do you want to proceed?\n ❯ 1. Yes\n ❯ 2. No\n\n Esc to cancel",
        # Numbering with a gap.
        "─" * 40
        + "\n Bash command\n\n Do you want to proceed?\n ❯ 1. Yes\n   3. No\n\n Esc to cancel",
        # First option is not the plain yes.
        "─" * 40
        + "\n Bash command\n\n Do you want to proceed?\n ❯ 1. Sure\n   2. No\n\n Esc to cancel",
    ],
)
def test_claude_lookalikes_are_not_dialogs(screen):
    assert permission_prompts.parse(screen, "claude", None) is None
    assert not permission_prompts.recognises(screen)


def test_opencode_ambiguous_cursor_is_no_answer():
    """Both options painted in the border's colour — a repaint in flight — reads as no cursor."""
    ink = "\x1b[38;2;9;9;9m"
    hi = "\x1b[48;2;9;9;9m"
    raw = (
        f"{ink}┃\x1b[0m  △ Permission required\r\n"
        f"{ink}┃\x1b[0m    # Shell command\r\n"
        f"{ink}┃\x1b[0m\r\n"
        f"{ink}┃\x1b[0m  $ ls\r\n"
        f"{ink}┃\x1b[0m\r\n"
        f"{ink}┃\x1b[0m   {hi}Allow once\x1b[0m   {hi}Allow always\x1b[0m   Reject\r\n"
        f"{ink}┃\x1b[0m\r\n"
        f"{ink}┃\x1b[0m  ⇆ select  enter confirm\r\n"
    ).encode()
    cells = vtscreen.render_cells(raw, 10, 60)
    text = "\n".join(c.text for c in cells)
    assert permission_prompts.recognises(text)
    assert permission_prompts.parse(text, "opencode", cells) is None
    # One highlighted option IS an answer.
    raw_one = raw.replace(f"{hi}Allow always".encode(), b"Allow always")
    cells = vtscreen.render_cells(raw_one, 10, 60)
    p = permission_prompts.parse("\n".join(c.text for c in cells), "opencode", cells)
    assert p is not None and permission_prompts.selected_of(p) == 1


def test_an_engine_without_a_kind_parses_nothing():
    text, cells = _frame("claude_permission_bash.raw", CL)
    for engine in ("codex", "gemini", "kimi", "shell"):
        assert permission_prompts.parse(text, engine, cells) is None


# --- the autonomy boundary ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ALL))
def test_no_permission_dialog_is_ever_a_menu(name):
    """`screen_menus.parse` is what every AUTONOMOUS path reads (auto-choose, the pass). It must
    stay blind to every permission dialog, so no tier can answer one."""
    engine, geom = ALL[name]
    text, _ = _frame(name, geom)
    assert screen_menus.parse(text, engine) is None


def test_a_permission_dialog_classifies_as_a_confirmation():
    for name in ("opencode_permission_grep.raw", "claude_permission_bash.raw"):
        text, _ = _frame(name, ALL[name][1])
        assert orchestrator._prompt_class(text) == "confirm", name


# --- keys ------------------------------------------------------------------------------------


def test_claude_keys_are_the_digit_alone():
    for n in (1, 2, 3):
        for cur in (1, 2, 3):
            assert permission_prompts.keys("claude-permission", n, cur, 3) == str(n).encode()


def test_opencode_keys_move_right_from_the_cursor_and_wrap():
    k = permission_prompts.keys
    assert k("opencode-permission", 1, 1, 3) == b"\r"
    assert k("opencode-permission", 3, 1, 3) == b"\x1b[C\x1b[C\r"
    assert k("opencode-permission", 1, 2, 3) == b"\x1b[C\x1b[C\r"  # wraps: 2 → 3 → 1
    assert k("opencode-permission", 2, 1, 2) == b"\x1b[C\r"


@pytest.mark.parametrize(
    "args",
    [
        ("opencode-permission", 4, 1, 3),
        ("opencode-permission", 1, 0, 3),
        ("opencode-permission", 1, 1, 1),
        ("opencode-permission", True, 1, 3),
        ("claude-numbered", 1, 1, 3),
        ("", 1, 1, 3),
    ],
)
def test_keys_refuse_anything_out_of_range(args):
    with pytest.raises(ValueError):
        permission_prompts.keys(*args)


def _choose(**over):
    rec = {
        "verb": "choose",
        "option": 3,
        "origin": "operator",
        "submit": "permission",
        "permission": {"parser": "opencode-permission", "from_selected": 1, "count": 3},
    }
    rec.update(over)
    return rec


def test_render_builds_the_recipe_for_the_operator():
    assert actuator.render(_choose(), {}) == b"\x1b[C\x1b[C\r"


@pytest.mark.parametrize("origin", [None, "model", "mission_supervisor", "auto_choose"])
def test_render_refuses_a_permission_answer_from_anyone_but_the_operator(origin):
    rec = _choose(origin=origin, auto_choose=True)
    with pytest.raises(actuator.NotDeliverable):
        actuator.render(rec, {})


def test_render_refuses_a_permission_answer_it_cannot_build():
    with pytest.raises(actuator.NotDeliverable):
        actuator.render(_choose(permission={"parser": "opencode-permission"}), {})


def test_summary_names_the_prompt():
    p = _parse("opencode_permission_grep.raw")
    assert permission_prompts.summary(p) == (
        'opencode asks permission — ✱ Grep "(?i)mission": Pattern: (?i)mission '
        "(Allow once · Allow always · Reject)"
    )


# --- the binding is lossless (#1218 review, finding 1) ----------------------------------------


def _claude_text() -> str:
    text, _ = _frame("claude_permission_bash.raw", CL)
    return text


def test_commands_that_display_alike_are_different_dialogs():
    base = _claude_text()
    assert "mkdir -p probe-dir-1213 && touch" in base
    spaced = base.replace("mkdir -p probe-dir-1213 && touch", "mkdir -p probe-dir-1213 &&  touch")
    a = permission_prompts.parse(base, "claude", None)
    b = permission_prompts.parse(spaced, "claude", None)
    assert a is not None and b is not None
    assert a["detail"] == b["detail"]  # the card would show the same words…
    assert not permission_prompts.same_prompt(a, b)  # …but they are not the same dialog
    assert permission_prompts.digest(a) != permission_prompts.digest(b)


def test_the_cursor_is_not_part_of_the_identity():
    base = _claude_text()
    moved = base.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No")
    a = permission_prompts.parse(base, "claude", None)
    b = permission_prompts.parse(moved, "claude", None)
    assert a is not None and b is not None
    assert a["identity"] == b["identity"] and permission_prompts.same_prompt(a, b)
    assert permission_prompts.digest(a) != permission_prompts.digest(b)


def test_a_dialog_too_long_to_show_whole_gets_no_buttons():
    base = _claude_text()
    long_cmd = "echo " + "x" * (permission_prompts.DETAIL_MAX + 10)
    huge = base.replace(
        "   mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt\n", "   " + long_cmd + "\n"
    )
    assert long_cmd in huge
    assert permission_prompts.parse(huge, "claude", None) is None


# --- the alternate screen (#1218 review 5403, finding 2) ---------------------------------------


def test_leaving_the_alternate_screen_takes_the_dialog_with_it():
    main = b"shell prompt $ \r\n"
    raw = main + b"\x1b[?1049h" + _raw("opencode_permission_shell.raw") + b"\x1b[?1049l"
    cells = vtscreen.render_cells(raw, *OC)
    text = "\n".join(c.text for c in cells)
    assert "Permission required" not in text
    assert text.startswith("shell prompt $")
    assert permission_prompts.parse(text, "opencode", cells) is None


def test_leaving_an_alternate_screen_whose_entry_was_cut_off_is_unknown():
    # The real capture enters the alternate screen itself; a slice that starts inside it and then
    # sees only the exit cannot know the normal buffer, so it shows nothing rather than the dialog.
    raw = _raw("opencode_permission_shell.raw").replace(b"\x1b[?1049h", b"") + b"\x1b[?1049l"
    assert vtscreen.render_cells(raw, *OC) == []


# --- fail closed on anything unmodelled (#1218 review 5405, finding 3) -------------------------


@pytest.mark.parametrize(
    "tail",
    [
        b"\x1b[46S",
        b"\x1b[2T",
        b"\x1b[H\x1b[46M",
        b"\x1b[3L",
        b"\x1b[5P",
        b"\x1b[4@",
        b"\x1b[9X",
        b"\x1b[5;20r",  # a scroll region this does not model
        b"\x1b[?7l",  # autowrap off
        b"\x1b[?01049l",  # numerically the alternate-screen exit: modelled, and the dialog goes
        b"\x1bM",
        b"\x1bD",
        b"\x1bc",
        b"\x1b(0",  # line-drawing charset
        b"\x0e",  # shift-out
        b"\x7f",  # DEL (backspace itself is modelled since #2058: see the tests at the end)
        b"\x1b[?45h",  # reverse wrap
        b"\x1b[8;10;20t",  # resize the window
        b"\x1bPq#0;2;0;0;0\x1b\\",  # sixel graphics
        b"\x1b",  # a torn sequence
    ],
)
def test_anything_unmodelled_after_the_dialog_leaves_no_trusted_frame(tail):
    raw = _raw("opencode_permission_shell.raw")
    assert vtscreen.render_cells(raw, *OC)
    assert vtscreen.render_cells(raw + tail, *OC) == []


@pytest.mark.parametrize(
    "tail",
    [
        b"\x1b[?25l\x1b[?25h",
        b"\x1b[?2026h\x1b[?2026l",
        b"\x1b[?1000h\x1b[?1006h\x1b[?2004h",
        b"\x1b]0;title\x07",
        b"\x1b]8;;https://example.invalid\x1b\\",
        b"\x1b[c\x1b[6n\x1b[14t\x1b[>q\x1b[?u\x1b[>1u\x1b[<u",
        b"\x1b[?2026$p",
        b"\x1b[2 q",
        b"\x1b_Gi=1,a=q;AAAA\x1b\\",
        b"\x1bP+q544e\x1b\\",
        b"\x1b7\x1b8\x1b[s\x1b[u",
        b"\x07\x0f\x1b(B",
        b"\x1b[r",
    ],
)
def test_inert_controls_keep_the_frame_trusted(tail):
    """Queries, modes and strings the reference terminal draws nothing for. Measured: every one of
    these occurs in real claude/opencode rings, and a list that refused them would refuse every
    real screen."""
    raw = _raw("opencode_permission_shell.raw")
    cells = vtscreen.render_cells(raw + tail, *OC)
    assert cells
    assert permission_prompts.parse("\n".join(c.text for c in cells), "opencode", cells)


# --- backspace is modelled (#2058) ---------------------------------------------------------------

#: sha256 of each capture's three renders (cells with colours, the strict grid, the reviewer's
#: text), computed on `main@2b47f807`, the commit before the renderer modelled backspace.
RENDERS_BEFORE_2058 = json.loads((FIX / "permission_prompts.renders-before-2058.json").read_text())


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_permission_capture_renders_byte_identically_to_before_backspace_was_modelled(name):
    raw = _raw(name)
    assert b"\x08" not in raw  # no capture holds a backspace, so none may change
    geom = ALL[name][1]
    cells = vtscreen.render_cells(raw, *geom)
    renders = {
        "cells": [[c.text, list(c.fg), list(c.bg)] for c in cells],
        "grid": vtscreen.render_grid(raw, *geom),
        "text": vtscreen.render(raw, *geom),
    }
    blob = json.dumps(renders, ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(blob).hexdigest() == RENDERS_BEFORE_2058[name]


def _read(raw: bytes, engine: str, geom: tuple[int, int]):
    cells = vtscreen.render_cells(raw, *geom)
    return permission_prompts.parse("\n".join(c.text for c in cells), engine, cells), cells


@pytest.mark.parametrize("name", ["opencode_permission_shell.raw", "claude_permission_bash.raw"])
def test_a_backspace_that_changes_no_cell_leaves_the_same_dialog(name):
    """BS moves the cursor and draws nothing: the reader sees the dialog that is on the screen."""
    engine, geom = ALL[name]
    before, _ = _read(_raw(name), engine, geom)
    after, _ = _read(_raw(name) + b"\x08", engine, geom)
    assert before and after == before
    assert permission_prompts.digest(after) == permission_prompts.digest(before)


#: The tails `tests/fixtures/shell_backspace_record.py` appended to `claude_permission_bash.raw`
#: for the two recorded adversarial rings. Each has the screen tmux 3.4 showed beside it.
SWAP_TAIL = b"\x1b[20;10H\x08\x08\x08No \x1b[23;9H\x08\x08Yes"
COMMAND_TAIL = b"\x1b[16;57H\x08\x08\x08\x08\x08x.sh "


def _adversarial(name: str, tail: bytes):
    """The ring through the REAL reader, after checking that the frame it read is, row for row,
    the screen the reference terminal showed for the same bytes."""
    raw = _raw(f"{name}.raw")
    assert raw == _raw("claude_permission_bash.raw") + tail and b"\x08" in tail
    shown = (FIX / f"{name}.tmux.txt").read_text()
    parsed, cells = _read(raw, "claude", CL)
    assert [c.text for c in cells] == shown.strip("\n").split("\n")
    grid = vtscreen.render_grid(raw, *CL)
    assert "\n".join(row.rstrip() for row in grid).rstrip("\n") + "\n" == shown
    return parsed, shown.split("\n")


def test_a_command_overwritten_with_backspaces_is_read_as_the_screen_shows_it():
    """Backspaces rewrite the end of the command inside a dialog whose every option is still as
    claude drew it. The reader shows the command that is on the screen, never the one that was
    there, and the dialog is no longer the one an answer was authorised against."""
    honest, _ = _read(_raw("claude_permission_bash.raw"), "claude", CL)
    forged, shown = _adversarial("claude_permission_bash_backspace_command", COMMAND_TAIL)
    assert honest["detail"].splitlines()[0].endswith("touch probe-dir-1213/f.txt")
    assert (
        forged["detail"].splitlines()[0] == "mkdir -p probe-dir-1213 && touch probe-dir-1213/x.sh"
    )
    assert "   mkdir -p probe-dir-1213 && touch probe-dir-1213/x.sh" in shown
    assert "   mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt" not in shown
    assert forged["options"] == honest["options"]

    # The binding moved with the screen: an answer prepared for the honest dialog is refused.
    assert forged["identity"] != honest["identity"]
    assert not permission_prompts.same_prompt(honest, forged)
    assert permission_prompts.digest(forged) != permission_prompts.digest(honest)


def test_options_overwritten_with_backspaces_are_never_read_as_what_they_were():
    """Backspaces turn `1. Yes` into `1. No` and `3. No` into `3. Yes`. The frame is trusted and
    is exactly what the terminal showed, and it is not a dialog claude draws (its first option
    is the plain yes, its last the no), so the reader offers no buttons at all. What it must
    never do is what a reader that skipped BS would: still see `1. Yes`."""
    forged, shown = _adversarial("claude_permission_bash_backspace_swap", SWAP_TAIL)
    assert " ❯ 1. No" in shown and "   3. Yes" in shown
    assert " ❯ 1. Yes" not in shown and "   3. No" not in shown
    assert forged is None


@pytest.mark.parametrize(
    "tail",
    [
        b"\x1b[20;100HX\x08\x08",  # a write in the last column, then BS in the pending wrap
        b"\x1b[20;999H\x1b[C\x08",  # the cursor pushed to the margin, then BS
        b"\x1b[20;100HX\x1b[D\x08No ",  # the margin, a step left (CUB), then BS (#2328 review)
        b"\x1b[?45h\x1b[20;1H\x08",  # reverse wrap switched on, then BS at column 0
        b"\x1b[20;10H\xcc\x81\x08\x08\x08No ",  # a combining mark, then the overwrite
    ],
)
def test_a_backspace_the_renderer_cannot_place_exactly_leaves_no_dialog(tail):
    raw = _raw("claude_permission_bash.raw")
    assert _read(raw, "claude", CL)[0]
    assert vtscreen.render_cells(raw + tail, *CL) == []


@pytest.mark.parametrize(
    "tail",
    [
        # 1049's entry overwrites the cursor DECRC restores (xterm.js: `.../fZtxt`).
        b"\x1b[16;57H\x1b7\x1b[16;54H\x1b[?1049h\r\x1b[?1049l\x1b8\x08Z",
        # 47 keeps the alternate screen's row; a CHA fixes the column only (xterm.js: row 17).
        b"\x1b[16;57H\x1b[?47h\x1b[17;1H\x1b[?47l\x1b[57G\x08Z",
        b"\x1b[16;57H\x1b[?1047h\x1b[17;1H\x1b[?1047l\x1b[57G\x08Z",
        # Review 8836: a saved cursor past a CUP, and a 47 entry with a 1049 exit.
        b"\x1b[16;57H\x1b[?47h\x1b[17;57H\x1b[?47l\x1b7\x1b[1;1H\x1b8\x08Z",
        b"\x1b[16;54H\x1b7\x1b[16;57H\x1b[?47h\r\x1b[?1049l\x08Z",
    ],
    ids=["decrc_after_1049", "47_row", "1047_row", "decsc_past_cup", "47_entry_1049_exit"],
)
def test_a_backspace_at_a_position_the_model_cannot_vouch_for_leaves_no_dialog(tail):
    """#2328 review 8833: both reader-level reproductions. Without the refusal the reader returns
    the command ending `f.txZ`, which the real terminal never showed."""
    raw = _raw("claude_permission_bash.raw").replace(b"\x1b[?1049h", b"", 1)
    assert _read(raw, "claude", CL)[0]
    assert vtscreen.render_cells(raw + tail, *CL) == []


@pytest.mark.parametrize("mode", [b"1049", b"1047", b"47"])
def test_a_backspace_from_an_unpinned_column_through_the_alternate_screen_leaves_no_dialog(mode):
    """#2328 review 8829: the capture with its opening `CSI ? 1049 h` removed, so the tail's own
    buffer switch is the only one. A row's end reached at the margin, a step left (unpinned), a
    round trip through the alternate screen, then BS and a write. xterm.js lands the write one
    cell left of this model; the frame must not be trusted."""
    raw = _raw("claude_permission_bash.raw").replace(b"\x1b[?1049h", b"", 1)
    assert _read(raw, "claude", CL)[0]
    tail = b"\x1b[16;100HX\x1b[44D\x1b[?" + mode + b"h\r\x1b[?" + mode + b"l\x08Z"
    assert vtscreen.render_cells(raw + tail, *CL) == []
