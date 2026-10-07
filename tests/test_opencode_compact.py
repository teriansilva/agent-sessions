"""Compaction uses disposable SQLite stores and only test-owned positive-PID processes.

Real locks, WAL readers and VACUUM establish the exclusion and integrity claims; barriers
separate cancellation of an await from the lifetime of a worker that still owns the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import maintenance
from agent_sessions import opencode_admission as admission
from agent_sessions import opencode_compact as compact


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "opencode.db"
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE session (id INTEGER PRIMARY KEY, body BLOB)")
        con.executemany("INSERT INTO session(body) VALUES (?)", [(b"x" * 4096,)] * 512)
        con.commit()
        con.execute("DELETE FROM session WHERE id > 10")
        con.commit()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(path))
    monkeypatch.setattr(compact, "sqlite_temp_dir", lambda: tmp_path)
    return path


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(
        compact,
        "holders",
        lambda _path, _engine=None: {
            "pids": [],
            "unknown": False,
            "unknown_processes": 0,
            "scan_incomplete": False,
        },
    )


def run_worker(worker=None, phase=lambda _: None):
    accepted = []
    result = (worker or compact.Worker()).run(accepted.append, phase)
    return result, accepted


def ready_line(proc):
    assert select.select([proc.stdout], [], [], 10)[0], "test child did not become ready"
    return proc.stdout.readline().strip()


def test_shared_launches_exclude_compaction_and_exclusive_excludes_launches():
    a = admission.acquire(exclusive=False)
    b = admission.acquire(exclusive=False)
    try:
        assert a is not None and b is not None
        assert admission.acquire(exclusive=True) is None
    finally:
        a.release()
        b.release()
    with admission.acquire(exclusive=True):
        assert admission.acquire(exclusive=False) is None
        assert admission.acquire(exclusive=True) is None


def test_cross_process_crash_releases_lock():
    code = """
from agent_sessions.opencode_admission import acquire
import sys
with acquire(exclusive=True):
 print('ready', flush=True)
 sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.pid > 1
        assert ready_line(child) == "ready"
        assert admission.acquire(exclusive=False) is None
        child.kill()  # exactly the process this test owns
        child.wait(timeout=10)
        with admission.acquire(exclusive=False):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_admission_is_not_inherited_even_with_close_fds_false():
    guard = admission.acquire(exclusive=True)
    st = os.fstat(guard.fd)
    code = """
import os, sys
from pathlib import Path
identity = tuple(map(int, sys.argv[1:]))
for p in Path('/proc/self/fd').iterdir():
 try: st = p.stat()
 except FileNotFoundError: continue
 assert (st.st_dev, st.st_ino) != identity, 'inherited maintenance lock'
print('clean')
"""
    try:
        assert not os.get_inheritable(guard.fd)
        out = subprocess.run(
            [sys.executable, "-c", code, str(st.st_dev), str(st.st_ino)],
            close_fds=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "clean"
    finally:
        guard.release()


@pytest.mark.anyio
async def test_launch_refuses_compaction_and_leaves_other_engines_alone():
    with admission.acquire(exclusive=True):
        with pytest.raises(admission.Unavailable):
            await admission.for_launch("opencode")
        assert await admission.for_launch("claude") is None


@pytest.mark.anyio
async def test_cancelled_launch_acquisition_drains_before_releasing(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    original = admission.acquire

    def acquire(**kwargs):
        guard = original(**kwargs)
        entered.set()
        assert finish.wait(10)
        return guard

    monkeypatch.setattr(admission, "acquire", acquire)
    task = asyncio.create_task(admission.for_launch("opencode"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert original(exclusive=True) is None
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with original(exclusive=True):
        pass


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [True, False])
async def test_spawn_cancellation_and_timeout_hold_admission_until_actual_handoff(cancel):
    entered, finish = asyncio.Event(), asyncio.Event()
    proc = SimpleNamespace(returncode=None, killed=False)

    def kill():
        proc.killed = True
        proc.returncode = -9

    async def reap():
        return proc.returncode

    proc.kill, proc.wait = kill, reap

    async def create():
        entered.set()
        await finish.wait()
        return proc

    guard = await admission.for_launch("opencode")
    task = asyncio.create_task(admission.spawn(create, guard, timeout=0.01 if not cancel else 10))
    await entered.wait()
    if cancel:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    else:
        await asyncio.sleep(0.03)
    assert admission.acquire(exclusive=True) is None
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert proc.killed
    with admission.acquire(exclusive=True):
        pass


def test_spawned_opencode_cmdline_and_alias_fd_are_seen(database, tmp_path, monkeypatch):
    procroot = tmp_path / "proc"
    procroot.mkdir()
    binary = str(tmp_path / "opencode")
    monkeypatch.setattr(compact.discover, "resolve", lambda _: binary)
    monkeypatch.setattr(compact, "PROC", procroot)
    # A process whose argv carries the provider binary, including the dtach handoff window.
    p = procroot / "456"
    (p / "fd").mkdir(parents=True)
    (p / "cmdline").write_bytes(b"/usr/bin/dtach\0-c\0sock\0" + os.fsencode(binary) + b"\0")
    # A renamed/hard-linked holder is still matched by file identity.
    q = procroot / "789"
    (q / "fd").mkdir(parents=True)
    (q / "cmdline").write_bytes(b"python\0")
    alias = tmp_path / "alias"
    os.link(database, alias)
    (q / "fd/7").symlink_to(alias)
    assert compact.holders(database)["pids"] == [456, 789]


def test_unreadable_process_and_exhausted_scan_remain_unknown(database, tmp_path, monkeypatch):
    root = tmp_path / "proc"
    (root / "12/fd").mkdir(parents=True)
    cmdline = root / "12/cmdline"
    cmdline.write_bytes(b"other\0")
    monkeypatch.setattr(compact, "PROC", root)
    original = Path.read_bytes

    def unreadable(path):
        if path == cmdline:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    out = compact.holders(database)
    assert out["unknown"] and out["unknown_processes"] == 1
    monkeypatch.setattr(compact, "SCAN_SECONDS", 0)
    assert compact.holders(database)["scan_incomplete"]


def test_real_test_owned_file_holder_is_seen(database, tmp_path, monkeypatch):
    code = "import sys; f=open(sys.argv[1], 'rb'); print('ready',flush=True); sys.stdin.read()"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    root = tmp_path / "proc"
    root.mkdir()
    (root / str(child.pid)).symlink_to(Path("/proc") / str(child.pid))
    monkeypatch.setattr(compact, "PROC", root)
    try:
        assert ready_line(child) == "ready"
        assert compact.holders(database)["pids"] == [child.pid]
    finally:
        child.communicate(input="", timeout=10)


def test_all_blockers_reported_together_and_mutation_refused(database, monkeypatch):
    monkeypatch.setattr(compact, "holders", lambda _p, _e=None: {"pids": [42], "unknown": True})
    monkeypatch.setattr(compact.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    before = database.read_bytes()
    out = compact.measure()
    assert {b["code"] for b in out["blockers"]} == {"held", "holders_unknown", "database_space"}
    assert out["disk"]["database_required"] == 3 * (out["db_bytes"] + out["wal_bytes"])
    result, accepted = run_worker()
    assert result["state"] == "refused" and result["vacuum"] == "not_started"
    assert not accepted and database.read_bytes() == before


def test_unknown_measurement_is_not_zero(database, idle, monkeypatch):
    monkeypatch.setattr(
        compact,
        "_connect",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("cannot read")),
    )
    out = compact.measure()
    assert out["reclaimable_bytes"] is None and not out["available"]
    assert "measurement" in {b["code"] for b in out["blockers"]}


def test_real_vacuum_reclaims_pages_without_deleting_rows(database, idle):
    before = compact.measure()
    assert before["available"] and before["reclaimable_bytes"] > 1_000_000
    result, accepted = run_worker()
    assert accepted == [True]
    assert result["vacuum"] == "done" and result["checkpoint"] == "done"
    assert result["bytes_freed"] > 1_000_000
    with contextlib.closing(sqlite3.connect(database)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT count(*) FROM session").fetchone() == (10,)
        assert con.execute("PRAGMA freelist_count").fetchone() == (0,)
    with admission.acquire(exclusive=False):
        pass


def test_late_external_reader_defers_checkpoint_without_undoing_vacuum(database, idle):
    reader = sqlite3.connect(database)

    def phase(value):
        if value == "vacuum":
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM session").fetchone()

    try:
        result, accepted = run_worker(phase=phase)
        assert accepted == [True]
        assert result["vacuum"] == "done" and result["checkpoint"] == "deferred"
        assert result["checkpoint_result"][0] == 1
        assert result["state"] == "done"
        assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        reader.close()
    with contextlib.closing(sqlite3.connect(database)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("SELECT count(*) FROM session").fetchone() == (10,)


def test_late_external_writer_refuses_vacuum_and_preserves_integrity(database, idle):
    writer = sqlite3.connect(database)
    try:
        result, _ = run_worker(
            phase=lambda p: writer.execute("BEGIN IMMEDIATE") if p == "vacuum" else None
        )
        assert result["vacuum"] == "rolled_back"
        assert result["checkpoint"] == "not_started"
        assert result["state"] == "failed"
    finally:
        writer.close()
    with contextlib.closing(sqlite3.connect(database)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_shutdown_after_vacuum_does_not_relabel_it_as_rolled_back(database, idle):
    worker = compact.Worker()
    result, _ = run_worker(
        worker, phase=lambda p: worker.interrupt() if p == "checkpoint" else None
    )
    assert result["vacuum"] == "done" and result["checkpoint"] == "failed"
    assert result["state"] == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_job", [False, True])
async def test_shutdown_and_disconnect_keep_slot_and_lock_until_worker_exits(
    monkeypatch, cancel_job
):
    entered, finish, interrupted = threading.Event(), threading.Event(), threading.Event()
    baseline_tasks = asyncio.all_tasks()

    class Worker(compact.Worker):
        def interrupt(self):
            interrupted.set()
            super().interrupt()

        def run(self, ready, phase):
            with admission.acquire(exclusive=True):
                entered.set()
                ready(True)
                assert finish.wait(10)
                return {
                    "state": "interrupted",
                    "vacuum": "rolled_back",
                    "checkpoint": "not_started",
                }

    monkeypatch.setattr(compact, "Worker", Worker)
    runner = maintenance.Runner()
    service = compact.Service(runner)
    request = asyncio.create_task(service.start())
    assert await asyncio.to_thread(entered.wait, 10)
    request.cancel()  # disconnect never cancels the job
    with contextlib.suppress(asyncio.CancelledError):
        await request
    if cancel_job:
        # Mirror event-loop teardown, which cancels *all* Tasks. A cancelled to_thread
        # wrapper is not proof its actual OS thread exited.
        for task in asyncio.all_tasks() - baseline_tasks:
            task.cancel()
        await asyncio.sleep(0)
        service.task.cancel()
    shutdown = asyncio.create_task(service.shutdown())
    try:
        assert await asyncio.to_thread(interrupted.wait, 10)
        shutdown.cancel()  # repeated cancellation cannot release a still-owned SQLite thread
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert runner.busy_info()["job"] == "opencode_compact"
        with pytest.raises(maintenance.MaintenanceBusy):
            runner.start("prune", lambda: asyncio.sleep(0))
        assert admission.acquire(exclusive=False) is None
    finally:
        finish.set()
    await shutdown
    await asyncio.sleep(0)
    assert runner.busy_info() is None
    assert service.snapshot()["result"]["vacuum"] == "rolled_back"
    with admission.acquire(exclusive=False):
        pass


def test_compaction_api_auth_csrf_strict_body_and_unknown_job(auth_cfg, fake_jsonl):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    url = "/api/maintenance/compact"
    assert c.get(url).status_code == 401
    assert c.post(url, json={"confirm": True}).status_code == 401
    assert (
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf = c.get("/api/config").json()["csrf"]
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}
    assert c.post(url, json={"confirm": True}).status_code == 403
    assert (
        c.post(
            url, json={"confirm": True}, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    for body in (
        None,
        [],
        {},
        {"confirm": 1},
        {"confirm": False},
        {"confirm": "true"},
        {"confirm": True, "path": "/foreign.db"},
        # #853 P3: a target may be named, but only an engine that selects the compaction kind —
        # never an arbitrary engine's store, and never a non-string.
        {"confirm": True, "engine": "claude"},
        {"confirm": True, "engine": "../opencode"},
        {"confirm": True, "engine": 1},
    ):
        assert c.post(url, json=body, headers=headers).status_code == 422
    assert c.get(url + "?engine=claude").status_code == 422
    assert c.get(url).json()["targets"] == ["opencode"]
    assert c.get(url + "?engine=opencode").status_code == 200
    assert c.get(url).json()["job"] is None
    assert c.get(url + "?job_id=old-job").status_code == 404
    refused = c.post(url, json={"confirm": True}, headers=headers)
    assert refused.status_code == 409
    assert refused.json()["job"]["result"]["blockers"][0]["code"] == "missing"
    assert refused.json()["job"]["engine"] == "opencode"


@pytest.mark.anyio
async def test_202_job_polling_is_stable_and_busy_prevents_resubmission(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from agent_sessions.routes import maintenance as routes

    finish = threading.Event()

    class Worker(compact.Worker):
        def run(self, ready, phase):
            with admission.acquire(exclusive=True):
                ready(True)
                phase("vacuum")
                assert finish.wait(10)
                return {"state": "done", "vacuum": "done", "checkpoint": "deferred"}

    monkeypatch.setattr(compact, "Worker", Worker)
    monkeypatch.setattr(compact, "measure", lambda **_kw: {"available": False, "blockers": []})
    app = FastAPI()
    routes.register(app, logged_in=lambda: "test", csrf_guard=lambda: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            result = await c.post("/api/maintenance/compact", json={"confirm": True})
            assert result.status_code == 202
            job_id = result.json()["job"]["id"]
            for endpoint, body in (
                ("compact", {"confirm": True}),
                ("prune", {"categories": ["stale_sockets"]}),
            ):
                busy = await c.post("/api/maintenance/" + endpoint, json=body)
                assert busy.status_code == 409 and busy.json()["busy"]["job"] == "opencode_compact"
            current = await c.get("/api/maintenance/compact", params={"job_id": job_id})
            assert current.json()["job"]["id"] == job_id
            assert (await c.get("/api/maintenance/compact?job_id=not-this-job")).status_code == 404
        finally:
            finish.set()
        await app.state.opencode_compaction.task
        done = await c.get("/api/maintenance/compact", params={"job_id": job_id})
        assert done.json()["job"]["result"]["checkpoint"] == "deferred"
        assert done.json()["runner"] is None
        # A new job replaces retention explicitly; the former id must return 404.
        await c.post("/api/maintenance/compact", json={"confirm": True})
        assert (
            await c.get("/api/maintenance/compact", params={"job_id": job_id})
        ).status_code == 404
        await app.state.opencode_compaction.shutdown()


def test_shutdown_during_vacuum_rolls_back_real_database(database, idle, monkeypatch):
    worker = compact.Worker()
    interrupted = []

    class Connection(sqlite3.Connection):
        def set_progress_handler(self, handler, interval):
            def progress():
                # The VM is executing real VACUUM now; interrupting before execute would be
                # a no-op, and a tiny DB could finish before the production 1000-op callback.
                interrupted.append(True)
                worker.interrupt()
                return handler()

            super().set_progress_handler(progress, 1)

    original = compact._connect
    monkeypatch.setattr(
        compact,
        "_connect",
        lambda path, *, write: sqlite3.connect(path, factory=Connection)
        if write
        else original(path, write=False),
    )
    result, accepted = run_worker(worker)
    assert accepted == [True] and interrupted
    assert result["state"] == "interrupted" and result["vacuum"] == "rolled_back"
    with contextlib.closing(sqlite3.connect(database)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA freelist_count").fetchone()[0] > 0


def test_separate_filesystems_need_database_and_temp_budgets(database, idle, tmp_path, monkeypatch):
    temp = tmp_path / "other-filesystem"
    temp.mkdir()
    monkeypatch.setattr(compact, "sqlite_temp_dir", lambda: temp)
    original = Path.stat

    def stat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return SimpleNamespace(st_dev=value.st_dev + 1) if path == temp else value

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(
        compact.shutil, "disk_usage", lambda p: SimpleNamespace(free=0 if p == temp else 10**12)
    )
    info = compact.measure()
    assert info["disk"]["database_required"] == 2 * (info["db_bytes"] + info["wal_bytes"])
    assert info["disk"]["temp_required"] == info["db_bytes"]
    assert {b["code"] for b in info["blockers"]} == {"temp_space"}
    result, accepted = run_worker()
    assert result["vacuum"] == "not_started" and not accepted


def test_launch_site_inventory_requires_review_when_another_route_can_spawn():
    """Inventory the provider launch consumers, plus both process-creation call sites.

    This is an early warning for new launch paths, not proof of a runtime lock. The websocket,
    headless and worker barrier tests separately establish that runtime ordering.
    """
    import ast
    from collections import Counter

    from agent_sessions import engines, headless_dispatch, plugins, webterm

    root = Path(compact.__file__).parent
    consumers = Counter()
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if "engines" in parts or "plugins" in parts:
            # Providers build argv and resolve the entrypoint (#853 P2: the store kinds under
            # engines/, the manifest-built `PluginProvider` + provenance under plugins/); they do
            # not execute it — pinned for plugins/ below.
            continue
        tree = ast.parse(path.read_text())
        watched = ("launch_argv", "new_launch_argv", "entrypoint_path")
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Attribute) and call.func.attr in watched:
                consumers[str(path.relative_to(root)), call.func.attr] += 1
            # A method REFERENCE handed to a runner — `asyncio.to_thread(prov.launch_argv, …)`,
            # the off-loop form (#853 P2) — is the same consumer and must still be counted.
            for arg in call.args:
                if isinstance(arg, ast.Attribute) and arg.attr in watched:
                    consumers[str(path.relative_to(root)), arg.attr] += 1
    assert consumers == {
        # resume's provider argv + `ptybridge.launch_argv`. ATTACH builds no launch argv any more
        # (#853 P2: `dtach -a` never execs the agent), so it is no longer a consumer.
        ("routes/terminal.py", "launch_argv"): 2,
        ("routes/terminal.py", "new_launch_argv"): 1,
        ("headless_dispatch.py", "launch_argv"): 1,
        ("headless_dispatch.py", "new_launch_argv"): 1,
        # The preflight probe asks for the provenance-checked binary directly instead of reading
        # argv[0] off a throwaway `new_launch_argv` (#853 §2b) — the same consumer, renamed.
        ("headless_dispatch.py", "entrypoint_path"): 1,
        # #1278: native readiness probes the admitted source's `--version`, and a worker launch
        # records that same provenance-checked path for its contained native child. Native API
        # sources are only codex-app-server / claude-stream-json — never opencode, whose
        # maintenance admission this inventory guards.
        ("native_runtime.py", "entrypoint_path"): 2,
        # …and builds the fixed systemd worker command (`native_containment.launch_argv`),
        # which starts BattleLab's own worker, never a provider's console argv.
        ("native_runtime.py", "launch_argv"): 1,
    }
    for module, name in ((webterm, "create_subprocess_exec"), (headless_dispatch, "_popen")):
        tree = ast.parse(Path(module.__file__).read_text())
        names = [
            call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute | ast.Name)
        ]
        assert names.count(name) == 1
    # OpenCode's provider is argv-only; no hidden subprocess import bypasses those consumers.
    provider_tree = ast.parse(
        Path(sys.modules[engines.OpenCodeProvider.__module__].__file__).read_text()
    )
    imports = [
        n.name
        for node in ast.walk(provider_tree)
        if isinstance(node, ast.Import)
        for n in node.names
    ]
    assert "subprocess" not in imports
    # Since #853 P2 the live opencode provider is a manifest-built `PluginProvider` wrapping that
    # kind: the provider, provenance and manifest code are argv/path-only too. P5 adds a bounded
    # runner whose ONLY current plugin caller verifies a feed using ssh-keygen. Any agent probe
    # caller added later must enter this inventory and prove its maintenance admission ordering.
    runner_consumers = Counter()
    for path in Path(plugins.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        mods = [
            n.name for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names
        ]
        mods += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert "subprocess" not in mods or path.name in ("runner.py", "process.py"), path.name
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "runner"
            ):
                runner_consumers[path.name, call.func.attr] += 1
    assert runner_consumers == {("feed.py", "run"): 1}
    # The separate temporary PTY owns one spawn and fixed systemctl teardown; admission is
    # tested at the actual spawn in test_plugin_process, including cancellation while acquiring.
    tree = ast.parse((Path(plugins.__file__).parent / "process.py").read_text())
    assert (
        sum(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "create_subprocess_exec"
            for n in ast.walk(tree)
        )
        == 1
    )


@pytest.mark.anyio
async def test_shutdown_interrupt_does_not_block_the_loop_behind_connection_close(monkeypatch):
    closing, finish, exited = threading.Event(), threading.Event(), threading.Event()

    class Worker(compact.Worker):
        def run(self, ready, phase):
            with admission.acquire(exclusive=True), self._mutex:
                ready(True)
                closing.set()
                finish.wait(10)  # bounded even against the old event-loop-blocking interrupt
            exited.set()
            return {"state": "done", "vacuum": "done", "checkpoint": "done"}

    monkeypatch.setattr(compact, "Worker", Worker)
    service = compact.Service(maintenance.Runner())
    await service.start()
    # `start()` returns on `ready(True)`; the worker sets `closing` on its own thread right after,
    # so wait for it rather than assert it already happened (a loaded CI runner lost that race).
    assert await asyncio.to_thread(closing.wait, 5)
    shutdown = asyncio.create_task(service.shutdown())
    try:
        assert await asyncio.to_thread(service.worker.stop.wait, 5)
        assert not exited.is_set(), "shutdown blocked the loop until the SQLite close returned"
        assert not shutdown.done()
        assert admission.acquire(exclusive=False) is None
    finally:
        finish.set()
        await shutdown
