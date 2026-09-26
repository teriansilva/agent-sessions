"""#853 P4 — the server half of the Agents pages: an engine's detail, the loader's diagnostics,
and the `agent_defaults` a new session starts with."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, prefs
from agent_sessions.engines import registry
from agent_sessions.main import create_app
from agent_sessions.plugins import FIRST_PARTY_DIR


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


def _post_prefs(c, cfg, body):
    csrf = c.get("/api/config").json()["csrf"]
    return c.post("/api/prefs", json=body, headers={"X-CSRF-Token": csrf, "Origin": cfg.origin})


# --- detail ---------------------------------------------------------------------------------------


def test_the_detail_route_is_what_the_manifest_declares(auth_cfg, engine_bin):
    b = engine_bin("claude")
    c = _client(auth_cfg)
    d = c.get("/api/engines/claude").json()
    m = engines.get("claude").manifest
    assert (d["id"], d["label"], d["publisher"], d["contract"]) == (
        "claude",
        m.identity.label,
        "battlelab",
        1,
    )
    assert d["source"] == "in-tree" and d["runtime"] == "pty" and d["kind"] == "agent"
    assert d["provenance"]["state"] == "adopted" and d["provenance"]["path"] == b
    assert d["binary"]["env_var"] == "AGENT_SESSIONS_CLAUDE_BIN"
    assert d["store"]["layout"] == "claude-projects" and d["store"]["root"] == "~/.claude"
    assert d["launch"] == {"resume": "flag", "new": "pin-flag", "admission": "none"}
    assert d["transcript"]["kind"] == "claude-jsonl"
    assert d["usage"] == {"source": "plan", "kind": "claude-cli-probe"}
    assert all(d["capabilities"].values())


def test_an_absent_binary_is_a_state_not_an_error(auth_cfg, no_engine_bin):
    c = _client(auth_cfg)
    r = c.get("/api/engines/gemini")
    assert r.status_code == 200
    assert r.json()["provenance"]["state"] in ("absent", "refused")


@pytest.mark.parametrize("bad", ["nope", "..", "claude.json", "CLAUDE"])
def test_an_unknown_engine_is_a_404_and_never_a_path(auth_cfg, bad):
    c = _client(auth_cfg)
    assert c.get(f"/api/engines/{bad}").status_code == 404


def test_the_detail_route_needs_a_login(auth_cfg):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get("/api/engines/claude").status_code == 401


# --- loader diagnostics ---------------------------------------------------------------------------


def test_a_manifest_that_did_not_load_is_a_PROBLEM_never_an_engine(auth_cfg, tmp_path):
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    (fp / "acme").mkdir()
    (fp / "acme" / "plugin.toml").write_text('contract = 1\n[identity]\nid = "acme"\n')
    registry.reload(first_party_dir=fp)
    try:
        c = _client(auth_cfg)
        body = c.get("/api/engines").json()
        assert "acme" not in {e["id"] for e in body["engines"]}
        (p,) = [p for p in body["problems"] if "acme" in p["source"]]
        assert p["error"]
        assert c.get("/api/engines/acme").status_code == 404
    finally:
        registry.reload()
    assert _client(auth_cfg).get("/api/engines").json()["problems"] == []


# --- agent_defaults -------------------------------------------------------------------------------


def test_defaults_start_as_today(auth_cfg):
    c = _client(auth_cfg)
    assert c.get("/api/config").json()["agent_defaults"] == {"default_engine": None, "bypass": True}


@pytest.mark.parametrize(
    "patch",
    [
        {"bypass": "false"},
        {"bypass": 0},
        {"bypass": None},
        {"default_engine": "nope"},
        {"default_engine": 1},
        {"extra": True},
        [],
    ],
)
def test_defaults_are_STRICT_on_write(auth_cfg, patch):
    c = _client(auth_cfg)
    r = _post_prefs(c, auth_cfg, {"agent_defaults": patch})
    assert r.status_code == 422, r.text
    assert c.get("/api/config").json()["agent_defaults"] == {"default_engine": None, "bypass": True}


def test_a_422_in_agent_defaults_writes_NOTHING_else_in_the_patch(auth_cfg):
    c = _client(auth_cfg)
    r = _post_prefs(c, auth_cfg, {"theme": "light", "agent_defaults": {"bypass": "no"}})
    assert r.status_code == 422
    assert c.get("/api/config").json()["theme"] != "light"


def test_defaults_are_LENIENT_on_read(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text('{"agent_defaults": {"default_engine": "../x", "bypass": "yes", "junk": 1}}')
    assert prefs.get_agent_defaults(p) == {"default_engine": None, "bypass": True}


def test_a_partial_write_keeps_the_other_field(auth_cfg):
    c = _client(auth_cfg)
    assert (
        _post_prefs(c, auth_cfg, {"agent_defaults": {"default_engine": "codex"}}).status_code == 200
    )
    assert _post_prefs(c, auth_cfg, {"agent_defaults": {"bypass": False}}).status_code == 200
    assert c.get("/api/config").json()["agent_defaults"] == {
        "default_engine": "codex",
        "bypass": False,
    }


def test_shell_can_be_the_default_for_new_sessions(auth_cfg):
    """It can start a new session; eligibility for handoff/missions is the pickers' business."""
    c = _client(auth_cfg)
    assert (
        _post_prefs(c, auth_cfg, {"agent_defaults": {"default_engine": "shell"}}).status_code == 200
    )


def test_an_ABSENT_stored_default_survives_saving_bypass_and_returns_on_re_add(auth_cfg, tmp_path):
    """The regression Hermes asked for on #1128: with the stored default's engine gone, toggling
    bypass must neither be refused nor erase `default_engine`; re-adding the engine restores it."""
    fp = tmp_path / "first_party"
    shutil.copytree(FIRST_PARTY_DIR, fp)
    c = _client(auth_cfg)
    assert (
        _post_prefs(c, auth_cfg, {"agent_defaults": {"default_engine": "gemini"}}).status_code
        == 200
    )
    shutil.rmtree(fp / "gemini")
    registry.reload(first_party_dir=fp)
    try:
        assert engines.get("gemini") is None
        r = _post_prefs(c, auth_cfg, {"agent_defaults": {"bypass": False}})
        assert r.status_code == 200, r.text
        # …and re-sending the stored (absent) id with it is allowed too — a form that echoes
        # the whole block must not be refused for a choice the operator already made.
        both = {"agent_defaults": {"default_engine": "gemini", "bypass": True}}
        assert _post_prefs(c, auth_cfg, both).status_code == 200
        # A NEW unknown id is still refused.
        other = {"agent_defaults": {"default_engine": "kimi2"}}
        assert _post_prefs(c, auth_cfg, other).status_code == 422
        assert c.get("/api/config").json()["agent_defaults"]["default_engine"] == "gemini"
    finally:
        registry.reload()
    assert engines.get("gemini") is not None
    assert c.get("/api/config").json()["agent_defaults"] == {
        "default_engine": "gemini",
        "bypass": True,
    }
