"""#1311: the signed catalog can carry native API clients; nothing else can add one."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid

import pytest

import test_manifest_api
import test_plugin_feed
from agent_sessions import structured_runtime
from agent_sessions.plugins import feed, load_first_party, manager, manifest, probes

signer = test_plugin_feed.signer


def _entry(*, artifacts=()):
    return {"manifest": test_manifest_api._api(), "recipe": {"artifacts": list(artifacts)}}


def _publish(signer, *entries):
    now = int(time.time())
    data = feed.canonical(
        {
            "contract": 1,
            "sequence": 1,
            "issued_at": now,
            "expires_at": now + 3600,
            "plugins": [copy.deepcopy(e) for e in entries],
        }
    )
    return feed.accept(data, signer(data))


def test_signed_api_entry_is_accepted_without_a_recipe():
    entry = feed.entry(_entry(), signed=True)
    assert entry.manifest.runtime == "api"
    assert entry.manifest.api.source == "codex"
    assert entry.artifacts == ()
    assert entry.manifest.install is None and entry.manifest.binary is None


def test_signed_api_entry_cannot_carry_artifacts():
    artifact = test_plugin_feed.entry()["recipe"]["artifacts"][0]
    with pytest.raises(feed.FeedError, match="installs no artifacts"):
        feed.entry(_entry(artifacts=[artifact]), signed=True)


def test_local_api_entry_is_refused_even_without_artifacts():
    with pytest.raises(feed.FeedError, match="only be installed from the signed catalog"):
        feed.entry(_entry(), signed=False)


def test_signed_feed_document_with_an_api_client_is_accepted(signer):
    accepted = _publish(signer, test_plugin_feed.entry(), _entry())
    assert [e.manifest.runtime for e in accepted.entries] == ["pty", "api"]


def test_api_client_installs_verifies_by_source_and_activates(signer):
    _publish(signer, _entry())
    review = manager.review(plugin_id="native-api")
    item = manager.begin_install(str(uuid.uuid4()), review["id"], review["digest"])
    assert manager.run_install(item["id"])["state"] == "installed"
    gen = manager.snapshot()["plugins"]["native-api"]["generations"][item["id"]]
    prov = manager.provider("native-api", gen)
    # The one check is the adapter's readiness against the source; no binary/version of its own.
    assert manager.required_checks(prov) == ["source"]
    assert prov.entrypoint() is None
    with pytest.raises(manager.ManagerError):
        manager.activate(str(uuid.uuid4()), "native-api", item["id"])  # unverified
    manager.record_verification("native-api", item["id"], [{"check": "source", "passed": True}])
    manager.activate(str(uuid.uuid4()), "native-api", item["id"])
    active = manager.overlay(load_first_party()).providers["native-api"]
    assert active.manifest.runtime == "api" and active.manifest.api.kind == "codex-app-server"


@pytest.mark.parametrize("reason", [None, "codex 0.159.3 or later is required for native mode"])
def test_source_probe_reports_the_adapters_own_readiness(monkeypatch, tmp_path, reason):
    prov = test_manifest_api._provider(test_manifest_api._api(), tmp_path)
    seen = []

    def unavailable(p):
        seen.append(p)
        return reason

    monkeypatch.setattr(structured_runtime, "unavailable_reason", unavailable)
    result = asyncio.run(probes._api_source(prov))
    assert seen == [prov]
    assert result["check"] == "source"
    assert result["passed"] is (reason is None)
    if reason:
        assert result["detail"] == reason


def test_unimplemented_adapter_is_never_ready(tmp_path, monkeypatch):
    prov = test_manifest_api._provider(test_manifest_api._api(), tmp_path)
    monkeypatch.setattr(structured_runtime, "_ADAPTERS", {})
    assert structured_runtime.unavailable_reason(prov) == (
        "this native API adapter is not implemented in this build"
    )


@pytest.mark.parametrize("block", ["binary", "launch", "install", "signin", "endpoint", "models"])
def test_signed_api_entry_cannot_name_an_executable_or_permission_setup(block):
    # The signature does not widen the manifest contract: a signed API entry is refused exactly
    # like an in-tree one if it names anything to run, launch flags (incl. bypass) or an endpoint.
    value = _entry()
    value["manifest"][block] = {}
    with pytest.raises(manifest.ManifestError, match=f"{block}: is forbidden"):
        feed.entry(value, signed=True)


@pytest.mark.parametrize("ready", [True, False])
def test_an_activated_signed_client_is_offered_exactly_when_its_adapter_is_ready(
    signer, monkeypatch, auth_cfg, ready
):
    """signed entry → verification → activation → the live roster (Hermes on #1315): an enabled
    API client must not read as "not installed", and must start exactly when it can."""
    from fastapi.testclient import TestClient

    from agent_sessions import engines, native_runtime
    from agent_sessions.engines import registry
    from agent_sessions.main import create_app

    reason = None if ready else "codex 0.159.3 or later is required for native mode"
    monkeypatch.setattr(native_runtime, "readiness", lambda prov: (ready, reason))
    _publish(signer, _entry())
    review = manager.review(plugin_id="native-api")
    item = manager.begin_install(str(uuid.uuid4()), review["id"], review["digest"])
    assert manager.run_install(item["id"])["state"] == "installed"
    manager.record_verification("native-api", item["id"], [{"check": "source", "passed": True}])
    manager.activate(str(uuid.uuid4()), "native-api", item["id"])
    try:
        registry.reload()
        prov = engines.get("native-api")
        assert prov is not None and prov.manifest.runtime == "api"
        assert registry.can_start(prov) is ready
        c = TestClient(create_app(auth_cfg), base_url="https://testserver")
        c.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            follow_redirects=False,
            headers={"Origin": auth_cfg.origin},
        )
        row = next(e for e in c.get("/api/engines").json()["engines"] if e["id"] == "native-api")
        assert row["present"] is ready and row["supports_new"] is ready
        assert row["api"] == {
            "kind": "codex-app-server",
            "source": "codex",
            "unavailable_reason": reason,
            "can_bypass": True,  # codex-app-server maps skip-permissions (#1339)
        }
        assert ("native-api" in c.get("/api/config").json()["new_session_engines"]) is ready
    finally:
        registry.reload()
