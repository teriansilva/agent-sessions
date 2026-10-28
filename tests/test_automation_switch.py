"""#1728 Phase 2 — terminal automations are labelled, never cut off yet, and "Switch to API client"
is a fresh consent through the existing validated save.

* A NEW console-target automation still saves: the new-save cutoff waits for Phase 3 (#1273 P3).
* Switching a start_session automation to its family's API client goes through `PATCH`, the same
  validated save: the client's own model/permission settings are checked
  (`automation_runner.client_settings_state`), an ENABLED automation needs fresh consent ("another
  agent" widens), and the terminal grant is never carried over — the receipt is replaced, not
  extended.
* Until the API client is admitted for unattended use, the switch is refused with the existing
  unavailable reason — never a terminal fallback.
"""

from __future__ import annotations

import pytest

from agent_sessions import automation_runner, engines
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.plugins import kinds
from agent_sessions.routes import automations as routes


def _pair() -> tuple[str, str]:
    """``(console id, api id)`` of a family with both clients, asked of the manifests."""
    for p in engines.all_providers():
        if p.manifest.runtime == "api":
            return p.manifest.api.source, p.engine_id
    raise AssertionError("no first-party family has an API client")


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


def test_a_switch_is_refused_with_the_unavailable_reason_until_the_api_client_is_admitted(
    folder, engine_bin
):
    console, api = _pair()
    engine_bin(console)
    aid = routes._create(_config(folder, console))["id"]
    row = store.get(aid)
    ok, why = automation_runner.engine_state(api)
    assert not ok and why  # today: no API client runs unattended (#1273 P3)
    with pytest.raises(model.AutomationError, match=why[:30]):
        routes._patch(aid, _switch_body(row, api))
    assert store.get(aid)["config"]["action"]["engine"] == console  # nothing written


@pytest.fixture
def admitted(monkeypatch):
    """Simulate the API client being admitted for unattended use (what #1273 P3 will provide)."""
    monkeypatch.setattr(automation_runner, "engine_state", lambda engine: (True, ""))


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


def test_switch_validates_the_api_clients_model_and_permission_settings(
    folder, admitted, monkeypatch
):
    console, api = _pair()
    aid = routes._create(_config(folder, console))["id"]
    row = store.get(aid)
    # A model: an automation's consent covers none yet (#1189) — refused, nothing written.
    with pytest.raises(model.AutomationError, match="model"):
        routes._patch(aid, _switch_body(row, api, model="some-model"))
    # Skip permission prompts on a client whose adapter cannot honour it: refused by name.
    monkeypatch.setattr(kinds, "API_BYPASS_KINDS", frozenset())
    with pytest.raises(model.AutomationError, match="can't skip permission prompts"):
        routes._patch(aid, _switch_body(row, api, bypass=True))
    assert store.get(aid)["config"]["action"]["engine"] == console
    # Asking before each request on the client's default model: saved (an OFF automation needs
    # no consent to save — enabling it asks for consent to the full scope).
    out = routes._patch(aid, _switch_body(row, api))
    assert out["action"]["engine"] == api and out["enabled"] is False


def test_client_settings_are_checked_at_enable_too(folder, admitted, monkeypatch):
    console, api = _pair()
    aid = routes._create(_config(folder, api, bypass=True))["id"]
    monkeypatch.setattr(kinds, "API_BYPASS_KINDS", frozenset())
    pub = routes.public(store.get(aid))
    with pytest.raises(model.AutomationError, match="can't skip permission prompts"):
        routes._enable(
            aid,
            {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]},
        )
    assert console  # the console target is untouched by this check:
    assert automation_runner.client_settings_state(
        {"engine": console, "bypass": True, "model": None}
    ) == (True, "")
