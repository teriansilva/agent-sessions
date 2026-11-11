"""HQ's Dispatches (#1837, #1746 P3a): the store, the tags, the bundles and `hq_notify`'s input.

Unit tests. The acceptance tests (real processes, SIGKILL, the 24 h window, the loss boundary) are
in ``test_hq_dispatch_acceptance.py``. Every model request here goes to a REAL loopback endpoint
(``hq_dispatch_harness.FakeEndpoint``) through the app's one transport, so the assertions are on
the bytes that actually left the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from pathlib import Path

import pytest

import hq_dispatch_harness as H
from agent_sessions import (
    ask_store,
    automation_runner,
    hq_contacts,
    hq_dispatch,
    mission_supervisor,
    missions,
    needs_you_notify,
    notifications,
    prefs,
    prompts,
)

ROOT = Path(__file__).resolve().parents[1]
SID = "claude:11111111-1111-4111-8111-111111111111"
MID = "msn_" + "a" * 32


@pytest.fixture
def ep():
    e = H.FakeEndpoint()
    yield e
    e.close()


@pytest.fixture
def hq(ep):
    H.set_up_hq(ep.base)
    return ep


def run_pass(now: float) -> dict:
    return asyncio.run(hq_dispatch.pass_once(now=now))


def later(s: float = 61) -> float:
    return time.time() + s


def doc() -> dict:
    return hq_dispatch._read_strict()


def joined() -> dict:
    """The memberships, or none when the store does not exist yet."""
    return doc()["member_of"] if hq_dispatch._path().exists() else {}


def announce_automation(aid="abc", rid="r1", name="nightly docs", reason="boom", **extra):
    automation_runner.notify(
        {
            "automation": {"id": aid, "name": name},
            "run": {"id": rid, "outcome": extra.get("outcome", "failed"), "reason": reason},
            "episode": ("open", f"automation:{aid}:{rid}"),
            **({"alert": extra["alert"]} if "alert" in extra else {}),
        }
    )


def announce_needs_you(monkeypatch, sid=SID, title="the session's title", project="api"):
    from agent_sessions.routes import pulse as pulse_routes

    monkeypatch.setattr(
        pulse_routes,
        "build_needs_you",
        lambda *_a, **_k: {
            "rows": [
                {
                    "id": sid,
                    "title": title,
                    "project": {"name": project},
                    "engine": "claude",
                    "kind": "question",
                    "action": {},
                }
            ],
            "needs_you_ids": [sid],
        },
    )
    return needs_you_notify.sync_once()


def announce_escalation(objective="ship_checks", reason="ship checks: nothing moved", title="Rel"):
    tag = hq_dispatch.escalation_tag(MID, objective, 1)
    mission_supervisor._announce(
        {"title": title, "project": {"name": "api"}, "engine": "claude"},
        SID,
        reason,
        dispatch_tag=tag,
    )
    return tag


def request_entries(ep, i: int = -1) -> list[dict]:
    body = ep.requests[i]
    users = [m for m in body["messages"] if m["role"] == "user"]
    assert len(users) == 1
    return json.loads(users[0]["content"])


# ---- tags ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        f"needs-you:{SID}:18c2f0a1b2c",
        "needs-you:opencode:ses_0123abc:1a",
        hq_dispatch.escalation_tag(MID, "ship_checks", 3),
        "automation:abc:r1",
        "automation:a1b2:9f1e2d3c-run",
    ],
)
def test_every_tag_pattern_is_stored(tag):
    rec = notifications.add(
        title="t", project="", session_id="", engine="", action_id="x", dispatch_tag=tag
    )
    assert rec["dispatch"] == tag
    assert notifications._read(notifications._notifications_path())[-1]["dispatch"] == tag


@pytest.mark.parametrize(
    "tag",
    [
        "automation:abc:r1:partial",  # a partial-send alert's key can never match
        "automation:ABC:r1",
        "needs-you:claude",
        "escalation:msn_x:abc:1",
        "escalation:" + MID + ":" + "0" * 12 + ":1 trailing",
        "Gen. Okafor says hi",
        "",
        "automation:" + "a" * 65 + ":r",
    ],
)
def test_anything_else_is_dropped_and_the_row_and_push_are_byte_identical(
    tag, monkeypatch, tmp_path
):
    """An invalid tag never costs the notification: the row and the push are exactly a call
    without ``dispatch_tag``, and nothing is enqueued."""
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    sent: list[bytes] = []
    subs = tmp_path / "subs.json"
    subs.write_text(json.dumps([{"id": "d", "endpoint": "https://push.example/x", "keys": {}}]))
    monkeypatch.setattr(notifications, "_send_all", lambda payload, *a, **k: sent.append(payload))

    def one(path: Path, **kw) -> bytes:
        rec = notifications.add(
            title="Automation failed: x",
            project="",
            session_id="",
            engine="",
            action_id="automation:abc:r1",
            path=path,
            **kw,
        )
        notifications.fanout(rec, path=subs)
        return path.read_bytes()

    with_tag = one(tmp_path / "a.json", dispatch_tag=tag)
    without = one(tmp_path / "b.json")
    assert with_tag == without
    assert sent[0] == sent[1]
    assert not hq_dispatch._path().exists() or not doc()["member_of"]


def test_a_producer_that_waited_for_the_lock_joins_the_bundle_opened_meanwhile(hq, monkeypatch):
    """A producer's join time is read UNDER the store's lock. Read before it, a producer that
    waited while another opened a bundle saw that bundle as opened in its future and started a
    second one dated before it (acceptance 5 under load: bundles of [2, 10] where [10, 2] was
    due). The second producer runs here between the first one's clock and its lock."""
    real = hq_dispatch.json_write_lock
    cut_in: list[bool] = []

    @contextlib.contextmanager
    def another_producer_first(path):
        if not cut_in:
            cut_in.append(True)
            announce_automation(aid="second", rid="r2", name="second job")
        with real(path):
            yield

    monkeypatch.setattr(hq_dispatch, "json_write_lock", another_producer_first)
    announce_automation(aid="first", rid="r1", name="first job")
    d = doc()
    assert cut_in and len(d["member_of"]) == 2
    assert len(d["bundles"]) == 1, d["bundles"]


def test_an_invalid_tag_from_a_producer_enqueues_nothing(hq):
    automation_runner._announce("t", "r", "automation:abc:r1:partial", dispatch_tag="bad tag")
    row = notifications._read(notifications._notifications_path())[-1]
    assert "dispatch" not in row
    assert not hq_dispatch._path().exists()


def test_the_escalation_tag_is_the_won_key_and_the_same_across_a_retry():
    a = hq_dispatch.escalation_tag(MID, "the objective: SENTINEL", 2)
    assert a == hq_dispatch.escalation_tag(MID, "the objective: SENTINEL", 2)
    assert re.fullmatch(rf"escalation:{MID}:[0-9a-f]{{12}}:2", a)
    assert "SENTINEL" not in a and "objective" not in a
    assert notifications.valid_dispatch_tag(a)


def test_escalate_hands_back_the_tag_it_won(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    missions.adopt(mid, SID)
    missions.instantiate_objectives(
        mid, [{"key": "k", "title": "k", "probe": "forge_pr", "gate": True, "source": "playbook"}]
    )
    won: dict = {}
    kw = {"session_key": SID, "objective_key": "k", "reason": "nothing moved"}
    assert asyncio.run(mission_supervisor.escalate(mid, **kw, won=won)) is True
    assert won["tag"] == hq_dispatch.escalation_tag(mid, "k", 1)
    again: dict = {}
    assert asyncio.run(mission_supervisor.escalate(mid, **kw, won=again)) is False
    assert again == {}


# ---- the partial-alert boundary -----------------------------------------------------------------


def test_one_run_with_a_failure_episode_and_a_partial_alert(hq, monkeypatch):
    """Two rows and two pushes, as today; only the episode row carries `dispatch`, so exactly one
    member and one model request — no collision, no double dispatch."""
    pushes: list[dict] = []
    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", lambda rec, *a, **k: pushes.append(rec))
    announce_automation(alert="automation:abc:r1:partial", outcome="partial")
    rows = notifications._read(notifications._notifications_path())
    assert len(rows) == 2 and len(pushes) == 2
    tagged = [r for r in rows if "dispatch" in r]
    assert [r["action_id"] for r in tagged] == ["automation:abc:r1"]
    assert list(doc()["member_of"]) == ["automation:abc:r1"]
    run_pass(later())
    assert hq.count() == 1
    assert request_entries(hq) == [
        {
            "class": "automation failed",
            "automation": "nightly docs",
            "outcome": "partial",
            "age_min": 0,
        }
    ]
    run_pass(later(400))
    assert hq.count() == 1


# ---- the input allowlist, at the actual outbound request ----------------------------------------


def test_the_request_carries_only_each_producers_facts(hq, monkeypatch):
    s = H.SENTINELS
    announce_needs_you(monkeypatch, title=s["session_title"])
    announce_escalation(
        objective=s["objective"], reason=f"{s['objective']}: {s['escalation_reason']}"
    )
    announce_automation(name="nightly docs", reason=s["run_reason"])
    # The row's own title and reason, planted on a fourth event through the real `add`.
    notifications.add(
        title=s["row_title"],
        project="",
        session_id="",
        engine="",
        reason=s["row_reason"],
        action_id="automation:def:r2",
        dispatch_tag="automation:def:r2",
    )
    rec = notifications._read(notifications._notifications_path())[-1]
    hq_dispatch.announce(rec, {"class": hq_dispatch.CLASS_AUTOMATION, "automation": "other"})
    run_pass(later())
    assert hq.count() == 1
    wire = hq.bodies()[0]
    for v in s.values():
        assert v not in wire, v
    entries = request_entries(hq)
    by = {e["class"]: e for e in entries if e.get("automation") != "other"}
    assert set(by["needs you"]) == {"class", "project", "engine", "age_min"} or set(
        by["needs you"]
    ) == {"class", "project", "age_min"}
    assert set(by["mission escalation"]) <= hq_dispatch.FACT_KEYS["mission escalation"]
    assert by["mission escalation"]["mission"] == "Rel"
    assert set(by["automation failed"]) == {"class", "automation", "outcome", "age_min"}
    for e in entries:
        assert set(e) <= hq_dispatch.FACT_KEYS[e["class"]]


def test_a_credential_shaped_automation_name_is_dropped(hq):
    announce_automation(name="deploy password=hunter2hunter2")
    run_pass(later())
    (entry,) = request_entries(hq)
    assert "automation" not in entry and "hunter2" not in hq.bodies()[0]
    assert set(entry) == {"class", "outcome", "age_min"}


def test_names_are_capped_at_80(hq):
    announce_automation(name="n" * 300)
    run_pass(later())
    assert request_entries(hq)[0]["automation"] == "n" * hq_dispatch.NAME_MAX


def test_display_is_never_sent(hq):
    announce_automation(name="nightly docs", reason="REASON-ONLY-IN-DISPLAY")
    run_pass(later())
    assert "REASON-ONLY-IN-DISPLAY" not in hq.bodies()[0]
    assert "Automation failed" not in hq.bodies()[0]
    assert "/mission/automations" not in hq.bodies()[0]


# ---- the output ---------------------------------------------------------------------------------


def test_the_240_character_cap(hq):
    hq.reply = "x" * 241
    announce_automation()
    run_pass(later())
    (bid,) = doc()["bundles"]
    assert doc()["claims"][bid]["state"] == "failed"
    assert doc()["claims"][bid]["code"] == "too_long"
    assert bid not in doc()["lines"]
    hq.reply = "y" * 240
    announce_automation(aid="b", rid="r2")
    run_pass(later(200))
    lines = list(doc()["lines"].values())
    assert [len(x["text"]) for x in lines] == [240]


def test_newlines_are_collapsed_into_one_line(hq):
    hq.reply = "  First part.\n\nSecond   part.  "
    announce_automation()
    run_pass(later())
    assert [x["text"] for x in doc()["lines"].values()] == ["First part. Second part."]


def test_credential_shaped_output_is_withheld_whole(hq):
    hq.reply = "Deploy is done; use password=hunter2hunter2 to log in."
    announce_automation()
    run_pass(later())
    (line,) = doc()["lines"].values()
    assert line["text"] == ask_store.WITHHELD
    assert "hunter2" not in json.dumps(hq_dispatch.list_view())


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda e: setattr(e, "status", 500), "http_500"),
        (lambda e: setattr(e, "reply", ""), "empty"),
        (lambda e: setattr(e, "reply", None), "empty"),
        (lambda e: setattr(e, "reply", "z" * 500), "too_long"),
        (lambda e: setattr(e, "extra", {"refusal": "I can't help with that."}), "refused"),
    ],
)
def test_a_model_failure_settles_failed_with_no_line_and_no_second_call(hq, setup, code):
    setup(hq)
    announce_automation()
    run_pass(later())
    (bid,) = doc()["bundles"]
    assert doc()["claims"][bid] == {
        "state": "failed",
        "at": pytest.approx(later(), abs=5),
        "code": code,
    }
    assert doc()["lines"] == {}
    assert hq.count() == 1
    hq.status, hq.reply, hq.extra = 200, "fine", {}
    run_pass(later(300))
    run_pass(later(3000))
    assert hq.count() == 1
    row = notifications._read(notifications._notifications_path())[-1]
    assert row["title"] == "Automation failed: nightly docs"


def test_a_timeout_is_failed_and_never_retried(hq, monkeypatch):
    monkeypatch.setattr(hq_dispatch, "CALL_CAP_S", 0.5)
    hq.hang = True
    announce_automation()
    run_pass(later())
    (bid,) = doc()["bundles"]
    assert doc()["claims"][bid]["code"] == "timeout"
    hq.release.set()
    run_pass(later(500))
    assert hq.count() == 1


def test_not_set_up_settles_failed_with_no_request(ep):
    announce_automation()
    run_pass(later())
    (c,) = doc()["claims"].values()
    assert (c["state"], c["code"]) == ("failed", "unconfigured")
    assert ep.count() == 0


# ---- the contact --------------------------------------------------------------------------------


def test_the_servers_perm_equals_the_clients():
    ts = (ROOT / "web" / "src" / "components" / "hq" / "contacts.ts").read_text()
    m = re.search(r"export const PERM: readonly string\[\] = \[(.*?)\];", ts, re.S)
    assert m
    assert tuple(re.findall(r'"([a-z-]+)"', m.group(1))) == hq_contacts.PERM
    assert sorted(hq_contacts.PERM) == sorted(hq_contacts.IDS)


def test_random_is_the_days_contact():
    day = hq_contacts.utc_day_number(time.time())
    assert hq_contacts.speaker("random", time.time()) == hq_contacts.PERM[day % 13]
    assert hq_contacts.contact_for_day(0) == "southamerica-woman"
    assert hq_contacts.contact_for_day(13) == "southamerica-woman"
    assert hq_contacts.contact_for_day(-1) == "africa-woman"


def test_off_sends_no_persona_and_signs_hq(hq):
    prefs.set_hq_contact("off")
    announce_automation()
    run_pass(later())
    systems = [m["content"] for m in hq.requests[0]["messages"] if m["role"] == "system"]
    assert systems == [prompts.effective("hq_notify")]
    (line,) = doc()["lines"].values()
    assert line["contact"] == "HQ"
    assert hq_dispatch.list_view()["dispatches"][0]["by"] == "HQ"


def test_a_contact_speaks_first_and_the_guard_is_the_last_word(hq):
    prefs.set_hq_contact("africa-woman")
    announce_automation()
    run_pass(later())
    systems = [m["content"] for m in hq.requests[0]["messages"] if m["role"] == "system"]
    assert systems == [prompts.effective("hq_persona_africa_woman"), prompts.effective("hq_notify")]
    assert systems[-1].endswith(prompts.GUARD_CLAUSE)
    body = hq.requests[0]
    assert body["temperature"] == 0 and body["max_tokens"] == 200 and "tools" not in body
    view = hq_dispatch.list_view()["dispatches"][0]
    assert (view["contact"], view["by"]) == ("General Amara Okafor", "Gen. Okafor")


# ---- bundles ------------------------------------------------------------------------------------


def _row(i: int) -> dict:
    return {
        "dispatch": f"automation:a{i}:r",
        "title": "t",
        "reason": "",
        "ts": 1.0,
        "action_id": f"automation:a{i}:r",
        "session_id": "",
    }


def test_a_bundle_takes_events_for_60_seconds_and_at_most_10():
    t0 = 1_800_000_000.0
    bids = [hq_dispatch.enqueue_row(_row(i), now=t0 + i) for i in range(12)]
    assert len(set(bids[:10])) == 1 and bids[10] != bids[0] and bids[11] == bids[10]
    late = hq_dispatch.enqueue_row(_row(20), now=t0 + 10 + hq_dispatch.WINDOW_S)
    assert late not in bids
    # Joining is create-only: the same tag again is the same bundle, and no second member.
    assert hq_dispatch.enqueue_row(_row(0), now=t0 + 500) == bids[0]
    assert doc()["bundles"][bids[0]]["members"].count("automation:a0:r") == 1


def test_only_a_sealed_bundle_is_claimed(hq):
    announce_automation()
    run_pass(time.time() + 5)
    assert hq.count() == 0 and doc()["claims"] == {}
    run_pass(later())
    assert hq.count() == 1


def test_a_stale_attempt_gives_up_and_is_never_called_again(hq):
    announce_automation()
    t = later()
    hq_dispatch._housekeep(t)
    (bid,) = doc()["bundles"]
    assert hq_dispatch._claim(bid, t) is not None
    assert hq_dispatch._claim(bid, t) is None  # create-only
    run_pass(t + hq_dispatch.GIVE_UP_S - 1)
    assert doc()["claims"][bid]["state"] == "attempt"
    run_pass(t + hq_dispatch.GIVE_UP_S)
    assert doc()["claims"][bid]["state"] == "gave_up"
    run_pass(t + 9999)
    assert hq.count() == 0
    # Its settlement can no longer land.
    assert hq_dispatch._settle(bid, t, {"text": "late"}, t + 9999) is False
    assert doc()["lines"] == {}


def test_pruning_by_age_and_by_count_never_touches_seen(monkeypatch):
    monkeypatch.setattr(hq_dispatch, "KEEP_MAX", 3)
    t0 = 1_800_000_000.0
    for i in range(5):
        hq_dispatch.enqueue_row(_row(i), now=t0 + i * 100)
    d = doc()
    assert len(d["bundles"]) == 3
    assert set(d["member_of"]) == {f"automation:a{i}:r" for i in (2, 3, 4)}
    assert set(d["facts"]) == set(d["display"]) == set(d["member_of"])
    assert d["reconcile_floor"] == t0 + 100 + hq_dispatch.WINDOW_S
    with hq_dispatch.json_write_lock(hq_dispatch._path()):
        d["seen"] = [t0, "d_" + "f" * 16]
        hq_dispatch._write(d)
    hq_dispatch.enqueue_row(_row(9), now=t0 + hq_dispatch.KEEP_S + 1000)
    d = doc()
    assert len(d["bundles"]) == 1 and d["seen"] == [t0, "d_" + "f" * 16]


# ---- the store is never rewritten when unreadable -----------------------------------------------


def test_an_unreadable_store_is_never_rewritten(hq, monkeypatch):
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    before = p.read_bytes()
    announce_automation()  # the enqueue fails, is logged and swallowed; the row stands
    assert len(notifications._read(notifications._notifications_path())) == 1
    assert run_pass(later()) == {"skipped": "store unreadable"}
    with pytest.raises(hq_dispatch.StoreUnreadable):
        hq_dispatch.mark_seen("d_" + "0" * 16)
    listing = hq_dispatch.join_listing(notifications.listing())
    assert all("dispatch" not in r for r in listing["notifications"])
    assert p.read_bytes() == before and hq.count() == 0


def test_an_unreadable_notification_store_skips_the_pass(hq):
    announce_automation()
    notifications._notifications_path().write_text("[[[")
    assert run_pass(later()) == {"skipped": "store unreadable"}
    assert hq.count() == 0


def test_the_store_is_private():
    hq_dispatch.enqueue_row(_row(1), now=1_800_000_000.0)
    assert hq_dispatch._path().stat().st_mode & 0o777 == 0o600


# ---- the bell's join ----------------------------------------------------------------------------


def test_the_listing_joins_only_written_lines_and_never_the_tag(hq, monkeypatch):
    announce_automation(aid="a", rid="1")
    run_pass(later())
    hq.status = 500
    announce_automation(aid="b", rid="2")
    run_pass(later(200))
    announce_automation(aid="c", rid="3")  # unsealed
    rows = {
        r["action_id"]: r
        for r in hq_dispatch.join_listing(notifications.listing())["notifications"]
    }
    (bid,) = [b for b, c in doc()["claims"].items() if c["state"] == "written"]
    assert rows["automation:a:1"]["dispatch"] == {
        "id": bid,
        "line": "HQ's line.",
        "by": rows["automation:a:1"]["dispatch"]["by"],
        "bundled": 1,
    }
    assert "dispatch" not in rows["automation:b:2"] and "dispatch" not in rows["automation:c:3"]
    # The stored row is unchanged: the tag, never text.
    stored = notifications._read(notifications._notifications_path())
    assert {r["dispatch"] for r in stored} == {"automation:a:1", "automation:b:2", "automation:c:3"}


def test_needs_you_rows_carry_their_episode_tag(hq, monkeypatch):
    announce_needs_you(monkeypatch)
    (row,) = notifications._read(notifications._notifications_path())
    assert row["dispatch"] == row["tag"] and row["tag"].startswith(f"needs-you:{SID}:")
    assert set(row) - {"dispatch"} == {
        "id",
        "ts",
        "read",
        "title",
        "project",
        "reason",
        "session_id",
        "engine",
        "action_id",
        "escalation",
        "auto_direction",
        "needs_you",
        "episode",
        "tag",
        "activity_at",
    }
    assert list(doc()["member_of"]) == [row["tag"]]


# ---- the unread cursor --------------------------------------------------------------------------


def _written(hq, aid: str, at: float) -> str:
    announce_automation(aid=aid, rid="r")
    run_pass(at)
    return next(
        b for b, c in doc()["claims"].items() if doc()["member_of"].get(f"automation:{aid}:r") == b
    )


def test_seen_is_clamped_to_written_bundles_and_never_moves_back(hq):
    t = later()
    a = _written(hq, "a", t)
    b = _written(hq, "b", t + 100)
    assert hq_dispatch.list_view()["new"] == 2
    assert hq_dispatch.mark_seen(b) == {"moved": True, "new": 0}
    assert hq_dispatch.mark_seen(a) == {"moved": False, "new": 0}  # older: never backwards
    assert hq_dispatch.mark_seen("d_" + "0" * 16)["moved"] is False  # unknown
    assert hq_dispatch.mark_seen(["not", "a", "string"])["moved"] is False
    hq.status = 500
    f = _written(hq, "f", t + 200)  # failed: not a dispatch
    assert hq_dispatch.mark_seen(f)["moved"] is False
    announce_automation(aid="u", rid="r")  # unsealed
    u = doc()["member_of"]["automation:u:r"]
    assert hq_dispatch.mark_seen(u)["moved"] is False
    assert doc()["seen"] == [doc()["lines"][b]["at"], b]


def test_the_cursor_orders_by_line_completion_not_bundle_opening(hq):
    t = later()
    announce_automation(aid="old", rid="r")
    hq_dispatch._housekeep(t)
    old = doc()["member_of"]["automation:old:r"]
    members = hq_dispatch._claim(old, t)
    assert members
    new = _written(hq, "new", t + 100)  # opened later, written first
    hq_dispatch.mark_seen(new)
    hq_dispatch._settle(old, t, {"text": "slow line", "contact": "HQ"}, t + 110)
    assert hq_dispatch.list_view()["new"] == 1  # the line that finished later is new


# ---- the routes ---------------------------------------------------------------------------------


@pytest.fixture
def client(auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    return TestClient(create_app(auth_cfg), base_url="https://testserver"), auth_cfg


def _login(c, cfg) -> str:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_the_routes_need_login_csrf_and_origin(client):
    c, cfg = client
    assert c.get("/api/hq/dispatches").status_code == 401
    assert c.get("/api/hq/dispatches/d_" + "0" * 16).status_code == 401
    assert c.post("/api/hq/dispatches/seen", json={"through": "x"}).status_code in (401, 403)
    csrf = _login(c, cfg)
    assert c.post("/api/hq/dispatches/seen", json={"through": "x"}).status_code == 403
    r = c.post(
        "/api/hq/dispatches/seen",
        json={"through": "x"},
        headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = c.post(
        "/api/hq/dispatches/seen",
        json={"through": "d_" + "0" * 16},
        headers={"X-CSRF-Token": csrf, "Origin": cfg.origin},
    )
    assert r.status_code == 200 and r.json() == {"moved": False, "new": 0}


def test_the_routes_list_and_read_one(client, hq):
    c, cfg = client
    _login(c, cfg)
    announce_automation()
    run_pass(later())
    (bid,) = doc()["lines"]
    listed = c.get("/api/hq/dispatches").json()
    assert listed["new"] == 1 and listed["unreadable"] is False
    assert [d["id"] for d in listed["dispatches"]] == [bid]
    one = c.get(f"/api/hq/dispatches/{bid}").json()["dispatch"]
    assert one["members"] == [
        {
            "class": "automation failed",
            "title": "Automation failed: nightly docs",
            "reason": "failed: boom",
            "link": "/mission/automations/abc",
            "ts": pytest.approx(time.time(), abs=60),
        }
    ]
    for missing in ("d_" + "0" * 16, "not-an-id", "d_XYZ"):
        r = c.get(f"/api/hq/dispatches/{missing}")
        assert r.status_code == 404 and r.json() == {"detail": "that dispatch isn't here"}
    bell = c.get("/api/pulse/notifications").json()["notifications"]
    assert bell[0]["dispatch"]["id"] == bid


# ---- Hermes 7619 ------------------------------------------------------------------------------


CRED = "password=hunter2hunter2"


def _boom(*a, **k):
    """A lost enqueue: it raises (swallowed by the producer), the next pass reconciles the row."""
    raise OSError("lost")


@pytest.mark.parametrize("path", ["enqueue", "reconcile"])
def test_a_credential_shaped_project_never_reaches_the_model(path, hq, monkeypatch):
    """Finding 1: EVERY label passes the HQ text boundary — a project too, on both paths."""
    with monkeypatch.context() as m:
        if path == "reconcile":
            m.setattr(hq_dispatch, "enqueue", _boom)
        announce_needs_you(monkeypatch, project=CRED)
        announce_escalation(title="Release train")
    assert bool(joined()) is (path == "enqueue")
    t = later(hq_dispatch.RECONCILE_GRACE_S + 1)
    run_pass(t)
    run_pass(t + 61)
    assert hq.count() == 1
    assert "hunter2" not in hq.bodies()[0]
    by = {e["class"]: e for e in request_entries(hq)}
    assert "project" not in by["needs you"]  # dropped, never sent altered
    assert by["mission escalation"]["project"] == "api"  # a clean label still goes
    for e in by.values():
        assert e.get("engine") in (None, hq_dispatch._engine_label("claude"))


@pytest.mark.parametrize("path", ["enqueue", "reconcile"])
def test_labels_are_dropped_when_the_secrets_snapshot_is_unavailable(path, hq, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "_secrets_snapshot", lambda: None)
        if path == "reconcile":
            m.setattr(hq_dispatch, "enqueue", _boom)
            announce_needs_you(monkeypatch, project="api")
            announce_automation(name="nightly docs")
        else:
            announce_needs_you(monkeypatch, project="api")
            announce_automation(name="nightly docs")
            t = later(hq_dispatch.RECONCILE_GRACE_S + 1)
            run_pass(t)
            run_pass(t + 61)
    if path == "reconcile":
        with monkeypatch.context() as m:
            m.setattr(hq_dispatch, "_secrets_snapshot", lambda: None)
            hq_dispatch._housekeep(later(hq_dispatch.RECONCILE_GRACE_S + 1))
        run_pass(later(hq_dispatch.RECONCILE_GRACE_S + 62))
    assert hq.count() == 1
    for e in request_entries(hq):
        assert not set(hq_dispatch.LABEL_KEYS) & set(e), e
    assert "nightly docs" not in hq.bodies()[0] and '"api"' not in hq.bodies()[0]


def _tagged_row(aid: str, ts: float, monkeypatch) -> None:
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: ts)
        notifications.add(
            title="t",
            project="",
            session_id="",
            engine="",
            action_id=f"automation:{aid}:r",
            dispatch_tag=f"automation:{aid}:r",
        )


@pytest.mark.parametrize(("gap", "bundles"), [(3600, 2), (30, 1)])
def test_recovered_rows_bundle_by_their_own_event_time(gap, bundles, hq, monkeypatch):
    """Finding 2: after a writer outage, rows an hour apart are two bundles, not one; rows within
    the window are one. A recovered bundle whose window has passed seals on that same pass."""
    t0 = time.time() - 7200
    _tagged_row("a", t0, monkeypatch)
    _tagged_row("b", t0 + gap, monkeypatch)
    run_pass(time.time())  # one pass, the writer back after its outage
    d = doc()
    assert len(d["bundles"]) == bundles
    assert all(b["sealed"] for b in d["bundles"].values())
    assert sorted(b["opened_at"] for b in d["bundles"].values()) == (
        [t0, t0 + gap] if bundles == 2 else [t0]
    )
    assert hq.count() == bundles and len(d["lines"]) == bundles


def test_a_malformed_reply_fails_its_bundle_and_the_pass_goes_on(hq):
    """Finding 3: a 200 whose `message` is not an object settles that bundle `failed: malformed`;
    the next due bundle is still written in the same pass."""
    t0 = 1_800_000_000.0
    first = hq_dispatch.enqueue_row(_row(1), now=t0)
    second = hq_dispatch.enqueue_row(_row(2), now=t0 + 100)
    hq.raw = [{"choices": [{"message": "not an object", "finish_reason": "stop"}]}]
    assert run_pass(t0 + 300) == {"claimed": 2, "written": 1, "failed": 1}
    d = doc()
    assert (d["claims"][first]["state"], d["claims"][first]["code"]) == ("failed", "malformed")
    assert d["claims"][second]["state"] == "written" and list(d["lines"]) == [second]


@pytest.mark.parametrize(
    "body",
    [
        {"choices": "nope"},
        {"choices": []},
        {"choices": ["x"]},
        {"choices": [{"message": {"content": ["a", "list"]}}]},
        ["not", "a", "dict"],
    ],
)
def test_other_malformed_shapes_settle_malformed(body, hq):
    hq.raw = [body]
    announce_automation()
    run_pass(later())
    (c,) = doc()["claims"].values()
    assert (c["state"], c["code"]) == ("failed", "malformed")


@pytest.mark.parametrize(
    ("tz", "ts", "contact"),
    [
        # The SAME instants and ids as web/src/components/hq/contacts.test.ts (finding 5).
        ("Pacific/Auckland", 1793575800, "russia-woman"),  # 23:30 UTC 1 Nov = 2 Nov there
        ("America/Los_Angeles", 1793588400, "europe-man"),  # 03:00 UTC 2 Nov = 1 Nov there
    ],
)
def test_random_uses_the_utc_day_whatever_the_hosts_timezone(tz, ts, contact, monkeypatch):
    import datetime

    try:
        with monkeypatch.context() as m:
            m.setenv("TZ", tz)
            time.tzset()
            local = datetime.datetime.fromtimestamp(ts).date()
            utc = datetime.datetime.fromtimestamp(ts, datetime.UTC).date()
            assert local != utc  # the boundary is really exercised
            assert hq_contacts.speaker("random", ts) == contact
    finally:
        time.tzset()  # the context restored TZ; re-read it


# ---- Hermes 7620 ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 422, 501])
def test_a_refusal_is_one_request_never_a_degrade_retry(status, hq, monkeypatch):
    """Finding 1: the transport's thinking-field degrade would POST a second time; a claim makes
    exactly one request, and the refusal settles the bundle failed."""
    from agent_sessions import review

    monkeypatch.setattr(review, "_thinking_refused", set())
    hq.status = status
    announce_automation()
    run_pass(later())
    assert hq.count() == 1
    sent_thinking = {"chat_template_kwargs", "reasoning_effort", "reasoning", "thinking"} & set(
        hq.requests[0]
    )
    assert sent_thinking, "the request carried no thinking field, so no retry was ever possible"
    (c,) = doc()["claims"].values()
    assert (c["state"], c["code"]) == ("failed", f"http_{status}")


def test_a_fresh_row_is_not_reconciled_while_its_push_may_still_run(hq, monkeypatch):
    """Finding 2: reconcile admits only rows older than the grace; a fresh one waits."""
    g = hq_dispatch.RECONCILE_GRACE_S
    now = time.time()
    _tagged_row("old", now - g - 1, monkeypatch)
    _tagged_row("fresh", now - g + 30, monkeypatch)
    hq_dispatch._housekeep(now)
    assert list(doc()["member_of"]) == ["automation:old:r"]
    hq_dispatch._housekeep(now + 31)
    assert set(doc()["member_of"]) == {"automation:old:r", "automation:fresh:r"}


def test_a_held_dispatch_lock_never_makes_a_needs_you_sync_busy(hq, monkeypatch):
    """Finding 3: the hand-off waits on the dispatch flock OUTSIDE the single-flight lock, so a
    second sync still opens (and records) its new episodes while the flock is held."""
    import threading

    from agent_sessions.routes import pulse as pulse_routes

    sids = ["claude:aaaaaaaa-1111-4111-8111-111111111111"]

    def needing(*_a, **_k):
        rows = [
            {"id": s, "title": s, "project": {"name": "api"}, "engine": "claude",
             "kind": "question", "action": {}}
            for s in list(sids)
        ]  # fmt: skip
        return {"rows": rows, "needs_you_ids": [r["id"] for r in rows]}

    monkeypatch.setattr(pulse_routes, "build_needs_you", needing)
    results: list[dict] = []
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with hq_dispatch.json_write_lock(hq_dispatch._path()):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    first = threading.Thread(target=lambda: results.append(needs_you_notify.sync_once()))
    first.start()  # opens its episode, then waits on the flock in the hand-off
    deadline = time.monotonic() + 10
    while not notifications._read(notifications._notifications_path()):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    # Its reservation gives up on the held flock within RESERVE_WAIT_S, it pushes, and releases the
    # single-flight lock — while it still waits, outside it, to hand the episode off.
    while needs_you_notify._lock.locked():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert first.is_alive()
    sids.append("claude:bbbbbbbb-2222-4222-8222-222222222222")
    second = threading.Thread(target=lambda: results.append(needs_you_notify.sync_once()))
    second.start()
    deadline = time.monotonic() + 10
    while len(notifications._read(notifications._notifications_path())) < 2:
        assert time.monotonic() < deadline, f"the second sync did not open its episode: {results}"
        time.sleep(0.02)
    assert {"skipped": "busy"} not in results
    release.set()
    for t in (holder, first, second):
        t.join(30)
    assert sorted(r.get("opened") for r in results) == [1, 1]
    assert len(doc()["member_of"]) == 2  # both handed off once the flock was free


# ---- Hermes 7623: the hand-off reservation ------------------------------------------------------


def _automation_result(aid="abc", rid="r1", name="nightly docs", outcome="refused"):
    return {
        "automation": {"id": aid, "name": name},
        "run": {"id": rid, "outcome": outcome, "reason": "boom"},
        "episode": ("open", f"automation:{aid}:{rid}"),
    }


@pytest.mark.parametrize("producer", ["automation", "needs-you"])
def test_a_fanout_longer_than_the_grace_keeps_its_event_for_the_producer(producer, hq, monkeypatch):
    """Finding 1: the producer reserves its tag before the push; a writer pass 10 minutes into an
    in-flight fanout (this process, alive) leaves it alone; the push completes and enqueues with
    the full facts; dispatched once, with the name and the outcome."""
    seen_during: list[dict] = []

    def slow_fanout(rec, *a, **k):
        assert hq_dispatch._drain_reservations()  # the background worker has written it
        assert doc()["pending"][rec["dispatch"]]["pid"] == os.getpid()
        seen_during.append(run_pass(time.time() + 600))  # the writer, mid-push
        seen_during.append(dict(doc()["member_of"]))
        return {"sent": 1, "pruned": 0, "failed": 0}

    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", slow_fanout)
    if producer == "needs-you":
        announce_needs_you(monkeypatch)
    else:
        automation_runner.notify(_automation_result())
    assert seen_during[1] == {}, "the writer reconciled an event whose push was still running"
    assert doc()["pending"] == {}  # the hand-off took the reservation away in its own write
    run_pass(time.time() + 700)
    run_pass(time.time() + 5000)
    assert hq.count() == 1
    if producer == "needs-you":
        assert request_entries(hq)[0]["project"] == "api"
        return
    assert request_entries(hq) == [
        {"class": "automation failed", "automation": "nightly docs", "outcome": "refused",
         "age_min": 0}
    ]  # fmt: skip


@pytest.mark.parametrize("owner", ["another pid", "same pid, another start"])
def test_a_dead_owners_reservation_is_stale_and_reconciled_after_the_grace(owner, hq, monkeypatch):
    g = hq_dispatch.RECONCILE_GRACE_S
    t0 = time.time() - g - 10
    _tagged_row("abc", t0, monkeypatch)
    me = hq_dispatch._proc_start(os.getpid())
    pending = {
        "another pid": {"pid": 2**22 + 7, "start": me, "at": t0},
        "same pid, another start": {"pid": os.getpid(), "start": "x:1", "at": t0},
    }[owner]
    with hq_dispatch.json_write_lock(hq_dispatch._path()):
        d = hq_dispatch._empty()
        d["pending"]["automation:abc:r"] = pending
        hq_dispatch._write(d)
    run_pass(time.time())
    assert list(doc()["member_of"]) == ["automation:abc:r"] and doc()["pending"] == {}
    assert hq.count() == 1


def test_a_live_owners_reservation_is_honoured_however_old(hq, monkeypatch):
    """Hermes 7625: a reservation whose owner lives (same pid and start) is never stale — here two
    hours old and not in this process's in-flight set (the record a restarted writer would read)."""
    t0 = time.time() - hq_dispatch.RECONCILE_GRACE_S - 10
    _tagged_row("abc", t0, monkeypatch)
    assert not hq_dispatch.in_flight("automation:abc:r")
    with hq_dispatch.json_write_lock(hq_dispatch._path()):
        d = hq_dispatch._empty()
        d["pending"]["automation:abc:r"] = {
            "pid": os.getpid(), "start": hq_dispatch._proc_start(os.getpid()), "at": t0 - 7200,
        }  # fmt: skip
        hq_dispatch._write(d)
    run_pass(time.time())
    assert joined() == {} and hq.count() == 0


def test_a_failed_reservation_leaves_the_push_byte_identical(hq, monkeypatch, tmp_path):
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    _subs_file = notifications._subs_path()
    _subs_file.parent.mkdir(parents=True, exist_ok=True)
    _subs_file.write_text(
        json.dumps([{"id": "d", "endpoint": "https://push.example/x", "keys": {}}])
    )
    sent: list[bytes] = []
    monkeypatch.setattr(notifications, "_send_all", lambda payload, *a, **k: sent.append(payload))

    def produce(store: str) -> None:
        monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / store))
        automation_runner.notify(_automation_result())

    def broken(*a, **k):
        raise OSError("the dispatch store is full")

    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "_try_lock", broken)  # every reservation write fails
        produce("broken.json")
    produce("normal.json")
    assert len(sent) == 2 and sent[0] == sent[1]
    rows_a = json.loads((tmp_path / "broken.json").read_text())
    rows_b = json.loads((tmp_path / "normal.json").read_text())
    assert rows_a == rows_b


@pytest.mark.parametrize("claimed", [False, True])
def test_a_reconciled_member_is_upgraded_by_the_producers_enqueue_until_claimed(
    claimed, hq, monkeypatch
):
    """Reconciled first (here: a writer that cannot see this push — no persisted reservation and,
    as for another process, no in-flight entry — after the grace), the producer's later enqueue
    upgrades the member's facts with its enqueue-only fields — unless the bundle is already
    claimed, when it stays as it is."""
    g = hq_dispatch.RECONCILE_GRACE_S

    def late_fanout(rec, *a, **k):
        due = hq_dispatch._housekeep(time.time() + g + 10)  # reconciled, no claim yet
        if claimed:
            assert hq_dispatch._claim(due[0], time.time() + g + 10) is not None
        return {"sent": 1}

    monkeypatch.setattr(hq_dispatch, "reserve", lambda *a, **k: False)
    monkeypatch.setattr(hq_dispatch, "in_flight", lambda tag: False)
    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", late_fanout)
    automation_runner.notify(_automation_result())
    (facts,) = doc()["facts"].values()
    if claimed:
        assert set(facts) == {"class", "age_min"}  # as it was claimed
    else:
        assert (facts["automation"], facts["outcome"]) == ("nightly docs", "refused")
        run_pass(time.time() + g + 20)
        (entry,) = request_entries(hq)
        assert (entry["automation"], entry["outcome"]) == ("nightly docs", "refused")


def test_the_strict_read_refuses_a_malformed_reservation():
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = hq_dispatch._empty()
    d["pending"]["automation:abc:r"] = {"pid": "1", "start": "x", "at": 1.0}
    p.write_text(json.dumps(d))
    with pytest.raises(hq_dispatch.StoreUnreadable):
        hq_dispatch._read_strict()


_POISONS = {
    "an infinite reconcile floor": lambda d: d.update(reconcile_floor=float("inf")),
    "a NaN cursor": lambda d: d.update(seen=[float("nan"), ""]),
    "an infinite bundle time": lambda d: d["bundles"].update(
        {
            "d_" + "a" * 16: {
                "opened_at": 1.0,
                "seals_at": float("inf"),
                "sealed": False,
                "members": [],
            }
        }
    ),
    "a facts entry that is not a mapping": lambda d: d["facts"].update({"automation:abc:r": "x"}),
    "a display entry that is not a mapping": lambda d: d["display"].update({"automation:abc:r": 1}),
    "a membership that is not a bundle id": lambda d: d["member_of"].update(
        {"automation:abc:r": 7}
    ),
    # Hermes 7743, finding 2: values that used to raise instead of reading as unreadable.
    "a 401-digit reconcile floor": lambda d: d.update(reconcile_floor=10**400),
    "a claim state that is a list": lambda d: d["claims"].update(
        {"d_" + "b" * 16: {"state": [], "at": 1.0, "code": ""}}
    ),
    # Hermes 7743, finding 1: a member is exactly what the cleaner can produce, or nothing is read.
    "a member with an extra fact": lambda d: _poison_member(d, unexpected="leak"),
    "a member of an unknown class": lambda d: _poison_member(d, **{"class": "other"}),
    "a label beyond its cap": lambda d: _poison_member(d, automation="x" * 81),
    "a label that is not text": lambda d: _poison_member(d, automation=["x"]),
    "an unknown outcome": lambda d: _poison_member(d, outcome="exploded"),
    "an age that is a boolean": lambda d: _poison_member(d, age_min=True),
}


def _member(**over) -> dict:
    """A stored automation member as the cleaner writes it, with ``over`` applied."""
    base = {
        "class": hq_dispatch.CLASS_AUTOMATION,
        "automation": "nightly docs",
        "outcome": "failed",
    }
    return {**base, "age_min": 3, **over}


def _poison_member(d: dict, **over) -> None:
    d["facts"]["automation:abc:r"] = _member(**over)


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_a_poisoned_store_is_unreadable_to_every_reader_and_writer(hq, poison):
    """Hermes 7681, finding 2: a document that parses but holds a non-finite time or a malformed
    entry is refused by the strict read, so no pass, claim or list acts on it, and nothing on disk
    is rewritten."""
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = hq_dispatch._empty()
    _POISONS[poison](d)
    p.write_text(json.dumps(d))
    before = p.read_bytes()
    with pytest.raises(hq_dispatch.StoreUnreadable):
        hq_dispatch._read_strict()
    with pytest.raises(hq_dispatch.StoreUnreadable):
        hq_dispatch._housekeep(time.time())
    with pytest.raises(hq_dispatch.StoreUnreadable):
        hq_dispatch.list_view()
    assert run_pass(time.time()) == {"skipped": "store unreadable"}
    assert p.read_bytes() == before


def test_a_well_formed_member_is_readable():
    """The other side: the shape the cleaner writes passes the strict read."""
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = hq_dispatch._empty()
    d["facts"]["automation:abc:r"] = _member()
    p.write_text(json.dumps(d))
    assert hq_dispatch._read_strict()["facts"]["automation:abc:r"] == _member()


def _sealed_bundle_with(member: dict) -> None:
    bid, tag = "d_" + "c" * 16, "automation:abc:r"
    d = hq_dispatch._empty()
    now = time.time()
    d["bundles"][bid] = {
        "opened_at": now - 120,
        "seals_at": now - 60,
        "sealed": True,
        "members": [tag],
    }
    d["member_of"][tag] = bid
    d["facts"][tag] = member
    d["display"][tag] = {"class": "automation failed", "title": "t", "reason": "", "link": ""}
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


def test_a_stored_member_with_an_extra_fact_never_reaches_the_model(hq):
    p = hq_dispatch._path()
    _sealed_bundle_with(_member(unexpected="leak"))
    before = p.read_bytes()
    assert run_pass(time.time()) == {"skipped": "store unreadable"}
    assert hq.requests == [] and p.read_bytes() == before


def test_the_same_bundle_well_formed_is_written(hq):
    """The control: only the extra fact made the pass above skip."""
    _sealed_bundle_with(_member())
    run_pass(time.time())
    (entry,) = request_entries(hq)
    assert "unexpected" not in entry and entry["automation"] == "nightly docs"


@pytest.mark.parametrize("poison", ["a 401-digit reconcile floor", "a claim state that is a list"])
def test_the_routes_answer_unreadable_for_a_store_that_used_to_raise(client, poison):
    c, cfg = client
    _login(c, cfg)
    p = hq_dispatch._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    d = hq_dispatch._empty()
    _POISONS[poison](d)
    p.write_text(json.dumps(d))
    listed = c.get("/api/hq/dispatches")
    assert listed.status_code == 200 and listed.json()["unreadable"] is True
    assert c.get("/api/hq/dispatches/d_" + "0" * 16).status_code == 503


# ---- Hermes 7625: the in-flight fence and a non-blocking reservation ---------------------------


def test_a_live_push_is_never_reconciled_whatever_its_age(hq, monkeypatch):
    """The in-flight set fences a live push in this process through the real housekeeping pass, at
    +1 h 1 min and at +25 h."""
    during: list[dict] = []

    def very_slow_fanout(rec, *a, **k):
        tag = rec["dispatch"]
        assert hq_dispatch.in_flight(tag)
        for dt in (3660, 25 * 3600):
            hq_dispatch._housekeep(time.time() + dt)
            during.append(dict(doc()["member_of"]))
        return {"sent": 1}

    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", very_slow_fanout)
    automation_runner.notify(_automation_result())
    assert during == [{}, {}]
    assert not hq_dispatch.in_flight("automation:abc:r1")
    (facts,) = doc()["facts"].values()
    assert (facts["automation"], facts["outcome"]) == ("nightly docs", "refused")


def test_the_in_flight_set_alone_fences_when_the_reservation_could_not_be_written(hq, monkeypatch):
    """The store is busy when the producer reserves: no persisted reservation. The in-flight set
    alone keeps reconcile off the tag until the hand-off."""
    import threading

    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with hq_dispatch.json_write_lock(hq_dispatch._path()):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(10)
    during: list = []

    def slow_fanout(rec, *a, **k):
        assert hq_dispatch._drain_reservations()  # its write found the store busy, and skipped
        release.set()  # the store is free again, mid-push
        holder.join(10)
        during.append(dict(doc()["pending"]))
        hq_dispatch._housekeep(time.time() + 600)
        during.append(dict(doc()["member_of"]))
        return {"sent": 1}

    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", slow_fanout)
    automation_runner.notify(_automation_result())
    assert during == [{}, {}]  # no persisted reservation, and still not reconciled
    run_pass(time.time() + 700)
    (entry,) = request_entries(hq)
    assert (entry["automation"], entry["outcome"]) == ("nightly docs", "refused")


def test_tags_leave_the_in_flight_set_on_every_path(hq, monkeypatch):
    automation_runner.notify(_automation_result(aid="ok", rid="r"))
    assert not hq_dispatch._inflight

    def boom(*a, **k):
        raise RuntimeError("the push blew up")

    with monkeypatch.context() as m:
        m.setattr(notifications, "list_subscriptions", boom)
        automation_runner.notify(_automation_result(aid="bad", rid="r"))  # logged, swallowed
    assert not hq_dispatch._inflight
    with monkeypatch.context() as m:
        m.setattr(needs_you_notify, "_deliver", boom)
        with pytest.raises(RuntimeError):
            announce_needs_you(monkeypatch)
    assert not hq_dispatch._inflight


# ---- Hermes 7678: the fence is up before the row can be read ---------------------------------


def _reconcile_right_after_the_row_write(monkeypatch) -> list[dict]:
    """The bell write lands, so the row is readable, and the producer then stalls past the grace
    before it returns: a real reconcile pass runs right there, in that window."""
    real = notifications._write
    seen: list[dict] = []

    def write_then_stall(p, rows, *a, **k):
        real(p, rows, *a, **k)
        tagged = [r["dispatch"] for r in rows if isinstance(r.get("dispatch"), str)]
        if tagged and not seen:
            hq_dispatch._housekeep(time.time() + hq_dispatch.RECONCILE_GRACE_S + 60)
            seen.append(
                {
                    "persisted": tagged,
                    "fenced": [t for t in tagged if hq_dispatch.in_flight(t)],
                    "member_of": dict(doc()["member_of"]),
                }
            )

    monkeypatch.setattr(notifications, "_write", write_then_stall)
    return seen


@pytest.mark.parametrize("producer", ["automation", "needs_you", "escalation"])
def test_a_row_is_fenced_before_it_can_be_read(hq, monkeypatch, producer):
    seen = _reconcile_right_after_the_row_write(monkeypatch)
    if producer == "automation":
        automation_runner.notify(_automation_result())
    elif producer == "needs_you":
        announce_needs_you(monkeypatch)
    else:
        announce_escalation()
    (at_the_write,) = seen
    assert at_the_write["persisted"] and at_the_write["fenced"] == at_the_write["persisted"]
    assert at_the_write["member_of"] == {}  # the stalled pass took nothing
    assert not hq_dispatch._inflight
    assert len(doc()["member_of"]) == 1  # the producer's own hand-off took it


def test_a_failed_needs_you_write_leaves_nothing_in_flight(hq, monkeypatch):
    real = notifications._write

    def fail_tagged(p, rows, *a, **k):
        if any(isinstance(r.get("dispatch"), str) for r in rows):
            raise OSError("disk full")
        return real(p, rows, *a, **k)

    monkeypatch.setattr(notifications, "_write", fail_tagged)
    with pytest.raises(OSError):
        announce_needs_you(monkeypatch)
    assert not hq_dispatch._inflight


def test_a_second_producer_ending_never_lifts_the_first_ones_fence(hq, monkeypatch):
    """Hermes 7681, finding 1: two producers hold the same tag; the second one's whole flight ends
    while the first is still pushing, and a pass past the grace still leaves the event alone."""
    during: list[dict] = []

    def slow_fanout(rec, *a, **k):
        tag = rec["dispatch"]
        with hq_dispatch.flight([rec]):  # a second producer for the same episode, start to end
            pass
        during.append({"fenced": hq_dispatch.in_flight(tag)})
        hq_dispatch._housekeep(time.time() + hq_dispatch.RECONCILE_GRACE_S + 60)
        during.append(dict(doc()["member_of"]))
        return {"sent": 1}

    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", slow_fanout)
    automation_runner.notify(_automation_result())
    assert during == [{"fenced": True}, {}]
    assert not hq_dispatch._inflight
    (facts,) = doc()["facts"].values()
    assert (facts["automation"], facts["outcome"]) == ("nightly docs", "refused")


def test_a_failed_hand_off_releases_only_when_the_last_holder_ends(hq, monkeypatch):
    tag = "automation:abc:r1"
    released: list[str] = []
    monkeypatch.setattr(hq_dispatch, "release", lambda t, **k: released.append(t))
    other = hq_dispatch.begin_flight([{"dispatch": tag}])  # another producer still holds it

    def boom(*a, **k):
        raise OSError("lost")

    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "enqueue", boom)
        automation_runner.notify(_automation_result())
    assert hq_dispatch.in_flight(tag) and released == []
    hq_dispatch.end_flight(other)
    assert not hq_dispatch.in_flight(tag) and released == [tag]
    assert not hq_dispatch._release_owed


def _slow_reservation_writes(monkeypatch, mode: str):
    """The worker's reservation write, made slow (300 ms) or stalled, HOLDING the store's lock as a
    slow fsync would. Returns the event that un-stalls it."""
    import threading

    real = hq_dispatch._write_reservation
    go = threading.Event()

    def slow(tags, now, path=None):
        with hq_dispatch.json_write_lock(path or hq_dispatch._path()):
            if mode == "slow":
                time.sleep(0.3)
            else:
                go.wait(30)
        return real(tags, now, path)

    monkeypatch.setattr(hq_dispatch, "_write_reservation", slow)
    return go


@pytest.mark.parametrize("mode", ["slow", "stalled"])
def test_a_slow_reservation_write_never_delays_the_needs_you_push(mode, hq, monkeypatch):
    """Hermes 7627, finding 2: the persisted reservation is off the critical path. With its write
    taking 300 ms (or stalled), a sync opening three episodes reaches `_deliver` with under 50 ms
    added after `sync_needs_you`, and the next sync is admitted, not busy."""
    import threading

    from agent_sessions.routes import pulse as pulse_routes

    sids = [f"claude:{c * 8}-1111-4111-8111-111111111111" for c in "abc"]

    def needing(*_a, **_k):
        rows = [
            {"id": s, "title": s, "project": {"name": "api"}, "engine": "claude",
             "kind": "question", "action": {}}
            for s in list(sids)
        ]  # fmt: skip
        return {"rows": rows, "needs_you_ids": [r["id"] for r in rows]}

    monkeypatch.setattr(pulse_routes, "build_needs_you", needing)
    go = _slow_reservation_writes(monkeypatch, mode)
    real_sync, real_deliver = notifications.sync_needs_you, needs_you_notify._deliver
    marks: dict[str, float] = {}

    def timed_sync(*a, **k):
        try:
            return real_sync(*a, **k)
        finally:
            marks.setdefault("synced", time.monotonic())

    def timed_deliver(res, push):
        marks.setdefault("delivering", time.monotonic())
        return real_deliver(res, push)

    monkeypatch.setattr(notifications, "sync_needs_you", timed_sync)
    monkeypatch.setattr(needs_you_notify, "_deliver", timed_deliver)
    results: list[dict] = []
    try:
        first = threading.Thread(target=lambda: results.append(needs_you_notify.sync_once()))
        first.start()
        deadline = time.monotonic() + 10
        while "delivering" not in marks:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        added = marks["delivering"] - marks["synced"]
        assert added < 0.05, f"{added * 1000:.0f} ms between the episodes and their push"
        while needs_you_notify._lock.locked():  # past its push (its hand-off may wait the store)
            assert time.monotonic() < deadline
            time.sleep(0.005)
        sids.append("claude:dddddddd-1111-4111-8111-111111111111")
        second = threading.Thread(target=lambda: results.append(needs_you_notify.sync_once()))
        second.start()
        while len(notifications._read(notifications._notifications_path())) < 4:
            assert time.monotonic() < deadline, results
            time.sleep(0.01)
        assert {"skipped": "busy"} not in results
    finally:
        go.set()
    for t in (first, second):
        t.join(30)
    assert sorted(r.get("opened") for r in results) == [1, 3]
    assert hq_dispatch._drain_reservations()
    assert len(doc()["member_of"]) == 4 and not hq_dispatch._inflight and doc()["pending"] == {}


@pytest.mark.parametrize("mode", ["slow", "stalled"])
def test_a_slow_reservation_write_never_delays_the_automation_fanout(mode, hq, monkeypatch):
    go = _slow_reservation_writes(monkeypatch, mode)
    real_add = notifications.add
    marks: dict[str, float] = {}

    def timed_add(*a, **k):
        try:
            return real_add(*a, **k)
        finally:
            marks["added"] = time.monotonic()

    def timed_fanout(rec, *a, **k):
        marks["fanout"] = time.monotonic()
        return {"sent": 1}

    monkeypatch.setattr(notifications, "add", timed_add)
    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", timed_fanout)
    import threading

    t = threading.Thread(target=lambda: automation_runner.notify(_automation_result()))
    try:
        t.start()
        deadline = time.monotonic() + 10
        while "fanout" not in marks:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        added = marks["fanout"] - marks["added"]
        assert added < 0.05, f"{added * 1000:.0f} ms between the row and its fanout"
    finally:
        go.set()
    t.join(30)
    assert hq_dispatch._drain_reservations()
    assert list(doc()["member_of"]) == ["automation:abc:r1"] and doc()["pending"] == {}


def test_a_reservation_landing_after_the_hand_off_never_recreates_a_pending_entry(hq, monkeypatch):
    import threading

    real = hq_dispatch._write_reservation
    go = threading.Event()
    wrote: list[bool] = []

    def late(tags, now, path=None):
        go.wait(30)  # the worker is held until the hand-off has run
        wrote.append(real(tags, now, path))
        return wrote[-1]

    monkeypatch.setattr(hq_dispatch, "_write_reservation", late)
    monkeypatch.setattr(notifications, "list_subscriptions", lambda *a, **k: [{"id": "d"}])
    monkeypatch.setattr(notifications, "fanout", lambda *a, **k: {"sent": 1})
    automation_runner.notify(_automation_result())
    assert list(doc()["member_of"]) == ["automation:abc:r1"]
    assert not hq_dispatch.in_flight("automation:abc:r1")
    go.set()
    assert hq_dispatch._drain_reservations()
    assert wrote == [False] and doc()["pending"] == {}


def test_a_reservation_landing_after_a_failed_hand_off_never_recreates_one(hq, monkeypatch):
    """The hand-off failed and gave its reservation back: the tag left the in-flight set first, so
    a reservation still queued is not written, and the next pass reconciles the event."""
    import threading

    real = hq_dispatch._write_reservation
    go = threading.Event()
    wrote: list[bool] = []

    def late(tags, now, path=None):
        go.wait(30)
        wrote.append(real(tags, now, path))
        return wrote[-1]

    def boom(*a, **k):
        raise OSError("lost")

    real_release = hq_dispatch.release

    def release_then_let_the_worker_run(tag, **k):
        real_release(tag, **k)
        go.set()  # the queued reservation runs right after the release, still inside the hand-off
        assert hq_dispatch._drain_reservations()

    monkeypatch.setattr(hq_dispatch, "_write_reservation", late)
    monkeypatch.setattr(hq_dispatch, "release", release_then_let_the_worker_run)
    with monkeypatch.context() as m:
        m.setattr(hq_dispatch, "enqueue", boom)
        automation_runner.notify(_automation_result())
    go.set()
    assert hq_dispatch._drain_reservations()
    assert wrote == [False] and (not hq_dispatch._path().exists() or doc()["pending"] == {})
    run_pass(time.time() + hq_dispatch.RECONCILE_GRACE_S + 10)
    assert hq.count() == 1
