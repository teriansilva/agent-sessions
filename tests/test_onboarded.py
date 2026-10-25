"""First-run onboarding flag (#463): the `onboarded` pref, its fresh-vs-existing-install
inference in `/api/config`, and the `POST /api/prefs` round-trip + validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_sessions import prefs
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


# ---- store --------------------------------------------------------------------


def test_onboarded_unset_is_none(tmp_path):
    assert prefs.get_onboarded(tmp_path / "prefs.json") is None


def test_onboarded_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_onboarded(True, p) is True
    assert prefs.get_onboarded(p) is True
    assert prefs.set_onboarded(False, p) is False
    assert prefs.get_onboarded(p) is False


def test_has_any_prefs_signal(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.has_any_prefs(p) is False
    prefs.set_theme("light", p)  # any pref counts
    assert prefs.has_any_prefs(p) is True


# ---- /api/config inference ----------------------------------------------------


def test_fresh_install_is_not_onboarded(tmp_home, auth_cfg, monkeypatch):
    """No prefs + no scanned sessions (empty tmp HOME) ⇒ the wizard should show."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False


def test_existing_install_with_prefs_is_onboarded(tmp_home, auth_cfg, monkeypatch):
    """An install that has already set any pref is treated as onboarded (no regression)."""
    prefs_path = tmp_home / ".config" / "as" / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(prefs_path))
    prefs.set_theme("light", prefs_path)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is True


def test_install_with_sessions_is_onboarded(fake_jsonl, auth_cfg, monkeypatch):
    """No prefs, but ≥1 scanned session ⇒ existing install ⇒ onboarded (fake_jsonl lays
    down Claude JSONLs under tmp HOME)."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(fake_jsonl / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is True


def test_explicit_pref_wins_over_inference(fake_jsonl, auth_cfg, monkeypatch):
    """Even with sessions present, an explicit onboarded=false shows the wizard."""
    prefs_path = fake_jsonl / ".config" / "as" / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(prefs_path))
    prefs.set_onboarded(False, prefs_path)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False


# ---- POST /api/prefs ----------------------------------------------------------


def test_complete_onboarding_persists(tmp_home, auth_cfg, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False
    r = c.post(
        "/api/prefs",
        json={"onboarded": True},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200
    assert r.json()["onboarded"] is True
    # Survives a fresh app/config read.
    assert c.get("/api/config").json()["onboarded"] is True


def test_onboarded_must_be_boolean(tmp_home, auth_cfg, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / ".config" / "as" / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"onboarded": "yes"},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


# ---- checklists were removed (#1653): `mission_playbooks` is neither served nor written ----------


def _seed_legacy_block() -> dict:
    """An install that HAD checklists: the block as an old build wrote it into `prefs.json`."""
    import json

    path = prefs._default_path()
    data = json.loads(path.read_text()) if path.exists() else {}
    data["mission_playbooks"] = {
        "revision": 3,
        "default_id": "ship_a_change",
        "playbooks": [{"id": "ship_a_change", "label": "Ship a change", "objectives": []}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data["mission_playbooks"]


def _stored_block():
    import json

    return json.loads(prefs._default_path().read_text()).get("mission_playbooks")


def test_a_mission_playbooks_write_is_refused_and_a_mixed_payload_persists_nothing(
    auth_cfg, tmp_home
):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post("/api/prefs", json={"theme": "dark"}, headers=hdr)
    legacy = _seed_legacy_block()
    block = {"revision": 3, "default_id": "", "playbooks": []}
    for payload in ({"mission_playbooks": block}, {"theme": "light", "mission_playbooks": block}):
        r = c.post("/api/prefs", json=payload, headers=hdr)
        assert r.status_code == 422, r.text
        assert "checklists were removed" in r.json()["detail"]
    assert prefs.get_theme() == "dark", "a valid key landed despite the request being refused"
    assert _stored_block() == legacy


def test_the_config_serves_no_checklists_and_the_stored_block_is_left_alone(auth_cfg, tmp_home):
    legacy = _seed_legacy_block()
    c = _client(auth_cfg)  # boot
    csrf = _login(c, auth_cfg)
    assert "mission_playbooks" not in c.get("/api/config").json()
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    assert c.post("/api/prefs", json={"theme": "light"}, headers=hdr).status_code == 200
    # Nothing migrates, rewrites or deletes it (workflow §0): the block is on disk, unread.
    assert _stored_block() == legacy
