"""Manual session-restart endpoint (#331): POST /api/sessions/{sid}/restart.

Recovers a WEDGED session (agent alive but no longer reading input / painting) by killing the dtach
master + wiping local terminal state so the next attach resumes from disk. These are contract tests:
the process kill (reaper.terminate_master) and the filesystem teardown are monkeypatched, so we
assert the endpoint's auth/owner-guard/cleanup wiring without touching real processes or sockets.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

import agent_sessions.owner as owner_mod
import agent_sessions.ptybridge as ptybridge_mod
import agent_sessions.reaper as reaper_mod
import agent_sessions.scrollback as scrollback_mod
from agent_sessions.main import create_app

_UUID = "11111111-1111-1111-1111-111111111111"  # present in the fake_jsonl fixture
_KEY = f"claude:{_UUID}"
_NONEXISTENT_SOCK = Path("/nonexistent-test-dir/agent-sessions-x.sock")


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _patch_teardown(monkeypatch, *, outcome="term", holder=None):
    """Stub the kill + filesystem teardown; record what the endpoint invoked."""
    calls: dict = {"outcome": outcome}

    async def fake_terminate(engine, sid, *, key=None, **kw):
        calls["terminate"] = (engine, sid, key)
        return outcome

    monkeypatch.setattr(reaper_mod, "terminate_master", fake_terminate)
    monkeypatch.setattr(
        scrollback_mod,
        "clear_scrollback",
        lambda keys=None: calls.__setitem__("clear_scrollback", list(keys or []))
        or {"removed": 0, "bytes_freed": 0},
    )
    monkeypatch.setattr(
        owner_mod, "clear_owner", lambda e, s: calls.__setitem__("clear_owner", (e, s)) or True
    )
    monkeypatch.setattr(owner_mod, "read_owner", lambda e, s: holder)
    # socket unlink is suppressed; point it at a non-existent tmp path so it's a hermetic no-op.
    monkeypatch.setattr(ptybridge_mod, "socket_path", lambda e, s: _NONEXISTENT_SOCK)
    return calls


def test_restart_kills_master_and_cleans(auth_cfg, fake_jsonl, monkeypatch):
    calls = _patch_teardown(monkeypatch, outcome="term", holder=None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "dev1", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json() == {"id": _KEY, "restarted": True, "master": "term"}
    # Kill targeted the physical native id, keyed by the physical buffer key the ws route uses.
    assert calls["terminate"] == ("claude", _UUID, _KEY)
    # Local terminal state wiped under the SAME physical key.
    assert calls["clear_scrollback"] == [_KEY]
    assert calls["clear_owner"] == ("claude", _UUID)


def test_restart_no_master_is_idempotent(auth_cfg, fake_jsonl, monkeypatch):
    _patch_teardown(monkeypatch, outcome="gone", holder=None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json()["restarted"] is False
    assert r.json()["master"] == "gone"


def test_restart_requires_csrf(auth_cfg, fake_jsonl, monkeypatch):
    _patch_teardown(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(f"/api/sessions/{_KEY}/restart", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403


def test_restart_owner_guard_blocks_other_active_tab(auth_cfg, fake_jsonl, monkeypatch):
    # A DIFFERENT device holds a fresh lease → a passive tab may not nuke the session.
    holder = {
        "fp": "other",
        "tab_id": "t2",
        "label": "Other",
        "since": 1.0,
        "last_seen": time.time(),
    }
    calls = _patch_teardown(monkeypatch, holder=holder)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},  # not the holder
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409
    assert "terminate" not in calls  # never killed


def test_restart_force_overrides_owner_guard(auth_cfg, fake_jsonl, monkeypatch):
    holder = {
        "fp": "other",
        "tab_id": "t2",
        "label": "Other",
        "since": 1.0,
        "last_seen": time.time(),
    }
    calls = _patch_teardown(monkeypatch, holder=holder)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1", "force": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert calls["terminate"] == ("claude", _UUID, _KEY)


def test_restart_same_device_owner_passes(auth_cfg, fake_jsonl, monkeypatch):
    # Our OWN tab holds the lease → restart is allowed without force.
    holder = {"fp": "mine", "tab_id": "t1", "label": "Me", "since": 1.0, "last_seen": time.time()}
    calls = _patch_teardown(monkeypatch, holder=holder)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert "terminate" in calls


def test_restart_stale_owner_is_ignored(auth_cfg, fake_jsonl, monkeypatch):
    # A holder whose lease aged out is a ghost → restart proceeds without force.
    holder = {"fp": "other", "tab_id": "t2", "label": "Old", "since": 1.0, "last_seen": 1.0}
    calls = _patch_teardown(monkeypatch, holder=holder)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert "terminate" in calls


def _registry(c):
    return c.app.state.session_registry


def test_restart_blocked_by_registry_claim_flag_off(auth_cfg, fake_jsonl, monkeypatch):
    # Flag-OFF (#184) ownership lives in the in-memory registry, not the on-disk lease. A passive
    # tab must still be blocked from nuking a session another active tab owns (Hermes #332).
    from agent_sessions.session_stream import Claim

    calls = _patch_teardown(monkeypatch, holder=None)  # no disk lease in flag-off mode
    c = _client(auth_cfg)
    monkeypatch.setattr(_registry(c), "current_owner", lambda e, s: Claim("other", "t2"))
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409
    assert "terminate" not in calls  # never killed


def test_restart_registry_same_tab_passes(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions.session_stream import Claim

    calls = _patch_teardown(monkeypatch, holder=None)
    c = _client(auth_cfg)
    monkeypatch.setattr(_registry(c), "current_owner", lambda e, s: Claim("mine", "t1"))
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert "terminate" in calls


def test_restart_force_overrides_registry_claim(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions.session_stream import Claim

    calls = _patch_teardown(monkeypatch, holder=None)
    c = _client(auth_cfg)
    monkeypatch.setattr(_registry(c), "current_owner", lambda e, s: Claim("other", "t2"))
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1", "force": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert "terminate" in calls


def test_restart_spared_midflight_returns_409_and_skips_cleanup(auth_cfg, fake_jsonl, monkeypatch):
    # TOCTOU (Hermes #332): a viewer can (re)claim between the initial guard and the kill. The
    # endpoint passes spare_if to terminate_master, which re-checks and reports "spared" — the
    # route must then 409 and SKIP all cleanup (don't erase the new owner's lease/terminal state).
    calls = _patch_teardown(monkeypatch, outcome="spared", holder=None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        f"/api/sessions/{_KEY}/restart",
        json={"fp": "mine", "tab_id": "t1"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 409
    assert "terminate" in calls  # the kill was attempted…
    assert "clear_scrollback" not in calls  # …but vetoed → cleanup skipped
    assert "clear_owner" not in calls
