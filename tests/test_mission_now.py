"""The "right now" strip (#1064, Phases 2–3): observation from facts the app already has.

Pinned here: the derivation (every status, the prompt settle, the recap-older-than-output flag and
the unobserved/quiet split); the screen cache; the one store read; the route's gate and its promise
to return derived fields only; and the loop hardening the issue review asked for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_sessions import mission_now as mn
from agent_sessions import mission_supervisor_loop as loop
from agent_sessions import missions, scrollback
from agent_sessions.main import create_app

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
SECRET = "SECRET-SCREEN-TEXT the strip must never carry this"


# ---- the derivation ---------------------------------------------------------------------------


def d(**kw):
    base = {"now": 1000.0, "last_output_at": 1000.0, "prompt_class": None, "recap_at": None}
    return mn.derive(**{**base, **kw})


def test_nothing_observed_is_not_quiet():
    r = d(last_output_at=None)
    assert r["status"] == "unobserved" and r["seconds_since_output"] is None


def test_recent_output_is_producing_and_long_silence_is_quiet():
    assert d(last_output_at=1000.0 - 5)["status"] == "producing"
    assert d(last_output_at=1000.0 - (mn.QUIET_AFTER_S + 1))["status"] == "quiet"
    assert d(last_output_at=1000.0 - 125)["seconds_since_output"] == 125


@pytest.mark.parametrize("cls", ["choice", "confirm", "question"])
def test_a_settled_waiting_prompt_is_at_prompt(cls):
    r = d(last_output_at=1000.0 - (mn.PROMPT_SETTLE_S + 1), prompt_class=cls)
    assert r["status"] == "at_prompt" and r["prompt_class"] == cls


def test_a_prompt_that_has_not_settled_is_still_producing():
    r = d(last_output_at=1000.0 - 1, prompt_class="choice")
    assert r["status"] == "producing" and r["prompt_class"] is None


def test_an_open_screen_is_never_at_prompt():
    assert d(last_output_at=1000.0 - 60, prompt_class="open")["status"] == "quiet"


def test_the_recap_age_and_whether_it_predates_the_latest_output():
    fresh = d(last_output_at=900.0, recap_at=950.0)
    assert fresh["recap_age_s"] == 50 and fresh["recap_older_than_output"] is False
    stale = d(last_output_at=990.0, recap_at=700.0)
    assert stale["recap_age_s"] == 300 and stale["recap_older_than_output"] is True
    assert d(recap_at=None)["recap_age_s"] is None


# ---- the screen read is cached and fail-soft --------------------------------------------------


def test_the_screen_is_read_once_per_ttl(monkeypatch):
    mn.reset_cache_for_test()
    reads: list[str] = []

    def tail(key, n=4000):
        reads.append(key)
        return "Do you want to proceed? (y/n)"

    monkeypatch.setattr(scrollback, "live_tail_text", tail)
    assert mn.prompt_class_cached("claude:x", now=100.0) == "confirm"
    assert mn.prompt_class_cached("claude:x", now=100.0 + mn.SCREEN_CACHE_TTL_S - 0.1) == "confirm"
    assert len(reads) == 1
    mn.prompt_class_cached("claude:x", now=100.0 + mn.SCREEN_CACHE_TTL_S + 0.1)
    assert len(reads) == 2


def test_an_unreadable_screen_is_open_not_an_error(monkeypatch):
    mn.reset_cache_for_test()

    def boom(*a, **k):
        raise OSError("ring gone")

    monkeypatch.setattr(scrollback, "live_tail_text", boom)
    assert mn.prompt_class_cached("claude:y", now=1.0) == "open"


# ---- the store read ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    mn.reset_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


def test_an_unknown_mission_is_not_found(store):
    with pytest.raises(missions.MissionNotFound):
        missions.now_facts("msn_" + "0" * 32)


def test_a_mission_with_no_session_has_no_facts(store):
    mid = missions.create_mission("x")["id"]
    assert missions.now_facts(mid) == []


# ---- the route --------------------------------------------------------------------------------


@pytest.fixture
def client(auth_cfg, tmp_home, fake_jsonl):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    mn.reset_cache_for_test()
    return c, auth_cfg


def test_the_route_has_the_same_gate_as_the_mission_detail(auth_cfg, tmp_home):
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    mid = missions.create_mission("x")["id"]
    now = anon.get(f"/api/missions/{mid}/now", follow_redirects=False)
    detail = anon.get(f"/api/missions/{mid}", follow_redirects=False)
    assert now.status_code == detail.status_code != 200


def test_an_unknown_mission_is_a_404(client):
    c, _ = client
    assert c.get("/api/missions/msn_" + "0" * 32 + "/now").status_code == 404


def test_a_quiet_session_at_a_prompt_is_reported_and_no_screen_text_leaves(client, monkeypatch):
    c, _ = client
    mid = missions.create_mission("x")["id"]
    missions.adopt(mid, CLAUDE_A)
    monkeypatch.setattr(scrollback, "get_last_visible_output_at", lambda key: 1.0)
    monkeypatch.setattr(
        scrollback, "live_tail_text", lambda key, n=4000: SECRET + "\nDo you want to proceed? (y/n)"
    )
    r = c.get(f"/api/missions/{mid}/now")
    assert r.status_code == 200
    [row] = r.json()["sessions"]
    assert row["session_key"] == CLAUDE_A
    assert row["status"] == "at_prompt" and row["prompt_class"] == "confirm"
    assert "SECRET" not in r.text, "screen text left the server"


def test_a_producing_session_does_not_read_the_screen_at_all(client, monkeypatch):
    c, _ = client
    mid = missions.create_mission("x")["id"]
    missions.adopt(mid, CLAUDE_A)
    import time as _time

    monkeypatch.setattr(scrollback, "get_last_visible_output_at", lambda key: _time.time())
    reads: list[str] = []
    monkeypatch.setattr(scrollback, "live_tail_text", lambda key, n=4000: reads.append(key) or "")
    [row] = c.get(f"/api/missions/{mid}/now").json()["sessions"]
    assert row["status"] == "producing" and reads == []


def test_nothing_observed_is_reported_as_such(client, monkeypatch):
    c, _ = client
    mid = missions.create_mission("x")["id"]
    missions.adopt(mid, CLAUDE_A)
    monkeypatch.setattr(scrollback, "get_last_visible_output_at", lambda key: None)
    [row] = c.get(f"/api/missions/{mid}/now").json()["sessions"]
    assert row["status"] == "unobserved"


# ---- the loop hardening (issue review) --------------------------------------------------------


def test_contention_defers_a_due_reading_without_spending_an_attempt(monkeypatch):
    loop._early.clear()
    loop._early["msn_a"] = (10.0, 1)
    loop._early["msn_later"] = (500.0, 0)
    loop.defer_due(20.0)
    assert loop._early["msn_a"] == (20.0 + loop.EARLY_CONTENTION_BACKOFF_S, 1)
    assert loop._early["msn_later"] == (500.0, 0), "a reading not yet due is left alone"
    assert loop.due_early(20.0) == [], "nothing is due again immediately — no zero-timeout spin"
    loop._early.clear()
