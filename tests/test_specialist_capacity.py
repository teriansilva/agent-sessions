"""Specialist capacity (#1693 S4a): atomic reservations against a specialist's capacity and the
pool's limit, one per session, released only by the attempt that took them, by reconcile on proof,
or by the operator for a held slot; and reconciliation per session kind, holding the unknown.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, sessionlock, skill_bundles
from agent_sessions import specialist_capacity as cap
from agent_sessions import specialists_store as store


def _body(**over):
    b = {
        "title": "Implementer",
        "summary": "",
        "skills": [],
        "engine": "claude",
        "model": "claude-opus-5",
        "effort": None,
        "capacity": 2,
    }
    b.update(over)
    return b


def _k(label: str) -> str:
    """A valid terminal session key, stable per label."""
    return f"claude:{uuid.uuid5(uuid.NAMESPACE_DNS, label)}"


@pytest.fixture
def spec():
    return store.create(_body())


def _reserve(sid, label, *, n=1, max_instances=6):
    return store.reserve(sid, n, _k(label), max_instances=max_instances)


def _placeholder_engine() -> str:
    """A terminal agent whose new sessions run under a `new-<uuid>` placeholder (asked, never
    named)."""
    for p in engines.all_providers():
        m = p.manifest
        if m and m.identity.kind == "agent" and m.runtime == "pty":
            try:
                engines.parse_key(f"{p.engine_id}:new-{uuid.uuid4()}", allow_new_placeholder=True)
            except Exception:  # noqa: BLE001
                continue
            return p.engine_id
    pytest.skip("no agent with new-session placeholders")


def _real_native(engine: str) -> str:
    for candidate in (str(uuid.uuid4()), "ses_" + "a" * 26, "session_" + str(uuid.uuid4())):
        try:
            engines.parse_key(f"{engine}:{candidate}")
            return candidate
        except Exception:  # noqa: BLE001
            continue
    pytest.skip(f"no valid native id shape found for {engine}")


# ---- reservations -------------------------------------------------------------------------------


def test_a_reservation_takes_a_slot_and_its_session_never_takes_two(spec):
    a = _reserve(spec["id"], "s1")
    assert a["created"] is True and a["owner"] and a["state"] == "starting"
    again = _reserve(spec["id"], "s1")  # a retried create, a double tap, a re-attach
    assert again["created"] is False and "owner" not in again
    assert again["reservation_id"] == a["reservation_id"]
    assert store.active_counts() == ({spec["id"]: 1}, 1)


def test_the_unique_index_itself_refuses_a_second_active_row_for_a_session(spec):
    a = _reserve(spec["id"], "s1")
    con = sqlite3.connect(store.db_path())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        con.execute(
            "INSERT INTO specialist_reservations (reservation_id, specialist_id, version, "
            "session_key, state, owner, process, attempt, rev, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, 'live', 'x', 'p', 1, 1, 1, 1)",
            ("r" + "0" * 24, spec["id"], a["session_key"]),
        )
    con.close()


def test_a_double_taps_second_attempt_can_never_free_the_first_ones_slot(spec):
    winner = _reserve(spec["id"], "s1")
    loser = _reserve(spec["id"], "s1")  # same reservation, no owner token
    assert "owner" not in loser
    with pytest.raises(store.StoreError):
        store.release(loser["reservation_id"], owner="")
    assert store.release(loser["reservation_id"], owner="not-the-token") is False
    # The loser's re-take rotated the token: the winner can't settle it either. Reconcile does,
    # on proof — never an attempt that may not be the one whose session runs.
    assert store.activate(winner["reservation_id"], owner=winner["owner"]) is False
    assert store.release(winner["reservation_id"], owner=winner["owner"]) is False
    assert [r["state"] for r in store.active()] == ["starting"]


def test_a_session_holding_another_specialists_slot_is_refused(spec):
    other = store.create(_body(title="Reviewer"))
    _reserve(spec["id"], "s1")
    with pytest.raises(store.Conflict, match="another specialist"):
        _reserve(other["id"], "s1")


def test_keys_are_canonical_placeholders_included_and_unreservable_kinds_refused(spec):
    bare = str(uuid.uuid4())
    assert store.reserve(spec["id"], 1, bare, max_instances=6)["session_key"] == f"claude:{bare}"
    engine = _placeholder_engine()
    ph = f"{engine}:new-{uuid.uuid4()}"
    assert store.reserve(spec["id"], 1, ph, max_instances=6)["session_key"] == ph
    for bad in ("not a key at all", "", "no-such-engine:x"):
        with pytest.raises(store.StoreError, match="invalid session key"):
            store.reserve(spec["id"], 1, bad, max_instances=6)
    chat = next(
        (
            p.engine_id
            for p in engines.all_providers()
            if p.manifest and p.manifest.runtime == "chat"
        ),
        None,
    )
    if chat is not None:
        key = f"{chat}:{_real_native(chat)}"
        with pytest.raises(store.StoreError, match="liveness can't be checked"):
            store.reserve(spec["id"], 1, key, max_instances=6)


def test_capacity_counts_every_version_against_the_current_limit(spec):
    _reserve(spec["id"], "s1", n=1)
    store.update(spec["id"], _body(capacity=2), expect_revision=1)  # v2
    _reserve(spec["id"], "s2", n=2)
    with pytest.raises(store.CapacityRefused) as e:
        _reserve(spec["id"], "s3", n=2)
    assert e.value.limit == "specialist" and "2 of 2" in str(e.value)


def test_lowering_capacity_stops_nothing_and_refuses_the_next_launch(spec):
    _reserve(spec["id"], "s1")
    _reserve(spec["id"], "s2")
    store.update(spec["id"], _body(capacity=1), expect_revision=1)
    assert store.active_counts()[1] == 2
    with pytest.raises(store.CapacityRefused, match="2 of 1"):
        _reserve(spec["id"], "s3")


def test_the_pool_limit_spans_every_specialist(spec):
    other = store.create(_body(title="Reviewer"))
    _reserve(spec["id"], "s1", max_instances=2)
    _reserve(other["id"], "s2", max_instances=2)
    with pytest.raises(store.CapacityRefused) as e:
        _reserve(other["id"], "s3", max_instances=2)
    assert e.value.limit == "pool"


def test_an_archived_specialist_or_a_missing_version_takes_no_slot(spec):
    with pytest.raises(store.NotFound):
        _reserve(spec["id"], "s1", n=9)
    store.set_archived(spec["id"], True, expect_revision=1)
    with pytest.raises(store.StoreError, match="archived"):
        _reserve(spec["id"], "s1")


def test_two_launches_racing_for_the_last_slot_exactly_one_wins(spec):
    _reserve(spec["id"], "s0")  # capacity 2: one slot left
    barrier = threading.Barrier(8)
    won, lost = [], []

    def go(i):
        barrier.wait()
        try:
            won.append(_reserve(spec["id"], f"race-{i}"))
        except store.CapacityRefused:
            lost.append(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(won) == 1 and len(lost) == 7
    assert store.active_counts()[1] == 2


def test_a_late_release_of_an_earlier_attempt_never_frees_a_newer_reservation(spec):
    first = _reserve(spec["id"], "s1")
    assert store.release(first["reservation_id"], owner=first["owner"]) is True
    second = _reserve(spec["id"], "s1")
    assert store.release(first["reservation_id"], owner=first["owner"]) is False
    assert [r["reservation_id"] for r in store.active()] == [second["reservation_id"]]


def test_a_resume_re_takes_its_slot_and_a_stale_reconcile_cannot_free_it(spec):
    r = _live(spec["id"], "s1")
    seen = store.active()[0]  # reconcile's snapshot
    again = store.reserve(spec["id"], 1, _k("s1"), max_instances=6, now=seen["updated_at"] + 5)
    assert again["state"] == "starting" and again["attempt"] == seen["attempt"] + 1
    assert store.reclaim(r["reservation_id"], seen_rev=seen["rev"]) is False
    assert store.active() != []


def test_a_v1_store_is_backed_up_whole_then_migrated_in_place():
    p = store.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    for stmt in store._SCHEMA.split(";"):
        if stmt.strip():
            con.execute(stmt)
    con.execute(
        "INSERT INTO specialists (id, current_version, revision, archived, created_at, updated_at)"
        " VALUES ('old', 1, 1, 0, 1, 1)"
    )
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    store.reset_schema_cache_for_test()
    s = store.create(_body())
    assert _reserve(s["id"], "s1")["state"] == "starting"
    backups = [b for b in p.parent.iterdir() if b.name.startswith("specialists.db.bak-v1-")]
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    try:  # the backup holds the v1 data, not just a name
        assert copy.execute("SELECT id FROM specialists").fetchall() == [("old",)]
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        copy.close()


# ---- the pool limit -----------------------------------------------------------------------------


def test_the_pool_limit_pref_is_validated_and_defaults_to_six(monkeypatch):
    from agent_sessions import prefs

    assert cap.max_instances() == 6
    assert cap.set_max_instances(3) == 3 and cap.max_instances() == 3
    for bad in (0, 33, True, "4", 2.5, None):
        assert cap.validate_max_instances(bad) is not None
        with pytest.raises(ValueError):
            cap.set_max_instances(bad)
    for stored in (0, 99, "7", True):  # a hand edit: never trusted
        monkeypatch.setattr(prefs, "read_block", lambda key, v=stored: v)
        assert cap.max_instances() == 6


# ---- reconcile ----------------------------------------------------------------------------------


def _verdicts(monkeypatch, table):
    async def fake(key):
        return table[key]

    monkeypatch.setattr(cap, "liveness", fake)


def _live(spec_id, label):
    r = _reserve(spec_id, label)
    assert store.activate(r["reservation_id"], owner=r["owner"])
    return store.get_reservation(r["reservation_id"])


def test_reconcile_releases_only_the_proven_dead_and_holds_the_unknown(spec, monkeypatch):
    other = store.create(_body(title="Reviewer", capacity=4))
    _live(spec["id"], "dead")
    _live(spec["id"], "live")
    _live(other["id"], "unknown")
    _verdicts(
        monkeypatch,
        {
            _k("dead"): (cap.DEAD, "gone"),
            _k("live"): (cap.LIVE, ""),
            _k("unknown"): (cap.UNKNOWN, "probe failed"),
        },
    )
    counts = asyncio.run(cap.reconcile())
    assert (counts["released"], counts["held"], counts["live"]) == (1, 1, 1)
    active = {r["session_key"]: r for r in store.active()}
    assert set(active) == {_k("live"), _k("unknown")}
    assert active[_k("unknown")]["held_reason"] == "probe failed"


def test_a_hold_reason_clears_once_the_session_is_found_live(spec, monkeypatch):
    _live(spec["id"], "s1")
    _verdicts(monkeypatch, {_k("s1"): (cap.UNKNOWN, "probe failed")})
    asyncio.run(cap.reconcile())
    assert store.active()[0]["held_reason"] == "probe failed"
    _verdicts(monkeypatch, {_k("s1"): (cap.LIVE, "")})
    asyncio.run(cap.reconcile())
    assert store.active()[0]["held_reason"] == ""


def test_a_young_starting_reservation_is_left_alone_while_its_process_lives(spec, monkeypatch):
    """This process or a PEER: while the process that took the slot is alive, its launch may not
    have taken its session lock yet — never reclaimed in its grace."""
    r = _reserve(spec["id"], "launching")
    _verdicts(monkeypatch, {_k("launching"): (cap.DEAD, "no lock yet")})
    assert asyncio.run(cap.reconcile())["skipped"] == 1  # this process
    peer = "999999:1:peer"  # another app process mid-launch
    con = sqlite3.connect(store.db_path())
    con.execute("UPDATE specialist_reservations SET process = ?", (peer,))
    con.commit()
    con.close()
    monkeypatch.setattr(
        store, "process_state", lambda token: store.ALIVE if token == peer else store.GONE
    )
    assert asyncio.run(cap.reconcile())["skipped"] == 1
    assert store.active() != []
    later = r["updated_at"] + store.STARTING_GRACE_S + 1
    assert asyncio.run(cap.reconcile(now=later))["released"] == 1


def test_a_starting_reservation_whose_process_is_gone_is_asked_at_once(spec, monkeypatch):
    _reserve(spec["id"], "launched")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    _verdicts(monkeypatch, {_k("launched"): (cap.LIVE, "")})
    asyncio.run(cap.reconcile())
    assert [x["state"] for x in store.active()] == ["live"]


def test_the_loop_retries_after_a_failed_pass(monkeypatch):
    seen = []

    async def fake_reconcile(*, now=None):
        seen.append(now)
        if len(seen) == 1:
            raise RuntimeError("database is locked")
        return {"released": 0, "held": 0, "failed": 0}

    async def fake_sleep(_s):
        if len(seen) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(cap, "reconcile", fake_reconcile)
    monkeypatch.setattr(cap.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("AGENT_SESSIONS_SPECIALIST_LOOP", "1")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cap.run())
    assert len(seen) == 2


def test_an_earlier_attempts_late_failure_never_frees_its_retrys_slot(spec):
    first = _reserve(spec["id"], "s1")
    retry = _reserve(spec["id"], "s1")  # re-taken while the first was still starting
    assert retry["created"] is False
    assert store.release(first["reservation_id"], owner=first["owner"]) is False
    assert store.activate(first["reservation_id"], owner=first["owner"]) is False
    assert len(store.active()) == 1


def test_a_re_take_clears_the_old_hold_so_the_operator_cannot_free_the_new_launch(spec):
    held = _live(spec["id"], "s1")
    assert store.hold(held["reservation_id"], "unknown", seen_rev=held["rev"])
    shown = store.get_reservation(held["reservation_id"])  # what the operator was shown
    again = store.reserve(spec["id"], 1, _k("s1"), max_instances=6, now=shown["updated_at"] + 5)
    assert again["held_reason"] == ""
    assert store.release_held(held["reservation_id"], seen_rev=shown["rev"]) is False
    assert len(store.active()) == 1


def test_a_reconcile_answer_from_before_a_re_take_is_never_applied_to_it(spec):
    r = _live(spec["id"], "s1")
    seen = store.get_reservation(r["reservation_id"])
    store.reserve(spec["id"], 1, _k("s1"), max_instances=6)  # a new attempt
    for settle in (
        lambda: store.mark_live(r["reservation_id"], seen_rev=seen["rev"]),
        lambda: store.hold(r["reservation_id"], "late unknown", seen_rev=seen["rev"]),
        lambda: store.reclaim(r["reservation_id"], seen_rev=seen["rev"]),
    ):
        assert settle() is False
    row = store.get_reservation(r["reservation_id"])
    assert row["state"] == "starting" and row["held_reason"] == "" and row["attempt"] == 2


def test_a_re_take_at_the_very_same_clock_time_is_still_a_new_attempt(spec):
    """The fence is a counter, not a clock: a clock that repeats (or steps back) changes nothing."""
    first = store.reserve(spec["id"], 1, _k("s1"), max_instances=6, now=1000.0)
    store.reserve(spec["id"], 1, _k("s1"), max_instances=6, now=1000.0)  # same instant
    assert store.reclaim(first["reservation_id"], seen_rev=first["rev"]) is False
    second = store.get_reservation(first["reservation_id"])
    store.reserve(spec["id"], 1, _k("s1"), max_instances=6, now=999.0)  # the clock stepped back
    assert store.mark_live(first["reservation_id"], seen_rev=second["rev"]) is False
    assert len(store.active()) == 1


def test_a_stale_dead_answer_never_releases_what_a_newer_live_answer_settled(spec, monkeypatch):
    """Two reconcilers read the same attempt; the LIVE answer lands first, the old DEAD after."""
    r = _live(spec["id"], "s1")
    snapshot = store.get_reservation(r["reservation_id"])
    assert store.mark_live(r["reservation_id"], seen_rev=snapshot["rev"]) is True
    assert store.reclaim(r["reservation_id"], seen_rev=snapshot["rev"]) is False
    assert [x["state"] for x in store.active()] == ["live"]


def test_a_stale_unknown_after_activate_never_makes_a_running_slot_releasable(spec, api):
    """Reconcile reads a starting slot; the launch activates; the old UNKNOWN lands; the operator
    must not be able to free the running session's slot — at the store and at the route."""
    c, hdr = api
    r = _reserve(spec["id"], "s1")
    snapshot = store.get_reservation(r["reservation_id"])
    assert store.activate(r["reservation_id"], owner=r["owner"]) is True
    assert store.hold(r["reservation_id"], "late unknown", seen_rev=snapshot["rev"]) is False
    row = store.get_reservation(r["reservation_id"])
    assert row["held_reason"] == "" and row["state"] == "live"
    assert c.get("/api/specialists").json()["specialists"][0]["held"] == []
    url = f"/api/specialists/reservations/{r['reservation_id']}/release"
    assert c.post(url, json={"rev": row["rev"]}, headers=hdr).status_code == 409
    assert store.release_held(r["reservation_id"], seen_rev=snapshot["rev"]) is False


def test_through_reconcile_a_late_dead_probe_loses_to_a_launch_that_went_live(spec, monkeypatch):
    r = _reserve(spec["id"], "s1")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)  # no grace

    async def dead_after_activate(key):
        store.activate(r["reservation_id"], owner=r["owner"])  # lands while the probe runs
        return cap.DEAD, "gone"

    monkeypatch.setattr(cap, "liveness", dead_after_activate)
    counts = asyncio.run(cap.reconcile())
    assert counts["released"] == 0 and [x["state"] for x in store.active()] == ["live"]


def test_one_reservation_failing_never_stops_the_pass(spec, monkeypatch):
    _live(spec["id"], "a")
    _live(spec["id"], "b")
    _verdicts(monkeypatch, {_k("a"): (cap.DEAD, "gone"), _k("b"): (cap.DEAD, "gone")})
    real = store.reclaim
    calls = []

    def flaky(rid, **kw):
        calls.append(rid)
        if len(calls) == 1:
            raise store.StoreError("database is locked", status=503)
        return real(rid, **kw)

    monkeypatch.setattr(store, "reclaim", flaky)
    counts = asyncio.run(cap.reconcile())
    assert counts["failed"] == 1 and counts["released"] == 1


def _bundle(rid: str):
    from agent_sessions.gallery import library
    from agent_sessions.gallery import skills as lib_skills

    if not lib_skills.storage.skills_root().joinpath("tdd").exists():
        library.create_skill("tdd", {"SKILL.md": "---\nname: TDD\ndescription: x\n---\nbody"})
    ref = {"id": "tdd", "revision": lib_skills.get("tdd")["revision"]}
    return skill_bundles.build(rid, [ref])


def test_a_released_reservations_bundle_goes_and_orphans_are_swept(spec, monkeypatch):
    dead = _live(spec["id"], "dead")
    path = _bundle(dead["reservation_id"])
    orphan = _bundle("r" + "f" * 24)  # its removal failed earlier; no reservation holds it
    _verdicts(monkeypatch, {_k("dead"): (cap.DEAD, "gone")})
    # A fresh orphan is left alone (its launch may be under way)…
    counts = asyncio.run(cap.reconcile())
    assert counts["released"] == 1 and not path.exists() and orphan.exists()
    # …and swept once older than the grace by its own times.
    counts = asyncio.run(cap.reconcile(now=time.time() + store.STARTING_GRACE_S + 1))
    assert counts["bundles"] == 1 and not orphan.exists()


def test_a_launch_publishing_its_bundle_during_a_pass_never_loses_it(spec, monkeypatch):
    """Review 7097: a launch reserves and builds its bundle while a pass runs. Whatever the pass
    read earlier, the bundle survives: a fresh one by its age, an old one by a fresh lookup."""
    monkeypatch.setattr(store, "active", lambda: [])  # the pass's view, read before the launch
    fresh = _reserve(spec["id"], "fresh")
    fresh_path = _bundle(fresh["reservation_id"])
    counts = asyncio.run(cap.reconcile())
    assert counts["bundles"] == 0 and fresh_path.exists()
    # Long past the grace (its own times say old), the row is still active: a fresh lookup keeps it.
    later = time.time() + store.STARTING_GRACE_S + 1
    counts = asyncio.run(cap.reconcile(now=later))
    assert counts["bundles"] == 0 and fresh_path.exists()
    # Released, it goes.
    assert store.release(fresh["reservation_id"], owner=fresh["owner"]) is True
    counts = asyncio.run(cap.reconcile(now=later))
    assert counts["bundles"] == 1 and not fresh_path.exists()


def test_an_unreadable_reservation_never_gets_its_bundle_swept(spec, monkeypatch):
    r = _reserve(spec["id"], "s1")
    path = _bundle(r["reservation_id"])
    store.release(r["reservation_id"], owner=r["owner"])  # the store alone: the bundle stays

    def unreadable(_rid):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "get_reservation", unreadable)
    counts = asyncio.run(cap.reconcile(now=time.time() + store.STARTING_GRACE_S + 1))
    assert counts["bundles"] == 0 and path.exists()


def test_a_failed_launch_releases_its_slot_with_its_bundle(spec):
    r = _reserve(spec["id"], "s1")
    path = _bundle(r["reservation_id"])
    assert cap.release(r["reservation_id"], owner=r["owner"]) is True
    assert not path.exists() and store.active() == []


# ---- liveness, per kind --------------------------------------------------------------------------


def _key_for(runtime: str) -> str:
    for p in engines.all_providers():
        m = p.manifest
        if m and m.identity.kind == "agent" and m.runtime == runtime:
            return f"{p.engine_id}:{_real_native(p.engine_id)}"
    pytest.skip(f"no {runtime} agent registered")


def test_a_terminal_session_is_live_while_its_writer_lock_is_held(monkeypatch):
    key = _key_for("pty")
    assert asyncio.run(cap.liveness(key))[0] == cap.DEAD
    lock = sessionlock.acquire(key)
    try:
        assert asyncio.run(cap.liveness(key))[0] == cap.LIVE  # a master not yet rediscovered
    finally:
        lock.release()

    def broken(_key):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(sessionlock, "is_locked", broken)
    verdict, reason = asyncio.run(cap.liveness(key))
    assert verdict == cap.UNKNOWN and "can't be read" in reason


def test_a_placeholder_session_and_its_reconciled_real_id_ask_the_placeholders_lock():
    from agent_sessions import metadata

    engine = _placeholder_engine()
    placeholder = f"{engine}:new-{uuid.uuid4()}"
    real = f"{engine}:{_real_native(engine)}"
    lock = sessionlock.acquire(placeholder)  # the master runs under the placeholder
    try:
        assert asyncio.run(cap.liveness(placeholder))[0] == cap.LIVE
        metadata.set_alias(placeholder, real)
        assert asyncio.run(cap.liveness(real))[0] == cap.LIVE  # never "dead" for a live session
    finally:
        lock.release()
    assert asyncio.run(cap.liveness(real))[0] == cap.DEAD


@pytest.mark.parametrize(
    "answer, verdict",
    [
        ({"containment": "gone"}, "dead"),
        ({"containment": "live"}, "live"),
        ({"containment": "unknown"}, "unknown"),
    ],
)
def test_an_api_session_is_asked_through_its_own_probe(monkeypatch, answer, verdict):
    from agent_sessions import structured_runtime

    async def probe(key):
        return answer

    monkeypatch.setattr(structured_runtime, "probe", probe)
    assert asyncio.run(cap.liveness(_key_for("api")))[0] == verdict


def test_an_api_probe_that_fails_or_an_unknown_key_is_unknown_never_dead(monkeypatch):
    from agent_sessions import structured_runtime

    async def boom(key):
        raise structured_runtime.StructuredError(503, "the native runtime is unavailable")

    monkeypatch.setattr(structured_runtime, "probe", boom)
    assert asyncio.run(cap.liveness(_key_for("api")))[0] == cap.UNKNOWN
    assert asyncio.run(cap.liveness("no-such-engine:x"))[0] == cap.UNKNOWN


# ---- routes -------------------------------------------------------------------------------------


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


@pytest.fixture
def api(auth_cfg):
    c = _client(auth_cfg)
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    return c, {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}


def test_the_pool_reports_live_counts_and_its_limit_and_the_limit_is_saved_alone(api, spec):
    c, hdr = api
    _reserve(spec["id"], "s1")
    out = c.get("/api/specialists").json()
    assert out["max_instances"] == 6 and out["live_total"] == 1
    assert out["specialists"][0]["live"] == 1
    assert c.get(f"/api/specialists/{spec['id']}").json()["live"] == 1
    assert (
        c.post("/api/prefs", json={"pool_max_instances": 4}, headers=hdr).json()[
            "pool_max_instances"
        ]
        == 4
    )
    assert c.get("/api/specialists").json()["max_instances"] == 4
    before = c.get("/api/config").json()["theme"]
    other = "light" if before != "light" else "dark"
    bad = c.post("/api/prefs", json={"theme": other, "pool_max_instances": 99}, headers=hdr)
    assert bad.status_code == 422
    assert c.get("/api/config").json()["theme"] == before  # nothing half-written


def test_the_operator_releases_a_held_slot_and_only_a_held_one(api, spec):
    c, hdr = api
    held = _live(spec["id"], "held")
    in_use = _live(spec["id"], "in-use")
    reason = "the session's state is unknown"
    store.hold(held["reservation_id"], reason, seen_rev=held["rev"])
    listed = c.get("/api/specialists").json()["specialists"][0]["held"]
    assert [h["reservation_id"] for h in listed] == [held["reservation_id"]]
    assert listed[0]["reason"] == reason
    url = "/api/specialists/reservations/{}/release"
    shown = {"rev": listed[0]["rev"]}
    in_use_url = url.format(in_use["reservation_id"])
    held_url = url.format(held["reservation_id"])
    assert c.post(in_use_url, json={"rev": in_use["rev"]}, headers=hdr).status_code == 409
    assert c.post(held_url, json=shown, headers={"Origin": hdr["Origin"]}).status_code == 403
    assert c.post(held_url, json={}, headers=hdr).status_code == 422
    stale = {"rev": listed[0]["rev"] - 1}  # not the state the operator was shown
    assert c.post(held_url, json=stale, headers=hdr).status_code == 409
    r = c.post(held_url, json=shown, headers=hdr)
    assert r.status_code == 200 and r.json() == {"released": True}
    assert [x["reservation_id"] for x in store.active()] == [in_use["reservation_id"]]
    assert c.post(url.format("bogus"), json=shown, headers=hdr).status_code == 404


def test_the_store_itself_releases_only_a_held_slot_for_the_operator(spec):
    """The route checks too; this is the second line: a slot in use is never the operator's."""
    in_use = _live(spec["id"], "in-use")
    assert store.release_held(in_use["reservation_id"], seen_rev=in_use["rev"]) is False
    assert store.hold(in_use["reservation_id"], "unknown", seen_rev=in_use["rev"])
    held = store.get_reservation(in_use["reservation_id"])  # the state the operator is shown
    assert store.release_held(in_use["reservation_id"], seen_rev=held["rev"]) is True


@pytest.mark.parametrize("why", ["unreadable", "invisible", "other-namespace"])
def test_an_owner_process_that_cant_be_proven_gone_keeps_the_launchs_grace(spec, monkeypatch, why):
    """A `/proc` entry this process can't read or can't see (hidepid, another pid namespace) is
    not proof the launcher died: its launch keeps its slot."""
    import os

    _reserve(spec["id"], "launching")
    own_ns = store._pid_namespace()
    ns = "1" if why == "other-namespace" else own_ns
    token = f"4242:777:{ns}:peer"
    con = sqlite3.connect(store.db_path())
    con.execute("UPDATE specialist_reservations SET process = ?", (token,))
    con.commit()
    con.close()
    # kill(4242, 0) says the pid exists (hidepid hides /proc, not signals); /proc is closed.
    monkeypatch.setattr(store.os, "kill", lambda pid, sig: None)
    real_read = store.Path.read_text

    def closed(self, *a, **k):
        if str(self) == "/proc/4242/stat":
            if why == "unreadable":
                raise PermissionError(13, "hidepid")
            raise FileNotFoundError(2, "hidden")
        return real_read(self, *a, **k)

    monkeypatch.setattr(store.Path, "read_text", closed)
    assert store.process_state(token) == store.UNKNOWN_PROCESS
    _verdicts(monkeypatch, {_k("launching"): (cap.DEAD, "no lock yet")})
    assert asyncio.run(cap.reconcile())["skipped"] == 1
    assert len(store.active()) == 1
    assert os.getpid()  # (keeps the import used)


def test_process_state_is_gone_only_on_proof():
    import os

    own = store._process()
    assert store.process_state(own) == store.ALIVE
    me = os.getpid()
    start = store._proc_start(me)
    ns = store._pid_namespace()
    assert store.process_state(f"{me}:{start}:{ns}:other-instance") == store.ALIVE
    assert store.process_state(f"{me}:1:{ns}:reused") == store.GONE  # same pid, another start
    assert store.process_state(f"2147483646:1:{ns}:x") == store.GONE  # no such pid, our namespace
    assert store.process_state("2147483646:1:1:x") == store.UNKNOWN_PROCESS  # another namespace
    for bad in ("", "garbage", f"0:1:{ns}:x", f"12:?:{ns}:x", "12:3:?:x", "1:2:3"):
        assert store.process_state(bad) == store.UNKNOWN_PROCESS


@pytest.mark.parametrize("attempt", [10**400, -(10**400), 0, 2**31, "1", True, None, 1.5, 1e300])
def test_a_malformed_rev_is_a_422_never_a_crash(api, spec, attempt):
    import json as _json

    c, hdr = api
    held = _live(spec["id"], "held")
    url = f"/api/specialists/reservations/{held['reservation_id']}/release"
    body = _json.dumps({"rev": attempt})
    r = c.post(url, content=body, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_a_nan_attempt_is_a_422(api, spec):
    c, hdr = api
    held = _live(spec["id"], "held")
    url = f"/api/specialists/reservations/{held['reservation_id']}/release"
    r = c.post(url, content='{"rev": NaN}', headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_an_archived_specialist_s_held_slot_is_never_re_taken(spec):
    """Review 7100: archiving stops every new attempt — a re-take of a HELD slot as much as a new
    reservation after its slot was released."""
    held = _reserve(spec["id"], "held")
    released = _reserve(spec["id"], "released")
    assert store.release(released["reservation_id"], owner=released["owner"])
    store.set_archived(spec["id"], True, expect_revision=spec["revision"])
    before = store.get_reservation(held["reservation_id"])
    with pytest.raises(store.StoreError, match="archived"):
        _reserve(spec["id"], "held")
    assert store.get_reservation(held["reservation_id"]) == before  # nothing rotated
    with pytest.raises(store.StoreError, match="archived"):
        _reserve(spec["id"], "released")
