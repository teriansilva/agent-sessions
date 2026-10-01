"""Discovery + system-info endpoints (Settings "Connected agents" + "System").

``/api/engines`` lists every known provider with presence/new-session/bin; ``/api/system``
returns the documented host fields (stdlib-only, fail-soft). Both authed-only.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from agent_sessions import engines
from agent_sessions.main import create_app
from agent_sessions.plugins import kinds


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
    assert set(d) == {"engines", "problems"} and d["problems"] == []
    ids = {e["id"] for e in d["engines"]}
    # every registered provider is reported, present or not
    assert ids == {p.engine_id for p in engines.all_providers()}
    for e in d["engines"]:
        assert set(e) == {
            "id",
            "present",
            "supports_new",
            "supports_seed_start",
            "seed_reason",
            "bin",
            # #853 P3 — the roster the SPA renders from, read from each manifest.
            "label",
            "kind",
            "runtime",
            "display",
            "capabilities",
            "session_id",
            "models",
            # #1189 — what a launch may choose, and the instruction files the engine reads.
            "model_select",
            "instructions",
            "usage",
            "terminal",
            "status",
            "status_reason",
        }
        m = engines.get(e["id"]).manifest
        assert e["label"] == m.identity.label and e["kind"] == m.identity.kind
        assert e["runtime"] == m.runtime  # `pty`, or `chat` for the API agent (#1209)
        assert e["display"] == {
            "name": m.display.name,
            "badge": m.display.badge,
            "accent": m.display.accent,
            "id_prefix": m.display.id_prefix,
            "order": m.display.order,
        }
        assert e["capabilities"] == {c: m.can(c) for c in kinds.CAPABILITIES}
        assert e["session_id"] == {"mint": m.session_id.mint}
        assert e["usage"] == {"source": m.usage.source}
        assert e["terminal"] == {"repaint": m.terminal.repaint}
        assert set(e["model_select"]) == {
            "supported",
            "on_resume",
            "configured_elsewhere",
            "offered",
        }
        assert e["instructions"] == {"files": list(m.instructions)}
        assert (e["status"], e["status_reason"]) == ("active", None)
        assert isinstance(e["present"], bool)
        assert isinstance(e["supports_new"], bool)
        # Handoff-target capability (#597): bool + a reason exactly when unsupported.
        assert isinstance(e["supports_seed_start"], bool)
        assert (e["seed_reason"] is None) == e["supports_seed_start"]
        assert e["bin"] is None or isinstance(e["bin"], str)
    # claude is installed in the isolated HOME, so it can launch new sessions
    claude = next(e for e in d["engines"] if e["id"] == "claude")
    assert claude["present"] is True
    assert claude["supports_new"] is True
    assert claude["bin"] == str(claude_bin)
    # #1189: claude takes `--model` (also on resume) and offers its manifest's models, `default`
    # excluded; opencode's model is its own configuration, so it offers nothing.
    sel = claude["model_select"]
    assert (sel["supported"], sel["on_resume"], sel["configured_elsewhere"]) == (True, True, False)
    offered = {o["id"]: o for o in sel["offered"]}
    assert offered["claude-opus-5"] == {
        "id": "claude-opus-5",
        "aliases": ["opus"],
        "context_window": 1000000,
        "source": "manifest",
    }
    assert "default" not in offered
    assert claude["instructions"] == {"files": ["CLAUDE.md"]}
    opencode = next(e for e in d["engines"] if e["id"] == "opencode")
    assert opencode["model_select"]["offered"] == []
    assert opencode["model_select"]["configured_elsewhere"] is True


def test_engines_marks_binary_only_opencode_installed(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    # Force discovery through the isolated HOME rather than a live opencode on the
    # machine running the suite (PATH has higher precedence than known install dirs).
    monkeypatch.setenv("PATH", str(tmp_home / "bin"))
    oc_bin = tmp_home / ".opencode" / "bin" / "opencode"
    oc_bin.parent.mkdir(parents=True)
    oc_bin.write_text("#!/bin/sh\n")
    oc_bin.chmod(0o755)

    c = _client(auth_cfg)
    _login(c, auth_cfg)
    d = c.get("/api/engines").json()
    opencode = next(e for e in d["engines"] if e["id"] == "opencode")
    assert {k: opencode[k] for k in ("id", "present", "supports_new", "supports_seed_start")} == {
        "id": "opencode",
        "present": True,
        "supports_new": True,
        "supports_seed_start": True,
    }
    assert opencode["seed_reason"] is None and opencode["bin"] == str(oc_bin)
    # The roster order and display come from the manifests, not from the SPA (#853 P3).
    assert [e["id"] for e in d["engines"]] == engines.engine_ids()
    assert opencode["display"]["id_prefix"] == "ses_"
    assert opencode["session_id"]["mint"] == "adopt"


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
    # Second read carries no timing window — it was cleared. `scan_memo` (#1048) rides along on
    # every response and is NOT a timing window, so it is excluded by name rather than by the
    # body happening to be empty: a reader that asserted `== {}` was asserting "no other key has
    # ever been added to this endpoint", which is not the property this test is about.
    second = c.get("/api/perf").json()
    assert {k: v for k, v in second.items() if k != "scan_memo"} == {}
    # …and the memo's own counters are reset by the same `?reset=1`, so a before/after run
    # measures a cold walk rather than inheriting the previous run's hits.
    assert second["scan_memo"] == {"entries": 0, "hits": 0, "misses": 0}


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


def test_every_availability_route_agrees_with_the_launcher(
    auth_cfg, fake_jsonl, tmp_home, monkeypatch
):
    """#853 P2: `/api/engines`, `/api/config.new_session_engines` and the handoff target check
    must give the launcher's answer (`engines.launchable_bin`) — a binary only on PATH is NOT
    offered by any of them, and becomes offered everywhere once the manifest's env var names it.
    Route-level, so a consumer quietly rewired back to a PATH probe turns this red."""
    gemini_on_path = tmp_home / "pathonly" / "gemini"
    gemini_on_path.parent.mkdir(parents=True)
    gemini_on_path.parent.chmod(0o755)
    gemini_on_path.write_bytes(b"#!/bin/true\n")
    gemini_on_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gemini_on_path.parent}:/usr/bin:/bin")
    monkeypatch.delenv("AGENT_SESSIONS_GEMINI_BIN", raising=False)
    engines.get("gemini")._cached = None

    c = _client(auth_cfg)
    _login(c, auth_cfg)

    def views():
        rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
        cfg = c.get("/api/config").json()
        return rows["gemini"], "gemini" in cfg["new_session_engines"]

    row, offered = views()
    assert (row["present"], row["bin"], row["supports_new"], offered) == (False, None, False, False)

    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_BIN", str(gemini_on_path))
    row, offered = views()
    assert (row["present"], row["bin"], row["supports_new"], offered) == (
        True,
        str(gemini_on_path),
        True,
        True,
    )
    assert engines.launchable_bin(engines.get("gemini")) == row["bin"]
