"""#1213 — the mission supervisor turns a TOOL-PERMISSION dialog into a decision card.

Driven through the real `mission_supervisor.run_pass` with the incident's own sequence, one real
capture per step: Grep → Shell → Always allow → answered. The screen read is the only seam; the
ledger, the missions store and the arbitration are real. What must hold:

* the dialog gets ONE live card per session, naming itself, and a repeated pass adds nothing;
* the next dialog in the same objective episode still gets its own card (the ledger's
  one-live-action rule and the timeline's once-per-episode arbitration must not suppress it);
* a card whose dialog is gone — answered in the terminal, or replaced — is retired, never left to
  be tapped;
* the escalation text names the prompt, and a nudge the model proposed is never typed into it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from agent_sessions import (
    mission_permission,
    missions,
    permission_prompts,
    prefs,
    vtscreen,
)
from agent_sessions import (
    mission_supervisor as sup,
)
from agent_sessions import orchestrator_ledger as ledger

SESSION = "opencode:ses_Perm1213Mission"
FIX = Path(__file__).parent / "fixtures"


def _observed(name: str | None) -> dict:
    if name is None:
        return {"prompt_class": "open", "menu": None, "permission": None, "fingerprint": "x"}
    cells = vtscreen.render_cells((FIX / name).read_bytes(), 46, 65)
    text = "\n".join(c.text for c in cells)
    return {
        "prompt_class": "confirm",
        "menu": None,
        "permission": permission_prompts.parse(text, "opencode", cells),
        "fingerprint": name,
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True})
    mid = missions.create_mission("Run a mission feature test", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, SESSION)
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": "ran",
                "title": "test ran",
                "probe": "forge_pr",
                "gate": True,
                "source": "playbook",
            }
        ],
    )

    from agent_sessions import review

    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    fp = {"n": 0}

    def gather(*a, **k):
        fp["n"] += 1  # the input always moved, so the model is always asked
        return ("transcript", f"fp{fp['n']}")

    monkeypatch.setattr(review, "gather_input", gather)

    async def reply(messages, **kw):
        # The model would happily nudge; the dialog must win over it.
        return {
            "recap": "waiting",
            "assessment": "needs_approval",
            "nudge": {"objective_key": "ran", "why": "keep going"},
        }

    monkeypatch.setattr(review, "complete_json", reply)
    sent: list = []

    async def no_nudge(*a, **k):
        sent.append(k)
        return {"sent": True}

    monkeypatch.setattr(sup, "nudge", no_nudge)
    screen = {"name": None}
    monkeypatch.setattr(mission_permission, "observe", lambda phys: _observed(screen["name"]))
    return mid, screen, sent


def _cards() -> list[dict]:
    return [
        r
        for r in ledger.latest_by_id().values()
        if r.get("origin") == mission_permission.ORIGIN and r.get("session_id") == SESSION
    ]


def _live_cards() -> list[dict]:
    return [r for r in _cards() if r.get("state") in ledger.ESCALATION_STATES]


def _operator_answers(card: dict) -> None:
    """What `menu_answer` does first: close the escalation by compare-and-set."""
    assert ledger.compare_and_set(card["id"], ledger.ESCALATION_STATES, "rejected", None)


@pytest.mark.anyio
async def test_grep_then_shell_then_always_allow_each_get_their_own_card(world):
    mid, screen, sent = world

    # Grep.
    screen["name"] = "opencode_permission_grep.raw"
    out = await sup.run_pass(mid)
    (grep,) = _live_cards()
    assert grep["observed_prompt"]["permission"]["title"] == '✱ Grep "(?i)mission"'
    assert grep["mission_id"] == mid and grep["verb"] == "escalate"
    assert sent == [], "a nudge was typed into a permission dialog"
    esc = [e for e in missions.get_mission(mid)["events"] if e["kind"] == "escalation"]
    assert len(esc) == 1
    assert esc[0]["text"].startswith('opencode asks permission — ✱ Grep "(?i)mission"')
    assert esc[0]["meta"]["prompt"]["title"] == '✱ Grep "(?i)mission"'
    assert out["per_session"][0]["permission"]["card"] == grep["id"]

    # A repeated pass on the same dialog adds nothing.
    await sup.run_pass(mid)
    assert [c["id"] for c in _live_cards()] == [grep["id"]]

    # Answered; the Shell dialog appears — same objective episode, so no second TIMELINE
    # escalation, but the card must still come.
    _operator_answers(grep)
    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    (shell,) = _live_cards()
    assert shell["id"] != grep["id"]
    assert shell["observed_prompt"]["permission"]["title"] == "# Shell command"

    # "Allow always" chosen; opencode's own confirmation stage shows the patterns.
    _operator_answers(shell)
    screen["name"] = "opencode_permission_always_stage.raw"
    await sup.run_pass(mid)
    (stage,) = _live_cards()
    assert stage["observed_prompt"]["permission"]["heading"] == "Always allow"

    # Confirmed; the dialog is gone and nothing is left to tap.
    _operator_answers(stage)
    screen["name"] = None
    await sup.run_pass(mid)
    assert _live_cards() == []
    assert sent == []


@pytest.mark.anyio
async def test_a_card_whose_dialog_was_answered_in_the_terminal_is_replaced(world):
    mid, screen, _sent = world
    screen["name"] = "opencode_permission_grep.raw"
    await sup.run_pass(mid)
    (grep,) = _live_cards()
    # The operator pressed Enter in the terminal; the next dialog is up. The stale Grep card must
    # not stay tappable beside it.
    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    assert ledger.get(grep["id"])["state"] == "expired"
    (shell,) = _live_cards()
    assert shell["observed_prompt"]["permission"]["title"] == "# Shell command"


@pytest.mark.anyio
async def test_a_card_whose_dialog_is_gone_is_retired(world):
    mid, screen, _sent = world
    screen["name"] = "opencode_permission_grep.raw"
    await sup.run_pass(mid)
    (card,) = _live_cards()
    screen["name"] = None
    await sup.run_pass(mid)
    assert ledger.get(card["id"])["state"] == "expired"
    assert _live_cards() == []


@pytest.mark.anyio
async def test_another_live_action_on_the_session_wins(world):
    """One live action per session is the ledger's rule: the card yields to a decision already
    waiting on the operator, and that decision is left exactly as it was."""
    mid, screen, _sent = world
    from automation_helpers import append_current_action

    append_current_action(
        {
            "id": "other",
            "state": "proposed",
            "verb": "continue",
            "session_id": SESSION,
            "mission_id": mid,
            "confidence": 0.9,
        }
    )
    screen["name"] = "opencode_permission_grep.raw"
    await sup.run_pass(mid)
    assert _live_cards() == []
    assert ledger.get("other")["state"] == "proposed"


def test_a_supervisor_card_is_never_autonomously_answerable():
    """The card is an escalation for the operator: its observed prompt carries no `menu`, so the
    auto-choose path (which reads `screen_menus` only) has nothing to answer."""
    obs = _observed("opencode_permission_shell.raw")
    assert obs["menu"] is None and obs["permission"] is not None


def _hold(card: dict, growth_mark: int | None = None, choose_id: str = "choose_uncertain") -> None:
    missions.permission_hold_add(
        choose_id,
        session_key=SESSION,
        identity=card["observed_prompt"]["permission"]["identity"],
        mission_id=None,
        growth_mark=growth_mark,
    )


@pytest.mark.anyio
async def test_a_held_dialog_is_not_offered_again(world):
    """#1218 review: the card was answered, the answer may have landed, the same dialog is still
    up. A new card would invite a second keypress on it."""
    mid, screen, sent = world
    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    (card,) = _live_cards()
    _operator_answers(card)
    _hold(card)
    await sup.run_pass(mid)
    await sup.run_pass(mid)
    assert _live_cards() == []
    assert sent == []


@pytest.mark.anyio
async def test_a_card_that_cannot_be_stored_still_blocks_the_nudge(world, monkeypatch):
    """#1218 review, finding 4: the dialog is known from the screen; losing its card must not let
    a model nudge through, and the escalation still names it."""
    mid, screen, sent = world

    def boom(*a, **k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(mission_permission, "sync_card", boom)
    rung: list = []
    monkeypatch.setattr(sup, "_announce", lambda row, key, reason: rung.append(reason))
    screen["name"] = "opencode_permission_grep.raw"
    await sup.run_pass(mid)
    assert sent == [], "a nudge was typed into a permission dialog"
    esc = [e for e in missions.get_mission(mid)["events"] if e["kind"] == "escalation"]
    assert len(esc) == 1 and esc[0]["text"].startswith("opencode asks permission")
    assert rung and rung[0].startswith("opencode asks permission")


@pytest.mark.anyio
async def test_the_card_does_not_wait_for_the_model(world, monkeypatch):
    """#1218 review, finding 6: an AI endpoint outage must not hide the dialog."""
    mid, screen, _sent = world
    from agent_sessions import review

    async def down(messages, **kw):
        raise review.ReviewError("endpoint down")

    monkeypatch.setattr(review, "complete_json", down)
    screen["name"] = "opencode_permission_grep.raw"
    with contextlib.suppress(Exception):
        await sup.run_pass(mid)
    assert len(_live_cards()) == 1


@pytest.mark.anyio
async def test_unreadable_holds_offer_no_card(world, monkeypatch):
    mid, screen, _sent = world
    from agent_sessions import permission_holds

    def unreadable(*a, **k):
        raise permission_holds.HoldsUnreadable("x")

    monkeypatch.setattr(permission_holds, "open_holds", unreadable)
    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    assert _live_cards() == []


def _frame_from(monkeypatch, frame: dict) -> None:
    from agent_sessions import scrollback

    def live_frame(key, n=4000):
        raw = frame["raw"]
        cells = vtscreen.render_cells(raw, 46, 65) if raw else []
        return ("\n".join(c.text for c in cells), cells) if cells else ("", None)

    monkeypatch.setattr(scrollback, "live_tail_frame", live_frame)


@pytest.mark.anyio
async def test_a_hold_is_released_only_on_positive_evidence(world, monkeypatch):
    """Nothing but a DIFFERENT complete dialog, or the agent's transcript advancing, ends a hold
    (#1218 review 5405, finding 2): not an empty frame, not the same dialog, not a half-drawn
    one."""
    mid, screen, _sent = world
    from agent_sessions import permission_holds

    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    (card,) = _live_cards()
    _operator_answers(card)
    _hold(card)
    frame = {"raw": b""}
    _frame_from(monkeypatch, frame)
    monkeypatch.setattr(permission_holds, "growth_mark", lambda key: None)

    for raw in (
        b"",  # nothing readable
        (FIX / "opencode_permission_shell.raw").read_bytes(),  # the same dialog
        (FIX / "opencode_permission_shell_truncated.raw").read_bytes(),  # a half-drawn one
        (FIX / "opencode_permission_shell.raw").read_bytes() + b"\x1b[46S",  # untrusted
    ):
        frame["raw"] = raw
        await sup.run_pass(mid)
        assert len(missions.permission_holds_open(SESSION)) == 1, raw[-20:]
        assert _live_cards() == []

    # A different COMPLETE dialog: released, and that dialog gets its own card.
    frame["raw"] = (FIX / "opencode_permission_always_stage.raw").read_bytes()
    screen["name"] = "opencode_permission_always_stage.raw"
    await sup.run_pass(mid)
    assert missions.permission_holds_open(SESSION) == []
    assert len(_live_cards()) == 1


@pytest.mark.anyio
async def test_the_agent_acting_releases_the_hold(world, monkeypatch):
    mid, screen, _sent = world
    from agent_sessions import permission_holds

    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    (card,) = _live_cards()
    _operator_answers(card)
    _hold(card, growth_mark=100)
    _frame_from(monkeypatch, {"raw": b""})
    mark = {"v": 100}
    monkeypatch.setattr(permission_holds, "growth_mark", lambda key: mark["v"])
    await sup.run_pass(mid)
    assert len(missions.permission_holds_open(SESSION)) == 1
    mark["v"] = 180  # the transcript grew: the agent acted after the answer
    await sup.run_pass(mid)
    assert missions.permission_holds_open(SESSION) == []
    assert len(_live_cards()) == 1  # the dialog still showing is offered again


@pytest.mark.anyio
async def test_a_hold_survives_ledger_compaction(world):
    """#1218 review 5405, finding 1, through the supervisor."""
    mid, screen, _sent = world
    screen["name"] = "opencode_permission_shell.raw"
    await sup.run_pass(mid)
    (card,) = _live_cards()
    _operator_answers(card)
    _hold(card)
    for i in range(ledger.HISTORY_MAX + 1):
        ledger.append(
            {"id": f"old-{i}", "state": "rejected", "verb": "continue", "session_id": "x"}
        )
    ledger.compact()
    await sup.run_pass(mid)
    assert _live_cards() == []


def test_the_holds_table_arrives_by_migration(tmp_path, monkeypatch):
    """An install at schema 32 gains `permission_holds` in place."""
    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    missions.create_mission("x", cwd="/tmp")
    con = sqlite3.connect(db)
    con.execute("DROP TABLE permission_holds")
    con.execute("PRAGMA user_version=32")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    assert missions.permission_holds_open(SESSION) == []
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 33
    con.close()
