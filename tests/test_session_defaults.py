"""API agents' new-session defaults (#1407): `agent_defaults.session_defaults`.

Strict on write against the client's OWN model list (the list a create checks), lenient on read,
merged per engine. The defaults only PRESELECT the new-session screen: nothing here substitutes a
model at create, which is pinned by the create tests in `test_native_models.py`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions import engines, native_models, prefs
from agent_sessions.main import create_app

API = "opencode-api"
MODELS = native_models.ModelList(
    "ok",
    (
        {
            "id": "local/fake-coder",
            "label": "Fake Coder",
            "description": None,
            "efforts": ["low", "high"],
            "is_default": False,
        },
        {
            "id": "openai/fake-gpt",
            "label": "Fake GPT",
            "description": None,
            "efforts": [],
            "is_default": False,
        },
    ),
)


class _Prov:
    """Just enough of a loaded API provider for the write check."""

    def __init__(self, engine_id: str, runtime: str):
        self.engine_id = engine_id
        self.manifest = type("M", (), {"runtime": runtime})()


@pytest.fixture
def clients(monkeypatch):
    """`opencode-api` is a loaded API client with MODELS; `codex` is a console engine."""
    provs = {API: _Prov(API, "api"), "codex": _Prov("codex", "pty")}
    monkeypatch.setattr(engines, "get", lambda eid: provs.get(eid))
    listing = {"value": MODELS}
    monkeypatch.setattr(native_models, "models", lambda prov: listing["value"])
    return listing


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


def _post(c, cfg, session_defaults):
    csrf = c.get("/api/config").json()["csrf"]
    return c.post(
        "/api/prefs",
        json={"agent_defaults": {"session_defaults": session_defaults}},
        headers={"X-CSRF-Token": csrf, "Origin": cfg.origin},
    )


def _stored(c):
    return c.get("/api/config").json()["agent_defaults"]["session_defaults"]


def test_a_listed_model_and_one_of_ITS_levels_save_and_show_in_config(auth_cfg, clients):
    c = _client(auth_cfg)
    r = _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": "high"}})
    assert r.status_code == 200, r.text
    assert _stored(c) == {API: {"model": "local/fake-coder", "effort": "high"}}


@pytest.mark.parametrize(
    "entry, needle",
    [
        ({"model": "local/not-listed", "effort": None}, "is not offered"),
        ({"model": "local/fake-coder", "effort": "max"}, "offers no effort"),
        # A model without levels takes none.
        ({"model": "openai/fake-gpt", "effort": "low"}, "offers no effort"),
        # A level belongs to a model: none for the client's own default (#1407 review 1).
        ({"model": None, "effort": "high"}, "an effort needs a model"),
        ({"model": "default", "effort": None}, "invalid model id"),
        ({"model": "local/fake-coder", "bypass": True}, "must be {model, effort}"),
    ],
)
def test_the_write_is_STRICT_and_a_refusal_stores_nothing(auth_cfg, clients, entry, needle):
    c = _client(auth_cfg)
    r = _post(c, auth_cfg, {API: entry})
    assert r.status_code == 422 and needle in r.json()["detail"], r.text
    assert _stored(c) == {}


def test_only_api_clients_take_session_defaults(auth_cfg, clients):
    c = _client(auth_cfg)
    r = _post(c, auth_cfg, {"codex": {"model": "local/fake-coder", "effort": None}})
    assert r.status_code == 422 and "is not an API client" in r.json()["detail"]


def test_an_unreadable_list_admits_only_clearing(auth_cfg, clients):
    c = _client(auth_cfg)
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}}).status_code == 200
    )
    clients["value"] = native_models.ModelList("unavailable", (), "offline")
    r = _post(c, auth_cfg, {API: {"model": "openai/fake-gpt", "effort": None}})
    assert r.status_code == 422 and "unavailable" in r.json()["detail"]
    assert _post(c, auth_cfg, {API: None}).status_code == 200
    assert _stored(c) == {}


def test_a_save_replaces_ONE_engine_and_keeps_the_others(auth_cfg, clients, monkeypatch):
    """Partial saves merge per engine (#1407 review 1); both-null or null clears that engine."""
    other = "claude-api"
    real_get = engines.get
    monkeypatch.setattr(
        engines, "get", lambda eid: _Prov(other, "api") if eid == other else real_get(eid)
    )
    c = _client(auth_cfg)
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": "low"}}).status_code == 200
    )
    assert _post(c, auth_cfg, {other: {"model": "openai/fake-gpt"}}).status_code == 200
    assert _stored(c) == {
        API: {"model": "local/fake-coder", "effort": "low"},
        other: {"model": "openai/fake-gpt", "effort": None},
    }
    assert _post(c, auth_cfg, {other: {"model": None, "effort": None}}).status_code == 200
    assert _stored(c) == {API: {"model": "local/fake-coder", "effort": "low"}}


def test_other_agent_defaults_are_untouched_by_a_session_defaults_save(auth_cfg, clients):
    c = _client(auth_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    c.post(
        "/api/prefs",
        json={"agent_defaults": {"bypass": False}},
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}}).status_code == 200
    )
    block = c.get("/api/config").json()["agent_defaults"]
    assert block["bypass"] is False  # never inherits or touches the bypass default (#1339)


def test_a_removed_client_stays_clearable(auth_cfg, clients, tmp_path, monkeypatch):
    c = _client(auth_cfg)
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}}).status_code == 200
    )
    monkeypatch.setattr(engines, "get", lambda eid: None)  # the client is gone
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}}).status_code == 422
    )
    assert _post(c, auth_cfg, {API: None}).status_code == 200
    assert _stored(c) == {}


def test_the_read_is_LENIENT_and_keeps_a_value_the_client_dropped(tmp_path):
    """A stored value the client no longer lists is still returned: the UI holds the same list
    and marks it stale rather than preselecting it. Junk is dropped; an effort never stands
    without a model."""
    p = tmp_path / "prefs.json"
    p.write_text(
        '{"agent_defaults": {"session_defaults": {'
        '"opencode-api": {"model": "gone/model", "effort": "max"},'
        '"codex-api": {"model": null, "effort": "high"},'
        '"../x": {"model": "a/b"},'
        '"claude-api": {"model": "default"},'
        '"kimi-api": "nope"}}}'
    )
    assert prefs.get_agent_defaults(p)["session_defaults"] == {
        "opencode-api": {"model": "gone/model", "effort": "max"}
    }


def test_a_save_that_would_exceed_the_cap_is_refused_not_silently_truncated(
    auth_cfg, clients, monkeypatch
):
    """#1407 PR review: the merged map is what the read caps, so the write refuses beyond it."""
    from agent_sessions import prefs as prefs_mod

    full = {
        f"api-{i}": {"model": "local/fake-coder", "effort": None}
        for i in range(prefs_mod._AGENT_MODELS_ENGINES_MAX)
    }
    monkeypatch.setattr(
        prefs_mod,
        "get_agent_defaults",
        lambda path=None: {
            "default_engine": None,
            "bypass": True,
            "models": {},
            "session_defaults": full,
        },
    )
    err = prefs_mod.validate_session_defaults_patch(
        {API: {"model": "local/fake-coder", "effort": None}}
    )
    assert err == "agent_defaults.session_defaults names too many engines"
    # Replacing or clearing an existing entry is still fine.
    assert prefs_mod.validate_session_defaults_patch({"api-0": None}) is None


def test_the_model_probe_never_runs_on_the_event_loop(auth_cfg, clients, monkeypatch):
    """#1407 PR review: `/api/prefs` validates off the loop, preflight included."""
    import asyncio

    seen: list[bool] = []

    def probe(prov):
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return MODELS

    monkeypatch.setattr(native_models, "models", probe)
    c = _client(auth_cfg)
    assert (
        _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}}).status_code == 200
    )
    assert seen and not any(seen), seen


def test_two_concurrent_saves_cannot_overfill_the_cap_under_the_lock(
    tmp_path, clients, monkeypatch
):
    """#1407 PR review 2: both writers pass validation against the SAME 31-entry snapshot; the
    lock-held merge refuses the second, so nothing is stored that the read would hide."""
    from agent_sessions import prefs as prefs_mod

    path = tmp_path / "prefs.json"
    cap = prefs_mod._AGENT_MODELS_ENGINES_MAX
    stored = {f"api-{i}": {"model": "local/fake-coder", "effort": None} for i in range(cap - 1)}
    path.write_text(json.dumps({"agent_defaults": {"session_defaults": stored}}))
    provs = {f"new-{i}": _Prov(f"new-{i}", "api") for i in range(2)}
    monkeypatch.setattr(engines, "get", lambda eid: provs.get(eid))
    a = {"session_defaults": {"new-0": {"model": "local/fake-coder", "effort": None}}}
    b = {"session_defaults": {"new-1": {"model": "local/fake-coder", "effort": None}}}
    # Both validate first (the interleaving), then both write.
    assert prefs_mod.validate_agent_defaults_patch(a, path) is None
    assert prefs_mod.validate_agent_defaults_patch(b, path) is None
    prefs_mod.set_agent_defaults(a, path)
    with pytest.raises(prefs_mod.AgentDefaultsRefused):
        prefs_mod.set_agent_defaults(b, path)
    got = prefs_mod.get_agent_defaults(path)["session_defaults"]
    assert len(got) == cap and "new-0" in got and "new-1" not in got


def test_a_lock_held_refusal_is_a_422_not_a_500(auth_cfg, clients, monkeypatch):
    from agent_sessions import prefs as prefs_mod

    def refuse(patch, path=None):
        raise prefs_mod.AgentDefaultsRefused(
            "agent_defaults.session_defaults names too many engines"
        )

    monkeypatch.setattr(prefs_mod, "set_agent_defaults", refuse)
    c = _client(auth_cfg)
    r = _post(c, auth_cfg, {API: {"model": "local/fake-coder", "effort": None}})
    assert r.status_code == 422 and "too many engines" in r.json()["detail"]


def test_session_defaults_are_saved_on_their_own_so_a_late_refusal_writes_nothing(
    auth_cfg, clients
):
    """#1407 PR review 3: the cap is re-checked under the lock, so a mixed save could commit its
    other keys before that refusal. Session defaults beside anything else are refused up front."""
    c = _client(auth_cfg)
    csrf = c.get("/api/config").json()["csrf"]
    before = c.get("/api/config").json()["theme"]
    other = "light" if before != "light" else "dark"
    for body in (
        {
            "theme": other,
            "agent_defaults": {
                "session_defaults": {API: {"model": "local/fake-coder", "effort": None}}
            },
        },
        {
            "agent_defaults": {
                "bypass": False,
                "session_defaults": {API: {"model": "local/fake-coder", "effort": None}},
            }
        },
    ):
        r = c.post(
            "/api/prefs", json=body, headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
        )
        assert r.status_code == 422 and "separately" in r.json()["detail"], r.text
    cfg = c.get("/api/config").json()
    assert cfg["theme"] == before and cfg["agent_defaults"]["session_defaults"] == {}
    assert cfg["agent_defaults"]["bypass"] is True
