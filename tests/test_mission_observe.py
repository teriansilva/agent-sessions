"""#1900 PR A — missions observe API sessions from structured state, not a screen.

* `mission_observe` is the one observation: an API session's Now line comes from its snapshot, a
  console session's from its output clock, and an unreadable snapshot is never "idle".
* An API member's pending requests become ONE mission card per SESSION: two concurrent requests
  make one card, a new request updates it without a second announcement, it is retired when the
  set empties, and a card the operator dismissed is not raised again for the same requests.
* The supervisor never nudges into an API session with something pending, and never reads a
  screen for it.
* The Now strip and the screen route answer for an API member without a terminal.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    mission_now,
    mission_observe,
    mission_permission,
    missions,
    notifications,
    orchestrator,
    prefs,
    scrollback,
)
from agent_sessions import mission_supervisor as sup
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions import structured_runtime as runtime
from agent_sessions.main import create_app

API = "claude-api:6f1c2b4e-1900-4a0b-9c1e-0a1b2c3d4e5f"
SECRET = "SECRET-PATCH-BODY"


def _req(rid: str, kind: str = "Edit", **payload) -> dict:
    return {
        "request_id": rid,
        "turn_id": "t1",
        "kind": kind,
        "summary": "{}",
        "payload": payload or {"file_path": f"src/{rid}.py", "new_string": SECRET},
        "payload_digest": "sha256:" + "0" * 64,
        "choices": ["approve", "reject"],
    }


def _snap(state: str = "idle", pending: list | None = None, **extra) -> dict:
    return {
        "session_key": API,
        "revision": 7,
        "state": state,
        "active_turn": extra.get("active_turn"),
        "turns": [{"kind": "operator", "turn_id": "t1", "ts": 100.0, "reply_ts": 160.0}],
        "pending_requests": pending or [],
    }


# ---- the observation ----------------------------------------------------------------------------


def test_pending_items_are_bounded_and_carry_no_payload():
    items = mission_observe.pending_items(
        [
            _req("a"),
            _req("b", "Bash", command="npm test\n-- login\x1b[31m"),
            _req(
                "c",
                "AskUserQuestion",
                questions=[{"question": "Keep the old cookie name?", "options": []}],
            ),
            _req("d", "Write", content=SECRET),
            {"no": "id"},
        ]
    )
    assert [i["request_id"] for i in items] == ["a", "b", "c", "d"]
    assert items[0] == {
        "request_id": "a",
        "kind": "Edit",
        "label": "src/a.py",
        "question": False,
    }
    assert items[1]["label"] == "npm test -- login [31m"
    assert items[2]["kind"] == "Question" and items[2]["question"] is True
    assert items[2]["label"] == "Keep the old cookie name?"
    # The content being written is never a label, and nothing else of the request is carried.
    assert items[3]["label"] == ""
    assert SECRET not in repr(items) and "digest" not in repr(items)
    long = mission_observe.pending_items([_req("e", "Bash", command="x" * 500)])
    assert len(long[0]["label"]) == mission_observe.LABEL_CHARS


@pytest.mark.parametrize(
    ("snap", "status", "cls"),
    [
        (_snap("running", active_turn="t1"), "producing", None),
        (_snap("awaiting_approval", [_req("a"), _req("b")]), "at_prompt", "approval"),
        (_snap("idle"), "quiet", None),
    ],
)
def test_an_api_line_maps_its_structured_state(snap, status, cls):
    line = mission_observe.now_line(
        mission_observe.api_facts(snap), now=200.0, recap_at=150.0, prompt_class="confirm"
    )
    assert line["status"] == status and line["prompt_class"] == cls
    assert line["api"] is True
    assert line["pending_count"] == len(snap["pending_requests"])
    assert line["seconds_since_output"] == 40  # the newest turn's reply
    assert line["recap_older_than_output"] is True


def test_an_unreadable_snapshot_is_unobserved_never_idle(monkeypatch):
    monkeypatch.setattr(mission_observe, "is_api", lambda key: True)

    async def boom(key, **kw):
        raise RuntimeError("worker state unreadable")

    monkeypatch.setattr(runtime, "snapshot", boom)
    import asyncio

    obs = asyncio.run(mission_observe.facts(API))
    assert obs["readable"] is False and obs["pending"] == []
    line = mission_observe.now_line(obs, now=1.0, recap_at=None, prompt_class=None)
    assert line["status"] == "unobserved"


def test_a_console_line_is_unchanged():
    obs = {"api": False, "last_output_at": 100.0}
    for cls in (None, "confirm"):
        assert mission_observe.now_line(
            obs, now=130.0, recap_at=None, prompt_class=cls
        ) == mission_now.derive(now=130.0, last_output_at=100.0, prompt_class=cls, recap_at=None)


# ---- one card per API session -------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True})
    mid = missions.create_mission("Harden the login flow", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, API)
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
    monkeypatch.setattr(mission_observe, "is_api", lambda key: key == API)
    snap = {"value": _snap()}

    async def snapshot(key, **kw):
        assert key == API
        return snap["value"]

    monkeypatch.setattr(runtime, "snapshot", snapshot)

    def no_screen(*a, **k):
        raise AssertionError("an API session's screen was read")

    monkeypatch.setattr(mission_permission, "observe", no_screen)
    monkeypatch.setattr(scrollback, "live_tail_text", no_screen)

    from agent_sessions import review

    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    n = {"fp": 0}

    def gather(*a, **k):
        n["fp"] += 1
        return ("transcript", f"fp{n['fp']}")

    monkeypatch.setattr(review, "gather_input", gather)

    async def reply(messages, **kw):
        return {
            "recap": "working",
            "assessment": "progress",
            "nudge": {"objective_key": "ran", "why": "keep going"},
        }

    monkeypatch.setattr(review, "complete_json", reply)
    sent: list = []

    async def nudge(*a, **k):
        sent.append(k)
        return {"sent": True}

    monkeypatch.setattr(sup, "nudge", nudge)
    announced: list = []
    real_add = notifications.add

    def add(**kw):
        announced.append(kw)
        return real_add(**kw)

    monkeypatch.setattr(notifications, "add", add)
    return mid, snap, sent, announced


def _cards() -> list[dict]:
    return [
        r
        for r in ledger.latest_by_id().values()
        if r.get("origin") == mission_permission.ORIGIN and r.get("session_id") == API
    ]


def _live() -> list[dict]:
    return [r for r in _cards() if r.get("state") in ledger.ESCALATION_STATES]


@pytest.mark.anyio
async def test_two_concurrent_requests_make_one_card_and_a_third_updates_it_quietly(world):
    mid, snap, sent, announced = world
    snap["value"] = _snap("awaiting_approval", [_req("a"), _req("b")])
    out = await sup.run_pass(mid)
    (card,) = _live()
    assert card["escalation_reason"] == mission_permission.API_REASON
    assert card["api_pending"]["count"] == 2 and card["api_pending"]["fp"] == ["a", "b"]
    assert card["api_pending"]["pane"] == "/s/claude-api/" + API.split(":", 1)[1]
    assert card["rationale"].startswith("2 approvals waiting in Harden the login flow")
    assert out["per_session"][0]["permission"]["card"] == card["id"]
    assert sent == [], "a nudge was proposed into a session with a pending request"
    assert len([a for a in announced if a.get("action_id") == card["id"]]) == 1

    # A third request arrives: the SAME card, its count current, and no second announcement.
    snap["value"] = _snap("awaiting_approval", [_req("a"), _req("b"), _req("c"), _req("d")])
    await sup.run_pass(mid)
    (again,) = _live()
    assert again["id"] == card["id"]
    assert again["api_pending"]["count"] == 4
    assert len(again["api_pending"]["items"]) == mission_observe.SHOWN_ITEMS
    assert len(announced) == 1
    assert SECRET not in repr(again)

    # One settles: still one card, still no announcement.
    snap["value"] = _snap("awaiting_approval", [_req("c")])
    await sup.run_pass(mid)
    (one,) = _live()
    assert one["id"] == card["id"] and one["api_pending"]["count"] == 1
    assert len(announced) == 1

    # All settled: the card is retired, nothing is left to tap.
    snap["value"] = _snap("idle")
    await sup.run_pass(mid)
    assert _live() == []
    assert next(r for r in _cards() if r["id"] == card["id"])["state"] == "expired"


@pytest.mark.anyio
async def test_a_dismissed_card_is_not_raised_again_until_a_new_request(world):
    mid, snap, sent, announced = world
    snap["value"] = _snap("awaiting_approval", [_req("a"), _req("b")])
    await sup.run_pass(mid)
    (card,) = _live()
    assert ledger.compare_and_set(card["id"], ledger.ESCALATION_STATES, "rejected", None)

    snap["value"] = _snap("awaiting_approval", [_req("b")])
    await sup.run_pass(mid)
    assert _live() == [] and len(announced) == 1

    snap["value"] = _snap("awaiting_approval", [_req("b"), _req("e")])
    await sup.run_pass(mid)
    (fresh,) = _live()
    assert fresh["id"] != card["id"] and fresh["api_pending"]["count"] == 2
    assert len(announced) == 2
    assert sent == []


@pytest.mark.anyio
async def test_an_idle_api_session_gets_no_card(world):
    mid, snap, sent, announced = world
    snap["value"] = _snap("idle")
    await sup.run_pass(mid)
    assert _cards() == [] and announced == []


@pytest.mark.anyio
async def test_a_live_card_is_kept_alive_while_requests_wait(world, monkeypatch):
    mid, snap, _sent, _announced = world
    snap["value"] = _snap("awaiting_approval", [_req("a")])
    await sup.run_pass(mid)
    (card,) = _live()
    later = time.time() + 25 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    mission_permission.sync_api_card(
        mid, API, {"title": "x"}, mission_observe.pending_items([_req("a")]), pane="/s/x/y"
    )
    (kept,) = _live()
    assert kept["id"] == card["id"] and kept["expires_at"] > later + 20 * 60


def test_the_card_words_count_approvals_and_questions_and_name_nothing():
    items = mission_observe.pending_items(
        [
            _req("a", "Bash", command="rm -rf build"),
            _req("q", "AskUserQuestion", questions=[{"question": "Which?"}]),
        ]
    )
    words = mission_permission.api_summary(items, "Review")
    assert (
        words == "1 approval and 1 question waiting in Review. Decide each one in its conversation."
    )
    assert "rm -rf" not in words and "Which?" not in words


# ---- the routes ---------------------------------------------------------------------------------


@pytest.fixture
def client(auth_cfg, tmp_home, fake_jsonl, monkeypatch):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    mission_now.reset_cache_for_test()
    monkeypatch.setattr(mission_observe, "is_api", lambda key: key == API)
    return c


def test_the_now_strip_reads_an_api_member_from_its_snapshot(client, monkeypatch):
    mid = missions.create_mission("x")["id"]
    missions.adopt(mid, API)

    async def snapshot(key, **kw):
        return _snap("awaiting_approval", [_req("a"), _req("b")])

    monkeypatch.setattr(runtime, "snapshot", snapshot)
    monkeypatch.setattr(scrollback, "live_tail_text", lambda *a, **k: pytest.fail("screen read"))
    [row] = client.get(f"/api/missions/{mid}/now").json()["sessions"]
    assert row["status"] == "at_prompt" and row["prompt_class"] == "approval"
    assert row["api"] is True and row["pending_count"] == 2
    assert SECRET not in repr(row)


def test_the_screen_route_answers_an_api_member_without_a_screen(client, monkeypatch):
    mid = missions.create_mission("x")["id"]
    missions.adopt(mid, API)
    monkeypatch.setattr(orchestrator, "evidence_for", lambda *a: pytest.fail("ring replayed"))
    r = client.get(f"/api/missions/{mid}/screen/{API}")
    assert r.status_code == 200
    assert r.json() == {
        "api": True,
        "session_key": API,
        "pane": "/s/claude-api/" + API.split(":", 1)[1],
    }
    assert "no-store" in r.headers["cache-control"]


def test_the_screen_route_still_refuses_a_session_the_mission_does_not_hold(client):
    mid = missions.create_mission("x")["id"]
    assert client.get(f"/api/missions/{mid}/screen/{API}").status_code == 409


@pytest.mark.anyio
async def test_a_short_proposal_ttl_never_re_announces_the_same_requests(world, monkeypatch):
    """Independent review on #1962: the card mirrors state, so its life is not the proposal TTL.
    With a 1-minute TTL and a review mission swept every 300 s, the timer used to expire the card
    between passes and the next pass raised and announced it again for the same requests."""
    mid, snap, _sent, announced = world
    prefs.set_automation_policy("mission", {"proposal_ttl_minutes": 1})
    snap["value"] = _snap("awaiting_approval", [_req("a")])
    await sup.run_pass(mid)
    (card,) = _live()
    assert card["expires_at"] - time.time() >= mission_permission.API_CARD_TTL_S - 5
    later = time.time() + 300
    monkeypatch.setattr(time, "time", lambda: later)
    ledger.expire_due(now=later)
    await sup.run_pass(mid)
    (still,) = _live()
    assert still["id"] == card["id"] and len(announced) == 1


def test_an_update_never_overwrites_a_newer_one(world, monkeypatch):
    """The in-place update is a compare-and-set on the record as read: a pass holding a stale read
    must not overwrite what a newer pass wrote."""
    mid, _snap_, _sent, _announced = world
    row = {"title": "x"}
    items = mission_observe.pending_items
    mission_permission.sync_api_card(mid, API, row, items([_req("a"), _req("b")]), pane="/s/x/y")
    (stale,) = _live()
    mission_permission.sync_api_card(
        mid, API, row, items([_req("a"), _req("b"), _req("c")]), pane="/s/x/y"
    )
    with monkeypatch.context() as m:
        m.setattr(mission_permission, "_api_cards", lambda key: [stale])
        out = mission_permission.sync_api_card(mid, API, row, items([_req("a")]), pane="/s/x/y")
    assert out["updated"] is False
    (card,) = _live()
    assert card["api_pending"]["count"] == 3


@pytest.mark.parametrize("first", ["native_ipc", "native_journal", "routes.missions"])
def test_importing_any_runtime_module_first_has_no_cycle(first):
    """CI on #1962: `routes.missions` imports `mission_observe`, and a module-level import of
    `structured_runtime` there closed a cycle (native_ipc → … → routes → mission_observe →
    structured_runtime → native_journal → native_ipc) for a process that imports `native_ipc`
    first, as the native worker's writer does."""
    import os
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", f"import agent_sessions.{first}"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]


def test_a_request_id_with_a_comma_never_hides_a_new_request(world):
    """Hermes on #1962: the content fingerprint is a list, not a comma join, so a dismissed set
    {"a,b"} does not swallow a later, different request "a"."""
    mid, _snap_, _sent, _announced = world
    row = {"title": "x"}
    items = mission_observe.pending_items
    mission_permission.sync_api_card(mid, API, row, items([_req("a,b")]), pane="/s/x/y")
    (card,) = _live()
    assert ledger.compare_and_set(card["id"], ledger.ESCALATION_STATES, "rejected", None)
    out = mission_permission.sync_api_card(mid, API, row, items([_req("a")]), pane="/s/x/y")
    assert out.get("dismissed") is not True
    (fresh,) = _live()
    assert fresh["id"] != card["id"]
