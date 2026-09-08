"""Mission routes + the bounded DB worker (#846, Phase 1 of #840).

Two things are pinned here that the store tests cannot reach: the **route contract** (auth, CSRF,
status codes, filter-before-paginate) and the **admission bound** — the part that makes "off the
event loop" an honest claim rather than a comment. A bounded pool bounds running threads, not the
queue; without admission above it a flood is experienced as a hang, and with a five-second SQLite
``busy_timeout`` that hang is minutes long.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, missions, orchestrator_ledger, projects
from agent_sessions.main import create_app

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
CLAUDE_B = "claude:22222222-2222-2222-2222-222222222222"


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
    return c.get("/api/config").json()["csrf"]


@pytest.fixture
def api(auth_cfg, tmp_home, tmp_path, fake_jsonl):
    """A logged-in client, a real project entity — and REAL SESSIONS on disk.

    `fake_jsonl` is not decoration: adoption now proves the session EXISTS, not merely that its
    key is well formed (#896 review 11, finding 1), and `CLAUDE_A` is one of the transcripts that
    fixture writes. Without it every adopt in this module would 404 — which is the correct answer
    for a key naming nothing, and exactly the hole the check closes."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # A real project entity: `cwd` is resolved from one server-side and is rejected as route
    # input, so a route test that wants a launchable mission has to go through the entity — which
    # is the point of the rule.
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    proj = projects.create("repo", folders=[str(repo)], default_folder=str(repo))
    return c, hdr, proj


def _set_objectives_state(mission_id, state):
    """Put the producer flag back into a NON-terminal state, which `settle_objectives_state`
    deliberately refuses to do — it is a settler, not a setter."""
    import sqlite3

    con = sqlite3.connect(missions._db_path())
    try:
        con.execute("UPDATE missions SET objectives_state=? WHERE id=?", (state, mission_id))
        con.commit()
    finally:
        con.close()


def _ready_objectives(mission_id, *, title="A PR is open"):
    """Give a mission a finalized, non-empty checklist — the state DISPATCH requires (#904 rev 4,
    finding 3). A fresh mission has `objectives_state='pending'` and no rows, which is not
    dispatchable, so every route test that expects a launch has to establish what done means
    first. That is the invariant, not test scaffolding."""
    missions.patch_objectives(
        mission_id, [{"op": "add", "key": "pr", "title": title, "gate": True}]
    )
    missions.settle_objectives_state(mission_id, "done")


def _obj_digest(mission_id):
    """The checklist digest a DISPATCH has to name (#904 review 3, finding 5) — computed the way
    the card does, from the mission's own rows."""
    return missions.objectives_digest(missions.get_mission(mission_id)["objectives"])


def _create(c, hdr, instruction="do the thing", **kw):
    r = c.post("/api/missions", json={"instruction": instruction, **kw}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


# ---- auth + CSRF -----------------------------------------------------------------


def test_every_route_requires_a_session(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    assert c.get("/api/missions").status_code == 401
    assert c.post("/api/missions", json={"instruction": "x"}).status_code == 401


def test_state_changing_routes_require_csrf(api, auth_cfg):
    c, hdr, proj = api
    m = _create(c, hdr)
    for path, method in (
        ("/api/missions", "post"),
        (f"/api/missions/{m['id']}/adopt", "post"),
        (f"/api/missions/{m['id']}/state", "post"),
        (f"/api/missions/{m['id']}/archive", "post"),
        (f"/api/missions/{m['id']}/objectives", "patch"),
    ):
        r = getattr(c, method)(path, json={}, headers={"Origin": auth_cfg.origin})
        assert r.status_code == 403, f"{method} {path} accepted a request with no CSRF token"


# ---- create + read ---------------------------------------------------------------


def test_create_returns_a_draft_with_no_cwd(api):
    c, hdr, proj = api
    m = _create(c, hdr, "implement issue #813 in abc")
    assert m["state"] == "draft" and m["cwd"] is None
    assert m["instruction"] == "implement issue #813 in abc"
    # The instruction is echoed to the OPERATOR over an authenticated route — that is the one
    # place it is allowed to appear.
    assert missions.MISSION_ID_RE.match(m["id"])


def test_an_empty_instruction_is_a_422(api):
    c, hdr, proj = api
    assert c.post("/api/missions", json={"instruction": "  "}, headers=hdr).status_code == 422


def test_a_malformed_mission_id_is_a_404_not_a_500(api):
    c, _hdr, _proj = api
    assert c.get("/api/missions/not-a-mission").status_code == 404
    assert c.get("/api/missions/msn_zzzz").status_code == 404


def test_get_returns_the_roster_objectives_and_a_bounded_timeline(api):
    c, hdr, proj = api
    m = _create(c, hdr)
    row = c.get(f"/api/missions/{m['id']}").json()
    assert row["sessions"] == [] and row["objectives"] == []
    # Two events, not one: creating a mission now runs the objective producer (#883), and this
    # test env has no AI endpoint, so it records WHY the checklist is empty rather than leaving
    # an empty list indistinguishable from a broken feature. Once, at creation — a fixed fact
    # about an install repeated on every poll would be noise rather than information.
    assert [e["kind"] for e in row["events"]] == ["objective", "operator_msg"]
    assert "no AI endpoint is configured" in (row["events"][0].get("text") or "")
    assert row["needs_you"] is False


# ---- filter before paginate --------------------------------------------------------


def test_the_filter_applies_before_the_page(api):
    c, hdr, proj = api
    for i in range(6):
        _create(c, hdr, f"m{i}", title=f"alpha {i}" if i < 2 else f"beta {i}")
    d = c.get("/api/missions?q=alpha&limit=1").json()
    assert d["total"] == 2 and len(d["missions"]) == 1


def test_facets_list_every_option_regardless_of_the_current_filter(api, tmp_path):
    c, hdr, proj = api
    other = tmp_path / "other"
    other.mkdir()
    proj2 = projects.create("other", folders=[str(other)], default_folder=str(other))
    _create(c, hdr, "a", project_id=proj.id)
    _create(c, hdr, "b", project_id=proj2.id)
    d = c.get(f"/api/missions?project={proj.id}").json()
    assert d["total"] == 1
    # Both options stay listed regardless of the current filter — the dropdown must not collapse
    # to the thing already selected.
    assert d["facets"]["projects"] == sorted([proj.id, proj2.id])


def test_archived_is_a_separate_scope(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "draft", "to": "abandoned", "outcome": "abandoned"},
        headers=hdr,
    )
    assert c.post(f"/api/missions/{m['id']}/archive", json={}, headers=hdr).status_code == 200
    assert c.get("/api/missions").json()["total"] == 0
    assert c.get("/api/missions?archived=1").json()["total"] == 1


# ---- adopt / detach ----------------------------------------------------------------


def test_adopting_a_held_session_is_a_409_naming_the_holder(api):
    c, hdr, proj = api
    a = _create(c, hdr, project_id=proj.id)
    b = _create(c, hdr, project_id=proj.id)
    assert (
        c.post(f"/api/missions/{a['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    ).status_code == 200
    r = c.post(f"/api/missions/{b['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 409
    assert a["id"] in r.json()["detail"]


def test_a_session_key_is_shape_checked_before_it_reaches_the_store(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    for bad in ("", "claude:not-a-uuid", "nosuchengine:abc", "../../etc/passwd"):
        r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": bad}, headers=hdr)
        assert r.status_code in (404, 422), f"{bad!r} was accepted"


def test_a_bare_uuid_is_canonicalised_so_it_cannot_become_a_second_row(api):
    """`engines.parse_key` accepts a bare Claude UUID for back-compat. If the store held both
    spellings they would be two rows racing for one partial-unique-index slot."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    bare = CLAUDE_A.split(":", 1)[1]
    c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": bare}, headers=hdr)
    row = c.get(f"/api/missions/{m['id']}").json()
    assert [s["session_key"] for s in row["sessions"]] == [CLAUDE_A]


def test_detach_releases_the_session(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    r = c.post(f"/api/missions/{m['id']}/detach", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 200
    assert missions.holder_of(CLAUDE_A) is None


# ---- state -------------------------------------------------------------------------


def test_state_is_compare_and_set_and_a_lost_race_is_a_409(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    assert (
        c.post(
            f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "planned"}, headers=hdr
        ).status_code
        == 200
    )
    r = c.post(
        f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "abandoned"}, headers=hdr
    )
    assert r.status_code == 409 and "no longer draft" in r.json()["detail"]


def test_an_illegal_transition_is_a_409(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    r = c.post(f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "done"}, headers=hdr)
    assert r.status_code == 409


# ---- archive -----------------------------------------------------------------------


def test_archiving_a_live_mission_is_a_409(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    r = c.post(f"/api/missions/{m['id']}/archive", json={}, headers=hdr)
    assert r.status_code == 409 and "abandon" in r.json()["detail"]


# ---- objectives ---------------------------------------------------------------------


def test_objectives_round_trip_through_the_route(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    r = c.patch(
        f"/api/missions/{m['id']}/objectives",
        json={"ops": [{"op": "add", "key": "pr_open", "title": "PR opened", "gate": True}]},
        headers=hdr,
    )
    assert r.status_code == 200
    assert [o["key"] for o in r.json()["objectives"]] == ["pr_open"]
    assert c.get(f"/api/missions/{m['id']}/objectives").json()["objectives"][0]["gate"] is True


def test_the_route_refuses_to_set_a_met_state(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    r = c.patch(
        f"/api/missions/{m['id']}/objectives",
        json={"ops": [{"op": "add", "key": "x", "title": "x", "state": "met"}]},
        headers=hdr,
    )
    assert r.status_code == 422
    assert "server observation" in r.json()["detail"]


def test_the_route_refuses_a_gating_agent_judged_objective(api):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    r = c.patch(
        f"/api/missions/{m['id']}/objectives",
        json={
            "ops": [{"op": "add", "key": "t", "title": "t", "gate": True, "probe": "agent_judged"}]
        },
        headers=hdr,
    )
    assert r.status_code == 422


# ---- sensitive operator data ----------------------------------------------------------


def test_an_error_body_never_carries_the_instruction(api, caplog):
    """`instruction` may contain anything the operator typed, including a token."""
    c, hdr, proj = api
    secret = "sk-do-not-leak-this-anywhere"  # noqa: S105 — test fixture value
    m = _create(c, hdr, secret, project_id=proj.id)
    r = c.post(
        f"/api/missions/{m['id']}/state", json={"from": "running", "to": "done"}, headers=hdr
    )
    assert r.status_code == 409
    assert secret not in r.text
    assert secret not in caplog.text


def test_nothing_logs_the_instruction(api, caplog):
    import logging

    c, hdr, proj = api
    secret = "sk-also-do-not-leak"  # noqa: S105 — test fixture value
    with caplog.at_level(logging.DEBUG):
        m = _create(c, hdr, secret, project_id=proj.id)
        c.get("/api/missions")
        c.get(f"/api/missions/{m['id']}")
        c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert secret not in caplog.text


def test_deleting_a_mission_removes_the_bytes_from_every_store_file(api):
    """The property has to be PROVEN, not inherited from whichever libsqlite3 is linked.

    The first version of this test asserted the plaintext was absent from `missions.db` and
    passed — on this host. Debian/Ubuntu compile SQLite with `SQLITE_SECURE_DELETE=1`, so the
    freed page was zeroed by the *build*, not by anything in this code; on a build without it the
    operator's instruction survived every delete, and CI (same host) could never see it. The
    review caught it on a different build.

    So: assert the pragma is on, force the row out of the WAL first (otherwise `missions.db` is
    checked while the bytes live in `-wal` and the assertion means nothing), and check every file
    the store owns.
    """
    c, hdr, proj = api
    secret = "sk-gone-for-good-" + "z" * 64  # noqa: S105 — test fixture value
    m = _create(c, hdr, secret, project_id=proj.id)
    for i in range(30):  # filler, so the victim's page is neither the whole file nor reused
        _create(c, hdr, f"filler {i} " + "x" * 200, project_id=proj.id)

    db = missions._db_path()
    con = missions._ready()
    try:
        assert (
            con.execute("PRAGMA secure_delete").fetchone()[0] == 1
        ), "secure_delete is a build option, not a promise — it must be set explicitly"
        con.execute("PRAGMA wal_checkpoint(FULL)")  # the bytes are now genuinely in the db file
    finally:
        con.close()
    assert secret.encode() in db.read_bytes(), "the fixture did not land where it is checked"

    assert missions.delete_mission(m["id"]) is True
    assert missions.get_mission(m["id"]) is None
    for suffix in ("", "-wal", "-shm"):
        f = db.with_name(db.name + suffix)
        if f.exists():
            assert secret.encode() not in f.read_bytes(), f"plaintext survived in {f.name}"


def test_a_destructive_flag_needs_a_real_boolean(api):
    """`bool("false")` is True. On the flag that authorises abandoning a live mission and
    terminating its agents, that is an authorisation bug reachable from any client that
    stringifies its JSON — not a type nit."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    for bad in ("false", "true", 0, 1, [], {}):
        r = c.post(f"/api/missions/{m['id']}/archive", json={"abandon": bad}, headers=hdr)
        assert r.status_code == 422, f"abandon={bad!r} was accepted"
        assert "true or false" in r.json()["detail"]
    # …and the mission is untouched: still live, still not archived.
    assert c.get(f"/api/missions/{m['id']}").json()["state"] == "draft"


def test_the_route_will_not_take_a_cwd_from_the_client(api):
    """`cwd` is the one field where getting it wrong is a path-traversal bug rather than a wrong
    link, so #840 §4 makes it the server's to author. Refused outright rather than ignored, so a
    client written against the old shape finds out instead of silently launching elsewhere."""
    c, hdr, proj = api
    for bad in ("/etc", "../../etc/passwd", ""):
        r = c.post("/api/missions", json={"instruction": "x", "cwd": bad}, headers=hdr)
        assert r.status_code == 422
        assert "resolved from the project server-side" in r.json()["detail"]


def test_cwd_comes_from_the_project_entity(api, tmp_path):
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    assert m["cwd"] == str(tmp_path / "repo")
    assert m["project_id"] == proj.id
    # An unknown project is a 404, and a mission with no project is a draft with no path — which
    # is the state in which the console asks which project was meant.
    assert (
        c.post("/api/missions", json={"instruction": "x", "project_id": "p-nope"}, headers=hdr)
    ).status_code == 404
    assert _create(c, hdr, "no project yet")["cwd"] is None


def test_objectives_on_a_missing_mission_is_a_404_not_an_empty_list(api):
    c, _hdr, _proj = api
    r = c.get(f"/api/missions/msn_{'a' * 32}/objectives")
    assert r.status_code == 404


# ---- the admission bound ----------------------------------------------------------------

# A bounded pool bounds RUNNING THREADS, not the queue: `ThreadPoolExecutor` accepts unlimited
# submissions and parks them behind its workers. With a five-second SQLite `busy_timeout` per
# contended write, that queue is experienced as a hang rather than as an answer — so admission is
# counted above the executor and equals the pool size. These three tests are the proof.


@pytest.fixture(autouse=True)
def _fresh_pool():
    missions.shutdown_executor_for_test()
    yield
    missions.shutdown_executor_for_test()
    assert missions.inflight_for_test() == 0, "a slot leaked"


def _occupy_pool_bypassing_admission(gate):
    """Fill every worker thread WITHOUT taking admission slots.

    Necessary to make a submission actually queue: because admission equals the pool size, the
    ordinary path can never queue — which is the design, and also why the pre-start cancellation
    has to be provoked from outside it.
    """
    pool = missions.executor()
    busy = [threading.Event() for _ in range(missions.MISSIONS_DB_WORKERS)]

    def _park(ev):
        ev.set()
        gate.wait(10)

    futures = [pool.submit(_park, ev) for ev in busy]
    for ev in busy:
        assert ev.wait(5), "the pool never filled"
    return futures


def test_a_flood_past_the_bound_is_a_503_not_a_hang():
    gate = threading.Event()
    ran: list[str] = []

    async def _drive():
        held = [
            asyncio.ensure_future(missions.run_admitted(lambda: gate.wait(10)))
            for _ in range(missions.MISSIONS_DB_MAX_INFLIGHT)
        ]
        await asyncio.sleep(0.1)
        assert missions.inflight_for_test() == missions.MISSIONS_DB_MAX_INFLIGHT
        with pytest.raises(missions.MissionsBusy) as e:
            await missions.run_admitted(lambda: ran.append("should not run"))
        assert e.value.status == 503
        assert "busy" in str(e.value)
        gate.set()
        await asyncio.gather(*held)

    try:
        asyncio.run(_drive())
    finally:
        gate.set()
    # Refused, not queued: the work never ran at all, and the bound is free again afterwards.
    assert ran == []
    assert missions.inflight_for_test() == 0


def test_a_cancel_before_the_worker_starts_leaks_no_slot():
    """The queued callable is dropped, so nothing in a worker can ever release — without the
    `cf.cancelled()` branch the slot leaks for the life of the process, and N of them kill the
    store until restart."""
    gate = threading.Event()
    ran: list[str] = []

    async def _drive():
        parked = _occupy_pool_bypassing_admission(gate)
        queued = asyncio.ensure_future(missions.run_admitted(lambda: ran.append("never")))
        await asyncio.sleep(0.1)
        assert missions.inflight_for_test() == 1  # admitted…
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert ran == []  # …but never started
        gate.set()
        await asyncio.to_thread(lambda: [f.result(5) for f in parked])

    try:
        asyncio.run(_drive())
    finally:
        gate.set()
    assert missions.inflight_for_test() == 0


def test_a_cancel_after_the_worker_starts_does_not_hand_the_slot_back_early():
    """The other direction, and the one a plain `finally: release()` gets wrong: the thread is
    still running, so the slot may NOT come back yet — otherwise occupancy drops to zero with the
    worker blocked and the bound admits more work than it names."""
    started, finish = threading.Event(), threading.Event()

    def _work():
        started.set()
        finish.wait(10)
        return "done"

    async def _drive():
        task = asyncio.ensure_future(missions.run_admitted(_work))
        assert await asyncio.to_thread(started.wait, 5)
        assert missions.inflight_for_test() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Still occupied — the worker owns the slot until its callable exits.
        assert missions.inflight_for_test() == 1
        finish.set()
        for _ in range(100):
            if missions.inflight_for_test() == 0:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the worker never released its slot")

    try:
        asyncio.run(_drive())
    finally:
        finish.set()


def test_the_pool_is_the_module_s_own_not_the_shared_default_executor():
    """`asyncio.to_thread` would put five-second SQLite waits on the interpreter's default pool,
    where an unbounded queue of them starves the file panel and every other off-loop call — and
    where the admission counter would be bounding one thing while a larger pool executed the
    work."""
    assert missions.executor()._max_workers == missions.MISSIONS_DB_WORKERS
    assert missions.MISSIONS_DB_MAX_INFLIGHT == missions.MISSIONS_DB_WORKERS

    async def _name():
        return await missions.run_admitted(lambda: threading.current_thread().name)

    assert asyncio.run(_name()).startswith("missions-db")


def test_a_malformed_field_type_is_a_422_never_a_500(api):
    """`x in frozenset` raises `TypeError: unhashable type` for a dict or list, which escapes as a
    500. And a *falsy* malformed value must not be quietly coerced into the default either —
    `body.get("role") or "primary"` turned `{}` into a successful adopt."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    for bad in ({}, [], 0, {"a": 1}, ["sub"]):
        r = c.post(
            f"/api/missions/{m['id']}/adopt",
            json={"session_key": CLAUDE_A, "role": bad},
            headers=hdr,
        )
        assert r.status_code == 422, f"role={bad!r} was accepted"
        assert "must be a string" in r.json()["detail"]
    for bad in ({}, [], 7):
        r = c.post(
            f"/api/missions/{m['id']}/state",
            json={"from": "draft", "to": "abandoned", "outcome": bad},
            headers=hdr,
        )
        assert r.status_code == 422, f"outcome={bad!r} was accepted"
    # …and an omitted role still defaults.
    assert (
        c.post(
            f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr
        ).status_code
        == 200
    )


def test_malformed_json_is_a_422_and_never_the_destructive_default(api):
    """Swallowing the parse error and returning `{}` made malformed JSON **fail open into the
    defaults** — and on this surface the defaults are the effectful ones. A truncated
    `{"sessions":false` parsed as nothing, defaulted `sessions` to True, and restored every
    session: the opposite of what the caller wrote, at 200."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    for bad in ('{"sessions":false', "not json at all", "[1,2,3]", '{"a":'):
        r = c.post(
            f"/api/missions/{m['id']}/unarchive",
            content=bad,
            headers={**hdr, "Content-Type": "application/json"},
        )
        assert r.status_code == 422, f"{bad!r} was accepted"
        assert "JSON" in r.json()["detail"]
    # The mission is untouched — the refusal happens before any mutation.
    assert c.get(f"/api/missions/{m['id']}").json()["archived_at"] is None
    # …and an ABSENT body is still legitimately empty, because several routes take none.
    r = c.post(f"/api/missions/{m['id']}/archive", content=b"", headers=hdr)
    assert r.status_code == 409  # reached the store: "live mission, needs an explicit abandon"


def test_a_malformed_page_cursor_is_a_422_not_a_silent_reset(api):
    """ "No cursor" and "bad cursor" are different answers. Collapsing them meant a client paging
    with a corrupt cursor silently got page one, which reads as the timeline having reset."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    for bad in ("abc", "", "1.5", "9e9x"):
        r = c.get(f"/api/missions/{m['id']}?events_before_seq={bad}")
        assert r.status_code == 422, f"cursor {bad!r} was accepted"
        assert "events_before_seq" in r.json()["detail"]
    # An omitted cursor is still the first page.
    assert c.get(f"/api/missions/{m['id']}").status_code == 200


def test_the_session_routes_refuse_to_archive_a_mission_owned_session(api):
    """Reproduced in review: the standalone archive route landed a provider effect inside a
    mission's teardown, and the mission then recorded "restored" over an archived session."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)

    uuid = CLAUDE_A.split(":", 1)[1]
    r = c.post(f"/api/sessions/{CLAUDE_A}/archive", headers=hdr)
    assert r.status_code == 409
    assert m["id"] in r.json()["detail"] and "is using it" in r.json()["detail"]

    r = c.post(f"/api/sessions/{CLAUDE_A}/unarchive", headers=hdr)
    assert r.status_code == 409

    # Detaching releases the claim, and the ordinary route works again.
    c.post(f"/api/missions/{m['id']}/detach", json={"session_key": CLAUDE_A}, headers=hdr)
    r = c.post(f"/api/sessions/{CLAUDE_A}/archive", headers=hdr)
    assert r.status_code != 409, f"still guarded after detach: {r.text}"
    assert uuid  # the id round-tripped through the route


# ---- "Stop telling me" (#885) ---------------------------------------------------------------


def _with_objective(c, hdr, proj, key="checks_green"):
    m = _create(c, hdr, project_id=proj.id)
    missions.instantiate_objectives(
        m["id"],
        [
            {
                "key": key,
                "title": "Checks are green",
                "probe": "forge_checks",
                "gate": True,
                "source": "playbook",
            }
        ],
    )
    return m["id"]


def test_a_stand_down_silences_WITHOUT_settling(api):
    """It silences; it does not settle.

    An operator's annoyance is not evidence about the work — a stand-down that marked something
    met would turn "leave me alone" into a false claim about the mission.
    """
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    r = c.post(
        f"/api/missions/{mid}/objectives/checks_green/stand-down", json={"episode": 1}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"episode": 1, "stood_down": True}

    rows = c.get(f"/api/missions/{mid}/objectives", headers=hdr).json()["objectives"]
    assert [o["state"] for o in rows] == ["pending"], "a stand-down settled the objective"


def test_a_STALE_stand_down_is_a_409_not_a_silent_no_op(api):
    """The board the operator tapped was rendered at a particular episode.

    If the objective moved since, their tap is about a situation that no longer exists, and
    silencing the NEW episode would suppress a report nobody has seen. A 409 lets the console
    re-render instead of quietly doing nothing.
    """
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    missions.bump_episode(mid, "checks_green")  # the objective moved

    r = c.post(
        f"/api/missions/{mid}/objectives/checks_green/stand-down", json={"episode": 1}, headers=hdr
    )
    assert r.status_code == 409, r.text
    assert r.json()["episode"] == 2
    _, stood_down = missions.objective_episode(mid, "checks_green")
    assert stood_down is False, "a stale tap silenced the new episode"


def test_the_episode_is_REQUIRED_and_must_be_an_integer(api):
    """Not defaulted to "whatever is current" — defaulting is what makes a stale tap silent."""
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    for body in ({}, {"episode": "1"}, {"episode": True}, {"episode": 0}, {"episode": -1}):
        r = c.post(
            f"/api/missions/{mid}/objectives/checks_green/stand-down", json=body, headers=hdr
        )
        assert r.status_code == 422, f"{body} was accepted: {r.text}"


def test_the_stand_down_route_requires_csrf(api, auth_cfg):
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    r = c.post(
        f"/api/missions/{mid}/objectives/checks_green/stand-down",
        json={"episode": 1},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_the_mission_detail_carries_the_SUPERVISOR_reading(api):
    """Derived at read time like `needs_you`, for the same reason: it is a projection of the
    ledger and the objective store, so a cached copy could disagree with both."""
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    # HOLDING A SESSION, because a nudge is a write into one: `may_nudge` refuses a mission with
    # none, and a board promising READY over nothing to write to is the bug that added the rule
    # (#896 review 7, finding 2).
    missions.adopt(mid, CLAUDE_A)
    row = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert "supervisor" in row, "the console has nothing to render the boards from"
    sup = row["supervisor"]
    assert [o["key"] for o in sup["objectives"]] == ["checks_green"]
    o = sup["objectives"][0]
    assert o["episode"] == 1 and o["remaining"] > 0 and o["may_nudge"] is True
    assert sup["likely_done"] is False and sup["unmet_gates"] == 1
    assert sup["no_session"] is False and sup["held_sessions"] == 1


def test_a_mission_with_NO_SESSION_says_so_instead_of_offering_READY(api):
    """Releasing the last session leaves a `running` mission with nothing to supervise.

    Neither obvious repair is right — refusing the detach takes away an ordinary operator act,
    and forcing `planned` takes away the mission's ability to be closed — so the state stays and
    the READING tells the truth. `may_nudge` refuses, because a nudge is a write into a session
    and there is none, and the board has the discriminator to explain it (#896 review 7).
    """
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    missions.adopt(mid, CLAUDE_A)
    missions.detach(mid, CLAUDE_A)

    sup = c.get(f"/api/missions/{mid}", headers=hdr).json()["supervisor"]
    assert sup["no_session"] is True and sup["held_sessions"] == 0
    o = sup["objectives"][0]
    assert o["may_nudge"] is False, "READY over a mission with nothing to write to"
    assert "no session" in o["why_not"], o["why_not"]


def test_the_page_still_RENDERS_when_the_supervisor_reading_fails(api, monkeypatch):
    """A mission's page must not 500 because the ledger would not open.

    The console shows nothing rather than something wrong — the same posture
    `MissionObjectives` already takes for a probe that could not run.
    """
    from agent_sessions import mission_supervisor

    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)

    def boom(*a, **k):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(mission_supervisor, "assess", boom)
    r = c.get(f"/api/missions/{mid}", headers=hdr)
    assert r.status_code == 200, r.text
    assert "supervisor" not in r.json(), "a failed reading was reported as an empty one"


# ---- the adopted path, through the route (#889) -----------------------------------


def test_an_adopted_mission_can_be_marked_running_through_the_route(api):
    """`planned -> running` end to end: create with a project, adopt, begin.

    The console's whole "start and steer" story runs through these four calls, and the reason it
    is asserted at the ROUTE rather than only at the store is the fence: `set_state` into a
    terminal state is wrapped in `_fenced_write` over the mission's held sessions, and a
    non-terminal transition has to pass through the same wrapper without tripping it.
    """
    c, hdr, proj = api
    m = _create(c, hdr, "track what I started", project_id=proj.id)
    assert m["cwd"], "a project-backed mission resolves its cwd server-side"

    assert (
        c.post(
            f"/api/missions/{m['id']}/state",
            json={"from": "draft", "to": "planned"},
            headers=hdr,
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/api/missions/{m['id']}/adopt",
            json={"session_key": CLAUDE_A},
            headers=hdr,
        ).status_code
        == 200
    )
    r = c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "planned", "to": "running"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"


def test_marking_running_without_a_session_is_a_409_that_says_what_to_do(api):
    """The refusal names the fix. "409" alone leaves the operator with a dead button."""
    c, hdr, proj = api
    m = _create(c, hdr, "nothing adopted", project_id=proj.id)
    c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "draft", "to": "planned"},
        headers=hdr,
    )
    r = c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "planned", "to": "running"},
        headers=hdr,
    )
    assert r.status_code == 409
    assert "adopt a session" in r.json()["detail"]
    # And the mission is untouched — a refused transition is not a partial one.
    assert c.get(f"/api/missions/{m['id']}").json()["state"] == "planned"


def test_a_project_less_mission_is_refused_for_its_CWD_not_its_roster(api):
    """Order matters in the refusal, because the two fixes are different.

    "adopt a session" would send the operator to adopt one they have already adopted, when the
    actual problem is that the mission has no project. The cwd check runs first.
    """
    c, hdr, proj = api
    m = _create(c, hdr, "no project")
    assert m["cwd"] is None
    c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "draft", "to": "planned"},
        headers=hdr,
    )
    c.post(
        f"/api/missions/{m['id']}/adopt",
        json={"session_key": CLAUDE_A},
        headers=hdr,
    )
    r = c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "planned", "to": "running"},
        headers=hdr,
    )
    assert r.status_code == 409
    assert "cwd" in r.json()["detail"]


def test_an_UNREADABLE_session_roster_is_not_reported_as_an_EMPTY_one(api, monkeypatch):
    """ "We could not look" is not "there is nothing there" (#896 review 8, finding 2).

    Suppressing the read error and reporting `0` turned an I/O failure into the factual claim
    that this mission holds no session — the same lie the probe runner's three-way answer exists
    to prevent, and the same one `unreadable` already prevents on the nudge budget.

    Red against `held = 0` with the exception suppressed: `no_session` comes back true.
    """
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    missions.adopt(mid, CLAUDE_A)

    def boom(*a, **k):
        raise OSError("the roster could not be read")

    monkeypatch.setattr(missions, "active_session_keys", boom)
    sup = c.get(f"/api/missions/{mid}", headers=hdr).json()["supervisor"]

    assert sup["sessions_unreadable"] is True
    assert sup["no_session"] is False, "an unread roster was reported as an empty one"
    assert sup["held_sessions"] is None
    # …and nothing may be sent, because authority that cannot be verified is not authority.
    o = sup["objectives"][0]
    assert o["may_nudge"] is False
    assert "could not be read" in o["why_not"], o["why_not"]


def test_a_ROSTER_READ_THAT_RECOVERS_does_not_produce_a_split_brain_reading(api, monkeypatch):
    """One snapshot, one verdict (#896 review 9, finding 1).

    Overloading `None` for both "read it yourself" and "I could not read it" let `assess` publish
    a contradiction: its own read failed, so it reported `sessions_unreadable: true` — and the
    second read inside `may_nudge` happened to succeed, so the row came back READY. The board
    then said "nothing can be sent" directly above a row promising to send something.

    Red against passing the same overloaded value down: `may_nudge` reads again and disagrees.
    """
    c, hdr, proj = api
    mid = _with_objective(c, hdr, proj)
    missions.adopt(mid, CLAUDE_A)

    calls = {"n": 0}
    real = missions.active_session_keys

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("the roster could not be read")
        return real(*a, **k)

    monkeypatch.setattr(missions, "active_session_keys", flaky)
    sup = c.get(f"/api/missions/{mid}", headers=hdr).json()["supervisor"]

    assert sup["sessions_unreadable"] is True
    o = sup["objectives"][0]
    assert o["may_nudge"] is False, "a recovered second read overruled the failed first one"
    assert "could not be read" in o["why_not"], o["why_not"]


# ---- The open turn rides on the mission detail (#902 review, finding 1) -----------------------


# ---- #894: relay — the operator's own words, through the actuator's fence -------------------


def _adopted(c, hdr, proj):
    """A running mission holding one session — what a relay needs to have a target at all.

    Through the PROJECT entity, because `dispatching` refuses a mission with no resolved cwd —
    the rule that keeps a client-supplied path out of the store, and the reason a relay test
    cannot take the short way to a running mission.
    """
    m = _create(c, hdr, project_id=proj.id)
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    missions.adopt(m["id"], CLAUDE_A)
    return m


def test_a_relay_goes_through_the_ACTUATOR_and_nowhere_else(api, monkeypatch):
    """#840 §9's relay contract, asserted on the door it uses rather than on the bytes.

    "It still goes through the actuator: precondition check, viewer-busy check, single-writer
    lock, ledger record." A second write path would satisfy every observable in this test except
    this one — which is why the assertion is that `actuator.deliver` was called with the action
    the route minted, not that something reached a pty.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    seen: list[str] = []

    async def deliver(action_id, **kw):
        seen.append(action_id)
        rec = orchestrator_ledger.get(action_id) or {}
        return {**rec, "state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", deliver)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "delivered"
    assert len(seen) == 1
    # The action it delivered is the one the route wrote, and it is OPERATOR-authored.
    rec = orchestrator_ledger.get(seen[0]) or {}
    assert rec.get("verb") == "relay"
    assert rec.get("origin") == "operator"
    assert rec.get("session_id") == CLAUDE_A


def test_a_relay_verb_can_NEVER_be_delivered_automatically(api):
    """ "Operator-authored … a NARROWER authority than the model-authored `answer`, not a wider
    one." The autonomous ceiling is `AUTO_VERBS_V1`, and `relay` is not in it — so no tier, no
    confidence and no configuration can cause one to be sent without an operator pressing send."""
    from agent_sessions import prefs

    assert "relay" not in prefs.AUTO_VERBS_V1
    # …and the model cannot even propose one: `relay` is not a verb the orchestrator's own
    # vocabulary contains, so there is no path from model output to this action.
    assert "relay" not in prefs.ORCH_VERBS


def test_a_relay_to_a_session_this_mission_does_NOT_hold_is_refused(api):
    """A relay aimed at a session the mission released would type the operator's words into
    somebody else's work. Refused with a reason — and refused again inside the fence by
    `deliver`'s own mission guard, which is the one that counts."""
    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_B, "text": "hello"},
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert "does not hold" in r.json()["detail"]


def test_a_relay_REQUIRES_login_and_csrf(api, auth_cfg):
    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    body = {"session_key": CLAUDE_A, "text": "hi"}
    assert c.post(f"/api/missions/{m['id']}/relay", json=body).status_code == 403
    fresh = _client(auth_cfg)
    assert fresh.post(f"/api/missions/{m['id']}/relay", json=body).status_code in (401, 403)


def test_an_EMPTY_or_OVERLONG_relay_is_refused_before_anything_is_written(api):
    """Bounded before it becomes a ledger record, like every other operator text on this
    surface — an unbounded relay is an unbounded durable row and an unbounded paste."""
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    before = len(orchestrator_ledger.live_actions())
    for body in (
        {"session_key": CLAUDE_A, "text": ""},
        {"session_key": CLAUDE_A, "text": "   "},
        {"session_key": CLAUDE_A, "text": 42},
        {"session_key": CLAUDE_A, "text": "x" * (actuator.NUDGE_MAX + 1)},
    ):
        r = c.post(f"/api/missions/{m['id']}/relay", json=body, headers=hdr)
        assert r.status_code == 422, (body, r.text)
    assert len(orchestrator_ledger.live_actions()) == before


def test_a_relay_is_RECORDED_on_the_timeline_even_when_it_is_refused(api, monkeypatch):
    """A write that was attempted and refused is a thing that happened to this mission. A
    transcript showing only the ones that worked is one the operator cannot reason about."""
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    async def refused(action_id, **kw):
        rec = orchestrator_ledger.get(action_id) or {}
        return {**rec, "state": "stale", "detail": "a viewer is attached"}

    monkeypatch.setattr(actuator, "deliver", refused)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "are you there?"},
        headers=hdr,
    )
    assert r.status_code == 200 and r.json()["state"] == "stale"
    events = missions.get_mission(m["id"])["events"]
    relayed = [e for e in events if (e.get("meta") or {}).get("relay")]
    assert len(relayed) == 1
    assert relayed[0]["kind"] == "operator_msg", "a relay must read as the OPERATOR's words"
    assert relayed[0]["text"] == "are you there?"
    assert relayed[0]["session_key"] == CLAUDE_A
    assert (relayed[0]["meta"] or {}).get("state") == "stale"


def test_a_relay_is_REFUSED_at_byte_one_if_the_session_changed_hands(api, monkeypatch):
    """The route's membership check and the write are two moments (#903 review, finding 1).

    Between them lie a ledger append, a quiet wait, an fd borrow and a lock queue — seconds, and
    the operator can detach the session and a second mission can adopt it inside them. Mission
    A's words would then land in mission B's work.

    **This drives the REAL fence** (#903 review 2, finding 4). The first version of this test
    replaced `actuator.deliver()` and called `_mission_membership_authority().check()` by hand,
    which is a test of the helper and not of the wiring: it stayed green with the production
    check deleted, so it protected nothing. Here the real `deliver()` runs the real
    `session_input.send_input()` against a real PTY, the membership moves inside the window the
    fence exists to cover, and the assertion is the one the operator cares about — **zero bytes
    reached the terminal**.
    """
    import os
    import pty
    import threading

    from agent_sessions import actuator, prefs, session_input

    c, hdr, proj = api
    a = _adopted(c, hdr, proj)
    b = _create(c, hdr, project_id=proj.id)
    missions.set_state(b["id"], "draft", "planned")
    missions.set_state(b["id"], "planned", "dispatching")
    missions.set_state(b["id"], "dispatching", "running")

    # A REAL pty, registered as the session's writer, so the whole write path is the production
    # one down to `os.write`.
    master, slave = pty.openpty()
    try:
        session_input.reset()
        session_input.register_writer(
            engines.physical_key(CLAUDE_A), master, threading.Lock(), "headless"
        )
        prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})

        # THE WINDOW. `_wait_quiet` runs after the ledger claim and before the final guard, which
        # is exactly where a detach-and-re-adopt lands in production. Moving membership here is
        # deterministic rather than raced — a test that merely races proves nothing when it
        # passes.
        real_quiet = session_input._wait_quiet

        def _move(key, deadline):
            missions.detach(a["id"], CLAUDE_A)
            missions.adopt(b["id"], CLAUDE_A)
            return real_quiet(key, deadline)

        monkeypatch.setattr(session_input, "_wait_quiet", _move)
        assert actuator.deliver is not None  # the REAL one; nothing is stubbed here

        r = c.post(
            f"/api/missions/{a['id']}/relay",
            json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] != "delivered", "a relay crossed into another mission's session"

        # ZERO BYTES. Read non-blocking from the other end of the pty: anything at all here is
        # mission A's words in mission B's agent.
        os.set_blocking(slave, False)
        try:
            got = os.read(slave, 4096)
        except BlockingIOError:
            got = b""
        assert got == b"", f"{len(got)} bytes reached the terminal: {got!r}"
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_a_relay_is_REFUSED_when_the_move_LANDS_BEFORE_the_fingerprint_is_taken(api, monkeypatch):
    """The other half of the same fence, and a genuinely different window.

    `send_input` captures the caller's fingerprint on entry, so a membership change AFTER that is
    caught by the comparison inside the write lock. A change that lands EARLIER — between the
    route's own check and the actuator's authority read — is invisible to that comparison, because
    the fingerprint is then taken with the NEW membership already in place and compares equal to
    itself. Only the guard branch refuses it.

    Both are real orderings, so both are pinned: without this, deleting the guard leaves a green
    suite because the fingerprint happens to cover the other case.
    """
    import os
    import pty
    import threading

    from agent_sessions import actuator, prefs, session_input

    c, hdr, proj = api
    a = _adopted(c, hdr, proj)
    b = _create(c, hdr, project_id=proj.id)
    missions.set_state(b["id"], "draft", "planned")
    missions.set_state(b["id"], "planned", "dispatching")
    missions.set_state(b["id"], "dispatching", "running")

    master, slave = pty.openpty()
    try:
        session_input.reset()
        session_input.register_writer(
            engines.physical_key(CLAUDE_A), master, threading.Lock(), "headless"
        )
        prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})

        # `is_live` is read early in `deliver`, before the authority tuple is derived — so the
        # move here is already in place when the fingerprint is taken.
        real_live = session_input.is_live
        moved: list[int] = []

        def _move(key):
            if not moved:
                moved.append(1)
                missions.detach(a["id"], CLAUDE_A)
                missions.adopt(b["id"], CLAUDE_A)
            return real_live(key)

        monkeypatch.setattr(session_input, "is_live", _move)
        assert actuator.deliver is not None  # the REAL one

        r = c.post(
            f"/api/missions/{a['id']}/relay",
            json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
            headers=hdr,
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"] != "delivered"

        os.set_blocking(slave, False)
        try:
            got = os.read(slave, 4096)
        except BlockingIOError:
            got = b""
        assert got == b"", f"{len(got)} bytes reached the terminal: {got!r}"
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_a_relay_whose_RECORD_cannot_be_written_sends_nothing(api, monkeypatch):
    """An acknowledged delivery with no transcript entry is the authorship contract broken.

    The record used to be written after the bytes with every failure suppressed, so a transient
    store failure sent the operator's words and answered `delivered` with nothing on the
    timeline saying who sent them. Writing it FIRST turns that into a refusal the operator can
    simply retry, because nothing has reached the pty yet.

    Red against the old order: 200 `delivered`, and no `operator_msg` event.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    delivered: list[str] = []

    async def deliver(action_id, **kw):
        delivered.append(action_id)
        rec = orchestrator_ledger.get(action_id) or {}
        return {**rec, "state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", deliver)

    real_append = missions.append_event

    def boom(mission_id, kind, **kw):
        if kind == "operator_msg":
            raise RuntimeError("the store is unavailable")
        return real_append(mission_id, kind, **kw)

    monkeypatch.setattr(missions, "append_event", boom)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 502, r.text
    assert "could not be written" in r.json().get("detail", "")
    assert delivered == [], "bytes were sent for a relay the mission could not record"


def test_a_relay_RECORD_exists_before_the_bytes_and_is_settled_after(api, monkeypatch):
    """The ordering, asserted directly: the event is on the timeline while the delivery runs."""
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    during: list[list] = []

    async def deliver(action_id, **kw):
        rows = missions.get_mission(m["id"])["events"]
        during.append(
            [e for e in rows if e["kind"] == "operator_msg" and e.get("action_id") == action_id]
        )
        rec = orchestrator_ledger.get(action_id) or {}
        return {**rec, "state": "delivered", "detail": ""}

    monkeypatch.setattr(actuator, "deliver", deliver)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text

    # It was already durable when the bytes were being written...
    assert len(during[0]) == 1, "the record was not written before the delivery"
    assert during[0][0]["meta"]["state"] == "sending"
    # ...and exactly ONE record exists afterwards, settled with the outcome.
    rows = [
        e
        for e in missions.get_mission(m["id"])["events"]
        if e["kind"] == "operator_msg" and (e.get("meta") or {}).get("relay")
    ]
    assert len(rows) == 1, f"the relay left {len(rows)} records; it must leave one"
    assert rows[0]["meta"]["state"] == "delivered"
    assert rows[0]["text"] == "yes, go ahead"


def test_a_relay_record_that_OUTLIVED_ITS_REQUEST_is_reconciled_on_the_next_read(api, monkeypatch):
    """`sending` is a claim about NOW, and a record that keeps making it lies by the next minute.

    The record is written before the bytes, so a process that exits in that window leaves one
    unsettled — and the ledger row under the same action id is the thing that knows how it ended
    (#903 review 2, finding 3).

    Red against a route with no read-time reconciliation: the record still says `sending`.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    async def deliver(action_id, **kw):
        # SETTLES THE LEDGER, as the real one does. The reconciler's whole job is to read that
        # row, so a stub that only returned a dict would leave the action `approved` — genuinely
        # in flight — and the record would correctly be left saying `sending`.
        return orchestrator_ledger.compare_and_set(
            action_id, frozenset({"approved", "claimed"}), "delivered"
        ) or {"state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", deliver)
    # THE CRASH: the settlement never runs, exactly as it would not if the process exited here.
    # Restored by hand rather than with `monkeypatch.undo()`, which would also revert the
    # fixture's own environment and point the read at a different store.
    real_settle = missions.settle_relay_event
    monkeypatch.setattr(missions, "settle_relay_event", lambda *a, **k: False)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    action_id = r.json()["action_id"]
    row = missions.get_mission(m["id"])
    stuck = next(e for e in row["events"] if e.get("action_id") == action_id)
    assert stuck["meta"]["state"] == "sending", "the fixture did not reproduce the stuck record"

    # …and the next ordinary read settles it from the ledger.
    monkeypatch.setattr(missions, "settle_relay_event", real_settle)
    body = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    settled = next(e for e in body["events"] if e.get("action_id") == action_id)
    assert settled["meta"]["state"] == "delivered"


def test_a_relay_whose_ACTION_WAS_NEVER_RECORDED_settles_as_definitely_not_sent(api, monkeypatch):
    """The one case that can be settled with certainty rather than inference.

    If the ledger append itself failed there is no row at all, so the actuator was never reached
    and no byte was written. Without this the record says `sending` for ever and a retry adds a
    second one beside it.
    """
    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    def boom(*a, **k):
        raise OSError("the ledger is unwritable")

    monkeypatch.setattr(orchestrator_ledger, "append", boom)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 502, r.text
    row = missions.get_mission(m["id"])
    rec = next(e for e in row["events"] if (e.get("meta") or {}).get("relay"))
    assert rec["meta"]["state"] == "failed", rec["meta"]
    assert "OSError" in rec["meta"].get("detail", "")


def test_a_DELIVERY_that_raised_after_the_claim_is_ambiguous_not_failed(api, monkeypatch):
    """#903 review 3, finding 3. The two exceptions are not the same fact, and stamping both
    `failed` asserts something nobody can know.

    Once `actuator.deliver` has CLAIMED the action the bytes may already be on the pty — the
    post-write ledger CAS lives inside that call and can raise after a successful write. So
    "nothing was sent" is not available as an answer, and the app has a word for that: the same
    `indeterminate` startup recovery uses for an orphaned claim.

    Red against a route with one `except` for the append and the delivery.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    async def raise_after_claiming(action_id, **kw):
        # WHAT DELIVERY LOOKS LIKE when the write landed and the settling CAS did not.
        orchestrator_ledger.claim(action_id, frozenset({"approved"}))
        raise OSError("the ledger could not be updated after the write")

    monkeypatch.setattr(actuator, "deliver", raise_after_claiming)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 502, r.text
    assert "may or may not" in r.json()["detail"]
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if (e.get("meta") or {}).get("relay")
    )
    assert rec["meta"]["state"] == "indeterminate", rec["meta"]
    # …AND THE LEDGER IS TERMINAL TOO (#903 review 4, finding 2). Left `claimed`, the action is
    # one startup recovery deliberately refuses to touch — its owner is still running — so the
    # session reads busy and later actions are refused until a restart. The process that knows
    # the delivery is over is this one.
    assert orchestrator_ledger.get(rec["action_id"])["state"] == "indeterminate"

    # BOTH DURABLE STORES AGREE, and a read does not talk either of them out of it: the ledger
    # row is terminal, its settlement is frozen from that row, and the reconciler prefers the
    # settlement. "Nobody could tell" is the recorded outcome, which is the point — the operator
    # decides whether to send it again, and nothing pretends to know for them.
    body = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    settled = next(e for e in body["events"] if e.get("action_id") == rec["action_id"])
    assert settled["meta"]["state"] == "indeterminate"


def test_a_relay_still_IN_FLIGHT_is_left_saying_sending(api):
    """The reconciler resolves what the ledger can answer for and nothing else. A live row is a
    delivery genuinely in progress, and `sending` is the true statement about it."""
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    orchestrator_ledger.append(
        {
            "id": "relay_live",
            "verb": "relay",
            "session_id": CLAUDE_A,
            "answer": "x",
            "state": "approved",
            "confidence": 1.0,
        }
    )
    missions.append_event(
        m["id"],
        "operator_msg",
        text="x",
        session_key=CLAUDE_A,
        action_id="relay_live",
        meta={"relay": True, "state": "sending"},
    )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 0
    again = missions.get_mission(m["id"])["events"]
    rec = next(e for e in again if e.get("action_id") == "relay_live")
    assert rec["meta"]["state"] == "sending"


def test_a_relay_read_BETWEEN_the_record_and_the_append_is_not_a_failure(api):
    """The record is committed BEFORE the ledger append, so "no row" is the NORMAL state for the
    moment between them — the same observation a crash leaves (#903 review 3, finding 1).

    Settling there stamped a perfectly live relay `failed` for ever, because only `sending` rows
    are ever revisited: the delivery then landed and could never repair the record.

    Red against treating absence as immediate proof.
    """
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    missions.append_event(
        m["id"],
        "operator_msg",
        text="go",
        session_key=CLAUDE_A,
        action_id="relay_racing",
        meta={"relay": True, "state": "sending"},
    )

    # THE WINDOW: the record exists, the append has not run yet.
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 0
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if e.get("action_id") == "relay_racing"
    )
    assert rec["meta"]["state"] == "sending", "a live relay was stamped failed mid-flight"

    # …the append and the delivery then land, and the next read settles it truthfully.
    orchestrator_ledger.append(
        {
            "id": "relay_racing",
            "verb": "relay",
            "session_id": CLAUDE_A,
            "answer": "go",
            "state": "approved",
            "confidence": 1.0,
        }
    )
    orchestrator_ledger.compare_and_set(
        "relay_racing", frozenset({"approved", "claimed"}), "delivered"
    )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 1
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if e.get("action_id") == "relay_racing"
    )
    assert rec["meta"]["state"] == "delivered"


def test_an_ABSENT_ledger_row_NEVER_settles_a_record_however_long_it_waits(api):
    """#903 review 3, finding 2. Absence is not a verdict and does not become one by waiting.

    Two measured failures killed the grace window rather than shrinking it: a ledger append waits
    on an unbounded writer flock, so a late append landed after the record had already been
    stamped `failed`; and compaction legitimately removes a terminal row once its settlement has
    been frozen, so a DELIVERED relay was overwritten to `failed`.

    Red against any version that concludes "nothing was sent" from a missing row.
    """
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    missions.append_event(
        m["id"],
        "operator_msg",
        text="go",
        session_key=CLAUDE_A,
        action_id="relay_stranded",
        meta={"relay": True, "state": "sending"},
    )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 0
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if e.get("action_id") == "relay_stranded"
    )
    assert rec["meta"]["state"] == "sending"

    # …AND THE BLOCKED APPEND THEN LANDS. This is the delivery the old shape had already called a
    # failure, and only `sending` rows were revisited — so the record could never be repaired.
    orchestrator_ledger.append(
        {
            "id": "relay_stranded",
            "verb": "relay",
            "session_id": CLAUDE_A,
            "answer": "go",
            "state": "delivered",
            "confidence": 1.0,
        }
    )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 1
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if e.get("action_id") == "relay_stranded"
    )
    assert rec["meta"]["state"] == "delivered"


def test_a_COMPACTED_delivery_is_read_from_its_frozen_settlement(api):
    """#903 review 3, finding 2, the other half. Compaction removes a terminal row only AFTER
    freezing its settlement — the projection is written precisely so it outlives the row. A
    reconcile that consults only the ledger sees a delivered relay as absent and overwrites the
    app's own record of a success.

    Red against a reconciler that reads the ledger and not the settlement.
    """
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    missions.append_event(
        m["id"],
        "operator_msg",
        text="go",
        session_key=CLAUDE_A,
        action_id="relay_compacted",
        meta={"relay": True, "state": "sending"},
    )
    # THE ROW IS GONE and the settlement remains — exactly what compaction leaves behind.
    missions.record_settlement(
        "relay_compacted",
        {
            "id": "relay_compacted",
            "verb": "relay",
            "state": "delivered",
            "detail": "the words landed",
        },
    )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 1
    rec = next(
        e
        for e in missions.get_mission(m["id"])["events"]
        if e.get("action_id") == "relay_compacted"
    )
    assert rec["meta"]["state"] == "delivered"


def test_the_STORE_refuses_to_overwrite_a_definite_relay_outcome(api):
    """The fence is on the WRITE, not in each caller's head. The reconciler filters open records
    before it asks, which is the right thing for it to do and the wrong thing to rely on: a second
    writer with a different opinion — a future pass, a repair script — must not be able to replace
    somebody's answer about what happened to the operator's words."""
    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    for aid, state in (("r_open", "sending"), ("r_done", "delivered")):
        missions.append_event(
            m["id"],
            "operator_msg",
            text="go",
            session_key=CLAUDE_A,
            action_id=aid,
            meta={"relay": True, "state": state},
        )
    assert missions.settle_relay_event(m["id"], action_id="r_open", state="failed") is True
    assert missions.settle_relay_event(m["id"], action_id="r_done", state="failed") is False
    after = {
        e["action_id"]: e["meta"]["state"]
        for e in missions.get_mission(m["id"])["events"]
        if e.get("action_id") in {"r_open", "r_done"}
    }
    assert after == {"r_open": "failed", "r_done": "delivered"}


def test_an_INDETERMINATE_record_is_revisited_and_a_DEFINITE_one_is_not(api):
    """`indeterminate` says nobody could tell YET, so a later terminal row may resolve it. A
    definite outcome is somebody's answer about the operator's own words and is never replaced —
    the fence is on the write, so no caller can talk its way past it."""
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    for aid, state in (("relay_amb", "indeterminate"), ("relay_done", "delivered")):
        missions.append_event(
            m["id"],
            "operator_msg",
            text="go",
            session_key=CLAUDE_A,
            action_id=aid,
            meta={"relay": True, "state": state},
        )
        orchestrator_ledger.append(
            {
                "id": aid,
                "verb": "relay",
                "session_id": CLAUDE_A,
                "answer": "go",
                "state": "failed",
                "detail": "the ledger's later word",
                "confidence": 1.0,
            }
        )
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 1
    after = {
        e["action_id"]: e["meta"]["state"]
        for e in missions.get_mission(m["id"])["events"]
        if e.get("action_id") in {"relay_amb", "relay_done"}
    }
    assert after == {"relay_amb": "failed", "relay_done": "delivered"}


def test_an_UNREADABLE_ledger_settles_nothing(api, monkeypatch):
    """`get()` maps every I/O error to "no such action", which turns a transient fault into the
    permanent claim that nothing was ever sent. The tri-state read is what keeps "we could not
    look" out of the record."""
    from agent_sessions import mission_relay_reconcile

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    missions.append_event(
        m["id"],
        "operator_msg",
        text="go",
        session_key=CLAUDE_A,
        action_id="relay_unreadable",
        meta={"relay": True, "state": "sending"},
    )
    monkeypatch.setattr(orchestrator_ledger, "lookup", lambda *a, **k: ("unreadable", None))
    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 0
    rec = next(
        e
        for e in missions.get_mission(m["id"])["events"]
        if e.get("action_id") == "relay_unreadable"
    )
    assert rec["meta"]["state"] == "sending"


def test_the_CONTEXT_offers_only_ACTIVE_sessions_as_controls(api):
    """The roster HISTORY is right for a record and wrong for a control surface (#903 review 3,
    finding 2).

    The console renders one live screen-and-relay block per session the context returns. With
    detached rows included, a mission that had released a session went on offering it — and VIEW
    SCREEN reads by session key, so it showed the CURRENT output of a session another mission had
    since adopted, in the old mission's context. SEND only failed later, at the write fence.

    Red against returning `m["sessions"]` verbatim.
    """
    c, hdr, proj = api
    a = _adopted(c, hdr, proj)
    b = _create(c, hdr, project_id=proj.id)
    missions.set_state(b["id"], "draft", "planned")
    missions.set_state(b["id"], "planned", "dispatching")
    missions.set_state(b["id"], "dispatching", "running")

    ctx = c.get(f"/api/missions/{a['id']}/context", headers=hdr).json()
    assert [s["session_key"] for s in ctx["sessions"]] == [CLAUDE_A]

    # A detaches it and B adopts it.
    missions.detach(a["id"], CLAUDE_A)
    missions.adopt(b["id"], CLAUDE_A)

    ctx = c.get(f"/api/missions/{a['id']}/context", headers=hdr).json()
    assert ctx["sessions"] == [], "a released session was still offered as a live control"
    # …and the mission that holds it now does offer it.
    ctx_b = c.get(f"/api/missions/{b['id']}/context", headers=hdr).json()
    assert [s["session_key"] for s in ctx_b["sessions"]] == [CLAUDE_A]

    # The RECORD is untouched — the timeline still says the session was there and left.
    kinds = [e["kind"] for e in missions.get_mission(a["id"])["events"]]
    assert kinds.count("session") >= 2


@pytest.mark.anyio
async def test_a_TRANSIENT_recovery_failure_is_retried_rather_than_ending_recovery(
    api, monkeypatch
):
    """#903 review 3, finding 4. Recovery reads a file that can be transiently unreadable, and one
    suppressed attempt turned that into "never": with the sweep switched off, `run()` returned and
    nothing would ever look again, leaving the ledger `claimed` and the record `sending` for ever.

    Red against a `recover_once` whose failure and success are the same answer.
    """
    from agent_sessions import orchestrator_loop

    c, hdr, proj = api
    orchestrator_ledger.append(
        {"id": "relay_orphan", "state": "claimed", "claim_owner": "4194305:1"}
    )

    calls: list[int] = []
    real = orchestrator_ledger.recover_claimed

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the ledger could not be read")
        return real(*a, **kw)

    monkeypatch.setattr(orchestrator_ledger, "recover_claimed", flaky)
    # No sleeping in a test: the retry cadence is not what is under test, the RETRY is.
    monkeypatch.setattr(orchestrator_loop, "RECOVERY_RETRY_S", 0)
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LOOP", "0")

    await orchestrator_loop.run()

    assert len(calls) == 2, "a failed first attempt ended recovery"
    assert orchestrator_ledger.get("relay_orphan")["state"] == "indeterminate"


@pytest.mark.anyio
async def test_CRASH_RECOVERY_runs_even_with_the_orchestrator_loop_switched_off(api, monkeypatch):
    """A kill-switch for the autonomous sweep is not a kill-switch for crash recovery.

    The relay is a MANUAL route — an operator presses send, the action is claimed, the process
    dies. With `recover_claimed()` behind `AGENT_SESSIONS_ORCHESTRATOR_LOOP=0`, a supported
    configuration, that left the ledger row `claimed` and the mission's own record `sending` for
    ever, with nothing that would ever revisit either (#903 review 3, finding 3).

    Red against a `run()` that returns on the kill-switch before recovering.
    """
    from agent_sessions import orchestrator_loop

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)
    orchestrator_ledger.append(
        {
            "id": "relay_claimed",
            "verb": "relay",
            "session_id": CLAUDE_A,
            "answer": "go",
            "state": "approved",
            "confidence": 1.0,
        }
    )
    orchestrator_ledger.claim("relay_claimed", frozenset({"approved"}))
    missions.append_event(
        m["id"],
        "operator_msg",
        text="go",
        session_key=CLAUDE_A,
        action_id="relay_claimed",
        meta={"relay": True, "state": "sending"},
    )
    assert orchestrator_ledger.get("relay_claimed")["state"] == "claimed"
    # THE PROCESS THAT CLAIMED IT IS GONE. Re-stamped with a pid that cannot exist, because
    # recovery now refuses to steal a claim whose owner is still running — and the owner of the
    # claim above is this very test process (#903 review 3, finding 4). Without this the test
    # would assert the crash path while exercising the live-sibling one.
    orchestrator_ledger.append(
        {"id": "relay_claimed", "state": "claimed", "claim_owner": "4194305:1"}
    )

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LOOP", "0")
    await orchestrator_loop.run()

    # The ledger row is terminal…
    assert orchestrator_ledger.get("relay_claimed")["state"] == "indeterminate"
    # …and the mission's own record can therefore be settled by the next read.
    from agent_sessions import mission_relay_reconcile

    events = missions.get_mission(m["id"])["events"]
    assert mission_relay_reconcile.reconcile(m["id"], events) == 1
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if e.get("action_id") == "relay_claimed"
    )
    assert rec["meta"]["state"] == "indeterminate"


def test_a_screen_read_that_finishes_AFTER_a_re_adoption_returns_nothing(api, monkeypatch):
    """#903 review 4, finding 1, and a cross-mission data-exposure race rather than a tidiness
    point.

    The membership check was about the REQUEST; the bytes are a different moment. Reading a live
    screen takes a ring replay and filesystem work, and a detach-and-re-adopt inside that window
    means what is in hand belongs to whoever owns the session now — mission A returned mission
    B's current terminal output, under A's own heading.

    Red against a route that checks once and returns whatever it read.
    """
    from agent_sessions import orchestrator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    def slow_read(session_id, kind):
        # THE OWNERSHIP MOVES WHILE THE READ IS IN FLIGHT — which is exactly what a detach and a
        # re-adopt by another mission does, and the only window this finding is about.
        missions.detach(m["id"], CLAUDE_A)
        return {"kind": "screen", "text": "the NEW owner's output", "available": True}

    monkeypatch.setattr(orchestrator, "evidence_for", slow_read)
    r = c.get(f"/api/missions/{m['id']}/screen/{CLAUDE_A}", headers=hdr)
    assert r.status_code == 409, r.text
    assert "no longer holds" in r.json()["detail"]
    assert "NEW owner" not in r.text


def test_a_COMPENSATING_WRITE_that_also_FAILS_is_retried_rather_than_dropped(api, monkeypatch):
    """#903 review 5, finding 1. The compensating `claimed -> indeterminate` CAS is written by the
    one process that knows the delivery is over — and it can fail for the same reason the delivery
    did, because it is the same store.

    Suppressed, that left the ledger `claimed` under an owner that is still running, which
    `recover_claimed` correctly refuses to touch: the session reads busy and later actions are
    refused until this process restarts, while the timeline says the delivery is terminal.

    Red against a `contextlib.suppress` around the compensating write.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    async def raise_after_claiming(action_id, **kw):
        orchestrator_ledger.claim(action_id, frozenset({"approved"}))
        raise OSError("the ledger could not be updated after the write")

    real_cas = orchestrator_ledger.compare_and_set
    broken = {"on": True}

    def flaky_cas(action_id, from_states, to_state, *a, **kw):
        # ONLY THE COMPENSATING WRITE. `claim()` goes through this same function, so a blanket
        # failure would stop the delivery ever claiming — and the split state this test is about
        # needs the claim to have SUCCEEDED.
        if broken["on"] and to_state == "indeterminate":
            raise OSError("the store is still down")
        return real_cas(action_id, from_states, to_state, *a, **kw)

    monkeypatch.setattr(actuator, "deliver", raise_after_claiming)
    monkeypatch.setattr(orchestrator_ledger, "compare_and_set", flaky_cas)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 502, r.text
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if (e.get("meta") or {}).get("relay")
    )
    # THE SPLIT STATE the finding is about: the event says terminal, the ledger says claimed.
    assert rec["meta"]["state"] == "indeterminate"
    assert orchestrator_ledger.get(rec["action_id"])["state"] == "claimed"
    # …and recovery will NOT touch it, because this process is still alive — which is right, and
    # is exactly why the obligation has to be remembered here.
    assert orchestrator_ledger.recover_claimed() == []

    # THE OBLIGATION SURVIVED, and the ordinary read-time reconcile discharges it once the store
    # comes back. No new loop: this is the cadence that already exists for unfinished relays.
    broken["on"] = False
    body = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert orchestrator_ledger.get(rec["action_id"])["state"] == "indeterminate"
    assert body["events"], "the read still worked"


# ---- The open turn rides on the mission detail (#902 review, finding 1) -----------------------


def test_the_mission_DETAIL_carries_the_turn_the_operator_is_still_owed(api):
    """What a reload finds. The composer used to hold this in component state, so closing the tab
    lost the fact that work was in flight — a durable turn nobody could read back is not one."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    missions.claim_turn(m["id"], "t1", "sha", text="run the tests")

    r = c.get(f"/api/missions/{m['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    turn = r.json().get("turn")
    assert turn and turn["turn_id"] == "t1"
    assert turn["state"] == "in_progress"
    assert turn["text"] == "run the tests"


def test_only_an_AMBIGUOUS_turn_can_be_dismissed_and_the_dismissal_STICKS(api):
    """`indeterminate` is terminal and the server cannot resolve it, so the operator's "I have
    seen this" is the only thing that ends it — and it has to outlive the tab that said it."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    _, row = missions.claim_turn(m["id"], "t1", "sha", text="restart it")

    # Still running: not dismissible, and the route says so rather than pretending.
    bad = c.post(f"/api/missions/{m['id']}/turns/t1/ack", headers=hdr)
    assert bad.status_code == 404, bad.text
    assert c.get(f"/api/missions/{m['id']}", headers=hdr).json().get("turn")

    missions.reserve_turn_write(m["id"], "t1", row["fence"])
    missions.abandon_turn(m["id"], "t1", row["fence"])
    assert c.get(f"/api/missions/{m['id']}", headers=hdr).json()["turn"]["state"] == (
        "indeterminate"
    )

    ok = c.post(f"/api/missions/{m['id']}/turns/t1/ack", headers=hdr)
    assert ok.status_code == 200, ok.text
    # …and it is gone from the next read, which is the whole point of it being durable.
    assert c.get(f"/api/missions/{m['id']}", headers=hdr).json().get("turn") is None


def test_dismissing_a_turn_REQUIRES_login_and_csrf(api, auth_cfg):
    """It is a state-changing write like every other mission mutation."""
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    assert c.post(f"/api/missions/{m['id']}/turns/t1/ack").status_code == 403
    fresh = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert fresh.post(f"/api/missions/{m['id']}/turns/t1/ack").status_code in (401, 403)


# #900 review round 7
# ==============================================================================================


def test_ANSWERING_takes_the_SAME_fence_a_question_takes(api, monkeypatch):
    """#900 review 7, finding 1, at the route — where the hole was.

    A question OPENING already enumerated fail-closed, locked the roster pseudo-key, and re-read
    inside the lock. The ANSWER route approximated all three: `_held_physical_keys` mapped a store
    read failure to `[]`, so a fence that could not see the sessions locked nothing and said it
    had, and nothing re-read the roster — so a session adopted between the enumeration and the
    transaction was never held while the answer withdrew the authority behind an in-flight nudge.

    Asserted on the LOCK the write takes, because that is the mechanism: a timing test would pass
    against the unfixed code most of the time, which is the worst kind of green.

    Red against a route with its own enumerate-and-lock.
    """
    import contextlib as _c

    from agent_sessions import mission_fence, session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    q = _asked(m["id"])

    taken: list[list[str]] = []
    real = session_input.sessions_transaction

    @_c.contextmanager
    def watched(keys):
        taken.append(list(keys))
        with real(keys):
            yield

    monkeypatch.setattr(session_input, "sessions_transaction", watched)
    r = c.post(
        f"/api/missions/{m['id']}/answer",
        json={"seq": q["seq"], "option_index": 0},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert taken, "the answer committed outside the write fence"
    assert (
        mission_fence.roster_key(m["id"]) in taken[0]
    ), "the answer did not take the roster's own lock, so an adoption can still interleave"


def test_an_UNREADABLE_roster_REFUSES_the_answer_rather_than_locking_nothing(api, monkeypatch):
    """#900 review 7, finding 1, the other half. A fence that cannot enumerate the sessions it is
    meant to lock does not become a fence by locking nothing — and the answer route swallowed the
    read failure into an empty set, so the write proceeded believing it was ordered.

    Red against `except Exception: return []`.
    """
    from agent_sessions import mission_fence

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    q = _asked(m["id"])

    def boom(*a, **k):
        raise OSError("the roster could not be read")

    monkeypatch.setattr(mission_fence, "held_keys", boom)
    r = c.post(
        f"/api/missions/{m['id']}/answer",
        json={"seq": q["seq"], "option_index": 0},
        headers=hdr,
    )
    assert r.status_code == 503, r.text
    # …and nothing was written: the question is still there to answer once the store recovers.
    assert missions.open_question_row(m["id"]) is not None


def test_ADOPT_takes_the_ROSTER_lock_a_question_serialises_against(api, monkeypatch):
    """#900 review 7, finding 12. The "adoption and a question share one fence" property was
    asserted by calling `_fenced_open()` alone — the QUESTION side — so removing the adoption
    side's lock left it green, and the two operations went back to checking in one lock domain
    and writing in another.

    This drives `POST /adopt`, which is the participant the other test cannot see.
    """
    import contextlib as _c

    from agent_sessions import mission_fence, session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    taken: list[list[str]] = []
    real = session_input.sessions_transaction

    @_c.contextmanager
    def watched(keys):
        taken.append(list(keys))
        with real(keys):
            yield

    monkeypatch.setattr(session_input, "sessions_transaction", watched)
    key = "claude:11111111-1111-1111-1111-111111111111"
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": key}, headers=hdr)
    assert r.status_code == 200, r.text
    assert taken, "the adoption committed outside any fence"
    assert (
        mission_fence.roster_key(m["id"]) in taken[0]
    ), "the adoption did not take the roster's own lock, so a question can still interleave"
    # …and the session being adopted is locked too: it is the one a nudge could be writing into.
    assert any("11111111" in k for k in taken[0])


def test_the_QUESTION_a_mission_GET_offers_is_one_its_TIMELINE_carries(api, monkeypatch):
    """#900 review 8, finding 1. The previous fix made the flag and the question agree with EACH
    OTHER, and that was not enough: they came from a different transaction than the mission row
    and its timeline, so a question opening between the two reads produced a 200 carrying an
    actionable question whose own event was not in the `events` array — an answer the console
    could offer and then not show.

    The interleaving is injected at `_attention_rows`, which the read reaches AFTER it has taken
    its snapshot of the timeline. Under one transaction the late question is invisible to both
    halves, so the answer stays consistent; under two it is visible to the attention half only.

    Red against a mission GET that reads the attention projection on its own connection.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr_open", "title": "A PR is open", "gate": True}]
    )

    real = missions._attention_rows
    fired: list[int] = []

    def open_then_read(con, ids):
        # A question committed on ANOTHER connection, in the window the two-read version left
        # open. Once only: the read below must not recurse into itself.
        if not fired:
            fired.append(1)
            missions.open_question(
                m["id"],
                "pr_open",
                "which of the two open PRs is this mission's?",
                [
                    {"label": "The one from Tuesday", "action": "note_answer"},
                    {"label": "Neither", "action": "waive_objective"},
                ],
            )
        return real(con, ids)

    monkeypatch.setattr(missions, "_attention_rows", open_then_read)
    r = c.get(f"/api/missions/{m['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert fired, "the interleaving never happened, so this test proves nothing"

    # THE WHOLE ANSWER AGREES WITH ITSELF. Not "the flag matches the question" — the question the
    # console is offered has to be one the timeline it was sent actually carries.
    q = body.get("question")
    seqs = {e["seq"] for e in body.get("events") or []}
    if q is not None:
        assert (
            q["seq"] in seqs
        ), "the mission was handed a question whose own event is not in the timeline beside it"
    assert ("question" in (body.get("needs_you_why") or [])) == (q is not None)

    # …and the NEXT read, which is a fresh snapshot, carries both.
    again = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert again["question"] is not None
    assert again["question"]["seq"] in {e["seq"] for e in again["events"]}


def test_a_BUSY_FENCE_on_EVERY_fenced_route_is_a_RETRY_not_a_500(api, monkeypatch):
    """#900 review 8, finding 2. The shared helper let `AuthorityFenceBusy` escape, and every
    route that uses it catches only `MissionError` — so a held fence, which is ordinary
    contention and means the write did NOT happen, surfaced as an internal server error on the
    one path whose next move is simply "try again".

    Asserted on ALL of them, because the defect was that they shared a helper and not a contract.

    Red against a `fenced_write` that does not translate.
    """
    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    q = _asked(m["id"])
    c.post(f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "planned"}, headers=hdr)

    def busy(keys):
        raise session_input.AuthorityFenceBusy("the fence is held")

    monkeypatch.setattr(session_input, "sessions_transaction", busy)
    calls = [
        ("answer", {"seq": q["seq"], "option_index": 0}),
        ("state", {"from": "planned", "to": "abandoned", "outcome": "abandoned"}),
        ("detach", {"session_key": CLAUDE_A}),
        ("objectives", {"ops": [{"op": "drop", "key": "pr_open"}]}),
    ]
    for path, body in calls:
        r = (
            c.patch(f"/api/missions/{m['id']}/{path}", json=body, headers=hdr)
            if path == "objectives"
            else c.post(f"/api/missions/{m['id']}/{path}", json=body, headers=hdr)
        )
        assert r.status_code == 503, (path, r.status_code, r.text)
        assert "retry" in r.json()["detail"], path
    # …and nothing was written: the question is still there to answer once the fence frees.
    assert missions.open_question_row(m["id"]) is not None


def test_a_BUSY_FENCE_at_ADOPT_is_a_RETRY_not_a_500(api, monkeypatch):
    """#900 review 7, finding 5. The shared fence being held is ordinary contention — a question
    is committing against this very roster — and the adoption did NOT happen. Every sibling
    mutation answers 503 for it; this route caught only `MissionError`, so the same condition
    became a 500 that tells the operator the app failed.
    """
    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    def busy(keys):
        raise session_input.AuthorityFenceBusy("the fence is held")

    monkeypatch.setattr(session_input, "sessions_transaction", busy)
    r = c.post(
        f"/api/missions/{m['id']}/adopt",
        json={"session_key": "claude:11111111-1111-1111-1111-111111111111"},
        headers=hdr,
    )
    assert r.status_code == 503, r.text
    assert "retry" in r.json()["detail"]
    assert missions.get_mission(m["id"])["sessions"] == []


def test_an_OWED_terminalization_is_discharged_WITHOUT_anybody_opening_the_page(api, monkeypatch):
    """#903 review 6, finding 1. The read-time reconcile only runs when somebody looks at a
    mission, and an operator who closes the page after the ambiguous 502 had nothing left that
    would ever retry it.

    The consequence is not cosmetic: a claim this process abandoned but could not release reads
    as LIVE, so `append_batch_for_free_sessions` treats the session as busy and drops every later
    autonomous action for it — indefinitely, because the owner is still running and startup
    recovery is right to refuse the row.

    Red against a discharge that only the mission read triggers.
    """
    from agent_sessions import actuator

    c, hdr, proj = api
    m = _adopted(c, hdr, proj)

    async def raise_after_claiming(action_id, **kw):
        orchestrator_ledger.claim(action_id, frozenset({"approved"}))
        raise OSError("the ledger could not be updated after the write")

    real_cas = orchestrator_ledger.compare_and_set
    broken = {"on": True}

    def flaky_cas(action_id, from_states, to_state, *a, **kw):
        if broken["on"] and to_state == "indeterminate":
            raise OSError("the store is still down")
        return real_cas(action_id, from_states, to_state, *a, **kw)

    monkeypatch.setattr(actuator, "deliver", raise_after_claiming)
    monkeypatch.setattr(orchestrator_ledger, "compare_and_set", flaky_cas)
    r = c.post(
        f"/api/missions/{m['id']}/relay",
        json={"session_key": CLAUDE_A, "text": "yes, go ahead"},
        headers=hdr,
    )
    assert r.status_code == 502, r.text
    rec = next(
        e for e in missions.get_mission(m["id"])["events"] if (e.get("meta") or {}).get("relay")
    )
    assert orchestrator_ledger.get(rec["action_id"])["state"] == "claimed"

    # THE SESSION READS BUSY, which is the actual harm — a later autonomous action is dropped.
    broken["on"] = False
    later = {"id": "act_later", "session_id": CLAUDE_A, "state": "approved", "verb": "continue"}
    kept, dropped = orchestrator_ledger.append_batch_for_free_sessions([dict(later)])
    # …and the admission check itself discharges the obligation first, so the action IS admitted.
    assert [k["id"] for k in kept] == ["act_later"], (kept, dropped)
    assert orchestrator_ledger.get(rec["action_id"])["state"] == "indeterminate"


@pytest.mark.anyio
async def test_the_ORCHESTRATOR_PASS_discharges_an_owed_terminalization(api, monkeypatch):
    """The recurring execution path, with no page read and no admission check in between."""
    from agent_sessions import orchestrator_loop

    c, hdr, proj = api
    orchestrator_ledger.append(
        {"id": "act_owed", "state": "claimed", "session_id": CLAUDE_A, "verb": "continue"}
    )
    orchestrator_ledger.owe_terminalize("act_owed", "the delivering process could not settle it")

    # `sweep` returns early for an unconfigured endpoint — which is the point: the discharge is
    # ABOVE that gate, because a kill-switch for the autonomous pass is not a kill-switch for
    # finishing what a delivery started.
    await orchestrator_loop.sweep()
    assert orchestrator_ledger.get("act_owed")["state"] == "indeterminate"


# ---- the dispatch proposal (#893) -------------------------------------------------------


def _plan_reply(monkeypatch, **over):
    """Stand in for the model. The route's own resolution is what these tests are about."""
    from agent_sessions import review

    async def reply(messages, **kw):
        return {"project_index": 0, "engine_index": 0, "brief": "go", **over}

    monkeypatch.setattr(review, "complete_json", reply)


def test_PLAN_launches_nothing_and_resolves_the_cwd_SERVER_SIDE(api, monkeypatch):
    """The separation is the feature: a proposal on screen, editable, before anything runs with
    nobody watching it. And the cwd comes from the project entity — the client sends an id."""
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)

    r = c.post(f"/api/missions/{m['id']}/plan", headers=hdr)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["project_id"] == proj.id
    assert plan["cwd"] == proj.default_folder
    assert plan["engine"] == "claude"
    # NOTHING STARTED — but the mission is now DISPATCHABLE, which is what a plan means.
    # Leaving it in `draft` made the proposal un-dispatchable, because `claim_plan` only claims
    # from `planned`, so the one path an operator actually takes 409'd (#904 review 1).
    assert c.get(f"/api/missions/{m['id']}", headers=hdr).json()["state"] == "planned"


def test_EDITING_a_plan_mints_a_NEW_id(api, monkeypatch):
    """Editing is not a lesser act than planning: it produces a different proposal.

    If an edit reused the id, a dispatch approved against the version on screen a minute ago
    would run the version typed since — the failure the id exists to prevent, through the other
    door.
    """
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    first = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()

    r = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": first["plan_id"], "brief": "do it differently"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan_id"] != first["plan_id"]
    assert r.json()["brief"] == "do it differently"


def test_an_EDIT_may_not_name_an_engine_that_could_not_have_been_OFFERED(api, monkeypatch):
    """`shell` is a login shell and a brief pasted into it EXECUTES. The operator may choose any
    agent that could have been offered, and may not choose one that could not."""
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    have = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()

    r = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": have["plan_id"], "engine": "shell"},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert "shell" in r.json()["detail"]
    # …and the stored plan is untouched.
    assert missions.get_plan(m["id"])["engine"] == "claude"


def test_an_EDIT_sends_a_PROJECT_ID_and_never_a_PATH(api, monkeypatch):
    """The same rule `POST /api/missions` follows, and the reason the client has no way to choose
    a working directory at all: an unknown id is a 404, and a path is simply not a field."""
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    have = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    pid = have["plan_id"]

    bad = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": pid, "project_id": "prj_nope"},
        headers=hdr,
    )
    assert bad.status_code == 404, bad.text
    # A path in the project_id slot is an unknown project, not a directory.
    worse = c.patch(
        f"/api/missions/{m['id']}/plan", json={"plan_id": pid, "project_id": "/etc"}, headers=hdr
    )
    assert worse.status_code == 404, worse.text
    assert missions.get_plan(m["id"])["cwd"] == proj.default_folder


def test_PLANNING_requires_login_and_csrf(api, auth_cfg):
    c, hdr, proj = api
    m = _create(c, hdr)
    assert c.post(f"/api/missions/{m['id']}/plan").status_code == 403
    assert c.patch(f"/api/missions/{m['id']}/plan", json={}).status_code == 403
    fresh = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert fresh.post(f"/api/missions/{m['id']}/plan").status_code in (401, 403)


def test_DISPATCH_refuses_a_STALE_plan_id(api, monkeypatch):
    """The operator dispatches the proposal on their screen, not "whatever is stored now".

    A model call and an edit both sit between reading the plan and pressing the button, so "the
    mission's current plan" is a slot. Red against a route that reads the stored plan and runs it.
    """
    from agent_sessions import mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    # The master switch is checked BEFORE the plan is consumed, so it has to be on for this test
    # to reach the fence it is about. That ordering is deliberate: the cheapest refusal first, and
    # a refused dispatch must not eat the proposal.
    prefs.set_orchestrator({"enabled": True})
    # …and so is CONTAINMENT, which is a property of the HOST (#904 review 6, finding 2). Without
    # stubbing it this test asserted the plan-id fence on a host that has transient scopes and
    # asserted nothing at all on one that does not — it got the containment 409 first and passed
    # for the wrong reason on neither. The dedicated containment test owns that refusal.
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    m = _create(c, hdr)
    _ready_objectives(m["id"])
    stale = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": stale["plan_id"], "brief": "changed my mind"},
        headers=hdr,
    )

    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": stale["plan_id"],
            "expect_cwd": stale["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert "replaced" in r.json()["detail"]
    # Nothing started, and the plan is still there to read again.
    assert missions.get_mission(m["id"])["state"] == "planned"
    assert missions.get_plan(m["id"]) is not None


def test_DISPATCH_re_reads_the_MASTER_SWITCH_at_the_write_boundary(api, monkeypatch):
    """A plan can sit on screen for as long as the operator likes, and orchestration can be
    switched off in that time. Reading policy at plan time and trusting it here is #887's shape.

    Red against a route that checks the switch only when the plan is made.
    """
    from agent_sessions import mission_plan, prefs

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()

    prefs.set_orchestrator({"enabled": False})
    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert "switched off" in r.json()["detail"]
    assert missions.get_mission(m["id"])["state"] == "planned"


def test_DISPATCH_refuses_a_host_that_cannot_CONTAIN_the_agent(api, monkeypatch):
    """A launch we could not clean up is not one to start unattended (#898 review 7's note).

    Without a transient scope the teardown boundary is a pid snapshot, and a target that forks a
    survivor during SIGTERM walks out of it and is reported as a clean stop. This is the caller
    that makes that matter, so the refusal lives here.
    """
    from agent_sessions import mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    m = _create(c, hdr)
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()

    monkeypatch.setattr(scopedspawn, "available", lambda: False)
    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert "could not be reliably stopped" in r.json()["detail"]
    assert missions.get_mission(m["id"])["state"] == "planned"


def test_DISPATCH_requires_login_and_csrf(api, auth_cfg):
    c, hdr, proj = api
    m = _create(c, hdr)
    assert c.post(f"/api/missions/{m['id']}/dispatch", json={"plan_id": "x"}).status_code == 403
    fresh = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert fresh.post(f"/api/missions/{m['id']}/dispatch", json={"plan_id": "x"}).status_code in (
        401,
        403,
    )


def test_CREATE_then_PLAN_then_DISPATCH_works_with_no_hand_moved_state(api, monkeypatch):
    """#904 review 1, end to end and through the routes only.

    The three calls an operator actually makes, in order, with nothing moving the mission's state
    between them. Every dispatch test in this file used to perform the missing `draft -> planned`
    transition itself, which is exactly why a `/plan` that left the mission in `draft` — and a
    DISPATCH that therefore always answered `409: mission is draft, not planned` — passed review
    and passed CI.

    The launch itself is stubbed at `mission_dispatch.run`: what is under test is the LIFECYCLE
    between the routes, not the spawn (#898 owns that, against a real pty).
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)

    launched: list[dict] = []

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        launched.append(plan)
        missions.settle_dispatch(
            mission_id, to="running", detail="dispatched", session_key="claude:" + "a" * 8
        )
        return {"state": "running", "reason": "", "session_key": None}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    m = _create(c, hdr)
    assert missions.get_mission(m["id"])["state"] == "draft"

    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    assert missions.get_mission(m["id"])["state"] == "planned"
    # …and the operator establishes what finishing means, which #904 review 4 finding 3 makes a
    # precondition of the launch rather than a warning beside it.
    _ready_objectives(m["id"])

    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    # The launch received the SERVER-resolved cwd, re-read from the entity at this moment.
    assert launched and launched[0]["cwd"] == proj.default_folder
    assert missions.get_mission(m["id"])["state"] == "running"


def test_a_mission_whose_PROJECT_VANISHED_can_still_be_READ(api, monkeypatch):
    """An offer that cannot be made must not take the mission's history with it (finding 5).

    `_resolve_cwd` raises for an unknown, archived or folderless project — correctly, in the launch
    path. Calling it unconditionally while projecting the optional spawn fields put that refusal in
    front of an ordinary detail read, so a mission whose project had since been archived answered
    HTTP 500 and its timeline, roster and objectives became unreachable. The control was never going
    to be offered for that mission anyway; the cost of computing its absence was the whole record.
    """
    from agent_sessions import projects

    c, hdr, proj = api
    # THE MISSION MUST ACTUALLY REFERENCE THE PROJECT. An earlier draft of this test created a
    # project-less mission, where `_resolve_cwd` returns `(None, None)` without raising — so it
    # never reached the defect and would have passed against the unguarded code. Verified by
    # mutation: the unguarded version answered 200, not the 500 this test exists for.
    m = _create(c, hdr, project_id=proj.id)
    mid = m["id"]
    assert m["project_id"] == proj.id

    # The project goes away underneath a mission that still references it. `delete` is the
    # harshest form and the one an operator can actually reach; archived and folderless take the
    # same branch, since `_resolve_cwd` raises for all three.
    projects.delete(proj.id)

    r = c.get(f"/api/missions/{mid}", headers=hdr)
    assert r.status_code == 200, f"an archived project broke the mission read: {r.text[:200]}"
    body = r.json()
    assert body["id"] == mid
    assert body["spawn_cwd"] is None
    assert body["spawn_unavailable"], "the reason the offer is withheld is not reported"


def test_AT_CAP_the_detail_route_itself_reclaims_finished_children(api, monkeypatch):
    """The read path an operator actually triggers must break the at-cap deadlock (finding 4).

    Driving `_reap_dead_spawns` directly proves the mechanism and nothing about the wiring — it
    passes against the exact defect, because the defect is that the ROUTE never reaches that call
    once the count hits the cap and the console disables the only control that would. So this
    fills the cap, stops the children, and issues the plain `GET` the polling console issues.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, ptybridge, scopedspawn

    c, hdr, _proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    parent = "claude:" + "a" * 8

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        missions.settle_dispatch(mission_id, to="running", detail="dispatched", session_key=parent)
        return {"state": "running", "reason": "", "session_key": parent}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    # Reach `running` the way an operator does, so the mission has a real resolved cwd.
    m = _create(c, hdr)
    mid = m["id"]
    plan = c.post(f"/api/missions/{mid}/plan", headers=hdr).json()
    _ready_objectives(mid)
    r = c.post(
        f"/api/missions/{mid}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(mid),
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text

    for i in range(missions.SPAWN_CAP):
        missions.claim_spawn(
            mid, parent_key=parent, engine="claude", cwd="/repo", brief=f"child {i}"
        )
        missions.settle_dispatch(
            mid,
            to="running",
            detail="up",
            session_key=f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-6666-6666-6666-666666666666",
        )

    # ALIVE first: these keys have no real socket, and a missing socket probes DEAD, so without
    # this the route would reclaim them on the very first read and the test would assert nothing.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.ALIVE)
    at_cap = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert at_cap["spawn_live"] == missions.SPAWN_CAP, at_cap.get("spawn_live")

    # An UNKNOWN probe must NOT hand the capacity back — a starved host is not a stopped agent.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.UNKNOWN)
    still = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert still["spawn_live"] == missions.SPAWN_CAP, "UNKNOWN freed a live agent's slot"

    # The children finish. Nobody presses anything — at the cap there is nothing left to press.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.DEAD)

    after = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert after["spawn_live"] == 0, (
        "a refresh at the cap did not reclaim children that had stopped, so the operator is "
        "locked out of the only control that would have discovered it"
    )


def test_SPAWN_REFUSES_when_the_project_moved_under_the_panel(api, monkeypatch):
    """The spawn approval binds to a DIRECTORY, not to whatever resolves at the tap (review 1,
    finding 1).

    The route resolved the project server-side — which stops a client naming a path — and then
    never compared that value with anything the operator had seen. So the panel could promise a
    child alongside a parent in A, the project mapping could move to B, and START would launch in
    B while the screen still said A. Re-resolving under the fence does not close this: it pins the
    value from the tap onward, and the approval is older than the tap.

    `expect_cwd` is a COMPARAND — compared and discarded. The path that reaches the launcher is
    still the one the server resolved.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, projects, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)

    _plan_reply(monkeypatch)
    launched: list[dict] = []
    parent = "claude:" + "a" * 8

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        launched.append(plan)
        if plan.get("spawn_parent"):
            return {"state": "running", "reason": "", "session_key": None}
        missions.settle_dispatch(mission_id, to="running", detail="dispatched", session_key=parent)
        return {"state": "running", "reason": "", "session_key": parent}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    # Reach `running` the way an operator does, so the mission has a real resolved cwd.
    m = _create(c, hdr)
    mid = m["id"]
    plan = c.post(f"/api/missions/{mid}/plan", headers=hdr).json()
    _ready_objectives(mid)
    r = c.post(
        f"/api/missions/{mid}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(mid),
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert missions.get_mission(mid)["state"] == "running"
    launched.clear()

    # 1. NO ASSERTION AT ALL is refused, rather than silently resolved for you.
    r = c.post(
        f"/api/missions/{mid}/spawn",
        json={"parent_key": parent, "engine": "claude", "brief": "review it"},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert "expect_cwd" in r.text

    # 2. THE PROJECT MOVES between the panel rendering and the tap.
    moved = projects.update(proj.id, folders=["/somewhere/else"], default_folder="/somewhere/else")
    assert moved.default_folder == "/somewhere/else"

    r = c.post(
        f"/api/missions/{mid}/spawn",
        json={
            "parent_key": parent,
            "engine": "claude",
            "brief": "review it",
            "expect_cwd": "/the/directory/the/panel/showed",
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert not launched, "a spawn launched into a directory the operator never approved"


def test_DISPATCH_REFUSES_when_the_project_moved_under_the_plan(api, monkeypatch):
    """#904 review 5, tightened by review 2's finding 6.

    Re-resolving the project at the write boundary is required — the plan carries the cwd the
    entity meant when it was written, and launching an unattended agent into the
    directory a project USED to mean is `stale policy across the await` with a filesystem path on
    the end of it. But re-resolving ALONE is the same defect wearing the fix's clothes: the
    operator confirms `/old` on the card and the agent starts in `/new`.

    So the client asserts which resolution it showed, and a mismatch refuses rather than
    launching. Red against a route that re-resolves and proceeds.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, projects, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)

    m = _create(c, hdr)
    _ready_objectives(m["id"])
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    assert plan["cwd"] == proj.default_folder

    # THE PROJECT MOVES, after the proposal was written and before the button is pressed.
    moved = "/repo/moved-since"
    loaded = projects.load()
    entity = loaded[proj.id]
    monkeypatch.setattr(
        projects,
        "load",
        lambda: {
            **loaded,
            proj.id: entity.__class__(
                **{
                    **{f: getattr(entity, f) for f in entity.__dataclass_fields__},
                    "default_folder": moved,
                    "folders": (moved,),
                }
            ),
        },
    )

    seen: list[str] = []

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        seen.append(plan["cwd"])
        missions.settle_dispatch(mission_id, to="failed", detail="stubbed")
        return {"state": "failed", "reason": "stubbed", "session_key": None}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)
    # The operator approves the path the CARD showed, which is no longer where the project points.
    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert r.status_code == 409, r.text
    assert "resolves somewhere else" in r.json()["detail"]
    assert seen == [], "an agent started in a directory the operator never approved"
    # …and the plan is untouched, so re-reading it shows the new path and the next tap approves
    # THAT one — which then launches where the project actually points now.
    assert missions.get_mission(m["id"])["state"] == "planned"
    again = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": moved,
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert again.status_code == 200, again.text
    assert seen == [moved]


def test_TWO_TABS_editing_one_plan_do_not_lose_an_acknowledged_edit(api, monkeypatch):
    """#904 review 6, through the route. The edit reads the plan, changes one field and writes the
    whole row back, so two tabs holding the same proposal both get a 200 and a new id — and the
    later write restores its own stale copy of the field the first one changed.

    Red against a route that does not require, or does not compare, the id it was handed.
    """
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    a = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()

    first = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": a["plan_id"], "brief": "one, but better"},
        headers=hdr,
    )
    assert first.status_code == 200, first.text

    # The second tab is still holding A, and its body carries A's stale brief.
    second = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"plan_id": a["plan_id"], "engine": "claude"},
        headers=hdr,
    )
    assert second.status_code == 409, second.text
    assert "changed while you were editing" in second.json()["detail"]
    assert missions.get_plan(m["id"])["brief"] == "one, but better"


def test_an_EDIT_without_the_plan_it_edited_is_REFUSED(api, monkeypatch):
    """The id is required rather than optional, for the reason `from` is required on a state
    change: an omitted comparand is a write with no comparand, and the failure it permits is
    silent."""
    from agent_sessions import mission_plan

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    m = _create(c, hdr)
    c.post(f"/api/missions/{m['id']}/plan", headers=hdr)
    r = c.patch(f"/api/missions/{m['id']}/plan", json={"brief": "no id"}, headers=hdr)
    assert r.status_code == 422, r.text
    assert "plan_id is required" in r.json()["detail"]


def test_the_OFF_autonomy_tier_may_not_start_an_agent(api, monkeypatch):
    """#904 review 2, finding 1. `enabled` and `autonomy` are two switches and they say different
    things: the first is "the orchestrator runs at all", the second is what it may DO.

    The settings page states the `off` contract in the operator's own words — "watch and propose,
    never send anything" — and starting an unattended, permission-shaped agent is the largest
    thing this app can send. A route that read only `enabled` was doing the one thing that tier
    promises it will not.

    Red against a check that stops at `enabled`.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    # ENABLED, and OFF. The combination the check has to tell apart.
    prefs.set_orchestrator({"enabled": True, "autonomy": "off"})

    ran: list[str] = []

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        ran.append(mission_id)
        return {"state": "running", "reason": "", "session_key": None}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    m = _create(c, hdr)
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    r = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )

    assert r.status_code == 409, r.text
    assert "autonomy is off" in r.json()["detail"]
    assert ran == [], "an agent was launched under the tier that promises it will not"
    # …and the refusal is cheap: nothing was consumed, so the operator can raise the tier and
    # press the same button again.
    assert missions.get_mission(m["id"])["state"] == "planned"
    assert missions.get_plan(m["id"])["plan_id"] == plan["plan_id"]


def test_DISPATCH_refuses_a_mission_with_NO_OBJECTIVES(api, monkeypatch):
    """#904 review 4, finding 3, at the route — where the operator meets it.

    The only server gate rejected `objectives_state == "pending"`, so a settled-but-EMPTY
    checklist launched an unattended agent with nothing for the supervisor to follow through on.
    #893's acceptance invariant is that the mission knows what finishing means BEFORE it starts.

    The gate is deliberately not `objectives_state == "done"`: production settles `skipped` on an
    install with no AI endpoint and `failed` when the call breaks, and the operator writing the
    checklist by hand is the intended flow in both cases. So the second half of this test is as
    load-bearing as the first — keying on the producer's verdict would make DISPATCH permanently
    unreachable on those installs while proving nothing.

    Red against a route that refuses only `pending`.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    ran: list[str] = []

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        ran.append(mission_id)
        missions.settle_dispatch(mission_id, to="running", detail="dispatched")
        return {"state": "running", "reason": "", "session_key": None}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    m = _create(c, hdr)
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    # PRODUCTION FINISHED AND PRODUCED NOTHING — the case that used to dispatch.
    missions.settle_objectives_state(m["id"], "skipped")
    assert missions.get_mission(m["id"])["objectives"] == []
    empty = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert empty.status_code == 409, empty.text
    # THE ROUTE'S OWN REFUSAL, not the store's. `claim_plan(require_objectives=True)` refuses this
    # too — that is the fence that cannot be raced — but it answers "this mission has no
    # objectives" without saying what to do about it. The route exists here to name the remedy,
    # so the remedy is what this asserts; a route that dropped its check would fall through to
    # the store's wording and this line is what notices.
    assert "add at least one before dispatching" in empty.json()["detail"]
    assert ran == [], "an unattended agent started with nothing to check it against"
    # …and the plan survives the refusal, so the operator can add the checklist and press again.
    assert missions.get_mission(m["id"])["state"] == "planned"

    # THE HAND-WRITTEN CHECKLIST DISPATCHES, on a mission whose production was `skipped`.
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    ok = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert ok.status_code == 200, ok.text
    assert ran == [m["id"]]


def test_DISPATCH_refuses_a_checklist_that_is_not_the_one_you_read(api, monkeypatch):
    """#904 review 3, finding 5. #893 wants the objectives on the plan boundary, and the reason
    is a mobile one: on a phone they are a separate stop, so an operator could start an unattended
    agent without seeing — or noticing the absence of — what the supervisor will chase.

    Two ways that goes wrong, and both are refused here: the producer has not finished (the
    mission starts `pending`), and the set changed since the card rendered it.

    Red against a route that dispatches on plan/engine/brief alone.
    """
    from agent_sessions import mission_dispatch, mission_plan, prefs, scopedspawn

    c, hdr, proj = api
    monkeypatch.setattr(
        mission_plan, "engine_options", lambda: [{"id": "claude", "label": "claude"}]
    )
    _plan_reply(monkeypatch)
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    ran: list[str] = []

    async def fake_run(mission_id, plan, *, registry, policy_epoch=None, verify_cwd=None):
        ran.append(mission_id)
        missions.settle_dispatch(mission_id, to="failed", detail="stubbed")
        return {"state": "failed", "reason": "stubbed", "session_key": None}

    monkeypatch.setattr(mission_dispatch, "run", fake_run)

    m = _create(c, hdr)
    plan = c.post(f"/api/missions/{m['id']}/plan", headers=hdr).json()
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    shown = _obj_digest(m["id"])

    # 1. STILL BEING WORKED OUT — the producer has not settled the list. `pending` is the state
    #    a fresh mission is CREATED in, so it is set the way the store gets there rather than
    #    through the settle-only writer, which by design only accepts terminal values.
    _set_objectives_state(m["id"], "pending")
    pending = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": shown,
        },
        headers=hdr,
    )
    assert pending.status_code == 409, pending.text
    assert "still being worked out" in pending.json()["detail"]
    assert ran == []

    # 2. SETTLED, but the set moved since the card rendered it.
    missions.settle_objectives_state(m["id"], "done")
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "green", "title": "Checks are green", "gate": True}]
    )
    stale = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": shown,
        },
        headers=hdr,
    )
    assert stale.status_code == 409, stale.text
    assert "objectives changed" in stale.json()["detail"]
    assert ran == []

    # 3. …and the set the operator has now read dispatches.
    ok = c.post(
        f"/api/missions/{m['id']}/dispatch",
        json={
            "plan_id": plan["plan_id"],
            "expect_cwd": plan["cwd"],
            "expect_objectives": _obj_digest(m["id"]),
        },
        headers=hdr,
    )
    assert ok.status_code == 200, ok.text
    assert ran == [m["id"]]


def test_a_FABRICATED_session_cannot_be_adopted_or_carry_a_mission_to_running(api):
    """#896 review 11, finding 1. A key that is well FORMED is not a session.

    `_session_key` proves the shape; nothing proved existence. A syntactically perfect
    `claude:<uuid>` naming nothing at all was adopted, the `planned -> running` guard then found
    the row it had just written, and the mission reached `running` — "work is under way", with
    nothing behind it, for the supervisor to follow through on.

    Red against an adopt route that checks only the shape.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    fake = "claude:99999999-9999-4999-8999-999999999999"

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": fake}, headers=hdr)
    assert r.status_code == 404, r.text
    assert "no session" in r.json()["detail"]

    # …and the mission cannot reach `running` on it, because it never held it.
    c.post(f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "planned"}, headers=hdr)
    bad = c.post(
        f"/api/missions/{m['id']}/state", json={"from": "planned", "to": "running"}, headers=hdr
    )
    assert bad.status_code == 409, bad.text
    assert "adopt a session" in bad.json()["detail"]

    # The REAL one still works, so the check refuses fabrications rather than adoption.
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_an_ARCHIVED_session_cannot_be_adopted_or_carry_a_mission_to_running(api):
    """#896 review 15, finding 1. "The store knows it" is not "there is an agent there".

    `scanner.scan()` returns live AND archived rows — deliberately, because the sidebar lists both
    — so a session that has been put away satisfied the existence gate, and the mission reached
    `running` with nothing live or resumable behind it for the supervisor to follow through on.
    That is the same claim-with-nothing-behind-it the fabricated-key check was added to stop, one
    door along.

    Red against a gate that asks only whether the row exists.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    # The `fake_jsonl` fixture writes this one under `projects-archive/`, so the provider
    # genuinely returns it — this is not a stub of the door.
    archived = "claude:44444444-4444-4444-4444-444444444444"
    from agent_sessions import engines as _e

    prov, native = _e.parse_key(archived)
    assert any(
        getattr(s, "uuid", None) == native for s in prov.scan()
    ), "the fixture's archived session is not in the scan, so this test proves nothing"

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": archived}, headers=hdr)
    assert r.status_code == 404, r.text
    assert "no session" in r.json()["detail"]

    # …and the mission cannot reach `running` on it, because it never held it.
    c.post(f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "planned"}, headers=hdr)
    bad = c.post(
        f"/api/missions/{m['id']}/state", json={"from": "planned", "to": "running"}, headers=hdr
    )
    assert bad.status_code == 409, bad.text

    # The LIVE one still adopts, so the gate refuses archived sessions rather than adoption.
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_the_SIDECAR_archive_flag_is_enough_to_refuse_an_adoption(api):
    """The other half of finding 1, and the reason this asks `mission_archive` rather than the
    scan row alone: several engines have no on-disk archive tree at all, so app-archive is a
    SIDECAR flag and the provider keeps reporting the session as live. The sidecar override wins
    where it is set — the same precedence the sidebar, `pulse.build_cards` and `mission_archive`
    already use.

    Red against a check that reads only `row.archived`.
    """
    from agent_sessions import metadata

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    # Archived through the APP, which for this engine also moves the JSONL — but the sidecar is
    # what makes the answer engine-agnostic, so that is what is set here.
    metadata.patch(CLAUDE_A, archived=True)

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 404, r.text

    # …and unarchiving makes it adoptable again: the refusal is about the flag, not the key.
    metadata.patch(CLAUDE_A, archived=False)
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_a_LIVE_SESSION_the_ENGINE_HAS_NOT_WRITTEN_DOWN_YET_can_be_adopted(api, monkeypatch):
    """#896 review 22. "There is no row" and "we could not look" are opposite facts.

    The gate documents two independent proofs — a store record, or a live writer — because a
    session can legitimately be one without the other: a record survives a restart, and a session
    started moments ago has not written one yet. Several engines persist only after the first
    turn, so that state can last as long as the operator stays quiet.

    But it asked the archive question through a helper that collapses *no row* and *unreadable
    store* into one `None`, and refused on both. The live-writer proof was therefore unreachable,
    and a real UNTRACKED session could not be adopted at all — the exact control this PR adds.

    Red against a gate that treats an absent provider row as an unknown archive state.
    """
    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    # A UUID `fake_jsonl` does NOT lay down — 1111/2222/3333/5555 are live and 4444 is the
    # fixture's ARCHIVED one, so picking either would test something else entirely.
    fresh = "claude:66666666-6666-6666-6666-666666666666"
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": fresh}, headers=hdr)
    assert r.status_code == 200, r.text
    assert [s["session_key"] for s in missions.get_mission(m["id"])["sessions"]] == [fresh]

    # …and with NO writer either there is no proof at all, so it is still a 404. This accepts a
    # live session, not any well-formed key.
    m2 = _create(c, hdr, project_id=proj.id)
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: False)
    gone = c.post(
        f"/api/missions/{m2['id']}/adopt",
        json={"session_key": "claude:77777777-7777-7777-7777-777777777777"},
        headers=hdr,
    )
    assert gone.status_code == 404, gone.text


def test_an_UNREADABLE_ARCHIVE_TREE_still_refuses_even_with_a_live_writer(api, monkeypatch):
    """#896 reviews 23 and 24. `scan()` cannot answer the NEGATIVE half of the archive question.

    Every provider catches its own read failures and returns an empty or partial list — on purpose,
    so one bad directory cannot take the sidebar down. That makes "there is no archived row" and "I
    could not look" arrive identically, and a gate built on it accepted a live writer while an
    archived transcript may simply have been hidden by the failed read.

    So only that question gets its own reader, and only `claude` needs one: it is the engine that
    also MOVES the transcript on archive, so its tree can hold a fact the sidecar does not.

    The seam is real — the archive tree is made unreadable on disk. `stat` rather than `glob` or
    `is_file` is what makes that visible: both of those swallow a permission error and answer "no
    such file".

    Red against an archive question answered through `scan()`.
    """
    import os

    from agent_sessions import engines as _e
    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)
    prov, native = _e.parse_key(CLAUDE_A)
    tree = Path(os.environ["HOME"]) / ".claude" / "projects-archive"
    assert tree.is_dir(), "the fixture's archive tree is the premise of this test"

    os.chmod(tree, 0o000)
    try:
        assert prov.archive_state(native) == "unreadable"
        r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    finally:
        os.chmod(tree, 0o755)

    assert r.status_code == 404, r.text
    assert missions.active_session_keys(m["id"]) == []
    # …and once it is readable again the same request is fine, so this refuses the unreadable tree
    # rather than the session.
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_a_claude_ONE_SHOT_transcript_is_not_a_session_to_adopt(api, monkeypatch):
    """#896 review 24, finding 1. A `claude -p` transcript is not a session anyone can attach to.

    `scanner` skips them on an in-stream marker (`entrypoint: "sdk-cli"`) precisely because the
    usage probe writes one every few minutes. An earlier version of the store lookup matched on
    the FILENAME, so one of those underwrote an adoption — and the mission could then reach
    `running` over a process that had already exited.

    Existence is `scan()`'s question, and it answers it semantically. Red against a lookup that
    treats any correctly named file as a session.
    """
    import os

    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    probe = "77777777-7777-7777-7777-777777777777"
    d = Path(os.environ["HOME"]) / ".claude" / "projects" / "-home-user-claude-repo-a"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{probe}.jsonl").write_text(
        json.dumps({"type": "user", "entrypoint": "sdk-cli", "message": {"content": "/usage"}})
        + "\n"
    )
    # THE PREMISE, asserted: the scanner does not call this a session, so the only thing that
    # could have adopted it is a lookup that reads the filename instead.
    from agent_sessions import engines as _e

    prov, _n = _e.parse_key(CLAUDE_A)
    assert all(getattr(row, "uuid", None) != probe for row in prov.scan())
    assert prov.archive_state(probe) == "not-archived"
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: False)

    r = c.post(
        f"/api/missions/{m['id']}/adopt", json={"session_key": f"claude:{probe}"}, headers=hdr
    )
    assert r.status_code == 404, r.text
    assert missions.active_session_keys(m["id"]) == []

    # …AND THE SAME MARKER IN THE ARCHIVE TREE MUST NOT BLOCK A LIVE SESSION. The usage probe runs
    # every few minutes and its transcripts get archived like anything else; one sitting there is
    # not an archived SESSION, so a live master with that id is still adoptable. Without the
    # semantic read this is a permanent refusal for a session that is running right now.
    arch = Path(os.environ["HOME"]) / ".claude" / "projects-archive" / "-home-user-claude-old"
    arch.mkdir(parents=True, exist_ok=True)
    live = "12121212-1212-4121-8121-121212121212"
    (arch / f"{live}.jsonl").write_text(
        json.dumps({"type": "user", "entrypoint": "sdk-cli", "message": {"content": "/usage"}})
        + "\n"
    )
    assert prov.archive_state(live) == "not-archived"
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)
    ok = c.post(
        f"/api/missions/{m['id']}/adopt", json={"session_key": f"claude:{live}"}, headers=hdr
    )
    assert ok.status_code == 200, ok.text


def test_a_LIVE_WRITER_does_not_rescue_an_ARCHIVED_session(api, monkeypatch):
    """#896 review 16, finding 2. The archive question was asked SECOND, and `is_live` returned
    before it — so an archived session that still has a writer was adopted.

    That state is reachable rather than theoretical: the archive route suppresses runtime-cleanup
    failures and archives the record anyway, so a teardown that left a writer behind produces
    exactly it. The gate must not turn an archived record into a `running` mission because the
    teardown was incomplete.

    Red against a gate that checks `is_live` first.
    """
    from agent_sessions import metadata, session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    metadata.patch(CLAUDE_A, archived=True)
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 404, r.text
    assert missions.get_mission(m["id"])["sessions"] == []

    # …and a LIVE, unarchived session still adopts on the writer alone, so this refuses the
    # archive rather than the evidence.
    metadata.patch(CLAUDE_A, archived=False)
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_an_UNREADABLE_sidecar_REFUSES_the_adoption(api, monkeypatch):
    """#896 review 16, finding 3. `metadata.load()` collapses missing, corrupt and unreadable into
    `{}` — the right trade for the list surfaces, the wrong one at an authorization boundary.

    For the engines whose archive lives ONLY in the sidecar the provider row then reports
    `archived=False`, so the gate answered "not archived" precisely when it could not tell, and an
    archived session was adopted on the strength of a store nobody could read.

    Red against a gate that asks only `_effective_archived`, whose fall-through cannot distinguish
    "no sidecar entry" from "no readable sidecar".
    """
    from agent_sessions import metadata

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    # A sidecar that EXISTS and does not parse — the state `load()` cannot tell from an empty one.
    metadata._default_path().parent.mkdir(parents=True, exist_ok=True)
    metadata._default_path().write_text("{not json at all")
    assert metadata.archive_override_under_lock(CLAUDE_A) == "unreadable"
    assert metadata.load() == {}, "the premise: load() cannot see the difference"

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 404, r.text
    assert missions.get_mission(m["id"])["sessions"] == []

    # …and a readable sidecar adopts, so the refusal is about the unreadable store rather than
    # about the session.
    metadata._default_path().write_text("{}")
    assert metadata.archive_override_under_lock(CLAUDE_A) == "unset"
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


def test_a_MALFORMED_archive_FLAG_refuses_the_adoption(api):
    """#896 review 28, finding 2. `False` had two meanings and only one of them was true.

    `archive_override_under_lock` answered `False` — which this gate read as "definitively not
    archived", its one licence to continue — for a row that EXISTS and is damaged: a row that is
    not an object, or an `archived` that is not a boolean. That is not an absent override, it is a
    session whose archive state could not be determined; and for a sidecar-only engine the
    sidecar is the ONLY place the answer lives. A live writer then supplied the second proof and
    an archived session was adopted into a running mission on the strength of damage.

    Absence stays `False`, because a sidecar that has never been told about a session is not
    asserting anything about it. Damage is `None`, and `None` already fails closed here.

    Red against a gate whose store answers `False` for a value it cannot interpret.
    """
    from agent_sessions import metadata

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    metadata._default_path().parent.mkdir(parents=True, exist_ok=True)

    # A ROW THAT IS NOT A ROW. The ROUTE is asserted first in each case, deliberately: the
    # store's answer is the mechanism, and a regression that fails on the mechanism alone would
    # not say whether the boundary still held.
    metadata._default_path().write_text(json.dumps({CLAUDE_A: "archived"}))
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 404, r.text
    assert missions.get_mission(m["id"])["sessions"] == []
    assert (
        metadata.archive_override_under_lock(CLAUDE_A) == "unreadable"
    ), "…and this is why it refused"

    # A FLAG THAT IS NOT A BOOLEAN — the shape review 28 reproduced, and the one that reads most
    # like a real archive: a string where the writer meant a flag.
    metadata._default_path().write_text(json.dumps({CLAUDE_A: {"archived": "true"}}))
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert r.status_code == 404, r.text
    assert missions.get_mission(m["id"])["sessions"] == []
    assert (
        metadata.archive_override_under_lock(CLAUDE_A) == "unreadable"
    ), "…and this is why it refused"

    # …and a row with NO override is still an ordinary readable answer, so the refusal is about
    # the damage rather than about the row being there at all.
    metadata._default_path().write_text(json.dumps({CLAUDE_A: {"title": "a name"}}))
    assert metadata.archive_override_under_lock(CLAUDE_A) == "unset"
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text


# The opencode row whose archive lives in the ENGINE's own database (`time_archived` set), which
# this app is read-only against. Same fixture row `test_api` uses for the unarchive route.
OC_NATIVELY_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"


def test_an_UNARCHIVED_session_can_be_ADOPTED_even_where_the_ENGINE_still_says_archived(
    api, opencode_db
):
    """#896 review 29. `False` still had two meanings, and this was the other one.

    The sidecar answered `False` both for "nobody has recorded an override" and for an explicit
    `archived: false`, so the gate could not tell them apart and asked the ENGINE in both cases.
    For opencode that is fatal rather than merely redundant: this app does not write
    `opencode.db`, so UNARCHIVE deliberately sets the sidecar override and leaves the native
    `time_archived` exactly where it is. The session correctly leaves the archived list and joins
    the active one — and ADOPT went on refusing it for ever, against a native flag the operator
    had already overridden through the app's own supported route.

    The override winning where it is SET is the app's precedence everywhere else (the sidebar's
    `archived` column, `mission_archive`); consulting the engine after an explicit answer is a
    second opinion this gate has no business forming.

    Driven end to end through the real routes — unarchive, create, adopt — because the defect is
    in how they compose, not in any one of them.

    Red against a gate that consults the engine after an explicit `active`: the adopt 404s.
    """
    from agent_sessions import metadata

    c, hdr, proj = api
    key = f"opencode:{OC_NATIVELY_ARCHIVED}"

    # THE PREMISE: archived in the engine's own store, and nothing here will ever change that.
    arch = c.get("/api/sessions?engine=opencode&archived=1&limit=200").json()["sessions"]
    assert any(s["id"] == key for s in arch), "the fixture row is not natively archived"
    assert metadata.archive_override_under_lock(key) == "unset"

    # THE OPERATOR UNARCHIVES IT, through the route that exists for exactly this.
    r = c.post(f"/api/sessions/{key}/unarchive", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False
    assert metadata.archive_override_under_lock(key) == "active"
    active = c.get("/api/sessions?engine=opencode&archived=0&limit=200").json()["sessions"]
    assert any(s["id"] == key for s in active), "unarchive did not move it into the active list"
    # …and the ENGINE still says archived, which is the whole point.
    assert engines.archive_state(*engines.parse_key(key)) == "archived"

    # SO IT CAN BE ADOPTED.
    m = _create(c, hdr, project_id=proj.id)
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": key}, headers=hdr)
    assert ok.status_code == 200, ok.text
    assert [x["session_key"] for x in missions.get_mission(m["id"])["sessions"]] == [key]


def test_an_ARCHIVE_cannot_LAND_BETWEEN_the_eligibility_check_and_the_adopt(api, monkeypatch):
    """#896 review 19, finding 2. The check and the insert were two moments.

    `_session_exists` answered about the past and `missions.adopt` wrote the row afterwards, and
    the sibling archive route holds its own reservation only until the provider settles — so
    `eligible -> archive -> release -> adopt` inserted a session that had been archived in
    between, with nothing left in the adopt transaction to notice it. Every earlier archive gate
    in this file is a check, and a check cannot fence a window it has already left.

    So the route takes the reservation the archive routes already take, and holds it across both
    halves. The interleaving is driven at the exact instant it used to land: from inside
    `_session_exists`, past the answer, using `routes.sessions._reserve_or_refuse` — the first
    act of `POST /api/sessions/{id}/archive` and the only thing standing between it and
    `cleanup_runtime`.

    Red against an adopt that does not hold the reservation: the archive takes the mutex, the
    sidecar flips, and the mission ends up holding an archived session.
    """
    from fastapi import HTTPException

    from agent_sessions import metadata
    from agent_sessions.routes import missions as routes_missions
    from agent_sessions.routes import sessions as routes_sessions

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    real = routes_missions._session_exists
    refused: list[int] = []

    async def _archive_in_the_window(key: str) -> bool:
        answer = await real(key)
        # WHAT THE ARCHIVE ROUTE DOES FIRST. If it can take this, it goes on to tear the session
        # down and set the sidecar, and the adopt below writes a row for something that is gone.
        try:
            token = routes_sessions._reserve_or_refuse(key)
        except HTTPException as e:
            refused.append(e.status_code)
        else:
            metadata.patch(key, archived=True)
            missions.release_session(key, token)
        return answer

    monkeypatch.setattr(routes_missions, "_session_exists", _archive_in_the_window)
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)

    assert refused == [409], "the archive was allowed inside the adopt's own window"
    assert metadata.archive_override_under_lock(CLAUDE_A) == "unset"
    assert r.status_code == 200, r.text
    assert [s["session_key"] for s in missions.get_mission(m["id"])["sessions"]] == [CLAUDE_A]

    # …and the reservation is GIVEN BACK: an archive after the adopt has finished is refused by
    # mission ownership, not by a reservation nobody released.
    with pytest.raises(missions.SessionBusy) as e:
        missions.reserve_session(CLAUDE_A, "session-route")
    assert m["id"] in str(e.value) and "using it" in str(e.value)


def test_an_ADOPT_that_takes_ITS_TIME_still_holds_its_reservation(api, monkeypatch):
    """#896 review 20, finding 1. Taking a mutex is not holding one.

    A reservation is reclaimable after `RESERVATION_MAX_AGE_S` WITHOUT PROOF OF LIFE, and the
    eligibility scan in front of the insert is unbounded provider and filesystem work — a cold
    engine store, a loaded box. So the fence added in review 19 was really a five-minute bet: an
    archive could reclaim the reservation, archive the session, release it, and this insert would
    still land. `holding` beats on its own thread for exactly that, and the archive route already
    takes it; an expiry is a property of the WORK's duration, so both sides of a mutex have to
    defend against it or only one of them is fenced.

    Driven by AGEING the reservation from inside the scan — the same interleaving, made
    deterministic and instant — and asked through `_reserve_or_refuse`, the first act of
    `POST /api/sessions/{id}/archive`.

    Red against an adopt that reserves and then does not renew.
    """
    from fastapi import HTTPException

    from agent_sessions import metadata
    from agent_sessions.routes import missions as routes_missions
    from agent_sessions.routes import sessions as routes_sessions

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    real = routes_missions._session_exists
    refused: list[int] = []

    async def _slow_scan(key: str) -> bool:
        answer = await real(key)
        # THE SCAN TOOK LONGER THAN THE RESERVATION'S PATIENCE. Written straight into the row so
        # the test does not have to sleep for five minutes to ask the question.
        con = missions._ready(None)
        try:
            con.execute(
                "UPDATE session_reservations SET at=? WHERE session_key=?",
                (time.time() - missions.RESERVATION_MAX_AGE_S - 5, key),
            )
            con.commit()
        finally:
            con.close()
        # …and the heartbeat gets a chance to notice before anybody tries to take it. Bounded
        # by a beat that is actually observed, not by a bare sleep: the assertion below is about
        # the renewal, so waiting for it is the honest wait.
        deadline = time.time() + 5
        while time.time() < deadline:
            con = missions._ready(None)
            try:
                row = con.execute(
                    "SELECT at FROM session_reservations WHERE session_key=?", (key,)
                ).fetchone()
            finally:
                con.close()
            if row is not None and float(row["at"]) > time.time() - 5:
                break
            time.sleep(0.02)
        try:
            token = routes_sessions._reserve_or_refuse(key)
        except HTTPException as e:
            refused.append(e.status_code)
        else:
            metadata.patch(key, archived=True)
            missions.release_session(key, token)
        return answer

    # A BEAT FAST ENOUGH TO SEE. The production interval is 60s and this test is not going to
    # wait for it; the property is that a beat happens at all while the work runs.
    monkeypatch.setattr(missions, "RESERVATION_RENEW_S", 0.05)
    monkeypatch.setattr(routes_missions, "_session_exists", _slow_scan)
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)

    assert refused == [409], "the reservation aged out and the archive took it"
    assert metadata.archive_override_under_lock(CLAUDE_A) == "unset"
    assert r.status_code == 200, r.text
    assert [s["session_key"] for s in missions.get_mission(m["id"])["sessions"]] == [CLAUDE_A]


def test_an_ADOPT_that_LOST_its_reservation_does_not_commit_anyway(api, monkeypatch):
    """#896 review 21. A heartbeat reduces the chance of expiry; it is not a check for it.

    Review 20 kept the claim beating across the eligibility scan, which makes losing it unlikely
    and not impossible: the heartbeat's own writes can fail — a locked store, a busy disk — until
    the row ages out, and a rival then reclaims the reservation, archives the session and releases
    its own row. The holder check inside the adopt transaction is a check on a STRING, so it sees
    nothing, and the original operation attaches a session that has since been archived.

    The fencing TOKEN is what tells those apart: it changes on every reclaim, so requiring the
    exact one turns "nobody else holds it now" into "nobody has held it since I took it".

    Driven at the moment it happens — the beats fail, the claim is reclaimed, the archive lands
    and releases — all from inside the scan the adoption is waiting on.

    Red against an adopt that takes a reservation and never proves it still has it.
    """
    from agent_sessions import metadata
    from agent_sessions.routes import missions as routes_missions

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    beats: list[str] = []

    def _no_beat(session_key, token, *, path=None):
        beats.append(token)
        raise sqlite3.OperationalError("database is locked")

    real = routes_missions._session_exists

    async def _scan_that_loses_the_claim(key: str) -> bool:
        answer = await real(key)
        # WAIT FOR A BEAT TO HAVE BEEN TRIED, so "every beat failed" is a fact rather than a
        # hope. Without this the test raced the heartbeat thread and passed or failed on how
        # busy the box was — green alone, red in a full run.
        deadline = time.time() + 5
        while not beats and time.time() < deadline:
            time.sleep(0.01)
        # THE CLAIM AGES OUT while the scan is running, because every beat failed.
        con = missions._ready(None)
        try:
            con.execute(
                "UPDATE session_reservations SET at=? WHERE session_key=?",
                (time.time() - missions.RESERVATION_MAX_AGE_S - 5, key),
            )
            con.commit()
        finally:
            con.close()
        # A RIVAL takes it, archives, and gives it back — so by the time this adoption resumes
        # there is no competing row left for a holder check to notice.
        token = missions.reserve_session(key, "session-route")
        metadata.patch(key, archived=True)
        assert missions.release_session(key, token)
        return answer

    monkeypatch.setattr(missions, "RESERVATION_RENEW_S", 0.02)
    monkeypatch.setattr(missions, "renew_session", _no_beat)
    monkeypatch.setattr(routes_missions, "_session_exists", _scan_that_loses_the_claim)
    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)

    assert beats, "the heartbeat never ran, so nothing was lost and this proves nothing"
    assert r.status_code == 409, r.text
    assert "reservation" in r.json()["detail"]
    assert missions.active_session_keys(m["id"]) == [], "an archived session was adopted anyway"


def test_a_CLOSED_mission_cannot_ADOPT_a_session_back_into_itself(api):
    """#896 review 20, finding 2. A terminal transition RELEASES the roster — that is what it is
    for — so adopting into a closed mission puts an active session on something nobody follows
    through on: the supervisor will not nudge it, the board does not render it, and the mission
    reads finished while owning live work.

    `_fence_busy` covers archived and mid-operation missions and deliberately not this, because a
    terminal state is a legal resting place rather than an in-flight one. It needs its own
    refusal, and the refusal has to name the way out.

    At the STORE boundary, because the route is reachable without the console.

    Red against an adopt that fences only archived and in-flight missions.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    ok = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert ok.status_code == 200, ok.text
    c.post(f"/api/missions/{m['id']}/state", json={"from": "draft", "to": "planned"}, headers=hdr)
    c.post(f"/api/missions/{m['id']}/state", json={"from": "planned", "to": "running"}, headers=hdr)
    closed = c.post(
        f"/api/missions/{m['id']}/state",
        json={"from": "running", "to": "done", "outcome": "done"},
        headers=hdr,
    )
    assert closed.status_code == 200, closed.text
    # THE PREMISE: closing released the roster, so the session is genuinely loose again.
    assert missions.active_session_keys(m["id"]) == []

    again = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert again.status_code == 409, again.text
    assert "reopen" in again.json()["detail"]
    assert missions.get_mission(m["id"])["state"] == "done"
    assert missions.active_session_keys(m["id"]) == []

    # …and REOPENING is the way out the refusal names, so this refuses a state rather than the
    # adoption.
    back = c.post(
        f"/api/missions/{m['id']}/state", json={"from": "done", "to": "running"}, headers=hdr
    )
    assert back.status_code == 200, back.text
    fine = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    assert fine.status_code == 200, fine.text


def test_the_ENGINE_STORE_check_reads_the_rows_a_provider_actually_returns(api):
    """The helper the check above rests on, against the REAL provider.

    `_has_store_record` read `row.get("id")`, and every provider's `scan()` returns a
    `scanner.Session` dataclass — so it raised `AttributeError` on the first row, swallowed it in
    its own broad `except`, and answered "no evidence" for every session that has ever existed.
    Invisible because #898's tests stub `scan()` with dicts: a door production cannot reach.

    Red against the dict-only reader.
    """
    from agent_sessions import engines, headless_dispatch

    prov, native = engines.parse_key(CLAUDE_A)
    assert headless_dispatch._has_store_record(prov, native, "") is True
    assert (
        headless_dispatch._has_store_record(prov, "99999999-9999-4999-8999-999999999999", "")
        is False
    )


def test_a_CONCURRENT_sidecar_WRITE_does_not_make_an_archived_session_adoptable(api):
    """#896 review 17, finding 3. `patch()` truncates the sidecar in place and then serializes, so
    every ordinary edit has a window in which the file EXISTS and is ZERO BYTES.

    A lock-free reader in that window sees no archive override at all — and for the engines whose
    archive lives only in the sidecar, the provider row then reports `archived=False`. So an
    archived session was adoptable for as long as somebody else's write took, on a store that was
    perfectly healthy.

    Red against a reader that does not take the writer's flock: the truncated window reads as
    "no override" and the adoption is accepted.
    """
    import threading

    from agent_sessions import metadata

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    metadata.patch(CLAUDE_A, archived=True)

    started = threading.Event()
    release = threading.Event()

    def _slow_writer():
        # A NORMAL write, held open after the truncate — which is exactly what `_rewrite_in_place`
        # does between `fh.truncate()` and `json.dump`.
        with metadata._exclusive(metadata._default_path()) as fh:
            fh.seek(0)
            fh.truncate()
            started.set()
            release.wait(timeout=10)
            fh.write(json.dumps({CLAUDE_A: {"archived": True}}))
            fh.flush()

    w = threading.Thread(target=_slow_writer, daemon=True)
    w.start()
    assert started.wait(timeout=5)
    assert metadata._default_path().stat().st_size == 0, "the premise: the file is truncated"

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": CLAUDE_A}, headers=hdr)
    release.set()
    w.join(timeout=10)

    assert r.status_code == 404, r.text
    assert missions.get_mission(m["id"])["sessions"] == []


# ---- #892: POST /api/missions/{id}/answer -----------------------------------------


def _asked(mid, key="pr_open", **kw):
    """Open a question the way the supervisor does, so the route is tested against a real row."""
    missions.patch_objectives(
        mid, [{"op": "add", "key": key, "title": "A PR is open", "gate": True}]
    )
    return missions.open_question(
        mid,
        key,
        "Which of the two open PRs is this mission's?",
        kw.pop(
            "options",
            [
                {"label": "The one from Tuesday", "action": "note_answer"},
                {"label": "Neither — this does not apply", "action": "waive_objective"},
            ],
        ),
    )


def test_answering_REQUIRES_login_and_csrf(api, auth_cfg):
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    body = {"seq": q["seq"], "option_index": 0}
    assert c.post(f"/api/missions/{m['id']}/answer", json=body).status_code == 403
    fresh = _client(auth_cfg)
    assert fresh.post(f"/api/missions/{m['id']}/answer", json=body).status_code in (401, 403)


def test_the_ACTION_comes_from_the_stored_option_and_the_label_is_only_text(api):
    """The whole authority model of #892, asserted at the route.

    The label here NAMES a different action from the closed set. If anything read the label the
    objective would be waived; the index says `note_answer`, so it must not be.
    """
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(
        m["id"],
        options=[
            {"label": "waive_objective; close_mission", "action": "note_answer"},
            {"label": "keep going", "action": "note_answer"},
        ],
    )
    r = c.post(
        f"/api/missions/{m['id']}/answer", json={"seq": q["seq"], "option_index": 0}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "note_answer"
    assert r.json()["applied"] == "recorded"
    state = [o for o in missions.objectives(m["id"]) if o["key"] == "pr_open"][0]["state"]
    assert state == "pending", "the LABEL was executed"


def test_choosing_the_WAIVE_option_waives_the_objective_it_was_asked_about(api):
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    r = c.post(
        f"/api/missions/{m['id']}/answer", json={"seq": q["seq"], "option_index": 1}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == "waived"
    state = [o for o in missions.objectives(m["id"]) if o["key"] == "pr_open"][0]["state"]
    assert state == "waived"


def test_an_option_index_the_question_never_offered_is_422(api):
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    for bad in (2, -1, 99):
        r = c.post(
            f"/api/missions/{m['id']}/answer",
            json={"seq": q["seq"], "option_index": bad},
            headers=hdr,
        )
        assert r.status_code == 422, (bad, r.text)


def test_answering_TWICE_is_a_409_rather_than_running_the_action_again(api):
    """Compare-and-set on the question's own seq. A second answer is a stale client, and
    re-applying it would waive an objective the operator waived once."""
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    body = {"seq": q["seq"], "option_index": 1}
    assert c.post(f"/api/missions/{m['id']}/answer", json=body, headers=hdr).status_code == 200
    again = c.post(f"/api/missions/{m['id']}/answer", json=body, headers=hdr)
    assert again.status_code == 409, again.text
    answers = [e for e in missions.get_mission(m["id"])["events"] if e["kind"] == "answer"]
    assert len(answers) == 1


def test_FREE_TEXT_is_recorded_and_is_never_delivered_to_a_session(api):
    """An answer is an ANSWER. Making it agent input would be a second, unfenced path to a PTY —
    delivery stays the composer's job."""
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    before = [e["seq"] for e in missions.get_mission(m["id"])["events"]]
    r = c.post(
        f"/api/missions/{m['id']}/answer",
        json={"seq": q["seq"], "text": "use the Tuesday one"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "note_answer" and r.json()["applied"] == "recorded"
    events = missions.get_mission(m["id"])["events"]
    answer = [e for e in events if e["kind"] == "answer"][0]
    assert "Tuesday" in (answer.get("text") or "")
    # Asserted on what the ANSWER added, not on what the timeline contains: creating a mission
    # already records the operator's instruction, so "no operator_msg on the mission" would be a
    # test of `create_mission` that passes for the wrong reason.
    added = [e["kind"] for e in events if e["seq"] not in before]
    assert added == ["answer"], added


def test_an_answer_with_NEITHER_an_option_nor_text_is_422(api):
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    r = c.post(f"/api/missions/{m['id']}/answer", json={"seq": q["seq"]}, headers=hdr)
    assert r.status_code == 422, r.text


def test_a_missing_or_MISTYPED_seq_is_422_rather_than_answering_whatever_is_open(api):
    """The client states WHICH question it is answering. Without that the route would answer
    whatever happens to be open when the request lands, which is the check-then-act shape."""
    c, hdr, _ = api
    m = _create(c, hdr)
    _asked(m["id"])
    for body in ({"option_index": 0}, {"seq": "1", "option_index": 0}, {"seq": True}):
        r = c.post(f"/api/missions/{m['id']}/answer", json=body, headers=hdr)
        assert r.status_code == 422, (body, r.text)


def test_an_OVERLONG_free_text_answer_is_refused_before_it_becomes_a_durable_event(api):
    c, hdr, _ = api
    m = _create(c, hdr)
    q = _asked(m["id"])
    r = c.post(
        f"/api/missions/{m['id']}/answer",
        json={"seq": q["seq"], "text": "x" * (missions.QUESTION_TEXT_MAX + 1)},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert not [e for e in missions.get_mission(m["id"])["events"] if e["kind"] == "answer"]


def test_an_UNREADABLE_question_is_an_ERROR_not_a_mission_without_one(api, monkeypatch):
    """#900 review 5, finding 6. The contract is that the attention flag and the thing the
    operator has to do about it cannot disagree — and swallowing this read produced exactly that
    disagreement with a 200: the rail kept saying "needs an answer" while the console removed the
    only way to answer it, and nothing on screen said a read had failed.

    Red against a `contextlib.suppress` around the attention read.

    Patched at `_open_question_row`, which is the query `get_mission(attention=True)` actually
    runs — the flag, the question and the timeline come from ONE transaction now (review 8,
    finding 1), so there is no separate read left on this route to fail.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)

    def boom(*a, **k):
        raise OSError("the question store could not be read")

    monkeypatch.setattr(missions, "_open_question_row", boom)
    r = c.get(f"/api/missions/{m['id']}", headers=hdr)
    # AN EXPLICIT DEGRADED CONTRACT, not a 200 with the field quietly missing.
    assert r.status_code == 503, r.text
    assert "could not be read" in r.json()["detail"]


def test_a_QUESTION_OPENED_MID_READ_never_arrives_without_its_own_event(api, monkeypatch):
    """#900 review 8, finding 1. The previous fix put the FLAG and the QUESTION in one
    transaction, and they agreed with each other — while disagreeing with the timeline returned
    beside them. A question opening between the mission read and the attention read produced a
    200 carrying an actionable question whose own event was not in `events`: an answer the
    console could offer and then not show.

    The interleaving is injected at the attention read itself, which is the gap that used to
    exist. With one snapshot the question is simply not visible yet — `question: null` beside a
    timeline that does not carry it, which is consistent — and the next poll shows both.

    Red against a second transaction for the attention projection.
    """
    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr_open", "title": "A PR is open", "gate": True}]
    )

    real = missions._attention_rows
    fired: list[int] = []

    def open_then_read(con, ids):
        if not fired:
            fired.append(1)
            # A DIFFERENT connection, exactly as the supervisor's own producer would be.
            missions.open_question(
                m["id"],
                "pr_open",
                "Which of the two open PRs is this mission's?",
                [
                    {"label": "The one from Tuesday", "action": "note_answer"},
                    {"label": "Neither", "action": "waive_objective"},
                ],
            )
        return real(con, ids)

    monkeypatch.setattr(missions, "_attention_rows", open_then_read)
    r = c.get(f"/api/missions/{m['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert fired, "the interleaving never ran, so this test proves nothing"

    q = body.get("question")
    seqs = {e["seq"] for e in body.get("events") or []}
    assert (
        q is None or q["seq"] in seqs
    ), "the response offered a question whose own event it did not carry"
    # …and the flag agrees with what was returned, not with what landed mid-read.
    assert ("question" in (body.get("needs_you_why") or [])) == (q is not None)

    # THE NEXT READ carries all three, so nothing is lost — only deferred to a consistent answer.
    monkeypatch.setattr(missions, "_attention_rows", real)
    later = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert later["question"] is not None
    assert later["question"]["seq"] in {e["seq"] for e in later["events"]}
    assert "question" in later["needs_you_why"]


def test_a_CONCURRENT_playbook_save_is_refused_rather_than_overwriting(api):
    """#900 review 5, finding 7. A whole-block write with no comparand is last-writer-wins over
    everything another tab did — a playbook it added, a probe target it fixed, a gate it set —
    deleted silently, with both operators told the save worked.

    Red against a `set_mission_playbooks` that ignores the revision it was handed.
    """
    from agent_sessions import prefs

    c, hdr, proj = api
    first = c.get("/api/config", headers=hdr).json()["mission_playbooks"]
    rev = first["revision"]

    # TAB A saves, and the revision moves.
    a = c.post(
        "/api/prefs",
        json={
            "mission_playbooks": {
                "default_id": "",
                "playbooks": [{"id": "from_a", "label": "A", "objectives": []}],
                "revision": rev,
            }
        },
        headers=hdr,
    )
    assert a.status_code == 200, a.text
    assert a.json()["mission_playbooks"]["revision"] == rev + 1

    # TAB B still holds the OLD revision, and its block does not contain A's playbook.
    b = c.post(
        "/api/prefs",
        json={
            "mission_playbooks": {
                "default_id": "",
                "playbooks": [{"id": "from_b", "label": "B", "objectives": []}],
                "revision": rev,
            }
        },
        headers=hdr,
    )
    assert b.status_code == 409, b.text
    assert "another tab" in b.json()["detail"]

    # A'S WORK SURVIVES, which is the whole point.
    now = prefs.get_mission_playbooks()
    assert [p["id"] for p in now["playbooks"]] == ["from_a"]

    # …and B can save once it has read the current revision.
    ok = c.post(
        "/api/prefs",
        json={
            "mission_playbooks": {
                "default_id": "",
                "playbooks": [{"id": "from_b", "label": "B", "objectives": []}],
                "revision": now["revision"],
            }
        },
        headers=hdr,
    )
    assert ok.status_code == 200, ok.text


def test_a_playbook_save_with_NO_revision_is_refused_at_the_route(api):
    """#900 review 6, finding 2. `None` means "no comparand" and exists for the installer and the
    shipped defaults, which have nothing to compare against — but over HTTP it made the whole
    concurrency check OPTIONAL: an authenticated stale client, including an older cached PWA
    build, could omit the field and overwrite a newer block wholesale.

    Red against a route that passes `None` through when the field is absent or mistyped.
    """
    c, hdr, proj = api
    block = {"default_id": "", "playbooks": [{"id": "x", "label": "X", "objectives": []}]}

    missing = c.post("/api/prefs", json={"mission_playbooks": block}, headers=hdr)
    assert missing.status_code == 422, missing.text
    assert "revision is required" in missing.json()["detail"]

    for bad in ("3", True, None, 1.5):
        r = c.post(
            "/api/prefs",
            json={"mission_playbooks": {**block, "revision": bad}},
            headers=hdr,
        )
        assert r.status_code == 422, (bad, r.text)

    # …and a real one works.
    rev = c.get("/api/config", headers=hdr).json()["mission_playbooks"]["revision"]
    ok = c.post("/api/prefs", json={"mission_playbooks": {**block, "revision": rev}}, headers=hdr)
    assert ok.status_code == 200, ok.text


# =======================================================================================


def test_a_LIVE_session_whose_engine_PINS_THE_ID_FIRST_can_be_adopted(api, monkeypatch):
    """#896 review 24, finding 1, the opposite false answer.

    Gemini starts with a caller-pinned id BEFORE it writes its chat file, so "the store has no row"
    is the ordinary state of a fresh live session on that engine — not a hint that something could
    not be read. Reporting it as "cannot tell" refused a real live master outright, which is the
    very capability this gate was widened to allow.

    The fix is to stop asking the store an existence question it cannot answer negatively: the
    store proves existence POSITIVELY (a semantically valid row), the live writer proves it
    otherwise, and the only thing the store must answer three ways is whether it has ARCHIVED the
    session — which, for an engine with no archive tree, is a definite no.

    Red against a lookup that answers `unreadable` for every no-row result.
    """
    from agent_sessions import session_input

    c, hdr, proj = api
    m = _create(c, hdr, project_id=proj.id)
    # Nothing on disk for this one: gemini has written no chat file yet.
    fresh = "gemini:88888888-8888-8888-8888-888888888888"
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: True)

    r = c.post(f"/api/missions/{m['id']}/adopt", json={"session_key": fresh}, headers=hdr)
    assert r.status_code == 200, r.text
    assert [s["session_key"] for s in missions.get_mission(m["id"])["sessions"]] == [fresh]

    # …and with NO writer there is no proof at all, so it is still a 404.
    m2 = _create(c, hdr, project_id=proj.id)
    monkeypatch.setattr(session_input, "is_live", lambda *a, **k: False)
    gone = c.post(
        f"/api/missions/{m2['id']}/adopt",
        json={"session_key": "gemini:99999999-9999-4999-8999-999999999999"},
        headers=hdr,
    )
    assert gone.status_code == 404, gone.text
