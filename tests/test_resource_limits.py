"""Saved launch budgets, legacy precedence and bounded unknown-aware diagnostics (#1362)."""

import json
import os

import pytest

from agent_sessions import (
    native_containment,
    native_worker,
    prefs,
    resource_limits,
    resource_usage,
    scopedspawn,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.delenv("AGENT_SESSIONS_SCOPE_PROPERTIES", raising=False)
    for key in resource_limits.THREAD_VARIABLES:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("value", [True, False, "4096", 0, -1, 255, 16385, None, 4096.0])
def test_bad_settings_are_atomic(value):
    resource_limits.save({"library_threads": 4})
    with pytest.raises(ValueError):
        resource_limits.save({"library_threads": 12, "console_tasks": value})
    assert resource_limits.values() == {
        "console_tasks": 4096,
        "api_tasks": 4096,
        "library_threads": 4,
    }


@pytest.mark.parametrize(
    "patch", [{}, [], {"TasksMax": 4096}, {"library_threads": 65}, {"library_threads": 0}]
)
def test_closed_schema(patch):
    with pytest.raises(ValueError):
        resource_limits.save(patch)


@pytest.mark.parametrize("legacy", ["512", "50%", "0.5%", "infinity", "18446744073709551614"])
def test_legacy_until_explicit_console_save(monkeypatch, legacy):
    monkeypatch.setenv("AGENT_SESSIONS_SCOPE_PROPERTIES", f"MemoryHigh=4G TasksMax={legacy}")
    resource_limits.save({"api_tasks": 1024})
    assert resource_limits.settings()["console_tasks_max"] == legacy
    assert resource_limits.settings()["sources"]["console_tasks"] == "environment"
    assert f"TasksMax={legacy}" in scopedspawn._properties()
    resource_limits.save({"console_tasks": 3072})
    assert scopedspawn._properties() == ["-p", "MemoryHigh=4G", "-p", "TasksMax=3072"]
    assert resource_limits.settings()["sources"]["console_tasks"] == "settings"


@pytest.mark.parametrize(
    "legacy",
    [
        "",
        "TasksMax=0",
        "TasksMax=-1",
        "TasksMax=0%",
        "TasksMax=101%",
        "TasksMax=bad",
        "MemoryHigh=4G bad;command",
    ],
)
def test_invalid_or_absent_legacy_keeps_finite_budget(monkeypatch, legacy):
    monkeypatch.setenv("AGENT_SESSIONS_SCOPE_PROPERTIES", legacy)
    assert "TasksMax=4096" in scopedspawn._properties()


def test_corrupt_block_uses_defaults_and_reports_notice():
    prefs.mutate_block("resource_limits", lambda _: {"api_tasks": True, "console_tasks": 999999})
    assert resource_limits.values() == resource_limits.DEFAULTS
    assert resource_limits.settings()["notice"]


def test_console_and_native_argv_receive_saved_nondefault_policy(monkeypatch):
    resource_limits.save({"console_tasks": 3072, "api_tasks": 1024, "library_threads": 4})
    monkeypatch.setattr(scopedspawn, "available", lambda: True)
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    command, _ = scopedspawn.wrap(["/bin/true"], engine="fixture", session_id="test")
    assert "TasksMax=3072" in command
    native = native_containment.launch_argv(
        native_containment.WorkerIdentity.mint(),
        python="/bin/python",
        home="/tmp",
        state_dir="/tmp",
        tasks_max=resource_limits.values()["api_tasks"],
    )
    assert "--property=TasksMax=1024" in native
    assert "--property=KillMode=control-group" in native
    for invalid in [0, "1024", True, 16385]:
        with pytest.raises(native_containment.ContainmentError):
            native_containment.launch_argv(
                native_containment.WorkerIdentity.mint(),
                python="/bin/python",
                home="/tmp",
                state_dir="/tmp",
                tasks_max=invalid,
            )


def test_threads_defaults_explicit_overrides_and_native_sanitization(monkeypatch):
    resource_limits.save({"library_threads": 4})
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    monkeypatch.setenv("OMP_NUM_THREADS", "2,3")
    threads = resource_limits.thread_environment(os.environ)
    assert threads["OPENBLAS_NUM_THREADS"] == "1" and threads["RAYON_NUM_THREADS"] == "4"
    assert threads["OMP_NUM_THREADS"] == "2,3"
    config = {"home": "/tmp", "path": "/bin", "thread_environment": threads}
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "never-copy")
    env = native_worker.native_environment(config)
    assert env["RAYON_NUM_THREADS"] == "4" and "AGENT_SESSIONS_SECRET_KEY" not in env
    resource_limits.save({"library_threads": 12})
    assert native_worker.native_environment(config) == env  # live generation is frozen
    with pytest.raises(native_worker.WorkerError):
        native_worker.native_environment(config | {"thread_environment": {"LD_PRELOAD": "/bad"}})


def _group(path, current="10", maximum="1000", events="max 7\n"):
    path.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "pids.current": current,
        "pids.max": maximum,
        "pids.events": events,
    }.items():
        (path / name).write_text(value)
    return path


def test_parent_pressure_and_missing_counter_never_look_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_usage, "CGROUP_ROOT", tmp_path)
    parent = _group(tmp_path / "parent", "980", "1000")
    leaf = _group(parent / "as-fixture.scope", "100", "4096")
    observed = resource_usage.observe(leaf)
    assert observed["severity"] == "critical" and observed["headroom"] == 20
    assert observed["own"]["denied"] == 7
    (parent / "pids.current").unlink()
    observed = resource_usage.observe(leaf)
    assert observed["severity"] == "unknown" and observed["headroom"] is None
    assert observed["incomplete"] and observed["ancestors"][0]["current"] is None
    (leaf / "pids.current").write_text("4090")
    assert resource_usage.observe(leaf)["severity"] == "critical"  # known danger wins


def test_bounded_discovery_only_known_local_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_usage, "CGROUP_ROOT", tmp_path)
    root = (
        tmp_path
        / "user.slice"
        / f"user-{os.getuid()}.slice"
        / f"user@{os.getuid()}.service"
        / "app.slice"
    )
    _group(root / "as-fixture.scope")
    _group(root / "unrelated.service")
    (root / "as-linked.scope").symlink_to(tmp_path, target_is_directory=True)
    result = resource_usage.collect()
    assert [r["unit"] for r in result["groups"]] == ["as-fixture.scope"]
    assert result["groups"][0]["incomplete"]  # parent files missing
    monkeypatch.setattr(resource_usage, "MAX_ENTRIES", 0)
    assert resource_usage.collect()["truncated"]


def test_unavailable_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_usage, "CGROUP_ROOT", tmp_path)
    monkeypatch.setenv("AGENT_SESSIONS_SESSION_SCOPES", "0")
    result = resource_usage.collect()
    assert result["error"] and result["console_containment"] == "disabled"


def test_resource_routes_auth_validation_and_persistence(auth_cfg, monkeypatch):
    from test_system import _client, _login

    monkeypatch.setattr(resource_usage, "collect", lambda: {"groups": [], "error": "unavailable"})
    c = _client(auth_cfg)
    assert c.get("/api/system/resources").status_code == 401
    _login(c, auth_cfg)
    assert c.post("/api/system/resources", json={"console_tasks": 3072}).status_code == 403
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": c.get("/api/config").json()["csrf"]}
    assert (
        c.post(
            "/api/system/resources",
            headers=headers | {"Origin": "https://elsewhere.invalid"},
            json={"console_tasks": 3072},
        ).status_code
        == 403
    )
    assert (
        c.post("/api/system/resources", headers=headers, json={"console_tasks": 3072}).status_code
        == 200
    )
    assert c.get("/api/system/resources").json()["settings"]["values"]["console_tasks"] == 3072
    assert c.post("/api/system/resources", headers=headers, content="x" * 4097).status_code == 413
    assert c.post("/api/system/resources", headers=headers, content="not json").status_code == 400
    assert (
        c.post(
            "/api/system/resources", headers=headers, content=json.dumps({"api_tasks": True})
        ).status_code
        == 422
    )
    assert resource_limits.values()["console_tasks"] == 3072


@pytest.mark.parametrize("launch", [False, True])
def test_console_spawn_env_applies_policy_only_on_launch(monkeypatch, launch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from agent_sessions import webterm
    from agent_sessions.plugins import admission

    resource_limits.save({"library_threads": 3})
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "1")
    observed = []

    async def spawn(*args, **kwargs):
        observed.append(kwargs["env"])
        raise OSError("stop at spawn boundary")

    async def admitted(create, *args, **kwargs):
        return await create()

    class Socket:
        async def close(self, **kwargs):
            pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(admission, "spawn", admitted)
    if not launch:
        monkeypatch.setattr(
            resource_limits,
            "thread_environment",
            lambda *a: pytest.fail("attach must not consult launch policy"),
        )
    asyncio.run(
        webterm.run(
            Socket(),
            ["/bin/true"],
            cwd=str(tmp_path),
            buf_key="fixture:test",
            launch_provider=SimpleNamespace(engine_id="fixture") if launch else None,
        )
    )
    assert len(observed) == 1 and observed[0]["OPENBLAS_NUM_THREADS"] == "1"
    assert observed[0].get("RAYON_NUM_THREADS") == ("3" if launch else None)


@pytest.mark.skipif(
    not os.path.exists(f"/run/user/{os.getuid()}/systemd/private")
    or os.environ.get("CI") == "true",
    reason="requires a real user manager",
)
def test_real_console_loaded_budget(monkeypatch):
    import subprocess
    import sys

    resource_limits.save({"console_tasks": 512, "library_threads": 3})
    monkeypatch.setenv("AGENT_SESSIONS_SESSION_SCOPES", "1")
    probe = """
import json, os, pathlib, subprocess, sys
relative = pathlib.Path('/proc/self/cgroup').read_text().strip().split(':', 2)[2]
group = pathlib.Path('/sys/fs/cgroup') / relative.lstrip('/')
before = (group / 'pids.events').read_text()
command = [sys.executable, '-c', 'import time; time.sleep(0.25)']
children = [subprocess.Popen(command) for _ in range(8)]
try:
    current = int((group / 'pids.current').read_text())
    subprocess.run([sys.executable, '-c', 'pass'], check=True)
finally:
    for child in children: child.wait(timeout=5)
print(json.dumps({'before': before, 'after': (group / 'pids.events').read_text(),
                  'maximum': (group / 'pids.max').read_text().strip(), 'loaded': current,
                  'threads': os.environ.get('RAYON_NUM_THREADS')}))
"""
    command, unit = scopedspawn.wrap(
        [sys.executable, "-c", probe], engine="fixture", session_id="resources"
    )
    assert unit is not None
    result = subprocess.run(
        command,
        env=dict(os.environ) | resource_limits.thread_environment(os.environ),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    evidence = json.loads(result.stdout)
    assert evidence["maximum"] == "512" and evidence["threads"] == "3"
    assert evidence["before"] == evidence["after"] and evidence["loaded"] >= 9
