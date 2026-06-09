"""Discovery + system-info endpoints (Settings "Connected agents" + "System").

``/api/engines`` lists every known provider with presence/new-session/bin; ``/api/system``
returns the documented host fields (stdlib-only, fail-soft). Both authed-only.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from agent_sessions import engines
from agent_sessions.main import create_app


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


# ---- /api/engines -------------------------------------------------------------


def test_engines_lists_all_providers(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/engines")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"engines"}
    ids = {e["id"] for e in d["engines"]}
    # every registered provider is reported, present or not
    assert ids == {p.engine_id for p in engines.all_providers()}
    for e in d["engines"]:
        assert set(e) == {"id", "present", "supports_new", "bin"}
        assert isinstance(e["present"], bool)
        assert isinstance(e["supports_new"], bool)
        assert e["bin"] is None or isinstance(e["bin"], str)
    # claude is present in the fake-jsonl fixture
    claude = next(e for e in d["engines"] if e["id"] == "claude")
    assert claude["present"] is True
    assert claude["supports_new"] is True


def test_engines_requires_auth(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/engines").status_code == 401


# ---- /api/system --------------------------------------------------------------


def test_system_shape_and_version(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/system")
    assert r.status_code == 200
    d = r.json()
    # Always-present (platform stdlib works everywhere); version is plausible.
    for key in ("os", "platform", "arch", "python", "version", "hostname", "cpus"):
        assert key in d, key
    assert isinstance(d["version"], str) and re.search(r"\d", d["version"])
    assert isinstance(d["cpus"], int) and d["cpus"] >= 1
    # No network interfaces / IPs leak into the payload.
    assert not any(k in d for k in ("ip", "ips", "interfaces", "addresses", "mac"))


def test_system_no_network_fields_and_disk(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/system").json()
    # disk_usage(Path.home()) works on any platform with a real fs.
    assert "disk_total" in d and "disk_free" in d
    assert d["disk_total"] >= d["disk_free"] >= 0
