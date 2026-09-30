"""Automations (#1201 Phase 1): store, consent, scheduler, runner and routes.

Every rule of the issue's Phase 1 has its named regression here. Nothing reaches a real engine:
dispatch and send are faked at the seams the runner calls (`headless_dispatch.dispatch`,
`session_input.send_input`, `template_send.resolve_target`), and clocks are injected, never slept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agent_sessions import automation_effect_lock as effect_lock
from agent_sessions import (
    automation_loop,
    automation_runner,
    headless_dispatch,
    missions,
    notifications,
    prefs,
    session_input,
    template_send,
    template_vars,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import templates as tstore
from agent_sessions.routes import automations as routes

SESSION = "claude:11111111-2222-3333-4444-555555555555"


# ---- helpers ------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    """conftest defaults the kill switch OFF for every test; these tests are about the loop."""
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    # An operator write racing a run waits this long for the effect lock (10 s in production).
    monkeypatch.setattr(effect_lock, "WRITER_WAIT_S", 0.2)
    automation_runner._TASKS.clear()
    yield
    automation_runner._TASKS.clear()


def _send_config(**over) -> dict:
    cfg = {
        "name": "nightly nudge",
        "trigger": {"kind": "manual"},
        "action": {"kind": "send_to_session", "session_key": SESSION, "message": {"text": "go"}},
        "policy": {},
    }
    cfg.update(over)
    return cfg


def _hourly(**policy) -> dict:
    return _send_config(
        trigger={
            "kind": "schedule",
            "cadence": {"kind": "interval", "every": 1, "unit": "hours"},
            "tz": "UTC",
        },
        policy=policy,
    )


def _create(cfg: dict) -> dict:
    return routes._create(cfg)


def _enable(aid: str) -> dict:
    pub = routes.public(store.get(aid))
    return routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )


def _made(cfg: dict, *, enabled: bool = True, active_since: float | None = None) -> str:
    aid = _create(cfg)["id"]
    if enabled:
        _enable(aid)
    if active_since is not None:
        store.mutate(aid, lambda _r: {"active_since": active_since})
    return aid


def _runs(aid: str) -> list[dict]:
    return store.list_runs(aid, limit=200)["runs"]


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def spawned(monkeypatch):
    """Capture what the scheduler would execute, without executing it."""
    out: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: out.append(run))
    return out


def _dead_token() -> str:
    """The owner token of a process that has exited."""
    import subprocess
    import sys

    p = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return f"{int(p.stdout)}:12345:dead"


def _orphan(run_id: str, token: str | None = None) -> None:
    """Make a run look like its owner process died (the test process itself is always alive)."""
    con = sqlite3.connect(store.db_path())
    con.execute("UPDATE automation_runs SET owner=? WHERE id=?", (token or _dead_token(), run_id))
    con.commit()
    con.close()


def _hour_start() -> float:
    return math.floor(time.time() / 3600) * 3600.0


# ---- cadence: DST skip and repeat ---------------------------------------------------------------


def _berlin_daily(hhmm: str) -> dict:
    return model.validate_trigger(
        {"kind": "schedule", "cadence": {"kind": "daily", "time": hhmm}, "tz": "Europe/Berlin"}
    )


def test_dst_skipped_local_time_runs_once_at_the_next_valid_minute():
    """2026-03-29, Europe/Berlin: 02:00 → 03:00. A daily 02:30 slot runs ONCE, at 03:00 local."""
    trig = _berlin_daily("02:30")
    day_start = datetime(2026, 3, 28, 22, 0, tzinfo=UTC).timestamp()  # 23:00 local on the 28th
    due = model.due_slots(trig, day_start, day_start + 86400)
    assert due["count"] == 1
    slot, at = due["last"]
    assert slot == "2026-03-29T02:30"  # identity is the nominal local slot
    assert datetime.fromtimestamp(at, UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)  # 03:00 CEST


def test_dst_repeated_local_time_runs_once():
    """2026-10-25, Europe/Berlin: 03:00 → 02:00. A daily 02:30 slot occurs twice and runs ONCE."""
    trig = _berlin_daily("02:30")
    start = datetime(2026, 10, 24, 20, 0, tzinfo=UTC).timestamp()
    due = model.due_slots(trig, start, start + 86400)
    assert due["count"] == 1
    slot, at = due["last"]
    assert slot == "2026-10-25T02:30"
    # the FIRST occurrence: 02:30 CEST = 00:30Z (the second would be 01:30Z)
    assert datetime.fromtimestamp(at, UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    # …and the slot after it is the next day's, never the repeated hour's
    nxt = model.next_slot(trig, at)
    assert nxt[0] == "2026-10-26T02:30"


def test_cadence_vocabulary_is_closed():
    with pytest.raises(model.AutomationError, match="whole number from 5"):
        model.validate_trigger(
            {"kind": "schedule", "cadence": {"kind": "interval", "every": 4, "unit": "minutes"}}
        )
    with pytest.raises(model.AutomationError, match="no cron"):
        model.validate_trigger({"kind": "schedule", "cadence": {"kind": "*/5 * * * *"}})
    for reserved in ("loop", "webhook"):
        with pytest.raises(model.AutomationError, match="not available yet"):
            model.validate_trigger({"kind": reserved})
    with pytest.raises(model.AutomationError, match="IANA"):
        model.validate_trigger(
            {"kind": "schedule", "cadence": {"kind": "daily", "time": "03:00"}, "tz": "Mars/Base"}
        )
    with pytest.raises(model.AutomationError, match="not available yet"):
        model.validate_action(
            {
                "kind": "start_session",
                "engine": "claude",
                "model": "opus",
                "folder": "/tmp",
                "message": {"text": "x"},
            }
        )


def test_a_once_in_the_past_is_refused():
    with pytest.raises(model.AutomationError, match="already passed"):
        model.validate_trigger(
            {"kind": "once", "at": "2020-01-01T09:00", "tz": "UTC"}, now=time.time()
        )


# ---- store --------------------------------------------------------------------------------------


def test_store_is_0600_and_versioned():
    _create(_send_config())
    p = store.db_path()
    assert (os.stat(p).st_mode & 0o777) == 0o600
    con = sqlite3.connect(p)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    con.close()


def test_a_newer_store_is_refused_and_never_rewritten():
    p = store.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE future (x)")
    con.execute("PRAGMA user_version=99")
    con.commit()
    con.close()
    before = p.read_bytes()
    with pytest.raises(store.StoreUnsupported):
        store.list_all()
    assert p.read_bytes() == before


def test_an_existing_store_is_backed_up_before_its_first_migration(monkeypatch):
    aid = _create(_send_config())["id"]
    store.reset_schema_cache_for_test()
    applied: list[int] = []

    def step(con):
        applied.append(1)
        con.execute("ALTER TABLE automations ADD COLUMN future TEXT")

    monkeypatch.setattr(store, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(store._MIGRATIONS, 1, step)
    assert store.get(aid)["id"] == aid
    assert applied == [1]
    backups = list(store.db_path().parent.glob("automations.db.bak-v1-*"))
    assert len(backups) == 1
    con = sqlite3.connect(backups[0])
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    assert con.execute("SELECT id FROM automations").fetchone()[0] == aid
    con.close()


def test_claims_survive_run_retention_pruning():
    aid = _made(_hourly())
    res = store.begin_run(aid, trigger="schedule", slot="S1", fire_at=time.time())
    assert res["claimed"]
    store.finish_run(res["run"]["id"], "ok")
    assert store.prune_runs(now=time.time() + 400 * 86400) == 1
    assert _runs(aid) == []
    again = store.begin_run(aid, trigger="schedule", slot="S1", fire_at=time.time())
    assert again["claimed"] is False and again["run"] is None
    assert store.watermark(aid)["slot"] == "S1"


def test_one_slot_can_only_be_claimed_once():
    aid = _made(_hourly())
    first = store.begin_run(aid, trigger="schedule", slot="S", fire_at=1.0)
    second = store.begin_run(aid, trigger="schedule", slot="S", fire_at=1.0)
    assert first["claimed"] and not second["claimed"]
    assert len(_runs(aid)) == 1


# ---- consent ------------------------------------------------------------------------------------


def test_off_by_default_and_enable_needs_consent():
    pub = _create(_send_config())
    assert pub["enabled"] is False and pub["consented_at"] is None and pub["state"] == "off"
    with pytest.raises(model.AutomationError) as e:
        routes._enable(pub["id"], {"revision": pub["revision"]})
    assert e.value.status == 422 and e.value.extra["scope_lines"]
    assert store.get(pub["id"])["enabled"] is False
    on = _enable(pub["id"])
    assert on["enabled"] and on["consented_scope"] == on["scope"]


def test_consent_must_name_the_scope_that_was_shown():
    pub = _create(_send_config())
    with pytest.raises(model.AutomationError) as e:
        routes._enable(
            pub["id"], {"revision": pub["revision"], "consent": True, "scope_digest": "x"}
        )
    assert e.value.status == 409
    assert store.get(pub["id"])["enabled"] is False


@pytest.mark.parametrize(
    "change,expect",
    [
        ({"policy": {"max_runs_per_day": 96}}, "a higher daily cap"),
        (
            {
                "action": {
                    "kind": "send_to_session",
                    "session_key": SESSION,
                    "message": {"text": "something else"},
                }
            },
            "what it sends changed",
        ),
        (
            {
                "trigger": {
                    "kind": "schedule",
                    "cadence": {"kind": "interval", "every": 30, "unit": "minutes"},
                    "tz": "UTC",
                }
            },
            "runs more often",
        ),
        ({"policy": {"concurrency": "allow", "max_concurrent": 2}}, "more runs at the same time"),
    ],
)
def test_widening_without_consent_is_422_and_nothing_is_written(change, expect):
    aid = _made(_hourly())
    before = store.get(aid)
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {"revision": before["revision"], **change})
    assert e.value.status == 422
    assert expect in e.value.extra["widened"]
    after = store.get(aid)
    assert after == before  # not one column moved


def test_widening_with_consent_writes_a_fresh_receipt():
    aid = _made(_hourly())
    row = store.get(aid)
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {"revision": row["revision"], "policy": {"max_runs_per_day": 96}})
    out = routes._patch(
        aid,
        {
            "revision": row["revision"],
            "policy": {"max_runs_per_day": 96},
            "consent": True,
            "scope_digest": e.value.extra["scope_digest"],
        },
    )
    assert out["consented_scope"]["max_runs_per_day"] == 96
    assert out["consented_at"] >= row["consented_at"]


def test_narrowing_needs_no_consent_and_moves_the_receipt():
    aid = _made(_hourly(max_runs_per_day=10))
    row = store.get(aid)
    out = routes._patch(aid, {"revision": row["revision"], "policy": {"max_runs_per_day": 5}})
    assert out["consented_scope"]["max_runs_per_day"] == 5
    # …so widening BACK needs consent, even though the older receipt covered 10 (#1042).
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {"revision": out["revision"], "policy": {"max_runs_per_day": 10}})
    assert e.value.status == 422


def test_a_client_can_never_write_a_receipt_or_a_result():
    pub = _create(_send_config())
    for field in ("consented_scope", "consented_at", "pins", "outcome", "enabled"):
        with pytest.raises(model.AutomationError):
            routes._patch(pub["id"], {"revision": pub["revision"], field: {}})
    with pytest.raises(model.AutomationError):
        routes._create({**_send_config(), "consented_scope": {}})


# ---- scheduler ----------------------------------------------------------------------------------


def test_a_due_slot_fires_once(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    out = asyncio.run(sched.tick())
    assert [r["automation_id"] for r in out["fired"]] == [aid]
    assert out["fired"][0]["catch_up"] is False
    assert asyncio.run(sched.tick())["fired"] == []  # same slot, again: nothing
    sched.release()


def test_two_instances_fire_one_slot_once(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    clock = Clock(h + 3600 + 5)
    a = automation_loop.Scheduler(clock=clock, started_at=h)
    b = automation_loop.Scheduler(clock=clock, started_at=h)
    try:
        assert len(asyncio.run(a.tick())["fired"]) == 1
        # The peer cannot take the kernel lock while A lives…
        assert asyncio.run(b.tick()) == {"owner": False, "fired": []}
        # …and once A is gone, the durable claim still keeps the slot single-fire.
        a.release()
        took = asyncio.run(b.tick())
        assert took["owner"] is True and took["fired"] == []
    finally:
        a.release()
        b.release()
    assert len(_runs(aid)) == 1 and len(spawned) == 1


def test_two_owners_racing_one_slot_still_fire_once(spawned, monkeypatch):
    """Even with the ownership lock defeated, the claim is the single-fire guarantee."""
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    clock = Clock(h + 3600 + 5)
    monkeypatch.setattr(automation_loop.Scheduler, "try_own", lambda self: True)
    a = automation_loop.Scheduler(clock=clock, started_at=h)
    b = automation_loop.Scheduler(clock=clock, started_at=h)
    fired = len(asyncio.run(a.tick())["fired"]) + len(asyncio.run(b.tick())["fired"])
    assert fired == 1 and len(_runs(aid)) == 1


def test_restart_during_a_run_is_interrupted_and_never_replayed(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    clock = Clock(h + 3600 + 5)
    first = automation_loop.Scheduler(clock=clock, started_at=h)
    assert len(asyncio.run(first.tick())["fired"]) == 1
    first.release()  # the process dies with its run `dispatching`: no finish ever lands
    [run] = _runs(aid)
    assert run["state"] == "dispatching"
    _orphan(run["id"])

    second = automation_loop.Scheduler(clock=clock, started_at=h)
    out = asyncio.run(second.tick())
    second.release()
    assert out["fired"] == []  # the slot is claimed: never replayed
    [run] = _runs(aid)
    assert run["state"] == "done" and run["outcome"] == "interrupted"
    assert "outcome unknown" in run["reason"]
    assert run["session_key"] is None  # no link is invented for a run that never recorded one
    assert store.get(aid)["consecutive_failures"] == 0  # unknown is not a failure
    assert len(spawned) == 1


def test_a_disable_acknowledged_before_the_start_record_prevents_the_run(spawned):
    """The #1042 barrier: the loop read an ENABLED snapshot, then the operator's disable commits,
    then the claim transaction runs. The claim must re-read and refuse."""
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    snapshot_enabled = []

    def barrier(a):
        snapshot_enabled.append(store.get(a)["enabled"])
        routes._simple(a, "disable")

    sched._before_claim = barrier
    out = asyncio.run(sched.tick())
    sched.release()
    assert snapshot_enabled == [True]
    assert out["fired"] == [] and spawned == []
    assert _runs(aid) == []
    assert store.watermark(aid) is None  # not even claimed


def test_a_missed_backlog_collapses_into_one_catch_up_run(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h - 10 * 3600 + 60)
    now = h + 5
    # Inside the 10-minute startup grace the backlog waits…
    early = automation_loop.Scheduler(clock=Clock(now), started_at=now - 60)
    assert asyncio.run(early.tick())["fired"] == []
    early.release()
    # …and after it, ten missed slots are ONE run that says how many it covered.
    late = automation_loop.Scheduler(clock=Clock(now), started_at=now - 601)
    out = asyncio.run(late.tick())
    late.release()
    [run] = out["fired"]
    assert run["catch_up"] is True and run["covered"] == 10
    assert len(_runs(aid)) == 1
    assert store.watermark(aid)["fire_at"] == h  # the latest slot


def test_the_kill_switch_fires_nothing(spawned, monkeypatch):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "0")
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    assert asyncio.run(sched.tick())["fired"] == []
    asyncio.run(asyncio.wait_for(sched.run(), 2))  # returns at once rather than looping
    with pytest.raises(automation_runner.RunRefused) as e:
        asyncio.run(automation_runner.run_now(aid, registry=None))
    assert e.value.status == 409
    assert _runs(aid) == [] and spawned == []
    assert not sched.owner  # it never even took ownership


def test_the_daily_cap_skips_with_a_reason(spawned):
    aid = _made(_send_config(policy={"max_runs_per_day": 1}))
    first = asyncio.run(automation_runner.run_now(aid, registry=None))
    store.finish_run(first["id"], "ok")
    second = asyncio.run(automation_runner.run_now(aid, registry=None))
    assert second["outcome"] == "skipped" and second["reason"] == "skipped: daily cap reached"
    assert len(spawned) == 1


def test_concurrency_skip_records_the_skipped_slot(spawned):
    aid = _made(_send_config())
    asyncio.run(automation_runner.run_now(aid, registry=None))  # still dispatching
    second = asyncio.run(automation_runner.run_now(aid, registry=None))
    assert second["outcome"] == "skipped" and "previous run still active" in second["reason"]


def test_run_now_is_refused_for_a_never_approved_automation(spawned):
    aid = _create(_send_config())["id"]
    with pytest.raises(automation_runner.RunRefused) as e:
        asyncio.run(automation_runner.run_now(aid, registry=None))
    assert e.value.status == 409 and "never been approved" in e.value.detail
    assert _runs(aid) == []


def test_run_now_on_a_paused_automation_runs_once_without_resuming(spawned):
    aid = _made(_send_config())
    routes._simple(aid, "pause")
    run = asyncio.run(automation_runner.run_now(aid, registry=None))
    assert run["state"] == "dispatching"
    assert store.get(aid)["paused"] is True


# ---- runner: sends, failures, notifications -----------------------------------------------------


@pytest.fixture
def live_target(monkeypatch):
    """A live, in-scope session, with every byte written captured instead of reaching a pty."""
    writes: list[bytes] = []
    state = {"live": True, "outcome": "delivered"}
    monkeypatch.setattr(
        template_send, "resolve_target", lambda key: ("claude:phys", "/home/somebody/work")
    )
    monkeypatch.setattr(session_input, "is_live", lambda key: state["live"])

    def fake_send(key, payload, *, precondition=None, final_guard=None, **kw):
        for i, guard in enumerate((precondition, final_guard)):
            if i == 1 and state.get("between"):
                state.pop("between")()  # something lands between the two checks, once
            if guard is not None:
                ok, why = guard()
                if not ok:
                    return session_input.Outcome("stale", why)
        writes.append(payload)
        return session_input.Outcome(state["outcome"], "")

    monkeypatch.setattr(session_input, "send_input", fake_send)
    return {"writes": writes, "state": state}


def _manual_run(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def test_a_text_send_goes_through_the_single_writer_seam(live_target):
    aid = _made(_send_config())
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "ok"
    assert live_target["writes"] == [session_input.bracketed_paste("go")]
    assert res["run"]["session_key"] == SESSION
    # Typing into a session did not START it: no origin badge for the operator's own session.
    assert SESSION not in store.origins()


def test_a_send_to_a_session_that_is_not_running_is_refused_never_redirected(live_target):
    live_target["state"]["live"] = False
    aid = _made(_send_config())
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "refused" and "isn't running" in res["run"]["reason"]
    assert live_target["writes"] == []


def test_pause_after_n_failures_with_one_notification_retracted_on_recovery(live_target):
    live_target["state"]["live"] = False
    aid = _made(_send_config(policy={"pause_after_failures": 2}))
    for _ in range(3):
        asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    row = store.get(aid)
    assert row["paused"] is True and row["consecutive_failures"] == 3
    assert "paused after 2" in row["paused_reason"]
    rows = notifications.listing()["notifications"]
    assert len(rows) == 1 and rows[0]["action_id"] == row["failure_episode"]
    # No model text in the dedupe key: it is the automation id and the first failed run's id.
    assert row["failure_episode"].startswith(f"automation:{aid}:")

    live_target["state"]["live"] = True
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "ok"
    assert notifications.listing()["notifications"] == []
    assert store.get(aid)["failure_episode"] == ""


def test_a_disable_during_a_send_stops_it_at_the_write_boundary(live_target, monkeypatch):
    aid = _made(_send_config(trigger={"kind": "once", "at": "2099-01-01T00:00", "tz": "UTC"}))
    run = store.begin_run(aid, trigger="once", slot="2099-01-01T00:00", fire_at=1.0)["run"]
    routes._simple(aid, "disable")
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "stopped"  # the operator stopped it: not a failure
    assert live_target["writes"] == []


# ---- secrets ------------------------------------------------------------------------------------

SECRET = "hunter2-PLANTED-secret-value"


def _secret_template() -> dict:
    template_vars.create_variable({"name": "token", "kind": "secret", "value": SECRET})
    return tstore.create_template(
        {
            "name": "deploy",
            "body": "deploy with {{token}} to {{env}}",
            "fields": [
                {"name": "token", "source": "library", "kind": "secret"},
                {"name": "env", "default": "staging"},
            ],
        }
    )


def test_a_secret_template_is_refused_for_a_mission_action():
    t = _secret_template()
    with pytest.raises(model.AutomationError, match="never carries a secret"):
        _create(
            {
                "name": "m",
                "trigger": {"kind": "manual"},
                "action": {
                    "kind": "start_mission",
                    "project_id": "p1",
                    "instruction": {"template_id": t["id"], "values": {}},
                },
            }
        )


def test_a_typed_secret_can_never_be_stored_in_an_automation():
    t = tstore.create_template(
        {"name": "typed", "body": "use {{pw}}", "fields": [{"name": "pw", "kind": "secret"}]}
    )
    with pytest.raises(model.AutomationError, match="typed secret"):
        _create(
            _send_config(
                action={
                    "kind": "send_to_session",
                    "session_key": SESSION,
                    "message": {"template_id": t["id"], "values": {}},
                }
            )
        )


def test_a_secret_never_appears_in_any_run_record_or_response(live_target):
    t = _secret_template()
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {"env": "prod"}},
            }
        )
    )
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    # It WAS delivered — through template_send's three writes…
    assert any(SECRET.encode() in w for w in live_target["writes"])
    run = store.get_run(res["run"]["id"])
    assert run["inputs"]["message"] == "deploy with [secret: token] to prod"
    # …and it is nowhere in the store, on disk, or in any response.
    blob = b"".join(
        p.read_bytes() for p in store.db_path().parent.glob("automations.db*") if p.is_file()
    )
    assert SECRET.encode() not in blob
    for payload in (
        routes.public_run(run),
        routes.public(store.get(aid)),
        store.list_runs(aid),
        store.origins(),
    ):
        assert SECRET not in json.dumps(payload)


# ---- pinned inputs ------------------------------------------------------------------------------


def _text_template(body: str = "check {{thing}}") -> dict:
    return tstore.create_template(
        {"name": "check", "body": body, "fields": [{"name": "thing", "default": "ci"}]}
    )


def test_a_template_edit_marks_needs_reapproval_and_pauses(spawned):
    t = _text_template()
    h = _hour_start()
    aid = _made(
        _hourly()
        | {
            "action": {
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        },
        active_since=h + 60,
    )
    tstore.update_template(
        t["id"],
        {"name": "check", "body": "delete {{thing}}", "fields": t["fields"]},
        t["updated_at"],
    )
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    row = store.get(aid)
    assert row["needs_reapproval"] and row["paused"]
    assert "template was edited" in row["reapproval_reason"]
    assert routes.public(row)["state"] == "needs_reapproval"
    with pytest.raises(model.AutomationError) as e:
        routes._simple(aid, "resume")
    assert e.value.status == 409
    with pytest.raises(automation_runner.RunRefused):
        asyncio.run(automation_runner.run_now(aid, registry=None))
    # Enabling with consent re-approves the NEW revision.
    assert _enable(aid)["state"] == "enabled"


def test_a_library_variable_turning_secret_needs_reapproval_and_refuses_the_run(live_target):
    t = tstore.create_template(
        {"name": "t", "body": "hi {{team}}", "fields": [{"name": "team", "source": "library"}]}
    )
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        )
    )
    run = _manual_run(aid)
    template_vars.create_variable({"name": "team", "kind": "secret", "value": SECRET})
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "refused"
    assert store.get(aid)["needs_reapproval"] is True
    assert live_target["writes"] == []


# ---- start_session ------------------------------------------------------------------------------


@pytest.fixture
def session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(automation_runner, "engine_state", lambda engine: (True, ""))
    calls: list[dict] = []

    hooks: dict = {}

    async def fake_dispatch(
        *, engine, cwd, brief, registry, bypass=False, on_key=None, authorize=None, **kw
    ):
        key = f"{engine}:00000000-0000-0000-0000-00000000000a"
        out = headless_dispatch.Dispatch(key=key, engine=engine, native=key[7:], cwd=cwd)
        if on_key:
            on_key(key)
        if hooks.get("before_spawn"):
            hooks.pop("before_spawn")()
        if authorize is not None:  # honoured exactly where the real launcher calls it
            why = authorize("epoch")
            if why:
                out.reason = why
                return out
        calls.append({"engine": engine, "cwd": cwd, "brief": brief, "bypass": bypass})
        out.launched = out.started = out.briefed = True
        return out

    monkeypatch.setattr(headless_dispatch, "dispatch", fake_dispatch)
    return {"folder": str(work), "calls": calls, "hooks": hooks, "tmp": tmp_path}


def _session_config(folder: str, **action) -> dict:
    return {
        "name": "morning session",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_session",
            "engine": "claude",
            "folder": folder,
            "message": {"text": "triage the inbox"},
            **action,
        },
    }


def test_start_session_records_the_dispatch_facts(session_env):
    aid = _made(_session_config(session_env["folder"]))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    run = store.get_run(res["run"]["id"])
    assert run["outcome"] == "ok"
    assert [s["step"] for s in run["steps"]][1:4] == ["launched", "started", "briefed"]
    assert run["session_key"].startswith("claude:")
    assert session_env["calls"][0]["bypass"] is False
    assert store.origins()[run["session_key"]]["name"] == "morning session"


def test_a_folder_outside_the_boundary_at_run_time_is_refused(session_env):
    aid = _made(_session_config(session_env["folder"]))
    prefs.set_folder_exclusions([session_env["folder"]])
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "refused"
    assert "outside your project folders" in res["run"]["reason"]
    assert session_env["calls"] == []  # nothing launched, and nothing launched elsewhere


def test_an_engine_that_cannot_start_unattended_is_refused_at_save(session_env, monkeypatch):
    monkeypatch.setattr(automation_runner, "engine_state", lambda e: (False, "no unattended start"))
    with pytest.raises(model.AutomationError, match="no unattended start"):
        _create(_session_config(session_env["folder"]))


def test_bypass_turned_on_is_a_widening(session_env):
    aid = _made(_session_config(session_env["folder"]))
    row = store.get(aid)
    action = dict(row["config"]["action"], bypass=True)
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {"revision": row["revision"], "action": action})
    assert "permission bypass turned on" in e.value.extra["widened"]


# ---- start_mission ------------------------------------------------------------------------------


@pytest.fixture
def mission_env(monkeypatch, tmp_path):
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(mroutes, "_resolve_cwd", lambda pid, **kw: ("p1", str(tmp_path)))

    async def no_producers(mid):
        return None  # the real producers make model calls

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", no_producers)
    monkeypatch.setattr(
        missions,
        "get_plan",
        lambda mid, **kw: {"plan_id": "plan-1", "project_id": "p1", "engine": "claude"},
    )
    dispatched: list[tuple] = []

    async def fake_dispatch_approved(mid, body, *, registry, bypass_ceiling=None, **kw):
        from fastapi.responses import JSONResponse

        dispatched.append((mid, body, bypass_ceiling))
        return JSONResponse({"state": "running", "outcome": "started", "session_key": SESSION})

    monkeypatch.setattr(mroutes, "dispatch_approved", fake_dispatch_approved)
    return dispatched


def _mission_config(autonomy: str) -> dict:
    return {
        "name": "nightly audit",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_mission",
            "project_id": "p1",
            "instruction": {"text": "audit the dependencies"},
            "autonomy": autonomy,
        },
    }


def test_a_propose_mission_is_planned_and_never_dispatched(mission_env):
    aid = _made(_mission_config("propose"))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "ok" and "waiting for you" in res["run"]["reason"]
    assert mission_env == []
    mid = res["run"]["mission_id"]
    assert missions.get_mission(mid)["title"] == "nightly audit"
    assert store.origins()[f"mission:{mid}"]["automation_id"] == aid


def test_a_dispatch_mission_goes_through_the_dispatch_route_and_concludes_with_the_mission(
    mission_env, monkeypatch, spawned
):
    aid = _made(_mission_config("dispatch"))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    run = res["run"]
    assert run["outcome"] == "started" and run["result_class"] == "pending"
    [(mid, body, ceiling)] = mission_env
    assert body["plan_id"] == "plan-1" and body["expect_objectives"]
    assert ceiling is False  # automated missions never bypass
    # Brief delivery is not success: only the mission's own conclusion is.
    monkeypatch.setattr(missions, "get_mission", lambda m, **kw: {"state": "done"})
    sched = automation_loop.Scheduler()
    asyncio.run(sched.tick())
    sched.release()
    assert store.get_run(run["id"])["outcome"] == "ok"


def test_higher_mission_autonomy_is_a_widening(mission_env):
    aid = _made(_mission_config("propose"))
    row = store.get(aid)
    action = dict(row["config"]["action"], autonomy="dispatch")
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {"revision": row["revision"], "action": action})
    assert "higher mission autonomy" in e.value.extra["widened"]


# ---- routes -------------------------------------------------------------------------------------


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_routes_require_login_and_csrf_and_are_never_cached(auth_cfg, spawned):
    c = _client(auth_cfg)
    r = c.get("/api/automations")
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/automations", json=_send_config()).status_code in (401, 403)
    r = c.post("/api/automations", json=_send_config(), headers=hdr)
    assert r.status_code == 201, r.text
    pub = r.json()
    assert r.headers["cache-control"] == "no-store"
    r = c.post(
        f"/api/automations/{pub['id']}/enable", json={"revision": pub["revision"]}, headers=hdr
    )
    assert r.status_code == 422 and r.json()["scope_digest"]
    r = c.post(
        f"/api/automations/{pub['id']}/enable",
        json={
            "revision": pub["revision"],
            "consent": True,
            "scope_digest": r.json()["scope_digest"],
        },
        headers=hdr,
    )
    assert r.status_code == 200 and r.json()["state"] == "enabled"
    r = c.post(f"/api/automations/{pub['id']}/run", headers=hdr)
    assert r.status_code == 202 and r.json()["state"] == "dispatching"
    runs = c.get(f"/api/automations/{pub['id']}/runs").json()
    assert runs["total"] == 1
    detail = c.get(f"/api/automations/runs/{runs['runs'][0]['id']}").json()
    assert detail["steps"][0]["step"] == "claimed"
    assert c.get("/api/automations/origins").json() == {"origins": {}}
    assert c.get("/api/automations/nope").status_code == 404
    lst = c.get("/api/automations").json()
    assert lst["loop"]["enabled"] is True and len(lst["automations"]) == 1


def test_shutdown_drains_and_records_what_it_could_not_finish_as_interrupted(monkeypatch):
    aid = _made(_send_config())

    async def hangs(run, config, pins, *, registry):
        await asyncio.sleep(3600)

    monkeypatch.setitem(automation_runner._ACTIONS, "send_to_session", hangs)

    async def scenario():
        sched = automation_loop.Scheduler()
        sched._task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        run = await automation_runner.run_now(aid, registry=None)
        monkeypatch.setattr(automation_runner, "DRAIN_TIMEOUT_S", 0.2)
        await sched.shutdown()
        assert sched._task.cancelled() and not sched.owner
        return run

    run = asyncio.run(scenario())
    got = store.get_run(run["id"])
    assert got["outcome"] == "interrupted" and "outcome unknown" in got["reason"]


def test_a_once_trigger_fires_once_and_then_reads_finished(spawned):
    at = datetime.fromtimestamp(time.time() + 3600, UTC).strftime("%Y-%m-%dT%H:%M")
    aid = _made(_send_config(trigger={"kind": "once", "at": at, "tz": "UTC"}))
    fire = model.next_slot(store.get(aid)["config"]["trigger"], 0)[1]
    sched = automation_loop.Scheduler(clock=Clock(fire + 5), started_at=fire - 3600)
    assert len(asyncio.run(sched.tick())["fired"]) == 1
    sched.clock = Clock(fire + 86400)
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    assert routes.public(store.get(aid))["state"] == "finished"


# ---- review round 1: the mission folder is pinned -----------------------------------------------


@pytest.fixture
def real_project(tmp_path, monkeypatch):
    from agent_sessions import projects

    monkeypatch.setenv("HOME", str(tmp_path))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    proj = projects.create("repo", folders=[str(a), str(b)], default_folder=str(a))
    return {"id": proj.id, "a": str(a), "b": str(b)}


def _real_mission_config(pid: str, autonomy: str = "propose") -> dict:
    cfg = _mission_config(autonomy)
    cfg["action"]["project_id"] = pid
    return cfg


def test_a_repointed_project_is_refused_and_needs_reapproval(real_project):
    from agent_sessions import projects

    aid = _made(_real_mission_config(real_project["id"]))
    pub = routes.public(store.get(aid))
    assert pub["consented_scope"]["target"]["cwd"] == os.path.realpath(real_project["a"])
    assert any(real_project["a"] in line for line in pub["scope_lines"])
    assert "Permission bypass: off (automated missions never bypass)" in pub["scope_lines"]
    assert "The agent is chosen by the mission planner" in pub["scope_lines"]

    projects.update(real_project["id"], default_folder=real_project["b"])
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "refused"
    assert "folder changed" in res["run"]["reason"]
    row = store.get(aid)
    assert row["needs_reapproval"] and row["paused"]
    assert missions.safe_list_missions()["missions"] == []  # nothing was created at b


def test_a_project_repointed_while_planning_is_refused_before_dispatch(mission_env, monkeypatch):
    from agent_sessions.routes import missions as mroutes

    aid = _made(_mission_config("dispatch"))

    async def repoint(mid):
        monkeypatch.setattr(mroutes, "_resolve_cwd", lambda pid, **kw: ("p1", "/elsewhere"))

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", repoint)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "refused" and "folder changed" in res["run"]["reason"]
    assert mission_env == []  # never reached the dispatch route


def test_dispatch_is_given_the_pinned_folder(mission_env, tmp_path):
    aid = _made(_mission_config("dispatch"))
    asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    [(_mid, body, _ceiling)] = mission_env
    assert body["expect_cwd"] == str(tmp_path)


def test_the_mission_scope_boundary_is_checked_at_run_time(mission_env, tmp_path):
    aid = _made(_mission_config("dispatch"))
    prefs.set_folder_exclusions([str(tmp_path)])
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert (
        res["run"]["outcome"] == "refused"
        and "outside your project folders" in res["run"]["reason"]
    )
    assert missions.safe_list_missions()["missions"] == []


# ---- review round 1: automated missions never bypass (tripwire through the REAL route) ----------


@pytest.fixture
def real_dispatch(real_project, monkeypatch):
    """The real `dispatch_approved` → `mission_dispatch.run`; only the launcher is faked."""
    from agent_sessions import mission_dispatch, scopedspawn
    from agent_sessions.routes import missions as mroutes

    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)

    async def producers(mid):  # what the planner and objective producer would have written
        m = missions.get_mission(mid)  # the mission's own project, as the planner would read it
        missions.put_plan(
            mid, project_id=m["project_id"], cwd=m["cwd"], engine="claude", brief="go"
        )
        missions.patch_objectives(mid, [{"op": "add", "key": "pr", "title": "a PR", "gate": True}])
        missions.settle_objectives_state(mid, "done")

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", producers)
    seen: list[bool] = []

    hooks: dict = {}
    cwds: list[str] = []

    async def launcher(**kw):
        out = headless_dispatch.Dispatch(
            key="claude:" + "0" * 36, engine="claude", native="0" * 36, cwd=kw["cwd"]
        )
        if hooks.get("in_fence"):
            hooks.pop("in_fence")()  # lands after every check before the fence
        # The real launcher calls `authorize` inside the launch fence, immediately before the
        # spawn; this fake does the same, and "spawns" only if it says yes.
        why = kw["authorize"](session_input.policy_fingerprint())
        if why:
            out.reason = why
            return out
        seen.append(kw["bypass"])
        cwds.append(kw["cwd"])
        out.reason = "stopped by the test"
        return out

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    return {"seen": seen, "md": mission_dispatch, "hooks": hooks, "cwds": cwds}


@pytest.mark.parametrize("path_decides_bypass", [False, True])
def test_an_automated_mission_never_launches_with_bypass(
    real_dispatch, real_project, monkeypatch, path_decides_bypass
):
    """#1220 makes the dispatch path honour a bypass grant; simulated here by making the path's own
    decision True. The automation's ceiling must still force False at the launcher."""
    monkeypatch.setattr(real_dispatch["md"], "_decided_bypass", lambda: path_decides_bypass)
    aid = _made(_real_mission_config(real_project["id"], "dispatch"))
    asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert real_dispatch["seen"] == [False]


def test_the_bypass_seam_is_live_for_the_manual_route(real_dispatch, real_project, monkeypatch):
    """Control for the tripwire: without a ceiling, the path's decision DOES reach the launcher —
    so the test above is not passing because nothing is wired."""
    from agent_sessions.routes import missions as mroutes

    monkeypatch.setattr(real_dispatch["md"], "_decided_bypass", lambda: True)
    row = missions.create_mission("x", project_id=real_project["id"], cwd=real_project["a"])
    asyncio.run(mroutes._produce_for_new_mission(row["id"]))
    plan = missions.get_plan(row["id"])
    body = {
        "plan_id": plan["plan_id"],
        "expect_cwd": real_project["a"],
        "expect_objectives": missions.objectives_digest(
            missions.get_mission(row["id"])["objectives"]
        ),
    }
    resp = asyncio.run(mroutes.dispatch_approved(row["id"], body, registry=object()))
    assert resp.status_code == 200, resp.body
    assert real_dispatch["seen"] == [True]


# ---- review round 1: widening covers every field ------------------------------------------------


def _scope(cfg: dict, pins: dict | None = None) -> dict:
    return model.scope_of(model.validate_config(cfg), pins or {})


@pytest.mark.parametrize(
    "before,after",
    [
        ({"kind": "daily", "time": "03:00"}, {"kind": "daily", "time": "14:00"}),
        (
            {"kind": "weekly", "days": ["sat"], "time": "03:00"},
            {"kind": "weekly", "days": ["mon"], "time": "03:00"},
        ),
        (
            {"kind": "monthly", "day": 1, "time": "03:00"},
            {"kind": "monthly", "day": 15, "time": "03:00"},
        ),
    ],
)
def test_any_schedule_change_is_a_widening(before, after):
    old = _scope(_send_config(trigger={"kind": "schedule", "cadence": before, "tz": "UTC"}))
    new = _scope(_send_config(trigger={"kind": "schedule", "cadence": after, "tz": "UTC"}))
    assert "the schedule changed" in model.widened(old, new)


def test_a_timezone_change_is_a_widening():
    cad = {"kind": "daily", "time": "03:00"}
    old = _scope(_send_config(trigger={"kind": "schedule", "cadence": cad, "tz": "UTC"}))
    new = _scope(
        _send_config(trigger={"kind": "schedule", "cadence": cad, "tz": "Pacific/Kiritimati"})
    )
    assert "the schedule changed" in model.widened(old, new)


def test_moving_a_once_earlier_is_a_widening_through_the_route(spawned):
    far = datetime.fromtimestamp(time.time() + 30 * 86400, UTC).strftime("%Y-%m-%dT%H:%M")
    soon = datetime.fromtimestamp(time.time() + 600, UTC).strftime("%Y-%m-%dT%H:%M")
    aid = _made(_send_config(trigger={"kind": "once", "at": far, "tz": "UTC"}))
    row = store.get(aid)
    with pytest.raises(model.AutomationError) as e:
        routes._patch(
            aid, {"revision": row["revision"], "trigger": {"kind": "once", "at": soon, "tz": "UTC"}}
        )
    assert e.value.status == 422 and "the schedule changed" in e.value.extra["widened"]
    assert store.get(aid) == row


def _base_scope(**policy) -> dict:
    return _scope(_hourly(**policy))


def test_widening_pause_after_failures():
    assert "tolerates more failures before pausing" in model.widened(
        _base_scope(pause_after_failures=3), _base_scope(pause_after_failures=5)
    )


def test_widening_expires_at_later_or_removed():
    old = _base_scope(expires_at=2_000_000_000)
    assert "runs until later" in model.widened(old, _base_scope(expires_at=2_100_000_000))
    assert "runs until later" in model.widened(old, _base_scope())
    assert model.widened(old, _base_scope(expires_at=1_900_000_000)) == []


def test_widening_target():
    other = dict(
        _send_config()["action"], session_key="claude:" + "9" * 8 + "-2222-3333-4444-555555555555"
    )
    assert "a new target" in model.widened(
        _scope(_send_config()), _scope(_send_config(action=other))
    )


def test_widening_engine():
    def cfg(engine):
        return _session_config("/tmp/work", engine=engine)

    assert "a different agent or model" in model.widened(
        _scope(cfg("claude")), _scope(cfg("codex"))
    )


def test_widening_checklist():
    cfg = _mission_config("propose")
    cfg["action"]["checklist_id"] = "release"
    old = _scope(cfg, {"checklist": {"id": "release", "digest": "a"}})
    new = _scope(cfg, {"checklist": {"id": "release", "digest": "b"}})
    assert "the checklist changed" in model.widened(old, new)


def test_widening_trigger():
    assert "a different trigger" in model.widened(_scope(_send_config()), _scope(_hourly()))


def test_widening_template_pin():
    old = _scope(_send_config(), {"template": {"digest": "a", "library": {}}})
    new = _scope(_send_config(), {"template": {"digest": "b", "library": {}}})
    assert "the template changed" in model.widened(old, new)


# ---- review round 1: surviving mutants ----------------------------------------------------------


def test_a_secret_template_is_refused_for_a_new_session_at_save(session_env):
    t = _secret_template()
    with pytest.raises(model.AutomationError, match="first message"):
        _create(
            _session_config(session_env["folder"], message={"template_id": t["id"], "values": {}})
        )


def test_the_render_rechecks_secret_kinds_at_run_time():
    t = tstore.create_template(
        {"name": "t", "body": "hi {{team}}", "fields": [{"name": "team", "source": "library"}]}
    )
    cfg = model.validate_config(
        _session_config("/tmp/w", message={"template_id": t["id"], "values": {}})
    )
    pins = model.compute_pins(cfg)  # approved while `team` did not exist
    template_vars.create_variable({"name": "team", "kind": "secret", "value": SECRET})
    with pytest.raises(automation_runner.RunRefused, match="secret"):
        automation_runner._render_plain(cfg, pins)


def test_the_engine_is_rechecked_at_run_time(session_env, monkeypatch):
    aid = _made(_session_config(session_env["folder"]))
    monkeypatch.setattr(automation_runner, "engine_state", lambda e: (False, "agent removed"))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "refused" and res["run"]["reason"] == "agent removed"
    assert session_env["calls"] == []


def test_the_send_guard_rechecks_the_boundary_at_the_write(live_target):
    aid = _made(_send_config())
    # The entry check is faked in-scope (live_target); the target's folder leaves scope after it.
    prefs.set_folder_exclusions(["/home/somebody/work"])
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert (
        res["run"]["outcome"] == "refused"
        and "outside your project folders" in res["run"]["reason"]
    )
    assert live_target["writes"] == []


def test_a_config_that_disagrees_with_its_receipt_never_runs(spawned):
    aid = _made(_send_config())
    con = sqlite3.connect(store.db_path())
    row = con.execute("SELECT consented_scope FROM automations WHERE id=?", (aid,)).fetchone()
    tampered = dict(json.loads(row[0]), max_runs_per_day=1)
    con.execute("UPDATE automations SET consented_scope=? WHERE id=?", (json.dumps(tampered), aid))
    con.commit()
    con.close()
    with pytest.raises(automation_runner.RunRefused, match="doesn't match"):
        asyncio.run(automation_runner.run_now(aid, registry=None))
    assert spawned == []


def test_the_ownership_lock_is_not_inherited():
    import fcntl

    sched = automation_loop.Scheduler()
    assert sched.try_own()
    try:
        assert fcntl.fcntl(sched._fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        sched.release()


# ---- review round 1: nothing stays `dispatching` ------------------------------------------------


def test_an_outcome_the_store_refuses_once_still_lands(live_target, monkeypatch):
    aid = _made(_send_config())
    real = store.finish_run
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(store, "finish_run", flaky)
    monkeypatch.setattr(automation_runner, "FINISH_BACKOFF_S", (0.01,))
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert store.get_run(run["id"])["outcome"] == "ok" and len(calls) == 2


def test_an_outcome_refused_every_retry_is_settled_by_the_next_tick(live_target, monkeypatch):
    aid = _made(_send_config())
    real = store.finish_run
    monkeypatch.setattr(
        store,
        "finish_run",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    monkeypatch.setattr(automation_runner, "FINISH_BACKOFF_S", ())
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert store.get_run(run["id"])["state"] == "dispatching"
    monkeypatch.setattr(store, "finish_run", real)
    sched = automation_loop.Scheduler()
    asyncio.run(sched.tick())
    sched.release()
    assert store.get_run(run["id"])["outcome"] == "ok"


def test_a_dead_peers_run_now_is_recovered_as_interrupted(spawned):
    aid = _made(_send_config())
    run = asyncio.run(automation_runner.run_now(aid, registry=None))
    _orphan(run["id"])
    sched = automation_loop.Scheduler()
    asyncio.run(sched.tick())
    sched.release()
    assert store.get_run(run["id"])["outcome"] == "interrupted"


def test_a_live_peers_in_flight_run_is_never_touched(spawned):
    import subprocess
    import sys

    peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        token = f"{peer.pid}:{store._proc_start(peer.pid)}:peer"
        aid = _made(_send_config())
        run = asyncio.run(automation_runner.run_now(aid, registry=None))
        _orphan(run["id"], token)
        sched = automation_loop.Scheduler()
        asyncio.run(sched.tick())
        sched.release()
        assert store.get_run(run["id"])["state"] == "dispatching"
    finally:
        peer.kill()
        peer.wait()


class _Crash(BaseException):
    """The process dying: nothing after it runs, not even `except Exception`."""


def test_a_crash_after_the_mission_is_created_and_before_its_link(mission_env, monkeypatch):
    aid = _made(_mission_config("propose"))
    real_link = store.link

    def dies(run_id, *, mission_id=None, **kw):
        if mission_id:
            raise _Crash
        return real_link(run_id, mission_id=mission_id, **kw)

    monkeypatch.setattr(store, "link", dies)
    run = _manual_run(aid)
    with pytest.raises(_Crash):
        asyncio.run(automation_runner.execute(run, registry=None))
    assert len(missions.safe_list_missions()["missions"]) == 1  # the mission exists
    _orphan(run["id"])
    sched = automation_loop.Scheduler()
    asyncio.run(sched.tick())
    sched.release()
    got = store.get_run(run["id"])
    assert got["outcome"] == "interrupted" and "A mission may have been created" in got["reason"]
    assert got["mission_id"] is None  # never invented


# ---- review round 1: retention, disabled Run now, a late once -----------------------------------


def test_retention_can_never_make_room_under_the_daily_cap(spawned):
    aid = _made(_send_config(policy={"max_runs_per_day": 250}))
    for i in range(250):
        r = store.begin_run(aid, trigger="manual", slot=f"manual:{i}", fire_at=None)["run"]
        store.finish_run(r["id"], "ok")
    assert store.prune_runs(keep=0, max_age_s=0) == 0
    run = asyncio.run(automation_runner.run_now(aid, registry=None))
    assert run["outcome"] == "skipped" and "daily cap" in run["reason"]


def test_run_now_on_a_disabled_automation_is_refused(spawned):
    aid = _made(_send_config())
    routes._simple(aid, "disable")
    with pytest.raises(automation_runner.RunRefused, match="turned off") as e:
        asyncio.run(automation_runner.run_now(aid, registry=None))
    assert e.value.status == 409 and spawned == []


def test_a_once_missed_by_more_than_a_day_is_skipped_not_fired(spawned):
    at = datetime.fromtimestamp(time.time() + 3600, UTC).strftime("%Y-%m-%dT%H:%M")
    aid = _made(_send_config(trigger={"kind": "once", "at": at, "tz": "UTC"}))
    fire = model.next_slot(store.get(aid)["config"]["trigger"], 0)[1]
    sched = automation_loop.Scheduler(clock=Clock(fire + 25 * 3600), started_at=fire - 86400)
    [run] = asyncio.run(sched.tick())["fired"]
    sched.release()
    assert run["outcome"] == "skipped" and run["reason"] == "skipped: missed by more than 24 h"
    assert spawned == []


def test_the_due_check_flags_a_repointed_project_without_firing(real_project, spawned):
    from agent_sessions import projects

    h = _hour_start()
    cfg = _real_mission_config(real_project["id"])
    cfg["trigger"] = _hourly()["trigger"]
    aid = _made(cfg, active_since=h + 60)
    projects.update(real_project["id"], default_folder=real_project["b"])
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    row = store.get(aid)
    assert row["needs_reapproval"] and "folder changed" in row["reapproval_reason"]
    assert spawned == []


def test_retention_keeps_at_least_the_daily_cap_maximum():
    aid = _made(_send_config())
    for i in range(model.MAX_RUNS_PER_DAY_MAX + 12):
        r = store.begin_run(aid, trigger="manual", slot=f"manual:{i}", fire_at=None, now=1000.0 + i)
        store.finish_run(r["run"]["id"], "ok")
    later = time.time() + 2 * 86400  # all of them outside the protected window
    assert store.prune_runs(now=later, keep=0, max_age_s=1e12) == 12
    assert store.list_runs(aid)["total"] == model.MAX_RUNS_PER_DAY_MAX


# ---- review round 2 (Hermes) ------------------------------------------------------------------


def _api(auth_cfg):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    return c, {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}


def test_a_verb_route_cannot_be_overridden_from_the_query_string(auth_cfg, spawned):
    c, hdr = _api(auth_cfg)
    aid = _made(_send_config())
    base = f"/api/automations/{aid}"
    assert c.post(base + "/pause?_verb_name=resume&verb=resume", headers=hdr).json()["paused"]
    r = c.post(base + "/resume?_verb_name=pause&verb=pause", headers=hdr).json()
    assert r["paused"] is False and r["enabled"] is True
    r = c.post(base + "/disable?_verb_name=resume&verb=resume", headers=hdr).json()
    assert r["enabled"] is False
    other = _made(_send_config())
    routes._simple(other, "pause")
    with pytest.raises(ValueError) as e:
        routes._simple(other, "bogus")  # an unknown verb is a bug, never a default
    assert type(e.value) is ValueError  # not a 4xx refusal that happens to subclass it
    assert store.get(other)["paused"] is True


def test_no_automation_route_exposes_a_parameter_as_request_input(auth_cfg):
    """The audit behind the verb fix: every input these routes take is read and validated by the
    handler itself; FastAPI must bind nothing but the path id and the auth dependencies."""
    from fastapi.dependencies.utils import get_flat_dependant

    from agent_sessions.main import create_app

    app = create_app(auth_cfg)
    seen = 0
    for route in app.routes:
        if not getattr(route, "path", "").startswith(routes.PREFIX):
            continue
        seen += 1
        flat = get_flat_dependant(route.dependant)
        exposed = [
            p.name
            for p in (
                *flat.query_params,
                *flat.body_params,
                *flat.header_params,
                *flat.cookie_params,
            )
        ]
        assert exposed == [], (route.path, exposed)
        assert {p.name for p in flat.path_params} <= {"aid", "run_id"}, route.path
    assert seen == 13  # list, create, origins, run, get, patch, delete, enable, 3 verbs, run, runs


def test_a_repointed_session_folder_is_refused_and_needs_reapproval(session_env):
    tmp = session_env["tmp"]
    a, b = tmp / "a", tmp / "b"
    a.mkdir()
    b.mkdir()
    link = tmp / "link"
    link.symlink_to(a)
    aid = _made(_session_config(str(link)))
    scope = store.get(aid)["consented_scope"]
    assert scope["target"]["cwd"] == os.path.realpath(a)
    link.unlink()
    link.symlink_to(b)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "refused" and "folder changed" in res["run"]["reason"]
    row = store.get(aid)
    assert row["needs_reapproval"] and row["paused"]
    assert session_env["calls"] == []


def test_a_session_launches_in_the_pinned_real_folder_whatever_the_link_says(session_env):
    tmp = session_env["tmp"]
    a, b = tmp / "a", tmp / "b"
    a.mkdir()
    b.mkdir()
    link = tmp / "link"
    link.symlink_to(a)
    aid = _made(_session_config(str(link)))

    def repoint():
        link.unlink()
        link.symlink_to(b)

    session_env["hooks"]["before_spawn"] = repoint  # between the run's check and the spawn
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "ok"
    assert session_env["calls"][0]["cwd"] == os.path.realpath(a)


def test_a_folder_swapped_for_a_symlink_before_the_spawn_is_refused(session_env):
    tmp = session_env["tmp"]
    a, b = tmp / "a", tmp / "b"
    a.mkdir()
    b.mkdir()
    aid = _made(_session_config(str(a)))

    def swap():
        a.rename(tmp / "a-moved")
        a.symlink_to(b)

    session_env["hooks"]["before_spawn"] = swap
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "refused" and "changed" in res["run"]["reason"]
    assert session_env["calls"] == []


def test_a_disable_between_the_guards_stops_a_text_send(live_target):
    aid = _made(_send_config())
    live_target["state"]["between"] = lambda: routes._simple(aid, "disable")
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped"  # the operator stopped it: not a failure
    assert live_target["writes"] == []


def test_a_disable_between_the_guards_stops_a_template_send(live_target):
    t = _text_template()
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        )
    )
    live_target["state"]["between"] = lambda: routes._simple(aid, "disable")
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped"  # the operator stopped it: not a failure
    assert live_target["writes"] == []


def _consented_patch(aid: str, change: dict) -> dict:
    row = store.get(aid)
    try:
        return routes._patch(aid, {"revision": row["revision"], **change})
    except model.AutomationError as e:
        return routes._patch(
            aid,
            {
                "revision": row["revision"],
                **change,
                "consent": True,
                "scope_digest": e.extra["scope_digest"],
            },
        )


def test_a_schedule_edit_after_the_due_check_does_not_fire_the_old_slot(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    far = datetime.fromtimestamp(h + 12 * 3600, UTC).strftime("%H:%M")
    daily = {"kind": "schedule", "cadence": {"kind": "daily", "time": far}, "tz": "UTC"}
    sched._before_claim = lambda a: _consented_patch(a, {"trigger": daily})
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    assert _runs(aid) == [] and spawned == []
    assert store.watermark(aid) is None


def test_a_content_edit_after_the_due_check_does_not_fire_under_the_old_row(spawned):
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    newer = {"kind": "send_to_session", "session_key": SESSION, "message": {"text": "newer"}}
    sched._before_claim = lambda a: _consented_patch(a, {"action": newer})
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    assert _runs(aid) == []
    # the next tick decides on the current row
    sched2 = automation_loop.Scheduler(clock=Clock(h + 3600 + 10), started_at=h)
    [run] = asyncio.run(sched2.tick())["fired"]
    sched2.release()
    assert run["scope"]["config"]["action"]["message"]["text"] == "newer"


def test_a_slot_that_is_not_due_under_the_current_schedule_is_never_claimed():
    h = _hour_start()
    aid = _made(_hourly(), active_since=h + 60)
    rev = store.get(aid)["revision"]
    res = store.begin_run(
        aid,
        trigger="schedule",
        slot="1999-01-01T00:00Z",
        fire_at=h,
        now=h + 3605,
        expect_revision=rev,
    )
    assert res["run"] is None and res.get("stale")
    assert _runs(aid) == []


def test_inputs_are_recorded_before_any_byte(live_target, monkeypatch):
    aid = _made(_send_config())

    def fails(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "set_inputs", fails)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "failed"
    assert live_target["writes"] == []  # nothing typed that the record could not describe


def test_a_bookkeeping_failure_after_a_session_launch_is_still_ok(session_env, monkeypatch):
    aid = _made(_session_config(session_env["folder"]))
    real = store.add_step

    def fails_after_launch(run_id, step, detail=""):
        if step == "launched":
            raise sqlite3.OperationalError("database is locked")
        return real(run_id, step, detail)

    monkeypatch.setattr(store, "add_step", fails_after_launch)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    run = store.get_run(res["run"]["id"])
    assert run["outcome"] == "ok" and "recording details failed" in run["reason"]
    assert store.get(aid)["consecutive_failures"] == 0


def test_a_bookkeeping_failure_after_a_mission_dispatch_is_still_started(mission_env, monkeypatch):
    aid = _made(_mission_config("dispatch"))
    real = store.link

    def fails(run_id, *, session_key=None, **kw):
        if session_key:
            raise sqlite3.OperationalError("database is locked")
        return real(run_id, session_key=session_key, **kw)

    monkeypatch.setattr(store, "link", fails)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    run = store.get_run(res["run"]["id"])
    assert run["outcome"] == "started" and "recording details failed" in run["reason"]
    assert store.get(aid)["consecutive_failures"] == 0


def test_a_failed_finish_after_delivery_never_becomes_a_failure(live_target, monkeypatch):
    aid = _made(_send_config())
    real = store.finish_run
    monkeypatch.setattr(
        store, "finish_run", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x"))
    )
    monkeypatch.setattr(automation_runner, "FINISH_BACKOFF_S", ())
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert automation_runner._UNSETTLED[run["id"]][0] == "ok"
    monkeypatch.setattr(store, "finish_run", real)
    asyncio.run(automation_runner.retry_unsettled())
    assert store.get_run(run["id"])["outcome"] == "ok"
    assert store.get(aid)["consecutive_failures"] == 0


@pytest.mark.parametrize("raw", ["1e999", str(10**400), "-1e999", "253402300800"])
def test_a_non_finite_or_absurd_expiry_is_refused_at_the_route(auth_cfg, raw):
    c, hdr = _api(auth_cfg)
    body = json.dumps(_send_config()).replace(
        '"policy": {}', '"policy": {"expires_at": ' + raw + "}"
    )
    r = c.post(
        "/api/automations", content=body, headers={**hdr, "Content-Type": "application/json"}
    )
    assert r.status_code == 422, r.text
    pub = c.post("/api/automations", json=_send_config(), headers=hdr).json()
    patch = f'{{"revision": {pub["revision"]}, "policy": {{"expires_at": {raw}}}}}'
    r = c.patch(
        f"/api/automations/{pub['id']}",
        content=patch,
        headers={**hdr, "Content-Type": "application/json"},
    )
    assert r.status_code == 422, r.text


def test_a_stored_bad_expiry_reads_as_unreadable_not_a_500(auth_cfg):
    c, hdr = _api(auth_cfg)
    aid = _create(_send_config())["id"]
    con = sqlite3.connect(store.db_path())
    cfg = json.loads(con.execute("SELECT config FROM automations WHERE id=?", (aid,)).fetchone()[0])
    cfg["policy"]["expires_at"] = float("inf")
    con.execute("UPDATE automations SET config=? WHERE id=?", (json.dumps(cfg), aid))
    con.commit()
    con.close()
    r = c.get("/api/automations")
    assert r.status_code == 200 and r.json()["automations"][0]["state"] == "unreadable"
    assert c.get(f"/api/automations/{aid}").status_code == 200


def test_an_old_process_never_writes_a_store_a_newer_peer_migrated(auth_cfg):
    aid = _create(_send_config())["id"]  # this process has cached "schema done"
    con = sqlite3.connect(store.db_path())
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.commit()
    before = con.execute("SELECT name, revision FROM automations").fetchall()
    con.close()
    with pytest.raises(store.StoreUnsupported):
        store.mutate(aid, lambda r: {"name": "renamed"})
    with pytest.raises(store.StoreUnsupported):
        store.get(aid)  # a clean refusal, not a crash on columns it does not know
    con = sqlite3.connect(store.db_path())
    assert con.execute("SELECT name, revision FROM automations").fetchall() == before
    con.close()
    c, _hdr = _api(auth_cfg)
    assert c.get("/api/automations").status_code == 503


def test_the_session_launch_itself_refuses_a_stale_folder_pin(session_env):
    """The launch's own check, independent of the generic pin comparison in `_execute`."""
    tmp = session_env["tmp"]
    a, b = tmp / "a", tmp / "b"
    a.mkdir()
    b.mkdir()
    link = tmp / "link"
    link.symlink_to(a)
    aid = _made(_session_config(str(link)))
    run = _manual_run(aid)
    link.unlink()
    link.symlink_to(b)
    cfg, pins = run["scope"]["config"], run["scope"]["pins"]
    out = asyncio.run(automation_runner._start_session(run, cfg, pins, registry=object()))
    assert out[0] == "refused" and "folder changed" in out[1]
    assert session_env["calls"] == []
    assert store.get(aid)["needs_reapproval"] is True


# ---- PR B commit 1: a run settles once, and a non-owner still settles its own parked outcome ----


def test_a_double_settle_counts_one_failure_and_opens_one_episode(spawned):
    aid = _made(_send_config())
    run = _manual_run(aid)
    first = store.finish_run(run["id"], "failed", "nothing reached the session")
    assert first["settled"] is True and first["episode"][0] == "open"
    again = store.finish_run(run["id"], "failed", "nothing reached the session")
    assert again["settled"] is False and again["episode"] is None
    row = store.get(aid)
    assert row["consecutive_failures"] == 1
    steps = [s["step"] for s in store.get_run(run["id"])["steps"]]
    assert steps.count("failed") == 1


def test_a_late_finish_never_overwrites_an_interrupted_run(spawned):
    aid = _made(_send_config())
    run = _manual_run(aid)
    _orphan(run["id"])
    assert store.recover_interrupted() == [run["id"]]
    late = store.finish_run(run["id"], "ok", "delivered")
    assert late["settled"] is False
    got = store.get_run(run["id"])
    assert got["outcome"] == "interrupted" and got["reason"].startswith("interrupted")


def test_a_non_owner_tick_retries_its_own_parked_outcome(live_target, monkeypatch):
    aid = _made(_send_config())
    real = store.finish_run
    monkeypatch.setattr(
        store,
        "finish_run",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    monkeypatch.setattr(automation_runner, "FINISH_BACKOFF_S", ())
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert run["id"] in automation_runner._UNSETTLED
    monkeypatch.setattr(store, "finish_run", real)
    owner = automation_loop.Scheduler()
    assert owner.try_own()
    try:
        peer = automation_loop.Scheduler()
        out = asyncio.run(peer.tick())
        assert out["owner"] is False
    finally:
        owner.release()
    assert store.get_run(run["id"])["outcome"] == "ok"
    assert run["id"] not in automation_runner._UNSETTLED


def test_disabling_during_a_manual_send_is_refused_at_the_write(live_target, monkeypatch):
    aid = _made(_send_config())
    writes = live_target["writes"]
    real_live = session_input.is_live

    def live_then_disabled(key):
        # The operator turns the automation off after the run passed its liveness check and
        # before the single writer's guards run: the send must not type anything.
        ok = real_live(key)
        routes._simple(aid, "disable")
        return ok

    monkeypatch.setattr(session_input, "is_live", live_then_disabled)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped"  # the operator stopped it: not a failure
    assert "turned it off" in res["run"]["reason"]
    assert writes == []


def test_the_finish_retry_backs_off_on_its_schedule(live_target, monkeypatch):
    aid = _made(_send_config())
    real = store.finish_run
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) <= 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def record(delay, *a, **k):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(store, "finish_run", flaky)
    monkeypatch.setattr(automation_runner.asyncio, "sleep", record)
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert slept == list(automation_runner.FINISH_BACKOFF_S[:3])
    assert len(calls) == 4 and store.get_run(run["id"])["outcome"] == "ok"
    assert run["id"] not in automation_runner._UNSETTLED


# ---- review round 3: the effect lock (authority and effect in one critical section) ----------


def test_a_disable_during_planning_never_dispatches(mission_env, monkeypatch):
    """The independent reviewer's repro: disable inside `_produce_for_new_mission`."""
    from agent_sessions.routes import missions as mroutes

    aid = _made(_mission_config("dispatch"))
    acks = []

    async def disable_while_planning(mid):
        acks.append(routes._simple(aid, "disable"))

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", disable_while_planning)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped"
    assert mission_env == []  # dispatch_approved never called
    # The disable could not take the lock (the run was mid-effect), so it said so.
    assert acks[0]["in_flight"] is True and acks[0]["in_flight_detail"]


def test_narrowing_autonomy_during_planning_never_dispatches(mission_env, monkeypatch):
    from agent_sessions.routes import missions as mroutes

    aid = _made(_mission_config("dispatch"))

    async def narrow(mid):
        row = store.get(aid)
        action = dict(row["config"]["action"], autonomy="propose")
        routes._patch(aid, {"revision": row["revision"], "action": action})

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", narrow)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped" and mission_env == []


def test_a_disable_inside_the_mission_dispatch_fence_stops_the_spawn(real_dispatch, real_project):
    aid = _made(_real_mission_config(real_project["id"], "dispatch"))
    real_dispatch["hooks"]["in_fence"] = lambda: store.mutate(aid, lambda r: {"enabled": 0})
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert real_dispatch["seen"] == []  # nothing spawned
    assert res["run"]["outcome"] in ("stopped", "refused")


def test_an_automated_mission_launches_in_the_pinned_real_folder(real_dispatch, tmp_path):
    from agent_sessions import projects

    a = tmp_path / "ra"
    a.mkdir()
    link = tmp_path / "rlink"
    link.symlink_to(a)
    proj = projects.create("linked", folders=[str(link)], default_folder=str(link))
    aid = _made(_real_mission_config(proj.id, "dispatch"))
    asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert real_dispatch["cwds"] == [os.path.realpath(a)]  # never the symlink string


def test_a_symlink_swap_inside_the_dispatch_fence_is_refused(real_dispatch, tmp_path):
    from agent_sessions import projects

    a, b = tmp_path / "sa", tmp_path / "sb"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "slink"
    link.symlink_to(a)
    proj = projects.create("swapped", folders=[str(link)], default_folder=str(link))
    aid = _made(_real_mission_config(proj.id, "dispatch"))

    def swap():
        link.unlink()
        link.symlink_to(b)

    real_dispatch["hooks"]["in_fence"] = swap
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert real_dispatch["seen"] == []
    assert res["run"]["outcome"] == "refused"


def test_a_disable_racing_the_send_never_acknowledges_a_later_write(live_target, monkeypatch):
    """Across the post-final-guard window: either no bytes, or the disable says `in_flight`.
    Never an acknowledged disable (no in_flight) AND bytes written."""
    aid = _made(_send_config())
    acks = []
    real_send = session_input.send_input

    def send_then_disable(key, payload, **kw):
        out = real_send(key, payload, **kw)  # the fake: guards pass, bytes are appended
        acks.append(routes._simple(aid, "disable"))  # lands between the guard and the write
        return out

    monkeypatch.setattr(session_input, "send_input", send_then_disable)
    asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert live_target["writes"], "the send was past its final guard"
    assert acks[0]["in_flight"] is True


def test_a_disable_with_no_run_in_flight_is_acknowledged_plainly():
    aid = _made(_send_config())
    out = routes._simple(aid, "disable")
    assert out["in_flight"] is False and out["in_flight_detail"] == ""


def test_a_disable_inside_the_session_launch_fence_stops_the_spawn(session_env):
    """The independent reviewer's gap: `_still_authorized` inside `start_session`'s authorize."""
    aid = _made(_session_config(session_env["folder"]))
    session_env["hooks"]["before_spawn"] = lambda: store.mutate(aid, lambda r: {"enabled": 0})
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert session_env["calls"] == []
    assert res["run"]["outcome"] == "stopped"


def test_the_effect_lock_is_not_inherited():
    import fcntl

    fd = effect_lock.acquire("abc123", 0)
    try:
        assert fd is not None and fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        assert effect_lock.acquire("abc123", 0) is None  # a second holder, same process, waits
    finally:
        effect_lock._release(fd)


# ---- review round 3: settle once, stale flags, partial sends, scalar validation --------------


def test_a_committed_finish_whose_ack_was_lost_is_not_counted_twice(live_target, monkeypatch):
    """Hermes' exact case: the commit lands, the acknowledgement is lost, the runner retries."""
    live_target["state"]["live"] = False
    aid = _made(_send_config())
    real = store.finish_run
    calls = []

    def commit_then_lose_ack(*a, **k):
        calls.append(1)
        res = real(*a, **k)
        if len(calls) == 1:
            raise sqlite3.OperationalError("the acknowledgement was lost")
        return res

    monkeypatch.setattr(store, "finish_run", commit_then_lose_ack)
    monkeypatch.setattr(automation_runner, "FINISH_BACKOFF_S", (0.0,))
    run = _manual_run(aid)
    asyncio.run(automation_runner.execute(run, registry=None))
    assert len(calls) == 2
    assert store.get(aid)["consecutive_failures"] == 1
    steps = [s["step"] for s in store.get_run(run["id"])["steps"]]
    assert steps.count("refused") == 1
    assert len(notifications.listing()["notifications"]) == 1  # announced once, not zero


def test_a_stale_drift_observation_never_revokes_newer_consent(spawned, monkeypatch):
    t = _text_template()
    h = _hour_start()
    aid = _made(
        _hourly()
        | {
            "action": {
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        },
        active_since=h + 60,
    )
    tstore.update_template(
        t["id"], {"name": "check", "body": "new {{thing}}", "fields": t["fields"]}, t["updated_at"]
    )
    real_flag = store.flag_reapproval

    def operator_approves_first(a, why, **kw):
        _enable(a)  # the operator consents to the edited template before the flag lands
        return real_flag(a, why, **kw)

    monkeypatch.setattr(store, "flag_reapproval", operator_approves_first)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    asyncio.run(sched.tick())
    sched.release()
    row = store.get(aid)
    assert row["enabled"] and not row["needs_reapproval"] and not row["paused"]


def test_a_flag_whose_drift_is_gone_is_a_no_op():
    t = _text_template()
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        )
    )
    row = store.get(aid)  # same revision, and the inputs still match the consent
    store.flag_reapproval(aid, "stale", observed_revision=row["revision"])
    assert store.get(aid)["needs_reapproval"] is False


def test_an_aborted_text_send_is_partial_pauses_at_once_and_notifies_once(live_target):
    live_target["state"]["outcome"] = "aborted"
    aid = _made(_send_config(policy={"pause_after_failures": 5}))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    run = res["run"]
    assert run["outcome"] == "partial" and run["result_class"] == "failed"
    row = store.get(aid)
    assert row["paused"] and row["paused_reason"] == store.PARTIAL_REASON
    assert row["consecutive_failures"] == 0
    assert len(notifications.listing()["notifications"]) == 1


def test_a_template_send_typed_but_not_submitted_is_partial(live_target, monkeypatch):
    t = _text_template()
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        )
    )
    real_send = session_input.send_input
    calls = []

    def enter_fails(key, payload, **kw):
        calls.append(payload)
        if payload == b"\r":
            return session_input.Outcome("failed", "")
        return real_send(key, payload, **kw)

    monkeypatch.setattr(session_input, "send_input", enter_fails)
    monkeypatch.setattr(template_send, "CLEAR_DELAY_S", 0)
    monkeypatch.setattr(template_send, "ENTER_DELAY_S", 0)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "partial"
    assert store.get(aid)["paused"] is True and store.get(aid)["consecutive_failures"] == 0


@pytest.mark.parametrize(
    "path,bad",
    [
        (("trigger", "kind"), []),
        (("trigger", "kind"), {}),
        (("trigger", "kind"), 7),
        (("action", "kind"), []),
        (("action", "kind"), {"a": 1}),
    ],
)
def test_a_non_string_kind_is_a_422_never_a_500(auth_cfg, path, bad):
    c, hdr = _api(auth_cfg)
    cfg = json.loads(json.dumps(_send_config()))
    cfg[path[0]][path[1]] = bad
    assert c.post("/api/automations", json=cfg, headers=hdr).status_code == 422


@pytest.mark.parametrize(
    "cadence,policy,action",
    [
        ({"kind": []}, {}, None),
        ({"kind": "interval", "every": 5, "unit": 5}, {}, None),
        ({"kind": "weekly", "days": [[1]], "time": "03:00"}, {}, None),
        ({"kind": "daily", "time": "03:00"}, {"concurrency": {}}, None),
        ({"kind": "daily", "time": "03:00"}, {}, {"autonomy": []}),
    ],
)
def test_nested_non_string_words_are_a_422(auth_cfg, cadence, policy, action):
    c, hdr = _api(auth_cfg)
    cfg = _send_config(trigger={"kind": "schedule", "cadence": cadence, "tz": "UTC"}, policy=policy)
    if action:
        cfg["action"] = {**_mission_config("propose")["action"], **action}
    assert c.post("/api/automations", json=cfg, headers=hdr).status_code == 422


# ---- addendum: N1–N7 ---------------------------------------------------------------------------


class _At:
    def __init__(self, t: float) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


def test_a_consented_schedule_edit_counts_from_now_not_from_the_old_watermark(spawned, monkeypatch):
    day = datetime(2026, 9, 30, tzinfo=UTC).timestamp()
    at = _At(day + 2 * 3600)
    monkeypatch.setattr(routes, "time", at)
    daily = lambda hhmm: {  # noqa: E731
        "kind": "schedule",
        "cadence": {"kind": "daily", "time": hhmm},
        "tz": "UTC",
    }
    aid = _made(_send_config(trigger=daily("03:00")))
    sched = automation_loop.Scheduler(clock=Clock(day + 3 * 3600 + 5), started_at=day)
    [first] = asyncio.run(sched.tick())["fired"]
    assert first["slot"] == "2026-09-30T03:00"
    store.finish_run(first["id"], "ok")
    at.t = day + 12 * 3600  # 12:00: the operator moves it to 09:00 and consents
    _consented_patch(aid, {"trigger": daily("09:00")})
    sched.clock = Clock(day + 12 * 3600 + 30)
    assert asyncio.run(sched.tick())["fired"] == []  # 09:00 today was never consented
    sched.release()


def _set_books(*books, default: str = "") -> None:
    cur = prefs.get_mission_playbooks()
    prefs.set_mission_playbooks(
        {"default_id": default, "playbooks": list(books)}, expect_revision=cur["revision"]
    )


def _book(pid: str, title: str = "t") -> dict:
    return {
        "id": pid,
        "label": pid.title(),
        "objectives": [{"key": "k", "title": title, "probe": "none", "gate": False}],
    }


def test_the_default_checklist_is_pinned_shown_and_used(mission_env):
    _set_books(_book("release"), default="release")
    aid = _made(_mission_config("propose"))
    pub = routes.public(store.get(aid))
    assert pub["pins"]["checklist"]["id"] == "release"
    assert "Checklist: Release (your default)" in pub["scope_lines"]
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert missions.get_mission(res["run"]["mission_id"])["playbook_id"] == "release"


@pytest.mark.parametrize("change", ["default", "contents"])
def test_a_changed_default_checklist_needs_reapproval(mission_env, spawned, change):
    _set_books(_book("release"), _book("other"), default="release")
    h = _hour_start()
    cfg = _mission_config("propose") | {"trigger": _hourly()["trigger"]}
    aid = _made(cfg, active_since=h + 60)
    if change == "default":
        _set_books(_book("release"), _book("other"), default="other")
    else:
        _set_books(_book("release", "edited"), _book("other"), default="release")
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    assert asyncio.run(sched.tick())["fired"] == []
    sched.release()
    assert store.get(aid)["needs_reapproval"] is True


def test_a_checklist_edited_while_the_mission_is_created_is_not_dispatched(
    mission_env, monkeypatch
):
    from agent_sessions.routes import missions as mroutes

    _set_books(_book("release"), default="release")
    aid = _made(_mission_config("dispatch"))

    async def edit_during_create(mid):
        _set_books(_book("release", "edited"), default="release")

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", edit_during_create)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "refused" and "checklist changed" in res["run"]["reason"]
    assert mission_env == []
    assert res["run"]["mission_id"]  # the mission exists, undispatched


def test_pausing_during_a_run_is_stopped_not_a_failure(live_target):
    aid = _made(_send_config(policy={"pause_after_failures": 1}))
    live_target["state"]["between"] = lambda: store.mutate(aid, lambda r: {"paused": 1})
    run = _hourly_run(aid)
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "stopped" and res["run"]["result_class"] == "skipped"
    assert "paused it" in res["run"]["reason"]
    assert store.get(aid)["consecutive_failures"] == 0
    assert notifications.listing()["notifications"] == []


def _hourly_run(aid: str) -> dict:
    """A scheduled (non-manual) run record for `aid`, claimed directly."""
    res = store.begin_run(aid, trigger="schedule", slot=f"S{time.time_ns()}", fire_at=1.0)
    assert res["claimed"], res
    return res["run"]


def test_a_send_target_outside_the_boundary_is_refused_at_save(monkeypatch):
    from types import SimpleNamespace

    from agent_sessions import engines

    monkeypatch.setattr(
        engines, "resolve_session", lambda e, n, **k: SimpleNamespace(cwd="/outside/x")
    )
    prefs.set_folder_exclusions(["/outside"])
    with pytest.raises(model.AutomationError, match="outside your project folders"):
        _create(_send_config())
    monkeypatch.setattr(engines, "resolve_session", lambda e, n, **k: None)
    assert _create(_send_config())["id"]  # not resolvable now: checked at run time


def test_malformed_request_inputs_are_422_or_413_never_500(auth_cfg):
    c, hdr = _api(auth_cfg)
    pub = c.post("/api/automations", json=_send_config(), headers=hdr).json()
    base = f"/api/automations/{pub['id']}"
    assert c.get(base + "/runs?limit=²").status_code == 422
    assert c.get(base + "/runs?offset=²").status_code == 422
    assert c.delete(base + "?revision=²", headers=hdr).status_code == 422
    deep = "[" * 100000 + "]" * 100000
    r = c.post(
        "/api/automations",
        content=deep[:60000],
        headers={**hdr, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    big = json.dumps({**_send_config(), "name": "x" * 70000})
    r = c.post("/api/automations", content=big, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_a_transient_resolution_failure_skips_the_tick_without_pausing(
    mission_env, spawned, monkeypatch
):
    from agent_sessions.routes import missions as mroutes

    h = _hour_start()
    aid = _made(_mission_config("propose") | {"trigger": _hourly()["trigger"]}, active_since=h + 60)

    def unreadable(pid, **kw):
        raise missions.MissionError("could not resolve the project", status=503)

    monkeypatch.setattr(mroutes, "_resolve_cwd", unreadable)
    sched = automation_loop.Scheduler(clock=Clock(h + 3600 + 5), started_at=h)
    assert asyncio.run(sched.tick())["fired"] == []
    row = store.get(aid)
    assert not row["needs_reapproval"] and not row["paused"]
    assert "not checked" in row["check_note"]

    def gone(pid, **kw):
        raise missions.MissionError("unknown project", status=404)

    monkeypatch.setattr(mroutes, "_resolve_cwd", gone)
    asyncio.run(sched.tick())
    sched.release()
    assert store.get(aid)["needs_reapproval"] is True  # an affirmative drift


def test_a_folder_replaced_at_the_same_path_is_a_drift(session_env):
    work = session_env["tmp"] / "work"
    aid = _made(_session_config(str(work)))
    work.rename(work.with_name("work-old"))  # kept, so its inode cannot be reused
    work.mkdir()  # same name, a different directory
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert res["run"]["outcome"] == "refused" and session_env["calls"] == []
    assert store.get(aid)["needs_reapproval"] is True


def test_the_dispatch_route_alone_refuses_a_swapped_pinned_folder(real_dispatch, tmp_path):
    """`dispatch_approved(pinned_real_cwd=…)` on its own — no automation authorize callback."""
    from agent_sessions import projects
    from agent_sessions.routes import missions as mroutes

    a, b = tmp_path / "da", tmp_path / "db"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "dlink"
    link.symlink_to(a)
    proj = projects.create("direct", folders=[str(link)], default_folder=str(link))
    row = missions.create_mission("x", project_id=proj.id, cwd=str(link))
    asyncio.run(mroutes._produce_for_new_mission(row["id"]))
    plan = missions.get_plan(row["id"])
    body = {
        "plan_id": plan["plan_id"],
        "expect_cwd": str(link),
        "expect_objectives": missions.objectives_digest(
            missions.get_mission(row["id"])["objectives"]
        ),
    }

    def swap():
        link.unlink()
        link.symlink_to(b)

    real_dispatch["hooks"]["in_fence"] = swap
    asyncio.run(
        mroutes.dispatch_approved(
            row["id"], body, registry=object(), pinned_real_cwd=os.path.realpath(a)
        )
    )
    assert real_dispatch["seen"] == []


def test_a_flag_observed_before_an_unrelated_edit_waits_for_the_next_check():
    """The revision half of the guard: the row moved on since the observation, so this flag is
    dropped; the next due check re-observes (and, if the drift is still real, flags it then)."""
    t = _text_template()
    aid = _made(
        _send_config(
            action={
                "kind": "send_to_session",
                "session_key": SESSION,
                "message": {"template_id": t["id"], "values": {}},
            }
        )
    )
    observed = store.get(aid)["revision"]
    tstore.update_template(
        t["id"], {"name": "check", "body": "new {{thing}}", "fields": t["fields"]}, t["updated_at"]
    )
    routes._simple(aid, "pause")  # an unrelated edit after the observation
    store.flag_reapproval(aid, "drift", observed_revision=observed)
    assert store.get(aid)["needs_reapproval"] is False
    store.flag_reapproval(aid, "drift", observed_revision=store.get(aid)["revision"])
    assert store.get(aid)["needs_reapproval"] is True


# ---- review round 4 ----------------------------------------------------------------------------


def test_auto_choose_is_never_granted_after_a_disable_that_won_the_lock(mission_env, monkeypatch):
    """The reviewer's repro: a real disable runs right after the runner releases the lock. The
    grant, if any, must come BEFORE that acknowledgement — never after it."""
    aid = _made(_mission_config("dispatch_auto_choose"))
    events: list[str] = []
    monkeypatch.setattr(
        missions, "set_auto_choose", lambda mid, on, **kw: events.append(f"grant:{on}") or {}
    )
    real_runner = effect_lock.runner

    @contextlib.asynccontextmanager
    async def runner_then_disable(a, timeout=None):
        async with real_runner(a, timeout) as held:
            yield held
        ack = routes._simple(aid, "disable")  # the lock is free now: a clean ack
        events.append(f"ack:in_flight={ack['in_flight']}")

    monkeypatch.setattr(effect_lock, "runner", runner_then_disable)
    asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    ack = events.index("ack:in_flight=False")
    assert "grant:True" not in events[ack:]


def test_auto_choose_is_skipped_when_authority_is_gone_before_it(mission_env, monkeypatch):
    aid = _made(_mission_config("dispatch_auto_choose"))
    granted = []
    monkeypatch.setattr(missions, "set_auto_choose", lambda mid, on, **kw: granted.append(on) or {})
    from agent_sessions.routes import missions as mroutes

    fake = mroutes.dispatch_approved

    async def dispatch_then_disable(*a, **kw):
        out = await fake(*a, **kw)
        store.mutate(aid, lambda r: {"enabled": 0})
        return out

    monkeypatch.setattr(mroutes, "dispatch_approved", dispatch_then_disable)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert granted == []
    steps = store.get_run(res["run"]["id"])["steps"]
    assert any(s["step"] == "auto_choose_skipped" and "not granted" in s["detail"] for s in steps)


def test_a_busy_effect_lock_is_skipped_not_a_failure(live_target):
    aid = _made(_send_config(policy={"concurrency": "allow", "max_concurrent": 2}))
    fd = effect_lock.acquire(aid, 0)
    try:
        import agent_sessions.automation_effect_lock as el

        old = el.RUNNER_WAIT_S
        el.RUNNER_WAIT_S = 0.1
        try:
            res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
        finally:
            el.RUNNER_WAIT_S = old
    finally:
        effect_lock._release(fd)
    assert res["run"]["outcome"] == "skipped"
    assert res["run"]["reason"] == "skipped: previous run still in its effect"
    assert store.get(aid)["consecutive_failures"] == 0
    assert notifications.listing()["notifications"] == []
    assert live_target["writes"] == []


def test_a_disable_during_a_session_launch_reports_in_flight_and_nothing_spawns(session_env):
    aid = _made(_session_config(session_env["folder"]))
    acks = []
    session_env["hooks"]["before_spawn"] = lambda: acks.append(routes._simple(aid, "disable"))
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert acks[0]["in_flight"] is True  # the launch held the lock
    assert session_env["calls"] == [] and res["run"]["outcome"] == "stopped"


def test_a_disable_that_wins_before_a_session_launch_is_plain_and_nothing_spawns(session_env):
    aid = _made(_session_config(session_env["folder"]))
    run = _manual_run(aid)
    ack = routes._simple(aid, "disable")
    assert ack["in_flight"] is False
    res = asyncio.run(automation_runner.execute(run, registry=object()))
    assert session_env["calls"] == [] and res["run"]["outcome"] == "stopped"


@pytest.mark.parametrize("writer", ["patch", "enable", "delete"])
def test_every_authority_writer_takes_the_effect_lock(writer):
    aid = _made(_hourly(max_runs_per_day=10))
    fd = effect_lock.acquire(aid, 0)  # a run mid-effect
    try:
        row = store.get(aid)
        if writer == "patch":  # narrowing: no consent needed
            out = routes._patch(
                aid, {"revision": row["revision"], "policy": {"max_runs_per_day": 5}}
            )
        elif writer == "enable":
            pub = routes.public(row)
            out = routes._enable(
                aid,
                {"revision": row["revision"], "consent": True, "scope_digest": pub["scope_digest"]},
            )
        else:
            out = routes._delete(aid, row["revision"])
    finally:
        effect_lock._release(fd)
    assert out["in_flight"] is True


def test_a_disable_before_the_run_starts_creates_no_mission(mission_env):
    aid = _made(_mission_config("dispatch"))
    run = _manual_run(aid)
    routes._simple(aid, "disable")
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "stopped"
    assert missions.safe_list_missions()["missions"] == [] and mission_env == []


def test_a_directory_replaced_inside_the_session_fence_is_refused(session_env):
    work = session_env["tmp"] / "work"
    aid = _made(_session_config(str(work)))

    def replace():
        work.rename(work.with_name("work-gone"))
        work.mkdir()

    session_env["hooks"]["before_spawn"] = replace
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert session_env["calls"] == [] and res["run"]["outcome"] == "refused"
    assert "changed while the launch" in res["run"]["reason"]


def test_a_directory_replaced_inside_the_mission_fence_is_refused(real_dispatch, real_project):
    from pathlib import Path

    aid = _made(_real_mission_config(real_project["id"], "dispatch"))
    a = Path(real_project["a"])

    def replace():
        a.rename(a.with_name("a-gone"))
        a.mkdir()

    real_dispatch["hooks"]["in_fence"] = replace
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=object()))
    assert real_dispatch["seen"] == [] and res["run"]["outcome"] == "refused"


def test_a_pause_during_a_run_now_stops_it_by_the_revision_bump(live_target):
    aid = _made(_send_config())
    live_target["state"]["between"] = lambda: routes._simple(aid, "pause")
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "stopped" and live_target["writes"] == []


def test_a_chunked_body_over_the_cap_is_413_without_content_length(auth_cfg):
    c, hdr = _api(auth_cfg)
    blob = json.dumps({**_send_config(), "name": "x" * 70000}).encode()

    def chunks():
        for i in range(0, len(blob), 8192):
            yield blob[i : i + 8192]

    r = c.post(
        "/api/automations", content=chunks(), headers={**hdr, "Content-Type": "application/json"}
    )
    assert r.status_code == 413


def test_a_transient_failure_at_run_time_is_skipped_noted_and_not_flagged(mission_env, monkeypatch):
    from agent_sessions.routes import missions as mroutes

    aid = _made(_mission_config("propose"))

    def unreadable(pid, **kw):
        raise missions.MissionError("could not resolve the project", status=503)

    monkeypatch.setattr(mroutes, "_resolve_cwd", unreadable)
    res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    assert res["run"]["outcome"] == "skipped"
    row = store.get(aid)
    assert not row["needs_reapproval"] and not row["paused"] and row["consecutive_failures"] == 0
    assert "not checked" in row["check_note"]


@pytest.mark.parametrize("bad", ["abc-def", "²", "A" * 3, "x" * 65])
def test_a_malformed_id_is_404_and_creates_no_lock_file(auth_cfg, bad):
    from agent_sessions import sessionlock

    c, hdr = _api(auth_cfg)
    for verb in ("pause", "resume", "disable", "enable", "run"):
        assert c.post(f"/api/automations/{bad}/{verb}", json={}, headers=hdr).status_code in (
            404,
            422,
        )
    assert c.get(f"/api/automations/{bad}").status_code == 404
    d = sessionlock.lock_dir()
    assert not list(d.glob("automation-*.lock")) if d.exists() else True


def test_an_unknown_well_formed_id_never_creates_a_lock_file(auth_cfg):
    from agent_sessions import sessionlock

    c, hdr = _api(auth_cfg)
    assert c.post("/api/automations/0123456789abcdef/pause", headers=hdr).status_code == 404
    d = sessionlock.lock_dir()
    assert not (d / "automation-0123456789abcdef.lock").exists()


def test_a_run_that_waited_past_expiry_does_nothing(live_target):
    aid = _made(_send_config(policy={"expires_at": time.time() + 3600}))
    run = _manual_run(aid)
    con = sqlite3.connect(store.db_path())
    cfg = json.loads(con.execute("SELECT config FROM automations WHERE id=?", (aid,)).fetchone()[0])
    cfg["policy"]["expires_at"] = time.time() - 1  # it expired while the run waited
    con.execute("UPDATE automations SET config=? WHERE id=?", (json.dumps(cfg), aid))
    con.commit()
    con.close()
    res = asyncio.run(automation_runner.execute(run, registry=None))
    assert res["run"]["outcome"] == "skipped" and "expired while waiting" in res["run"]["reason"]
    assert live_target["writes"] == []


def test_a_genuine_failure_during_an_auto_pause_stays_a_failure(live_target, monkeypatch):
    aid = _made(_hourly())

    def fails_and_autopauses(key, payload, **kw):
        # Another run's failure streak auto-pauses it (no operator write, no revision bump)…
        con = sqlite3.connect(store.db_path())
        con.execute("UPDATE automations SET paused=1 WHERE id=?", (aid,))
        con.commit()
        con.close()
        return session_input.Outcome("failed", "")  # …and this run's write genuinely failed

    monkeypatch.setattr(session_input, "send_input", fails_and_autopauses)
    res = asyncio.run(automation_runner.execute(_hourly_run(aid), registry=None))
    assert res["run"]["outcome"] == "failed"


def test_a_stale_consent_409_says_what_widened(auth_cfg):
    c, hdr = _api(auth_cfg)
    aid = _made(_hourly(max_runs_per_day=10))
    row = store.get(aid)
    r = c.patch(
        f"/api/automations/{aid}",
        json={
            "revision": row["revision"],
            "policy": {"max_runs_per_day": 20},
            "consent": True,
            "scope_digest": "stale",
        },
        headers=hdr,
    )
    assert r.status_code == 409
    assert "a higher daily cap" in r.json()["widened"]


@pytest.mark.parametrize("bad", ["²", "abc-def", "../x", "", "A"])
def test_the_lock_path_refuses_anything_but_an_ascii_id(bad):
    with pytest.raises(ValueError):
        effect_lock.path_for(bad)
    assert effect_lock.path_for("0123456789abcdef").name == "automation-0123456789abcdef.lock"


# ---- review round 5 (Hermes on 0c33423) --------------------------------------------------------


def test_cancelling_a_run_mid_acquire_never_strands_the_effect_lock(monkeypatch):
    """Cancel at exactly the boundary: the worker has WON the flock, the coroutine is cancelled
    before it sees the fd. The lock must be released and the fd closed."""
    import threading

    aid = "0123456789abcdef"
    won, go = threading.Event(), threading.Event()
    fds: list[int] = []
    real = effect_lock._try

    def barrier(p):
        fd = real(p)
        fds.append(fd)
        won.set()
        go.wait(5)
        return fd

    monkeypatch.setattr(effect_lock, "_try", barrier)

    async def scenario():
        async def hold():
            async with effect_lock.runner(aid, timeout=5):
                await asyncio.sleep(10)

        task = asyncio.ensure_future(hold())
        await asyncio.to_thread(won.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)  # the cancellation is pending while the worker holds the flock
        go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    monkeypatch.setattr(effect_lock, "_try", real)
    assert fds and fds[0] is not None
    with pytest.raises(OSError):
        os.fstat(fds[0])  # closed, not leaked
    t0 = time.monotonic()
    fd = effect_lock.acquire(aid, 1.0)
    assert fd is not None and time.monotonic() - t0 < 0.5  # promptly acquirable again
    effect_lock._release(fd)


_CHILD = r"""
import os, sys, time
tag, out, go, wait_for = sys.argv[1:5]
from agent_sessions import session_input, template_send
from agent_sessions import templates as tstore

def record(key, payload, **kw):
    with open(out, "ab") as f:
        f.write(f"{tag}:{payload!r}\n".encode())
    time.sleep(0.3)
    return session_input.Outcome("delivered", "")

session_input.send_input = record
template_send.resolve_target = lambda s: ("claude:phys-shared", "/x")
template_send.CLEAR_DELAY_S = 0
template_send.ENTER_DELAY_S = 0
t = tstore.create_template({"name": "t", "body": "hello " + tag, "fields": []})
while not os.path.exists(go):
    time.sleep(0.01)
if wait_for:
    while wait_for + ":" not in open(out).read():
        time.sleep(0.01)
try:
    template_send.send(
        t["id"],
        {"session": "claude:x", "values": {}, "expected_updated_at": t["updated_at"]},
    )
    print("ok")
except template_send.SendRefused as e:
    print(f"refused {e.status} busy={e.busy}")
"""


def test_two_processes_sending_into_one_session_never_interleave(tmp_path):
    import subprocess
    import sys

    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    out, go = tmp_path / "writes.log", tmp_path / "go"
    out.write_bytes(b"")
    locks = tmp_path / "locks"

    def child(tag: str, wait_for: str):
        home = tmp_path / f"home-{tag}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_SESSIONS_")} | {
            "HOME": str(home),
            "AGENT_SESSIONS_LOCK_DIR": str(locks),  # SHARED, as across app instances
            "AGENT_SESSIONS_TEMPLATES": str(home / "templates.json"),
            "AGENT_SESSIONS_TEMPLATE_VARS": str(home / "vars.json"),
            "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY": str(home / "secrets.key"),
            "AGENT_SESSIONS_PREFS": str(home / "prefs.json"),
        }
        return subprocess.Popen(
            [sys.executable, str(script), tag, str(out), str(go), wait_for],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )

    a, b = child("A", ""), child("B", "A")  # B starts the moment A's first write lands
    go.write_text("go")
    results = [p.communicate(timeout=60)[0].strip() for p in (a, b)]
    tags = [line.split(":", 1)[0] for line in out.read_text().splitlines()]
    assert results[0] == "ok"
    # B either waited for A's whole sequence or was refused as busy — never interleaved.
    assert tags in (["A"] * 3 + ["B"] * 3, ["A"] * 3), (tags, results)
    if tags == ["A"] * 3:
        assert results[1] == "refused 409 busy=True"


def test_the_send_lock_is_released_however_the_send_ends():
    phys = "claude:phys-release"
    with pytest.raises(RuntimeError):
        with template_send.session_send_lock(phys):
            raise RuntimeError("the delivery blew up")
    with template_send.session_send_lock(phys, timeout=0):  # immediately free again
        pass


def _hold_send_lock(phys: str) -> int:
    import fcntl

    p = template_send.send_lock_path(phys)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_manual_template_send_is_busy_while_another_process_holds_the_session(monkeypatch):
    monkeypatch.setattr(template_send, "resolve_target", lambda s: ("claude:phys-m", "/x"))
    monkeypatch.setattr(template_send, "SEND_LOCK_WAIT_S", 0.1)
    written = []
    monkeypatch.setattr(
        session_input,
        "send_input",
        lambda *a, **k: written.append(a) or session_input.Outcome("delivered", ""),
    )
    t = tstore.create_template({"name": "t", "body": "hi", "fields": []})
    fd = _hold_send_lock("claude:phys-m")
    try:
        with pytest.raises(template_send.SendRefused) as e:
            template_send.send(
                t["id"],
                {"session": "claude:x", "values": {}, "expected_updated_at": t["updated_at"]},
            )
    finally:
        os.close(fd)
    assert e.value.status == 409 and e.value.busy and written == []


def test_an_automation_send_is_skipped_while_the_session_is_being_sent_to(live_target, monkeypatch):
    monkeypatch.setattr(template_send, "SEND_LOCK_WAIT_S", 0.1)
    aid = _made(_send_config())
    fd = _hold_send_lock("claude:phys")  # live_target resolves every session to this key
    try:
        res = asyncio.run(automation_runner.execute(_manual_run(aid), registry=None))
    finally:
        os.close(fd)
    assert res["run"]["outcome"] == "skipped"
    assert res["run"]["reason"] == "skipped: another send to that session is in progress"
    assert live_target["writes"] == [] and store.get(aid)["consecutive_failures"] == 0


def test_the_body_cap_stops_reading_at_the_first_chunk_past_it():
    class Chunked:
        headers: dict = {}

        def __init__(self) -> None:
            self.consumed = 0

        async def stream(self):
            for _ in range(200):  # 1.6 MiB on offer
                self.consumed += 8192
                yield b"x" * 8192

    req = Chunked()
    with pytest.raises(model.AutomationError) as e:
        asyncio.run(routes._body(req))
    assert e.value.status == 413
    assert req.consumed <= routes.BODY_MAX + 8192


def _fds_on(path) -> int:
    n = 0
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            if os.readlink(f"/proc/self/fd/{fd}") == str(path):
                n += 1
    return n


def test_shutdown_racing_a_late_ownership_win_never_strands_the_lock():
    """Hermes' ordering on ef6e2ad: the worker WINS the flock, the tick is cancelled, shutdown runs,
    and only then does the worker try to store what it won. It must let go."""
    import threading

    won, go = threading.Event(), threading.Event()
    sched = automation_loop.Scheduler()

    def barrier():
        won.set()
        go.wait(5)

    sched._after_flock = barrier
    states = {}

    async def scenario():
        sched._task = asyncio.ensure_future(sched.tick())
        await asyncio.to_thread(won.wait, 5)
        stopping = asyncio.ensure_future(sched.shutdown())  # cancels the tick, then waits
        await asyncio.sleep(0.1)
        states["during_shutdown"] = sched.owner
        go.set()  # the worker now tries to store the fd it won
        await stopping

    asyncio.run(scenario())
    assert states["during_shutdown"] is False
    assert sched.owner is False
    assert _fds_on(automation_loop.lock_path()) == 0  # closed, not leaked
    peer = automation_loop.Scheduler()
    t0 = time.monotonic()
    assert peer.try_own() is True and time.monotonic() - t0 < 0.5  # a peer can own at once
    peer.release()


def test_an_acquisition_shutdown_cannot_see_still_lets_go_after_the_release():
    """The stop check alone: an attempt started OUTSIDE the tick (so nothing awaits it) wins the
    flock, shutdown releases, and only then does the attempt try to store its fd."""
    import threading

    won, go = threading.Event(), threading.Event()
    sched = automation_loop.Scheduler()

    def barrier():
        won.set()
        go.wait(5)

    sched._after_flock = barrier
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(sched.try_own()))
    t.start()
    assert won.wait(5)
    asyncio.run(sched.shutdown())  # nothing to cancel or join: it releases what it can see
    go.set()
    t.join(5)
    assert result == [False] and sched.owner is False
    assert _fds_on(automation_loop.lock_path()) == 0
    peer = automation_loop.Scheduler()
    assert peer.try_own() is True
    peer.release()
