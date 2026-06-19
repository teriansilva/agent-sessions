"""Session-list sort order (#506): per-engine ``created_at`` derivation, the recent_activity /
created_at server-side sort modes (favorites pinned in both), and the pref plumbing + 422 guard."""

from __future__ import annotations

import json
import os
from datetime import datetime

from fastapi.testclient import TestClient

from agent_sessions import engines, prefs, scanner
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
    return c.get("/api/config").json()["csrf"]


def _iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---- _parse_epoch: seconds / milliseconds / ISO / junk ------------------------


def test_parse_epoch_variants():
    assert scanner._parse_epoch(1_700_000_000) == 1_700_000_000.0  # seconds
    assert scanner._parse_epoch(1_700_000_000_000) == 1_700_000_000.0  # ms heuristic
    assert scanner._parse_epoch("2026-01-02T03:04:05Z") == _iso("2026-01-02T03:04:05Z")
    assert scanner._parse_epoch("not-a-date") is None
    assert scanner._parse_epoch(True) is None  # bool is not a timestamp
    assert scanner._parse_epoch(0) is None
    assert scanner._parse_epoch(None) is None


def test_first_record_created_at_skips_malformed_head(tmp_home):
    f = tmp_home / "x.jsonl"
    # Junk line + a record without a timestamp + a record WITH one — all within the line cap,
    # so the head is scanned but never the whole file.
    f.write_text(
        "not json at all\n"
        + json.dumps({"type": "user", "message": {"content": "a"}})
        + "\n"
        + json.dumps({"type": "assistant", "timestamp": "2026-03-04T05:06:07Z"})
        + "\n"
    )
    assert scanner.first_record_created_at(f) == _iso("2026-03-04T05:06:07Z")


def test_first_record_created_at_none_when_no_timestamp_in_head(tmp_home):
    f = tmp_home / "y.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"content": "no ts"}}) + "\n")
    assert scanner.first_record_created_at(f) is None


# ---- per-engine created_at derivation -----------------------------------------


def test_claude_created_at_from_first_record(tmp_home):
    proj = tmp_home / ".claude" / "projects" / "-home-user-claude-x"
    proj.mkdir(parents=True)
    f = proj / "11111111-1111-1111-1111-111111111111.jsonl"
    f.write_text(
        json.dumps(
            {"type": "user", "timestamp": "2026-01-02T03:04:05Z", "message": {"content": "hi"}}
        )
        + "\n"
    )
    # Push the file mtime far into the future: created_at must come from the record, not the file.
    os.utime(f, (2_000_000_000, 2_000_000_000))
    s = next(x for x in scanner.scan(tmp_home) if x.uuid.startswith("11111111"))
    assert s.created_at == _iso("2026-01-02T03:04:05Z")
    assert s.last_mtime == 2_000_000_000


def test_claude_created_at_falls_back_to_fs(tmp_home):
    proj = tmp_home / ".claude" / "projects" / "-home-user-claude-z"
    proj.mkdir(parents=True)
    f = proj / "22222222-2222-2222-2222-222222222222.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"content": "no ts"}}) + "\n")
    s = next(x for x in scanner.scan(tmp_home) if x.uuid.startswith("22222222"))
    # No record timestamp → filesystem fallback: a positive epoch matching the helper.
    assert s.created_at > 0
    assert s.created_at == scanner.fs_created_at(f.stat())


def test_opencode_created_at_ms_to_seconds(opencode_db):
    top = next(r for r in engines.scan_all() if r.uuid == "ses_aaaaaaaaaaaaaaaaaaaaaaaa")
    # conftest seeds time_created=1777400000000 ms for OC_TOP; the row exposes it in seconds.
    assert top.created_at == 1777400000000 / 1000.0
    assert top.last_mtime == 1777460564154 / 1000.0


# ---- prefs round-trip + coercion ----------------------------------------------


def test_session_list_order_unset_is_recent_activity(tmp_path):
    assert prefs.get_session_list_order(tmp_path / "prefs.json") == "recent_activity"


def test_session_list_order_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_session_list_order("created_at", p) == "created_at"
    assert prefs.get_session_list_order(p) == "created_at"


def test_session_list_order_unknown_coerced_to_default(tmp_path):
    p = tmp_path / "prefs.json"
    # An unknown/legacy persisted value normalizes back to recent_activity on read.
    p.write_text('{"session_list_order": "by_title"}')
    assert prefs.get_session_list_order(p) == "recent_activity"
    assert prefs.set_session_list_order("sideways", p) == "recent_activity"


# ---- API: the two sort modes + sticky precedence + the 422 guard --------------


def _seed_three(tmp_home):
    """Three claude sessions whose CREATION order is the REVERSE of their UPDATE order:
    a = oldest created / newest updated … c = newest created / oldest updated."""
    root = tmp_home / ".claude" / "projects" / "-home-user-claude-s"
    root.mkdir(parents=True)
    specs = [
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "2026-01-01T00:00:00Z", 3000),
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "2026-01-02T00:00:00Z", 2000),
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", "2026-01-03T00:00:00Z", 1000),
    ]
    for uuid, created, mtime in specs:
        f = root / f"{uuid}.jsonl"
        f.write_text(
            json.dumps({"type": "user", "timestamp": created, "message": {"content": uuid[:4]}})
            + "\n"
        )
        os.utime(f, (mtime, mtime))


def test_sessions_default_order_is_recent_activity(auth_cfg, tmp_home):
    _seed_three(tmp_home)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = c.get("/api/sessions?limit=200").json()["sessions"]
    # newest update first: a (mtime 3000) > b (2000) > c (1000)
    assert [r["uuid"][:4] for r in rows] == ["aaaa", "bbbb", "cccc"]
    assert [r["created_at"] for r in rows] == [
        _iso("2026-01-01T00:00:00Z"),
        _iso("2026-01-02T00:00:00Z"),
        _iso("2026-01-03T00:00:00Z"),
    ]


def test_sessions_created_at_order(auth_cfg, tmp_home):
    _seed_three(tmp_home)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert (
        c.post("/api/prefs", json={"session_list_order": "created_at"}, headers=hdr).status_code
        == 200
    )
    rows = c.get("/api/sessions?limit=200").json()["sessions"]
    # newest CREATED first: c (Jan 3) > b (Jan 2) > a (Jan 1) — the reverse of update order.
    assert [r["uuid"][:4] for r in rows] == ["cccc", "bbbb", "aaaa"]


def test_favorites_pin_to_top_in_both_orders(auth_cfg, tmp_home):
    _seed_three(tmp_home)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    # Favorite b — first by NEITHER mtime nor created order.
    assert (
        c.post(
            "/api/sessions/claude:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/favorite", headers=hdr
        ).status_code
        == 200
    )
    for order in ("recent_activity", "created_at"):
        c.post("/api/prefs", json={"session_list_order": order}, headers=hdr)
        rows = c.get("/api/sessions?limit=200").json()["sessions"]
        assert rows[0]["uuid"][:4] == "bbbb", order  # sticky leads in BOTH modes
        assert rows[0]["sticky"] is True


def test_unknown_session_list_order_is_422(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    r = c.post("/api/prefs", json={"session_list_order": "by_title"}, headers=hdr)
    assert r.status_code == 422


def test_config_echoes_default_order(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["session_list_order"] == "recent_activity"
