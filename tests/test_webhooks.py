"""Webhooks as a list (#2221 P1): the store, the migration of #2202's review-loop webhooks, the
recorded shape (names and types, never values), the signature forms and the API.

Delivery routing (`/hooks/w`, `/hooks/a`) is covered in `tests/test_forge_review_loop.py`.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import template_secrets, webhooks
from agent_sessions.routes import webhooks as routes

# ---- the shape of a delivery --------------------------------------------------------------------


def test_the_shape_has_names_and_types_never_values():
    payload = {
        "action": "opened",
        "pull_request": {"number": 41, "draft": False, "title": "Leaky title", "head": None},
        "labels": [{"name": "secret-label"}],
        "": "skipped",
    }
    shape = {f["path"]: f["type"] for f in webhooks.shape_of(payload)}
    assert shape == {
        "action": "text",
        "pull_request.number": "number",
        "pull_request.draft": "yes/no",
        "pull_request.title": "text",
        "pull_request.head": "empty",
        "labels[].name": "text",
    }
    assert "Leaky" not in json.dumps(webhooks.shape_of(payload))
    assert "secret-label" not in json.dumps(webhooks.shape_of(payload))


def test_the_shape_is_bounded():
    wide = {f"k{i}": i for i in range(1000)}
    assert len(webhooks.shape_of(wide)) == webhooks.SHAPE_MAX_PATHS
    deep: dict = {}
    cur = deep
    for _ in range(20):
        cur["x"] = {}
        cur = cur["x"]
    assert all(f["path"].count(".") < webhooks.SHAPE_MAX_DEPTH for f in webhooks.shape_of(deep))


@pytest.mark.parametrize(
    "headers, want",
    [
        ({"x-forgejo-signature": "ab12"}, "ab12"),
        ({"x-gitea-signature": " ab12 "}, "ab12"),
        ({"x-hub-signature-256": "sha256=ab12"}, "ab12"),
        ({"x-hub-signature-256": "sha1=ab12"}, ""),  # not HMAC-SHA256: never accepted
        ({"authorization": "ab12"}, None),
        ({}, None),
    ],
)
def test_signature_header_forms(headers, want):
    assert webhooks.signature_header(headers) == want


# ---- the store ----------------------------------------------------------------------------------


def test_create_rename_rotate_delete():
    pub, secret = webhooks.create("  shop repo  ")
    assert pub["name"] == "shop repo" and pub["path"] == f"/hooks/w/{pub['id']}"
    assert pub["pickable"] and pub["secret_set"] and secret not in json.dumps(pub)
    renamed = webhooks.rename(pub["id"], "shop", expect_revision=pub["revision"])
    assert renamed["name"] == "shop"
    with pytest.raises(store.Conflict):
        webhooks.rename(pub["id"], "x", expect_revision=pub["revision"])
    new = webhooks.rotate(pub["id"])
    assert set(webhooks.secrets(pub["id"])) == {secret, new}
    webhooks.delete(pub["id"])
    assert webhooks.list_all() == []


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "bad\nname", 5])
def test_names_are_checked(name):
    with pytest.raises(webhooks.WebhookError):
        webhooks.create(name)


def test_a_review_loops_webhook_is_adopted_unchanged(monkeypatch, tmp_path):
    """#2202's per-automation webhook moves into the list as it was: same id, the same envelope
    (still decrypting under its original AAD), the same counters — and a re-run changes nothing."""
    monkeypatch.setattr(
        model, "resolve_mission_cwd", lambda _p: model.folder_identity(str(tmp_path))
    )
    monkeypatch.setattr(
        model,
        "specialist_pin",
        lambda r: {"id": r["specialist_id"], "version": r["version"], "engine": "x-api"},
    )
    cfg = model.validate_config(
        {
            "name": "old loop",
            "trigger": {"kind": "webhook", "scheme": "forgejo"},
            "action": {
                "kind": "forge_review_loop",
                "project_id": "shop",
                "repo": "example-org/shop",
                "reviewer": {"specialist_id": "rev", "version": 1},
                "reviewer_login": "bot",
            },
        }
    )
    aid = store.create(cfg, model.compute_pins(cfg))["id"]
    env = template_secrets.encrypt(f"automation-webhook:{aid}:1", "old-secret-value")
    with store._tx() as con:
        con.execute(
            "INSERT INTO automation_hook_secrets (automation_id, gen, envelope, created_at, "
            "retire_at) VALUES (?,?,?,?,NULL)",
            (aid, 1, json.dumps(env), time.time()),
        )
        con.execute(
            "INSERT INTO automation_hook_stats (automation_id, last_at, last_event, last_note, "
            "unsigned_count, unsigned_since) VALUES (?,?,?,?,?,?)",
            (aid, 123.0, "pull_request", "received", 2, time.time()),
        )
        store._adopt_legacy_webhooks(con)
        store._adopt_legacy_webhooks(con)  # idempotent
    assert webhooks.secrets(aid) == ["old-secret-value"]
    pub = webhooks.get(aid)
    assert pub["legacy_automation"] == aid and not pub["pickable"]
    assert pub["path"] == f"/hooks/a/{aid}" and pub["unsigned_24h"] == 2
    assert pub["last_delivery_at"] == 123.0
    assert webhooks.consumers(aid, include_disabled=True) == [aid]
    with pytest.raises(webhooks.WebhookError, match="belongs to an automation"):
        webhooks.delete(aid)
    with store._read() as con:
        assert con.execute("SELECT COUNT(*) FROM webhooks WHERE id=?", (aid,)).fetchone()[0] == 1


def test_a_fresh_store_has_the_webhook_tables():
    webhooks.list_all()
    with store._read() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"webhooks", "webhook_secrets"} <= names


# ---- the API ------------------------------------------------------------------------------------


@pytest.fixture
def api():
    app = FastAPI()
    routes.register(app, logged_in=lambda: "operator", csrf_guard=lambda: None)
    with TestClient(app) as c:
        yield c


def test_the_api_shows_the_secret_once(api):
    r = api.post("/api/webhooks", json={"name": "deploy notices"})
    assert r.status_code == 201
    made = r.json()
    wid, secret = made["webhook"]["id"], made["secret"]
    assert len(secret) == 64
    listed = api.get("/api/webhooks").json()
    assert [w["id"] for w in listed["webhooks"]] == [wid]
    assert secret not in json.dumps(listed) and secret not in api.get(f"/api/webhooks/{wid}").text
    rotated = api.post(f"/api/webhooks/{wid}/rotate").json()
    assert rotated["secret"] != secret and rotated["secret"] not in json.dumps(rotated["webhook"])
    assert api.get("/api/webhooks").headers["cache-control"] == "no-store"


def test_the_api_is_strict(api):
    assert api.post("/api/webhooks", json={"name": "x", "secret": "mine"}).status_code == 422
    assert api.post("/api/webhooks", json={"name": ""}).status_code == 422
    assert api.post("/api/webhooks", content=b"[1]").status_code == 422
    assert api.get("/api/webhooks/nope").status_code == 404
    wid = api.post("/api/webhooks", json={"name": "x"}).json()["webhook"]["id"]
    assert api.patch(f"/api/webhooks/{wid}", json={"name": "y", "extra": 1}).status_code == 422
    assert api.patch(f"/api/webhooks/{wid}", json={"name": "y"}).json()["name"] == "y"
    assert api.delete(f"/api/webhooks/{wid}").json() == {"deleted": wid}
    assert api.delete(f"/api/webhooks/{wid}").status_code == 404
