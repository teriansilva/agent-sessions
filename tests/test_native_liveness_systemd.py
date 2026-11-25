"""The process floor against a REAL systemd unit (#1724): a real child the agent started keeps
the worker alive, a grandchild through an MCP server counts, a SIGKILLed worker is settled from
its last inventory, and Codex's background terminal is reported by the engine.

Needs a systemd user manager (skipped in CI containers, like the containment test it follows).
The agent is the scripted fake (`tests/fake_native.py`); every process here is the test's own.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

import test_manifest_api
from agent_sessions import native_containment, native_journal, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from test_native_runtime import ENGINES, FAKE, ident, project, settle  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.path.exists(f"/run/user/{os.getuid()}/systemd/private")
    or not os.path.exists("/usr/bin/systemd-run")
    or os.environ.get("CI") == "true",
    reason="needs a real systemd user manager (not available in CI containers)",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def real_codex(tmp_home, tmp_path, monkeypatch):
    monkeypatch.setattr(native_runtime, "HOST", native_runtime.Host())
    bindir = tmp_path / "engine-bin"
    bindir.mkdir()
    bindir.chmod(0o755)
    script = bindir / "codex"
    script.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    script.chmod(0o755)
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_BIN", str(script))
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    providers = list(registry._PROVIDERS)
    doc = test_manifest_api._api(name=ENGINES["codex"], source="codex", kind="codex-app-server")
    providers.append(test_manifest_api._provider(doc, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    return ENGINES["codex"]


async def _until(predicate, seconds=40.0):
    for _ in range(int(seconds / 0.1)):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


async def _native(key):
    return (await runtime.snapshot(key))["native"]


def _processes(native):
    return [t for t in native["background_tasks"] if t["source"] == "process"]


async def _turn(key, text):
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text=text)
    await settle(key, turn)


async def _create(engine, work):
    return (await runtime.create_session(engine, str(work), operation_id=ident()))["session_key"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][0] not in "ZX"


@pytest.mark.anyio
async def test_a_detached_job_keeps_the_worker_alive_and_then_it_idles_out(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _create(real_codex, project)
    try:
        # `setsid sleep 15`: Codex reports nothing (measured). Long enough to outlast the journal
        # cadence (a process is listed once it lived a tick, ≤ 5 s here) plus the wait below.
        await _turn(key, "SPAWN:15")
        pid = int((project / "spawned-pid").read_text())
        native = await _until(lambda: _with_process(key))
        (job,) = _processes(native)
        assert job["comm"] == "sleep" and job["pid"] == pid and job["started_at"]
        assert set(job) == {"task_id", "source", "state", "pid", "comm", "started_at", "updated_at"}
        await asyncio.sleep(3.5)  # well past the 2 s idle timeout
        assert (await _native(key))["worker"] == native["worker"] and _alive(pid)
        # The job ends; the next ticks see an idle worker, which exits and says why.
        await _until(lambda: _gone(key), 30)
        (row,) = (await _native(key))["worker_ends"]
        assert row["reason"] == "idle" and row["idle_for"] >= 2
    finally:
        await runtime.stop(key)


async def _with_process(key):
    native = await _native(key)
    return native if _processes(native) else None


async def _gone(key):
    return (await _native(key))["worker"] is None


@pytest.mark.anyio
async def test_a_grandchild_through_an_mcp_server_counts_and_stop_settles_it(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    (project / "start-fake-mcp").write_text("")  # a long-lived child before readiness
    key = await _create(real_codex, project)
    try:
        await _turn(key, "hello")
        # The MCP server is baseline: it alone never keeps the worker alive, so it idles out.
        try:
            await _until(lambda: _gone(key), 30)
        except AssertionError:
            native = await _native(key)
            raise AssertionError(f"still live: {native['stop_inventory']}") from None

        # The relaunch waits until the old unit is proved gone; its ExecStopPost exit record
        # (#1643) can take seconds under load. A real operator's send simply retries later.
        old = native_containment.WorkerIdentity(
            native_state.read_session(key.partition(":")[2])["current_worker"]
        )

        async def collected():
            seen = native_runtime.HOST.show(old)
            return seen is not None and (
                seen.load_state == "not-found" or seen.active_state in {"inactive", "failed"}
            )

        await _until(collected, 60)
        await _turn(key, "hello again")  # a successor, with the MCP server in ITS baseline
        (project / "grandchild-pid").unlink(missing_ok=True)
        (project / "spawn-grandchild").write_text("")
        native = await _until(lambda: _with_process(key))
        grandchild = int((project / "grandchild-pid").read_text())
        assert [p["pid"] for p in _processes(native)] == [grandchild]
        assert [p["comm"] for p in _processes(native)] == ["sleep"]  # not the python MCP
        await asyncio.sleep(3.0)
        assert (await _native(key))["worker"] == native["worker"]
        inventory = native["stop_inventory"]
        # A stable, complete list: the exact confirmation goes.
        stopped = await runtime.operator_stop(
            key, worker=inventory["worker"], digest=inventory["digest"]
        )
        assert stopped["containment"] == "gone" and not _alive(grandchild)
        (row,) = [r for r in (await _native(key))["worker_ends"] if r["reason"] == "stop"]
        assert [p["pid"] for p in row["processes"]] == [grandchild]
    finally:
        await runtime.stop(key)


@pytest.mark.anyio
async def test_a_sigkilled_worker_is_settled_from_its_last_inventory(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _create(real_codex, project)
    try:
        await _turn(key, "SPAWN:120")
        pid = int((project / "spawned-pid").read_text())
        native = await _until(lambda: _with_process(key))
        worker = native["worker"]
        os.kill(native_state.read_lifecycle(worker)["pid"], signal.SIGKILL)
        await _until(lambda: _gone(key), 30)
        assert not _alive(pid)  # KillMode=control-group took the job with the worker
        (row,) = (await _native(key))["worker_ends"]
        assert row["reason"] == "worker_ended" and row["settled"] is False
        assert [p["pid"] for p in row["processes"]] == [pid]
    finally:
        await runtime.stop(key)


@pytest.mark.anyio
async def test_a_codex_background_terminal_is_an_engine_task_until_it_completes(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key = await _create(real_codex, project)
    try:
        await _turn(key, "TERMINAL:6")

        async def terminal():
            native = await _native(key)
            engine = [t for t in native["background_tasks"] if t["source"] == "engine"]
            return engine or None

        (task,) = await _until(terminal)
        assert task["task_type"] == "background_terminal" and "sleep 6" in task["description"]
        await asyncio.sleep(3.0)
        assert (await _native(key))["worker"] is not None
        await _until(lambda: _gone(key), 30)
        (row,) = (await _native(key))["worker_ends"]
        assert row["reason"] == "idle"
    finally:
        await runtime.stop(key)


async def _looping(engine, work):
    key = await _create(engine, work)
    await _turn(key, "LOOP")
    loop = int((work / "loop-pid").read_text())

    async def shown():
        native = await _native(key)
        pids = [p["pid"] for p in native["stop_inventory"]["processes"]]
        return native if loop in pids else None

    return key, loop, await _until(shown)


@pytest.mark.anyio
async def test_a_loop_refuses_an_exact_stop_and_stop_everything_ends_it_in_one_more_tap(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    """A polling loop starts a new `sleep` every 0.2 s, so no two scans see the same set: an
    exact confirmation is refused (409, the current list), and the refresh offers Stop
    everything, which ends it with one more tap — never an endless refresh (operator decision
    on PR #1757)."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)  # tick 0.5 s, journal ≤ 5 s
    key, loop, native = await _looping(real_codex, project)
    try:
        await asyncio.sleep(1.0)  # several loop children since the list was observed
        confirmed = native["stop_inventory"]
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.operator_stop(key, worker=confirmed["worker"], digest=confirmed["digest"])
        assert refused.value.status == 409 and refused.value.reason == "inventory_changed"
        assert _alive(loop)  # nothing stopped
        fresh = refused.value.inventory
        stopped = await runtime.operator_stop(key, worker=fresh["worker"], everything=True)
        assert stopped["containment"] == "gone" and not _alive(loop)
        rows = (await _native(key))["worker_ends"]
        assert [r["reason"] for r in rows] == ["stop"]
    finally:
        await runtime.stop(key)


@pytest.mark.anyio
async def test_a_loops_children_never_reach_the_journal_one_by_one(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    """Liveness sees every process; the journal sees only ones that lived a tick, and a
    process-only change at most once per interval (5 s here, 5 min in production)."""
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    key, loop, native = await _looping(real_codex, project)
    try:
        start = native["stop_inventory"]["observed_at"]
        await asyncio.sleep(6.0)  # ~30 loop children come and go meanwhile
        root = native_runtime._journal_root(native_runtime._api_provider(real_codex))
        journal = native_journal.read(root, key.partition(":")[2])
        inventories = [
            e
            for e in journal.events
            if e["event"]["kind"] == "background_inventory" and e["ts"] >= start
        ]
        assert len(inventories) <= 3, [e["event"]["data"]["processes"] for e in inventories]
        assert all(
            {p["comm"] for p in e["event"]["data"]["processes"]} == {"sh"} for e in inventories
        )
        assert (await _native(key))["worker"] == native["worker"]  # and it stays alive
    finally:
        await runtime.stop(key)


def _state(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]


@pytest.mark.anyio
async def test_an_independent_process_born_during_the_claim_is_never_stopped_exactly(
    real_codex,
    project,  # noqa: F811
    monkeypatch,
):
    """Hermes' probe on a real unit: the dialog shows a detached job; while the worker's exact
    Stop waits in its claim (the test holds a shared lock on the journal, so the claim's
    exclusive lock waits), an MCP server present since readiness — a separate parent — starts
    a long-lived child. The freeze finds it, nothing it was not confirmed for is ended: 409,
    every process resumed and running, `stop_aborted` journaled, the session open."""
    import fcntl

    from agent_sessions import chat_store

    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 2.0)
    (project / "start-fake-mcp").write_text("")  # the separate, baseline parent
    key = await _create(real_codex, project)
    sid = key.partition(":")[2]
    try:
        await _turn(key, "SPAWN:120")
        job = int((project / "spawned-pid").read_text())

        async def listed():
            native = await _native(key)
            return (
                native
                if [p["pid"] for p in native["stop_inventory"]["processes"]] == [job]
                else None
            )

        shown = (await _until(listed))["stop_inventory"]
        root = native_runtime._journal_root(native_runtime._api_provider(real_codex))
        fd = os.open(chat_store._path(root, sid), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)  # reads go on; the Stop's claim (exclusive) waits
            stopping = asyncio.ensure_future(
                runtime.operator_stop(key, worker=shown["worker"], digest=shown["digest"])
            )
            await asyncio.sleep(1.5)  # checked, now waiting in its claim
            assert not stopping.done()
            (project / "spawn-grandchild").write_text("")
            for _ in range(100):
                if (project / "grandchild-pid").exists():
                    break
                await asyncio.sleep(0.05)
            grandchild = int((project / "grandchild-pid").read_text())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        with pytest.raises(runtime.StructuredError) as refused:
            await asyncio.wait_for(stopping, 30)
        assert refused.value.status == 409 and refused.value.reason == "inventory_changed"
        assert grandchild in [p["pid"] for p in refused.value.inventory["processes"]]
        for pid in (job, grandchild):
            assert _alive(pid) and _state(pid) != "T"  # resumed, running
        journal = native_journal.read(root, sid)
        aborted = [e for e in journal.events if e["event"]["kind"] == "stop_aborted"]
        assert [e["event"]["data"]["reason"] for e in aborted] == ["changed"]
        assert native_state.read_session(sid)["closed"] is False
        assert (await _native(key))["worker"] is not None
    finally:
        await runtime.stop(key)
