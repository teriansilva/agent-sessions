"""Contained native workers end to end, with scripted native agents (#1278).

The real ``native_worker`` module runs as a separate process for every generation, speaking
the real codecs to ``fake_native.py`` over stdio and the real private socket to this process.
Only the service manager is substituted (``FakeHost``): it starts the exact command systemd
would run and reports unit/cgroup evidence from the process's liveness. The real-systemd
path is exercised by ``test_real_systemd_contains_and_stops_a_worker`` where available.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

import test_manifest_api
from agent_sessions import (
    native_containment,
    native_journal,
    native_ownership,
    native_runtime,
    native_state,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

FAKE = Path(__file__).with_name("fake_native.py")
ENGINES = {"codex": "codex-api", "claude": "claude-api", "opencode": "opencode-api"}
KINDS = {"codex": "codex-app-server", "claude": "claude-stream-json", "opencode": "opencode-acp"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeHost(native_runtime.Host):
    """Runs the exact contained command as a plain child; evidence follows its liveness."""

    def __init__(self, runtime_dir: str):
        self.runtime = runtime_dir
        self.procs: dict[str, subprocess.Popen] = {}
        self.invocations: dict[str, str] = {}
        self.launches: list[list[str]] = []

    def runtime_dir(self) -> str:
        return self.runtime

    def available(self) -> str | None:
        return None

    def launch(self, argv: list[str]) -> bool:
        self.launches.append(argv)
        assert argv[:2] == ["/usr/bin/systemd-run", "--user"]
        assert "--property=KillMode=control-group" in argv
        unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
        assert unit not in self.procs, "a unit name was reused"
        command = argv[argv.index("--") + 1 :]
        assert command[:2] == ["/usr/bin/env", "-i"]
        env, index = {}, 2
        while "=" in command[index] and not command[index].startswith("/"):
            key, value = command[index].split("=", 1)
            env[key] = value
            index += 1
        cwd = next(a.split("=", 1)[1] for a in argv if a.startswith("--working-directory="))
        self.procs[unit] = subprocess.Popen(command[index:], env=env, cwd=cwd)
        self.invocations[unit] = uuid.uuid4().hex
        return True

    def _alive(self, unit: str) -> bool:
        proc = self.procs.get(unit)
        return proc is not None and proc.poll() is None

    def show(self, worker):
        unit = worker.unit
        if not self._alive(unit):
            return native_containment.UnitObservation(
                unit, "not-found", "inactive", "dead", None, None, 0, ""
            )
        return native_containment.UnitObservation(
            unit,
            "loaded",
            "active",
            "running",
            self.invocations[unit],
            f"/user.slice/{unit}",
            self.procs[unit].pid,
            "success",
        )

    def cgroup(self, control_group: str):
        unit = os.path.basename(control_group)
        state = "populated" if self._alive(unit) else "absent"
        return native_containment.CgroupObservation(control_group, state)

    def stop(self, argv: list[str]) -> bool:
        proc = self.procs.get(argv[-1])
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(10)
        return True

    def worker_running(self, worker, pid) -> bool:
        proc = self.procs.get(worker.unit)
        return proc is not None and proc.poll() is None and proc.pid == pid

    def kill(self, worker_id: str) -> None:
        proc = self.procs[native_containment.WorkerIdentity(worker_id).unit]
        proc.kill()
        proc.wait(10)

    def close(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)


@pytest.fixture
def host(tmp_home, tmp_path, monkeypatch):
    bindir = tmp_path / "engine-bin"
    bindir.mkdir()
    bindir.chmod(0o755)
    for name in ENGINES:
        script = bindir / name
        script.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
        script.chmod(0o755)
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_BIN", str(bindir / "codex"))
    monkeypatch.setenv("AGENT_SESSIONS_CLAUDE_BIN", str(bindir / "claude"))
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_BIN", str(bindir / "opencode"))
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    # The in-tree clients (#1311) are replaced by fixtures whose store is this test's own.
    providers = [p for p in registry._PROVIDERS if p.engine_id not in ENGINES.values()]
    for source, name in ENGINES.items():
        doc = test_manifest_api._api(name=name, source=source, kind=KINDS[source])
        providers.append(test_manifest_api._provider(doc, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    short = tempfile.mkdtemp(prefix="blnt-", dir="/tmp")  # AF_UNIX path budget, any TMPDIR
    fake = FakeHost(short)
    monkeypatch.setattr(native_runtime, "HOST", fake)
    yield fake
    fake.close()
    shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def project(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return work


def ident() -> str:
    return str(uuid.uuid4())


def frames(project: Path) -> list[dict]:
    path = project / "native-frames.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def settle(key: str, turn_id: str, *, state=("completed", "failed", "interrupted")):
    for _ in range(400):
        snap = await runtime.snapshot(key)
        turn = next((t for t in snap["turns"] if t["turn_id"] == turn_id), None)
        if turn is not None and turn["state"] in state:
            return snap, turn
        await asyncio.sleep(0.05)
    raise AssertionError(f"turn never reached {state}: {snap}")


async def pending(key: str):
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if snap["pending_requests"]:
            return snap
        await asyncio.sleep(0.05)
    raise AssertionError(f"no pending request: {snap}")


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode"])
async def test_create_submit_replay_and_reconnect_observe_one_native_write(host, project, source):
    engine = ENGINES[source]
    described = runtime.describe(engine)
    assert described.ready, described.reason
    assert described.operations == (
        "create",
        "start",  # the second phase of a skip-permissions creation (#1339)
        "snapshot",
        "submit",
        "decide",
        "events",
        "interrupt",
        "stop",
        "probe",
    )
    assert not described.mission_ready
    create = ident()
    created = await runtime.create_session(engine, str(project), operation_id=create)
    key = created["session_key"]
    assert key == f"{engine}:{create}"
    assert created["state"] == "idle" and created["native"]["native_id"]
    again = await runtime.create_session(engine, str(project), operation_id=create)
    assert again["session_key"] == key and len(host.launches) == 1  # replay: no second worker
    with pytest.raises(runtime.StructuredError) as conflict:
        await runtime.create_session(engine, str(project / "elsewhere"), operation_id=create)
    assert conflict.value.status == 409

    turn = ident()
    first = await runtime.submit_turn(key, operation_id=turn, text="hello")
    assert first["operation_id"] == turn
    snap, settled = await settle(key, turn)
    assert settled["reply"] == "echo:hello" and settled["state"] == "completed"
    # An exact repeat (a lost acknowledgement, a browser or web restart) observes the record.
    repeat = await runtime.submit_turn(key, operation_id=turn, text="hello")
    assert repeat["state"] == "completed"
    users = [
        f
        for f in frames(project)
        if f["frame"].get("method") in {"turn/start", "session/prompt"}
        or f["frame"].get("type") == "user"
    ]
    assert len(users) == 1
    with pytest.raises(runtime.StructuredError) as changed:
        await runtime.submit_turn(key, operation_id=turn, text="different")
    assert changed.value.status == 409
    page = await runtime.events(key, after=0)
    kinds = [event["kind"] for event in page["events"]]
    assert "turn_completed" in kinds and "text" in kinds
    assert all("capability" not in json.dumps(event) for event in page["events"])
    later = await runtime.events(key, after=page["next_cursor"])
    assert later["events"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode"])
async def test_native_crash_is_uncertain_never_resent_and_the_next_turn_resumes(
    host, project, source
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="hello")
    await settle(key, first)
    died = ident()
    await runtime.submit_turn(key, operation_id=died, text="DIE")
    snap, turn = await settle(key, died, state=("uncertain",))
    assert turn["reason"]
    native_before = snap["native"]["native_id"]
    # The same operation id only observes; nothing is written to any native process again.
    repeat = await runtime.submit_turn(key, operation_id=died, text="DIE")
    assert repeat["state"] == "uncertain"
    assert sum(1 for f in frames(project) if "DIE" in json.dumps(f["frame"])) == 1
    # A new turn starts a NEW generation that resumes the same native history.
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="again")
    snap, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:again"
    assert snap["native"]["native_id"] == native_before
    assert len(host.launches) == 2
    argvs = {tuple(f["argv"]) for f in frames(project)}
    if source == "claude":
        assert any(f"--resume={native_before}" in argv for argv in argvs)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode"])
async def test_interrupt_is_requested_and_stop_proves_containment_gone(host, project, source):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="HANG")
    await asyncio.sleep(0.3)
    out = await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
    assert out["handoff"] == "sent"
    snap, settled = await settle(key, turn, state=("interrupted",))
    assert (await runtime.probe(key))["containment"] == "live"
    stopped = await runtime.stop(key)
    assert stopped["containment"] == "gone"
    assert (await runtime.probe(key))["containment"] == "gone"
    # The closed generation never starts again: a later stop observes "gone" too.
    assert (await runtime.stop(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_codex_history_is_bound_before_any_turn_and_hidden_from_the_console(host, project):
    from agent_sessions import engines

    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    snap = await runtime.snapshot(key)
    native = snap["native"]["native_id"]
    intent = native_ownership.lookup(key)
    assert intent.state == "bound" and intent.native_id == native
    source = engines.get("codex")
    with pytest.raises(native_ownership.OwnershipError, match="belongs to an API session"):
        native_ownership.check_console(source, native)


@pytest.mark.anyio
async def test_claude_history_is_bound_before_the_worker_exists(host, project, monkeypatch):
    launched = []
    real = host.launch

    def launch(argv):
        session = argv[argv.index("--worker-id") - 0]  # noqa: F841 — argv shape asserted above
        launched.append(native_ownership.lookup(ENGINES["claude"] + ":" + created_id))
        return real(argv)

    created_id = ident()
    monkeypatch.setattr(host, "launch", launch)
    await runtime.create_session(ENGINES["claude"], str(project), operation_id=created_id)
    assert launched and launched[0].state == "bound"


@pytest.mark.anyio
async def test_a_creation_that_never_starts_is_stopped_and_discharged(host, project, monkeypatch):
    """A failed Codex creation must not leave the source's new-console launches blocked."""
    from agent_sessions import engines

    real = host.launch

    def broken(argv):
        argv = list(argv)
        argv[argv.index("--worker-id") + 1] = ident()  # the worker finds no config: refuses
        return real(argv)

    monkeypatch.setattr(host, "launch", broken)
    create = ident()
    with pytest.raises(runtime.StructuredError) as failed:
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=create)
    assert failed.value.status == 503
    key = f"{ENGINES['codex']}:{create}"
    assert native_ownership.lookup(key) is None  # discharged after containment proved gone
    native_ownership.check_console(engines.get("codex"), f"new-{ident()}")


@pytest.mark.anyio
async def test_a_stale_generation_refuses_to_start_a_second_native_child(host, project):
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    session_id = key.partition(":")[2]
    record = native_state.read_session(session_id)
    old_worker = record["current_worker"]
    old_argv = host.launches[0]
    await runtime.stop(key)
    starts = len([f for f in frames(project) if f["frame"].get("method") == "initialize"])
    # Re-running the old generation's exact command (a late duplicate start) must refuse.
    command = old_argv[old_argv.index("--") + 1 :]
    env = {}
    index = 2
    while "=" in command[index] and not command[index].startswith("/"):
        k, v = command[index].split("=", 1)
        env[k] = v
        index += 1
    cwd = next(a.split("=", 1)[1] for a in old_argv if a.startswith("--working-directory="))
    done = subprocess.run(command[index:], env=env, cwd=cwd, timeout=60)
    assert done.returncode == 3
    assert native_state.read_lifecycle(old_worker)["phase"] == "gone"
    assert len([f for f in frames(project) if f["frame"].get("method") == "initialize"]) == starts


@pytest.mark.anyio
async def test_private_socket_refuses_a_wrong_capability(host, project):
    from agent_sessions import native_ipc

    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    gen = native_runtime._generation(key.partition(":")[2])
    socket_path = Path(gen.config["socket"])
    assert (socket_path.stat().st_mode & 0o777) == 0o600
    assert (socket_path.parent.stat().st_mode & 0o777) == 0o700
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(native_ipc.encode_handshake(gen.binding, native_ipc.Capability.create()).data)
    await writer.drain()
    assert await reader.readline() == b""  # closed without a welcome
    writer.close()
    # The real capability never appears in the journal, events or snapshot.
    root = native_runtime._journal_root(native_runtime._api_provider(ENGINES["codex"]))
    raw = (root / f"{key.partition(':')[2]}.jsonl").read_text()
    assert gen.config["capability"] not in raw
    assert gen.config["capability"] not in json.dumps(await runtime.snapshot(key))


@pytest.mark.anyio
async def test_native_refuses_an_execution_guard_and_console_clients_stay_unsupported(
    host, project
):
    from agent_sessions.structured_types import ExecutionGuard

    async def acquire():  # pragma: no cover — never reached
        raise AssertionError

    guard = ExecutionGuard("mission-grant", acquire)
    with pytest.raises(runtime.StructuredError, match="caller authority"):
        await runtime.create_session(
            ENGINES["codex"], str(project), operation_id=ident(), execution_admission=guard
        )
    assert not host.launches
    assert runtime.describe("codex").operations == ()


def test_readiness_refuses_an_overridden_vendor_store(host, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(tmp_path / "elsewhere"))
    ready, reason = native_runtime.readiness(registry._BY_ID[ENGINES["codex"]])
    assert not ready and "overridden" in reason


def test_readiness_refuses_an_old_cli(host, tmp_path, monkeypatch):
    script = Path(os.environ["AGENT_SESSIONS_CLAUDE_BIN"])
    script.write_text(f"#!{sys.executable}\nprint('2.1.200 (Claude Code)')\n")
    ready, reason = native_runtime.readiness(registry._BY_ID[ENGINES["claude"]])
    assert not ready and "2.1.287 or later" in reason


def test_release_lease_is_taken_before_launch_and_installer_support_is_required(
    host, tmp_path, monkeypatch
):
    prefix = tmp_path / "install"
    release = prefix / "releases" / "20261006-000000-abc"
    (release / "venv" / "bin").mkdir(parents=True)
    (prefix / "current").symlink_to(release)
    monkeypatch.setattr(native_runtime, "interpreter", lambda: (sys.executable, release))
    ready, reason = native_runtime.readiness(registry._BY_ID[ENGINES["codex"]])
    assert not ready and "retain a release" in reason
    (release / "src").mkdir()
    (release / "src" / "install.sh").write_text("# honours native-leases\n")
    assert native_runtime.readiness(registry._BY_ID[ENGINES["codex"]])[0]
    worker = ident()
    native_runtime._lease(release, worker)
    assert (release / "native-leases" / worker).exists()
    native_runtime._lease(release, worker, remove=True)
    assert not (release / "native-leases" / worker).exists()


def test_journal_records_no_raw_native_frames(host, project):
    """Only normalized allowlisted events are durable; a private 'send' frame cannot be."""
    from agent_sessions import native_ipc

    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "send", "data": {"frame": {}}})
    assert native_journal.MAX_BYTES > 0


@pytest.mark.skipif(
    not os.path.exists(f"/run/user/{os.getuid()}/systemd/private")
    or not os.path.exists("/usr/bin/systemd-run")
    or os.environ.get("CI") == "true",
    reason="needs a real systemd user manager (not available in CI containers)",
)
@pytest.mark.anyio
async def test_real_systemd_contains_and_stops_a_worker(tmp_home, tmp_path, project, monkeypatch):
    """The production Host: a transient unit, a pinned invocation, cgroup-proved teardown."""
    from agent_sessions import resource_limits, resource_usage

    resource_limits.save({"api_tasks": 512, "library_threads": 3, "api_memory_gib": 2})
    for name in resource_limits.THREAD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(native_runtime, "HOST", native_runtime.Host())
    bindir = tmp_path / "engine-bin"
    bindir.mkdir()
    bindir.chmod(0o755)
    script = bindir / "codex"
    # Inert children exercise the real native child's inherited finite group/env.
    probe = """
import json, os, pathlib, subprocess, sys
if 'app-server' in sys.argv:
    relative = pathlib.Path('/proc/self/cgroup').read_text().strip().split(':', 2)[2]
    cg = pathlib.Path('/sys/fs/cgroup') / relative.lstrip('/')
    before = (cg / 'pids.events').read_text()
    command = [sys.executable, '-c', 'import time; time.sleep(0.25)']
    peers = [subprocess.Popen(command) for _ in range(8)]
    try:
        loaded = int((cg / 'pids.current').read_text())
        subprocess.run([sys.executable, '-c', 'pass'], check=True)
    finally:
        for peer in peers: peer.wait(timeout=5)
    evidence = {'before': before, 'after': (cg / 'pids.events').read_text(),
                'maximum': (cg / 'pids.max').read_text().strip(), 'loaded': loaded,
                'memory_maximum': (cg / 'memory.max').read_text().strip(),
                'threads': os.environ.get('RAYON_NUM_THREADS')}
    pathlib.Path('resource-probe.json').write_text(json.dumps(evidence))
"""
    script.write_text(f"#!{sys.executable}\n" + probe + "\n" + FAKE.read_text())
    script.chmod(0o755)
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_BIN", str(script))
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    providers = list(registry._PROVIDERS)
    doc = test_manifest_api._api(name="codex-api", source="codex", kind="codex-app-server")
    providers.append(test_manifest_api._provider(doc, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    key = (await runtime.create_session("codex-api", str(project), operation_id=ident()))[
        "session_key"
    ]
    try:
        session_id = key.partition(":")[2]
        worker = native_state.read_session(session_id)["current_worker"]
        life = native_state.read_lifecycle(worker)
        assert life["invocation_id"] and life["control_group"].endswith(".service")
        observed = resource_usage.observe(
            Path("/sys/fs/cgroup") / life["control_group"].lstrip("/")
        )
        assert observed["own"]["maximum"] == 512
        evidence = json.loads((project / "resource-probe.json").read_text())
        assert evidence["maximum"] == "512" and evidence["threads"] == "3"
        assert evidence["memory_maximum"] == str(2 * 1024**3)
        assert evidence["before"] == evidence["after"] and evidence["loaded"] >= 10
        resource_limits.save({"api_tasks": 2048, "api_memory_gib": 16})
        assert (
            int(
                (
                    Path("/sys/fs/cgroup") / life["control_group"].lstrip("/") / "memory.max"
                ).read_text()
            )
            == 2 * 1024**3
        )
        assert (
            resource_usage.observe(Path("/sys/fs/cgroup") / life["control_group"].lstrip("/"))[
                "own"
            ]["maximum"]
            == 512
        )
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text="hello")
        await settle(key, turn)
        assert (await runtime.probe(key))["containment"] == "live"
    finally:
        stopped = await runtime.stop(key)
    assert stopped["containment"] == "gone"


def _real_binary(name: str) -> str | None:
    import pwd

    home = pwd.getpwuid(os.getuid()).pw_dir
    for candidate in (
        shutil.which(name),
        os.path.join(home, ".npm-global", "bin", name),
        os.path.join(home, ".local", "bin", name),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate) if name == "claude" else candidate
    return None


@pytest.mark.skipif(
    os.environ.get("AGENT_SESSIONS_TEST_REAL_NATIVE") != "1",
    reason="opt-in: AGENT_SESSIONS_TEST_REAL_NATIVE=1 runs the installed vendor CLIs",
)
@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_real_cli_handshake_binds_and_stops_without_inference(
    tmp_home, tmp_path, project, monkeypatch, source
):
    """Installed CLI + real systemd, with an EMPTY home: no credentials, so no inference can run.

    Proves the reviewed argv, initialize handshake, (Codex) thread/start → bind, the contained
    unit and its proved teardown against the real vendor binary — not a scripted stand-in.
    """
    binary = _real_binary(source)
    if binary is None:
        pytest.skip(f"{source} is not installed")
    monkeypatch.setattr(native_runtime, "HOST", native_runtime.Host())
    monkeypatch.setenv(f"AGENT_SESSIONS_{source.upper()}_BIN", binary)
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    providers = list(registry._PROVIDERS)
    kind = "codex-app-server" if source == "codex" else "claude-stream-json"
    doc = test_manifest_api._api(name=ENGINES[source], source=source, kind=kind)
    providers.append(test_manifest_api._provider(doc, tmp_path))
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    ready, reason = native_runtime.readiness(registry._BY_ID[ENGINES[source]])
    assert ready, reason
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    try:
        snap = await runtime.snapshot(key)
        assert snap["native"]["native_id"]
        assert native_ownership.lookup(key).state == "bound"
        assert (await runtime.probe(key))["containment"] == "live"
    finally:
        stopped = await runtime.stop(key)
    assert stopped["containment"] == "gone"


def test_routes_require_login_and_csrf_and_drive_a_native_session(host, project, auth_cfg):
    import time

    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    with TestClient(create_app(auth_cfg), base_url="https://testserver") as c:
        engine = ENGINES["codex"]
        assert c.get(f"/api/structured/clients/{engine}").status_code == 401
        body = {"engine": engine, "cwd": str(project), "operation_id": ident()}
        assert c.post("/api/structured/sessions", json=body).status_code in (401, 403)
        r = c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        assert r.status_code in (302, 303)
        # Logged in but no CSRF token: every mutation still refuses.
        assert c.post("/api/structured/sessions", json=body).status_code == 403
        assert not host.launches
        csrf = c.get("/api/config").json()["csrf"]
        c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
        client = c.get(f"/api/structured/clients/{engine}").json()
        assert client["ready"] and client["authentication"] == "vendor_native"
        assert client["mission_ready"] is False
        for field, value in (("capability", "x"), ("fresh_create", True), ("native_id", ident())):
            bad = c.post("/api/structured/sessions", json={**body, field: value})
            assert bad.status_code == 422  # no internal binding selector or supplied native id
        created = c.post("/api/structured/sessions", json=body)
        assert created.status_code == 201, created.text
        key = created.json()["session_key"]
        turn = ident()
        sent = c.post(
            f"/api/structured/sessions/{key}/turns", json={"operation_id": turn, "text": "hi"}
        )
        assert sent.status_code in (200, 202), sent.text
        for _ in range(200):
            snap = c.get(f"/api/structured/sessions/{key}").json()
            if snap["turns"] and snap["turns"][-1]["state"] == "completed":
                break
            time.sleep(0.05)
        assert snap["turns"][-1]["reply"] == "echo:hi"
        assert client["images"] is True and snap["images"] is True  # #1332 Phase 3
        # A picture must be an upload: a path is refused before anything is recorded.
        refused = c.post(
            f"/api/structured/sessions/{key}/turns",
            json={"operation_id": ident(), "text": "x", "attachments": ["/etc/hosts"]},
        )
        assert refused.status_code == 422, refused.text
        events = c.get(f"/api/structured/sessions/{key}/events?after=0&limit=5").json()
        assert len(events["events"]) <= 5 and events["next_cursor"] >= 1
        assert c.get(f"/api/structured/sessions/{key}/containment").json() == {
            "session_key": key,
            "containment": "live",
        }
        # The private worker config holds no server secret (session key, password hash, …).
        live = snap["native"]["worker"]
        config_path = native_state.worker_dir(live) / "config.json"
        config = config_path.read_text()
        assert "x" * 64 not in config and "AGENT_SESSIONS_PASSWORD_HASH" not in config
        assert config_path.stat().st_mode & 0o077 == 0
        stopped = c.post(f"/api/structured/sessions/{key}/stop")
        assert stopped.json()["containment"] == "gone"
    # A proved-gone generation's capability does not linger on disk.
    assert not config_path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_an_idle_worker_exits_and_the_next_turn_resumes_the_same_history(
    host, project, monkeypatch, source
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="one")
    snap, _ = await settle(key, first)
    native, worker = snap["native"]["native_id"], snap["native"]["worker"]
    # Counted from HERE (#1337): on a loaded runner the creation's own worker can idle out
    # (0.5 s) before the first turn reaches it, and that turn then resumes in a fresh one —
    # correct behaviour, but it shifts every count taken from the start of the test.
    launched = len(host.launches)
    for _ in range(200):
        if native_state.read_lifecycle(worker).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    assert native_state.read_lifecycle(worker)["phase"] == "exited"
    assert native_state.read_lifecycle(worker)["reason"] == ""
    assert (await runtime.snapshot(key))["native"]["worker"] is None
    # …and the successor gets a long window, so it cannot idle out before its turn arrives either.
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 60.0)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    snap, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two" and snap["native"]["native_id"] == native
    assert snap["native"]["worker"] != worker and len(host.launches) == launched + 1


def test_lease_sweep_drops_only_gone_generations(host, tmp_path):
    release = tmp_path / "install" / "releases" / "r1"
    release.mkdir(parents=True)
    alive = native_containment.WorkerIdentity.mint()
    gone = native_containment.WorkerIdentity.mint()
    for worker in (alive, gone):
        native_runtime._lease(release, worker.worker_id)
        native_state.update_lifecycle(
            worker.worker_id,
            invocation_id=uuid.uuid4().hex,
            control_group=f"/user.slice/{worker.unit}",
        )
    unpinned = native_containment.WorkerIdentity.mint()  # being launched by another session
    native_runtime._lease(release, unpinned.worker_id)
    host.procs[alive.unit] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    host.invocations[alive.unit] = uuid.uuid4().hex
    try:
        host.invocations[alive.unit] = native_state.read_lifecycle(alive.worker_id)["invocation_id"]
        native_runtime._sweep_leases(release)
        remaining = {p.name for p in (release / "native-leases").iterdir()}
        assert remaining == {alive.worker_id, unpinned.worker_id}
    finally:
        host.close()


async def _codex(project):
    created = await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())
    return created["session_key"]


@pytest.mark.anyio
async def test_interrupt_succeeds_while_output_keeps_moving_the_revision(host, project):
    """Review of #1278: interrupt used a single revision read and 409'd while streaming."""
    key = await _codex(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="STREAM")
    await asyncio.sleep(0.5)  # deltas are being journaled
    out = await runtime.interrupt(key, operation_id=ident(), turn_id=turn)
    assert out["handoff"] == "sent"
    _, settled = await settle(key, turn, state=("interrupted",))
    assert settled["reply"].startswith(".")


@pytest.mark.anyio
async def test_a_refused_turn_fails_definitively_and_frees_the_generation(host, project):
    """Review of #1278: an errored turn/start left the codec "active" and wedged the worker."""
    key = await _codex(project)
    refused = ident()
    await runtime.submit_turn(key, operation_id=refused, text="REFUSE")
    _, turn = await settle(key, refused, state=("failed",))
    assert "refused" in turn["reason"]
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="after")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:after" and len(host.launches) == 1


@pytest.mark.anyio
async def test_an_oversized_native_frame_is_omitted_not_fatal(host, project):
    """Review of #1278: one >1 MiB tool output ended the generation and the turn."""
    key = await _codex(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="BIG")
    _, settled = await settle(key, turn)
    assert settled["state"] == "completed" and settled["reply"] == "echo:BIG"
    page = await runtime.events(key, after=0)
    assert any(
        e["kind"] == "error" and "size bound" in e["data"]["message"] for e in page["events"]
    )
    assert len(host.launches) == 1  # the same generation survived


@pytest.mark.anyio
async def test_codex_resume_never_asks_for_the_full_history(host, project, monkeypatch):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.3)
    key = await _codex(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="one")
    await settle(key, first)
    await asyncio.sleep(1.5)  # idle exit
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await settle(key, nxt)
    resumes = [f["frame"] for f in frames(project) if f["frame"].get("method") == "thread/resume"]
    assert resumes and all(r["params"]["excludeTurns"] is True for r in resumes)


@pytest.mark.anyio
async def test_no_successor_launches_until_every_earlier_generation_is_proved_gone(
    host, project, monkeypatch
):
    """Review of #1278: a second submit after an `unknown` generation launched a successor."""
    key = await _codex(project)
    snap = await runtime.snapshot(key)
    worker = snap["native"]["worker"]
    host.kill(worker)
    real_show = host.show
    monkeypatch.setattr(host, "show", lambda w: None)  # systemd cannot answer: `unknown`
    for _ in range(2):
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.submit_turn(key, operation_id=ident(), text="x")
        assert refused.value.status == 409 and "unknown" in refused.value.detail
    assert len(host.launches) == 1
    monkeypatch.setattr(host, "show", real_show)  # evidence returns: proved gone, resume
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="back")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "echo:back" and len(host.launches) == 2


@pytest.mark.anyio
async def test_a_bound_history_missing_from_the_record_is_recovered_from_ownership(host, project):
    """Review of #1278: bind committed, then the record write lost — never a dead session."""
    key = await _codex(project)
    session_id = key.partition(":")[2]
    native = (await runtime.snapshot(key))["native"]["native_id"]
    host.kill((await runtime.snapshot(key))["native"]["worker"])
    with native_state.session_lock(session_id):
        record = native_state.read_session(session_id)
        record["native_id"] = None
        native_state.write_session(session_id, record)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="again")
    snap, settled = await settle(key, turn)
    assert settled["reply"] == "echo:again" and snap["native"]["native_id"] == native


@pytest.mark.anyio
async def test_a_not_current_generation_is_refused_by_the_startup_gate(host, project):
    """Defence in depth: even with its config present, an old generation cannot start."""
    key = await _codex(project)
    session_id = key.partition(":")[2]
    old = native_state.read_session(session_id)["current_worker"]
    config = (native_state.worker_dir(old) / "config.json").read_text()
    old_argv = host.launches[0]
    host.kill(old)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="successor")
    await settle(key, turn)
    (native_state.worker_dir(old) / "config.json").write_text(config)
    (native_state.worker_dir(old) / "config.json").chmod(0o600)
    starts = len([f for f in frames(project) if f["frame"].get("method") == "initialize"])
    command = old_argv[old_argv.index("--") + 1 :]
    env, index = {}, 2
    while "=" in command[index] and not command[index].startswith("/"):
        k, v = command[index].split("=", 1)
        env[k] = v
        index += 1
    cwd = next(a.split("=", 1)[1] for a in old_argv if a.startswith("--working-directory="))
    assert subprocess.run(command[index:], env=env, cwd=cwd, timeout=60).returncode == 3
    assert native_state.read_lifecycle(old)["phase"] == "refused"
    assert len([f for f in frames(project) if f["frame"].get("method") == "initialize"]) == starts


@pytest.mark.anyio
async def test_another_clients_engine_cannot_reach_a_session_by_its_uuid(host, project):
    """Hermes on #1278: sessions were found by UUID alone, so client B's key could stop A."""
    key = await _codex(project)
    forged = f"{ENGINES['claude']}:{key.partition(':')[2]}"
    for call in (
        runtime.snapshot(forged),
        runtime.probe(forged),
        runtime.stop(forged),
        runtime.events(forged, after=0),
        runtime.submit_turn(forged, operation_id=ident(), text="x"),
    ):
        with pytest.raises(runtime.StructuredError) as refused:
            await call
        assert refused.value.status == 404
    assert (await runtime.probe(key))["containment"] == "live"


@pytest.mark.anyio
async def test_probe_never_certifies_gone_for_a_creation_not_yet_launched(host, project):
    session_id = ident()
    key = f"{ENGINES['codex']}:{session_id}"
    native_state.write_session(
        session_id,
        {
            "version": 1,
            "session_key": key,
            "adapter": "codex-app-server",
            "source_engine": "codex",
            "request": {"cwd": str(project), "model": None, "adapter": "codex-app-server"},
            "owner_token": ident(),
            "native_id": None,
            "current_worker": None,
            "closed": False,
            "workers": [],
        },
    )
    assert (await runtime.probe(key))["containment"] == "unknown"


@pytest.mark.anyio
async def test_an_unfinished_final_frame_is_discarded_not_completed(host, project):
    """Hermes on #1278: EOF synthesized a newline and completed a frame never finished."""
    key = await _codex(project)
    turn = ident()
    try:
        await runtime.submit_turn(key, operation_id=turn, text="PARTIAL")
    except runtime.StructuredError as exc:
        # The injected EOF can end the worker before it flushes the RPC acknowledgement.
        # Either acknowledgement outcome must leave the durable turn uncertain below;
        # never turn a transport race into a test of whether the partial frame completed.
        assert exc.status == 503 and "invalid frame" in exc.detail
    _, settled = await settle(key, turn, state=("uncertain",))
    assert settled["state"] == "uncertain"
    page = await runtime.events(key, after=0)
    assert any("mid-frame" in e["data"].get("message", "") for e in page["events"])
    assert not any(e["kind"] == "turn_completed" for e in page["events"])


@pytest.mark.anyio
async def test_a_turn_waits_for_a_dying_worker_then_resumes_in_a_successor(
    host, project, monkeypatch
):
    """pr-validate on #1309: right after a native crash the old worker refuses connections but
    is still exiting; three instant retries all hit it and the turn failed "unreachable"."""
    import time as clock

    key = await _codex(project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="hello")
    await settle(key, first)
    old = (await runtime.snapshot(key))["native"]["worker"]
    dying_until = clock.monotonic() + 0.8
    real_call, real_running = native_runtime._call, host.worker_running

    async def refusing(gen, action, params, **kw):
        if gen.worker_id == old and clock.monotonic() < dying_until:
            raise native_runtime._Unreachable("connection refused")
        return await real_call(gen, action, params, **kw)

    def running(worker, pid):
        if worker.worker_id == old and clock.monotonic() < dying_until:
            return True  # still shutting down
        return real_running(worker, pid)

    monkeypatch.setattr(native_runtime, "_call", refusing)
    monkeypatch.setattr(host, "worker_running", running)
    host.kill(old)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="after crash")
    _, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:after crash" and len(host.launches) == 2


@pytest.mark.anyio
async def test_a_sub_agent_permission_prompt_is_refused_not_attributed_to_the_turn(host, project):
    """Hermes on #1278: a background agent's callback was journaled against the human turn."""
    key = (await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident()))[
        "session_key"
    ]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="SUBAGENT")
    snap, settled = await settle(key, turn)
    assert settled["reply"] == "subagent:error" and not snap["pending_requests"]
    page = await runtime.events(key, after=0)
    assert not [e for e in page["events"] if e["kind"] == "approval"]


@pytest.mark.anyio
async def test_a_creation_stopped_before_it_launched_is_terminal(host, project):
    """Hermes on #1278: stop reported an unlaunched creation gone, then a replay of the same
    operation id reserved ownership and launched it anyway."""
    session_id = ident()
    key = f"{ENGINES['codex']}:{session_id}"
    native_state.write_session(
        session_id,
        {
            "version": 1,
            "session_key": key,
            "adapter": "codex-app-server",
            "source_engine": "codex",
            "request": {"cwd": str(project), "model": None, "adapter": "codex-app-server"},
            "owner_token": ident(),
            "native_id": None,
            "current_worker": None,
            "closed": False,
            "workers": [],
        },
    )
    assert (await runtime.stop(key))["containment"] == "gone"
    with pytest.raises(runtime.StructuredError, match="was stopped") as replay:
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=session_id)
    assert replay.value.status == 409 and not host.launches
    assert native_ownership.lookup(key) is None


@pytest.mark.anyio
async def test_a_retiring_clients_running_worker_can_still_be_probed_and_stopped(
    host, project, monkeypatch
):
    """Hermes on #1278: retirement made the structured stop path 404 for a live worker."""
    key = await _codex(project)
    retiring = registry._BY_ID[ENGINES["codex"]]
    remaining = [p for p in registry._PROVIDERS if p is not retiring]
    monkeypatch.setattr(registry, "_PROVIDERS", remaining)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in remaining})
    monkeypatch.setattr(registry, "_RETIRING", {retiring.engine_id: retiring})
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(key, operation_id=ident(), text="new work")
    assert (await runtime.probe(key))["containment"] == "live"
    assert (await runtime.stop(key))["containment"] == "gone"


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_complete_approval_is_decided_once_by_its_operator_then_stale(
    host, project, source
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="APPROVE")
    snap = await pending(key)
    request = snap["pending_requests"][0]
    assert request["complete"] and request["choices"] == ["approve", "reject", "cancel"]
    assert len(request["payload_digest"]) == 64 and snap["state"] == "awaiting_approval"
    if source == "codex":  # the whole request, not a command excerpt
        assert request["payload"]["networkApprovalContext"] == {"host": "example.invalid"}
        assert request["payload"]["proposedNetworkPolicyAmendments"]
    else:  # the whole permission prompt, not only its input
        assert request["payload"]["blocked_path"] == "/etc/hosts"
        assert request["payload"]["decision_reason"] == "outside the working directory"
        assert request["payload"]["input"] == {"command": "ls -la"}
    decision = ident()
    args = dict(turn_id=turn, request_id=request["request_id"], decision="approve")
    out = await runtime.decide(key, decision_id=decision, user="alice", **args)
    assert out["handoff"] == "sent"
    again = await runtime.decide(key, decision_id=decision, user="alice", **args)
    assert again["handoff"] == "sent"  # exact replay observes
    with pytest.raises(runtime.StructuredError) as other_actor:
        await runtime.decide(key, decision_id=decision, user="mallory", **args)
    assert other_actor.value.status == 409  # who decided is part of the decision
    _, settled = await settle(key, turn)
    assert settled["reply"] == ("approved:accept" if source == "codex" else "approved:allow")
    replies = [
        f
        for f in frames(project)
        if f["frame"].get("id") == 77 or f["frame"].get("type") == "control_response"
    ]
    assert len(replies) == 1
    if source == "codex":
        assert replies[0]["frame"]["result"] == {"decision": "accept"}  # never policy-wide
    with pytest.raises(runtime.StructuredError):
        await runtime.decide(key, decision_id=ident(), user="alice", **args)
    # A callback lost with its worker is never offered again from the journal.
    second = ident()
    await runtime.submit_turn(key, operation_id=second, text="APPROVE")
    request = (await pending(key))["pending_requests"][0]
    host.kill((await runtime.snapshot(key))["native"]["worker"])
    for _ in range(200):
        snap = await runtime.snapshot(key)
        if not snap["pending_requests"]:
            break
        await asyncio.sleep(0.05)
    assert not snap["pending_requests"]
    with pytest.raises(runtime.StructuredError):
        await runtime.decide(
            key,
            decision_id=ident(),
            turn_id=second,
            request_id=request["request_id"],
            decision="reject",
            user="alice",
        )


@pytest.mark.anyio
async def test_a_file_change_presents_its_patch_but_can_only_be_declined(host, project):
    """Hermes on #1278: nothing binds the applied patch to the shown one, so no approve."""
    key = await _codex(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="EDIT")
    request = (await pending(key))["pending_requests"][0]
    assert request["kind"] == "file_change" and not request["complete"]
    assert request["choices"] == ["reject", "cancel"]
    assert request["payload"]["changes"] == [
        {"path": "README.md", "kind": {"type": "update"}, "diff": "-old\n+new\n"}
    ]
    with pytest.raises(runtime.StructuredError, match="presented completely"):
        await runtime.decide(
            key,
            decision_id=ident(),
            turn_id=turn,
            request_id=request["request_id"],
            decision="approve",
            user="alice",
        )
    await runtime.decide(
        key,
        decision_id=ident(),
        turn_id=turn,
        request_id=request["request_id"],
        decision="reject",
        user="alice",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "edited:decline"


@pytest.mark.anyio
async def test_a_file_change_without_its_patch_can_only_be_declined(host, project):
    key = await _codex(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="EDIT_NOPATCH")
    request = (await pending(key))["pending_requests"][0]
    assert not request["complete"] and request["choices"] == ["reject", "cancel"]
    with pytest.raises(runtime.StructuredError, match="presented completely") as refused:
        await runtime.decide(
            key,
            decision_id=ident(),
            turn_id=turn,
            request_id=request["request_id"],
            decision="approve",
            user="alice",
        )
    assert refused.value.status == 409
    assert not [f for f in frames(project) if f["frame"].get("id") == 88]
    gen = native_runtime._generation(key.partition(":")[2])
    journal = native_runtime._journal(
        native_runtime._api_provider(ENGINES["codex"]), key.partition(":")[2]
    )
    # The worker refuses it too, even to a caller that bypasses the web gate.
    with pytest.raises(native_runtime.NativeError, match="presented completely"):
        await native_runtime._call(
            gen,
            "decide",
            {
                "operation_id": ident(),
                "expected_revision": journal.revision,
                "turn_id": turn,
                "request_id": request["request_id"],
                "item_id": request["item_id"],
                "payload_digest": request["payload_digest"],
                "decision": "approve",
                "approval_worker_id": gen.worker_id,
                "approval_connection_id": gen.config["connection_id"],
                "actor": "alice",
            },
        )
    await runtime.decide(
        key,
        decision_id=ident(),
        turn_id=turn,
        request_id=request["request_id"],
        decision="reject",
        user="alice",
    )
    _, settled = await settle(key, turn)
    assert settled["reply"] == "edited:decline"


@pytest.mark.anyio
async def test_a_patch_that_changes_after_presentation_withdraws_the_approval(host, project):
    """What was shown must be what is approved: a later patch declines the stale callback."""
    key = await _codex(project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="EDIT_CHANGING")
    _, settled = await settle(key, turn)
    assert settled["reply"] == "edited:decline"
    assert not (await runtime.snapshot(key))["pending_requests"]


_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_turn_carries_pictures_to_the_agent_but_never_into_the_journal(
    host, project, source, tmp_path
):
    """#1332 Phase 3: the picture reaches the agent inline; the record keeps its name only."""
    import base64

    uploads = tmp_path / ".agent-sessions" / "uploads"
    uploads.mkdir(parents=True, mode=0o700, exist_ok=True)
    stored = "20261008-010000-shot.png"
    (uploads / stored).write_bytes(_PNG)
    engine = ENGINES[source]
    assert runtime.describe(engine).images
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="look", attachments=[stored])
    snap, settled = await settle(key, turn)
    assert settled["reply"].startswith("echo:IMAGES:1:") and settled["reply"].endswith(":look")
    assert "image/png" in settled["reply"]
    assert settled["attachments"] == [{"stored": stored, "mime": "image/png"}]
    # The same id naming a different picture is a different request.
    (uploads / "20261008-010001-other.png").write_bytes(_PNG + b"!")
    with pytest.raises(runtime.StructuredError) as changed:
        await runtime.submit_turn(
            key, operation_id=turn, text="look", attachments=["20261008-010001-other.png"]
        )
    assert changed.value.status == 409
    # Bytes went to the agent (its own frame log) and nowhere BattleLab keeps.
    encoded = base64.b64encode(_PNG).decode("ascii").encode()
    for path in tmp_path.rglob("*"):
        if path.is_file() and project not in path.parents and uploads not in path.parents:
            assert encoded not in path.read_bytes(), path
    # A picture-only turn needs no words.
    only = ident()
    await runtime.submit_turn(key, operation_id=only, text="", attachments=[stored])
    _, settled = await settle(key, only)
    assert settled["reply"].startswith("echo:IMAGES:1:")


@pytest.mark.anyio
async def test_a_client_that_takes_no_pictures_refuses_them_before_anything_is_sent(
    host, project, tmp_path, monkeypatch
):
    from agent_sessions.plugins import kinds

    uploads = tmp_path / ".agent-sessions" / "uploads"
    uploads.mkdir(parents=True, mode=0o700, exist_ok=True)
    (uploads / "20261008-010000-shot.png").write_bytes(_PNG)
    engine = ENGINES["codex"]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    monkeypatch.setitem(kinds.API_IMAGE_INPUT, "codex-app-server", False)
    assert not runtime.describe(engine).images
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(
            key, operation_id=ident(), text="look", attachments=["20261008-010000-shot.png"]
        )
    assert refused.value.status == 422
    with pytest.raises(runtime.StructuredError) as bogus:
        await runtime.submit_turn(key, operation_id=ident(), text="x", attachments=["../x.png"])
    assert bogus.value.status == 422
    assert not [f for f in frames(project) if f["frame"].get("method") == "turn/start"]


@pytest.mark.anyio
async def test_resource_policy_is_frozen_per_generation(host, project, monkeypatch):
    from agent_sessions import resource_limits

    resource_limits.save({"api_tasks": 1024, "library_threads": 3, "api_memory_gib": 32})
    for name in resource_limits.THREAD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    created = await runtime.create_session("codex-api", str(project), operation_id=ident())
    assert "--property=TasksMax=1024" in host.launches[-1]
    assert "--property=MemoryMax=32G" in host.launches[-1]
    key = created["session_key"]
    record = native_state.read_session(key.split(":", 1)[1])
    worker = record["current_worker"]
    path = native_state.root() / "workers" / worker / "config.json"
    config = json.loads(path.read_text())
    assert config["thread_environment"]["RAYON_NUM_THREADS"] == "3"
    assert config["thread_environment"]["OPENBLAS_NUM_THREADS"] == "1"
    resource_limits.save({"api_tasks": 2048, "library_threads": 12, "api_memory_gib": 16})
    await runtime.probe(key)
    assert len(host.launches) == 1
    assert json.loads(path.read_text()) == config
    assert native_state.read_session(key.split(":", 1)[1])["current_worker"] == worker
    host.kill(worker)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)
    assert len(host.launches) == 2
    assert "--property=MemoryMax=32G" in host.launches[0]
    assert "--property=MemoryMax=16G" in host.launches[1]
