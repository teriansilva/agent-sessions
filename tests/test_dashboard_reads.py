"""The BattleLab dashboard's session reads (#1123 Phase 1) and their three read contracts.

1. **Unknown survives the backend** — a failed runtime probe is "couldn't read", never "0 running".
2. **A count equals its destination** — every dashboard count is over the rows the session list
   shows, so the number and the list it links to always agree; "live" and "working" are distinct.
3. **Latest N is by last activity** — sorted before it is limited, review-excluded omitted.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import metadata, prefs, ptybridge, webterm
from agent_sessions.main import create_app
from agent_sessions.routes import sessions as sessions_routes

A = "11111111-1111-1111-1111-111111111111"  # /home/user/claude/repo/a
B = "22222222-2222-2222-2222-222222222222"  # /home/user/claude/repo/a
C = "33333333-3333-3333-3333-333333333333"  # /tmp/other
D = "55555555-5555-5555-5555-555555555555"  # /home/user/claude/demoapp.io
OUTSIDE = "99999999-9999-4999-8999-999999999999"  # running, but not a session the list shows


@pytest.fixture(autouse=True)
def _fresh_cache():
    sessions_routes._running_cache = None
    yield
    sessions_routes._running_cache = None


def _client(cfg):
    c = TestClient(create_app(cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c


def _running(monkeypatch, *sids):
    monkeypatch.setattr(
        ptybridge, "list_sessions_checked", lambda: ([("claude", s) for s in sids], True)
    )


def _age(fake_jsonl, sid: str, seconds_ago: float) -> None:
    """Set a session's last activity (the fixture's JSONLs carry no timestamps, so the scanner
    falls back to the file's mtime)."""
    for p in fake_jsonl.rglob(f"{sid}.jsonl"):
        t = time.time() - seconds_ago
        os.utime(p, (t, t))


def _dash(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    r = c.get(f"/api/dashboard/sessions{'?' + qs if qs else ''}")
    assert r.status_code == 200
    return r.json()


# ---- contract 1: unknown survives ----


def test_an_unreadable_runtime_is_unavailable_never_zero_running(auth_cfg, fake_jsonl, monkeypatch):
    def boom():
        raise OSError("runtime dir unreadable")

    monkeypatch.setattr(ptybridge, "list_sessions_checked", boom)
    c = _client(auth_cfg)
    d = _dash(c)
    assert d["live"] == {"health": "unavailable"}
    # The drill-down refuses the same question rather than listing "nothing running".
    lst = c.get("/api/sessions?running=live").json()
    assert lst.get("running_filter_unavailable") is True and lst["sessions"] == []
    # Recent sessions still read; their running flag is unknown, not False.
    assert d["recent"]["rows"] and all(r["running"] is None for r in d["recent"]["rows"])


# The PRODUCTION boundary (Hermes on #1138): the real enumeration, not a mocked list, so a failure
# that `ptybridge.list_sessions` would fold into "none running" is exercised where it happens.


@pytest.fixture
def runtime(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="rt", dir="/tmp"))  # short: AF_UNIX paths are ~107 bytes
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(d))
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None)
    socks: list[socket.socket] = []

    def add(sid: str) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(d / f"claude-{sid}.sock"))
        socks.append(s)

    yield d, add
    for s in socks:
        s.close()
    shutil.rmtree(d, ignore_errors=True)


def test_an_UNLISTABLE_runtime_dir_is_unavailable(auth_cfg, fake_jsonl, monkeypatch, runtime):
    d, _add = runtime
    real = Path.iterdir

    def iterdir(self):
        if self == d:
            raise PermissionError(13, "denied")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    c = _client(auth_cfg)
    assert _dash(c)["live"] == {"health": "unavailable"}
    assert c.get("/api/sessions?running=live").json().get("running_filter_unavailable") is True


def test_an_INDETERMINATE_socket_probe_is_unavailable(auth_cfg, fake_jsonl, monkeypatch, runtime):
    _d, add = runtime
    add(A)
    add(B)
    verdicts = {A: ptybridge.ALIVE, B: ptybridge.UNKNOWN}
    monkeypatch.setattr(ptybridge, "probe_master", lambda p: verdicts[p.stem.split("-", 1)[1]])
    assert _dash(_client(auth_cfg))["live"] == {"health": "unavailable"}


def test_a_decisive_real_enumeration_counts(auth_cfg, fake_jsonl, monkeypatch, runtime):
    _d, add = runtime
    add(A)
    add(B)
    verdicts = {A: ptybridge.ALIVE, B: ptybridge.DEAD}
    monkeypatch.setattr(ptybridge, "probe_master", lambda p: verdicts[p.stem.split("-", 1)[1]])
    live = _dash(_client(auth_cfg))["live"]
    assert live["health"] == "ok" and live["total"] == 1


def test_an_empty_or_missing_runtime_dir_is_a_healthy_zero(auth_cfg, fake_jsonl, runtime):
    d, _add = runtime
    c = _client(auth_cfg)
    live = _dash(c)["live"]
    assert live["health"] == "ok" and live["total"] == 0
    shutil.rmtree(d)  # removed after it was created: no sockets, so nothing runs
    sessions_routes._running_cache = None
    live = _dash(c)["live"]
    assert live["health"] == "ok" and live["total"] == 0


def test_a_genuinely_empty_runtime_is_zero_running_and_healthy(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch)
    d = _dash(_client(auth_cfg))
    assert d["live"]["health"] == "ok"
    assert d["live"]["total"] == 0 and d["live"]["rows"] == []


# ---- contract 2: a count equals its destination ----


def test_counts_equal_their_drill_downs_and_an_excluded_live_session_counts_nowhere(
    auth_cfg, fake_jsonl, monkeypatch
):
    prefs.set_folder_exclusions(["/tmp/other"])  # C runs, but the list does not show it
    _running(monkeypatch, A, B, C, OUTSIDE)
    # Model steady output while comparing multiple requests. A fixed timestamp ages past the
    # ten-second working window on a loaded CI host, changing the fixture halfway through.
    ages = {f"claude:{A}": 1, f"claude:{B}": 600}  # B: live, not working
    monkeypatch.setattr(
        webterm, "get_last_output_at", lambda k: time.time() - ages[k] if k in ages else None
    )
    c = _client(auth_cfg)
    live = _dash(c)["live"]
    assert live["total"] == 2 and live["working"] == 1
    assert live["by_engine"] == {"claude": {"live": 2, "working": 1}}
    assert {r["id"] for r in live["rows"]} == {f"claude:{A}", f"claude:{B}"}
    # The destinations: the SAME numbers, from the list the tile links to.
    assert c.get("/api/sessions?running=live").json()["total"] == live["total"]
    assert c.get("/api/sessions?running=working").json()["total"] == live["working"]


def test_a_capped_preview_still_counts_the_whole_set(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch, A, B, D)
    c = _client(auth_cfg)
    live = _dash(c, live_limit=1)["live"]
    assert len(live["rows"]) == 1
    assert live["total"] == 3 == c.get("/api/sessions?running=live&limit=1").json()["total"]


def test_the_running_filter_is_validated(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch, A)
    assert _client(auth_cfg).get("/api/sessions?running=everything").status_code == 422


def test_a_live_row_carries_its_current_state_line_not_its_opening(
    auth_cfg, fake_jsonl, monkeypatch
):
    _running(monkeypatch, A)
    metadata.patch(f"claude:{A}", ai_recap="Started the refactor.\nNow: running the e2e suite")
    row = _dash(_client(auth_cfg))["live"]["rows"][0]
    assert row["state_line"] == "Now: running the e2e suite"


# ---- contract 3: latest N is by last activity ----


def test_latest_is_by_last_activity_not_by_band(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch)
    _age(fake_jsonl, A, 3600)  # older, but needs you
    _age(fake_jsonl, B, 60)  # newer, idle-ish
    _age(fake_jsonl, C, 7200)
    _age(fake_jsonl, D, 9000)
    metadata.patch(f"claude:{A}", intervention_required=True)
    rows = _dash(_client(auth_cfg), recent_limit=2)["recent"]["rows"]
    assert [r["id"] for r in rows] == [f"claude:{B}", f"claude:{A}"]
    assert rows[1]["band"] == "needs_you"
    assert rows[0]["band"] == "recently_active"


def test_review_excluded_sessions_are_omitted_from_recent(auth_cfg, fake_jsonl, monkeypatch):
    _running(monkeypatch)
    metadata.patch(f"claude:{B}", review_excluded=True)
    ids = [r["id"] for r in _dash(_client(auth_cfg))["recent"]["rows"]]
    assert f"claude:{B}" not in ids and f"claude:{A}" in ids


def test_the_dashboard_read_needs_a_login(auth_cfg, fake_jsonl):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get("/api/dashboard/sessions").status_code in (401, 403)
