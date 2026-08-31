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


# ---- mission_playbooks through POST /api/prefs (#883 review) -----------------------------
#
# The block was writable only by hand-editing `prefs.json`: a payload containing just this key
# fell past every branch to "no known preference key" and 422'd. Settings had no way to save.


def _PB(**over):
    pb = {
        "default_id": "p",
        "playbooks": [
            {
                "id": "p",
                "label": "P",
                "objectives": [
                    {
                        "key": "live",
                        "title": "It is live",
                        "probe": "http_status",
                        "probe_args": {"url": "https://app.example.com/healthz"},
                        "gate": True,
                    }
                ],
            }
        ],
    }
    pb.update(over)
    return pb


def test_mission_playbooks_round_trips_through_the_prefs_API(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs",
        json={"mission_playbooks": _PB()},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 200, r.text
    assert r.json()["mission_playbooks"]["default_id"] == "p"
    assert prefs.get_mission_playbooks()["playbooks"][0]["objectives"][0]["probe"] == "http_status"


def test_the_prefs_API_refuses_a_malformed_playbook_rather_than_degrading_it(auth_cfg, tmp_home):
    """Strict on write, lenient on read — the same split as `accent` and `term_font_size`.

    Degrading here would take an operator's typo'd probe target and silently store a template
    that can never gate, with the Settings panel showing it as saved.
    """
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    bad = _PB()
    bad["playbooks"][0]["objectives"][0]["probe_args"] = {"url": ["not", "a", "url"]}
    r = c.post(
        "/api/prefs",
        json={"mission_playbooks": bad},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422
    assert "mission_playbooks" in r.json()["detail"]


def test_a_MIXED_payload_with_a_bad_playbook_persists_NOTHING(auth_cfg, tmp_home):
    """The preflight's whole reason to exist (#859): a 422 must mean nothing was written, not
    that the keys before the bad one already landed."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    c.post("/api/prefs", json={"theme": "dark"}, headers=hdr)
    bad = _PB()
    bad["playbooks"][0]["objectives"][0]["probe_args"] = {"url": "gopher://nope"}
    r = c.post("/api/prefs", json={"theme": "light", "mission_playbooks": bad}, headers=hdr)
    assert r.status_code == 422
    assert prefs.get_theme() == "dark", "the valid key landed despite the request being refused"
    # Still the shipped defaults: the refused playbook did not land. (Not `== []` — an install
    # that has never configured playbooks reads as the defaults, which is the other half of this
    # round of review.)
    assert [pb["id"] for pb in prefs.get_mission_playbooks()["playbooks"]] == [
        "ship_a_change",
        "investigate",
    ]


def test_the_prefs_API_refuses_an_explicit_null_playbook_block(auth_cfg, tmp_home):
    """The write side of the absent-vs-null distinction.

    A stored `null` fails CLOSED on read (it degrades to no playbooks), and the API refuses to
    create one in the first place — the operator clears playbooks with an empty BLOCK, which is a
    decision the store can represent, not with a null it cannot. Asserted because I reasoned this
    from the strict path rather than observing it (review on #884).
    """
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    r = c.post("/api/prefs", json={"mission_playbooks": None}, headers=hdr)
    assert r.status_code == 422, r.text
    assert "mission_playbooks" in r.json()["detail"]

    # …and the empty block IS accepted, so "clear my playbooks" remains expressible.
    ok = c.post(
        "/api/prefs", json={"mission_playbooks": {"default_id": "", "playbooks": []}}, headers=hdr
    )
    assert ok.status_code == 200, ok.text
    assert prefs.get_mission_playbooks() == {"default_id": "", "playbooks": []}
