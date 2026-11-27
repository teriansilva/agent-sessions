"""The automation queue (#2422): a step that launches a session waits for a free slot instead of
failing, and part of every launch limit is reserved for automations.

Read `docs/invariants/automations.md` ("The automation queue") and `session-limit.md`. The world
here is faked at the seams the runner calls (as in `test_generic_reviewer.py`): the API launch
(`mission_structured.automation_start`) marks its session running in a fake census and settles the
slots it was handed; the specialist pool, the session-limit gate, the automation store and the
drain are the real ones. The real native create path is pinned in
`test_automation_queue_native.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import pytest

from agent_sessions import (
    automation_loop,
    automation_queue,
    automation_runner,
    headless_dispatch,
    mission_structured,
    resource_limits,
    session_capacity,
    specialist_capacity,
    webhook_actions,
    webhooks,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import specialists_store as sstore
from agent_sessions.routes import automations as routes

ENGINE = "claude-api"


class Prov:
    engine_id = ENGINE


class World:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.running: set[str] = set()
        self.started: list[tuple[float, str]] = []  # (monotonic, run id)
        self.claims: dict[str, object] = {}  # session key → its specialist claim


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    folder = tmp_path / "work"
    folder.mkdir()
    w = World(folder)
    monkeypatch.setattr(automation_runner.engines, "get", lambda _e: Prov())
    monkeypatch.setattr(mission_structured, "is_api", lambda _p: True)
    monkeypatch.setattr(automation_runner, "client_settings_state", lambda _a: (True, ""))
    monkeypatch.setattr(automation_runner, "target_state", lambda _e: (True, ""))
    monkeypatch.setattr(automation_runner, "folder_ok", lambda _f: (True, ""))
    monkeypatch.setattr(automation_runner, "webhook_target_api", lambda _a: True)
    # A reservation's key is canonicalised against the engine registry; this world has none.
    monkeypatch.setattr(sstore, "canonical_key", lambda raw: raw)
    monkeypatch.setattr(session_capacity.ptybridge, "master_census", lambda: ([], 0))
    monkeypatch.setattr(session_capacity, "_api_running", lambda: set(w.running))

    async def fake_start(**kw):
        """The structured create as the runner sees it: the key is linked first, the held slots
        are taken over, the worker then counts itself (the census) and the brief lands."""
        session_id, _b, _s = mission_structured.automation_ids(kw["run_id"])
        key = f"{kw['engine']}:{session_id}"
        out = headless_dispatch.Dispatch(key=key, engine=kw["engine"], native=session_id, cwd="")
        kw["on_key"](key)
        hold = kw.get("hold")
        if hold is not None:
            slot = hold.take_session()
            claim = hold.take_claim()
            if claim is not None:
                sstore.activate(claim.reservation_id, owner=claim.owner)
                w.claims[key] = claim
            w.running.add(key)
            if slot is not None:
                slot.release()
        else:
            w.running.add(key)
        w.started.append((time.monotonic(), kw["run_id"]))
        out.launched = out.started = out.briefed = True
        out.seed_outcome = "delivered"
        return out

    monkeypatch.setattr(mission_structured, "automation_start", fake_start)
    automation_runner._TASKS.clear()
    automation_runner._FROM_QUEUE.clear()
    automation_runner._QUEUED.clear()
    automation_queue.bind(None)
    automation_queue.queued()
    yield w
    automation_queue.bind(None)
    automation_runner._TASKS.clear()


def limits(monkeypatch, *, sessions: int, reserved: int = 1, pool: int = 6, pool_reserved=1):
    monkeypatch.setattr(resource_limits, "max_sessions", lambda: sessions)
    resource_limits.save({"session_reserved_automations": min(reserved, sessions)})
    if reserved > sessions:  # a limit lowered below a saved reserve
        monkeypatch.setattr(resource_limits, "reserved_automations", lambda: reserved)
    specialist_capacity.set_limits(limit=pool, reserved=min(pool_reserved, pool))


def _spec(capacity: int = 1, title: str = "Reviewer") -> dict:
    return sstore.create(
        {
            "title": title,
            "summary": "",
            "skills": [],
            "engine": ENGINE,
            "model": "default",
            "effort": None,
            "capacity": capacity,
        }
    )


def _config(folder, *, specialist: dict | None = None, policy=None, trigger=None) -> dict:
    action = {
        "kind": "start_session",
        "engine": ENGINE,
        "folder": str(folder),
        "message": {"text": "review the change"},
    }
    if specialist is not None:
        action["specialist"] = {"specialist_id": specialist["id"], "version": 1}
    return {
        "name": f"auto {time.time_ns()}",
        "trigger": trigger or {"kind": "manual"},
        "action": action,
        "policy": {"concurrency": "allow", "max_concurrent": 3} if policy is None else policy,
    }


def _made(cfg: dict) -> str:
    aid = routes._create(cfg)["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _claim(aid: str, *, now: float | None = None) -> dict:
    res = store.begin_run(
        aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None, now=now
    )
    assert res["claimed"], res
    return res["run"]


async def _run(aid: str, **kw) -> dict:
    run = _claim(aid, **kw)
    return await automation_runner.execute(run, registry=None)


def _other_holder(spec: dict, key: str = "claude-api:interactive-1") -> dict:
    """An interactive session holding one of ``spec``'s slots (origin `other`)."""
    return sstore.reserve(spec["id"], 1, key, max_instances=specialist_capacity.max_instances())


async def _drain() -> automation_queue.DrainStats:
    stats = await automation_queue.drain(registry=None)
    await _settle_tasks()
    return stats


async def _settle_tasks() -> None:
    while automation_runner._TASKS:
        await asyncio.gather(*list(automation_runner._TASKS), return_exceptions=True)


def _end(world: World, key: str) -> None:
    """A session ends: its worker no longer counts, and its specialist slot is released."""
    world.running.discard(key)
    claim = world.claims.pop(key, None)
    if claim is not None:
        specialist_capacity.release(claim.reservation_id, owner=claim.owner)


def _key_of(run_id: str) -> str:
    return f"{ENGINE}:{mission_structured.automation_ids(run_id)[0]}"


# ---- the queue: capacity full → queued, then it continues ----------------------------------------


@pytest.mark.anyio
async def test_a_full_pool_queues_the_step_and_it_continues_when_a_slot_frees(world, monkeypatch):
    limits(monkeypatch, sessions=10, pool=6)
    spec = _spec(capacity=1)
    held = _other_holder(spec)
    aid = _made(_config(world.folder, specialist=spec, policy={"pause_after_failures": 1}))

    res = await _run(aid)
    run = store.get_run(res["run"]["id"])
    assert run["state"] == "queued" and run["outcome"] == ""
    assert run["queue"]["capacity"] == "specialist"
    assert run["queue"]["position"] == 1
    assert "at capacity" in run["queue"]["detail"]
    assert [s["step"] for s in run["steps"]][-1] == "queued"
    row = store.get(aid)
    assert row["consecutive_failures"] == 0 and not row["paused"], "a full limit is no failure"
    assert world.started == [] and world.running == set(), "nothing was created"

    sstore.release(held["reservation_id"], owner=held["owner"])
    stats = await _drain()
    assert stats.started == 1
    done = store.get_run(run["id"])
    assert (done["state"], done["outcome"]) == ("done", "ok"), done["reason"]
    assert "dequeued" in [s["step"] for s in done["steps"]]
    assert [r["origin"] for r in sstore.active()] == ["automation"]


@pytest.mark.anyio
async def test_a_full_session_limit_queues_likewise_and_counts_nothing_toward_the_pause(
    world, monkeypatch
):
    limits(monkeypatch, sessions=2, reserved=1)
    world.running |= {"claude-api:a", "claude-api:b"}
    aid = _made(_config(world.folder, policy={"pause_after_failures": 1}))
    for _ in range(3):
        await _run(aid)
    runs = store.list_runs(aid)["runs"]
    assert {r["state"] for r in runs} == {"queued"}
    assert sorted(r["queue"]["position"] for r in runs) == [1, 2, 3]
    assert all(r["queue"]["capacity"] == "session_limit" for r in runs)
    assert not store.get(aid)["paused"]
    assert store.queue_length() == 3

    world.running.discard("claude-api:a")
    stats = await _drain()
    # One starts; the other two wait for this automation's own `max_concurrent` (1) and are passed
    # over without asking any launch limit.
    assert (stats.started, stats.blocked, stats.gate_asks) == (1, 2, 1)
    assert store.queue_length() == 2


@pytest.mark.anyio
async def test_fifo_across_two_automations(world, monkeypatch):
    limits(monkeypatch, sessions=1, reserved=1)
    world.running.add("claude-api:busy")
    a = _made(_config(world.folder))
    b = _made(_config(world.folder))
    order = []
    t = time.time()
    for i, aid in enumerate([a, b, a, b]):
        res = await _run(aid, now=t + i)
        order.append(res["run"]["id"])
    for _ in order:
        world.running.clear()
        await _drain()
    assert [rid for _t, rid in world.started] == order


@pytest.mark.anyio
async def test_a_new_arrival_never_jumps_a_waiter(world, monkeypatch):
    limits(monkeypatch, sessions=3, reserved=1)
    world.running |= {"claude-api:x", "claude-api:y", "claude-api:z"}
    aid = _made(_config(world.folder))
    first = (await _run(aid))["run"]["id"]
    world.running.discard("claude-api:x")  # a slot frees, but a waiter is owed it
    second = (await _run(aid))["run"]["id"]
    assert store.get_run(second)["state"] == "queued"
    assert world.started == []
    await _drain()
    assert [rid for _t, rid in world.started] == [first]
    assert store.get_run(second)["state"] == "queued"


@pytest.mark.anyio
async def test_the_queue_survives_a_restart_and_recovery_never_touches_it(world, monkeypatch):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    aid = _made(_config(world.folder))
    rid = (await _run(aid))["run"]["id"]
    # A restart: a new process (owner token), a fresh schema cache, the recovery pass.
    monkeypatch.setattr(store, "_OWNER", None)
    store.reset_schema_cache_for_test()
    assert store.recover_interrupted() == []
    assert store.get_run(rid)["state"] == "queued"
    world.running.clear()
    await _drain()
    assert store.get_run(rid)["outcome"] == "ok"


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["off", "paused", "edited", "reapproval", "deleted"])
async def test_a_waiter_whose_automation_changed_settles_skipped(world, monkeypatch, how):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    aid = _made(
        _config(
            world.folder,
            trigger={
                "kind": "schedule",
                "cadence": {"kind": "interval", "every": 1, "unit": "hours"},
            },
        )
    )
    rid = (await _run(aid))["run"]["id"]
    if how == "off":
        store.mutate(aid, lambda _r: {"enabled": 0})
    elif how == "paused":
        store.mutate(aid, lambda _r: {"paused": 1, "paused_reason": "by hand"})
    elif how == "edited":
        store.mutate(aid, lambda _r: {"name": "renamed"})
    elif how == "reapproval":
        store.mutate(aid, lambda _r: {"needs_reapproval": 1, "reapproval_reason": "x"})
    world.running.clear()
    if how == "deleted":
        store.delete(aid)
        await _drain()
        with pytest.raises(store.NotFound):
            store.get_run(rid)
        return
    stats = await _drain()
    run = store.get_run(rid)
    assert (run["state"], run["outcome"]) == ("done", "skipped"), run
    assert stats.skipped == 1 and world.started == []
    assert store.get(aid)["consecutive_failures"] == 0


@pytest.mark.anyio
async def test_queue_max_wait_fails_it_without_pausing_and_rings_once(world, monkeypatch):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    rung: list[dict] = []
    monkeypatch.setattr(automation_runner, "announce_no_slot", rung.append)
    aid = _made(_config(world.folder, policy={"pause_after_failures": 1}))
    old = time.time() - store.QUEUE_MAX_WAIT_S - 60
    rids = [(await _run(aid, now=old + i))["run"]["id"] for i in range(3)]
    stats = await automation_queue.drain(registry=None)
    assert stats.expired == 3
    for rid in rids:
        run = store.get_run(rid)
        assert (run["outcome"], run["reason"]) == ("failed", store.QUEUE_EXPIRED_REASON)
    row = store.get(aid)
    assert not row["paused"] and row["consecutive_failures"] == 0
    assert len(rung) == 1 and rung[0]["episode"]
    # The same episode: another expiry of this automation rings nothing new.
    rid = (await _run(aid, now=old + 10))["run"]["id"]
    await automation_queue.drain(registry=None)
    assert store.get_run(rid)["outcome"] == "failed"
    assert len(rung) == 1


@pytest.mark.anyio
async def test_a_refused_step_creates_no_session(world, monkeypatch):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    aid = _made(_config(world.folder))
    rid = (await _run(aid))["run"]["id"]
    assert world.started == []
    run = store.get_run(rid)
    assert run["session_key"] is None, "nothing was linked: nothing was created"
    assert _key_of(rid) not in store.origins()


@pytest.mark.anyio
async def test_tell_hq_never_queues(world, monkeypatch):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    told: list = []
    monkeypatch.setattr(automation_runner, "_announce", lambda *a, **kw: told.append(a) or {})
    aid = _made(
        {
            "name": "tell",
            "trigger": {"kind": "manual"},
            "action": {"kind": "tell_hq", "message": {"text": "hello"}},
            "policy": {},
        }
    )
    res = await _run(aid)
    assert res["run"]["outcome"] == "ok" and told
    assert store.queue_length() == 0


@pytest.mark.anyio
async def test_a_schedule_overlap_still_skips(world, monkeypatch):
    limits(monkeypatch, sessions=10)
    aid = _made(
        _config(
            world.folder,
            trigger={
                "kind": "schedule",
                "cadence": {"kind": "interval", "every": 1, "unit": "hours"},
            },
            policy={},
        )
    )
    first = store.begin_run(aid, trigger="schedule", slot="s1", fire_at=1.0)
    assert first["run"]["state"] == "dispatching"
    second = store.begin_run(aid, trigger="schedule", slot="s2", fire_at=2.0)
    assert second["run"]["outcome"] == "skipped"
    assert second["reason"] == "skipped: previous run still active"


def test_a_webhook_over_max_concurrent_is_queued_not_skipped(world, monkeypatch):
    limits(monkeypatch, sessions=10)
    spawned: list[dict] = []
    monkeypatch.setattr(automation_runner, "spawn", lambda run, registry=None: spawned.append(run))
    wid = webhooks.create("ci")[0]["id"]
    aid = _made(_config(world.folder, trigger={"kind": "webhook", "webhook_id": wid}, policy={}))
    for n in (1, 2, 3):
        asyncio.run(
            webhook_actions.handle(
                aid, json.dumps({"n": n}).encode(), {}, registry=None, source=wid
            )
        )
    runs = sorted(store.list_runs(aid)["runs"], key=lambda r: r["created_at"])
    assert [r["state"] for r in runs] == ["dispatching", "queued", "queued"]
    assert runs[1]["queue"]["capacity"] == "concurrency"
    assert len(spawned) == 1
    # A queued delivery is CLAIMED: the same body redelivered runs nothing twice.
    asyncio.run(
        webhook_actions.handle(aid, json.dumps({"n": 2}).encode(), {}, registry=None, source=wid)
    )
    assert len(store.list_runs(aid)["runs"]) == 3


def test_pruning_never_drops_a_queued_run():
    aid = store.create(_config("/tmp"), {})["id"]
    with store._tx() as con:
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, state, scope, created_at,"
            " queue_capacity) VALUES ('q1', ?, 'webhook', 'queued', '{}', 1.0, 'session_limit')",
            (aid,),
        )
    assert store.prune_runs(now=time.time(), keep=0, max_age_s=1) == 0
    assert store.get_run("q1")["state"] == "queued"


# ---- the blocked prefix and the burst ----------------------------------------------------------


def _queued_rows(aid: str, n: int, *, start: float, specialist: dict | None = None) -> list[str]:
    """``n`` queued runs of ``aid`` as a burst leaves them (claimed, counted once, no effect)."""
    row = store.get(aid)
    scope = {
        "config": row["config"],
        "pins": row["pins"],
        "consented_scope": row["consented_scope"],
        "consented_at": row["consented_at"],
        "revision": row["revision"],
    }
    ids = []
    with store._tx() as con:
        for i in range(n):
            rid = f"{aid[:8]}{i:04d}{time.time_ns() % 10**8:08d}"
            con.execute(
                "INSERT INTO automation_runs (id, automation_id, trigger, slot, state, scope, "
                "counts, created_at, queue_capacity) VALUES (?,?,?,?,?,?,1,?,?)",
                (
                    rid,
                    aid,
                    "webhook",
                    f"delivery:{rid}",
                    "queued",
                    json.dumps(scope),
                    start + i * 0.001,
                    "specialist" if specialist else "session_limit",
                ),
            )
            ids.append(rid)
    return ids


@pytest.mark.anyio
async def test_more_than_fifty_rows_blocked_on_one_specialist_hide_nothing_and_stop_no_expiry(
    world, monkeypatch
):
    limits(monkeypatch, sessions=200, pool=32)
    spec = _spec(capacity=1)
    _other_holder(spec)
    blocked_aid = _made(_config(world.folder, specialist=spec))
    plain_aid = _made(_config(world.folder))
    t = time.time() - 60
    blocked = _queued_rows(blocked_aid, 120, start=t, specialist=spec)
    eligible = _queued_rows(plain_aid, 1, start=t + 1)  # behind all 120
    expired = _queued_rows(plain_aid, 1, start=time.time() - store.QUEUE_MAX_WAIT_S - 5)
    stats = await _drain()
    assert stats.expired == 1 and store.get_run(expired[0])["outcome"] == "failed"
    assert stats.started == 1 and [rid for _t, rid in world.started] == eligible
    assert stats.gate_asks == 2, "the specialist is asked ONCE; its later rows are skipped"
    assert stats.scanned == 121 and stats.pages == 4
    assert all(store.get_run(r)["state"] == "queued" for r in blocked[:3])


def _burst_world(monkeypatch, world) -> tuple[str, str, dict]:
    limits(monkeypatch, sessions=6, reserved=1, pool=6, pool_reserved=1)
    spec = _spec(capacity=1, title="Blocked reviewer")
    _other_holder(spec)  # the specialist stays full for the whole burst
    wid = webhooks.create("ci")[0]["id"]
    trig = {"kind": "webhook", "webhook_id": wid}
    a = _made(_config(world.folder, specialist=spec, trigger=trig))
    b = _made(_config(world.folder, trigger=trig))
    return a, b, spec


async def _burst(world, a: str, b: str, *, events: int) -> dict:
    """``events`` deliveries alternating over ``a`` and ``b``, through the real delivery path
    (claim → run or queue), with the drain woken by every release as in production."""
    sched = automation_loop.Scheduler(registry=None)
    sched._fd = -1  # owner, without the real flock (this test is not about ownership)
    automation_loop.SCHEDULER = sched
    automation_queue.bind(asyncio.get_running_loop())
    wid = store.webhook_of(a)
    for n in range(events):
        aid = a if n % 2 == 0 else b
        await webhook_actions.handle(
            aid, json.dumps({"n": n}).encode(), {}, registry=None, source=wid
        )
        await _settle_tasks()
    return {"depth": store.queue_length()}


@pytest.mark.anyio
async def test_a_burst_of_fifty_events_is_served_in_order_and_measured(world, monkeypatch):
    """#2422 rule 4: a 50-event burst across two automations with one specialist blocked.
    Records queue depth, rows scanned, whole-tick duration and slot-free-to-start delay."""
    a, b, _spec_row = _burst_world(monkeypatch, world)
    try:
        depth = (await _burst(world, a, b, events=50))["depth"]
        # B's runs took every free session slot (an automation may use the reserved one too);
        # A's wait on their specialist.
        assert len(world.started) == 6
        ticks: list[automation_queue.DrainStats] = []
        delays: list[float] = []
        while store.queue_length() and len(ticks) < 200:
            running_b = [k for k in sorted(world.running)]
            if running_b:
                before = len(world.started)
                freed = time.monotonic()
                _end(world, running_b[0])
                automation_queue.wake()  # what a session's end does (coalesced)
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if len(world.started) > before or not store.queue_length():
                        break
                await _settle_tasks()
                if len(world.started) > before:
                    delays.append(world.started[before][0] - freed)
            ticks.append(await _drain())
            if not world.running and store.queue_length():
                break
        left = store.list_runs(a, limit=200)["runs"]
        assert all(r["state"] == "queued" for r in left if r["state"] != "done")
        assert all(r["queue"]["capacity"] == "specialist" for r in left if r["queue"])
        done_b = [r for r in store.list_runs(b, limit=200)["runs"] if r["outcome"] == "ok"]
        assert len(done_b) == 25, "every B event got its session: none failed or skipped"
        order = [rid for _t, rid in world.started]
        created = {r["id"]: r["created_at"] for r in store.list_runs(b, limit=200)["runs"]}
        assert [created[r] for r in order] == sorted(created[r] for r in order), "FIFO"
        assert not store.get(a)["paused"] and not store.get(b)["paused"]
        metrics = {
            "events": 50,
            "queue_depth_after_burst": depth,
            "rows_scanned_max": max(t.scanned for t in ticks),
            "gate_asks_max": max(t.gate_asks for t in ticks),
            "tick_duration_ms_max": round(max(t.duration_s for t in ticks) * 1000, 2),
            "tick_duration_ms_median": round(
                statistics.median(t.duration_s for t in ticks) * 1000, 2
            ),
            "slot_free_to_start_ms_median": round(statistics.median(delays) * 1000, 2),
            "slot_free_to_start_ms_max": round(max(delays) * 1000, 2),
        }
        _record("burst-50", metrics)
        assert metrics["slot_free_to_start_ms_max"] < automation_loop.INTERVAL_S * 1000
    finally:
        automation_loop.SCHEDULER = None
        automation_queue.bind(None)


@pytest.mark.anyio
async def test_a_burst_with_more_than_fifty_rows_blocked_on_one_specialist(world, monkeypatch):
    """The same measurement with 60 rows blocked on one specialist ahead of the eligible ones."""
    limits(monkeypatch, sessions=6, reserved=1, pool=6)
    spec = _spec(capacity=1)
    _other_holder(spec)
    blocked_aid = _made(_config(world.folder, specialist=spec))
    plain_aid = _made(_config(world.folder))
    t = time.time() - 30
    _queued_rows(blocked_aid, 60, start=t, specialist=spec)
    # As many eligible as its automation's `max_concurrent` (3), so the count is deterministic.
    eligible = _queued_rows(plain_aid, 3, start=t + 1)
    stats = await _drain()
    assert [rid for _t, rid in world.started] == eligible
    _record(
        "blocked-60",
        {
            "queued": 63,
            "rows_scanned": stats.scanned,
            "pages": stats.pages,
            "gate_asks": stats.gate_asks,
            "started": stats.started,
            "tick_duration_ms": round(stats.duration_s * 1000, 2),
        },
    )


def _record(name: str, metrics: dict) -> None:
    """The burst numbers, for the PR (`AGENT_SESSIONS_QUEUE_METRICS` names a JSONL file)."""
    print(f"automation-queue metrics {name}: {json.dumps(metrics, sort_keys=True)}")
    out = os.environ.get("AGENT_SESSIONS_QUEUE_METRICS")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"case": name, **metrics}, sort_keys=True) + "\n")


# ---- admission and crash boundaries -----------------------------------------------------------


@pytest.mark.anyio
async def test_two_concurrent_drainers_start_each_run_once(world, monkeypatch):
    limits(monkeypatch, sessions=50)
    aid = _made(_config(world.folder))
    rids = _queued_rows(aid, 3, start=time.time() - 10)
    monkeypatch.setattr(model, "MAX_CONCURRENT_MAX", 3)
    # Two drainers that do not share the process-local lock (a peer, or a lock bug).
    monkeypatch.setattr(automation_queue, "_lock", lambda: asyncio.Lock())
    await asyncio.gather(
        automation_queue.drain(registry=None), automation_queue.drain(registry=None)
    )
    await _settle_tasks()
    started = [rid for _t, rid in world.started]
    assert sorted(started) == sorted(rids) and len(started) == len(set(started))


def test_claim_queued_is_one_compare_and_set():
    aid = store.create(_config("/tmp"), {})["id"]
    row = store.get(aid)
    with store._tx() as con:
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, state, scope, created_at) "
            "VALUES ('q1', ?, 'manual', 'queued', ?, 1.0)",
            (aid, json.dumps({"revision": row["revision"]})),
        )
    store.mutate(aid, lambda _r: {"enabled": 1})
    first = store.claim_queued("q1")
    second = store.claim_queued("q1")
    assert second["verdict"] == store.CLAIM_GONE
    assert first["verdict"] in (store.CLAIM_RUN, store.CLAIM_SETTLED)


@pytest.mark.anyio
async def test_a_peer_takes_over_the_queue_and_a_crash_after_dispatch_is_never_retried(
    world, monkeypatch
):
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")
    aid = _made(_config(world.folder))
    rid = (await _run(aid))["run"]["id"]
    # The process that queued it dies; a peer becomes the owner: the run is still queued.
    monkeypatch.setattr(store, "_OWNER", None)
    assert store.recover_interrupted() == [] and store.get_run(rid)["state"] == "queued"
    # Peer claims it (queued → dispatching), then crashes before the effect.
    got = store.claim_queued(rid)
    assert got["verdict"] == store.CLAIM_RUN
    with store._tx() as con:
        con.execute("UPDATE automation_runs SET owner='1:dead:x' WHERE id=?", (rid,))
    assert store.recover_interrupted() == [rid]
    run = store.get_run(rid)
    assert (run["state"], run["outcome"]) == ("done", "interrupted")
    world.running.clear()
    stats = await _drain()
    assert stats.started == 0 and world.started == [], "never retried"


@pytest.mark.anyio
async def test_typed_refusal_requeues_only_before_any_effect(world, monkeypatch):
    """A launch a limit refused before anything existed (typed `capacity`) goes back to the queue;
    the same words without the type fail as before — nothing matches error text."""
    limits(monkeypatch, sessions=10)
    aid = _made(_config(world.folder, policy={"pause_after_failures": 5}))

    async def refused(capacity: str):
        async def start(**kw):
            session_id = mission_structured.automation_ids(kw["run_id"])[0]
            out = headless_dispatch.Dispatch(
                key=f"{ENGINE}:{session_id}", engine=ENGINE, native=session_id, cwd=""
            )
            out.reason = "the pool is at its limit (6 of 6 instances running)"
            out.capacity = capacity
            return out

        monkeypatch.setattr(mission_structured, "automation_start", start)
        return await _run(aid)

    typed = await refused("pool")
    assert store.get_run(typed["run"]["id"])["state"] == "queued"
    store.expire_queued(max_wait_s=-1)  # out of the way: a fresh run never queues behind it
    untyped = await refused("")
    assert store.get_run(untyped["run"]["id"])["outcome"] == "refused"


# ---- the reserve ---------------------------------------------------------------------------------


def test_an_other_start_is_refused_at_limit_minus_reserved_on_the_pool(monkeypatch):
    limits(monkeypatch, sessions=10, pool=3, pool_reserved=1)
    spec = _spec(capacity=5)
    keep = specialist_capacity.reserved_effective()
    for i in range(2):
        sstore.reserve(spec["id"], 1, f"claude-api:o{i}", max_instances=3, reserved=keep)
    with pytest.raises(sstore.CapacityRefused) as refused:
        sstore.reserve(spec["id"], 1, "claude-api:o2", max_instances=3, reserved=keep)
    assert refused.value.limit == "pool" and "reserved for automations" in str(refused.value)
    got = sstore.reserve(
        spec["id"], 1, "claude-api:a0", max_instances=3, reserved=keep, origin="automation"
    )
    assert got["origin"] == "automation"
    # The total guard holds for every origin: full is full.
    with pytest.raises(sstore.CapacityRefused):
        sstore.reserve(
            spec["id"], 1, "claude-api:a1", max_instances=3, reserved=keep, origin="automation"
        )


def test_a_pool_full_of_automation_sessions_refuses_an_interactive_start():
    spec = _spec(capacity=5)
    for i in range(3):
        sstore.reserve(spec["id"], 1, f"claude-api:a{i}", max_instances=3, origin="automation")
    with pytest.raises(sstore.CapacityRefused):
        sstore.reserve(spec["id"], 1, "claude-api:o", max_instances=3, reserved=1)


def test_a_retake_keeps_its_stored_origin():
    spec = _spec(capacity=5)
    sstore.reserve(spec["id"], 1, "claude-api:a", max_instances=3, origin="automation")
    again = sstore.reserve(spec["id"], 1, "claude-api:a", max_instances=3)
    assert again["created"] is False and again["origin"] == "automation"


def test_an_other_start_is_refused_at_limit_minus_reserved_on_the_session_limit(world, monkeypatch):
    limits(monkeypatch, sessions=3, reserved=1)
    world.running |= {"claude-api:o1", "claude-api:o2"}
    with pytest.raises(session_capacity.SessionLimitReached) as refused:
        session_capacity.admit("claude-api:o3")
    assert refused.value.reserved == 1 and "kept for automations" in str(refused.value)
    slot = session_capacity.admit("claude-api:a1", origin=session_capacity.AUTOMATION)
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("claude-api:a2", origin=session_capacity.AUTOMATION)
    slot.release()


@pytest.mark.anyio
async def test_the_census_reads_launch_provenance_across_a_restart(world, monkeypatch):
    """An automation's session still running after its launch slot was released counts as
    `automation`, also after a restart; an interactive session an automation only messages stays
    `other`, so the next interactive start still respects the reserve."""
    limits(monkeypatch, sessions=4, reserved=2)
    aid = _made(_config(world.folder))
    rid = (await _run(aid))["run"]["id"]
    auto_key = _key_of(rid)
    assert auto_key in world.running and auto_key in store.launched_session_keys()
    # One interactive session that an automation later sends to: linked, but not an origin.
    world.running.add("claude-api:mine")
    store.link(rid, session_key="claude-api:mine", origin=False)
    store.reset_schema_cache_for_test()  # a restart: nothing in memory carries the origin
    monkeypatch.setattr(store, "_OWNER", None)
    census = session_capacity.census()
    assert (census["total"], census["other"]) == (2, 1)
    # Limit 4, two reserved: other starts may hold 2. One more interactive start fits ...
    # (were the automation's session miscounted as `other` after the restart, it would not).
    held = session_capacity.admit("claude-api:mine2")
    # ... and the next is refused although a slot is free: it is the automations'.
    with pytest.raises(session_capacity.SessionLimitReached) as refused:
        session_capacity.admit("claude-api:mine3")
    assert refused.value.reserved == 2
    # Had the message target been miscounted as `automation`, mine3 would have been admitted.
    assert session_capacity.census()["total"] == 3
    held.release()


def test_limits_below_the_reserve_reserve_the_whole_limit(world, monkeypatch):
    limits(monkeypatch, sessions=1, reserved=1, pool=1, pool_reserved=1)
    assert resource_limits.reserved_effective() == 1
    assert specialist_capacity.reserved_effective() == 1
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("claude-api:mine")
    session_capacity.admit("claude-api:auto", origin=session_capacity.AUTOMATION).release()
    # A limit saved below a stored reserve is allowed; the reserve in force is the limit.
    monkeypatch.setattr(resource_limits, "reserved_automations", lambda: 5)
    assert resource_limits.reserved_effective(2) == 2
    # Lowering the limit stops nothing: what runs keeps running, starts are only refused.
    world.running |= {"claude-api:x", "claude-api:y"}
    assert world.running == {"claude-api:x", "claude-api:y"}


@pytest.mark.parametrize(
    "body, ok",
    [
        ({"pool_reserved_automations": 1}, True),
        ({"pool_reserved_automations": 6}, True),
        ({"pool_reserved_automations": 7}, False),  # above the pool limit in force (6)
        ({"pool_reserved_automations": 0}, False),
        ({"pool_reserved_automations": True}, False),
        ({"pool_max_instances": 2, "pool_reserved_automations": 3}, False),
        ({"pool_max_instances": 8, "pool_reserved_automations": 7}, True),
    ],
)
def test_the_pool_reserve_is_validated_against_its_limit(body, ok):
    if ok:
        specialist_capacity.set_limits(
            limit=body.get("pool_max_instances"), reserved=body["pool_reserved_automations"]
        )
        assert specialist_capacity.reserved_automations() == body["pool_reserved_automations"]
    else:
        with pytest.raises(ValueError):
            specialist_capacity.set_limits(
                limit=body.get("pool_max_instances"), reserved=body["pool_reserved_automations"]
            )


@pytest.mark.parametrize(
    "patch, ok",
    [
        ({"session_reserved_automations": 1}, True),
        ({"session_reserved_automations": 0}, False),
        ({"max_sessions": 4, "session_reserved_automations": 4}, True),
        ({"max_sessions": 4, "session_reserved_automations": 5}, False),
    ],
)
def test_the_session_reserve_is_validated_against_its_limit(patch, ok):
    if ok:
        resource_limits.save(patch)
        assert resource_limits.reserved_automations() == patch["session_reserved_automations"]
    else:
        with pytest.raises(ValueError):
            resource_limits.save(patch)


def test_the_store_migrates_to_v5_and_the_reservations_gain_their_origin(tmp_path, monkeypatch):
    assert store.SCHEMA_VERSION == 5
    with store._read() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(automation_runs)")}
        idx = {r["name"] for r in con.execute("PRAGMA index_list(automation_runs)")}
    assert "queue_capacity" in cols and "automation_runs_by_state" in idx
    spec = _spec()
    r = sstore.reserve(spec["id"], 1, "claude-api:x", max_instances=3)
    assert r["origin"] == "other"


# ---- Hermes on #2425: wake-up loop, cancellation, consent behind a full gate ------------------


@pytest.mark.anyio
async def test_a_blocked_concurrency_gate_never_makes_the_drain_wake_itself(world, monkeypatch):
    """One run of an automation (`max_concurrent` 1) is dispatching, another of it waits, the
    specialist has room: with no external release, a wake-up runs a BOUNDED number of passes and
    holds no slot afterwards (it used to take and give back the specialist slot, whose release
    woke the drain again, for ever)."""
    limits(monkeypatch, sessions=10, pool=6)
    spec = _spec(capacity=3)
    aid = _made(_config(world.folder, specialist=spec, policy={"concurrency": "skip"}))
    first = _claim(aid)  # dispatching: the automation is at its max_concurrent
    _queued_rows(aid, 1, start=time.time() - 5, specialist=spec)
    passes = 0
    real = automation_queue.drain

    async def counted(**kw):
        nonlocal passes
        passes += 1
        return await real(**kw)

    monkeypatch.setattr(automation_queue, "drain", counted)
    sched = automation_loop.Scheduler(registry=None)
    sched._fd = -1
    automation_loop.SCHEDULER = sched
    automation_queue.bind(asyncio.get_running_loop())
    try:
        automation_queue.queued()
        automation_queue.wake()
        await asyncio.sleep(0.5)
    finally:
        automation_loop.SCHEDULER = None
        automation_queue.bind(None)
    assert 1 <= passes <= 2, f"{passes} drain passes with nothing released"
    assert sstore.active() == [], "no specialist slot is held for a run that can't start"
    assert store.get_run(first["id"])["state"] == "dispatching"


@pytest.mark.anyio
@pytest.mark.parametrize("where", ["drain", "runner"])
async def test_a_cancellation_while_the_gate_is_parked_leaves_no_reservation(
    world, monkeypatch, where
):
    """The gate's worker wins both slots AFTER the caller was cancelled: what it holds is given
    back before the cancellation propagates (it used to keep a durable specialist reservation
    with no launch)."""
    import threading

    limits(monkeypatch, sessions=10, pool=6)
    spec = _spec(capacity=2)
    aid = _made(_config(world.folder, specialist=spec))
    parked, go = threading.Event(), threading.Event()
    real = automation_queue.launch_slot

    def slow(run):
        got = real(run)  # the slots are taken ...
        parked.set()
        go.wait(10)  # ... and the worker is still running when the cancellation lands
        return got

    monkeypatch.setattr(automation_queue, "launch_slot", slow)
    if where == "drain":
        _queued_rows(aid, 1, start=time.time() - 5, specialist=spec)
        task = asyncio.ensure_future(automation_queue.drain(registry=None))
    else:
        task = asyncio.ensure_future(
            automation_runner._start_session(
                _claim(aid), store.get(aid)["config"], store.get(aid)["pins"], registry=None
            )
        )
    await asyncio.to_thread(parked.wait, 10)
    assert len(sstore.active()) == 1  # held by the parked worker
    task.cancel()
    await asyncio.sleep(0.05)
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sstore.active() == [], "the hold won after the cancellation was released"
    assert list(session_capacity._slots_dir().iterdir()) == []


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["off", "edited", "expired_wait"])
async def test_a_withdrawn_waiter_settles_skipped_while_the_limit_stays_full(
    world, monkeypatch, how
):
    """Consent is reconciled whatever the launch limits say: a run whose automation was turned
    off or edited settles `skipped` though the session limit is still full, and one that also
    waited past `QUEUE_MAX_WAIT_S` is recorded as the operator's withdrawal, not a capacity
    failure (it used to stay `queued` behind the full gate, then expire with a bell)."""
    limits(monkeypatch, sessions=1)
    world.running.add("claude-api:busy")  # stays full for the whole test
    rung: list[dict] = []
    monkeypatch.setattr(automation_runner, "announce_no_slot", rung.append)
    aid = _made(_config(world.folder))
    behind = _made(_config(world.folder))
    old = time.time() - store.QUEUE_MAX_WAIT_S - 60 if how == "expired_wait" else None
    rid = (await _run(aid, now=old))["run"]["id"]
    later = (await _run(behind))["run"]["id"]
    store.mutate(aid, lambda _r: {"enabled": 0} if how != "edited" else {"name": "renamed"})
    stats = await _drain()
    run = store.get_run(rid)
    assert (run["state"], run["outcome"]) == ("done", "skipped"), run
    assert stats.skipped == 1 and rung == []
    assert store.get_run(later)["state"] == "queued", "the other automation still waits"
