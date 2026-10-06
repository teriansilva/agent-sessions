"""Start again after a failure that delivered nothing (#966 P2, the server half).

`failed -> planned` reopens a mission whose launch failed. The hazard is reopening one whose agent
DID act, so the transition is allowed only on affirmative, durable, dispatch-bound evidence that
nothing was written:

* the dispatcher records a `seed_outcome` (`not_attempted` / `zero_write` / `partial` /
  `delivered` / `unknown`) and whether the teardown was confirmed, on the dispatch record, BEFORE
  the mission settles;
* only `not_attempted` or `zero_write` with a confirmed teardown is eligible, and the evidence is
  re-read under the existing fenced state write, never taken from the browser;
* a missing acknowledgement is not evidence: a crash between the write and the ack ends `unknown`.

Fakes stand at the dispatch-record boundary. Nothing here spawns a process.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest

from agent_sessions import (
    handoff,
    headless_dispatch,
    headless_seed,
    mission_dispatch,
    mission_dispatch_recover,
    mission_fence,
    missions,
    ptybridge,
    runtime_cleanup,
    start_evidence,
)

UUID = "11111111-2222-3333-4444-555555555555"
KEY = f"claude:{UUID}"
#: A pid above any `pid_max`, so its lease is provably dead.
DEAD_OWNER = "4194305:1"
#: The brief is sensitive operator text; it must never appear in the failure message or meta.
BRIEF = "brief-text-that-must-not-leak-7f3a"
REASON = "the agent did not register a live session within 90s: no entry"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    handoff.reset_for_tests()
    yield tmp_path
    handoff.reset_for_tests()
    missions.reset_schema_cache_for_test()


@pytest.fixture(autouse=True)
def _teardown_proves_empty(monkeypatch):
    """The request-time teardown after a failed settle. Stubbed: nothing here signals a process."""
    calls: list[str] = []

    async def fake(engine, native, **kw):
        calls.append(f"{engine}:{native}")
        return "term"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", fake)
    return calls


def _planned(brief=BRIEF):
    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd="/repo", engine="claude", brief=brief)
    return m["id"], plan


def _fake_dispatch(monkeypatch, *, seed_outcome, teardown="stopped", briefed=False, fields=True):
    """A launch that came up and failed, carrying the launcher's own evidence."""

    async def fake(**kw):
        kw["on_key"](KEY)
        if not fields:
            # A stand-in with no evidence at all: the dispatcher must not invent any.
            class Bare:
                key = KEY
                launched = True
                started = False
                reason = REASON

            b = Bare()
            b.briefed = briefed
            b.ok = False
            return b
        out = headless_dispatch.Dispatch(key=KEY, engine="claude", native=UUID, cwd=kw["cwd"])
        out.launched = True
        out.started = False
        out.briefed = briefed
        out.reason = REASON
        out.seed_outcome = seed_outcome
        out.teardown = teardown
        return out

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)


def _failed(monkeypatch, seed_outcome="not_attempted", **kw):
    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _fake_dispatch(monkeypatch, seed_outcome=seed_outcome, **kw)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert out["state"] == "failed", out
    return mid, plan


def _failure_event(mid) -> dict:
    events = missions.get_mission(mid)["events"]
    for e in events:  # newest first
        if e["kind"] == "state" and (e.get("meta") or {}).get("to") == "failed":
            return e
    raise AssertionError("no failure event")


def _start_again(mid):
    return missions.set_state(mid, "failed", "planned")


def _refused(mid) -> missions.MissionError:
    with pytest.raises(missions.MissionError) as e:
        _start_again(mid)
    assert e.value.status == 409, e.value
    assert missions.get_mission(mid)["state"] == "failed"
    assert missions.get_plan(mid) is None, "a refused start again restored a plan anyway"
    return e.value


# ---- allowed ------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", ["not_attempted", "zero_write"])
def test_start_again_is_ALLOWED_on_affirmative_nothing_written_evidence(store, monkeypatch, seed):
    mid, plan = _failed(monkeypatch, seed)
    row = missions.get_mission(mid)
    assert row["seed_outcome"] == seed
    assert row["retry_eligible"] is True, row.get("retry_reason")

    out = _start_again(mid)
    assert out["state"] == "planned"

    # A USABLE PLAN, CONSISTENT WITH ITS STATE: the approved proposal is back under the CURRENT
    # generation, `plan_state` says `ready`, and it carries a NEW plan id so no late write keyed on
    # the failed attempt's id can land on the next one.
    stored = missions.get_plan(mid)
    assert stored is not None
    assert (stored["engine"], stored["cwd"], stored["brief"]) == ("claude", "/repo", BRIEF)
    assert stored["plan_id"] != plan["plan_id"]
    intent = missions.plan_intent(mid)
    assert intent["plan_state"] == "ready"
    assert intent["stored_generation"] == intent["plan_generation"]

    # …SO BEGIN WORKS AS USUAL.
    missions.claim_plan(mid, stored["plan_id"])
    assert missions.get_mission(mid)["state"] == "dispatching"


def test_the_evidence_is_persisted_BEFORE_the_mission_settles(store, monkeypatch):
    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _fake_dispatch(monkeypatch, seed_outcome="not_attempted")
    seen: list[dict] = []
    real = missions.settle_dispatch

    def spy(mission_id, **kw):
        if kw.get("to") == "failed":
            d = missions.get_dispatch(mission_id) or {}
            seen.append(
                {
                    "state": missions.get_mission(mission_id)["state"],
                    "seed_outcome": d.get("seed_outcome"),
                    "teardown_confirmed": d.get("teardown_confirmed"),
                }
            )
        return real(mission_id, **kw)

    monkeypatch.setattr(missions, "settle_dispatch", spy)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert seen == [
        {"state": "dispatching", "seed_outcome": "not_attempted", "teardown_confirmed": True}
    ]


def test_a_teardown_PROVED_later_confirms_the_evidence(store, monkeypatch):
    """The launcher's own teardown leaked; the request-time teardown then proved the boundary
    empty, which is what `clear_dispatch(stopped=True)` records."""
    mid, _ = _failed(monkeypatch, "not_attempted", teardown="leaked")
    assert missions.get_dispatch(mid) is None
    assert missions.get_mission(mid)["retry_eligible"] is True
    assert _start_again(mid)["state"] == "planned"


# ---- refused ------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", ["partial", "delivered", "unknown"])
def test_start_again_is_REFUSED_when_the_brief_may_have_been_typed(store, monkeypatch, seed):
    mid, _ = _failed(monkeypatch, seed, briefed=(seed == "delivered"))
    row = missions.get_mission(mid)
    assert row["seed_outcome"] == seed
    assert row["retry_eligible"] is False
    _refused(mid)


def test_a_launcher_that_reports_NO_evidence_is_unknown_and_refused(store, monkeypatch):
    mid, _ = _failed(monkeypatch, fields=False)
    assert missions.get_mission(mid)["seed_outcome"] == "unknown"
    _refused(mid)


def test_MISSING_evidence_is_refused(store):
    """A failure with no dispatch evidence at all — the operator marked a launch failed."""
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"])
    missions.set_state(mid, "dispatching", "failed")
    assert missions.get_mission(mid)["retry_eligible"] is False
    _refused(mid)


def test_an_UNCONFIRMED_teardown_is_refused(store):
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"])
    missions.note_dispatch_session(mid, KEY, expect_plan=plan["plan_id"])
    assert missions.note_dispatch_evidence(
        mid, expect_plan=plan["plan_id"], seed_outcome="not_attempted", teardown_confirmed=False
    )
    missions.settle_dispatch(mid, to="failed", detail=REASON, expect_plan=plan["plan_id"])
    row = missions.get_mission(mid)
    assert row["seed_outcome"] == "not_attempted"
    assert row["retry_eligible"] is False
    err = _refused(mid)
    assert "stopped" in str(err)


def test_a_RETAINED_teardown_obligation_is_refused(store, monkeypatch, _teardown_proves_empty):
    """Both teardowns leaked: the dispatch record is kept for recovery, so a possibly live agent
    is still out there and nothing may reopen the mission beside it."""

    async def leaked(engine, native, **kw):
        return "leaked"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaked)
    mid, _ = _failed(monkeypatch, "not_attempted", teardown="leaked")
    assert missions.get_dispatch(mid) is not None
    assert missions.get_mission(mid)["retry_eligible"] is False
    _refused(mid)


def test_a_mission_that_RAN_is_refused_even_with_old_eligible_evidence(store, monkeypatch):
    mid, _ = _failed(monkeypatch, "not_attempted")
    missions.set_state(mid, "failed", "running")
    missions.set_state(mid, "running", "failed")
    assert missions.get_mission(mid)["retry_eligible"] is False
    _refused(mid)


def test_a_mission_that_RAN_is_refused_even_if_its_evidence_SURVIVED(store, monkeypatch):
    """The "ever ran" check on its own. Every lifecycle write deletes the evidence today, so no
    route reaches this; the row is put back by hand, as a future path that forgot to invalidate
    it would leave it, and the timeline's `running` must still refuse."""
    mid, _ = _failed(monkeypatch, "not_attempted")
    con = sqlite3.connect(missions._db_path())
    con.row_factory = sqlite3.Row
    saved = dict(
        con.execute("SELECT * FROM mission_dispatch_evidence WHERE mission_id=?", (mid,)).fetchone()
    )
    con.close()
    missions.set_state(mid, "failed", "running")
    missions.set_state(mid, "running", "failed")
    con = sqlite3.connect(missions._db_path())
    cols = ", ".join(saved)
    marks = ", ".join("?" for _ in saved)
    con.execute(
        f"INSERT INTO mission_dispatch_evidence ({cols}) VALUES ({marks})",  # noqa: S608
        tuple(saved.values()),
    )
    con.commit()
    con.close()
    assert missions.get_mission(mid)["seed_outcome"] == "not_attempted"
    assert missions.get_mission(mid)["retry_eligible"] is False
    err = _refused(mid)
    assert "run" in str(err)


@pytest.mark.parametrize("later", ["running", "review", "done"])
def test_running_or_later_is_refused(store, monkeypatch, later):
    mid, _ = _failed(monkeypatch, "not_attempted")
    missions.set_state(mid, "failed", "running")
    if later in ("review", "done"):
        missions.set_state(mid, "running", later)
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, "failed", "planned")
    assert e.value.status == 409
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, later, "planned")
    assert e.value.status == 409
    assert missions.get_mission(mid)["state"] == later


def test_an_ABANDONED_mission_is_refused(store):
    mid, _ = _planned()
    missions.set_state(mid, "planned", "abandoned")
    for frm in ("failed", "abandoned"):
        with pytest.raises(missions.MissionError) as e:
            missions.set_state(mid, frm, "planned")
        assert e.value.status == 409
    assert missions.get_mission(mid)["state"] == "abandoned"


def test_an_ARCHIVED_mission_is_refused(store, monkeypatch):
    mid, _ = _failed(monkeypatch, "not_attempted")
    con = sqlite3.connect(missions._db_path())
    con.execute("UPDATE missions SET archived_at=? WHERE id=?", (time.time(), mid))
    con.commit()
    con.close()
    _refused(mid)


def test_evidence_with_NO_PROPOSAL_is_refused_and_leaves_no_ready_plan(store, monkeypatch):
    """Never `plan_state='ready'` with no plan row: with nothing to restore, refuse."""
    mid, _ = _failed(monkeypatch, "not_attempted")
    before = missions.plan_intent(mid)
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "UPDATE mission_dispatch_evidence SET brief=NULL, engine=NULL WHERE mission_id=?", (mid,)
    )
    con.commit()
    con.close()
    err = _refused(mid)
    assert "plan" in str(err)
    assert missions.plan_intent(mid)["plan_state"] == before["plan_state"]


# ---- the crash window ---------------------------------------------------------------------


def test_a_CRASH_after_the_write_before_the_ack_is_UNKNOWN_and_refused(store, monkeypatch):
    """The process wrote the brief and died before anything was acknowledged or recorded. The
    dispatch record still says what the claim wrote, `unknown`, and recovery must not upgrade it."""
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, KEY, expect_plan=plan["plan_id"])
    # The write started: the seed is claimed, and no ack will ever arrive.
    h = handoff.create_handle(
        source_key="", target_engine="claude", mode="dispatch", seed=BRIEF, cwd="/repo"
    )
    handoff.bind_target(h, KEY)
    assert handoff.claim_seed(KEY) == BRIEF
    assert missions.get_dispatch(mid)["seed_outcome"] == "unknown"
    # …and the process is gone, with its in-memory seed store.
    handoff.reset_for_tests()

    monkeypatch.setattr(headless_dispatch, "store_record_state", lambda *a, **k: "absent")
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    row = missions.get_mission(mid)
    assert row["state"] == "failed"
    assert row["seed_outcome"] == "unknown"
    assert row["retry_eligible"] is False
    meta = _failure_event(mid)["meta"]
    assert meta["seed_outcome"] == "unknown" and meta["retry_eligible"] is False
    assert "nothing was typed" not in meta["message"].lower()
    _refused(mid)


def test_a_claim_still_OUTSTANDING_when_the_launcher_returns_is_unknown(store):
    h = handoff.create_handle(
        source_key="", target_engine="claude", mode="dispatch", seed=BRIEF, cwd="/repo"
    )
    handoff.bind_target(h, KEY)
    assert handoff.claim_seed(KEY) == BRIEF
    assert handoff.retire_seed(KEY) == "unknown"
    # A late ack of that write changes nothing, and nobody can claim the brief again.
    handoff.ack_seed(KEY, "retry")
    assert handoff.claim_seed(KEY) is None


@pytest.mark.parametrize(
    ("acks", "want"),
    [
        ([], "not_attempted"),
        (["retry"], "zero_write"),
        (["retry", "retry"], "zero_write"),
        (["abort"], "partial"),
        (["retry", "abort"], "partial"),
        (["delivered"], "delivered"),
    ],
)
def test_the_seed_store_answers_what_was_WRITTEN(store, acks, want):
    h = handoff.create_handle(
        source_key="", target_engine="claude", mode="dispatch", seed=BRIEF, cwd="/repo"
    )
    handoff.bind_target(h, KEY)
    for a in acks:
        if handoff.claim_seed(KEY) is None:
            break
        handoff.ack_seed(KEY, a)
    assert handoff.retire_seed(KEY) == want
    assert handoff.claim_seed(KEY) is None, "a retired brief could still be typed by a later attach"


def test_a_seed_the_store_no_longer_knows_is_unknown(store):
    assert handoff.retire_seed(KEY) == "unknown"


# ---- the launcher records the evidence ----------------------------------------------------


class _Prov:
    engine_id = "claude"

    # #853 P3: capability answers come from the manifest; a fake carries its engine's real one.
    @property
    def manifest(self):
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].manifest  # the REAL roster: tests patch `get`

    supports_seed_start = True
    new_session_reconciles = False

    def store_root(self):
        # Native-history ownership (#1277) identifies a console source by its store; a fake
        # without one cannot be proved unowned and its launch would (rightly) fail closed.
        from agent_sessions.engines import registry as _registry

        return _registry._BY_ID[self.engine_id].store_root()

    def is_present(self):
        return True

    def scan(self):
        return []

    def new_launch_argv(self, native, *, cwd, bypass):
        return ["/bin/true"]


class _Registry:
    async def ensure_headless(self, engine, sid):
        return None


@pytest.fixture
def launcher(store, monkeypatch):
    import shutil
    import tempfile

    sockdir = tempfile.mkdtemp(prefix="as-sa-")
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", sockdir)
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(store / "locks"))
    prov = _Prov()
    monkeypatch.setattr(headless_dispatch.engines, "get", lambda e: prov if e == "claude" else None)
    monkeypatch.setattr(
        headless_dispatch.engine_auth,
        "check",
        lambda *a, **k: (headless_dispatch.engine_auth.AUTHENTICATED, "stubbed"),
    )
    monkeypatch.setitem(
        headless_dispatch._START_EVIDENCE,
        "claude-sessions",
        lambda n, c, **k: (start_evidence.FOUND, ""),
    )

    class P:
        returncode = 0

        def wait(self):
            return 0

    def spawn(argv, **kw):
        open(argv[list(argv).index("-n") + 1], "w").close()
        return P()

    monkeypatch.setattr(headless_dispatch, "_popen", spawn)
    yield monkeypatch
    shutil.rmtree(sockdir, ignore_errors=True)


def _launch(monkeypatch, deliver, *, started=True):
    if not started:
        monkeypatch.setitem(
            headless_dispatch._START_EVIDENCE,
            "claude-sessions",
            lambda n, c, **k: (start_evidence.ABSENT, "no entry"),
        )
    monkeypatch.setattr(headless_seed, "deliver", deliver)
    return asyncio.run(
        headless_dispatch.dispatch(
            engine="claude", cwd="/tmp", brief=BRIEF, registry=_Registry(), start_timeout=0.05
        )
    )


def test_the_launcher_reports_NOT_ATTEMPTED_when_the_gate_never_opened(launcher):
    async def never_ready(key, seed_key, **kw):
        return False, "the session never became ready (first-paint never true)"

    out = _launch(launcher, never_ready)
    assert not out.ok
    assert (out.seed_outcome, out.teardown) == ("not_attempted", "stopped")


def test_the_launcher_reports_NOT_ATTEMPTED_when_it_never_started(launcher):
    async def boom(key, seed_key, **kw):
        raise AssertionError("the brief must not be offered before start evidence")

    out = _launch(launcher, boom, started=False)
    assert (out.seed_outcome, out.teardown) == ("not_attempted", "stopped")


def test_the_launcher_reports_ZERO_WRITE_for_a_retry_ack(launcher):
    async def zero(key, seed_key, **kw):
        assert handoff.claim_seed(seed_key) is not None
        handoff.ack_seed(seed_key, "retry")
        return False, "the brief was not delivered"

    out = _launch(launcher, zero)
    assert out.seed_outcome == "zero_write"


def test_the_launcher_reports_PARTIAL_for_an_abort_ack(launcher):
    async def half(key, seed_key, **kw):
        assert handoff.claim_seed(seed_key) is not None
        handoff.ack_seed(seed_key, "abort")
        return False, "the brief was not delivered"

    out = _launch(launcher, half)
    assert out.seed_outcome == "partial"


def test_the_launcher_reports_UNKNOWN_for_a_write_whose_ack_never_came(launcher):
    async def lost(key, seed_key, **kw):
        assert handoff.claim_seed(seed_key) is not None
        raise RuntimeError("died mid-write")

    out = _launch(launcher, lost)
    assert out.seed_outcome == "unknown"


def test_the_launcher_does_not_call_a_LEAKED_teardown_confirmed(launcher):
    async def leaked(engine, native, **kw):
        return "leaked"

    launcher.setattr(runtime_cleanup, "cleanup_runtime", leaked)

    async def never_ready(key, seed_key, **kw):
        return False, "never ready"

    out = _launch(launcher, never_ready)
    assert out.seed_outcome == "not_attempted"
    assert out.teardown == "leaked"


# ---- the thread event ---------------------------------------------------------------------


def test_the_failure_event_says_NOTHING_WAS_TYPED_only_on_eligible_evidence(store, monkeypatch):
    mid, _ = _failed(monkeypatch, "not_attempted")
    e = _failure_event(mid)
    meta = e["meta"]
    assert meta["seed_outcome"] == "not_attempted"
    assert meta["teardown_confirmed"] is True
    assert meta["retry_eligible"] is True
    assert "nothing was typed" in meta["message"].lower()
    assert REASON in meta["detail"], "the technical detail rides beside the message"
    assert BRIEF not in json.dumps(e)


@pytest.mark.parametrize("seed", ["partial", "unknown", "delivered"])
def test_the_failure_event_never_claims_nothing_was_typed_otherwise(store, monkeypatch, seed):
    mid, _ = _failed(monkeypatch, seed, briefed=(seed == "delivered"))
    e = _failure_event(mid)
    meta = e["meta"]
    assert meta["seed_outcome"] == seed
    assert meta["retry_eligible"] is False
    assert "nothing was typed" not in meta["message"].lower()
    if seed != "delivered":
        assert "may have been typed" in meta["message"].lower()
    assert BRIEF not in json.dumps(e)


def test_a_start_again_says_so_on_the_timeline(store, monkeypatch):
    mid, _ = _failed(monkeypatch, "zero_write")
    _start_again(mid)
    e = missions.get_mission(mid)["events"][0]
    assert e["kind"] == "state"
    assert e["meta"]["from"] == "failed" and e["meta"]["to"] == "planned"
    assert e["meta"]["seed_outcome"] == "zero_write"
    assert BRIEF not in json.dumps(e)
    # The evidence is consumed: it cannot justify a second reopening.
    assert missions.get_mission(mid)["seed_outcome"] is None


# ---- the fence ----------------------------------------------------------------------------


def test_CONCURRENT_start_again_produces_exactly_ONE_transition(store, monkeypatch):
    mid, _ = _failed(monkeypatch, "not_attempted")
    barrier = threading.Barrier(2, timeout=10)
    results: list[object] = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        try:
            r = asyncio.run(
                mission_fence.fenced_write(
                    mid, lambda: missions.set_state(mid, "failed", "planned")
                )
            )
        except Exception as e:  # noqa: BLE001
            r = e
        with lock:
            results.append(r)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a concurrent start again hung"
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, missions.MissionError)]
    assert len(ok) == 1 and len(refused) == 1, results
    assert refused[0].status == 409
    reopened = [
        e
        for e in missions.get_mission(mid)["events"]
        if e["kind"] == "state" and (e.get("meta") or {}).get("from") == "failed"
    ]
    assert len(reopened) == 1


def test_the_evidence_is_RE_READ_under_the_fence_not_before_it(store, monkeypatch):
    """A verdict read before the write lock is a stale read: the evidence can change between it
    and the commit. The writer waits on the lock; meanwhile the evidence stops being eligible."""
    mid, _ = _failed(monkeypatch, "not_attempted")
    results: list[object] = []
    started = threading.Event()

    def attempt():
        started.set()
        try:
            results.append(missions.set_state(mid, "failed", "planned"))
        except Exception as e:  # noqa: BLE001
            results.append(e)

    with missions._write_lock:
        t = threading.Thread(target=attempt)
        t.start()
        assert started.wait(5)
        time.sleep(0.5)  # the writer is now queued on the lock (or, broken, past a stale read)
        con = sqlite3.connect(missions._db_path(), timeout=5)
        con.execute(
            "UPDATE mission_dispatch_evidence SET seed_outcome='partial' WHERE mission_id=?", (mid,)
        )
        con.commit()
        con.close()
    t.join(timeout=30)
    assert not t.is_alive()
    assert len(results) == 1 and isinstance(results[0], missions.MissionError), results
    assert results[0].status == 409
    assert missions.get_mission(mid)["state"] == "failed"


# ---- the route ----------------------------------------------------------------------------


@pytest.fixture
def api(store, auth_cfg, tmp_home, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(store / "m.db"))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    return c, hdr


def test_the_ROUTE_starts_again_through_the_state_write(api, monkeypatch):
    c, hdr = api
    mid, _ = _failed(monkeypatch, "not_attempted")
    got = c.get(f"/api/missions/{mid}").json()
    assert got["retry_eligible"] is True and got["seed_outcome"] == "not_attempted"
    assert BRIEF not in json.dumps(
        {k: got.get(k) for k in ("retry_eligible", "seed_outcome", "retry_reason")}
    )

    r = c.post(f"/api/missions/{mid}/state", json={"from": "failed", "to": "planned"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "planned"
    after = c.get(f"/api/missions/{mid}").json()
    assert after["retry_eligible"] is False


def test_the_ROUTE_ignores_a_client_supplied_outcome(api, monkeypatch):
    c, hdr = api
    mid, _ = _failed(monkeypatch, "unknown")
    r = c.post(
        f"/api/missions/{mid}/state",
        json={
            "from": "failed",
            "to": "planned",
            "seed_outcome": "not_attempted",
            "teardown_confirmed": True,
            "retry_eligible": True,
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert BRIEF not in r.text
    got = c.get(f"/api/missions/{mid}").json()
    assert got["state"] == "failed" and got["retry_eligible"] is False
    assert got["retry_reason"]


def test_the_ROUTE_requires_auth_and_csrf(api, auth_cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    c, hdr = api
    mid, _ = _failed(monkeypatch, "not_attempted")
    body = {"from": "failed", "to": "planned"}
    anon = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert anon.post(f"/api/missions/{mid}/state", json=body, headers=hdr).status_code == 401
    r = c.post(f"/api/missions/{mid}/state", json=body, headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403
    assert missions.get_mission(mid)["state"] == "failed"


# ---- the schema ---------------------------------------------------------------------------


def test_a_v25_store_UPGRADES_to_the_same_evidence_schema_a_fresh_one_has(tmp_path, monkeypatch):
    import re

    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"])

    def ddl(con, name):
        raw = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", raw)).strip()

    def cols(con, table):
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

    con = sqlite3.connect(db)
    fresh = ddl(con, "mission_dispatch_evidence")
    con.execute("DROP TABLE mission_dispatch_evidence")
    # A real v25 `mission_dispatches`, rebuilt rather than DROP COLUMNed: SQLite's column drop
    # rewrites the stored DDL text and trips over its comment lines.
    v25 = (
        "mission_id, plan_id, engine, cwd, session_key, started_at, project_id, engine_reason, "
        "brief, owner, spawn_parent"
    )
    con.execute("ALTER TABLE mission_dispatches RENAME TO md_v26")
    con.execute(
        "CREATE TABLE mission_dispatches (mission_id TEXT PRIMARY KEY REFERENCES missions(id) "
        "ON DELETE CASCADE, plan_id TEXT NOT NULL, engine TEXT NOT NULL, cwd TEXT NOT NULL, "
        "session_key TEXT, started_at REAL NOT NULL, project_id TEXT, engine_reason TEXT, "
        "brief TEXT, owner TEXT, spawn_parent TEXT)"
    )
    con.execute(f"INSERT INTO mission_dispatches ({v25}) SELECT {v25} FROM md_v26")  # noqa: S608
    con.execute("DROP TABLE md_v26")
    assert not {"seed_outcome", "teardown_confirmed"} & cols(con, "mission_dispatches")
    con.execute("PRAGMA user_version=25")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    d = missions.get_dispatch(mid)
    assert d is not None and d["seed_outcome"] == "unknown" and d["teardown_confirmed"] is False
    con = sqlite3.connect(db)
    assert int(con.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
    assert {"seed_outcome", "teardown_confirmed"} <= cols(con, "mission_dispatches")
    assert ddl(con, "mission_dispatch_evidence") == fresh
    con.close()


# ---- a cancelled request after the launcher returned (PR #980 review, P1) -----------------


def _launch_returns(monkeypatch, *, ok: bool):
    """The launcher came back: briefed and started, or launched and failed having typed nothing."""

    async def fake(**kw):
        kw["on_key"](KEY)
        out = headless_dispatch.Dispatch(key=KEY, engine="claude", native=UUID, cwd=kw["cwd"])
        out.launched = True
        out.started = ok
        out.briefed = ok
        out.reason = "" if ok else REASON
        out.seed_outcome = "delivered" if ok else "not_attempted"
        out.teardown = "" if ok else "stopped"
        return out

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)


def _gate(monkeypatch, owner, name, *, when=None, fail=False):
    """Hold one call to `owner.name` on its worker thread until the test releases it.

    Real Events, and a timeout on every wait, so a broken fix fails rather than hangs.
    """
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    real = getattr(owner, name)

    def gated(*a, **kw):
        if when is not None and not when(kw):
            return real(*a, **kw)
        entered.set()
        try:
            if not release.wait(10):
                raise TimeoutError("the test never released the gate")
            if fail:
                raise OSError("the mission store could not be written")
            return real(*a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(owner, name, gated)
    return entered, release, done


def _gate_at(monkeypatch, step, *, fail=False):
    if step == "evidence":
        return _gate(monkeypatch, missions, "note_dispatch_evidence", fail=fail)
    to = "running" if step == "adoption" else "failed"
    return _gate(monkeypatch, mission_dispatch, "fenced_settle", when=lambda kw: kw.get("to") == to)


def _cancel_inside(mid, claimed, gate):
    """Start `run`, cancel it while the gated step is in its worker, release, and await it."""
    entered, release, done = gate

    async def drive():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(mission_dispatch.run(mid, claimed, registry=object()))
        started = await loop.run_in_executor(None, entered.wait, 10)
        if not started:
            release.set()
        assert started, "the gated step never ran, so nothing was cancelled inside it"
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
        assert await loop.run_in_executor(None, done.wait, 10), "the gated worker never finished"

    asyncio.run(drive())


def _assert_nothing_stranded(mid):
    """Not `dispatching`, no dispatch record under this live process's lease, nothing to recover."""
    assert (
        missions.get_mission(mid)["state"] != "dispatching"
    ), "a cancelled request left the mission dispatching under a live lease with no runner"
    d = missions.get_dispatch(mid)
    assert d is None, (
        f"a dispatch record survived the cancelled request under lease {d and d['owner']}; "
        "recovery skips a live lease, so nothing repairs it while this process runs"
    )
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0


@pytest.mark.parametrize("step", ["evidence", "adoption"])
def test_a_CANCEL_after_a_BRIEFED_launch_still_adopts_it(
    store, monkeypatch, _teardown_proves_empty, step
):
    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _launch_returns(monkeypatch, ok=True)
    _cancel_inside(mid, claimed, _gate_at(monkeypatch, step))

    _assert_nothing_stranded(mid)
    assert missions.get_mission(mid)["state"] == "running"
    assert missions.active_session_keys(mid) == [KEY]
    assert _teardown_proves_empty == [], "a briefed agent the mission adopted was torn down"


@pytest.mark.parametrize("step", ["evidence", "settlement"])
def test_a_CANCEL_after_a_FAILED_launch_still_settles_and_reconciles(
    store, monkeypatch, _teardown_proves_empty, step
):
    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _launch_returns(monkeypatch, ok=False)
    _cancel_inside(mid, claimed, _gate_at(monkeypatch, step))

    _assert_nothing_stranded(mid)
    row = missions.get_mission(mid)
    assert row["state"] == "failed"
    # THE EVIDENCE WAS RECORDED BEFORE THE SETTLEMENT COPIED IT, cancel or not.
    assert row["seed_outcome"] == "not_attempted"
    assert row["retry_eligible"] is True, row.get("retry_reason")
    assert _teardown_proves_empty == [KEY], "the orphan reconciliation never ran"


def test_a_CANCEL_while_the_evidence_write_FAILS_settles_on_unknown(store, monkeypatch):
    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _launch_returns(monkeypatch, ok=False)
    _cancel_inside(mid, claimed, _gate_at(monkeypatch, "evidence", fail=True))

    _assert_nothing_stranded(mid)
    row = missions.get_mission(mid)
    assert row["state"] == "failed"
    assert row["seed_outcome"] == "unknown"
    assert row["retry_eligible"] is False
    _refused(mid)


def test_an_evidence_write_that_FAILS_still_settles_on_unknown(store, monkeypatch):
    def boom(*a, **kw):
        raise OSError("the mission store could not be written")

    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    _launch_returns(monkeypatch, ok=False)
    monkeypatch.setattr(missions, "note_dispatch_evidence", boom)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    _assert_nothing_stranded(mid)
    row = missions.get_mission(mid)
    assert row["seed_outcome"] == "unknown"
    assert row["retry_eligible"] is False
    _refused(mid)


# ---- the failure's session link: display identity, never ownership (#967 P4, PR #986) -------


def _spy_settlements(monkeypatch) -> list:
    """Every `session_key` a settlement was handed. That parameter ADOPTS, so a display link must
    never travel through it."""
    handed: list = []
    real = missions.settle_dispatch

    def spy(*a, **kw):
        handed.append(kw.get("session_key"))
        return real(*a, **kw)

    monkeypatch.setattr(missions, "settle_dispatch", spy)
    return handed


@pytest.mark.parametrize("seed", ["not_attempted", "delivered"])
def test_a_FAILED_launch_names_the_session_it_started_for_DISPLAY_only(store, monkeypatch, seed):
    """The production path: the launcher stamps the key through `on_key`, comes up, and fails, so
    the settlement goes through `_orphaned_after_launch` with no `session_key`. The failure event
    still names the launched session, taken from the dispatch record, and nothing is adopted."""
    handed = _spy_settlements(monkeypatch)
    mid, _ = _failed(monkeypatch, seed, briefed=(seed == "delivered"))

    e = _failure_event(mid)
    assert e["meta"]["launch_session_key"] == KEY
    # DISPLAY IDENTITY, NOT OWNERSHIP.
    assert "session_key" not in e["meta"], "the adoption-shaped key appeared on the failure"
    assert e["session_key"] is None
    assert missions.active_session_keys(mid) == []
    assert missions.get_mission(mid)["sessions"] == [], "the failed launch was adopted"
    assert handed, "the failure never settled through settle_dispatch"
    assert all(k is None for k in handed), f"a settlement was handed a session to adopt: {handed}"
    # The separate `session` event is unchanged and still keyed.
    assert any(
        x["kind"] == "session" and x["session_key"] == KEY
        for x in missions.get_mission(mid)["events"]
    )
    assert BRIEF not in json.dumps(e)


def test_the_SYNC_settlement_shape_used_by_cancel_and_recovery_keeps_the_link(store, monkeypatch):
    """The cancelled branch of `_orphaned_after_launch` and dispatch recovery settle through the
    same store call, on the same dispatch row, with `keep_record=True`."""
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"])
    missions.note_dispatch_session(mid, KEY, expect_plan=plan["plan_id"])
    missions.settle_dispatch(
        mid, to="failed", detail="cancelled", keep_record=True, expect_plan=plan["plan_id"]
    )
    e = _failure_event(mid)
    assert e["meta"]["launch_session_key"] == KEY
    assert missions.active_session_keys(mid) == []


@pytest.mark.parametrize("stamped", ["claude:../../etc", "opencode:new-" + UUID, "not a key"])
def test_a_stamped_value_that_is_not_a_plain_key_builds_NO_link(store, stamped):
    mid, plan = _planned()
    missions.claim_plan(mid, plan["plan_id"])
    missions.note_dispatch_session(mid, stamped, expect_plan=plan["plan_id"])
    missions.settle_dispatch(
        mid, to="failed", detail="x", keep_record=True, expect_plan=plan["plan_id"]
    )
    assert "launch_session_key" not in _failure_event(mid)["meta"]


def test_a_launch_that_failed_BEFORE_minting_a_key_names_NO_session(store, monkeypatch):
    async def boom(**kw):
        raise OSError("the launcher failed before minting an id")

    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", boom)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed" and out["session_key"] is None
    assert "launch_session_key" not in _failure_event(mid)["meta"]
    assert missions.active_session_keys(mid) == []


def test_a_launch_REFUSED_before_anything_ran_records_no_session_link(store, monkeypatch):
    async def refuse(**kw):
        raise headless_dispatch.DispatchError("that engine is not eligible for an unattended start")

    mid, plan = _planned()
    claimed = missions.claim_plan(mid, plan["plan_id"])
    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", refuse)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "planned"
    for e in missions.get_mission(mid)["events"]:
        assert "launch_session_key" not in (e.get("meta") or {}), e
