"""#1728 Phase 2 — terminal automations are labelled, never cut off yet, and "Switch to API client"
is a fresh consent through the existing validated save.

* A NEW console-target automation still saves: the new-save cutoff is #1728 Phase 3.
* Switching a start_session automation to its family's API client goes through `PATCH`, the same
  validated save: the client's own model/permission settings are checked
  (`automation_runner.client_settings_state`), an ENABLED automation needs fresh consent ("another
  agent" widens), and the terminal grant is never carried over — the receipt is replaced, not
  extended.
* The API client is admitted by the REAL admission (#1901): its kind's `API_MISSION_READY` bit and
  `registry.can_start`. Claude's API client is mission-ready; Codex's is not yet (#1903), so a
  switch to it is refused with the existing unavailable reason — never a terminal fallback.
"""

from __future__ import annotations

import pytest

from agent_sessions import automation_runner, engines
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.plugins import kinds
from agent_sessions.routes import automations as routes


def _client(source: str, kind: str) -> tuple[str, str]:
    """``(console id, api id)`` of ``source``'s family, its API client named by its KIND."""
    for p in engines.all_providers():
        m = p.manifest
        if m.runtime == "api" and m.api.source == source and m.api.kind == kind:
            return m.api.source, p.engine_id
    raise AssertionError(f"no first-party {source} API client of kind {kind}")


def _pair() -> tuple[str, str]:
    """The mission-ready family, named explicitly: Claude's stream-json client (#1900)."""
    return _client("claude", "claude-stream-json")


def _not_ready_pair() -> tuple[str, str]:
    return _client("codex", "codex-app-server")


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


def _config(folder: str, engine: str, **action) -> dict:
    return {
        "name": "morning triage",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_session",
            "engine": engine,
            "folder": folder,
            "message": {"text": "triage the inbox"},
            **action,
        },
    }


def _enable(aid: str) -> dict:
    pub = routes.public(store.get(aid))
    return routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )


def _switch_body(row: dict, api: str, **over) -> dict:
    """What the SPA's Switch sends: the same action on the API client, model the client's default,
    asking before each request — the terminal grant is not carried."""
    action = {**row["config"]["action"], "engine": api, "model": None, "bypass": False, **over}
    return {"revision": row["revision"], "action": action}


def test_a_new_console_target_automation_still_saves(folder, engine_bin):
    """No cutoff in Phase 2: the REAL `engine_state` admits a present console agent at save."""
    console, _api = _pair()
    engine_bin(console)
    pub = routes._create(_config(folder, console))
    assert pub["action"]["engine"] == console and pub["state"] == "off"
    assert engines.manifest_of(console).runtime == "pty"  # it IS a console target


def test_a_switch_to_an_api_client_that_is_not_mission_ready_is_refused(folder, engine_bin):
    _codex, api = _not_ready_pair()
    assert not kinds.API_MISSION_READY[engines.manifest_of(api).api.kind]
    # Codex's console client is no unattended target either, so the row starts on Claude's.
    console, _claude_api = _pair()
    engine_bin(console)
    aid = routes._create(_config(folder, console))["id"]
    row = store.get(aid)
    ok, why = automation_runner.target_state(api)
    assert (ok, why) == (False, automation_runner.NOT_ADMITTED)
    with pytest.raises(model.AutomationError, match=why):
        routes._patch(aid, _switch_body(row, api))
    assert store.get(aid)["config"]["action"]["engine"] == console  # nothing written


def test_a_switch_waits_for_the_client_to_be_able_to_start(folder, engine_bin, monkeypatch):
    """Mission-ready is a capability bit, never readiness: a client that can't start is refused
    with its own reason."""
    from agent_sessions import mission_structured

    console, api = _pair()
    engine_bin(console)
    aid = routes._create(_config(folder, console))["id"]
    monkeypatch.setattr(mission_structured.registry, "can_start", lambda _p: False)
    ok, why = automation_runner.target_state(api)
    assert not ok and why and why != automation_runner.NOT_ADMITTED
    with pytest.raises(model.AutomationError):
        routes._patch(aid, _switch_body(store.get(aid), api))
    assert store.get(aid)["config"]["action"]["engine"] == console


@pytest.fixture
def admitted(engine_bin, monkeypatch):
    """The REAL admission for the mission-ready client: its bit is on in production; only its
    readiness (a launchable API source on this host) is supplied."""
    from agent_sessions import mission_structured

    console, api = _pair()
    engine_bin(console)
    assert kinds.API_MISSION_READY[engines.manifest_of(api).api.kind]
    monkeypatch.setattr(mission_structured.registry, "can_start", lambda _p: True)
    assert automation_runner.target_state(api) == (True, "")


def test_switching_an_enabled_automation_needs_fresh_consent_and_replaces_the_receipt(
    folder, admitted
):
    console, api = _pair()
    aid = routes._create(_config(folder, console, bypass=True))["id"]
    _enable(aid)
    row = store.get(aid)
    assert row["consented_scope"]["engine"] == console and row["consented_scope"]["bypass"]
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, _switch_body(row, api))
    assert "a different agent or model" in e.value.extra["widened"]
    assert e.value.extra["scope_digest"] and e.value.extra["scope_lines"]
    after = store.get(aid)
    assert after["revision"] == row["revision"]  # nothing written without the confirmation
    assert after["config"]["action"]["engine"] == console
    # With the consent the server asked for — the digest of the NEW scope:
    out = routes._patch(
        aid,
        {**_switch_body(row, api), "consent": True, "scope_digest": e.value.extra["scope_digest"]},
    )
    assert out["action"]["engine"] == api and out["action"]["bypass"] is False
    assert out["consented_scope"]["engine"] == api
    assert out["consented_scope"]["bypass"] is False  # the terminal grant was not carried over


def test_a_stale_digest_is_not_a_confirmation(folder, admitted):
    console, api = _pair()
    aid = routes._create(_config(folder, console))["id"]
    _enable(aid)
    row = store.get(aid)
    with pytest.raises(model.AutomationError) as e:
        routes._patch(aid, {**_switch_body(row, api), "consent": True, "scope_digest": "0" * 32})
    assert e.value.status == 409
    assert store.get(aid)["config"]["action"]["engine"] == console


def test_switch_validates_the_api_clients_model_and_permission_settings(folder, admitted):
    console, api = _pair()
    aid = routes._create(_config(folder, console))["id"]
    row = store.get(aid)
    # A model: an automation's consent covers none yet (#1189) — refused, nothing written.
    with pytest.raises(model.AutomationError, match="model"):
        routes._patch(aid, _switch_body(row, api, model="some-model"))
    # Skip permission prompts on ANY API client (#1901): its guarded first turn never runs in a
    # session that skips them, even where the adapter maps the setting (`API_BYPASS_KINDS`).
    assert engines.manifest_of(api).api.kind in kinds.API_BYPASS_KINDS
    with pytest.raises(model.AutomationError, match="can't skip permission prompts"):
        routes._patch(aid, _switch_body(row, api, bypass=True))
    assert store.get(aid)["config"]["action"]["engine"] == console
    # Asking before each request on the client's default model: saved (an OFF automation needs
    # no consent to save — enabling it asks for consent to the full scope).
    out = routes._patch(aid, _switch_body(row, api))
    assert out["action"]["engine"] == api and out["enabled"] is False


def test_client_settings_are_checked_at_enable_too(folder, admitted):
    """A row stored with bypass on an API client (a build before #1901 admitted it for some kinds)
    is refused at enable, by the same check."""
    console, api = _pair()
    aid = routes._create(_config(folder, api))["id"]
    stored = store.get(aid)["config"]
    store.mutate(
        aid, lambda _r: {"config": {**stored, "action": {**stored["action"], "bypass": True}}}
    )
    pub = routes.public(store.get(aid))
    with pytest.raises(model.AutomationError, match="can't skip permission prompts"):
        routes._enable(
            aid,
            {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]},
        )
    # The console target keeps its consented bypass: this check is the API client's only.
    assert automation_runner.client_settings_state(
        {"engine": console, "bypass": True, "model": None}
    ) == (True, "")
