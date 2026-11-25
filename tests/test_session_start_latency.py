"""#1505: an API session's page opens on launch, not on ready; every start logs its stage timings.

The HTTP create/start answer once the session is recorded and its worker launched
(`wait_ready=False`, routes only). A background waiter applies today's exact failure handling and
settles the snapshot's generation-scoped `launch` view; shutdown cancels it without settling, and
the next app start reconciles from the worker's own lifecycle and liveness. Real workers run
against the scripted natives (`fake_native.py`) under the substitute service manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import pytest

from agent_sessions import launchtiming, native_ownership, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from test_native_clients import app_client  # noqa: F401 — fixture
from test_native_runtime import ENGINES, host, ident, project  # noqa: F401 — fixtures

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_sessions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client(host, app_client):  # noqa: F811 — the host is substituted BEFORE the app exists
    return app_client


def _poll(read, done, timeout=30.0):
    deadline = time.monotonic() + timeout
    value = read()
    while not done(value):
        assert time.monotonic() < deadline, f"never settled: {value}"
        time.sleep(0.05)
        value = read()
    return value


async def _until(read, done, timeout=30.0):
    deadline = time.monotonic() + timeout
    value = await read()
    while not done(value):
        assert time.monotonic() < deadline, f"never settled: {value}"
        await asyncio.sleep(0.05)
        value = await read()
    return value


def _hold(project: Path) -> None:  # noqa: F811
    (project / "hold-initialize").touch()


def _release(project: Path) -> None:  # noqa: F811
    (project / "release-initialize").touch()


def _break_launches(host, monkeypatch):  # noqa: F811
    """The worker finds no config and refuses: launched, never ready (the real failure path)."""
    real = host.launch

    def broken(argv):
        argv = list(argv)
        argv[argv.index("--worker-id") + 1] = ident()
        return real(argv)

    monkeypatch.setattr(host, "launch", broken)
    return real


async def _waiters() -> None:
    while native_runtime._READINESS_TASKS:
        await asyncio.gather(*list(native_runtime._READINESS_TASKS), return_exceptions=True)


# --- stage timings -----------------------------------------------------------------------------


def test_the_timing_line_carries_stages_and_nothing_else(caplog):
    rec = launchtiming.Recorder("claude-api", "create", started=100.0)
    rec._last = 100.0
    rec._stages = [("model", 3.2), ("spawn", 41.0), ("ready", 1702.4)]
    rec._last = 101.7466
    line = rec.line()
    assert line == (
        "launch timings engine=claude-api op=create total=1747ms stages=model:3,spawn:41,ready:1702"
    )
    # Anything that is not a short identifier never reaches the line: no path, no prompt.
    unsafe = launchtiming.Recorder("/home/me/secret", "create")
    unsafe.mark("cwd=/home/me/secret")
    assert "/home" not in unsafe.line(where="/home/me/x") and "engine=unknown" in unsafe.line()
    with caplog.at_level(logging.INFO, logger="agent_sessions.launchtiming"):
        rec.emit()
        rec.emit()  # once
    assert [r.getMessage() for r in caplog.records].count(line) == 1


def test_marks_without_a_start_are_no_ops():
    launchtiming._CURRENT.set(None)
    launchtiming.mark("anything")  # never raises, records nowhere
    with launchtiming.stage("block"):
        pass
    assert launchtiming.current() is None


def test_only_the_http_routes_skip_readiness():
    """Every internal caller keeps the default `wait_ready=True`: only the two route handlers
    pass False, so no mission/Ask/playbook path changes meaning. Read from the call sites."""
    import ast
    import inspect

    found = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "wait_ready" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is True
                ):
                    found.setdefault(str(path.relative_to(SRC)), []).append(ast.unparse(kw.value))
    # The facade and adapter forward the caller's value; only the routes choose False.
    assert found == {
        "routes/structured.py": ["False", "False"],
        "structured_runtime.py": ["wait_ready", "wait_ready", "wait_ready", "wait_ready"],
    }
    for fn in (
        runtime.create_session,
        runtime.start_session,
        native_runtime.create,
        native_runtime.start,
    ):
        assert inspect.signature(fn).parameters["wait_ready"].default is True


def test_the_lease_sweep_runs_at_most_once_a_minute(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "r1"
    (release / native_state.LEASES).mkdir(parents=True)
    (release / native_state.LEASES / ident()).write_text("")
    asked = []
    monkeypatch.setattr(native_runtime, "_proved_gone", lambda w: asked.append(w) or False)
    monkeypatch.setattr(native_runtime, "_SWEPT_AT", None)
    native_runtime._sweep_leases(release)
    native_runtime._sweep_leases(release)  # inside the interval: no walk at all
    assert len(asked) == 1
    monkeypatch.setattr(native_runtime, "_SWEPT_AT", time.monotonic() - 61)
    native_runtime._sweep_leases(release)
    assert len(asked) == 2
    native_runtime._sweep_leases(None)  # no release to retain: nothing, and no throttle stamp
    assert len(asked) == 2


# --- the routes return at launch -----------------------------------------------------------------


def test_the_create_route_returns_before_ready_and_the_snapshot_turns_ready(
    client,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    caplog,
):
    seen = []
    real_create = native_runtime.create

    async def spy(*args, **kwargs):
        seen.append(kwargs.get("wait_ready"))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "create", spy)
    _hold(project)
    with caplog.at_level(logging.INFO, logger="agent_sessions.launchtiming"):
        r = client.post(
            "/api/structured/sessions",
            json={"engine": ENGINES["codex"], "cwd": str(project), "operation_id": ident()},
        )
        assert r.status_code == 201, r.text
        snap = r.json()
        # Answered while the worker cannot be ready: it is still held in `initialize`.
        assert seen == [False]  # route → facade → adapter → native_runtime
        assert snap["launch"]["state"] == "starting" and snap["launch"]["gen"]
        gen = snap["launch"]["gen"]
        assert native_state.read_lifecycle(gen).get("phase") != "ready"
        assert "reason" not in snap["launch"] and len(host.launches) == 1
        _release(project)
        key = snap["session_key"]
        snap = _poll(
            lambda: client.get(f"/api/structured/sessions/{key}").json(),
            lambda s: s["launch"]["state"] != "starting",
        )
    assert snap["launch"] == {**snap["launch"], "gen": gen, "state": "ready"}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("launch timings")]
    assert len(lines) == 1 and "op=create" in lines[0] and "ready:" in lines[0]
    assert str(project) not in lines[0] and key.partition(":")[2] not in lines[0]
    # The facade's own default still waits: an internal caller is unchanged.
    seen.clear()
    asyncio.run(_facade_create(project))
    assert seen == [True]


async def _facade_create(project):  # noqa: F811
    snap = await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())
    assert "launch" not in snap  # no view on the waiting path: byte-for-byte today's record
    await runtime.stop(snap["session_key"])


def test_the_start_route_returns_at_launch_with_no_start_offered(
    client,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    seen = []
    real_start = native_runtime.start

    async def spy(*args, **kwargs):
        seen.append(kwargs.get("wait_ready"))
        return await real_start(*args, **kwargs)

    monkeypatch.setattr(native_runtime, "start", spy)
    created = client.post(
        "/api/structured/sessions",
        json={
            "engine": ENGINES["codex"],
            "cwd": str(project),
            "operation_id": ident(),
            "bypass": True,
        },
    ).json()
    key = created["session_key"]
    assert created["pending_start"] is True and "launch" not in created
    _hold(project)
    r = client.post(f"/api/structured/sessions/{key}/start")
    assert r.status_code == 200, r.text
    snap = r.json()
    assert seen == [False]
    assert snap["launch"]["state"] == "starting"
    # Starting is not "nothing has run": no Start, no incomplete/failed copy.
    assert snap["pending_start"] is False and snap["start_in_flight"] is True
    assert "start_incomplete" not in snap and "start_failed" not in snap
    _release(project)
    snap = _poll(
        lambda: client.get(f"/api/structured/sessions/{key}").json(),
        lambda s: s["launch"]["state"] != "starting",
    )
    assert snap["launch"]["state"] == "ready" and snap["pending_start"] is False
    assert native_state.read_session(key.partition(":")[2])["start"]["state"] == "started"


# --- failures: today's handling, then the owned-generation view ----------------------------------


def test_a_never_ready_guarded_create_is_stopped_discharged_then_failed(
    client,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    _break_launches(host, monkeypatch)
    r = client.post(
        "/api/structured/sessions",
        json={"engine": ENGINES["codex"], "cwd": str(project), "operation_id": ident()},
    )
    assert r.status_code == 201  # answered at launch; the failure arrives through the snapshot
    key = r.json()["session_key"]
    snap = _poll(
        lambda: client.get(f"/api/structured/sessions/{key}").json(),
        lambda s: s["launch"]["state"] != "starting",
    )
    assert snap["launch"]["state"] == "failed" and snap["launch"]["reason"]
    assert len(snap["launch"]["reason"]) <= native_runtime.LAUNCH_REASON_MAX
    assert native_runtime.UNCONFIRMED_STOP not in snap["launch"]["reason"]
    # Stopped first (containment proved), reservation released: console launches are not held.
    record = native_state.read_session(key.partition(":")[2])
    assert record["closed"] is True and record["workers"] == []
    assert native_ownership.lookup(key) is None
    assert client.get(f"/api/structured/sessions/{key}/containment").json()["containment"] == "gone"
    # A guarded creation has no start state: nothing offers Start again.
    assert snap["pending_start"] is False and "start_failed" not in snap


@pytest.mark.anyio
async def test_an_unconfirmed_stop_says_so_and_leaves_the_record_as_the_stop_left_it(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    _break_launches(host, monkeypatch)
    stopped = []

    def unconfirmed(session_id, record, worker_id):
        stopped.append(worker_id)
        return "unknown"

    monkeypatch.setattr(native_runtime, "_stop_generation_locked", unconfirmed)
    snap = await runtime.create_session(
        ENGINES["codex"], str(project), operation_id=ident(), wait_ready=False
    )
    await _waiters()
    op = snap["session_key"].partition(":")[2]
    record = native_state.read_session(op)
    assert stopped == [snap["launch"]["gen"]]  # only its own generation, never the session
    assert record["launch"]["state"] == "failed"
    assert record["launch"]["reason"].endswith(native_runtime.UNCONFIRMED_STOP)
    # As an unconfirmed Stop leaves it: admission closed, the generation still listed (not
    # proved gone), so the reservation is NOT discharged.
    assert record["closed"] is True and record["workers"] == [snap["launch"]["gen"]]
    assert native_ownership.lookup(snap["session_key"]) is not None


@pytest.mark.anyio
async def test_a_failed_generation_never_cleans_up_its_replacement(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (1): generation A's waiter fails AFTER `/start` installed replacement B.
    A's cleanup is fenced before any side effect: B keeps running, the session stays open, and B
    settles ready."""
    op = ident()
    key = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    real = native_runtime._wait_ready
    first: dict = {}
    replaced = asyncio.Event()

    async def gated(worker_id):
        first.setdefault("gen", worker_id)
        if worker_id == first["gen"]:
            await replaced.wait()  # A's failure arrives only after B started
            raise native_runtime.NativeError(503, "the native worker stopped: A died")
        return await real(worker_id)

    monkeypatch.setattr(native_runtime, "_wait_ready", gated)
    _hold(project)
    a = (await runtime.start_session(key, wait_ready=False))["launch"]["gen"]
    await _until(
        lambda: asyncio.to_thread(native_state.read_lifecycle, a),
        lambda life: life.get("phase") not in (None, "intent"),
    )
    host.kill(a)  # A dies …
    # … and is contained (admission closed, proved gone) without discarding the start, so a
    # Start again may replace it while A's waiter has not run its failure handling yet.
    await native_runtime.stop(ENGINES["codex"], op, discard=False)
    b = (await runtime.start_session(key, wait_ready=False))["launch"]["gen"]
    assert b != a
    _release(project)
    replaced.set()
    await _waiters()
    record = native_state.read_session(op)
    assert record["closed"] is False and record["current_worker"] == b
    assert record["start"] == {**record["start"], "state": "started", "gen": b}
    assert record["launch"]["gen"] == b and record["launch"]["state"] == "ready"
    assert (await runtime.probe(key))["containment"] == "live"  # B was never stopped
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_request_cancelled_mid_launch_still_hands_its_generation_to_a_waiter(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (2): cancelling the request does not stop the launch thread. The
    generation it launches still gets its waiter, so the view settles instead of reading
    Starting forever."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real = host.launch

    def slow(argv):
        entered.set()
        release.wait(30)
        return real(argv)

    monkeypatch.setattr(host, "launch", slow)
    op = ident()
    request = asyncio.create_task(
        native_runtime.create(ENGINES["codex"], str(project), session_id=op, wait_ready=False)
    )
    await asyncio.to_thread(entered.wait, 30)
    request.cancel()  # the client went away while the thread is still launching
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    await _until(
        lambda: asyncio.to_thread(native_state.read_session, op),
        lambda r: (r or {}).get("launch", {}).get("state") not in (None, "starting"),
    )
    record = native_state.read_session(op)
    assert record["launch"]["state"] == "ready"
    await _waiters()
    await runtime.stop(f"{ENGINES['codex']}:{op}")


@pytest.mark.anyio
async def test_a_transient_read_error_does_not_kill_the_readiness_waiter(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (3): one failed lifecycle read used to end the only owner of a launch."""
    monkeypatch.setattr(native_runtime, "LAUNCH_RETRY_DELAYS", (0.05,) * 5, raising=False)
    _hold(project)
    op = ident()
    key = (
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=op, wait_ready=False
        )
    )["session_key"]
    real = native_state.read_lifecycle
    armed = {"n": 1}

    def flaky(worker_id):
        if armed["n"]:
            armed["n"] -= 1
            raise native_state.StateError("lifecycle temporarily unreadable")
        return real(worker_id)

    monkeypatch.setattr(native_state, "read_lifecycle", flaky)
    await _until(lambda: asyncio.sleep(0.05, armed["n"]), lambda n: n == 0)
    _release(project)
    await _waiters()
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_transient_error_during_reconciliation_is_retried(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (3): the startup pass used to log one error per record and never retry."""
    monkeypatch.setattr(native_runtime, "LAUNCH_RETRY_DELAYS", (0.05,) * 5, raising=False)
    op, key, gen = await _starting_after_shutdown(project)
    _release(project)
    await _phase(gen, "ready")
    real = native_runtime._restart_verdict
    armed = {"n": 1}

    def flaky(worker_id):
        if armed["n"]:
            armed["n"] -= 1
            raise native_state.StateError("lifecycle temporarily unreadable")
        return real(worker_id)

    monkeypatch.setattr(native_runtime, "_restart_verdict", flaky)
    await native_runtime.reconcile_launches()
    await _waiters()
    assert armed["n"] == 0
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    await runtime.stop(key)


def test_a_failed_skip_start_reads_failed_and_starts_again(
    client,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    created = client.post(
        "/api/structured/sessions",
        json={
            "engine": ENGINES["codex"],
            "cwd": str(project),
            "operation_id": ident(),
            "bypass": True,
        },
    ).json()
    key = created["session_key"]
    real = _break_launches(host, monkeypatch)
    assert client.post(f"/api/structured/sessions/{key}/start").status_code == 200
    snap = _poll(
        lambda: client.get(f"/api/structured/sessions/{key}").json(),
        lambda s: s["launch"]["state"] != "starting",
    )
    assert snap["launch"]["state"] == "failed"
    assert snap["start_failed"] is True and snap["start_failed_reason"] == snap["launch"]["reason"]
    assert snap["pending_start"] is True  # the pane offers Start again + Discard
    assert native_state.read_session(key.partition(":")[2])["start"]["state"] == "failed"
    # #2096: a failed skip-start whose worker is proved gone with no turn discharges its
    # reservation so console launches for the shared store are not held behind 4502.
    assert native_ownership.lookup(key) is None
    from agent_sessions import engines

    prov = engines.get("codex")
    assert prov.snapshot_session_ids(str(project)) is not None
    native_ownership.check_console(prov)
    monkeypatch.setattr(host, "launch", real)
    assert client.post(f"/api/structured/sessions/{key}/start").status_code == 200
    snap = _poll(
        lambda: client.get(f"/api/structured/sessions/{key}").json(),
        lambda s: s["launch"]["state"] != "starting",
    )
    assert snap["launch"]["state"] == "ready" and snap["pending_start"] is False
    assert "start_failed" not in snap
    assert native_ownership.lookup(key).state == "bound"
    client.post(f"/api/structured/sessions/{key}/stop")


def test_a_start_refused_before_any_worker_exists_is_recorded(
    client,
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    created = client.post(
        "/api/structured/sessions",
        json={
            "engine": ENGINES["codex"],
            "cwd": str(project),
            "operation_id": ident(),
            "bypass": True,
        },
    ).json()
    key = created["session_key"]
    real = native_runtime.readiness
    monkeypatch.setattr(native_runtime, "readiness", lambda prov: (False, "the CLI went away"))
    r = client.post(f"/api/structured/sessions/{key}/start")
    assert r.status_code == 409 and "went away" in r.json()["detail"]
    assert not host.launches
    # No waiter exists to publish it, so `/start` recorded it — a lost answer still reads true.
    snap = client.get(f"/api/structured/sessions/{key}").json()
    assert snap["launch"]["state"] == "failed" and snap["launch"]["gen"] is None
    assert snap["launch"]["reason"] == "the CLI went away"
    assert snap["pending_start"] is True  # the start state is untouched: still pending
    assert native_state.read_session(key.partition(":")[2])["start"]["state"] == "pending"
    monkeypatch.setattr(native_runtime, "readiness", real)
    assert client.post(f"/api/structured/sessions/{key}/start").status_code == 200
    snap = _poll(
        lambda: client.get(f"/api/structured/sessions/{key}").json(),
        lambda s: s["launch"]["state"] != "starting",
    )
    assert snap["launch"]["state"] == "ready" and len(host.launches) == 1


# --- the generation fence -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_discard_racing_the_waiter_fences_its_write_out(host, project):  # noqa: F811
    op = ident()
    key = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    _hold(project)
    snap = await runtime.start_session(key, wait_ready=False)
    gen = snap["launch"]["gen"]
    await runtime.stop(key)  # the operator's Discard while the worker is still not ready
    record = native_state.read_session(op)
    assert "launch" not in record and record["start"]["state"] == "discarded"
    _release(project)
    await _waiters()  # the waiter sees its worker gone and runs its failure handling …
    record = native_state.read_session(op)
    # … whose write is a no-op: no view resurrected, the discard stands, nothing relaunched.
    assert "launch" not in record and record["start"]["state"] == "discarded"
    assert len(host.launches) == 1
    assert native_runtime._settle_launch(op, gen, "ready") is False


@pytest.mark.anyio
async def test_a_replacement_generation_fences_the_old_waiter(host, project):  # noqa: F811
    op = ident()
    key = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    snap = await runtime.start_session(key, wait_ready=False)
    await _waiters()
    record = native_state.read_session(op)
    assert record["launch"]["state"] == "ready"
    stale = ident()
    record["launch"] = {"gen": stale, "state": "starting", "since": 0}
    native_state.write_session(op, record)
    assert native_runtime._settle_launch(op, snap["launch"]["gen"], "failed", "late") is False
    assert native_state.read_session(op)["launch"]["gen"] == stale
    assert native_runtime._settle_launch(op, stale, "failed", "x" * 500) is True
    reason = native_state.read_session(op)["launch"]["reason"]
    assert len(reason) == native_runtime.LAUNCH_REASON_MAX and reason.endswith("…")
    await runtime.stop(key)


# --- shutdown and the next start --------------------------------------------------------------


def test_shutdown_drains_a_waiter_in_flight_without_settling_it(
    host,  # noqa: F811
    project,  # noqa: F811
    tmp_home,
    auth_cfg,
):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _hold(project)
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        c.headers.update({"X-CSRF-Token": c.get("/api/config").json()["csrf"]})
        c.headers.update({"Origin": auth_cfg.origin})
        r = c.post(
            "/api/structured/sessions",
            json={"engine": ENGINES["codex"], "cwd": str(project), "operation_id": ident()},
        )
        assert r.status_code == 201
        key = r.json()["session_key"]
        assert native_runtime._READINESS_TASKS
    assert not native_runtime._READINESS_TASKS  # drained by the lifespan teardown
    record = native_state.read_session(key.partition(":")[2])
    # Cancellation is not a failure: nothing stopped, nothing released, still `starting`.
    assert record["launch"]["state"] == "starting" and record["closed"] is False
    assert native_ownership.lookup(key) is not None
    _release(project)


async def _starting_after_shutdown(project, *, bypass=False):  # noqa: F811
    """A route-style launch whose app process went away before its worker was ready."""
    _hold(project)
    op = ident()
    key = (
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=op, bypass=bypass, wait_ready=False
        )
    )["session_key"]
    if bypass:
        await runtime.start_session(key, wait_ready=False)
    await native_runtime.drain_readiness()
    assert not native_runtime._READINESS_TASKS
    record = native_state.read_session(op)
    assert record["launch"]["state"] == "starting"
    return op, key, record["launch"]["gen"]


async def _phase(gen, wanted):
    await _until(
        lambda: asyncio.to_thread(native_state.read_lifecycle, gen),
        lambda life: life.get("phase") == wanted,
    )


@pytest.mark.anyio
async def test_restart_settles_a_ready_and_live_worker_as_ready(host, project):  # noqa: F811
    op, key, gen = await _starting_after_shutdown(project)
    _release(project)
    await _phase(gen, "ready")
    await native_runtime.reconcile_launches()
    await _waiters()
    record = native_state.read_session(op)
    assert record["launch"]["state"] == "ready" and record["closed"] is False
    assert native_ownership.lookup(key).state == "bound"  # the reservation is the session's
    await runtime.stop(key)


@pytest.mark.anyio
@pytest.mark.parametrize("bypass", [False, True])
async def test_restart_settles_a_ready_file_whose_worker_died_as_failed(
    host,  # noqa: F811
    project,  # noqa: F811
    bypass,
):
    op, key, gen = await _starting_after_shutdown(project, bypass=bypass)
    _release(project)
    await _phase(gen, "ready")
    host.kill(gen)  # it reported ready, then died: the lifecycle file still says ready
    assert native_state.read_lifecycle(gen)["phase"] == "ready"
    await native_runtime.reconcile_launches()
    await _waiters()
    record = native_state.read_session(op)
    assert record["launch"]["state"] == "failed"
    assert record["launch"]["reason"] == native_runtime.RESTARTED
    assert record["workers"] == []  # today's failure handling proved it gone
    if bypass:
        assert record["start"]["state"] == "failed"  # Start again reconciles
        snap = await runtime.snapshot(key)
        assert snap["start_failed"] is True and snap["pending_start"] is True
    else:
        assert record["closed"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("bypass", [False, True])
async def test_restart_settles_a_worker_that_exited_starting_as_failed(
    host,  # noqa: F811
    project,  # noqa: F811
    bypass,
):
    op, key, gen = await _starting_after_shutdown(project, bypass=bypass)
    host.kill(gen)  # gone before it ever became ready
    await native_runtime.reconcile_launches()
    await _waiters()
    record = native_state.read_session(op)
    assert record["launch"] == {
        **record["launch"],
        "state": "failed",
        "reason": native_runtime.RESTARTED,
    }
    assert record["workers"] == []
    # #2096: both guarded create and skip-start discharge an unbound, zero-turn reservation
    # once the exited worker is proved gone.
    assert native_ownership.lookup(key) is None
    if bypass:
        assert record["start"]["state"] == "failed"
    else:
        assert record["closed"] is True
    _release(project)


@pytest.mark.anyio
async def test_reconcile_launches_recovers_a_settled_failed_skip_start_and_is_idempotent(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """#2096: an already-settled `start.failed` + `launch.failed` record that still holds an
    unbound reservation from before #2096 is recovered by `reconcile_launches` under
    `session_lock`, unblocking console launches while still allowing `Start again`."""
    from agent_sessions import engines

    real = _break_launches(host, monkeypatch)
    real_discharge = native_runtime._discharge_if_unused
    op = ident()
    key = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    # Simulate a pre-#2096 failed start: suppress `_discharge_if_unused` during the failure so the
    # record settles to `start.failed` + `launch.failed` with its pending reservation stranded.
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", lambda *a, **kw: None)
    await runtime.start_session(key, wait_ready=False)
    await _waiters()
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", real_discharge)
    monkeypatch.setattr(host, "launch", real)

    record = native_state.read_session(op)
    assert record["start"]["state"] == "failed" and record["launch"]["state"] == "failed"
    assert record["workers"] == [] and record["native_id"] is None
    assert (
        native_ownership.lookup(key) is not None and native_ownership.lookup(key).state == "pending"
    )
    prov = engines.get("codex")
    with pytest.raises(native_ownership.OwnershipError) as exc_info:
        prov.snapshot_session_ids(str(project))
    assert exc_info.value.code == "pending"

    # Startup reconciliation discovers the stranded failed start and discharges it under the lock.
    await native_runtime.reconcile_launches()
    assert native_ownership.lookup(key) is None
    assert prov.snapshot_session_ids(str(project)) is not None
    native_ownership.check_console(prov)

    # Repeated reconciliation is an idempotent no-op.
    assert native_runtime._stranded_failed_starts() == ([], 0)
    await native_runtime.reconcile_launches()
    assert native_ownership.lookup(key) is None

    # And `Start again` re-reserves cleanly and brings the session up.
    await runtime.start_session(key, wait_ready=False)
    await _waiters()
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    assert native_ownership.lookup(key).state == "bound"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_failed_start_recovery_revalidates_under_session_lock_against_concurrent_retry(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """#2096 (Hermes review): if `_stranded_failed_starts` scans a failed record and a concurrent
    `Start again` moves the session out of `failed` before `_recover_failed_start` acquires
    `session_lock`, re-reading the record inside the lock leaves the replacement untouched."""
    real = _break_launches(host, monkeypatch)
    real_discharge = native_runtime._discharge_if_unused
    op = ident()
    key = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    )["session_key"]
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", lambda *a, **kw: None)
    await runtime.start_session(key, wait_ready=False)
    await _waiters()
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", real_discharge)
    monkeypatch.setattr(host, "launch", real)

    # 1. The scan observes the stranded failed record.
    stranded, unreadable = native_runtime._stranded_failed_starts()
    assert (ENGINES["codex"], op) in stranded and unreadable == 0

    # 2. A concurrent `Start again` runs before `_recover_failed_start` acquires the lock.
    _hold(project)
    await runtime.start_session(key, wait_ready=False)
    assert native_state.read_session(op)["start"]["state"] == "starting"
    assert native_ownership.lookup(key) is not None

    # 3. Recovery now runs with the stale scan entry, re-reads inside `session_lock`, and skips.
    assert native_runtime._recover_failed_start(ENGINES["codex"], op) is False
    assert native_ownership.lookup(key) is not None

    _release(project)
    await _waiters()
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    assert native_ownership.lookup(key).state == "bound"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_failed_skip_start_retains_reservation_on_every_unsafe_condition(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """#2096 negative matrix: unknown containment, a remaining worker, a bound native_id, a
    claimed submit turn, or an unreadable journal must each retain the ownership reservation."""
    from agent_sessions import native_journal

    _break_launches(host, monkeypatch)

    # (a) Unknown containment: `_stop_generation_locked` returns `"unknown"` and leaves the worker.
    op_a = ident()
    key_a = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op_a, bypass=True)
    )["session_key"]
    # Clean up op_a so the source store has no pending reservation for the next sub-cases.
    real_stop_gen = native_runtime._stop_generation_locked
    op_a_id = key_a.partition(":")[2]
    monkeypatch.setattr(
        native_runtime,
        "_stop_generation_locked",
        lambda s, r, w: "unknown" if s == op_a_id else real_stop_gen(s, r, w),
    )
    await runtime.start_session(key_a, wait_ready=False)
    await _waiters()
    assert native_state.read_session(op_a)["start"]["state"] == "failed"
    assert native_ownership.lookup(key_a) is not None
    assert native_runtime._recover_failed_start(ENGINES["codex"], op_a) is False
    assert native_ownership.lookup(key_a) is not None

    monkeypatch.setattr(native_runtime, "_stop_generation_locked", real_stop_gen)
    await runtime.stop(key_a)
    assert native_ownership.lookup(key_a) is None

    # (b) Another remaining worker in `record["workers"]` even when this generation is `"gone"`.
    op_b = ident()
    key_b = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op_b, bypass=True)
    )["session_key"]
    other_worker = ident()

    def stop_leaving_other(session_id, record, worker_id):
        res = real_stop_gen(session_id, record, worker_id)
        record["workers"] = [other_worker]
        native_state.write_session(session_id, record)
        return res

    monkeypatch.setattr(native_runtime, "_stop_generation_locked", stop_leaving_other)
    await runtime.start_session(key_b, wait_ready=False)
    await _waiters()
    assert native_ownership.lookup(key_b) is not None
    assert native_runtime._recover_failed_start(ENGINES["codex"], op_b) is False
    monkeypatch.setattr(native_runtime, "_stop_generation_locked", real_stop_gen)
    with native_state.session_lock(op_b):
        rec_b = native_state.read_session(op_b)
        rec_b["workers"] = []
        native_state.write_session(op_b, rec_b)
        native_runtime._discharge_if_unused(ENGINES["codex"], op_b, rec_b)
    assert native_ownership.lookup(key_b) is None

    # (c) Bound `native_id`: a session whose `native_id` is already set keeps its reservation.
    op_c = ident()
    key_c = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op_c, bypass=True)
    )["session_key"]

    def stop_with_bound_id(session_id, record, worker_id):
        res = real_stop_gen(session_id, record, worker_id)
        record["native_id"] = ident()
        native_state.write_session(session_id, record)
        return res

    monkeypatch.setattr(native_runtime, "_stop_generation_locked", stop_with_bound_id)
    await runtime.start_session(key_c, wait_ready=False)
    await _waiters()
    assert native_ownership.lookup(key_c) is not None
    assert native_runtime._recover_failed_start(ENGINES["codex"], op_c) is False
    monkeypatch.setattr(native_runtime, "_stop_generation_locked", real_stop_gen)
    with native_state.session_lock(op_c):
        rec_c = native_state.read_session(op_c)
        rec_c["native_id"] = None
        native_state.write_session(op_c, rec_c)
        native_runtime._discharge_if_unused(ENGINES["codex"], op_c, rec_c)
    assert native_ownership.lookup(key_c) is None

    # (d) Journal holds a `submit` claim: reservation is retained.
    from agent_sessions import native_ipc

    op_d = ident()
    key_d = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op_d, bypass=True)
    )["session_key"]
    prov_api = native_runtime._api_provider(ENGINES["codex"])
    jroot = native_runtime._journal_root(prov_api)
    binding_d = native_ipc.Binding(
        session_key=key_d,
        worker_id=ident(),
        connection_id=ident(),
        adapter="codex-app-server",
    )
    native_journal.claim(
        jroot,
        {
            **binding_d.envelope("request"),
            "request_id": ident(),
            "action": "submit",
            "params": {
                "operation_id": ident(),
                "expected_revision": 1,
                "text": "hi",
                "context": {},
            },
        },
    )
    await runtime.start_session(key_d, wait_ready=False)
    await _waiters()
    assert native_ownership.lookup(key_d) is not None
    assert native_runtime._recover_failed_start(ENGINES["codex"], op_d) is True
    assert native_ownership.lookup(key_d) is not None  # still retained because of the submit claim
    with native_runtime.storage.locked("launch", wait=30):
        native_ownership.discharge(
            key_d,
            operation_id=op_d,
            owner_token=native_state.read_session(op_d)["owner_token"],
        )

    # (e) Unreadable journal: reservation is retained (fail closed).
    op_e = ident()
    key_e = (
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op_e, bypass=True)
    )["session_key"]

    def stop_corrupting_journal(session_id, record, worker_id):
        res = real_stop_gen(session_id, record, worker_id)
        (jroot / f"{session_id}.jsonl").write_text("{corrupt-jsonl\n")
        return res

    monkeypatch.setattr(native_runtime, "_stop_generation_locked", stop_corrupting_journal)
    await runtime.start_session(key_e, wait_ready=False)
    await _waiters()
    assert native_ownership.lookup(key_e) is not None
    assert native_runtime._recover_failed_start(ENGINES["codex"], op_e) is True
    assert native_ownership.lookup(key_e) is not None  # retained on unreadable journal


@pytest.mark.anyio
async def test_restart_gives_a_still_starting_worker_a_fresh_waiter(host, project):  # noqa: F811
    op, key, gen = await _starting_after_shutdown(project)
    await native_runtime.reconcile_launches()
    assert native_runtime._READINESS_TASKS  # a fresh waiter, nothing settled yet
    assert native_state.read_session(op)["launch"]["state"] == "starting"
    _release(project)
    await _waiters()
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    await runtime.stop(key)


@pytest.mark.anyio
async def test_an_outage_longer_than_every_retry_is_settled_by_the_slow_owner(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (round 2, 2): a lifecycle outage outlasting all retry pauses used to end
    the only owner; the launch then read `starting` until the next app restart. The periodic
    in-process owner picks it up again and settles it within one `RECONCILE_INTERVAL` (fake
    clock: the pauses are recorded, not slept)."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(native_runtime, "_sleep", fake_sleep, raising=False)
    _hold(project)
    op = ident()
    key = (
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=op, wait_ready=False
        )
    )["session_key"]
    gen = native_state.read_session(op)["launch"]["gen"]
    real = native_state.read_lifecycle
    outage = {"on": True}

    def down(worker_id):
        if outage["on"]:
            raise native_state.StateError("lifecycle unreadable (sustained)")
        return real(worker_id)

    monkeypatch.setattr(native_state, "read_lifecycle", down)
    # The waiter burns through every retry and gives up.
    await _until(
        lambda: asyncio.sleep(0.05, list(native_runtime._READINESS_TASKS)), lambda t: not t
    )
    assert native_state.read_session(op)["launch"]["state"] == "starting"
    periodic = asyncio.create_task(native_runtime.reconcile_launches(periodic=True))
    try:
        await _until(lambda: asyncio.sleep(0.05, len(slept)), lambda n: n >= 8)
        outage["on"] = False  # recovery
        _release(project)
        await _until(
            lambda: asyncio.to_thread(native_state.read_session, op),
            lambda r: r["launch"]["state"] != "starting",
        )
    finally:
        periodic.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await periodic
    assert native_state.read_session(op)["launch"] == {
        **native_state.read_session(op)["launch"],
        "gen": gen,
        "state": "ready",
    }
    assert native_runtime.RECONCILE_INTERVAL in slept  # the slow owner's own pause
    await _waiters()
    await runtime.stop(key)


@pytest.mark.anyio
async def test_shutdown_waits_for_a_launch_thread_and_the_next_start_settles_it(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes on #1518 (round 2, 4): shutdown used to cancel the launch thread's handle (not the
    thread), so a thread that persisted `starting` afterwards had no owner — and the next process
    could scan before it wrote. Shutdown now waits (bounded) for the thread; the next start's
    scan then finds the record and settles it."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real_launch = host.launch

    def slow(argv):
        entered.set()
        release.wait(30)
        return real_launch(argv)

    monkeypatch.setattr(host, "launch", slow)
    op = ident()
    request = asyncio.create_task(
        native_runtime.create(ENGINES["codex"], str(project), session_id=op, wait_ready=False)
    )
    await asyncio.to_thread(entered.wait, 30)
    shutdown = asyncio.create_task(native_runtime.drain_readiness())
    await asyncio.sleep(0.3)
    assert not shutdown.done(), "shutdown must wait for the launch thread"
    release.set()
    await asyncio.wait_for(shutdown, 30)
    # When shutdown returns, the thread's `starting` is on disk and no owner survived it.
    assert native_state.read_session(op)["launch"]["state"] == "starting"
    assert not native_runtime._READINESS_TASKS
    await request
    await native_runtime.reconcile_launches()  # the next process's startup scan
    await _waiters()
    assert native_state.read_session(op)["launch"]["state"] == "ready"
    await runtime.stop(f"{ENGINES['codex']}:{op}")


@pytest.mark.anyio
async def test_failing_stranded_candidate_does_not_terminate_reconciliation(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    """#2096 (Hermes PR review): a stranded failed-start candidate whose recovery exhausts its
    retry budget during `reconcile_launches(periodic=True)` must not terminate the periodic
    scanner or prevent an unrelated `starting` launch in the same pass from settling; once the
    transient failure clears, the next periodic pass recovers and discharges the reservation."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(native_runtime, "_sleep", fake_sleep, raising=False)

    # 1. Create a stranded failed-start candidate (`launch.state == "failed"`, `workers == []`,
    #    `native_id is None`, pending reservation in `ownership.db`).
    real_launch = _break_launches(host, monkeypatch)
    stranded_op = ident()
    stranded_key = (
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=stranded_op, bypass=True
        )
    )["session_key"]
    real_discharge = native_runtime._discharge_if_unused
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", lambda *_a, **_kw: False)
    await runtime.start_session(stranded_key, wait_ready=False)
    await _waiters()
    monkeypatch.setattr(native_runtime, "_discharge_if_unused", real_discharge)
    monkeypatch.setattr(host, "launch", real_launch)
    assert native_ownership.lookup(stranded_key) is not None

    # 2. Create an unrelated `starting` launch in the same pass (as after a restart).
    starting_op, starting_key, _starting_gen = await _starting_after_shutdown(project)

    # 3. Fault-inject `_recover_failed_start` for `stranded_op` so it fails through the entire
    #    first pass's retry budget (`_RETRY_DELAYS`), then succeeds on the second periodic pass.
    real_recover = native_runtime._recover_failed_start
    fail_stranded = {"on": True, "attempts": 0, "passes": 0}

    def flaky_recover(engine_id: str, session_id: str) -> bool:
        if session_id == stranded_op and fail_stranded["on"]:
            fail_stranded["attempts"] += 1
            raise native_state.StateError("session lock unavailable during recovery")
        fail_stranded["passes"] += 1
        return real_recover(engine_id, session_id)

    monkeypatch.setattr(native_runtime, "_recover_failed_start", flaky_recover)

    periodic = asyncio.create_task(native_runtime.reconcile_launches(periodic=True))
    try:
        # Wait until the first pass has exhausted all retry attempts on `stranded_op` and entered
        # the periodic `RECONCILE_INTERVAL` sleep — proving `periodic` survived!
        await _until(
            lambda: asyncio.sleep(0.02, native_runtime.RECONCILE_INTERVAL in slept),
            bool,
        )
        assert (
            not periodic.done()
        ), "reconcile_launches(periodic=True) must survive recovery failure"
        assert fail_stranded["attempts"] >= len(native_runtime.LAUNCH_RETRY_DELAYS) + 1
        assert native_ownership.lookup(stranded_key) is not None  # retained for the next pass

        # The unrelated `starting` launch received its owner in pass 1 and settles to `ready`.
        _release(project)
        await _until(
            lambda: asyncio.to_thread(native_state.read_session, starting_op),
            lambda r: r["launch"]["state"] == "ready",
        )

        # Clear the outage; the next periodic pass recovers and discharges `stranded_key`.
        fail_stranded["on"] = False
        await _until(
            lambda: asyncio.to_thread(native_ownership.lookup, stranded_key),
            lambda row: row is None,
        )
        assert fail_stranded["passes"] >= 1
    finally:
        periodic.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await periodic
    await _waiters()
    await runtime.stop(starting_key)
