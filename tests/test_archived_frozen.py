"""#2064 — an archived session is frozen: no runtime, and every start path refuses it.

Covers the shared pieces (`archive_gate`, `freeze_lock`, `runtime_freeze`) and each start path:
the terminal websocket (close code 4410), the native runtime's launch gate, the structured facade,
and the chat runtime — including a turn cancelled and settled by an archive, a failed freeze that
leaves the session usable, and the cross-process cancel contract with a real second process.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions import (
    archive_gate,
    chat_config,
    chat_runtime,
    chat_store,
    engines,
    freeze_lock,
    metadata,
    native_runtime,
    review,
    runtime_cleanup,
    runtime_freeze,
    sessionlock,
)
from agent_sessions import structured_runtime as structured
from agent_sessions.main import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---- helpers ---------------------------------------------------------------------------------


class _Row(SimpleNamespace):
    pass


class FakeProv:
    """A provider with a per-session `lookup` and a chosen runtime kind."""

    def __init__(self, engine_id="fakeeng", runtime="terminal", rows=None, broken=False):
        self.engine_id = engine_id
        self.manifest = SimpleNamespace(runtime=runtime)
        self.rows = rows or {}
        self.broken = broken

    def lookup(self, native):
        if self.broken:
            raise OSError("store unreadable")
        archived = self.rows.get(native)
        return None if archived is None else _Row(archived=archived)


def _write_sidecar(raw: dict) -> None:
    path = metadata._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))


@pytest.fixture
def terminal_runtime(monkeypatch):
    """`frozen()` for a terminal session with the teardown faked: records each cleanup call."""
    calls: list = []
    outcome = {"value": "term"}

    async def resolve(engine, native):
        return f"{engine}:{native}"

    async def cleanup(engine, native, *, physical_key=None, **_kw):
        calls.append((engine, native))
        if isinstance(outcome["value"], Exception):
            raise outcome["value"]
        return outcome["value"]

    monkeypatch.setattr(runtime_cleanup, "resolve_runtime_key", resolve)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", cleanup)
    return SimpleNamespace(calls=calls, outcome=outcome)


# ---- archive_gate ----------------------------------------------------------------------------


def test_effective_state_the_sidecar_override_wins_over_the_engine():
    prov = FakeProv(rows={"a": True, "b": False})
    metadata.patch("fakeeng:a", archived=False)  # unarchived by the operator: engine flag ignored
    metadata.patch("fakeeng:b", archived=True)
    assert archive_gate.effective_state(prov, "a") == archive_gate.ACTIVE
    assert archive_gate.effective_state(prov, "b") == archive_gate.ARCHIVED


def test_effective_state_falls_back_to_the_engine_only_where_the_sidecar_is_silent():
    prov = FakeProv(rows={"a": True, "b": False})
    assert archive_gate.effective_state(prov, "a") == archive_gate.ARCHIVED
    assert archive_gate.effective_state(prov, "b") == archive_gate.ACTIVE
    assert archive_gate.effective_state(prov, "never-seen") == archive_gate.ACTIVE


def test_a_damaged_override_or_an_unreadable_store_is_unreadable_never_active():
    _write_sidecar({"fakeeng:a": {"archived": "yes"}})
    assert archive_gate.effective_state(FakeProv(), "a") == archive_gate.UNREADABLE
    assert archive_gate.effective_state(FakeProv(broken=True), "b") == archive_gate.UNREADABLE
    with pytest.raises(archive_gate.Refused) as refused:
        archive_gate.check(FakeProv(broken=True), "b")
    assert refused.value.status == 503


def test_check_refuses_archived_with_the_shared_detail():
    metadata.patch("fakeeng:a", archived=True)
    with pytest.raises(archive_gate.Refused) as refused:
        archive_gate.check(FakeProv(), "a")
    assert refused.value.status == 409
    assert refused.value.detail == archive_gate.ARCHIVED_DETAIL


# ---- freeze_lock -----------------------------------------------------------------------------


def test_shared_and_exclusive_exclude_each_other_and_a_timeout_leaves_nothing_held():
    key = "fakeeng:x"
    shared = freeze_lock.acquire(key, exclusive=False, timeout=1)
    with pytest.raises(freeze_lock.FreezeBusy):
        freeze_lock.acquire(key, exclusive=True, timeout=0.2)
    # Two launchers may decide at once.
    freeze_lock.acquire(key, exclusive=False, timeout=0.2).release()
    shared.release()
    # The timed-out attempt left no lock behind: an exclusive one is immediate now.
    held = freeze_lock.acquire(key, exclusive=True, timeout=0.2)
    assert freeze_lock.held_exclusively(key)
    with pytest.raises(freeze_lock.FreezeBusy):
        freeze_lock.acquire(key, exclusive=False, timeout=0.2)
    held.release()
    assert not freeze_lock.held_exclusively(key)


def test_a_marker_counts_only_while_its_freeze_holds_the_lock():
    key = "fakeeng:x"
    held = freeze_lock.acquire(key, exclusive=True, timeout=1)
    token = freeze_lock.write_marker(key)
    assert freeze_lock.cancel_requested(key)
    freeze_lock.clear_marker(key, "someone-else")  # never another freeze's marker
    assert freeze_lock.marker_path(key).exists()
    held.release()
    # Its freeze is gone (crashed, say): the leftover marker is inert…
    assert not freeze_lock.cancel_requested(key)
    # …and the next launcher clears it.
    freeze_lock.acquire(key, exclusive=False, timeout=1).release()
    assert not freeze_lock.marker_path(key).exists()
    del token


# ---- frozen(): terminal ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_terminal_freeze_holds_the_fence_and_the_writer_lock_then_releases(terminal_runtime):
    prov, key = FakeProv(), "fakeeng:t1"
    async with runtime_freeze.frozen(prov, "t1"):
        assert terminal_runtime.calls == [("fakeeng", "t1")]
        assert freeze_lock.held_exclusively(key)
        assert sessionlock.is_locked(key), "no LAUNCH can win the writer lock meanwhile"
        assert freeze_lock.marker_path(key).exists()
        with pytest.raises(freeze_lock.FreezeBusy):  # a launch decision waits
            freeze_lock.acquire(key, exclusive=False, timeout=0.2)
    assert not freeze_lock.held_exclusively(key)
    assert not sessionlock.is_locked(key)
    assert not freeze_lock.marker_path(key).exists()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["leaked", RuntimeError("boom")])
async def test_terminal_freeze_fails_closed_and_cleans_up(terminal_runtime, failure):
    terminal_runtime.outcome["value"] = failure
    prov, key = FakeProv(), "fakeeng:t2"
    ran = False
    with pytest.raises(runtime_freeze.FreezeFailed):
        async with runtime_freeze.frozen(prov, "t2"):
            ran = True
    assert not ran, "nothing may be recorded archived"
    assert not freeze_lock.held_exclusively(key)
    assert not freeze_lock.marker_path(key).exists()
    assert not sessionlock.is_locked(key)


@pytest.mark.anyio
async def test_terminal_freeze_stops_a_master_a_late_launch_spawned(terminal_runtime, monkeypatch):
    # A launch admitted just before the fence closed still holds the writer lock while it
    # spawns: the freeze waits, stops that master too, and only then holds the lock.
    monkeypatch.setattr(runtime_freeze, "_TERMINAL_RETRY_S", 0.05)
    key = "fakeeng:t3"
    launching = sessionlock.acquire(key)
    threading.Timer(0.2, launching.release).start()
    async with runtime_freeze.frozen(FakeProv(), "t3"):
        assert sessionlock.is_locked(key)
    assert len(terminal_runtime.calls) >= 2


@pytest.mark.anyio
async def test_a_cancelled_terminal_freeze_never_orphans_the_writer_lock(
    terminal_runtime, monkeypatch
):
    # Hermes on #2073 (review 8264): the freeze is cancelled after its worker thread has taken the
    # real writer lock but before the lock reaches the coroutine. The late lock must be released,
    # or the session stays BUSY (and un-archivable) for the life of the process.
    key = "fakeeng:t4"
    real_acquire = sessionlock.acquire
    taken, release = threading.Event(), threading.Event()

    def paused_acquire(k):
        lock = real_acquire(k)
        taken.set()
        release.wait(5)
        return lock

    monkeypatch.setattr(sessionlock, "acquire", paused_acquire)

    async def freeze():
        async with runtime_freeze.frozen(FakeProv(), "t4"):
            pass

    task = asyncio.create_task(freeze())
    await asyncio.to_thread(taken.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(sessionlock, "acquire", real_acquire)
    for _ in range(100):  # the late lock is released by the worker's done-callback
        lock = sessionlock.acquire(key)
        if lock is not None:
            break
        await asyncio.sleep(0.02)
    assert lock is not None, "the writer lock is reacquirable after the cancelled freeze"
    lock.release()
    assert not freeze_lock.held_exclusively(key)
    assert not freeze_lock.marker_path(key).exists()


# ---- frozen(): native ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_native_freeze_runs_stop_everything_for_the_live_generation(monkeypatch):
    prov = FakeProv(runtime="api")
    gens = iter([SimpleNamespace(worker_id="w1"), SimpleNamespace(worker_id="w2")])
    monkeypatch.setattr(native_runtime, "_generation", lambda sid: next(gens))
    seen: list = []

    async def stop(engine, native, *, discard=True, confirm=None):
        seen.append(confirm)
        if len(seen) == 1:  # a generation that was already starting replaced the confirmed one
            raise native_runtime.NativeError(409, "inventory changed")
        return {"containment": "gone"}

    async def resolve(engine, native):
        return f"{engine}:{native}"

    monkeypatch.setattr(native_runtime, "stop", stop)
    monkeypatch.setattr(runtime_cleanup, "resolve_runtime_key", resolve)
    async with runtime_freeze.frozen(prov, "n1"):
        pass
    assert seen == [{"worker": "w1", "everything": True}, {"worker": "w2", "everything": True}]


@pytest.mark.anyio
@pytest.mark.parametrize("containment", ["live", "unknown"])
async def test_native_freeze_refuses_unless_containment_is_gone(monkeypatch, containment):
    monkeypatch.setattr(native_runtime, "_generation", lambda sid: SimpleNamespace(worker_id="w"))

    async def stop(engine, native, *, discard=True, confirm=None):
        return {"containment": containment}

    async def resolve(engine, native):
        return f"{engine}:{native}"

    monkeypatch.setattr(native_runtime, "stop", stop)
    monkeypatch.setattr(runtime_cleanup, "resolve_runtime_key", resolve)
    with pytest.raises(runtime_freeze.FreezeFailed) as failed:
        async with runtime_freeze.frozen(FakeProv(runtime="api"), "n2"):
            pass
    assert containment in failed.value.reason


def test_native_launch_gate_refuses_archived_and_waits_out_a_freeze(monkeypatch):
    monkeypatch.setattr(engines, "get_any", lambda engine: FakeProv())
    metadata.patch("fakeeng:n3", archived=True)
    with pytest.raises(native_runtime.NativeError) as refused:
        native_runtime.archive_refusal("fakeeng", "n3")
    assert refused.value.status == 409
    assert refused.value.detail == archive_gate.ARCHIVED_DETAIL
    with pytest.raises(native_runtime.NativeError) as refused:
        with native_runtime._archive_admission("fakeeng", "n3"):
            pass
    assert refused.value.detail == archive_gate.ARCHIVED_DETAIL
    # A freeze in progress: the launch is refused retryably, never decided under it.
    monkeypatch.setattr(freeze_lock, "SHARED_TIMEOUT", 0.2)
    held = freeze_lock.acquire("fakeeng:n4", exclusive=True, timeout=1)
    try:
        with pytest.raises(native_runtime.NativeError) as busy:
            with native_runtime._archive_admission("fakeeng", "n4"):
                pass
        assert busy.value.remedy == "retry"
    finally:
        held.release()


# ---- the sweep -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sweep_freezes_only_archived_sessions_never_unreadable(monkeypatch, terminal_runtime):
    states = {"fakeeng:a": "archived", "fakeeng:b": "active", "fakeeng:c": "unreadable"}
    monkeypatch.setattr(runtime_freeze, "_live_keys", lambda: set(states))
    monkeypatch.setattr(engines, "parse_key", lambda k: (FakeProv(), k.partition(":")[2]))
    monkeypatch.setattr(
        archive_gate, "effective_state", lambda prov, native: states[f"fakeeng:{native}"]
    )
    assert await runtime_freeze.sweep_once() == ["fakeeng:a"]
    assert terminal_runtime.calls == [("fakeeng", "a")]


@pytest.mark.anyio
async def test_a_sweep_queued_behind_an_unarchive_never_stops_the_new_generation(
    monkeypatch, terminal_runtime
):
    state = {"value": "archived"}
    monkeypatch.setattr(runtime_freeze, "_live_keys", lambda: {"fakeeng:s"})
    monkeypatch.setattr(engines, "parse_key", lambda k: (FakeProv(), "s"))
    monkeypatch.setattr(archive_gate, "effective_state", lambda prov, native: state["value"])
    # The unarchive holds the fence; the sweep saw "archived" before it and queues behind it.
    unarchive = freeze_lock.acquire("fakeeng:s", exclusive=True, timeout=1)

    def finish_unarchive():
        state["value"] = "active"
        unarchive.release()

    threading.Timer(0.3, finish_unarchive).start()
    assert await runtime_freeze.sweep_once() == []
    assert terminal_runtime.calls == [], "the re-read under the fence saw it active"


# ---- the terminal websocket ------------------------------------------------------------------


def _login_headers(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"cookie": f"agent_sessions={c.cookies.get('agent_sessions')}", "origin": cfg.origin}


def _ws_outcome(c, key, headers, monkeypatch, action, query=""):
    from agent_sessions.routes import terminal as terminal_route

    async def _action(_engine, _native):
        return action, None

    bridged: list = []

    async def _bridge(ws, argv, **_kw):
        bridged.append(list(argv))
        await ws.close(code=4999)

    monkeypatch.setattr(terminal_route, "_open_action_offloop", _action)
    monkeypatch.setattr(terminal_route.webterm, "run", _bridge)
    code = None
    try:
        with c.websocket_connect(f"/ws/term/{key}{query}", headers=headers) as ws:
            for _ in range(20):
                msg = ws.receive()
                if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                    code = msg.get("code")
                    break
    except WebSocketDisconnect as e:
        code = e.code
    return ("bridged", bridged[0]) if bridged else ("closed", code)


UUID1 = "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("action", ["launch", "attach"])
def test_an_archived_terminal_session_is_closed_4410_never_launched_or_attached(
    auth_cfg, fake_jsonl, monkeypatch, action
):
    from agent_sessions.routes import terminal as terminal_route

    metadata.patch(f"claude:{UUID1}", archived=True)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    act = terminal_route.sessions.LAUNCH if action == "launch" else terminal_route.sessions.ATTACH
    got = _ws_outcome(c, f"claude:{UUID1}", _login_headers(c, auth_cfg), monkeypatch, act)
    assert got == ("closed", terminal_route.ARCHIVED_CLOSE_CODE) == ("closed", 4410)


@pytest.mark.parametrize("action", ["launch", "attach"])
def test_new_1_never_exempts_an_archived_id(auth_cfg, fake_jsonl, monkeypatch, action):
    # Hermes on #2073 (P1): `new=1` is the caller's claim, not proof the id is fresh. An already
    # archived id is refused 4410 with it too — on LAUNCH, and on ATTACH to a still-live master.
    from agent_sessions.routes import terminal as terminal_route

    metadata.patch(f"claude:{UUID1}", archived=True)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    act = terminal_route.sessions.LAUNCH if action == "launch" else terminal_route.sessions.ATTACH
    got = _ws_outcome(
        c, f"claude:{UUID1}", _login_headers(c, auth_cfg), monkeypatch, act, query="?new=1"
    )
    assert got == ("closed", 4410)


def test_only_a_new_placeholder_may_pass_a_store_that_cannot_answer():
    placeholder = f"new-{uuid.uuid4()}"
    broken = FakeProv(broken=True)
    assert archive_gate.effective_state(broken, placeholder, new_session=True) == "active"
    assert archive_gate.effective_state(broken, placeholder) == "unreadable"
    assert archive_gate.effective_state(broken, str(uuid.uuid4()), new_session=True) == "unreadable"
    metadata.patch(f"fakeeng:{placeholder}", archived=True)  # the override still wins
    assert archive_gate.effective_state(broken, placeholder, new_session=True) == "archived"


@pytest.mark.anyio
async def test_a_cancelled_open_never_orphans_the_launch_lock(monkeypatch):
    # Hermes on #2073 (P2): the client vanishing while the archive state is being read must not
    # leave the LAUNCH lock held (it has no finalizer → the session would stay BUSY for good).
    from agent_sessions.routes import terminal as terminal_route

    key = "fakeeng:c1"
    started, release = threading.Event(), threading.Event()

    async def _action(engine, native):
        return terminal_route.sessions.LAUNCH, sessionlock.acquire(key)

    def _slow_check(prov, native, *, new_session=False):
        started.set()
        release.wait(5)

    monkeypatch.setattr(terminal_route, "_open_action_offloop", _action)
    monkeypatch.setattr(archive_gate, "check", _slow_check)
    task = asyncio.create_task(
        terminal_route._admit_open(FakeProv(), "c1", key, "c1", is_new=False)
    )
    await asyncio.to_thread(started.wait, 5)
    assert sessionlock.is_locked(key)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    lock = sessionlock.acquire(key)
    assert lock is not None, "the writer lock is reacquirable after the cancelled open"
    lock.release()
    assert not freeze_lock.held_exclusively(key)
    freeze_lock.acquire(key, exclusive=True, timeout=0.5).release()


def test_busy_stays_4409_and_an_active_session_still_launches(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions.routes import terminal as terminal_route

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    headers = _login_headers(c, auth_cfg)
    key = f"claude:{UUID1}"
    busy = _ws_outcome(c, key, headers, monkeypatch, terminal_route.sessions.BUSY)
    assert busy == ("closed", 4409)
    got = _ws_outcome(c, key, headers, monkeypatch, terminal_route.sessions.ATTACH)
    assert got[0] == "bridged"


# ---- the chat runtime ------------------------------------------------------------------------

ENGINE = "apichat"


class Endpoint:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.gate: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_CHAT_DIR", str(tmp_path / "chat-store"))
    ep = Endpoint()
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(ep.handler))
    monkeypatch.setattr(chat_runtime, "ARCHIVE_WATCH_INTERVAL", 0.05)
    chat_config.set_config(
        ENGINE,
        {"base_url": "https://llm.example.test/v1", "api_key": "sk-test-0123456789", "model": "m"},
    )
    yield ep
    chat_runtime._TASKS.clear()
    chat_runtime._LOCKS.clear()


def _chat_prov():
    return chat_runtime._chat_provider(ENGINE)


async def _wait_for(pred, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_a_send_to_an_archived_chat_session_is_refused_and_reaches_nobody(endpoint, tmp_path):
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    _chat_prov().archive(sid)
    with pytest.raises(chat_runtime.ChatError) as refused:
        await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    assert refused.value.status == 409 and refused.value.detail == archive_gate.ARCHIVED_DETAIL
    with pytest.raises(structured.StructuredError) as refused:
        await structured.submit_turn(f"{ENGINE}:{sid}", operation_id=str(uuid.uuid4()), text="hi")
    assert refused.value.status == 409 and refused.value.detail == archive_gate.ARCHIVED_DETAIL
    assert not endpoint.requests


@pytest.mark.anyio
async def test_archive_cancels_a_running_turn_and_waits_for_it_to_settle(endpoint, tmp_path):
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    endpoint.gate = asyncio.Event()  # the model never answers
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    task = chat_runtime.running_task(ENGINE, sid)
    await _wait_for(lambda: endpoint.requests)
    prov = _chat_prov()
    async with runtime_freeze.frozen(prov, sid):
        assert task.done(), "the fence is held only once the turn has settled"
        prov.archive(sid)
    turn = chat_store.read(tmp_path / "chat-store", sid).turns[-1]
    assert turn.status == "failed" and turn.reason == chat_runtime.ARCHIVED_REASON
    with pytest.raises(chat_runtime.ChatError):
        await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "again")


@pytest.mark.anyio
async def test_a_failed_freeze_leaves_the_session_usable(endpoint, tmp_path, monkeypatch):
    # The turn cannot be cancelled in time (its watcher is not looking): the freeze fails on its
    # bound, records nothing and removes its marker — so a later send in the still-active session
    # is never cancelled by a leftover request.
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    monkeypatch.setattr(runtime_freeze, "TURN_SETTLE_TIMEOUT", 0.3)
    monkeypatch.setattr(chat_runtime, "ARCHIVE_WATCH_INTERVAL", 60)
    monkeypatch.setattr(chat_runtime, "cancel_for_archive", lambda key: None)
    endpoint.gate = asyncio.Event()
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "hello")
    first = chat_runtime.running_task(ENGINE, sid)
    await _wait_for(lambda: endpoint.requests)
    with pytest.raises(runtime_freeze.FreezeFailed):
        async with runtime_freeze.frozen(_chat_prov(), sid):
            pass
    assert not freeze_lock.marker_path(f"{ENGINE}:{sid}").exists()
    endpoint.gate.set()
    await first
    monkeypatch.setattr(chat_runtime, "ARCHIVE_WATCH_INTERVAL", 0.05)
    await chat_runtime.send(ENGINE, sid, str(uuid.uuid4()), "second")
    await chat_runtime.running_task(ENGINE, sid)
    statuses = [t.status for t in chat_store.read(tmp_path / "chat-store", sid).turns]
    assert statuses[-1] == "done", statuses


_OTHER_PROCESS = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from agent_sessions import chat_edits, freeze_lock

    root, sid, key, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
    fence = chat_edits.turn_fence(root, sid)
    assert fence is not None
    print("running", flush=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if freeze_lock.cancel_requested(key):
            print("cancel-seen", flush=True)
            if mode == "settle":
                time.sleep(0.3)  # the in-flight write settles first
                print("settled", flush=True)
                fence.release()
                break
        time.sleep(0.05)
    time.sleep(1.5)
    """
)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["settle", "stuck"])
async def test_a_turn_in_another_process_is_cancelled_and_settled_before_archive_commits(
    endpoint, tmp_path, monkeypatch, mode
):
    sid = await chat_runtime.new_session(ENGINE, str(tmp_path))
    prov = _chat_prov()
    key = f"{ENGINE}:{sid}"
    root = chat_runtime._root(prov)
    proc = subprocess.Popen(
        [sys.executable, "-c", _OTHER_PROCESS, str(root), sid, key, mode],
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert (await asyncio.to_thread(proc.stdout.readline)).strip() == "running"
        lines: list[str] = []
        if mode == "settle":
            async with runtime_freeze.frozen(prov, sid):
                # Only reachable once the other process released its turn fence.
                lines = [proc.stdout.readline().strip(), proc.stdout.readline().strip()]
                assert lines == ["cancel-seen", "settled"]
        else:
            monkeypatch.setattr(runtime_freeze, "TURN_SETTLE_TIMEOUT", 0.6)
            started = time.monotonic()
            with pytest.raises(runtime_freeze.FreezeFailed):
                async with runtime_freeze.frozen(prov, sid):
                    pass
            assert time.monotonic() - started < 10
            # The failed freeze's marker is gone: the other process's turn is not cancelled.
            assert not freeze_lock.cancel_requested(key)
            assert not freeze_lock.marker_path(key).exists()
    finally:
        proc.kill()
        proc.wait()
