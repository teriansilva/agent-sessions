"""What's new (#971): the `whats_new_seen` pref — strict on write, never lowered, stored only once
onboarding is explicitly complete, and always present in `/api/config`."""

from __future__ import annotations

import random
import threading

import pytest
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


def _post(c, cfg, csrf, payload):
    return c.post("/api/prefs", json=payload, headers={"X-CSRF-Token": csrf, "Origin": cfg.origin})


@pytest.fixture
def prefs_path(tmp_home, monkeypatch):
    p = tmp_home / ".config" / "as" / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(p))
    return p


# ---- the version shape ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.20.0", (0, 20, 0)),
        ("0.9.0", (0, 9, 0)),
        ("10.0.12", (10, 0, 12)),
        ("9999.0.0", (9999, 0, 0)),
    ],
)
def test_release_tuple_reads_major_minor_patch(value, expected):
    assert prefs.release_tuple(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,  # bool is an int subclass; refused on type, never read as 1
        20,
        0.2,
        "",
        "0.20",
        "0.20.0.1",
        "v0.20.0",
        "0.20.0rc1",
        "0.20.0.dev1",
        "0.20.0+g1a2b3c",
        "0.20.0\n",
        " 0.20.0",
        "00.20.0",
        "0.020.0",
        "10000.0.0",
        "٠.٢٠.٠",  # Arabic-Indic digits: `\d` would accept these
    ],
)
def test_release_tuple_refuses_anything_that_is_not_exactly_a_release(value):
    assert prefs.release_tuple(value) is None


# ---- the store -----------------------------------------------------------------------------


def test_unset_reads_none(tmp_path):
    assert prefs.get_whats_new_seen(tmp_path / "prefs.json") is None


def test_round_trip(tmp_path):
    p = tmp_path / "prefs.json"
    assert prefs.set_whats_new_seen("0.20.0", p) == "0.20.0"
    assert prefs.get_whats_new_seen(p) == "0.20.0"


def test_a_corrupt_stored_value_reads_none_and_the_next_write_replaces_it(tmp_path):
    p = tmp_path / "prefs.json"
    prefs._set("whats_new_seen", "banana", p)
    assert prefs.get_whats_new_seen(p) is None
    assert prefs.set_whats_new_seen("0.20.0", p) == "0.20.0"


def test_the_store_refuses_a_non_release(tmp_path):
    with pytest.raises(ValueError):
        prefs.set_whats_new_seen("0.20.0rc1", tmp_path / "prefs.json")


def test_an_older_write_after_a_newer_one_keeps_the_newer(tmp_path):
    """The stale-tab case: another device stored 0.21.0; this tab still holds 0.20.0 slides."""
    p = tmp_path / "prefs.json"
    prefs.set_whats_new_seen("0.21.0", p)
    assert prefs.set_whats_new_seen("0.20.0", p) == "0.21.0"
    assert prefs.get_whats_new_seen(p) == "0.21.0"


def test_order_is_numeric_not_lexical(tmp_path):
    """As strings "0.9.0" > "0.10.0"; as releases it is the other way round."""
    p = tmp_path / "prefs.json"
    prefs.set_whats_new_seen("0.9.0", p)
    assert prefs.set_whats_new_seen("0.10.0", p) == "0.10.0"
    assert prefs.set_whats_new_seen("0.9.0", p) == "0.10.0"


def test_concurrent_writes_end_at_the_maximum(tmp_path):
    p = tmp_path / "prefs.json"
    versions = [f"0.{minor}.{patch}" for minor in range(18, 23) for patch in range(3)]
    random.Random(971).shuffle(versions)
    gate = threading.Barrier(len(versions))

    def write(v):
        gate.wait()
        prefs.set_whats_new_seen(v, p)

    threads = [threading.Thread(target=write, args=(v,)) for v in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert prefs.get_whats_new_seen(p) == "0.22.2"


def test_writing_it_preserves_other_keys(tmp_path):
    p = tmp_path / "prefs.json"
    prefs.set_theme("light", p)
    prefs.set_whats_new_seen("0.20.0", p)
    assert prefs.get_theme(p) == "light"


# ---- /api/config -----------------------------------------------------------------------------


def test_config_always_carries_the_key_as_null_until_set(prefs_path, auth_cfg):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    config = c.get("/api/config").json()
    assert "whats_new_seen" in config
    assert config["whats_new_seen"] is None


# ---- POST /api/prefs ---------------------------------------------------------------------------


def test_wizard_completion_with_the_key_persists_both(prefs_path, auth_cfg):
    """A fresh install finishing the wizard marks the current notes seen in the same request, so it
    never gets the wizard and then the dialog."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/config").json()["onboarded"] is False
    r = _post(c, auth_cfg, csrf, {"onboarded": True, "whats_new_seen": "0.20.0"})
    assert r.status_code == 200, r.text
    assert r.json() == {"onboarded": True, "whats_new_seen": "0.20.0"}
    config = c.get("/api/config").json()
    assert config["onboarded"] is True
    assert config["whats_new_seen"] == "0.20.0"


def test_a_lone_write_on_a_fresh_install_is_refused_and_does_not_end_onboarding(
    prefs_path, auth_cfg
):
    """Onboarding is inferred from whether prefs.json holds anything; this key must not be the
    first thing written into it."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _post(c, auth_cfg, csrf, {"whats_new_seen": "0.20.0"})
    assert r.status_code == 409
    assert prefs.has_any_prefs(prefs_path) is False
    assert c.get("/api/config").json()["onboarded"] is False


def test_a_mixed_payload_refused_for_onboarding_persists_nothing(prefs_path, auth_cfg):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _post(c, auth_cfg, csrf, {"theme": "light", "whats_new_seen": "0.20.0"})
    assert r.status_code == 409
    assert prefs.has_any_prefs(prefs_path) is False, "the theme landed despite the refusal"


def test_onboarded_false_in_the_same_request_is_refused_before_anything_is_written(
    prefs_path, auth_cfg
):
    prefs.set_onboarded(True, prefs_path)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _post(c, auth_cfg, csrf, {"onboarded": False, "whats_new_seen": "0.20.0"})
    assert r.status_code == 409
    assert prefs.get_onboarded(prefs_path) is True
    assert prefs.get_whats_new_seen(prefs_path) is None


def test_once_onboarded_a_lone_write_is_accepted(prefs_path, auth_cfg):
    prefs.set_onboarded(True, prefs_path)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _post(c, auth_cfg, csrf, {"whats_new_seen": "0.20.0"})
    assert r.status_code == 200, r.text
    assert c.get("/api/config").json()["whats_new_seen"] == "0.20.0"


@pytest.mark.parametrize("bad", [True, 20, None, "0.20", "0.20.0rc1", "v0.20.0", "0.20.0\n"])
def test_a_non_release_value_is_422_and_nothing_in_the_payload_lands(prefs_path, auth_cfg, bad):
    prefs.set_onboarded(True, prefs_path)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _post(c, auth_cfg, csrf, {"theme": "light", "whats_new_seen": bad})
    assert r.status_code == 422
    assert prefs.get_theme(prefs_path) == "dark"
    assert prefs.get_whats_new_seen(prefs_path) is None


def test_the_route_never_lowers_and_reports_what_it_kept(prefs_path, auth_cfg):
    prefs.set_onboarded(True, prefs_path)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert (
        _post(c, auth_cfg, csrf, {"whats_new_seen": "0.21.0"}).json()["whats_new_seen"] == "0.21.0"
    )
    r = _post(c, auth_cfg, csrf, {"whats_new_seen": "0.20.0"})
    assert r.status_code == 200
    assert r.json()["whats_new_seen"] == "0.21.0"
    assert c.get("/api/config").json()["whats_new_seen"] == "0.21.0"
