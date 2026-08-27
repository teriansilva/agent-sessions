"""Mission routes + the bounded DB worker (#846, Phase 1 of #840).

Two things are pinned here that the store tests cannot reach: the **route contract** (auth, CSRF,
status codes, filter-before-paginate) and the **admission bound** — the part that makes "off the
event loop" an honest claim rather than a comment. A bounded pool bounds running threads, not the
queue; without admission above it a flood is experienced as a hang, and with a five-second SQLite
``busy_timeout`` that hang is minutes long.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from agent_sessions import missions, projects
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
def api(auth_cfg, tmp_home, tmp_path):
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
    assert [e["kind"] for e in row["events"]] == ["operator_msg"]
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
