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


def test_engines_lists_all_providers(auth_cfg, fake_jsonl, tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_home / "bin"))
    claude_bin = tmp_home / ".local" / "bin" / "claude"
    claude_bin.parent.mkdir(parents=True)
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)

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
    # claude is installed in the isolated HOME, so it can launch new sessions
    claude = next(e for e in d["engines"] if e["id"] == "claude")
    assert claude["present"] is True
    assert claude["supports_new"] is True
    assert claude["bin"] == str(claude_bin)


def test_engines_marks_binary_only_opencode_installed(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    oc_bin = tmp_home / ".opencode" / "bin" / "opencode"
    oc_bin.parent.mkdir(parents=True)
    oc_bin.write_text("#!/bin/sh\n")
    oc_bin.chmod(0o755)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/engines").json()
    opencode = next(e for e in d["engines"] if e["id"] == "opencode")
    assert opencode == {
        "id": "opencode",
        "present": True,
        "supports_new": True,
        "bin": str(oc_bin),
    }


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


# ---- /api/perf (#652 measurement scaffold) -------------------------------------


def test_perf_requires_auth(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/perf").status_code == 401


def test_perf_reports_recorded_metrics(auth_cfg):
    from agent_sessions import perfstats

    perfstats.reset()
    for v in (10.0, 20.0, 30.0):
        perfstats.record("api_sessions_ms", v)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/perf")
    assert r.status_code == 200
    d = r.json()
    assert "api_sessions_ms" in d
    row = d["api_sessions_ms"]
    assert row["count"] == 3
    assert set(row) == {"count", "p50", "p95", "p99", "max", "mean"}
    assert row["max"] == 30.0
    perfstats.reset()


def test_perf_reset_query_clears_after_returning(auth_cfg):
    from agent_sessions import perfstats

    perfstats.reset()
    perfstats.record("attach_prep_ms", 5.0)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    # ?reset=1 returns the current window THEN clears it.
    first = c.get("/api/perf", params={"reset": 1})
    assert first.status_code == 200
    assert first.json()["attach_prep_ms"]["count"] == 1
    # Second read is empty — the window was cleared.
    assert c.get("/api/perf").json() == {}


def test_perf_api_sessions_probe_fires_on_real_request(auth_cfg, fake_jsonl):
    # End-to-end: a real /api/sessions request must record an `api_sessions_ms` sample
    # via the `perfstats.timed(...)` wrapper on the actual handler — proving the probe
    # measures the production path, not just direct record() calls.
    from agent_sessions import perfstats

    perfstats.reset()
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/sessions").status_code == 200
    row = c.get("/api/perf").json().get("api_sessions_ms")
    assert row is not None and row["count"] >= 1
    assert row["p50"] >= 0.0
    perfstats.reset()


# ---- /api/system/sessions (#346 Phase C) ---------------------------------------


def test_system_sessions_requires_auth(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/system/sessions").status_code == 401


def test_system_sessions_lists_socks_with_scope_fields(auth_cfg, fake_jsonl, tmp_path, monkeypatch):
    # Shape contract: one row per live sock file; pid/scope are null when no master
    # matches (nothing in /proc binds these socks) — the endpoint must not error.
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))
    from agent_sessions import ptybridge

    d = ptybridge.runtime_dir()
    (d / "claude-aaaa.sock").touch()
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/system/sessions")
    assert r.status_code == 200
    rows = r.json()["sessions"]
    names = {row["sock"] for row in rows}
    assert "claude-aaaa.sock" in names
    row = next(x for x in rows if x["sock"] == "claude-aaaa.sock")
    assert row["pid"] is None and row["scope"] is None


def test_dtach_master_sock_matcher_is_strict():
    # Hermes #354: only a real `dtach -c <sock>` cmdline maps; lookalikes don't.
    from agent_sessions.routes.system import _dtach_master_sock

    assert (
        _dtach_master_sock([b"/usr/bin/dtach", b"-c", b"/run/u/claude-a.sock", b"-z"])
        == "/run/u/claude-a.sock"
    )
    assert _dtach_master_sock([b"dtach", b"-c", b"/x/y.sock"]) == "/x/y.sock"
    # python -c '…' with a sock-looking trailing arg (the reproduced false positive)
    py = [b"/usr/bin/python3", b"-c", b"import time", b"/x/claude-f.sock"]
    assert _dtach_master_sock(py) is None
    # the sock must IMMEDIATELY follow -c
    assert _dtach_master_sock([b"/usr/bin/dtach", b"-c", b"-z", b"/x/y.sock"]) is None
    # dtach attach mode (-a) is a viewer, not a master
    assert _dtach_master_sock([b"/usr/bin/dtach", b"-a", b"/x/y.sock"]) is None
    assert _dtach_master_sock([]) is None and _dtach_master_sock([b""]) is None


def test_system_sessions_ignores_non_dtach_sock_lookalike(
    auth_cfg, fake_jsonl, tmp_path, monkeypatch
):
    # Endpoint-level regression for the Hermes #354 false positive: a non-dtach process
    # whose argv contains `-c` and our sock path must NOT be reported as the master.
    import subprocess
    import sys

    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))
    from agent_sessions import ptybridge

    sock = ptybridge.runtime_dir() / "claude-false.sock"
    sock.touch()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)", "-c", str(sock)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        c = _client(auth_cfg)
        _login(c, auth_cfg)
        rows = c.get("/api/system/sessions").json()["sessions"]
        row = next(x for x in rows if x["sock"] == "claude-false.sock")
        assert row["pid"] is None and row["scope"] is None
    finally:
        proc.kill()
        proc.wait(timeout=5)
