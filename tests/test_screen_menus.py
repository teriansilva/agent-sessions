"""`screen_menus.parse` — an engine's own menu, or nothing (#1060, Phase 2).

The fixture is a real claude select list rendered by `vtscreen.render`, words neutralised, chrome
and geometry byte-for-byte (see its PROVENANCE). Everything below is about what must NOT parse:
the parser is the only thing standing between "a numbered list appeared" and "a button the operator
will press", and a wrong yes is the failure it exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_sessions import screen_menus as sm

REAL = (Path(__file__).parent / "fixtures" / "claude_select_menu.screen.txt").read_text("utf-8")
FOOTER = "Enter to select · ↑/↓ to navigate · Esc to cancel"


def lines():
    return REAL.rstrip("\n").split("\n")


def screen(ls):
    return "\n".join(ls) + "\n"


# ---- the real rendering ---------------------------------------------------------------------


def test_the_real_claude_menu_parses_to_its_answers_only():
    menu = sm.parse(REAL, "claude")
    assert menu is not None
    assert menu["engine"] == "claude"
    assert menu["question"].startswith("Earlier the instruction was to hold every merge")
    assert [(o["n"], o["label"]) for o in menu["options"]] == [
        (1, "Merge both ready branches"),
        (2, "Keep the hold — no merges"),
        (3, "Merge only the backend fix"),
        (4, "Merge everything, held too"),
    ]
    # `Type something.` opens a text box and `Chat about this` a conversation: neither answers.
    assert all(o["label"] not in ("Type something.", "Chat about this") for o in menu["options"])
    assert [o["selected"] for o in menu["options"]] == [True, False, False, False]


def test_output_ABOVE_the_menu_does_not_matter_only_the_bottom_does():
    noisy = ["pytest collected 412 items", "1. one", "2. two", ""] + lines()
    assert sm.parse(screen(noisy), "claude") is not None


def test_trailing_blank_lines_are_ignored():
    assert sm.parse(REAL + "\n\n   \n", "claude") is not None


def test_the_option_numbers_are_the_menus_own_even_when_the_cursor_moved():
    ls = lines()
    ls = [line.replace("❯ 1.", "  1.") for line in ls]
    ls = [line.replace("  3. Merge only", "❯ 3. Merge only") for line in ls]
    menu = sm.parse(screen(ls), "claude")
    assert menu is not None
    assert [o["n"] for o in menu["options"] if o["selected"]] == [3]


# ---- spoofing: a numbered list is not a menu ------------------------------------------------


def test_a_menu_echoed_HIGHER_UP_never_parses():
    """The anchor is the footer as the LAST line. An agent that printed a menu — in a log, a code
    block, a quote, on purpose — and then kept going is not at a menu."""
    assert sm.parse(screen(lines() + ["", "Done. Anything else?", "> "]), "claude") is None


def test_a_numbered_list_in_prose_is_never_a_menu():
    prose = "Here is the plan:\n1. Fix the parser\n2. Add tests\n3. Open a PR\nShall I proceed?\n"
    assert sm.parse(prose, "claude") is None


def test_a_yes_no_confirm_is_not_this_menu():
    assert sm.parse("Do you want to proceed? (y/n)\n", "claude") is None


def test_the_footer_alone_is_not_enough():
    assert sm.parse(screen(["some output", FOOTER]), "claude") is None


def test_a_description_that_starts_with_a_number_is_not_an_option():
    ls = lines()
    i = next(k for k, line in enumerate(ls) if line.startswith("  2. Keep the hold"))
    ls.insert(i + 1, "     2. and then merge anyway")
    menu = sm.parse(screen(ls), "claude")
    assert menu is not None
    assert [o["label"] for o in menu["options"]].count("and then merge anyway") == 0


def test_an_unrecognised_line_inside_the_menu_rejects_the_whole_parse():
    ls = lines()
    i = next(k for k, line in enumerate(ls) if line.startswith("  2. Keep the hold"))
    ls.insert(i, "rm -rf / # a line no rendering of this menu contains")
    assert sm.parse(screen(ls), "claude") is None


# ---- the numbering must agree with the digit that would be sent -----------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.replace("  3. Merge only", "  7. Merge only"),  # a gap
        lambda s: s.replace("  3. Merge only", "  2. Merge only"),  # a duplicate
        lambda s: s.replace("❯ 1.", "  1."),  # no cursor at all
        lambda s: s.replace("  2. Keep", "❯ 2. Keep"),  # two cursors
    ],
)
def test_any_disagreement_in_the_numbering_yields_nothing(mutate):
    assert sm.parse(mutate(REAL), "claude") is None


def test_a_menu_whose_only_options_are_non_answers_yields_nothing():
    ls = [
        " ☐ Anything else",
        "",
        "│ Anything else?",
        "",
        "❯ 1. Type something.",
        "  2. Chat about this",
        "",
        FOOTER,
    ]
    assert sm.parse(screen(ls), "claude") is None


def test_a_tabbed_multi_question_prompt_is_refused_rather_than_guessed():
    ls = lines()
    ls[0] = " ☐ Release scope  ☐ Order  ✔ Submit"
    assert sm.parse(screen(ls), "claude") is None


def test_no_title_or_no_question_yields_nothing():
    no_title = [line for line in lines() if "☐" not in line]
    assert sm.parse(screen(no_title), "claude") is None
    no_question = [line for line in lines() if not line.startswith("│")]
    assert sm.parse(screen(no_question), "claude") is None


# ---- a permission dialog is never a menu (issue review) ------------------------------------
#
# SYNTHETIC, and labelled so: no real claude permission prompt exists on the capture host, because
# BattleLab's own sessions run with permissions bypassed. A mission's dispatch does not bypass them,
# so the prompt is reachable, and a one-tap "don't ask again" is the button this module must never
# make. The worst case is asserted: the permission text drawn in the select-list chrome itself.


def _permission_in_menu_chrome(question="Do you want to proceed?"):
    return screen(
        [
            " ☐ Bash command",
            "",
            f"│ {question}",
            "",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again for this command",
            "  3. No, and tell Claude what to do differently",
            "",
            FOOTER,
        ]
    )


def test_a_permission_prompt_in_the_select_list_chrome_is_refused():
    assert sm.parse(_permission_in_menu_chrome(), "claude") is None


def test_the_dont_ask_again_option_alone_is_enough_to_refuse():
    text = _permission_in_menu_chrome(question="Run the migration script?")
    assert sm.parse(text, "claude") is None
    # …and with typographic apostrophes, as a TUI may render them.
    assert sm.parse(text.replace("don't", "don\u2019t"), "claude") is None


def test_a_plain_permission_dialog_without_a_title_is_refused():
    plain = screen(
        [
            "Bash command",
            "  rm -rf build/",
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again for rm commands",
            "  3. No",
            "",
            FOOTER,
        ]
    )
    assert sm.parse(plain, "claude") is None


def test_the_word_proceed_in_an_ordinary_question_does_not_refuse():
    """The guard is the permission dialog's own phrases, not any mention of proceeding."""
    ok = REAL.replace("Is that hold lifted now?", "Is that hold lifted now, so we can proceed?")
    assert sm.parse(ok, "claude") is not None


# ---- display text is display text -----------------------------------------------------------


def test_labels_and_question_are_cleaned_of_control_bytes_and_capped():
    ls = lines()
    ls = [
        line.replace("  2. Keep the hold — no merges", "  2. Keep\x1b[31m the\x07 hold" + "!" * 500)
        for line in ls
    ]
    menu = sm.parse(screen(ls), "claude")
    assert menu is not None
    label = menu["options"][1]["label"]
    assert "\x1b" not in label and "\x07" not in label
    assert len(label) <= sm.LABEL_MAX
    assert len(menu["question"]) <= sm.QUESTION_MAX


# ---- engines without a captured rendering ---------------------------------------------------


@pytest.mark.parametrize(
    "engine", ["codex", "opencode", "kimi", "antigravity", "gemini", "shell", "", "x"]
)
def test_an_engine_without_a_captured_rendering_always_yields_nothing(engine):
    assert sm.parse(REAL, engine) is None


def test_junk_input_never_raises():
    for junk in ("", "   ", "\n\n", None, 42, FOOTER):
        assert sm.parse(junk, "claude") is None  # type: ignore[arg-type]


def test_engine_of():
    assert sm.engine_of("claude:1111-2222") == "claude"
    assert sm.engine_of("not-a-key") == ""
    assert sm.engine_of("") == ""


# ---- the same menu drawn without the `│` gutter (a fresh 2.1.280 session, unedited) ----------

REAL_NO_GUTTER = (
    Path(__file__).parent / "fixtures" / "claude_select_menu_v2_1_280.screen.txt"
).read_text("utf-8")


def test_the_gutterless_rendering_parses_too():
    assert sm.parse(REAL_NO_GUTTER, "claude") == {
        "engine": "claude",
        "question": "Which colour do you prefer?",
        "options": [
            {"n": 1, "label": "Red", "selected": True},
            {"n": 2, "label": "Green", "selected": False},
            {"n": 3, "label": "Blue", "selected": False},
        ],
    }
    assert sm.recognises(REAL_NO_GUTTER)


def test_without_the_gutter_a_line_after_the_options_still_rejects_the_parse():
    ls = REAL_NO_GUTTER.splitlines()
    i = next(k for k, line in enumerate(ls) if line.startswith("  2. Green"))
    ls.insert(i, "Which colour do you prefer?")
    assert sm.parse("\n".join(ls), "claude") is None


def test_without_the_gutter_a_permission_dialog_is_still_refused():
    s = REAL_NO_GUTTER.replace("Which colour do you prefer?", "Do you want to proceed?")
    assert sm.parse(s, "claude") is None


def test_a_numbered_line_before_the_options_is_never_question_text():
    # The option pattern runs first: a numbered line at option indent between title and options
    # starts the options, so a gap in the numbering (here 1 → 1) refuses the parse.
    s = REAL_NO_GUTTER.replace("Which colour do you prefer?", "  1. Which colour?")
    assert sm.parse(s, "claude") is None
