"""Public installer boundaries and the real, capture-free sign-in socket (#1259)."""

import asyncio
import hashlib
import json
import subprocess
import sys
import tarfile
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import test_plugin_artifacts
import test_plugin_manager
import test_plugin_process
from agent_sessions import auth
from agent_sessions.plugins import manager, process, storage
from agent_sessions.routes import chat, plugins
from test_plugin_manager import installed, request_id

recipe = test_plugin_manager.recipe
user_manager = test_plugin_process.user_manager


@pytest.fixture
def api(auth_cfg):
    app = FastAPI()
    forced = {"v": False}
    plugins.register(
        app,
        logged_in=auth.require_session(auth_cfg),
        csrf_guard=auth.require_csrf_and_origin(auth_cfg),
        cfg=auth_cfg,
        must_change=forced,
    )
    chat.register(
        app,
        logged_in=auth.require_session(auth_cfg),
        csrf_guard=auth.require_csrf_and_origin(auth_cfg),
    )
    client = TestClient(app, base_url=auth_cfg.origin)
    token = auth._serializer(auth_cfg).dumps({"uid": auth_cfg.username, "csrf": "fixture-csrf"})
    headers = {
        "cookie": "agent_sessions=" + token,
        "origin": auth_cfg.origin,
        "X-CSRF-Token": "fixture-csrf",
    }
    yield client, headers, forced
    asyncio.run(app.state.plugin_jobs.shutdown())


MUTATIONS = [
    ("POST", "/api/plugins/" + name)
    for name in (
        "feed/refresh",
        "review",
        "install",
        "recover",
        "signin",
        "verify",
        "activate",
        "disable",
        "remove",
        "reload",
        "operations/none/cancel",
    )
] + [
    ("PATCH", "/api/plugins/fixture/generations/none/endpoint"),
    ("POST", "/api/agents/catalog/refresh"),
    ("PATCH", "/api/agents/catalog/preferences"),
]


@pytest.mark.parametrize("method,path", MUTATIONS)
def test_every_mutation_needs_cookie_csrf_and_same_origin(api, method, path):
    client, headers, _ = api
    assert client.request(method, path, json={}).status_code == 401
    for omitted in ("X-CSRF-Token", "origin"):
        bad = {k: v for k, v in headers.items() if k != omitted}
        assert client.request(method, path, json={}, headers=bad).status_code == 403
    assert (
        client.request(
            method, path, json={}, headers={**headers, "origin": "https://evil.test"}
        ).status_code
        == 403
    )


def test_routes_reject_commands_results_duplicates_and_oversized_body(api, monkeypatch):
    client, headers, _ = api
    for path, body in (
        ("review", {"command": "anything"}),
        (
            "verify",
            {
                "request_id": request_id(),
                "plugin_id": "fixture",
                "generation_id": request_id(),
                "results": [{"passed": True}],
            },
        ),
    ):
        assert client.post("/api/plugins/" + path, json=body, headers=headers).status_code == 422
    assert (
        client.post(
            "/api/plugins/review", content='{"local":{},"local":{}}', headers=headers
        ).status_code
        == 422
    )
    monkeypatch.setattr(plugins, "MAX_BODY", 64)
    assert client.post("/api/plugins/review", content="x" * 65, headers=headers).status_code == 413


def test_install_lost_response_reload_and_same_id_never_download_twice(api, recipe, monkeypatch):
    client, headers, _ = api
    calls = []
    fetch = manager.artifacts.fetch
    monkeypatch.setattr(
        manager.artifacts, "fetch", lambda *args: (calls.append(args), fetch(*args))[1]
    )
    review = client.post("/api/plugins/review", json={"local": recipe}, headers=headers).json()
    body = {"request_id": request_id(), "review_id": review["id"], "digest": review["digest"]}
    assert client.post("/api/plugins/install", json=body, headers=headers).status_code == 409
    body["confirm_local"] = True
    first = client.post("/api/plugins/install", json=body, headers=headers)
    assert first.status_code == 202, first.text
    deadline = time.monotonic() + 30
    while True:
        op = client.get("/api/plugins/operations/" + body["request_id"], headers=headers).json()
        if op["state"] not in ("planned", "running"):
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert op["state"] == "installed", op
    assert client.post("/api/plugins/install", json=body, headers=headers).json() == op
    assert len(calls) == 1
    listed = client.get("/api/plugins", headers=headers).json()
    row = next(p for p in listed["plugins"] if p["id"] == "fixture")
    assert row["enabled"] is not True
    assert row["generations"][0]["review"]["source"] == "local"
    enable = {
        "request_id": request_id(),
        "plugin_id": "fixture",
        "generation_id": op["generation_id"],
    }
    assert client.post("/api/plugins/activate", json=enable, headers=headers).status_code == 409


@pytest.fixture
def signin_candidate(recipe, monkeypatch):
    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    script = (
        f"#!{sys.executable}\nimport os,sys,time\nprint('READY '+os.getcwd(),flush=True)\n"
        "data=sys.stdin.readline()\nprint('GOT:'+data.strip(),flush=True)\n"
        "time.sleep(30)\n"
    ).encode()
    data = test_plugin_artifacts.archive([("bin/fixture", script, tarfile.REGTYPE)])
    digest = hashlib.sha256(data).hexdigest()
    recipe["manifest"]["install"]["digest"] = "sha256:" + digest
    recipe["recipe"]["artifacts"][0]["sha256"] = digest
    monkeypatch.setattr(manager.artifacts, "fetch", lambda *_: data)
    return installed(recipe)


def test_signin_auth_single_owner_disconnect_and_secret_exclusion(
    api, signin_candidate, user_manager, caplog, monkeypatch
):
    client, headers, forced = api
    rid = request_id()
    item = client.post(
        "/api/plugins/signin",
        headers=headers,
        json={"request_id": rid, "plugin_id": "fixture", "generation_id": signin_candidate},
    )
    assert item.status_code == 200, item.text
    url = "/ws/plugins/signin/" + rid
    for bad in ({}, {**headers, "origin": "https://evil.test"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=bad):
                pytest.fail("unauthenticated sign-in accepted")
    forced["v"] = True
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers=headers):
            pytest.fail("password-change gate bypassed")
    forced["v"] = False
    secret = b"secret-shaped-853-route-login"
    with client.websocket_connect(url, headers=headers) as ws:
        output = b""
        while b"\n" not in output:
            output += ws.receive_bytes()
        assert b"READY " + str(manager.workspace("fixture", signin_candidate)).encode() in output
        # No second socket can take ownership or send input.
        with client.websocket_connect(url, headers=headers) as second:
            with pytest.raises(WebSocketDisconnect):
                second.receive_bytes()
        ws.send_json({"rows": 24, "cols": 80})
        ws.send_bytes(secret + b"\n")
        while b"GOT:" + secret not in output:
            output += ws.receive_bytes()
        with pytest.raises(storage.StateError):
            manager.recover()
    assert manager.operation(rid)["state"] == "interrupted"
    with client.websocket_connect(url, headers=headers) as again:
        with pytest.raises(WebSocketDisconnect):
            again.receive_bytes()
    state = subprocess.run(
        ["/usr/bin/systemctl", "--user", "is-active", process.unit_name(rid)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert state.stdout.strip() in (b"inactive", b"failed", b"unknown")
    assert secret.decode() not in caplog.text
    # Includes manager checkpoint, lock files and sign-in workspace; there is no capture file.
    for path in storage.root().rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), path
    assert secret.decode() not in json.dumps(client.get("/api/plugins", headers=headers).json())


def test_cancel_ready_and_recovery_never_claim_success(api, signin_candidate, monkeypatch):
    client, headers, _ = api
    rid = request_id()
    args = {"request_id": rid, "plugin_id": "fixture", "generation_id": signin_candidate}
    assert client.post("/api/plugins/signin", json=args, headers=headers).json()["state"] == "ready"
    assert (
        client.post(f"/api/plugins/operations/{rid}/cancel", json={}, headers=headers).json()[
            "state"
        ]
        == "interrupted"
    )
    assert (
        client.post("/api/plugins/signin", json=args, headers=headers).json()["state"]
        == "interrupted"
    )
    args["request_id"] = request_id()
    client.post("/api/plugins/signin", json=args, headers=headers)
    monkeypatch.setattr(process, "stop_operation", lambda _: False)
    assert client.post("/api/plugins/recover", json={}, headers=headers).status_code == 409
    assert manager.operation(args["request_id"])["state"] == "ready"
    monkeypatch.setattr(process, "stop_operation", lambda _: True)
    assert client.post("/api/plugins/recover", json={}, headers=headers).status_code == 200
    assert manager.operation(args["request_id"])["state"] == "interrupted"


def test_candidate_endpoint_never_returns_key_or_sends_it_to_a_changed_origin(
    api, recipe, monkeypatch
):
    import httpx

    import test_plugins
    from agent_sessions import review

    client, headers, _ = api
    doc = test_plugins.doc("apichat")
    doc["identity"]["id"] = "fixture-api"
    gen = installed({"manifest": doc, "recipe": {"artifacts": []}})
    base = f"/api/plugins/fixture-api/generations/{gen}/endpoint"
    secret = "fixture-plugin-origin-secret"
    saved = client.patch(
        base,
        headers=headers,
        json={"base_url": "https://one.test/v1", "api_key": secret, "model": "m"},
    )
    assert saved.status_code == 200, saved.text
    assert secret not in saved.text and "envelope" not in saved.text
    assert secret not in client.get(base, headers=headers).text
    assert secret not in client.get("/api/plugins", headers=headers).text
    assert secret.encode() not in (storage.root() / manager.DOCUMENT).read_bytes()
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(lambda r: calls.append(r)))
    assert (
        client.patch(
            base, headers=headers, json={"base_url": "https://elsewhere.test/v1"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            base + "/test", headers=headers, json={"base_url": "https://elsewhere.test/v1"}
        ).status_code
        == 422
    )
    assert not calls
    assert client.post(base + "/test", json={}).status_code == 401
    assert (
        client.post(base + "/test", json={}, headers={"cookie": headers["cookie"]}).status_code
        == 403
    )
    assert manager.snapshot()["plugins"]["fixture-api"]["enabled"] is not True


def test_worker_error_after_visible_install_never_relabels_it_failed(api, recipe, monkeypatch):
    client, headers, _ = api
    original = manager._run_install

    def late_error(rid):
        original(rid)
        raise OSError("publication response failed")

    monkeypatch.setattr(manager, "_run_install", late_error)
    reviewed = client.post("/api/plugins/review", headers=headers, json={"local": recipe}).json()
    rid = request_id()
    response = client.post(
        "/api/plugins/install",
        headers=headers,
        json={
            "request_id": rid,
            "review_id": reviewed["id"],
            "digest": reviewed["digest"],
            "confirm_local": True,
        },
    )
    assert response.status_code == 202
    # The service releases its busy flag only after its exception handler finishes.
    service = client.app.state.plugin_jobs
    service._thread.join(timeout=30)
    assert not service._thread.is_alive()
    state = client.get(f"/api/plugins/operations/{rid}", headers=headers).json()
    assert state["state"] == "installed" and state["error"] is None


@pytest.mark.parametrize("kind", ["disable", "remove"])
@pytest.mark.parametrize("committed_before_replacement", [False, True])
@pytest.mark.parametrize("same_generation", [False, True])
def test_deactivation_cannot_revoke_an_intervening_activation(
    api, recipe, kind, committed_before_replacement, same_generation
):
    from test_plugin_manager import verified

    client, headers, _ = api
    first = verified(recipe)
    manager.activate(request_id(), "fixture", first)
    revision = client.get("/api/plugins", headers=headers).json()["roster_revision"]
    body = {
        "request_id": request_id(),
        "plugin_id": "fixture",
        "expected_active": first,
        "expected_revision": revision,
    }
    url = f"/api/plugins/{kind}"
    if committed_before_replacement:
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
    else:
        assert (
            client.get(f"/api/plugins/operations/{body['request_id']}", headers=headers).status_code
            == 409
        )
    if same_generation:
        manager.deactivate(request_id(), "fixture")
    second = first if same_generation else verified(recipe)
    manager.activate(request_id(), "fixture", second)
    before = manager.snapshot()
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == (200 if committed_before_replacement else 409), response.text
    assert manager.snapshot() == before
    assert before["plugins"]["fixture"]["active"] == second
    assert before["plugins"]["fixture"]["enabled"] is True
    # A fresh confirmation targets the new installation with a new operation id.
    revision = client.get("/api/plugins", headers=headers).json()["roster_revision"]
    assert revision == before["roster_revision"] != body["expected_revision"]
    fresh = {
        **body,
        "request_id": request_id(),
        "expected_active": second,
        "expected_revision": revision,
    }
    assert client.post(url, headers=headers, json=fresh).status_code == 200
    assert manager.snapshot()["plugins"]["fixture"]["enabled"] is False


@pytest.mark.parametrize("kind", ["disable", "remove"])
def test_deactivation_requires_and_binds_the_observed_roster_revision(api, kind):
    client, headers, _ = api
    assert client.get("/api/plugins", headers=headers).json()["roster_revision"] is None
    body = {"request_id": request_id(), "plugin_id": "fixture", "expected_active": None}
    url = f"/api/plugins/{kind}"
    assert client.post(url, headers=headers, json=body).status_code == 422
    body["expected_revision"] = None
    assert client.post(url, headers=headers, json=body).status_code == 200
    before = manager.snapshot()
    # Changing the precondition on a recorded ID is a different request, never a replay.
    body["expected_revision"] = before["roster_revision"]
    assert client.post(url, headers=headers, json=body).status_code == 409
    assert manager.snapshot() == before


@pytest.mark.parametrize("old_tools", ["none", "read", "write"])
@pytest.mark.parametrize("new_tools", ["none", "read", "write"])
def test_candidate_endpoint_isolated_until_verified_activation(api, recipe, old_tools, new_tools):
    import test_plugins
    from agent_sessions import chat_config
    from agent_sessions.engines import registry

    client, headers, _ = api
    doc = test_plugins.doc("apichat")
    doc["identity"]["id"] = "fixture-api"
    entry = {"manifest": doc, "recipe": {"artifacts": []}}
    first = installed(entry)

    def base(gen):
        return f"/api/plugins/fixture-api/generations/{gen}/endpoint"

    old = {
        "base_url": "https://old.test/v1",
        "api_key": "old-private-key",
        "model": "old",
        "tools": old_tools,
    }
    new = {
        "base_url": "https://new.test/v1",
        "api_key": "new-private-key",
        "model": "new",
        "tools": new_tools,
    }
    assert client.patch(base(first), headers=headers, json=old).status_code == 200
    manager.record_verification(
        "fixture-api",
        first,
        [
            {
                "check": "endpoint",
                "passed": True,
                "binding": manager._endpoint_binding("fixture-api", first),
            }
        ],
    )
    manager.activate(request_id(), "fixture-api", first)
    second = installed(entry)
    assert client.patch(base(second), headers=headers, json=new).status_code == 200
    assert chat_config.snapshot("fixture-api")["base_url"] == old["base_url"]
    assert chat_config.snapshot("fixture-api")["api_key"] == old["api_key"]
    assert client.get(base(first), headers=headers).json()["model"] == "old"
    assert client.get(base(second), headers=headers).json()["model"] == "new"
    assert client.patch(base(first), headers=headers, json=new).status_code == 409
    manager.record_verification(
        "fixture-api",
        second,
        [
            {
                "check": "endpoint",
                "passed": True,
                "binding": manager._endpoint_binding("fixture-api", second),
            }
        ],
    )
    with registry.snapshot_scope():
        assert chat_config.tools_enabled("fixture-api") == (old_tools != "none")
        assert chat_config.edits_enabled("fixture-api") == (old_tools == "write")
        manager.activate(request_id(), "fixture-api", second)
        before = manager.snapshot()
        assert client.patch(base(first), headers=headers, json=new).status_code == 409
        assert manager.snapshot() == before
        # A captured turn retains its endpoint and cannot retain revoked permissions or
        # gain newly granted permissions from the replacement generation.
        assert chat_config.tools_enabled("fixture-api") == (
            old_tools != "none" and new_tools != "none"
        )
        assert chat_config.edits_enabled("fixture-api") == (old_tools == new_tools == "write")
        assert chat_config.snapshot("fixture-api")["base_url"] == old["base_url"]
    assert chat_config.snapshot("fixture-api")["base_url"] == new["base_url"]
    assert chat_config.snapshot("fixture-api")["api_key"] == new["api_key"]
    # The ordinary editor cannot bypass the generation-specific verification boundary.
    before = manager.snapshot()
    assert (
        client.patch(
            "/api/agents/fixture-api/endpoint", headers=headers, json={"model": "edited-active"}
        ).status_code
        == 422
    )
    assert manager.snapshot() == before
    assert client.get(base(second), headers=headers).json()["model"] == "new"
    assert client.get(base(first), headers=headers).json()["model"] == "old"
    manager.deactivate(request_id(), "fixture-api")
    for gen in (first, second):
        assert client.patch(base(gen), headers=headers, json=new).status_code == 409


@pytest.mark.parametrize("kind", ["signin", "verify"])
def test_active_generation_cannot_invalidate_its_verification(api, recipe, kind):
    client, headers, _ = api
    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    gen = test_plugin_manager.verified(recipe)
    manager.activate(request_id(), "fixture", gen)
    before = manager.snapshot()
    body = {"request_id": request_id(), "plugin_id": "fixture", "generation_id": gen}
    if kind == "verify":
        body["confirm_effects"] = True
    response = client.post(f"/api/plugins/{kind}", headers=headers, json=body)
    assert response.status_code == 409
    assert "disable" in response.json()["detail"]
    assert manager.snapshot() == before
    # Withdrawal makes a recheck explicit and keeps the old generation available for rollback.
    manager.deactivate(request_id(), "fixture")
    result = manager.begin_action(**{k: v for k, v in body.items()}, kind=kind)
    assert result["state"] == ("ready" if kind == "signin" else "planned")


def test_review_forbidden_binary_is_a_controlled_refusal(api, recipe):
    client, headers, _ = api
    recipe["manifest"]["binary"]["aliases"] = ["bash"]
    response = client.post("/api/plugins/review", headers=headers, json={"local": recipe})
    assert response.status_code == 422
    assert "provenance" in response.json()["detail"]
    assert manager.snapshot()["reviews"] == {}


def test_candidate_endpoint_save_invalidates_previous_verification(api, recipe):
    import test_plugins

    client, headers, _ = api
    doc = test_plugins.doc("apichat")
    doc["identity"]["id"] = "fixture-api"
    gen = installed({"manifest": doc, "recipe": {"artifacts": []}})
    base = f"/api/plugins/fixture-api/generations/{gen}/endpoint"
    assert (
        client.patch(
            base,
            headers=headers,
            json={"base_url": "https://old.test/v1", "api_key": "private-key", "model": "old"},
        ).status_code
        == 200
    )
    manager.record_verification(
        "fixture-api",
        gen,
        [
            {
                "check": "endpoint",
                "passed": True,
                "binding": manager._endpoint_binding("fixture-api", gen),
            }
        ],
    )
    assert client.patch(base, headers=headers, json={"model": "changed"}).status_code == 200
    assert manager.generation("fixture-api", gen)["verification"] is None
    catalog = client.get("/api/plugins", headers=headers).json()
    assert catalog["plugins"][0]["generations"][0]["verification"] is None
    with pytest.raises(manager.ManagerError, match="verification"):
        manager.activate(request_id(), "fixture-api", gen)


@pytest.mark.parametrize("kind", ["install", "signin", "verify", "activate", "disable", "remove"])
def test_abandoned_signin_expiry_admits_fresh_operations(api, recipe, monkeypatch, kind):
    from agent_sessions.plugins import probes

    client, headers, _ = api
    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    gen = test_plugin_manager.verified(recipe)
    abandoned = {"request_id": request_id(), "plugin_id": "fixture", "generation_id": gen}
    old = client.post("/api/plugins/signin", json=abandoned, headers=headers)
    assert old.status_code == 200 and old.json()["state"] == "ready"
    cutoff = old.json()["created_at"] + manager.REVIEW_SECONDS
    body = {"request_id": request_id(), "plugin_id": "fixture", "generation_id": gen}
    if kind in ("disable", "remove"):
        body = {
            "request_id": request_id(),
            "plugin_id": "fixture",
            "expected_active": None,
            "expected_revision": None,
        }
    elif kind == "verify":
        body["confirm_effects"] = True

        async def verified(item):
            candidate = manager.generation("fixture", gen)
            prov = manager.provider("fixture", candidate)
            manager.finish_verification(
                item["id"], [{"check": c, "passed": True} for c in manager.required_checks(prov)]
            )

        monkeypatch.setattr(probes, "run", verified)
    # Before expiry the abandoned, still claimable POST reserves the lane.
    if kind != "install":
        refused = client.post(f"/api/plugins/{kind}", json=body, headers=headers)
        assert refused.status_code == 409 and "busy" in refused.json()["detail"]
    monkeypatch.setattr(manager, "_now", lambda: cutoff)
    if kind == "install":
        review = client.post("/api/plugins/review", json={"local": recipe}, headers=headers).json()
        body = {
            "request_id": request_id(),
            "review_id": review["id"],
            "digest": review["digest"],
            "confirm_local": True,
        }
    response = client.post(f"/api/plugins/{kind}", json=body, headers=headers)
    assert response.status_code == (202 if kind in ("install", "verify") else 200), response.text
    expired = client.get(
        f"/api/plugins/operations/{abandoned['request_id']}", headers=headers
    ).json()
    assert expired["state"] == "interrupted" and expired["error"] == "sign-in expired"
    assert expired["updated_at"] == cutoff
    # A late socket cannot claim this old request after a new operation was admitted.
    with pytest.raises(manager.ManagerError, match="not ready"):
        with storage.locked("worker", wait=30):
            manager.claim_signin(abandoned["request_id"])


@pytest.mark.parametrize("state", ["running", "planned", "cleanup_pending"])
def test_signin_expiry_never_releases_running_or_uncertain_work(api, recipe, monkeypatch, state):
    client, headers, _ = api
    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    gen = installed(recipe)
    body = {"request_id": request_id(), "plugin_id": "fixture", "generation_id": gen}
    old = client.post("/api/plugins/signin", json=body, headers=headers).json()
    manager._set_operation(body["request_id"], state)
    monkeypatch.setattr(manager, "_now", lambda: old["created_at"] + manager.REVIEW_SECONDS)
    before = manager.snapshot()
    with pytest.raises(manager.ManagerError, match="busy"):
        manager.deactivate(request_id(), "fixture")
    assert manager.snapshot() == before


def test_signin_expiration_waits_for_exclusive_worker_admission(api, recipe, monkeypatch):
    client, headers, _ = api
    recipe["manifest"]["signin"] = {"kind": "cli-subcommand", "subcommand": "login"}
    gen = installed(recipe)
    body = {"request_id": request_id(), "plugin_id": "fixture", "generation_id": gen}
    old = client.post("/api/plugins/signin", json=body, headers=headers).json()
    monkeypatch.setattr(manager, "_now", lambda: old["created_at"] + manager.REVIEW_SECONDS)
    fresh = {
        "request_id": request_id(),
        "plugin_id": "fixture",
        "expected_active": None,
        "expected_revision": None,
    }
    before = manager.snapshot()
    with storage.locked("worker", wait=0):
        response = client.post("/api/plugins/disable", json=fresh, headers=headers)
        assert response.status_code == 409
        assert manager.snapshot() == before
    assert client.post("/api/plugins/disable", json=fresh, headers=headers).status_code == 200
    assert manager.operation(body["request_id"])["state"] == "interrupted"


def test_public_agent_catalog_api_names_and_preferences(api):
    client, headers, _ = api
    assert client.get("/api/agents/catalog").status_code == 401
    response = client.get("/api/agents/catalog", headers=headers)
    assert response.status_code == 200
    value = response.json()
    assert value["feed"]["source"] == "bundled" and len(value["catalog"]) >= 9
    assert all(c["reason"] and not c["installable"] for c in value["catalog"] if c["included"])
    for bad in ("true", 1, None):
        assert (
            client.patch(
                "/api/agents/catalog/preferences", json={"automatic": bad}, headers=headers
            ).status_code
            == 422
        )
    changed = client.patch(
        "/api/agents/catalog/preferences", json={"automatic": False}, headers=headers
    )
    assert changed.status_code == 200 and changed.json()["feed"]["refresh"]["automatic"] is False
    assert (
        client.get("/api/plugins", headers=headers).json()["feed"]["refresh"]["automatic"] is False
    )
