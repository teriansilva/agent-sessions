"""The nudge budget, derived rather than counted (#885, Phase 5a of #840).

The operator's complaint is the spec: agents stall on mundane things and nothing picks it up. So
the supervisor must nudge — and, just as importantly, must know when to STOP, because a feature
that nags forever gets switched off and then nothing is picked up at all.

Everything here is about what a nudge COSTS. A count kept at send time is wrong on one side of a
crash or the other, so the store records which ACTION each nudge was and the charge is read back
from that action's terminal state. These tests drive the states that decide it.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_supervisor as sup
from agent_sessions import missions
from agent_sessions import orchestrator_ledger as ledger

SESSION = "claude:11111111-1111-1111-1111-111111111111"
KEY = "checks_green"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    missions.reset_schema_cache_for_test()
    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    # A SESSION, because `may_nudge` now refuses a mission that holds none — a nudge is a write
    # into a session, and with none there is nothing to write to (#896 review 7, finding 2).
    # Every test in this file is about a mission the supervisor is following through on, which by
    # definition has one; the tests that are about the EMPTY case adopt nothing and say so.
    missions.adopt(mid, SESSION)
    return mid


def _ensure_objective(mid: str, key: str = KEY) -> None:
    """The objective row must EXIST for a lifecycle write to land (#888 review, finding 2).

    Supervisor rows are cleaned when an objective is dropped, so an insert that did not check
    could resurrect them afterwards. These tests always meant a real objective; they just used to
    get away with naming one that existed nowhere.
    """
    if not any(o.get("key") == key for o in missions.objectives(mid)):
        missions.instantiate_objectives(
            mid,
            [
                {
                    "key": key,
                    "title": key.replace("_", " "),
                    "probe": "forge_pr",
                    "gate": True,
                    "source": "playbook",
                }
            ],
        )


def _sent(mid: str, action_id: str, state: str, *, episode: int = 1) -> None:
    """Record a nudge the way the pass does — binding first, then the ledger row."""
    _ensure_objective(mid)
    missions.record_supervisor_action(
        mid, session_key=SESSION, objective_key=KEY, episode=episode, action_id=action_id
    )
    ledger.append({"id": action_id, "state": state, "verb": "continue", "session_id": SESSION})


def _real_objective(mid, key=KEY, *, gate=True):
    """Create the objective row these tests name.

    They used to pass a bare key that existed nowhere, which the store accepted. It no longer does
    (#888 review, findings 5 and 8): a stand-down on an unknown key would sit waiting to silence it
    if it were ever added, and a nudge is re-authorized against the objective at the write boundary.
    Creating the row is what these tests always meant.
    """
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": key,
                "title": key.replace("_", " "),
                "probe": "forge_pr",
                "gate": gate,
                "source": "playbook",
            }
        ],
    )


def test_a_fresh_objective_has_its_whole_budget(store):
    b = sup.budget_state(store, KEY)
    assert (b["spent"], b["remaining"], b["episode"]) == (0, sup.NUDGE_BUDGET, 1)
    assert sup.may_nudge(store, KEY) == (True, "")


def test_only_a_DELIVERED_nudge_costs_a_unit(store):
    """The asymmetry the whole budget rests on.

    Charging a refusal drains the budget without anything reaching the agent, and then escalates
    as though the agent had ignored three nudges — a lie about the AGENT rather than a report
    about it. A viewer at the keyboard is the common case: the operator is typing, so the nudge
    is correctly refused, and it must not count against the agent.
    """
    _sent(store, "a1", "delivered")
    assert sup.budget_state(store, KEY)["spent"] == 1
    for i, state in enumerate(("stale", "failed", "expired", "rejected")):
        _sent(store, f"r{i}", state)
    assert sup.budget_state(store, KEY)["spent"] == 1, "a nudge that reached nobody was charged"
    assert sup.may_nudge(store, KEY)[0] is True


def test_the_budget_runs_out_and_says_so(store):
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    allowed, why = sup.may_nudge(store, KEY)
    assert allowed is False
    assert "budget" in why, why


def test_an_INDETERMINATE_nudge_charges_AND_stops(store):
    """ "It may or may not have been sent" ends the automatic attempts.

    A retry could be the second copy of an instruction the agent already has, and delivering the
    same instruction twice is worse than not delivering it — the agent cannot tell which one the
    operator meant.
    """
    _sent(store, "a1", "indeterminate")
    b = sup.budget_state(store, KEY)
    assert b["spent"] == 1 and b["indeterminate"] is True
    assert b["remaining"] > 0, "the fixture must leave budget, or this proves nothing"
    allowed, why = sup.may_nudge(store, KEY)
    assert allowed is False and "may or may not" in why


def test_an_action_RECORDED_but_ABSENT_from_the_ledger_is_indeterminate(store):
    """Recorded as sent and not in the ledger is indistinguishable from an append that landed and
    was lost, so it gets the same answer rather than being read as "never happened"."""
    _ensure_objective(store)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="ghost"
    )
    b = sup.budget_state(store, KEY)
    assert (b["spent"], b["indeterminate"]) == (1, True)
    assert sup.may_nudge(store, KEY)[0] is False


def test_a_LIVE_nudge_blocks_another_without_being_charged(store):
    """In flight has told the agent nothing yet, so it costs nothing — but sending a second now
    races two nudges into one session."""
    _sent(store, "a1", "approved")
    b = sup.budget_state(store, KEY)
    assert (b["spent"], b["live"]) == (0, 1)
    allowed, why = sup.may_nudge(store, KEY)
    assert allowed is False and "settled" in why


def test_an_UNREADABLE_ledger_stands_down_rather_than_assuming_a_fresh_budget(store, monkeypatch):
    """A store that will not read has not said the budget is fresh.

    Reading it as fresh would hand a stalled objective an unlimited supply of nudges every time
    the file hiccups — the fail-open this tri-state read exists to prevent.
    """
    _sent(store, "a1", "delivered")
    monkeypatch.setattr(ledger, "latest_by_id_checked", lambda *a, **k: ("unreadable", {}))
    b = sup.budget_state(store, KEY)
    assert b["unreadable"] is True and b["remaining"] == 0
    allowed, why = sup.may_nudge(store, KEY)
    assert allowed is False and "could not be read" in why


def test_the_budget_RESETS_on_a_new_episode_but_keeps_the_old_ones_history(store):
    """Delivered, then progress, then a later stall starts the count at zero — and the earlier
    episode's charges are superseded, not destroyed."""
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    assert sup.may_nudge(store, KEY)[0] is False

    missions.bump_episode(store, KEY)  # the objective moved
    b = sup.budget_state(store, KEY)
    assert (b["episode"], b["spent"], b["remaining"]) == (2, 0, sup.NUDGE_BUDGET)
    assert sup.may_nudge(store, KEY) == (True, "")
    assert len(missions.supervisor_action_ids(store, KEY, 1)) == sup.NUDGE_BUDGET


def test_a_STOOD_DOWN_objective_is_silent_until_it_moves(store):
    """ "Stop telling me" silences the episode the operator was looking at. It does not mark the
    objective met, and the silence ends when the objective transitions."""
    _real_objective(store)
    episode, _ = missions.objective_episode(store, KEY)
    assert missions.stand_down(store, KEY, episode=episode) is True
    allowed, why = sup.may_nudge(store, KEY)
    assert allowed is False and "not to be told" in why

    missions.bump_episode(store, KEY)
    assert sup.may_nudge(store, KEY) == (True, ""), "a moved objective stayed silenced"


# ---- the mechanical half runs ALWAYS, and that is the point --------------------------------


def _objective(mid, key, *, gate=True, state="pending"):
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": key,
                "title": key.replace("_", " "),
                "probe": "forge_pr",
                "gate": gate,
                "source": "playbook",
            }
        ],
    )
    if state != "pending":
        missions.patch_objectives(mid, [{"op": "waive", "key": key}])


def test_likely_done_is_a_PROPOSAL_and_needs_objectives_to_exist(store):
    """A mission with no objectives is not done — it is UNMEASURED, which is a different fact.

    Reporting it as done would let a mission with an empty checklist close itself, which is the
    failure the whole objective store exists to prevent.
    """
    assert sup.assess(store)["likely_done"] is False

    _objective(store, "pr_open")
    a = sup.assess(store)
    assert a["likely_done"] is False and a["unmet_gates"] == 1

    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    a = sup.assess(store)
    assert a["likely_done"] is True and a["unmet_gates"] == 0
    # …and nothing was marked met by the assessment itself.
    assert [o["state"] for o in missions.objectives(store)] == ["waived"]


def test_the_mechanical_half_needs_NO_model_and_no_ledger_write(store, monkeypatch):
    """It runs on every pass, so it must be cheap and must not touch the model.

    Gating it on the input fingerprint is the trap #885 names: the fingerprint tracks the
    reviewable CONTENT, while the thing that most needs noticing — the final gate becoming met —
    is a change to the OBJECTIVE store. A mission would finish and nobody would say so.
    """
    from agent_sessions import review

    def boom(*a, **k):
        raise AssertionError("the mechanical half called the model")

    monkeypatch.setattr(review, "complete_json", boom)
    monkeypatch.setattr(review, "_post_chat", boom, raising=False)

    _objective(store, "pr_open")
    a = sup.assess(store)
    assert [o["key"] for o in a["objectives"]] == ["pr_open"]
    assert a["objectives"][0]["may_nudge"] is True


def test_the_assessment_carries_each_objectives_budget_and_reason(store):
    """Every refusal names itself — "the supervisor did nothing" is the state the operator
    complained about, and an unexplained silence is indistinguishable from a broken feature."""
    _objective(store, "checks_green")
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    row = next(o for o in sup.assess(store)["objectives"] if o["key"] == "checks_green")
    assert row["spent"] == sup.NUDGE_BUDGET and row["remaining"] == 0
    assert row["may_nudge"] is False and "budget" in row["why_not"]


def test_a_session_that_has_written_NOTHING_reads_stuck_not_running(tmp_path, monkeypatch):
    """Alive is not started (#840 §9.6).

    A trust dialog, an auth wall and a first-run prompt all look identical to a healthy session
    from outside the process. The engine's own store is what tells them apart.
    """
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-tmp-x").mkdir(parents=True)
    native = "11111111-2222-3333-4444-555555555555"

    stalled, why, _turns = sup.session_is_stalled(
        "claude", native, since=0.0, now=sup.STALL_AFTER_S + 1, home=home
    )
    assert stalled is True and "written nothing" in why

    # …but not before the grace period: a session that started five seconds ago is not stuck.
    stalled, why, _turns = sup.session_is_stalled("claude", native, since=0.0, now=5.0, home=home)
    assert stalled is False

    # …and once it has written, it is running no matter how long it has been.
    (home / ".claude" / "projects" / "-tmp-x" / f"{native}.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
    )
    stalled, why, _turns = sup.session_is_stalled(
        "claude", native, since=0.0, now=sup.STALL_AFTER_S * 10, home=home
    )
    assert stalled is False and "grew" in why


def test_an_engine_with_NO_transcript_adapter_is_never_called_stalled(tmp_path):
    """`shell` has no transcript at all. An engine we cannot measure is not an engine that has
    stopped — reporting it as stuck would manufacture the same false negative #801 spent seven
    rounds removing from the nudge matrix."""
    stalled, why, _turns = sup.session_is_stalled(
        "shell", "abc", since=0.0, now=sup.STALL_AFTER_S * 10, home=tmp_path
    )
    assert stalled is False and "no transcript signal" in why


# ---- the model half is GATED; the mechanical half is not -----------------------------------


@pytest.fixture
def configured(monkeypatch):
    from agent_sessions import review

    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    return review


def _input(monkeypatch, review, body="transcript", fp="fp1"):
    monkeypatch.setattr(review, "gather_input", lambda *a, **k: (body, fp))


def _reply(monkeypatch, review, obj):
    calls = []

    async def fake(messages, **kw):
        calls.append(messages)
        return obj

    monkeypatch.setattr(review, "complete_json", fake)
    return calls


@pytest.mark.anyio
async def test_UNCHANGED_input_makes_NO_model_call(store, configured, monkeypatch):
    """Asserted on the CALL, not on the stored row.

    A recap of an unchanged transcript costs the operator money to be told nothing twice, and the
    fingerprint moves exactly when the reviewable content does.
    """
    _input(monkeypatch, configured, fp="same")
    calls = _reply(monkeypatch, configured, {"recap": "r", "assessment": "on_track"})
    missions.advance_checkpoint(
        store, session_key=SESSION, input_fp="same", recap_text="already said this"
    )

    out = await sup.consider(store, SESSION)
    assert "skipped" in out and "not changed" in out["skipped"]
    assert calls == [], "the model was asked about an unchanged session"


@pytest.mark.anyio
async def test_the_MECHANICAL_half_still_runs_when_the_model_half_is_skipped(
    store, configured, monkeypatch
):
    """The trap #885 names. The fingerprint tracks the TRANSCRIPT; a gate becoming met changes
    the OBJECTIVE store and moves no fingerprint at all. If the skip covered both halves, a
    mission would finish and nobody would say so."""
    _objective(store, "pr_open")
    _input(monkeypatch, configured, fp="same")
    _reply(monkeypatch, configured, {"recap": "r", "assessment": "on_track"})
    missions.advance_checkpoint(store, session_key=SESSION, input_fp="same")

    assert (await sup.consider(store, SESSION)).get("skipped")

    # THE ASSERTION THAT BITES: `assess` must not consult the checkpoint AT ALL. Reading it is
    # the only way it could come to be gated on the fingerprint, so make touching it fatal.
    # (Asserting "the reading is still correct after a skip" documents the architecture but
    # cannot catch the trap — `assess` is a separate function, so gutting `consider`'s gate
    # leaves it untouched either way.)
    def _forbidden(*a, **k):
        raise AssertionError("the mechanical half consulted the recap checkpoint")

    monkeypatch.setattr(missions, "supervisor_checkpoint", _forbidden)
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    a = sup.assess(store)
    assert a["likely_done"] is True and a["unmet_gates"] == 0


@pytest.mark.anyio
async def test_a_nudge_naming_an_UNKNOWN_objective_is_dropped_not_remapped(
    store, configured, monkeypatch
):
    """Choosing WHICH objective is the whole of the model's authority here.

    Mapping an unknown key onto the nearest real one would quietly exceed that authority — the
    same reason `mission_objectives` refuses a model-authored probe rather than sanitising it.
    """
    _objective(store, "pr_open")
    _input(monkeypatch, configured)
    _reply(
        monkeypatch,
        configured,
        {
            "recap": "r",
            "assessment": "stalled",
            "nudge": {"objective_key": "not_a_key", "why": "x"},
        },
    )
    out = await sup.consider(store, SESSION)
    assert out["nudge"] is None, "an unknown objective key was accepted"

    _reply(
        monkeypatch,
        configured,
        {"recap": "r", "assessment": "stalled", "nudge": {"objective_key": "pr_open", "why": "x"}},
    )
    out = await sup.consider(store, SESSION)
    assert out["nudge"] == {"objective_key": "pr_open", "why": "x"}


@pytest.mark.anyio
async def test_an_UNKNOWN_assessment_falls_back_to_the_quiet_one(store, configured, monkeypatch):
    """A reply that stops matching the contract must not become an alarming state by accident.
    `on_track` proposes nothing, which is the safe reading of an answer nobody can parse."""
    _objective(store, "pr_open")
    _input(monkeypatch, configured)
    _reply(monkeypatch, configured, {"recap": "r", "assessment": "PANIC"})
    assert (await sup.consider(store, SESSION))["assessment"] == "on_track"


@pytest.mark.anyio
async def test_the_model_half_NEVER_marks_an_objective_met(store, configured, monkeypatch):
    """`likely_done` is a proposal. Gates are settled by observation, and a transcript reading is
    not an observation."""
    _objective(store, "pr_open")
    _input(monkeypatch, configured)
    _reply(monkeypatch, configured, {"recap": "r", "assessment": "likely_done"})
    await sup.consider(store, SESSION)
    assert [o["state"] for o in missions.objectives(store)] == ["pending"]


@pytest.mark.anyio
async def test_an_unconfigured_endpoint_skips_QUIETLY(store, monkeypatch):
    """The console keeps working and the operator is simply not paying for a recap."""
    from agent_sessions import review

    monkeypatch.setattr(
        review,
        "_require_config",
        lambda: (_ for _ in ()).throw(review.NotConfiguredError("no endpoint")),
    )
    out = await sup.consider(store, SESSION)
    assert out == {"skipped": "no AI endpoint is configured"}


@pytest.mark.anyio
async def test_the_supervisor_prompt_goes_through_the_REGISTRY(store, configured, monkeypatch):
    """Every system prompt comes from `prompts.effective`, and the guarded suffix rides last."""
    from agent_sessions import prompts

    _objective(store, "pr_open")
    _input(monkeypatch, configured)
    calls = _reply(monkeypatch, configured, {"recap": "r", "assessment": "on_track"})
    await sup.consider(store, SESSION)
    assert calls, "no model call was made"
    system = calls[0][0]
    assert system["role"] == "system"
    assert system["content"] == prompts.effective("mission_supervisor")


# ---- acting on the reading: one verb, one escalation, no new entry point --------------------


def test_the_supervisor_may_mint_only_CONTINUE_and_a_never_auto_DRAFT_DIRECTION(store):
    """`choose` and `answer` type an ANSWER into a prompt.

    A supervisor that guessed which option an agent should pick would be making the operator's
    decision for them, so `needs_approval` escalates instead of becoming bytes. Asserted on the
    verb the module can mint, not on the label the model returned (#885).

    **Changed on purpose by #983 P3.** This test was `…may_mint_only_CONTINUE`. The supervisor may
    now also mint `draft_direction`: model-authored text that is only ever a proposal. It widens
    what may be MINTED for approval and nothing about what may auto-deliver. `continue` stays the
    one auto-capable verb, and `draft_direction` is outside the autonomy ceiling, outside the
    orchestrator's vocabulary, and refused by every automatic path (pinned in
    `tests/test_mission_drafts.py`).
    """
    from agent_sessions import actuator, orchestrator, prefs

    assert sup.NUDGE_VERB == "continue"
    assert sup.NUDGE_VERB in orchestrator.DELIVERING_VERBS
    assert sup.NUDGE_VERB in prefs.AUTO_VERBS_V1

    assert sup.DRAFT_VERB == prefs.DRAFT_DIRECTION_VERB == "draft_direction"
    assert sup.DRAFT_VERB not in prefs.AUTO_VERBS_V1, "an AI draft became auto-deliverable"
    assert sup.DRAFT_VERB not in prefs.ORCH_VERBS, "the orchestrator pass can name a draft"
    assert sup.DRAFT_VERB not in orchestrator.DELIVERING_VERBS
    assert sup.DRAFT_VERB in actuator.RENDERABLE_VERBS  # an operator's approval can deliver it

    # The two verbs are the WHOLE vocabulary: no other verb literal is minted here.
    import ast

    src = (__import__("pathlib").Path(sup.__file__)).read_text()
    for forbidden in ('"choose"', '"answer"', '"relay"', '"escalate"'):
        assert forbidden not in src, f"the supervisor can mint {forbidden}"
    minted: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "verb":
                minted.add(getattr(value, "id", None) or repr(getattr(value, "value", value)))
    assert minted == {"NUDGE_VERB", "DRAFT_VERB"}, minted


@pytest.mark.anyio
async def test_a_nudge_REFUSED_by_the_budget_sends_nothing_and_says_why(store):
    _real_objective(store)
    # The mission has to HOLD the session: an automatic nudge is re-authorized against current
    # ownership at both fences (#888 review, finding 1), so a mission that never adopted it is
    # correctly refused before the budget is even consulted.
    missions.adopt(store, SESSION)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    out = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert out["sent"] is False and "budget" in out["why"]


@pytest.mark.anyio
async def test_the_BINDING_is_written_before_the_action(store, monkeypatch):
    """Ordering is what makes the budget survive a crash.

    A binding with no ledger row reads `indeterminate` — charged, and the automatic attempts stop
    — which is the honest answer for a write nobody can account for. The reverse order loses the
    charge entirely and the supervisor would nudge again.
    """
    from agent_sessions import orchestrator

    _real_objective(store)
    missions.adopt(store, SESSION)

    def die(*a, **k):
        raise RuntimeError("crash between the binding and the append")

    monkeypatch.setattr(orchestrator, "_persist", die)
    with pytest.raises(RuntimeError):
        await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")

    ids = missions.supervisor_action_ids(store, KEY, 1)
    assert len(ids) == 1, "the binding was not written before the append"
    b = sup.budget_state(store, KEY)
    assert (b["spent"], b["indeterminate"]) == (1, True)
    assert sup.may_nudge(store, KEY)[0] is False


@pytest.mark.anyio
async def test_only_ONE_pass_wins_an_escalation_and_only_it_announces(store):
    """The uniqueness constraint arbitrates. Announcing a row somebody else wrote is how one
    stall becomes three notifications."""
    _ensure_objective(store)
    kw = {"session_key": SESSION, "objective_key": KEY, "reason": "nothing moved"}
    assert await sup.escalate(store, **kw) is True
    assert await sup.escalate(store, **kw) is False

    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "escalation"]
    assert len(events) == 1, f"the escalation was announced {len(events)} times"
    assert "nothing moved" in (events[0].get("text") or "")


@pytest.mark.anyio
async def test_a_new_EPISODE_may_escalate_again(store):
    """Easing and then stalling later is a new episode, not a continuation of one the operator
    has already seen."""
    _ensure_objective(store)
    kw = {"session_key": SESSION, "objective_key": KEY, "reason": "nothing moved"}
    assert await sup.escalate(store, **kw) is True
    missions.bump_episode(store, KEY)
    assert await sup.escalate(store, **kw) is True


# ---- one pass over one mission --------------------------------------------------------------


def _running(store):
    missions.set_state(store, "draft", "planned")
    missions.set_state(store, "planned", "dispatching")
    missions.set_state(store, "dispatching", "running")
    missions.adopt(store, SESSION)


@pytest.mark.anyio
async def test_a_COMPLETED_checklist_proposes_review_with_NO_model_call(
    store, configured, monkeypatch
):
    """The trap, end to end.

    The checklist finishing is exactly the moment the input is most likely to be UNCHANGED — the
    agent stopped writing because it was done. Gating the completion proposal on the fingerprint
    would swallow the one event the operator most needs. So it is reached mechanically, before the
    gate, and this asserts on the CALL.
    """
    _running(store)
    _objective(store, "pr_open")
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    _input(monkeypatch, configured, fp="unchanged")
    calls = _reply(monkeypatch, configured, {"recap": "r", "assessment": "on_track"})
    missions.advance_checkpoint(store, session_key=SESSION, input_fp="unchanged")

    out = await sup.run_pass(store)
    assert out.get("likely_done") is True and out.get("proposed_review") is True
    assert calls == [], "the model was asked about a mission that was already done"
    assert missions.get_mission(store)["state"] == "review"
    # …and it PROPOSED, it did not close.
    assert missions.get_mission(store)["state"] != "done"


@pytest.mark.anyio
async def test_the_pass_SKIPS_a_mission_that_is_not_running(store, configured, monkeypatch):
    """A closed mission needs no follow-through, and a draft has no session to nudge."""
    calls = _reply(monkeypatch, configured, {"recap": "r", "assessment": "on_track"})
    out = await sup.run_pass(store)  # still `draft`
    assert "skipped" in out and calls == []


@pytest.mark.anyio
async def test_NEEDS_APPROVAL_never_becomes_bytes(store, configured, monkeypatch):
    """It is the operator's decision. A supervisor that answered it would be making that decision
    for them, so the nudge is dropped even though the model named a real objective."""
    _running(store)
    _objective(store, "pr_open")
    _input(monkeypatch, configured)
    _reply(
        monkeypatch,
        configured,
        {
            "recap": "waiting on a call",
            "assessment": "needs_approval",
            "nudge": {"objective_key": "pr_open", "why": "poke it"},
        },
    )
    sent = []
    monkeypatch.setattr(sup, "nudge", lambda *a, **k: sent.append(k) or {"sent": True})

    out = await sup.run_pass(store)
    assert out["assessment"] == "needs_approval"
    assert sent == [], "a nudge was sent for a decision only the operator can make"


@pytest.mark.anyio
async def test_the_recap_and_the_checkpoint_advance_in_the_pass(store, configured, monkeypatch):
    _running(store)
    _objective(store, "pr_open")
    _input(monkeypatch, configured, fp="moved")
    _reply(monkeypatch, configured, {"recap": "the branch appeared", "assessment": "on_track"})

    await sup.run_pass(store)
    cp = missions.supervisor_checkpoint(store, session_key=SESSION)
    assert cp["input_fp"] == "moved" and cp["recap_seq"] is not None
    recaps = [e for e in missions.get_mission(store)["events"] if e["kind"] == "recap"]
    assert len(recaps) == 1 and "branch appeared" in (recaps[0].get("text") or "")


@pytest.mark.anyio
async def test_a_SPENT_budget_escalates_once_and_rings_the_bell(store, configured, monkeypatch):
    _running(store)
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")
    _reply(monkeypatch, configured, {"recap": "still stuck", "assessment": "stalled"})

    rung = []
    from agent_sessions import notifications

    monkeypatch.setattr(notifications, "add", lambda **kw: rung.append(kw))

    out = await sup.run_pass(store)
    assert out["escalated"]["objective_key"] == KEY
    assert len(rung) == 1 and rung[0]["escalation"] is True

    # A second pass must NOT re-announce: the database arbitrates, so the bell cannot fill with
    # one unchanged situation.
    _input(monkeypatch, configured, fp="moved-again")
    out2 = await sup.run_pass(store)
    assert out2["escalated"] is None
    assert len(rung) == 1, "one stall was announced twice"


# ---- the sweep -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_sweep_is_OFF_when_the_orchestrator_is(store, monkeypatch):
    """The supervisor nudges THROUGH the orchestrator's verb path, so it inherits its master
    switch rather than adding a second one the operator has to find and remember."""
    from agent_sessions import mission_supervisor_loop as loop
    from agent_sessions import prefs

    ran = []
    monkeypatch.setattr(loop.mission_supervisor, "run_pass", lambda *a, **k: ran.append(a))

    monkeypatch.setattr(prefs, "get_orchestrator", lambda: {"enabled": False, "autonomy": "yolo"})
    assert (await loop.sweep())["skipped"] == "disabled"

    monkeypatch.setattr(prefs, "get_orchestrator", lambda: {"enabled": True, "autonomy": "off"})
    assert (await loop.sweep())["skipped"] == "disabled"
    assert ran == []


@pytest.mark.anyio
async def test_the_env_kill_switch_beats_prefs(store, monkeypatch):
    from agent_sessions import mission_supervisor_loop as loop
    from agent_sessions import prefs

    monkeypatch.setattr(prefs, "get_orchestrator", lambda: {"enabled": True, "autonomy": "yolo"})
    monkeypatch.setenv("AGENT_SESSIONS_MISSION_SUPERVISOR", "0")
    assert (await loop.sweep())["skipped"] == "disabled"


@pytest.mark.anyio
async def test_one_stuck_mission_does_not_stop_the_FLEET(store, monkeypatch):
    """The whole complaint this feature answers is "one thing stalls and nothing else happens".
    A supervisor that stops sweeping because one mission raised would reproduce it exactly."""
    from agent_sessions import mission_supervisor_loop as loop
    from agent_sessions import prefs

    _running(store)
    other = missions.create_mission("second", cwd="/tmp")["id"]
    missions.set_state(other, "draft", "planned")
    missions.set_state(other, "planned", "dispatching")
    missions.set_state(other, "dispatching", "running")

    monkeypatch.setattr(prefs, "get_orchestrator", lambda: {"enabled": True, "autonomy": "yolo"})
    seen = []

    async def flaky(mid, **kw):
        seen.append(mid)
        if len(seen) == 1:
            raise RuntimeError("this one is broken")
        return {"nudged": None, "escalated": None}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", flaky)
    report = await loop.sweep()
    assert len(seen) == 2, "the sweep stopped at the first failure"
    assert report["swept"] == 1


# ---- #892: when it is unsure, it ASKS ---------------------------------------------------------


def _two_replies(monkeypatch, review, *, recap, question):
    """One `complete_json` stub for two different calls, told apart by the SYSTEM PROMPT.

    Told apart by which registry prompt was sent rather than by call ORDER: an order-keyed stub
    passes just as happily when the two calls swap, which is exactly the mistake a test about
    "the escalation happens, and then the question" must not make.
    """
    from agent_sessions import prompts

    question_prompt = prompts.effective("mission_question")
    seen: list[str] = []

    async def fake(messages, **kw):
        system = str(messages[0].get("content") or "")
        if system == question_prompt:
            seen.append("question")
            return question
        seen.append("recap")
        return recap

    monkeypatch.setattr(review, "complete_json", fake)
    return seen


_A_QUESTION = {
    "question": "Which of the two open PRs is this mission's?",
    "options": [
        {"label": "The one from Tuesday", "action_index": 0},
        {"label": "Neither — this does not apply here", "action_index": 1},
    ],
}


@pytest.mark.anyio
async def test_a_SPENT_budget_escalates_AND_THEN_ASKS(store, configured, monkeypatch):
    """The producer #840 asked for, wired to the one moment it described.

    An escalation says SOMETHING is wrong without saying what would fix it — which is #840's own
    complaint about it. So the terminal escalation is now followed by a bounded question about the
    same objective, and the question supersedes it: standing the objective down drops the vague
    reason, and the answerable one takes its place. The mission is flagged throughout; it never
    blinks through "fine".
    """
    _running(store)
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")
    seen = _two_replies(
        monkeypatch,
        configured,
        recap={"recap": "still stuck", "assessment": "stalled"},
        question=_A_QUESTION,
    )

    out = await sup.run_pass(store)
    assert out["escalated"]["objective_key"] == KEY
    assert out["asked"]["objective_key"] == KEY, "the escalation was not followed by a question"
    assert seen == ["recap", "question"], seen

    q = missions.open_question_row(store)
    assert q is not None and q["objective"] == KEY
    # The ACTIONS came from the closed set by index; the labels are display text.
    assert [o["action"] for o in q["options"]] == ["note_answer", "waive_objective"]

    why = missions.derive_needs_you([store])[store]
    assert why["needs_you"] is True
    assert "question" in why["why"], why
    assert "escalation" not in why["why"], "the vague reason outlived the answerable one"


@pytest.mark.anyio
async def test_a_question_that_COULD_NOT_BE_PRODUCED_leaves_the_escalation_standing(
    store, configured, monkeypatch
):
    """Asking degrades to the status quo, never to silence.

    A fresh install has no AI endpoint and a configured one can answer with something unusable.
    In both cases the operator must still be told the mission needs them — which is why the ask
    is hung off the escalation rather than replacing it.
    """
    _running(store)
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")
    _two_replies(
        monkeypatch,
        configured,
        recap={"recap": "still stuck", "assessment": "stalled"},
        question={"question": "Which?", "options": [{"label": "only one", "action_index": 0}]},
    )

    out = await sup.run_pass(store)
    assert out["escalated"]["objective_key"] == KEY
    assert out["asked"] is None
    assert missions.open_question_row(store) is None
    why = missions.derive_needs_you([store])[store]
    assert why["needs_you"] is True and "escalation" in why["why"]
    # …and it SAID SO on the timeline, under `error` rather than `question`: a "could not ask"
    # filed as a question would flag the mission for ever with nothing on screen to answer.
    kinds = [e["kind"] for e in missions.get_mission(store)["events"]]
    assert "error" in kinds and "question" not in kinds


@pytest.mark.anyio
async def test_ONE_question_per_objective_EPISODE(store, configured, monkeypatch):
    """Bounded without a second counter to keep honest.

    `escalate_once` already arbitrates on `(mission, objective, episode)`, and the ask hangs off
    its win — so a mission that is asked and not answered is not asked again. Questions that keep
    arriving train the operator to ignore them, which costs more than the feature is worth.
    """
    _running(store)
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")
    _two_replies(
        monkeypatch,
        configured,
        recap={"recap": "still stuck", "assessment": "stalled"},
        question=_A_QUESTION,
    )

    first = await sup.run_pass(store)
    assert first["asked"] is not None
    _input(monkeypatch, configured, fp="moved-again")
    second = await sup.run_pass(store)
    assert second["asked"] is None, "a second question arrived for the same episode"
    questions = [e for e in missions.get_mission(store)["events"] if e["kind"] == "question"]
    assert len(questions) == 1, f"asked {len(questions)} times about one episode"


@pytest.mark.anyio
async def test_a_question_on_ONE_objective_does_not_stall_the_others(
    store, configured, monkeypatch
):
    """The reason a question stands down the OBJECTIVE and not the mission.

    A mission may have five objectives and be stuck on one. Silencing the whole mission would
    stall follow-through on the other four — so the pass keeps nudging the objective nobody is
    being asked about.
    """
    _running(store)
    _objective(store, KEY)
    _objective(store, "other_thing")
    missions.open_question(
        store,
        KEY,
        "Which of the two open PRs is this mission's?",
        [
            {"label": "The one from Tuesday", "action": "note_answer"},
            {"label": "Neither", "action": "waive_objective"},
        ],
    )
    _input(monkeypatch, configured, fp="moved")
    _reply(
        monkeypatch,
        configured,
        {
            "recap": "working",
            "assessment": "blocked",
            "nudge": {"objective_key": "other_thing", "why": "no movement on the other one"},
        },
    )
    sent: list[dict] = []

    async def _nudge(*a, **k):
        sent.append(k)
        return {"sent": True}

    monkeypatch.setattr(sup, "nudge", _nudge)

    out = await sup.run_pass(store)
    assert out.get("skipped") is None, f"the whole mission stopped: {out.get('skipped')}"
    assert sent and sent[0]["objective_key"] == "other_thing", sent


@pytest.mark.anyio
async def test_an_ASK_THAT_PRODUCED_NOTHING_is_retried_on_a_later_pass(
    store, configured, monkeypatch
):
    """#900 review 4, finding 1, and the reason the escalation's uniqueness is the wrong hook.

    `escalate_once` is arbitrated on `(mission, objective, episode)`, so the pass that WINS it is
    the only pass that ever entered the ask branch. An ask that produced nothing — a busy
    authority fence, an unconfigured endpoint, an unusable reply — therefore lost the episode's
    only question permanently: the operator kept the vague escalation and never got the concrete
    choice, on an objective the supervisor had already decided it could not resolve alone.

    Red against an ask hung off the escalation's win.
    """
    from agent_sessions import prompts

    _running(store)
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")

    question_prompt = prompts.effective("mission_question")
    asks = {"n": 0}

    async def fake(messages, **kw):
        if str(messages[0].get("content") or "") == question_prompt:
            asks["n"] += 1
            # The FIRST ask is unusable — one option is not a choice, so nothing opens.
            if asks["n"] == 1:
                return {"question": "Which?", "options": [{"label": "one", "action_index": 0}]}
            return _A_QUESTION
        return {"recap": "still stuck", "assessment": "stalled"}

    monkeypatch.setattr(configured, "complete_json", fake)

    first = await sup.run_pass(store)
    assert first["escalated"]["objective_key"] == KEY
    assert first["asked"] is None
    assert missions.open_question_row(store) is None

    # THE SECOND PASS. The escalation already exists, so `escalate_once` loses — and that must not
    # be what decides whether the operator gets a question.
    _input(monkeypatch, configured, fp="moved-again")
    second = await sup.run_pass(store)
    assert second["escalated"] is None, "the escalation is still once per episode"
    assert second["asked"] is not None, "the episode's only ask was lost to one bad reply"
    q = missions.open_question_row(store)
    assert q is not None and q["objective"] == KEY
    assert asks["n"] == 2

    # …and the bound still holds: with a question open, a THIRD pass asks nothing.
    _input(monkeypatch, configured, fp="moved-thrice")
    third = await sup.run_pass(store)
    assert third["asked"] is None
    questions = [e for e in missions.get_mission(store)["events"] if e["kind"] == "question"]
    assert len(questions) == 1, f"asked {len(questions)} times about one episode"


@pytest.mark.anyio
async def test_ONE_UNANSWERABLE_objective_does_not_starve_the_others(
    store, configured, monkeypatch
):
    """#900 review 5, finding 5. `_ask_owed` returned after the first eligible objective whatever
    happened, so one persistently unanswerable objective — an endpoint that keeps refusing, a
    fence that keeps being busy — starved every later owed one: each pass retried the same fixed
    prefix and the second was never asked about at all.

    Red against a return that is not conditional on the ask having landed.
    """
    from agent_sessions import prompts

    _running(store)
    _objective(store, KEY)
    _objective(store, "second")
    for i in range(sup.NUDGE_BUDGET):
        _sent(store, f"d{i}", "delivered")
    _input(monkeypatch, configured, fp="moved")
    # BOTH ALREADY ESCALATED — which is the state the starvation needs and the one a mission
    # reaches over successive episodes. The pass skips (the mission needs the operator), so the
    # only thing that runs is `_ask_owed`, which is precisely what this is about.
    for key in (KEY, "second"):
        assert missions.escalate_once(
            store,
            session_key=SESSION,
            objective_key=key,
            episode=1,
            reason=f"{key} has not moved",
        )

    question_prompt = prompts.effective("mission_question")
    asked_about: list[str] = []

    async def fake(messages, **kw):
        if str(messages[0].get("content") or "") == question_prompt:
            body = "\n".join(str(mm.get("content") or "") for mm in messages)
            key = KEY if f"Objective in question: {KEY}" in body else "second"
            asked_about.append(key)
            # The FIRST objective is permanently unanswerable; the second is fine.
            if key == KEY:
                return {"question": "Which?", "options": [{"label": "one", "action_index": 0}]}
            return _A_QUESTION
        return {"recap": "still stuck", "assessment": "stalled"}

    monkeypatch.setattr(configured, "complete_json", fake)

    out = await sup.run_pass(store)
    # BOTH were tried in the same pass, and the one that could produce a question did.
    assert KEY in asked_about and "second" in asked_about, asked_about
    assert out["asked"] is not None and out["asked"]["objective_key"] == "second"
    q = missions.open_question_row(store)
    assert q is not None and q["objective"] == "second"
