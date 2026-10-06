"""Temporary vendor PTYs own their lifecycle and never enter session capture (#1259)."""

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions.plugins import admission, process


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def vendor(tmp_path, monkeypatch):
    binary = tmp_path / "fixture"
    binary.write_text(f"#!{sys.executable}\nimport sys\nprint('fixture 1.0', flush=True)\n")
    binary.chmod(0o700)
    prov = SimpleNamespace(
        engine_id="fixture",
        manifest=SimpleNamespace(
            runtime="pty",
            binary=SimpleNamespace(version_flag="--version"),
            launch=None,
            signin_kind="cli-subcommand",
            signin_subcommand="login",
            probe_kind="terminal",
        ),
        entrypoint_path=lambda: str(binary),
    )
    # These tests exercise the real transient-service lifecycle with a synthetic executable.
    # Durable candidate validation has separate admission tests; keep the real launch lock.
    monkeypatch.setattr(admission, "_candidate_provider", lambda *args: None)
    monkeypatch.setattr("agent_sessions.native_ownership.check_console", lambda *args: None)
    return prov, binary


@pytest.fixture
def user_manager():
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "is-system-running"], capture_output=True, timeout=5
    )
    if result.stdout.strip() not in (b"running", b"degraded"):
        pytest.skip("this host has no systemd user manager; fixed argv/refusal tests still run")


def test_service_argv_has_host_lifetime_cgroup_cleanup_and_no_app_environment(
    vendor, tmp_path, monkeypatch
):
    prov, binary = vendor
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "secret-must-not-enter-vendor-env")
    argv = process._wrapped(
        process._argv(prov, "signin", tmp_path, None),
        tmp_path,
        "battlelab-plugin-fixture.service",
        600,
    )
    assert "--pty" in argv and "--scope" not in argv
    assert "--property=RuntimeMaxSec=600" in argv
    assert "--property=KillMode=control-group" in argv
    assert "--expand-environment=no" in argv
    assert argv[-2:] == [str(binary), "login"]
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["/usr/bin/env", "-i"]
    assert all("secret-must" not in arg and "AGENT_SESSIONS_" not in arg for arg in argv)


def test_verification_cannot_request_permission_bypass(vendor, tmp_path):
    prov, _ = vendor
    prov.manifest.can = lambda _: True
    called = []

    def launch(native, *, cwd, bypass):
        called.append((native, cwd, bypass))
        return ["fixture", native]

    prov.new_launch_argv = prov.launch_argv = launch
    for purpose in ("new", "resume"):
        process._argv(prov, purpose, tmp_path, "native-id")
    assert called == [("native-id", str(tmp_path), False)] * 2


@pytest.mark.anyio
async def test_real_service_provides_a_controlling_tty_and_never_persists_signin_bytes(
    vendor, tmp_path, monkeypatch, caplog, user_manager
):
    prov, binary = vendor
    marker = b"secret-test-password-853"
    binary.write_text(
        f"#!{sys.executable}\nimport os,sys\n"
        "tty=open('/dev/tty','rb',buffering=0)\n"
        "print('READY',flush=True)\n"
        "data=tty.readline()\nprint('GOT:'+data.decode().strip(),flush=True)\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    received = b""
    operation_id = str(uuid.uuid4())
    async with process.spawn(prov, "signin", cwd=tmp_path, operation_id=operation_id) as terminal:
        while b"READY" not in received:
            chunk = await terminal.read()
            assert chunk, received
            received += chunk
        await terminal.write(marker + b"\n")
        while chunk := await terminal.read():
            received += chunk
        assert await terminal.proc.wait() == 0
    assert marker in received and b"GOT:" in received
    assert marker.decode() not in caplog.text
    journal = subprocess.run(
        ["journalctl", "--user", "--no-pager", "-u", process.unit_name(operation_id)],
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout
    assert marker not in journal
    assert (
        str(binary).encode() not in journal
    ), "systemd must log a fixed description, not vendor argv"
    # Process admission creates only its private lock, never sign-in capture or history.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_plugin-state", "fixture"]
    assert [p.name for p in (tmp_path / "_plugin-state").iterdir()] == ["launch.lock"]


@pytest.mark.anyio
async def test_real_service_reaps_a_child_that_detaches_into_another_session(
    vendor, tmp_path, user_manager
):
    prov, binary = vendor
    pidfile = tmp_path / "child-pid"
    binary.write_text(
        f"#!{sys.executable}\nimport os,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n os.setsid()\n"
        f" open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
        " time.sleep(60)\n"
        "else:\n time.sleep(.1)\n print('done',flush=True)\n"
    )
    async with process.spawn(prov, "version", cwd=tmp_path) as terminal:
        while await terminal.read():
            pass
        await terminal.proc.wait()
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            state = Path(f"/proc/{child}/stat").read_text().split(") ", 1)[1]
        except FileNotFoundError:
            break
        if state.startswith("Z"):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("a detached child escaped the temporary service lifecycle")


@pytest.mark.anyio
async def test_timeout_and_output_excess_stop_the_service(
    vendor, tmp_path, monkeypatch, user_manager
):
    prov, binary = vendor
    monkeypatch.setattr(process, "PROBE_SECONDS", 1)
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        async with process.spawn(prov, "version", cwd=tmp_path) as terminal:
            await terminal.read()
    assert time.monotonic() - start < 8
    monkeypatch.setattr(process, "MAX_OUTPUT", 100)
    binary.write_text(f"#!{sys.executable}\nprint('x'*1000,flush=True)\n")
    with pytest.raises(process.ProcessError, match="output limit"):
        async with process.spawn(prov, "version", cwd=tmp_path) as terminal:
            while await terminal.read():
                pass


@pytest.mark.anyio
async def test_maintenance_refusal_precedes_any_subprocess(vendor, tmp_path, monkeypatch):
    prov, _ = vendor
    prov.manifest.launch = SimpleNamespace(admission="sqlite-store-shared")
    monkeypatch.setattr(process.opencode_admission, "acquire", lambda *a, **kw: None)
    monkeypatch.setattr(process, "_stop", lambda *_: True)
    monkeypatch.setattr(
        process.asyncio,
        "create_subprocess_exec",
        lambda *a, **kw: pytest.fail("spawned through a maintenance refusal"),
    )
    with pytest.raises(process.ProcessError, match="maintenance"):
        async with process.spawn(prov, "version", cwd=tmp_path):
            pytest.fail("admitted a maintenance-refused probe")


@pytest.mark.anyio
async def test_cancel_while_waiting_for_maintenance_drains_then_releases_without_spawn(
    vendor, tmp_path, monkeypatch
):
    import threading

    prov, _ = vendor
    prov.manifest.launch = SimpleNamespace(admission="sqlite-store-shared")
    entered, finish = threading.Event(), threading.Event()
    released = []

    def acquire(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return SimpleNamespace(release=lambda: released.append(True))

    monkeypatch.setattr(process.opencode_admission, "acquire", acquire)

    async def forbidden(*args, **kwargs):
        pytest.fail("a cancelled admission spawned a process")

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", forbidden)

    async def run():
        async with process.spawn(prov, "signin", cwd=tmp_path):
            pytest.fail("a cancelled admission reached sign-in")

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released == [True]


@pytest.mark.anyio
async def test_a_refused_candidate_admission_never_spawns(vendor, tmp_path, monkeypatch):
    """Hermes on #1277: the spawn site must honour a refused guard itself."""
    from agent_sessions.plugins import admission

    prov, _ = vendor
    released = []

    class Refused(admission.Guard):
        def __init__(self):
            super().__init__()
            self.reason = "this native history belongs to an API session"

        def release(self):
            released.append(True)

    async def refusing(*args, **kwargs):
        return Refused()

    monkeypatch.setattr(admission, "acquire_candidate_async", refusing)
    monkeypatch.setattr(process, "_stop", lambda *_: True)
    monkeypatch.setattr(
        process.asyncio,
        "create_subprocess_exec",
        lambda *a, **kw: pytest.fail("spawned after admission refusal"),
    )
    with pytest.raises(admission.Refused, match="API session"):
        async with process.spawn(prov, "version", cwd=tmp_path):
            pytest.fail("admitted a refused candidate")
    assert released
