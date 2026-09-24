"""NEEDS YOU for sessions no mission holds (#1086): the pure join, then the route over it."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import missions, needs_you, orchestrator, orchestrator_ledger, pulse
from agent_sessions.main import create_app


def _card(
    sid, *, flagged=False, engine="claude", project=("p1", "Alpha"), reviewed=None, last=100.0
):
    return {
        "id": sid,
        "engine": engine,
        "title": f"title {sid}",
        "project": {"id": project[0], "name": project[1]},
        "last_activity": last,
        "ai_summary": f"summary {sid}",
        "intervention_required": flagged,
        "intervention_reason": "waiting on you" if flagged else None,
        "reviewed_at": reviewed,
        "cwd": f"/work/{sid.split(':')[-1]}",
    }


def _action(sid, *, ts, can_approve=True, verb="continue", **extra):
    return {
        "id": f"act-{sid}-{ts}",
        "session_id": sid,
        "ts": ts,
        "verb": verb,
        "state": "proposed",
        "projection": "actionable" if can_approve else "historical",
        "can_approve": can_approve,
        "can_reject": can_approve,
        "precondition": {"fingerprint": "INTERNAL"},
        **extra,
    }


def _observe_open(_row):
    return {"prompt_class": "open", "menu": None}


def test_a_session_a_mission_holds_is_never_listed():
    out = needs_you.build(
        [_card("claude:a", flagged=True), _card("claude:b", flagged=True)],
        [],
        {"claude:a"},
        observe=_observe_open,
    )
    assert [r["id"] for r in out["rows"]] == ["claude:b"]


def test_unreadable_membership_is_an_error_never_an_empty_list():
    with pytest.raises(needs_you.MembershipUnavailable):
        needs_you.build([_card("claude:a", flagged=True)], [], None, observe=_observe_open)


def test_flag_or_actionable_decision_qualifies_and_nothing_else_does():
    cards = [
        _card("claude:flag", flagged=True, reviewed=50.0),
        _card("claude:act"),
        _card("claude:settled"),
        _card("claude:quiet"),
    ]
    pending = [
        _action("claude:act", ts=70.0),
        _action("claude:settled", ts=90.0, can_approve=False),
    ]
    out = needs_you.build(cards, pending, set(), observe=_observe_open)
    # Newest first by WHEN it started needing you: the decision at 70, the review at 50.
    assert [r["id"] for r in out["rows"]] == ["claude:act", "claude:flag"]
    act = out["rows"][0]["action"]
    assert act["id"] == "act-claude:act-70.0" and act["can_approve"] is True
    # Whitelisted: internals of the ledger record never ride out on this polled read.
    assert "precondition" not in act and "session_id" not in act


def test_the_row_offers_the_NEWEST_actionable_decision():
    out = needs_you.build(
        [_card("claude:a")],
        [_action("claude:a", ts=10.0), _action("claude:a", ts=30.0), _action("claude:a", ts=20.0)],
        set(),
        observe=_observe_open,
    )
    assert out["rows"][0]["action"]["ts"] == 30.0


def test_facets_are_computed_BEFORE_the_filters():
    cards = [
        _card("claude:a", flagged=True, project=("p1", "Alpha")),
        _card("codex:b", flagged=True, engine="codex", project=("p2", "Beta")),
    ]
    out = needs_you.build(cards, [], set(), observe=_observe_open, engine="codex")
    assert [r["id"] for r in out["rows"]] == ["codex:b"]
    assert out["facets"]["engines"] == ["claude", "codex"]
    assert [p["id"] for p in out["facets"]["projects"]] == ["p1", "p2"]
    assert (out["total"], out["total_unfiltered"]) == (1, 2)
    # Membership ignores the filter: an answer can still mark the filtered-out session.
    assert sorted(out["needs_you_ids"]) == ["claude:a", "codex:b"]
    only_p1 = needs_you.build(cards, [], set(), observe=_observe_open, project="p1")
    assert [r["id"] for r in only_p1["rows"]] == ["claude:a"]


def test_kind_comes_from_the_screen_and_a_menu_wins():
    menu = {
        "engine": "claude",
        "question": "Proceed?",
        "options": [{"n": 1, "label": "Yes", "selected": True}],
    }
    screens = {
        "claude:menu": {"prompt_class": "choice", "menu": menu},
        "claude:confirm": {"prompt_class": "confirm", "menu": None},
        "claude:q": {"prompt_class": "question", "menu": None},
        "claude:open": {"prompt_class": "open", "menu": None},
    }
    cards = [_card(k, flagged=True, reviewed=float(i)) for i, k in enumerate(screens)]
    out = needs_you.build(cards, [], set(), observe=lambda r: screens[r["id"]])
    kinds = {r["id"]: (r["kind"], r["menu"]) for r in out["rows"]}
    assert kinds["claude:menu"] == ("choice", menu)
    assert kinds["claude:confirm"] == ("approval", None)
    assert kinds["claude:q"] == ("question", None)
    assert kinds["claude:open"] == ("needs_inspection", None)


def test_an_unreadable_screen_is_needs_inspection_not_an_error():
    def boom(_row):
        raise OSError("gone")

    out = needs_you.build([_card("claude:a", flagged=True)], [], set(), observe=boom)
    assert out["rows"][0]["kind"] == "needs_inspection"


def test_the_RECORDED_menu_is_shown_because_it_is_what_choose_checks():
    recorded = {
        "engine": "claude",
        "question": "Q",
        "options": [{"n": 2, "label": "Recorded", "selected": False}],
    }
    live = {
        "engine": "claude",
        "question": "Q",
        "options": [{"n": 2, "label": "Live", "selected": False}],
    }
    esc = _action("claude:a", ts=5.0, verb="escalate", observed_prompt={"menu": recorded})
    out = needs_you.build(
        [_card("claude:a")],
        [esc],
        set(),
        observe=lambda r: {"prompt_class": "choice", "menu": live},
    )
    row = out["rows"][0]
    assert row["kind"] == "choice"
    assert row["menu"] == recorded
    assert row["action"]["menu"] == recorded


def test_rows_are_capped_and_the_cap_is_reported(monkeypatch):
    monkeypatch.setattr(needs_you, "ROWS_MAX", 2)
    seen = []

    def observe(row):
        seen.append(row["id"])
        return None

    cards = [_card(f"claude:{i}", flagged=True, reviewed=float(i)) for i in range(5)]
    out = needs_you.build(cards, [], set(), observe=observe)
    assert len(out["rows"]) == 2 and out["truncated"] is True and out["total"] == 5
    # The screen is read only for rows that will be SHOWN.
    assert seen == ["claude:4", "claude:3"]


def test_a_past_deadline_action_is_never_offered_even_if_housekeeping_did_not_run():
    out = needs_you.build(
        [_card("claude:a")],
        [_action("claude:a", ts=10.0, expires_at=50.0)],
        set(),
        observe=_observe_open,
        now=100.0,
    )
    assert out["rows"] == []  # decision-only session, decision expired → not a needs-you row


def test_non_finite_times_never_reach_the_payload():
    out = needs_you.build(
        [_card("claude:a", flagged=True, reviewed=float("nan"), last=float("inf"))],
        [],
        set(),
        observe=_observe_open,
    )
    row = out["rows"][0]
    assert row["since"] == 0.0 and row["last_activity"] is None
    json.dumps(out, allow_nan=False)


def test_an_oversized_integer_time_never_raises():
    huge = int("9" * 400)
    out = needs_you.build(
        [_card("claude:a", reviewed=huge, last=huge)],
        [_action("claude:a", ts=huge, expires_at=huge)],
        set(),
        observe=_observe_open,
        now=100.0,
    )
    row = out["rows"][0]
    assert row["since"] == 0.0 and row["last_activity"] is None
    json.dumps(out, allow_nan=False)


# ---- the route --------------------------------------------------------------------------------


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


@pytest.fixture
def stubbed(monkeypatch):
    seen: dict = {}

    def cards(*, window_days, now=None, working_keys=None):
        seen["window_days"] = window_days
        return [_card("claude:a", flagged=True, reviewed=10.0), _card("claude:held", flagged=True)]

    monkeypatch.setattr(pulse, "build_cards", cards)
    monkeypatch.setattr(orchestrator_ledger, "live_actions", lambda *a, **k: [])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {"claude:held": "msn_1"})
    monkeypatch.setattr(
        orchestrator, "observed_screen", lambda key: {"prompt_class": "question", "menu": None}
    )
    return seen


def test_route_requires_login(auth_cfg, fake_jsonl):
    r = _client(auth_cfg).get("/api/pulse/needs-you")
    assert r.status_code == 401


def test_route_lists_unheld_sessions_and_coerces_the_window(auth_cfg, fake_jsonl, stubbed):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/pulse/needs-you", params={"window_days": 9})
    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["rows"]] == ["claude:a"]
    assert body["rows"][0]["kind"] == "question"
    # A read COERCES (1–3): 9 is the longest window offered, never an error.
    assert body["window_days"] == 3 and stubbed["window_days"] == 3


def test_route_defaults_the_window_to_the_stored_pref(auth_cfg, fake_jsonl, stubbed):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/pulse/needs-you").json()["window_days"] == 1


def test_route_answers_503_when_ownership_cannot_be_read(
    auth_cfg, fake_jsonl, stubbed, monkeypatch
):
    def unreadable(**_k):
        raise OSError("locked")

    monkeypatch.setattr(missions, "all_active_memberships", unreadable)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/pulse/needs-you")
    assert r.status_code == 503
    assert "rows" not in r.json()


def _real_ledger_with_live_proposal(expires_at):
    from agent_sessions import orchestrator_ledger as L

    rec = {
        "id": "act-real",
        "state": "proposed",
        "ts": time.time() - 120,
        "expires_at": expires_at,
        "session_id": "claude:a",
        "verb": "continue",
        "engine": "claude",
        "title": "t",
    }
    path = L._path(None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec) + "\n")
    return path


@pytest.fixture
def real_ledger_route(monkeypatch):
    def cards(*, window_days, now=None, working_keys=None):
        return [_card("claude:a")]  # NOT flagged: only the decision can make it a row

    monkeypatch.setattr(pulse, "build_cards", cards)
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: {})
    monkeypatch.setattr(
        orchestrator, "observed_screen", lambda key: {"prompt_class": "open", "menu": None}
    )


def test_route_offers_a_live_decision_from_the_real_ledger(auth_cfg, fake_jsonl, real_ledger_route):
    _real_ledger_with_live_proposal(time.time() + 3600)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/pulse/needs-you").json()["rows"]
    assert [r["action"]["id"] for r in rows] == ["act-real"]


def test_route_never_offers_an_EXPIRED_decision(auth_cfg, fake_jsonl, real_ledger_route):
    """Review 5032 finding 2: expired 60 s ago, unflagged session — it used to come back
    `can_approve: true`. The route now retires it as every decision read does, and refuses it."""
    _real_ledger_with_live_proposal(time.time() - 60)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/pulse/needs-you")
    assert r.status_code == 200 and r.json()["rows"] == []


def test_route_answers_503_when_the_LEDGER_cannot_be_read(
    auth_cfg, fake_jsonl, real_ledger_route, monkeypatch
):
    """Review 5032 finding 3: an unreadable ledger used to read as `rows: []` — a decision-only
    session silently gone, indistinguishable from "nothing needs you"."""
    from agent_sessions import orchestrator_ledger as L

    _real_ledger_with_live_proposal(time.time() + 3600)
    monkeypatch.setattr(L, "latest_by_id_checked", lambda path=None: ("unreadable", {}))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/pulse/needs-you")
    assert r.status_code == 503 and "ledger" in r.json()["detail"]
