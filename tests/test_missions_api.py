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
import threading

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
    row = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert "supervisor" in row, "the console has nothing to render the boards from"
    sup = row["supervisor"]
    assert [o["key"] for o in sup["objectives"]] == ["checks_green"]
    o = sup["objectives"][0]
    assert o["episode"] == 1 and o["remaining"] > 0 and o["may_nudge"] is True
    assert sup["likely_done"] is False and sup["unmet_gates"] == 1


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
