"""What an escalation knows about the screen, and the window it is judged from (#1060, Phase 1).

Two defects, both found against the REAL claude menu that motivated #1060 (the fixture):

* `_prompt_class` judged it from the last 400 characters of a 1200-character read. A select list
  with wrapped descriptions keeps its "1." further up than that, and a 1200-char tail cut off its
  title too — so the menu classified as `open`, and a pass could never know it was at a choice.
* An escalation delivers nothing, so it recorded no precondition — no fact about the screen at all.

The fix widens the classification window, asks the strict parser first, records what an escalation
saw, and — the part that must not regress — reads the SAME window at proposal and at delivery, so a
frame that did not change can never be refused as "a different kind of prompt now".
"""

from __future__ import annotations

from pathlib import Path

from agent_sessions import actuator, orchestrator, screen_menus

FIX = (Path(__file__).parent / "fixtures" / "claude_select_menu.screen.txt").read_text("utf-8")
KEY = "claude:11111111-2222-4333-8444-555555555555"


def _screen(monkeypatch, text: str):
    """`live_tail_text` returns the tail of a rendered frame, sliced to what was asked for."""
    reads: list[int] = []

    def tail(key, max_chars=4000):
        reads.append(max_chars)
        return text[-max_chars:]

    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", tail)
    return reads


def test_the_real_claude_menu_is_a_choice():
    assert orchestrator._prompt_class(FIX) == "choice"
    # …which the old 400-character substring check alone could not see.
    assert "1." not in FIX[-400:]


def test_the_menu_does_not_fit_in_the_fingerprint_window():
    """The premise of the wider read: at `PRECONDITION_CHARS` the title is gone and the strict
    parser cannot recognise the menu."""
    assert screen_menus.parse(FIX[-orchestrator.PRECONDITION_CHARS :], "claude") is None
    assert screen_menus.parse(FIX[-orchestrator.PROMPT_SCREEN_CHARS :], "claude") is not None


def test_proposal_and_delivery_read_the_SAME_window_and_agree_on_an_unchanged_menu(monkeypatch):
    reads = _screen(monkeypatch, FIX)
    pre = orchestrator.precondition_for(KEY)
    assert pre["prompt_class"] == "choice"
    assert actuator.screen_matches(KEY, pre) == (True, "")
    assert set(reads) == {orchestrator.PROMPT_SCREEN_CHARS}, reads


def test_the_fingerprint_is_unchanged_by_the_wider_read():
    """Only the class window widened. The fingerprint still hashes the last `PRECONDITION_CHARS`,
    so a precondition recorded before this change still verifies against the same frame."""
    assert orchestrator._screen_fingerprint(FIX) == orchestrator._screen_fingerprint(
        FIX[-orchestrator.PRECONDITION_CHARS :]
    )


def test_a_moved_screen_is_still_refused(monkeypatch):
    _screen(monkeypatch, FIX)
    pre = orchestrator.precondition_for(KEY)
    _screen(monkeypatch, "Done. Anything else?\n> ")
    ok, why = actuator.screen_matches(KEY, pre)
    assert not ok and why


def test_an_escalation_records_what_the_screen_showed(monkeypatch):
    _screen(monkeypatch, FIX)
    seen = orchestrator.observed_prompt_for(KEY)
    assert seen["prompt_class"] == "choice"
    assert [o["n"] for o in seen["menu"]["options"]] == [1, 2, 3, 4]
    assert seen["menu"]["question"].startswith("Earlier the instruction")


def test_an_engine_without_a_captured_menu_records_the_class_and_no_menu(monkeypatch):
    _screen(monkeypatch, FIX)
    seen = orchestrator.observed_prompt_for("codex:11111111-2222-4333-8444-555555555555")
    assert seen["menu"] is None


def test_an_unreadable_screen_records_open_and_no_menu(monkeypatch):
    def boom(*a, **k):
        raise OSError("ring gone")

    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", boom)
    seen = orchestrator.observed_prompt_for(KEY)
    assert seen["prompt_class"] == "open" and seen["menu"] is None
