"""A reader paused across publication keeps one complete provider/consumer generation."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import test_roster_conformance
from agent_sessions import agent_usage, engines
from agent_sessions.engines import registry
from test_roster_conformance import ZETA

fixture_roster = test_roster_conformance.fixture_roster


def test_reader_paused_across_reload_keeps_provider_and_usage_generation(fixture_roster):
    before = registry.get(ZETA)
    assert before is not None
    with registry.snapshot_scope() as generation:
        assert ZETA in generation.views["usage"]["engines"]
        (fixture_roster / ZETA / "plugin.toml").unlink()
        with ThreadPoolExecutor() as pool:
            pool.submit(registry.reload, fixture_roster).result(timeout=10)
        assert registry.get(ZETA) is before
        assert ZETA in registry.engine_ids()
        assert ZETA in registry.current().views["usage"]["engines"]
        assert not registry.admits(before), "new work must check current admission"
        assert registry.current() is generation
    with registry.snapshot_scope(fresh=True) as after:
        assert after.generation > generation.generation
        assert registry.get(ZETA) is None
        assert ZETA not in after.views["usage"]["engines"]


def test_publication_waits_for_consumer_views_without_blocking_pinned_reader(
    fixture_roster, monkeypatch
):
    building, publish = threading.Event(), threading.Event()

    def pause():
        building.set()
        assert publish.wait(10)

    monkeypatch.setattr(registry, "_RELOAD_LISTENERS", [*registry._RELOAD_LISTENERS, pause])
    with registry.snapshot_scope() as old, ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(registry.reload, fixture_roster)
        assert building.wait(10)
        next_reader = pool.submit(registry.capture)
        try:
            assert registry.current() is old
            assert ZETA in registry.engine_ids()
            assert not next_reader.done(), "an unpinned reader saw partial publication"
        finally:
            publish.set()
        future.result(timeout=10)
        new = next_reader.result(timeout=10)
        assert new.generation > old.generation
        assert tuple(new.views["usage"]["engines"]) == agent_usage.ENGINES


def test_failed_roster_build_preserves_previous_generation(fixture_roster, monkeypatch):
    before = registry.capture()
    with monkeypatch.context() as patch:
        patch.setattr(
            registry, "_build_roster", lambda *_: (_ for _ in ()).throw(OSError("read failed"))
        )
        with pytest.raises(OSError):
            registry.reload(fixture_roster)
    after = registry.capture()
    assert after.generation == before.generation
    assert after.providers == before.providers and after.views == before.views


def test_captured_maps_cannot_be_modified(fixture_roster):
    frozen = registry.capture()
    with pytest.raises(TypeError):
        frozen.by_id[ZETA] = None
    with pytest.raises(TypeError):
        frozen.views["usage"]["reporters"][ZETA] = None


def test_http_request_and_worker_share_one_roster_generation(fixture_roster):
    import asyncio

    from fastapi.testclient import TestClient

    from agent_sessions.auth import AuthConfig
    from agent_sessions.main import create_app

    # A diagnostic test route has no external side effects and avoids invoking the real lifespan.
    app = create_app(
        AuthConfig(
            username="tester",
            password_hash="unused",
            secret_key="test",
            origin="https://testserver",
        )
    )

    @app.get("/snapshot-test")
    async def probe():
        first = registry.current()
        (fixture_roster / ZETA / "plugin.toml").unlink()
        await asyncio.to_thread(registry.reload, fixture_roster)
        second = await asyncio.to_thread(registry.current)
        return {"same": first is second, "still_present": engines.get(ZETA) is not None}

    # Put the diagnostic before the app's SPA catch-all; leave lifespan workers unstarted.
    app.router.routes.insert(0, app.router.routes.pop())
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/snapshot-test").json() == {"same": True, "still_present": True}
    assert registry.get(ZETA) is None


def test_failed_consumer_rolls_back_provider_and_usage_views(fixture_roster, monkeypatch):
    before = registry.capture()
    (fixture_roster / ZETA / "plugin.toml").unlink()

    def fail_after_usage_update():
        assert ZETA not in agent_usage.ENGINES
        raise RuntimeError("later consumer failed")

    with monkeypatch.context() as patch:
        patch.setattr(
            registry, "_RELOAD_LISTENERS", [*registry._RELOAD_LISTENERS, fail_after_usage_update]
        )
        with pytest.raises(RuntimeError):
            registry.reload(fixture_roster)
    after = registry.capture()
    assert after.generation == before.generation
    assert after.providers == before.providers and after.views == before.views
    assert ZETA in agent_usage.ENGINES


def test_background_usage_sweep_pins_one_generation(fixture_roster, tmp_path, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    observed = []

    def reporter():
        observed.append(registry.current())
        entered.set()
        assert finish.wait(10)
        assert registry.current() is observed[0]
        assert engines.get(ZETA) is not None
        return agent_usage.Report(engine=ZETA, source=agent_usage.SOURCE_PLAN, at=1)

    monkeypatch.setitem(agent_usage.REPORTERS, ZETA, reporter)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            agent_usage.refresh, path=tmp_path / "usage.json", engines=[ZETA], budgets={}
        )
        assert entered.wait(10)
        try:
            (fixture_roster / ZETA / "plugin.toml").unlink()
            registry.reload(fixture_roster)
            assert engines.get(ZETA) is None
        finally:
            finish.set()
        future.result(timeout=10)


def test_committed_disable_reaches_another_worker_without_caller_reload():
    import os
    import subprocess
    import sys

    registry.reload()
    before = registry.get("claude")
    assert before is not None
    with registry.snapshot_scope(fresh=True) as captured:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import uuid; from agent_sessions.plugins import manager; "
                "manager.deactivate(str(uuid.uuid4()), 'claude')",
            ],
            env=dict(os.environ),
            capture_output=True,
            timeout=20,
            check=True,
        )
        assert registry.get("claude") is before
        assert not registry.admits(before)
        assert registry.current() is captured
    with registry.snapshot_scope(fresh=True):
        assert registry.get("claude") is None


def test_failed_committed_reload_cannot_admit_previous_roster(monkeypatch):
    import uuid

    from agent_sessions.plugins import manager

    registry.reload()
    before = registry.get("claude")
    reload = registry.reload
    attempts = []

    def fail():
        attempts.append(True)
        raise OSError("consumer unavailable")

    rid = str(uuid.uuid4())
    monkeypatch.setattr(registry, "reload", fail)
    with pytest.raises(OSError, match="consumer unavailable"):
        manager.deactivate(rid, "claude")
    with pytest.raises(OSError, match="consumer unavailable"):
        registry.admits(before)
    assert len(attempts) == 2
    assert registry.capture().by_id["claude"] is before
    monkeypatch.setattr(registry, "reload", reload)
    assert manager.deactivate(rid, "claude")["state"] == "complete"
    assert not registry.admits(before)


@pytest.mark.parametrize("damage", ["json", "directory", "file"])
@pytest.mark.parametrize("with_revision", [False, True])
def test_corrupt_manager_keeps_diagnostics_available_and_new_work_closed(
    auth_cfg, with_revision, damage, monkeypatch
):
    import json
    import uuid

    from fastapi.testclient import TestClient

    from agent_sessions import auth
    from agent_sessions.main import create_app
    from agent_sessions.plugins import admission, manager, storage

    registry.reload()
    if with_revision:
        manager.deactivate(str(uuid.uuid4()), "codex")
    path = storage.root() / manager.DOCUMENT
    original = path.read_bytes() if path.exists() else json.dumps(manager.snapshot()).encode()
    before = registry.get("claude")
    assert before is not None
    cookie = auth._serializer(auth_cfg).dumps({"uid": auth_cfg.username, "csrf": "fixture"})
    headers = {"cookie": "agent_sessions=" + cookie}
    client = TestClient(
        create_app(auth_cfg), base_url=auth_cfg.origin, raise_server_exceptions=False
    )
    assert client.get("/healthz").status_code == 200
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(registry, "_engines_with_masters", lambda _candidates=(): {"claude"})
    damaged = b"{broken-private-state" if damage == "json" else original
    path.write_bytes(damaged)
    path.chmod(0o666 if damage == "file" else 0o600)
    if damage == "directory":
        path.parent.chmod(0o777)
    for route in ("/healthz", "/login", "/api/config", "/api/engines"):
        response = client.get(route, headers=headers if route.startswith("/api/") else {})
        assert response.status_code == 200, (route, response.status_code)
    diagnostic = client.get("/api/engines", headers=headers).json()
    assert any(p["source"] == "manager" for p in diagnostic["problems"])
    if damage == "directory":
        # The same untrusted directory contains roster.json: do not trust its manifest copies
        # for attachment either. The masters are untouched; restoring permissions recovers it.
        assert registry.get_any("claude") is None
        assert registry.capture().retirement_problems
        assert path.parent.stat().st_mode & 0o777 == 0o777
    else:
        assert registry.get_any("claude") is not None
        assert registry.is_retiring("claude"), "the trusted live-session record stays attachable"
    with admission.acquire(before) as guard:
        assert guard.reason, "a read-only fallback admitted new agent work"
    assert path.read_bytes() == damaged
    # Restoring identical bytes/revision must recover; the failed publication is not 'seen'.
    path.parent.chmod(0o700)
    path.chmod(0o600)
    path.write_bytes(original)
    assert client.get("/healthz").status_code == 200
    assert registry.admits(before)
    assert not any(
        p["source"] == "manager"
        for p in client.get("/api/engines", headers=headers).json()["problems"]
    )
