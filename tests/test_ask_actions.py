"""Ask actions (#1086 Phase 3): editing a text decision before approving it, dismissing a row
until its screen changes, the read-only details, the badge surface, and the orchestrator's
roots/exclusions eligibility gate."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    missions,
    needs_you_dismissals,
    notifications,
    orchestrator,
    orchestrator_ledger,
    prefs,
    pulse,
    review,
)
from agent_sessions.main import create_app
from agent_sessions.routes import pulse as pulse_routes


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def _action(aid="act-1", *, sid="claude:a", verb="answer", state="proposed", **extra):
    rec = {
        "id": aid,
        "state": state,
        "session_id": sid,
        "verb": verb,
        "answer": "Use the second option.",
        "engine": "claude",
        "title": "t",
        **extra,
    }
    orchestrator_ledger.append(rec)
    return rec


@pytest.fixture
def delivered(monkeypatch):
    """`deliver` is the fence; it is covered elsewhere. Here: WHAT it is asked to deliver."""
    seen: list[dict] = []

    async def fake_deliver(action_id, **kw):
        # What the REAL claim writes: the edit rides the claim itself (review 5184).
        rec = {**orchestrator_ledger.get(action_id), **(kw.get("edit") or {})}
        seen.append(dict(rec))
        return {**rec, "state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", fake_deliver)
    return seen


# ---- approve with edited text --------------------------------------------------------------------


def test_a_bodyless_approve_is_exactly_what_it_was(auth_cfg, fake_jsonl, delivered):
    _action()
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/pulse/actions/act-1/approve", headers=hdr)
    assert r.status_code == 200
    assert delivered[0]["verb"] == "answer" and "operator_edited" not in delivered[0]


def test_edited_text_becomes_a_RELAY_with_both_texts_recorded(auth_cfg, fake_jsonl, delivered):
    _action()
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/approve",
        json={"text": "Use option two, and add a unit test."},
        headers=hdr,
    )
    assert r.status_code == 200
    sent = delivered[0]
    # Operator-authored bytes are a `relay`, never laundered through the model's verb.
    assert sent["verb"] == "relay" and sent["origin"] == "operator"
    assert sent["answer"] == sent["sent_text"] == "Use option two, and add a unit test."
    assert sent["suggested_text"] == "Use the second option."
    assert sent["suggested_verb"] == "answer" and sent["operator_edited"] is True


def test_text_identical_to_the_suggestion_changes_nothing(auth_cfg, fake_jsonl, delivered):
    _action()
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/approve", json={"text": "Use the second option."}, headers=hdr
    )
    assert r.status_code == 200 and delivered[0]["verb"] == "answer"


def test_a_continue_can_be_edited_too(auth_cfg, fake_jsonl, delivered):
    _action(verb="continue")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/approve", json={"text": "Carry on, then push."}, headers=hdr
    )
    assert r.status_code == 200
    assert delivered[0]["verb"] == "relay"
    assert delivered[0]["suggested_text"] == actuator.default_nudge_text(prefs.get_orchestrator())


@pytest.mark.parametrize(
    ("body", "setup", "status"),
    [
        ({"text": 5}, {}, 422),
        ({"text": "x" * (pulse_routes.EDIT_TEXT_MAX + 1)}, {}, 422),
        ({"text": "   "}, {}, 422),
        ({"text": "\x1b\x07\x00"}, {}, 422),  # nothing survives the sanitiser
        ({"text": "hi", "extra": 1}, {}, 422),
        ({"text": "hi"}, {"verb": "choose", "option": 2}, 422),  # a choice is a digit
        ({"text": "hi"}, {"mission_id": "msn_1"}, 422),  # checked against its objectives
        ({"text": "hi"}, {"state": "rejected"}, 409),
    ],
    ids=[
        "not-a-string",
        "too-long",
        "blank",
        "sanitised-away",
        "unknown-key",
        "choose",
        "mission",
        "too-late",
    ],
)
def test_the_edit_refusal_matrix_delivers_nothing(
    auth_cfg, fake_jsonl, delivered, body, setup, status
):
    _action(**setup)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/pulse/actions/act-1/approve", json=body, headers=hdr)
    assert r.status_code == status, r.text
    assert delivered == []
    assert orchestrator_ledger.get("act-1").get("verb") == setup.get("verb", "answer")


def test_an_edit_is_never_written_to_the_pending_proposal(auth_cfg, fake_jsonl, delivered):
    """Review 5184: the edit rides the delivery claim; the proposal itself is never rewritten, so no
    other approval can ever read (or send) a half-applied edit."""
    _action()
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    c.post("/api/pulse/actions/act-1/approve", json={"text": "mine"}, headers=hdr)
    stored = orchestrator_ledger.get("act-1")
    assert stored["verb"] == "answer" and "sent_text" not in stored  # the fake never claimed


# ---- the claim is the one place an edit lands: real `deliver`, interrupted mid-render -----------


@pytest.fixture
def real_deliver_env(monkeypatch):
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    monkeypatch.setattr(actuator.session_input, "is_live", lambda key: True)
    real_render = actuator.render
    hooks: list = []

    def render(action, cfg):
        if hooks:
            hooks.pop(0)()  # a competing approval lands WHILE this one renders
        return real_render(action, cfg)

    monkeypatch.setattr(actuator, "render", render)
    return hooks


def _edit(text):
    return {
        "verb": "relay",
        "answer": text,
        "origin": "operator",
        "operator_edited": True,
        "suggested_verb": "answer",
        "suggested_text": "Use the second option.",
        "sent_text": text,
    }


def _competing_claim(**fields):
    ts = orchestrator_ledger.get("act-1")["ts"]
    return lambda: orchestrator_ledger.claim(
        "act-1", orchestrator_ledger.CLAIMABLE_STATES, expect_ts=ts, **fields
    )


def test_edited_vs_edited_the_loser_sends_nothing_and_never_the_others_text(real_deliver_env):
    import asyncio

    _action()
    real_deliver_env.append(_competing_claim(**_edit("B's words")))
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act-1", operator_approval=True, edit=_edit("A's words")))
    rec = orchestrator_ledger.get("act-1")
    assert rec["state"] == "claimed" and rec["sent_text"] == rec["answer"] == "B's words"


def test_an_unedited_approve_loses_to_an_edit_claimed_mid_render(real_deliver_env):
    import asyncio

    _action()
    real_deliver_env.append(_competing_claim(**_edit("B's words")))
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act-1", operator_approval=True))
    rec = orchestrator_ledger.get("act-1")
    # The record says B's words were claimed, and nothing rendered from the ORIGINAL was sent.
    assert rec["verb"] == "relay" and rec["sent_text"] == "B's words"


def test_an_edit_loses_to_an_unedited_approve_claimed_mid_render(real_deliver_env):
    import asyncio

    _action()
    real_deliver_env.append(_competing_claim())
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act-1", operator_approval=True, edit=_edit("A's words")))
    rec = orchestrator_ledger.get("act-1")
    assert rec["verb"] == "answer" and "sent_text" not in rec  # the audit trail cannot lie


def test_an_edit_needs_the_operators_own_approval(real_deliver_env):
    import asyncio

    _action()
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act-1", edit=_edit("x")))
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_editing_requires_csrf(auth_cfg, fake_jsonl, delivered):
    _action()
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/pulse/actions/act-1/approve",
        json={"text": "x"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403 and delivered == []


# ---- the standalone session routes ---------------------------------------------------------------


def _card(sid="claude:aaaaaaaa-0000-0000-0000-00000000000a", cwd="/work/a", flagged=True):
    return {
        "id": sid,
        "engine": "claude",
        "title": f"title {sid}",
        "cwd": cwd,
        "project": {"kind": "folder", "id": "p1", "name": "Alpha"},
        "last_activity": time.time() - 60,
        "intervention_required": flagged,
        "intervention_reason": "asked a question",
        "ai_summary": "s",
    }


@pytest.fixture
def world(monkeypatch):
    state = {
        "cards": [_card()],
        "held": {},
        "screen": {
            "prompt_class": "question",
            "menu": None,
            "fingerprint": "fp-1",
            "screen": "? x",
        },
        "reads": [],
    }
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [dict(c) for c in state["cards"]])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: state["held"])

    def screen(key):
        state["reads"].append(key)
        return dict(state["screen"])

    monkeypatch.setattr(orchestrator, "observed_screen", screen)
    monkeypatch.setattr(review, "last_words", lambda key, n=1500: "Which option do you want?")
    return state


def test_details_are_read_only_and_carry_the_editable_suggestion(auth_cfg, fake_jsonl, world):
    _action(sid="claude:aaaaaaaa-0000-0000-0000-00000000000a")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/details").json()
    assert d["last_words"] == "Which option do you want?"
    assert d["screen"] == "? x" and d["prompt_class"] == "question"
    assert d["action"]["editable"] is True
    assert d["action"]["suggested_text"] == "Use the second option."
    assert "precondition" not in d["action"]


@pytest.mark.parametrize(
    ("mutate", "status"),
    [
        (
            lambda w: w["held"].update({"claude:aaaaaaaa-0000-0000-0000-00000000000a": "msn_1"}),
            409,
        ),  # decided in its mission
        (lambda w: w["cards"].clear(), 404),  # no such session
    ],
    ids=["mission-held", "unknown"],
)
def test_the_standalone_routes_refuse_what_the_list_would_not_show(
    auth_cfg, fake_jsonl, world, mutate, status
):
    mutate(world)
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert (
        c.get(
            "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/details"
        ).status_code
        == status
    )
    assert (
        c.post(
            "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss", headers=hdr
        ).status_code
        == status
    )


def test_the_standalone_routes_honour_folder_exclusions(auth_cfg, fake_jsonl, world):
    world["cards"][:] = [_card(cwd="/secret/a")]
    prefs.set_folder_exclusions(["/secret"])
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert (
        c.get(
            "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/details"
        ).status_code
        == 404
    )
    assert (
        c.post(
            "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss", headers=hdr
        ).status_code
        == 404
    )


def test_a_dismissed_row_stays_hidden_until_its_SCREEN_changes(auth_cfg, fake_jsonl, world):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert [r["id"] for r in c.get("/api/pulse/needs-you").json()["rows"]] == [
        "claude:aaaaaaaa-0000-0000-0000-00000000000a"
    ]
    r = c.post(
        "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss", headers=hdr
    )
    assert r.status_code == 200 and r.json() == {"dismissed": True, "rejected": False}
    # The server read the fingerprint itself; the client never named a screen.
    assert needs_you_dismissals.suppressed() == {
        "claude:aaaaaaaa-0000-0000-0000-00000000000a": "fp-1"
    }
    assert c.get("/api/pulse/needs-you").json()["rows"] == []
    world["screen"]["fingerprint"] = "fp-2"  # it moved on: a new reason to need you
    assert [r["id"] for r in c.get("/api/pulse/needs-you").json()["rows"]] == [
        "claude:aaaaaaaa-0000-0000-0000-00000000000a"
    ]


def test_dismiss_rejects_the_named_decision_and_only_for_its_own_session(
    auth_cfg, fake_jsonl, world
):
    _action(sid="claude:aaaaaaaa-0000-0000-0000-00000000000b")
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    wrong = c.post(
        "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss",
        json={"action_id": "act-1"},
        headers=hdr,
    )
    assert wrong.status_code == 404
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"
    _action("act-2", sid="claude:aaaaaaaa-0000-0000-0000-00000000000a")
    ok = c.post(
        "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss",
        json={"action_id": "act-2"},
        headers=hdr,
    )
    assert ok.status_code == 200 and ok.json()["rejected"] is True
    assert orchestrator_ledger.get("act-2")["state"] == "rejected"


def test_dismiss_requires_csrf_and_refuses_extra_body_keys(auth_cfg, fake_jsonl, world):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert (
        c.post(
            "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss",
            headers={"Origin": auth_cfg.origin},
        ).status_code
        == 403
    )
    r = c.post(
        "/api/pulse/needs-you/claude:aaaaaaaa-0000-0000-0000-00000000000a/dismiss",
        json={"fingerprint": "mine"},
        headers=hdr,
    )
    assert r.status_code == 422  # a client may never name the screen


def test_a_live_decision_is_listed_WHATEVER_the_window(auth_cfg, fake_jsonl, world, monkeypatch):
    old = _card("claude:old", flagged=False)
    old["last_activity"] = time.time() - 10 * 86400

    def cards(*, window_days, now=None, working_keys=None):
        return [dict(old)] if window_days is None else []

    monkeypatch.setattr(pulse, "build_cards", cards)
    _action(sid="claude:old", expires_at=time.time() + 3600)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/pulse/needs-you", params={"window_days": 1}).json()["rows"]
    assert [r["id"] for r in rows] == ["claude:old"]


# ---- the badge surface ---------------------------------------------------------------------------


def test_every_session_has_a_surface_once_membership_reads(monkeypatch):
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {"claude:m": "msn"})
    surfaces = notifications.decision_surfaces()
    assert "claude:standalone" in surfaces and "claude:m" in surfaces
    # The PUSH gate keeps the mission-only set until Phase 4.
    assert notifications.mission_surfaces() == {"claude:m"}


def test_an_unreadable_membership_is_still_uncertain(monkeypatch):
    def boom(**_k):
        raise OSError("locked")

    monkeypatch.setattr(missions, "all_active_memberships", boom)
    assert notifications.decision_surfaces() is None
    assert notifications.mission_surfaces() is None


# ---- the orchestrator's eligibility honours the boundary -----------------------------------------


def test_the_orchestrator_never_proposes_into_an_excluded_folder(monkeypatch):
    cards = [_card("claude:in", cwd="/work/in"), _card("claude:out", cwd="/secret/out")]
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [dict(c) for c in cards])
    monkeypatch.setattr(orchestrator.engines, "orchestrator_input_engines", lambda: {"claude"})
    prefs.set_folder_exclusions(["/secret"])
    eligible, skipped = orchestrator.eligible_cards(now=time.time())
    assert [c["id"] for c in eligible] == ["claude:in"]
    assert skipped["scope"] == 1


def test_a_malformed_session_id_is_simply_unknown(auth_cfg, fake_jsonl, world):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    assert c.get("/api/pulse/needs-you/claude:not-a-uuid/details").status_code == 404
    assert c.post("/api/pulse/needs-you/../../etc/dismiss", headers=hdr).status_code in (404, 405)


@pytest.mark.parametrize(("boundary", "eligible"), [(False, True), (True, False)])
def test_a_card_with_no_cwd_follows_the_terminals_rule(monkeypatch, boundary, eligible):
    """No cwd = nothing to check: fail CLOSED where a boundary is configured, open where none is."""
    card = {"id": "claude:nocwd", "engine": "claude", "last_activity": time.time() - 60}
    monkeypatch.setattr(pulse, "build_cards", lambda **k: [dict(card)])
    monkeypatch.setattr(orchestrator.engines, "orchestrator_input_engines", lambda: {"claude"})
    if boundary:
        prefs.set_folder_exclusions(["/anything"])
    eligible_cards, skipped = orchestrator.eligible_cards(now=time.time())
    assert bool(eligible_cards) is eligible
    assert skipped["scope"] == (0 if eligible else 1)


def test_a_record_that_changed_mid_render_is_never_sent_even_in_the_same_state(real_deliver_env):
    """The REVISION half of review 5184: any event on the action after it was read — here a
    rewrite that leaves it `proposed`, the shape the old in-place edit had — makes the rendered
    view stale, so the claim refuses and nothing is typed."""
    import asyncio

    _action()
    real_deliver_env.append(
        lambda: orchestrator_ledger.append(
            {"id": "act-1", "state": "proposed", "answer": "changed"}
        )
    )
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act-1", operator_approval=True))
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


# ---- a decision whose surface disappears is withdrawn (review 5184, finding 6) ------------------


@pytest.fixture
def screen_ok(monkeypatch):
    """Isolate the NO-SURFACE rule from the live-screen rule next to it."""
    monkeypatch.setattr(actuator.session_input, "is_live", lambda key: True)
    monkeypatch.setattr(actuator, "screen_matches", lambda phys, pre: (True, ""))


A_ID = "claude:aaaaaaaa-0000-0000-0000-00000000000a"


def test_a_decision_for_a_session_archived_after_the_proposal_is_withdrawn(
    auth_cfg, fake_jsonl, world, screen_ok
):
    """The operator ARCHIVED it — a recorded fact in the sidecar, the evidence the rule acts on."""
    from agent_sessions import metadata

    world["cards"].clear()
    _action(sid=A_ID, state="escalated", verb="escalate")
    metadata.patch(A_ID, archived=True)
    notifications.add(
        title="t",
        project="p",
        reason="r",
        session_id=A_ID,
        engine="claude",
        action_id="act-1",
        escalation=True,
    )
    assert notifications.listing()["unread"] == 1  # the probe's state before any read
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/pulse/needs-you").json()["rows"] == []
    rec = orchestrator_ledger.get("act-1")
    assert rec["state"] == "stale" and rec["detail"] == actuator.NO_SURFACE_DETAIL
    assert notifications.listing()["unread"] == 0


def test_a_review_excluded_session_is_withdrawn_too(world, screen_ok):
    from agent_sessions import metadata

    _action(sid=A_ID)
    metadata.patch(A_ID, review_excluded=True)
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "stale"


def test_a_proposal_whose_folder_is_now_excluded_is_withdrawn(world, screen_ok):
    _action(sid=A_ID, cwd="/secret/proj")
    prefs.set_folder_exclusions(["/secret"])
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "stale"


def test_a_TRANSIENT_scan_failure_never_withdraws_a_valid_decision(world, screen_ok, monkeypatch):
    """Review 5188: scans fail soft, so an absence from one is not evidence. With the session's
    reads failing (no cards, an empty walk) and nothing recorded against it, the decision stays —
    and is still there once reads recover."""

    class _Known:
        key = A_ID

    # Hermes' exact shape: the walk SEES the session, while the second, independent scan inside
    # the card builder fails and returns nothing — "known but not listed" with nothing recorded.
    monkeypatch.setattr(actuator.engines, "scan_all", lambda: [_Known()])
    monkeypatch.setattr(actuator.engines, "session_key", lambda x: x.key)
    world["cards"].clear()
    _action(sid=A_ID, cwd="/work/a")
    assert actuator.withdraw_undeliverable() == []
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_an_unreadable_sidecar_is_no_evidence(world, screen_ok, tmp_path):
    from agent_sessions import metadata

    _action(sid=A_ID)
    metadata.patch(A_ID, archived=True)
    metadata._default_path().write_text("{ not json")  # corrupt → read as {} → no flags
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_a_mission_held_decision_is_never_withdrawn_for_this(world, screen_ok):
    world["cards"].clear()
    world["held"][A_ID] = "msn_1"
    _action(sid=A_ID)
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_an_unreadable_membership_withdraws_nothing(world, screen_ok, monkeypatch):
    def boom(**_k):
        raise OSError("locked")

    monkeypatch.setattr(missions, "all_active_memberships", boom)
    world["cards"].clear()
    _action(sid=A_ID)
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_a_listed_session_keeps_its_decision(world, screen_ok):
    _action(sid=A_ID)  # `world` lists A_ID by default
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"


def test_a_session_the_scan_does_not_know_keeps_its_decision(world, screen_ok, monkeypatch):
    """No card is NOT evidence of no surface: "a live action with no card is still reachable"."""
    monkeypatch.setattr(actuator.engines, "scan_all", lambda: [])
    world["cards"].clear()
    _action(sid=A_ID)
    actuator.withdraw_undeliverable()
    assert orchestrator_ledger.get("act-1")["state"] == "proposed"
