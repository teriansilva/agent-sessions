"""An explicit reload reaches another process at its next boundary, preserving old readers."""

import os
import subprocess
import sys

import pytest

from agent_sessions.engines import registry
from agent_sessions.plugins import manager, storage


def test_another_worker_observes_reload_but_a_pinned_reader_keeps_its_generation():
    registry.reload()
    before = registry.get("claude")
    assert before is not None
    try:
        with registry.snapshot_scope(fresh=True) as captured:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import uuid
from agent_sessions.plugins import manager
from agent_sessions.engines import registry
manager.deactivate(str(uuid.uuid4()), 'claude')
registry.request_reload()
""",
                ],
                env=dict(os.environ),
                capture_output=True,
                timeout=20,
                check=True,
            )
            assert not result.stdout
            assert registry.get("claude") is before
            assert not registry.admits(before)
            assert registry.current() is captured
        with registry.snapshot_scope(fresh=True) as new:
            assert new.generation > captured.generation
            assert registry.get("claude") is None
    finally:
        # Preserve the real worker implementation; remove only this test's in-memory overlay.
        from agent_sessions.plugins import FIRST_PARTY_DIR

        registry.reload(first_party_dir=FIRST_PARTY_DIR)


def test_failed_reload_is_pending_and_never_grants_old_launch_admission(monkeypatch):
    registry.reload()
    before = registry.get("claude")
    with storage.locked("reload.json") as path:
        storage.write(path, {"id": "11111111-1111-4111-8111-111111111111"})
    attempts = []

    def fail():
        attempts.append(True)
        raise manager.ManagerError("injected consumer failure")

    monkeypatch.setattr(registry, "reload", fail)
    for _ in range(2):
        with pytest.raises(manager.ManagerError, match="consumer failure"):
            registry.admits(before)
    assert len(attempts) == 2
    assert registry.capture().by_id["claude"] is before


def test_reload_cannot_publish_between_admission_and_process_handoff():
    from agent_sessions.plugins import admission

    with admission.acquire(registry.get("claude")) as guard:
        assert not guard.reason
        with pytest.raises(storage.StateError, match="busy"):
            registry.request_reload()
        assert storage.read(storage.root() / "reload.json") is None
    registry.request_reload()
    assert storage.read(storage.root() / "reload.json") is not None


@pytest.mark.parametrize("broken", [b"{broken", b'{"id":"invalid"}'])
def test_unreadable_reload_signal_keeps_reads_available_but_refuses_new_work(auth_cfg, broken):
    from fastapi.testclient import TestClient

    from agent_sessions import auth
    from agent_sessions.main import create_app
    from agent_sessions.plugins import admission

    registry.request_reload()
    before = registry.get("claude")
    path = storage.root() / "reload.json"
    original = path.read_bytes()
    cookie = auth._serializer(auth_cfg).dumps({"uid": auth_cfg.username, "csrf": "fixture"})
    headers = {"cookie": "agent_sessions=" + cookie}
    client = TestClient(
        create_app(auth_cfg), base_url=auth_cfg.origin, raise_server_exceptions=False
    )
    path.write_bytes(broken)
    for route in ("/healthz", "/login", "/api/config", "/api/engines"):
        response = client.get(route, headers=headers if route.startswith("/api/") else {})
        assert response.status_code == 200, (route, response.status_code)
    problems = client.get("/api/engines", headers=headers).json()["problems"]
    assert any(p["source"] == "reload" for p in problems)
    assert registry.get_any("claude") is before
    with admission.acquire(before) as guard:
        assert guard.reason == admission.UNAVAILABLE
    assert path.read_bytes() == broken
    path.write_bytes(original)
    assert registry.admits(before)
    assert not any(
        p["source"] == "reload"
        for p in client.get("/api/engines", headers=headers).json()["problems"]
    )
