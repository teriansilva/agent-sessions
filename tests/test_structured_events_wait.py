"""The operator's events read follows a streaming turn at the worker's flush cadence (#1727).

Text reaches the API pane through the journal's existing ``after`` / ``next_cursor`` pages and
its persisted normalized text events, never a second offset protocol. A read may wait (``wait``,
capped at 3 s so Home Free's buffering proxy answers well inside its ≈5 s read timeout) until the
journal moves past ``after``. Workers append from their own process, so the wake is a file stat.

The real worker runs for every session here (``test_native_runtime``'s scripted natives).
"""

# ruff: noqa: F811 — `host` / `project` are fixtures imported from test_native_runtime

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent_sessions import native_ipc, native_journal, native_runtime
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _session(project: Path) -> str:
    created = await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())
    return created["session_key"]


async def _hanging_turn(key: str) -> str:
    """A turn the scripted agent starts and never ends: its claim binds a live generation."""
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="HANG")
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if any(t["turn_id"] == turn and t["state"] == "running" for t in snap["turns"]):
            return turn
        await asyncio.sleep(0.05)
    raise AssertionError("the turn never started")


async def _quiet(key: str) -> int:
    """The journal's revision once nothing has moved it for half a second."""
    last = (await runtime.events(key, after=0, limit=1))["revision"]
    while True:
        await asyncio.sleep(0.5)
        now = (await runtime.events(key, after=0, limit=1))["revision"]
        if now == last:
            return now
        last = now


def _journal_of(key: str) -> tuple[Path, str]:
    engine, native = key.split(":", 1)
    prov, _ = native_runtime._session(engine, native, retiring_ok=True)
    return native_runtime._journal_root(prov), native


def _claim_record(root: Path, native: str, turn: str) -> dict:
    lines = (root / f"{native}.jsonl").read_text().splitlines()
    return next(
        r
        for r in map(json.loads, lines)
        if r.get("type") == "native_operation" and r.get("operation_id") == turn
    )


def _delta(turn: str, text: str, item: str = "msg-1") -> dict:
    return {
        "kind": "text",
        "data": {
            "operation_id": turn,
            "native_turn_id": "native-turn",
            "item_id": item,
            "text": text,
            "partial": True,
            "truncated": False,
        },
    }


# A worker stand-in in its OWN process: the same `append_events` the worker's flush calls, every
# 200 ms (native_worker.FLUSH_INTERVAL). It prints when each append returned (the moment a real
# worker's flush is durable). Its fold cache is warmed first, as a long-running worker's is.
_WRITER = r"""
import sys, time
from pathlib import Path
from agent_sessions import native_ipc, native_journal
root, session_key, worker, connection, turn, count = sys.argv[1:7]
binding = native_ipc.Binding(session_key=session_key, worker_id=worker,
                             connection_id=connection, adapter="codex-app-server")
native_journal.read(Path(root), session_key.partition(":")[2])
for i in range(int(count)):
    time.sleep(0.2)
    data = {"operation_id": turn, "native_turn_id": "native-turn", "item_id": "msg-1",
            "text": f"<{i}>", "partial": True, "truncated": False}
    native_journal.append_events(Path(root), binding, [{"kind": "text", "data": data}])
    print(i, time.time(), flush=True)
"""


@pytest.mark.anyio
async def test_an_event_page_follows_the_200ms_flush_without_a_projection(
    host, project, monkeypatch
):
    """The Phase 2 proof. A journal already ~10k records long; a separate process appends one
    partial text event per 200 ms flush; the pane's read (``wait_events``) sees each one well
    inside the next flush, every one exactly once, and never projects the conversation (#1557
    measured a projection at ≈0.5 s on a large journal: the cadence would be the snapshot's)."""
    key = await _session(project)
    turn = await _hanging_turn(key)
    root, native = _journal_of(key)
    claim = _claim_record(root, native, turn)
    binding = native_ipc.Binding(
        session_key=key,
        worker_id=claim["worker_id"],
        connection_id=claim["connection_id"],
        adapter="codex-app-server",
    )
    history = [_delta(turn, "word " * 60) for _ in range(100)]
    for _ in range(100):
        native_journal.append_events(root, binding, history)
    cursor = await _quiet(key)
    assert cursor > 10_000

    def no_projection(*args, **kwargs):
        raise AssertionError("an events read projected the conversation")

    monkeypatch.setattr(native_runtime, "project", no_projection)
    count = 12
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRITER,
            str(root),
            key,
            claim["worker_id"],
            claim["connection_id"],
            turn,
            str(count),
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.PIPE,
        text=True,
    )
    seen: list[tuple[int, float]] = []
    reads = 0
    try:
        deadline = time.monotonic() + 30
        while len(seen) < count and time.monotonic() < deadline:
            page = await runtime.wait_events(key, after=cursor, limit=100, wait=3)
            got = time.time()
            reads += 1
            seen.extend((int(e["data"]["text"][1:-1]), got) for e in page["events"])
            cursor = page["next_cursor"]
    finally:
        out, _ = writer.communicate(timeout=30)
    assert writer.returncode == 0
    appended = {int(i): float(at) for i, at in (line.split() for line in out.splitlines())}
    assert [index for index, _ in seen] == list(range(count))  # each once, in order
    latencies = sorted(got - appended[index] for index, got in seen)
    median = latencies[len(latencies) // 2]
    # Each flush is visible before the next one lands: the read keeps the worker's cadence.
    assert median < 0.2 and latencies[-2] < 0.4, latencies
    assert reads <= count + 2  # a wake per flush, not a busy loop of empty answers


@pytest.mark.anyio
async def test_a_quiet_read_waits_at_most_three_seconds_whatever_it_asks(host, project):
    key = await _session(project)
    revision = await _quiet(key)
    for asked in (3, 60):
        started = time.monotonic()
        page = await runtime.wait_events(key, after=revision, wait=asked)
        took = time.monotonic() - started
        assert page["events"] == [] and page["next_cursor"] == revision
        assert 2.5 <= took < 4.5, took
    started = time.monotonic()
    page = await runtime.wait_events(key, after=revision, wait=0)  # no wait: as before
    assert time.monotonic() - started < 1 and page["next_cursor"] == revision
    for bad in (-1, float("nan"), float("inf"), True, "3"):
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.wait_events(key, after=revision, wait=bad)
        assert refused.value.status == 422


@pytest.mark.anyio
async def test_the_wait_counts_from_the_request_even_when_the_first_read_is_slow(
    host, project, monkeypatch
):
    """Review of #1747: the deadline started after the first read, so a slow one (a cold fold
    after the cache let the session go, or one queued behind another waiter) made a quiet held
    read last its own time PLUS 3 s, eating Home Free's ≈5 s margin. It is 3 s in all."""
    key = await _session(project)
    revision = await _quiet(key)
    real = runtime.events
    calls = 0

    async def slow_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1.0)
        return await real(*args, **kwargs)

    monkeypatch.setattr(runtime, "events", slow_first)
    started = time.monotonic()
    page = await runtime.wait_events(key, after=revision, wait=3)
    took = time.monotonic() - started
    assert page["events"] == [] and calls >= 1
    assert 2.5 <= took < 3.6, took  # was ≈ 4 s: 1 s of read, then a full 3 s wait


@pytest.mark.anyio
async def test_an_append_by_the_worker_process_wakes_the_waiting_read(host, project):
    """The worker journals in its own process; the waiting read in this one wakes on it."""
    key = await _session(project)
    revision = await _quiet(key)
    waiting = asyncio.create_task(runtime.wait_events(key, after=revision, wait=3))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    started = time.monotonic()
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="STREAM")
    page = await asyncio.wait_for(waiting, 5)
    assert time.monotonic() - started < 1.5
    assert page["events"] and page["events"][0]["cursor"] > revision
    await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
    await settle(key, turn, state=("interrupted",))


@pytest.mark.anyio
async def test_streamed_text_rebuilt_from_events_equals_the_next_snapshot(host, project):
    """Consistency: the running turn's snapshot says its reply is streamed text (``stream``);
    that reply plus every partial text event after the snapshot's cursor is exactly what the
    next snapshot shows. Retries and other turns never splice in: deltas name their turn."""
    key = await _session(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="STREAM")
    for _ in range(200):
        base = await runtime.snapshot(key)
        shown = next((t for t in base["turns"] if t["turn_id"] == turn), None)
        if shown and shown["state"] == "running" and shown["reply"]:
            break
        await asyncio.sleep(0.05)
    assert shown["stream"] == {"item": "msg-s"}
    text, cursor = shown["reply"], base["event_cursor"]
    pages = 0
    while pages < 6:
        page = await runtime.wait_events(key, after=cursor, wait=3)
        pages += 1
        for event in page["events"]:
            data = event["data"]
            assert event["kind"] == "text" and data["partial"]
            assert data["operation_id"] == turn and data["item_id"] == "msg-s"
            text += data["text"]
        cursor = page["next_cursor"]
    later = await runtime.snapshot(key)
    # The snapshot may be newer than the last page read: rebuild up to ITS cursor.
    while cursor < later["event_cursor"]:
        page = await runtime.events(key, after=cursor, limit=100)
        text += "".join(e["data"]["text"] for e in page["events"])
        cursor = page["next_cursor"]
    assert cursor == later["event_cursor"]
    again = next(t for t in later["turns"] if t["turn_id"] == turn)
    assert again["reply"] == text and len(text) > len(shown["reply"])
    await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
    _, ended = await settle(key, turn, state=("interrupted",))
    assert "stream" not in ended  # a settled turn is never extended


@pytest.mark.anyio
async def test_a_cursor_the_journal_cannot_serve_is_the_reset_answer_waiting_or_not(host, project):
    key = await _session(project)
    revision = await _quiet(key)
    for wait in (0, 3):
        started = time.monotonic()
        with pytest.raises(runtime.StructuredError) as reset:
            await runtime.wait_events(key, after=revision + 50, wait=wait)
        assert reset.value.status == 409 and time.monotonic() - started < 1
    assert runtime.events_waiters() == 0


@pytest.mark.anyio
async def test_live_waiter_caps_answer_at_once_and_recover_when_waiters_leave(
    host, project, monkeypatch
):
    monkeypatch.setattr(runtime, "EVENTS_WAITERS_PER_SESSION", 2)
    monkeypatch.setattr(runtime, "EVENTS_WAITERS_TOTAL", 3)
    one, two = await _session(project), await _session(project)
    at_one, at_two = await _quiet(one), await _quiet(two)

    def wait_on(key, after):
        return asyncio.create_task(runtime.wait_events(key, after=after, wait=3))

    held = [wait_on(one, at_one), wait_on(one, at_one)]
    await asyncio.sleep(0.3)
    assert runtime.events_waiters(one) == 2
    started = time.monotonic()
    over = await runtime.wait_events(one, after=at_one, wait=3)  # over the session's cap
    assert time.monotonic() - started < 1 and over["next_cursor"] == at_one
    held.append(wait_on(two, at_two))  # another session still has room…
    await asyncio.sleep(0.3)
    assert runtime.events_waiters() == 3
    started = time.monotonic()
    await runtime.wait_events(two, after=at_two, wait=3)  # …until the global cap
    assert time.monotonic() - started < 1
    for task in held[:2]:  # the clients go away
        task.cancel()
    await asyncio.gather(*held[:2], return_exceptions=True)
    assert runtime.events_waiters(one) == 0 and runtime.events_waiters() == 1
    started = time.monotonic()
    await runtime.wait_events(one, after=at_one, wait=1)  # capacity is back: it waits again
    assert time.monotonic() - started >= 0.8
    await held[2]
    assert runtime.events_waiters() == 0


# ---- through the real app on a real socket ---------------------------------------------------


@pytest.fixture
def live_app(auth_cfg):
    """The real app under uvicorn: the only way to model a client that hangs up mid-wait."""
    import uvicorn

    from agent_sessions.main import create_app

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(auth_cfg), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not (server.started and server.servers) and time.monotonic() < deadline:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port, auth_cfg
    server.should_exit = True
    thread.join(timeout=10)


def _login(port: int, cfg) -> tuple[str, str]:
    """(Cookie header, CSRF token) for the operator. The cookie is `Secure`, so it is carried by
    hand over this loopback hop, as Home Free's proxy carries the browser's."""
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as c:
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": cfg.origin},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), r.text
        cookie = "; ".join(v.split(";", 1)[0] for v in r.headers.get_list("set-cookie"))
        csrf = c.get("/api/config", headers={"Cookie": cookie}).json()["csrf"]
    return cookie, csrf


def _create(port: int, cfg, cookie: str, csrf: str, project: Path) -> str:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as c:
        r = c.post(
            "/api/structured/sessions",
            json={"engine": ENGINES["codex"], "cwd": str(project), "operation_id": ident()},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": cfg.origin},
        )
        assert r.status_code == 201, r.text
        return r.json()["session_key"]


def _revision(port: int, cookie: str, key: str) -> int:
    """The quiet revision of a session whose worker is READY: the create route answers at launch
    (#1505), and the worker's own `session` report lands after it."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snap = httpx.get(
            f"http://127.0.0.1:{port}/api/structured/sessions/{key}", headers={"Cookie": cookie}
        ).json()
        if (snap.get("launch") or {}).get("state") == "ready" and snap["native"]["native_id"]:
            break
        time.sleep(0.1)
    last = None
    while True:
        r = httpx.get(
            f"http://127.0.0.1:{port}/api/structured/sessions/{key}/events?after=0&limit=1",
            headers={"Cookie": cookie},
        )
        assert r.status_code == 200, r.text
        if r.json()["revision"] == last:
            return last
        last = r.json()["revision"]
        time.sleep(0.5)


def test_a_waiting_read_requires_login_and_ends_when_its_client_hangs_up(host, project, live_app):
    port, cfg = live_app
    events = "/api/structured/sessions/{key}/events?after={after}&wait=3"
    cookie, csrf = _login(port, cfg)
    key = _create(port, cfg, cookie, csrf, project)
    revision = _revision(port, cookie, key)
    url = f"http://127.0.0.1:{port}" + events.format(key=key, after=revision)
    started = time.monotonic()
    assert httpx.get(url).status_code == 401  # no login: refused before any wait
    assert time.monotonic() - started < 1
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(
        f"GET {events.format(key=key, after=revision)} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\nCookie: {cookie}\r\n\r\n".encode()
    )
    deadline = time.monotonic() + 2
    while runtime.events_waiters(key) != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runtime.events_waiters(key) == 1
    s.close()  # the browser leaves mid-wait
    started = time.monotonic()
    while runtime.events_waiters(key) and time.monotonic() - started < 2.5:
        time.sleep(0.02)
    assert runtime.events_waiters(key) == 0
    assert time.monotonic() - started < 1  # freed at the hang-up, not at the 3 s cap


def test_a_quiet_wait_answers_through_the_home_free_proxy(host, project, live_app):
    """The real Home Free app proxy, with its own default client (≈5 s read timeout): a quiet
    poll, asking for far more than the cap, still answers 200 — after the 3 s wait."""
    from agent_sessions.homefree.appproxy import AppProxyTarget
    from agent_sessions.homefree.mux import Mux

    port, cfg = live_app
    cookie, csrf = _login(port, cfg)
    key = _create(port, cfg, cookie, csrf, project)
    revision = _revision(port, cookie, key)

    async def through_proxy() -> tuple[dict, bytes, float]:
        proxy = AppProxyTarget(app_port=port)
        mux: dict[str, Mux] = {}
        mux["browser"] = Mux(is_initiator=True, on_send=lambda f: mux["agent"].feed(f))
        mux["agent"] = Mux(
            is_initiator=False,
            on_send=lambda f: mux["browser"].feed(f),
            on_stream=lambda s: asyncio.ensure_future(proxy.serve(s)),
        )
        path = f"/api/structured/sessions/{key}/events?after={revision}&wait=30"
        info = {"k": "http", "method": "GET", "path": path, "headers": {"Cookie": cookie}}
        started = time.monotonic()
        stream = mux["browser"].open(json.dumps(info).encode())
        await stream.end()
        head = b""
        while len(head) < 4:
            part = await stream.read(4 - len(head))
            assert part, "the proxy reset the stream"
            head += part
        size = struct.unpack(">I", head)[0]
        raw = b""
        while True:
            part = await stream.read()
            if not part:
                break
            raw += part
        return json.loads(raw[:size]), raw[size:], time.monotonic() - started

    meta, body, took = asyncio.run(through_proxy())
    assert meta["status"] == 200, body
    page = json.loads(body)
    assert page["events"] == [] and page["next_cursor"] == revision
    assert 2.5 <= took < 5, took
