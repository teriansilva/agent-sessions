"""AI-review prefs block (#356 Phase 1): server-side schema validation, write-only API
key (masked-sentinel round trip), explicit 0600 prefs perms, and no-key-leak guarantees
across /api/config, /api/prefs echoes, and error bodies."""

from __future__ import annotations

import json
import stat

from fastapi.testclient import TestClient

from agent_sessions import prefs
from agent_sessions.main import create_app

SECRET = "sk-supersecret-9d8f7a"  # noqa: S105 — test fixture value


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


def _post_prefs(c, cfg, csrf, block):
    return c.post(
        "/api/prefs",
        json={"ai_review": block},
        headers={"X-CSRF-Token": csrf, "Origin": cfg.origin},
    )


# ---- pure prefs layer ----------------------------------------------------------------


def test_defaults_round_trip(tmp_home):
    block = prefs.get_ai_review()
    assert block["enabled"] is False
    assert block["api_key"] == ""
    assert block["prompt"] == prefs.DEFAULT_AI_REVIEW_PROMPT
    assert block["interval_minutes"] == 5


def test_prefs_file_is_chmod_0600_on_write(tmp_home, monkeypatch):
    path = tmp_home / "prefs.json"
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(path))
    # Pre-create world-readable: the write path must tighten it, not inherit it.
    path.write_text("{}")
    path.chmod(0o644)
    prefs.set_ai_review({"api_key": SECRET})
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    # Any other pref write keeps it tight too.
    prefs.set_theme("dark")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_masked_sentinel_round_trip(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"api_key": SECRET, "base_url": "https://ai.example/v1"})
    assert prefs.get_ai_review()["api_key"] == SECRET
    # "" and the mask both mean "unchanged".
    prefs.set_ai_review({"api_key": ""})
    assert prefs.get_ai_review()["api_key"] == SECRET
    prefs.set_ai_review({"api_key": prefs.AI_REVIEW_KEY_MASK})
    assert prefs.get_ai_review()["api_key"] == SECRET
    # A real new value replaces; null clears.
    prefs.set_ai_review({"api_key": "sk-new"})
    assert prefs.get_ai_review()["api_key"] == "sk-new"  # noqa: S105
    prefs.set_ai_review({"api_key": None})
    assert prefs.get_ai_review()["api_key"] == ""
    assert prefs.public_ai_review()["api_key_set"] is False


def test_public_view_never_contains_the_key(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"api_key": SECRET})
    pub = prefs.public_ai_review()
    assert "api_key" not in pub
    assert pub["api_key_set"] is True
    assert SECRET not in json.dumps(pub)


def test_empty_prompt_falls_back_to_default(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"prompt": "   "})
    assert prefs.get_ai_review()["prompt"] == prefs.DEFAULT_AI_REVIEW_PROMPT


def test_validate_rejects_bad_blocks():
    cases = [
        ("not a dict", "object"),
        ({"bogus": 1}, "unknown"),
        ({"enabled": "yes"}, "boolean"),
        ({"base_url": "ftp://nope"}, "http"),
        ({"base_url": "https://" + "x" * 2000}, "http"),
        ({"api_key": 42}, "api_key"),
        ({"model": "m" * 999}, "model"),
        ({"prompt": "p" * 90000}, "prompt"),
        ({"interval_minutes": 0}, "interval_minutes"),
        ({"interval_minutes": True}, "interval_minutes"),
        ({"max_input_chars": 10}, "max_input_chars"),
        ({"request_timeout": 5}, "request_timeout"),
        ({"request_timeout": 601}, "request_timeout"),
        ({"request_timeout": "x"}, "request_timeout"),
        ({"request_timeout": True}, "request_timeout"),
        ({"request_timeout": float("nan")}, "request_timeout"),
    ]
    for patch, frag in cases:
        err = prefs.validate_ai_review_patch(patch)
        assert err is not None and frag in err, (patch, err)
    assert prefs.validate_ai_review_patch({"base_url": "", "enabled": True}) is None
    assert (
        prefs.validate_ai_review_patch({"base_url": "https://ai.example/v1", "interval_minutes": 5})
        is None
    )
    # request_timeout: the full [10, 600] band (int or float) plus null-to-unset.
    for ok in (10, 600, 120, 90.5, None):
        assert prefs.validate_ai_review_patch({"request_timeout": ok}) is None, ok


def test_request_timeout_round_trip_and_unset(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    assert prefs.get_ai_review()["request_timeout"] is None  # default = unset
    prefs.set_ai_review({"request_timeout": 180})
    assert prefs.get_ai_review()["request_timeout"] == 180
    # Non-secret: the public view (what /api/config returns) carries it for the UI.
    assert prefs.public_ai_review()["request_timeout"] == 180
    prefs.set_ai_review({"request_timeout": None})  # null clears back to unset
    assert prefs.get_ai_review()["request_timeout"] is None
    # An out-of-range value smuggled into prefs.json on disk is dropped on read.
    prefs.set_ai_review({"request_timeout": 180})
    raw = json.loads((tmp_home / "prefs.json").read_text())
    raw["ai_review"]["request_timeout"] = 2
    (tmp_home / "prefs.json").write_text(json.dumps(raw))
    assert prefs.get_ai_review()["request_timeout"] is None


# ---- HTTP surface ----------------------------------------------------------------------


def test_api_prefs_validates_and_masks(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)

    # Invalid block → 422, nothing persisted.
    r = _post_prefs(c, auth_cfg, csrf, {"interval_minutes": 0})
    assert r.status_code == 422
    assert prefs.get_ai_review()["interval_minutes"] == 5
    r = _post_prefs(c, auth_cfg, csrf, {"unknown_field": 1})
    assert r.status_code == 422
    r = _post_prefs(c, auth_cfg, csrf, "nope")
    assert r.status_code == 422
    r = _post_prefs(c, auth_cfg, csrf, {"request_timeout": 5})
    assert r.status_code == 422
    assert prefs.get_ai_review()["request_timeout"] is None
    # Valid timeout persists and is echoed in the public view.
    r = _post_prefs(c, auth_cfg, csrf, {"request_timeout": 240})
    assert r.status_code == 200
    assert r.json()["ai_review"]["request_timeout"] == 240

    # Valid write → echo is the PUBLIC view (api_key_set, no key value).
    r = _post_prefs(
        c,
        auth_cfg,
        csrf,
        {"base_url": "https://ai.example/v1", "api_key": SECRET, "model": "m2"},
    )
    assert r.status_code == 200
    echoed = r.json()["ai_review"]
    assert echoed["api_key_set"] is True
    assert echoed["configured"] is True
    assert SECRET not in r.text

    # /api/config carries the same public view — never the key.
    conf = c.get("/api/config")
    assert conf.status_code == 200
    assert SECRET not in conf.text
    assert conf.json()["ai_review"]["api_key_set"] is True
    assert conf.json()["ai_review"]["default_prompt"] == prefs.DEFAULT_AI_REVIEW_PROMPT

    # Masked-sentinel round trip over HTTP: posting the mask back preserves the key.
    r = _post_prefs(c, auth_cfg, csrf, {"api_key": prefs.AI_REVIEW_KEY_MASK, "model": "m3"})
    assert r.status_code == 200
    assert prefs.get_ai_review()["api_key"] == SECRET
    assert prefs.get_ai_review()["model"] == "m3"

    # 422 error bodies never echo the stored key either.
    r = _post_prefs(c, auth_cfg, csrf, {"interval_minutes": -3})
    assert r.status_code == 422
    assert SECRET not in r.text
