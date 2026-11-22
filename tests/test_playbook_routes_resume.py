"""Resume by operation id over HTTP (#2263): a body of exactly {"operation_id"} and nothing else.

A second client (a reload, another browser) settles an interrupted bind, apply or removal with no
request in hand. Only the fields' ABSENCE means resume: null, empty, malformed and partial bodies
refuse before any effect, as before, and a request-free call never starts an operation.
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent_sessions.main import create_app
from agent_sessions.playbooks import lifecycle, material_write
from test_playbook_routes_lifecycle import _review_and_bind, login, url
from test_playbook_routes_lifecycle import client as _client
from test_playbook_routes_lifecycle import setup as _setup

client, setup = _client, _setup

SECRET = "routed-resume-secret"


def _second_client(auth_cfg):
    """A different browser: its own cookie jar, session and CSRF token."""
    c = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    return c, login(c, auth_cfg)


def _fail_second(module, name, patch):
    real, calls = getattr(module, name), []

    def fail(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(*a, **kw)

    patch.setattr(module, name, fail)


def _bound(c, entity, inputs, hdr):
    _, bound = _review_and_bind(c, entity, inputs, hdr)
    assert bound.status_code == 200, bound.text
    return bound.json()


def test_another_browser_resumes_an_interrupted_removal(client, auth_cfg, monkeypatch):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    bound = _bound(c, entity, inputs, hdr)
    c.post(
        url(entity, "/apply"),
        json={"receipt": bound["receipt"], "operation_id": "apply-operation-1"},
        headers=hdr,
    )
    digest = c.post(url(entity, "/remove/plan"), json={}, headers=hdr).json()["digest"]
    with monkeypatch.context() as patch:
        _fail_second(material_write, "remove", patch)
        failed = c.post(
            url(entity, "/remove"),
            json={"digest": digest, "operation_id": "remove-operation-1"},
            headers=hdr,
        )
    assert failed.status_code >= 400
    other, ohdr = _second_client(auth_cfg)
    status = other.get(url(entity)).json()
    assert status["state"] == "interrupted" and status["operation"] == "remove"
    body = {"operation_id": status["operation_id"]}
    resumed = other.post(url(entity, "/remove"), json=body, headers=ohdr)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "removed" and not (folder / "RULES.md").exists()
    assert resumed.headers["cache-control"] == "no-store"
    # The first browser's response was lost; its own retry with the digest answers the same.
    again = c.post(
        url(entity, "/remove"),
        json={"digest": digest, "operation_id": "remove-operation-1"},
        headers=hdr,
    )
    assert again.status_code == 200 and again.json() == resumed.json()
    assert other.post(url(entity, "/remove"), json=body, headers=ohdr).json() == resumed.json()


def test_another_browser_resumes_an_interrupted_apply(client, auth_cfg, monkeypatch):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    bound = _bound(c, entity, inputs, hdr)
    with monkeypatch.context() as patch:
        _fail_second(material_write, "create", patch)
        failed = c.post(
            url(entity, "/apply"),
            json={"receipt": bound["receipt"], "operation_id": "apply-operation-1"},
            headers=hdr,
        )
    assert failed.status_code >= 400
    other, ohdr = _second_client(auth_cfg)
    status = other.get(url(entity)).json()
    assert status["state"] == "interrupted" and status["operation"] == "apply"
    resumed = other.post(
        url(entity, "/apply"), json={"operation_id": status["operation_id"]}, headers=ohdr
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "applied" and (folder / "RULES.md").exists()
    assert other.get(url(entity)).json()["state"] == "applied"


def test_another_browser_resumes_an_interrupted_bind_and_no_value_comes_back(
    client, auth_cfg, monkeypatch
):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    inputs = {**inputs, "bindings": [{"name": "token", "kind": "secret", "value": SECRET}]}
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle, "_check_plan", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
        )
        _, failed = _review_and_bind(c, entity, inputs, hdr)
    assert failed.status_code >= 400
    other, ohdr = _second_client(auth_cfg)
    status = other.get(url(entity)).json()
    assert status["state"] == "interrupted" and status["operation"] == "bind"
    resumed = other.post(
        url(entity, "/bind"), json={"operation_id": status["operation_id"]}, headers=ohdr
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "bound"
    assert SECRET not in resumed.text and SECRET not in json.dumps(other.get(url(entity)).json())
    # Binding stops at bound: nothing in the destination until a separate apply.
    assert list(folder.iterdir()) == []
    applied = other.post(
        url(entity, "/apply"),
        json={"receipt": resumed.json()["receipt"], "operation_id": "apply-operation-1"},
        headers=ohdr,
    )
    assert applied.status_code == 200, applied.text


@pytest.mark.parametrize("tail", ["/bind", "/apply", "/remove"])
def test_a_request_free_call_with_nothing_pending_is_a_409_and_changes_nothing(
    client, auth_cfg, tail
):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    if tail == "/remove":
        bound = _bound(c, entity, inputs, hdr)
        c.post(
            url(entity, "/apply"),
            json={"receipt": bound["receipt"], "operation_id": "apply-operation-1"},
            headers=hdr,
        )
    elif tail == "/apply":
        _bound(c, entity, inputs, hdr)
    before = (c.get(url(entity)).json(), sorted(p.name for p in folder.rglob("*")))
    r = c.post(url(entity, tail), json={"operation_id": "never-started-1"}, headers=hdr)
    assert r.status_code == 409, r.text
    assert "pending or recorded under this id" in r.json()["detail"]
    assert r.headers["cache-control"] == "no-store"
    assert (c.get(url(entity)).json(), sorted(p.name for p in folder.rglob("*"))) == before


@pytest.mark.parametrize(
    "tail, body",
    [
        ("/apply", {"operation_id": "apply-operation-1", "receipt": None}),
        ("/apply", {"operation_id": "apply-operation-1", "receipt": ""}),
        ("/apply", {"operation_id": None}),
        ("/apply", {"operation_id": "BAD"}),
        ("/remove", {"operation_id": "remove-operation-1", "digest": None}),
        ("/remove", {"operation_id": "remove-operation-1", "digest": ""}),
        ("/remove", {"operation_id": "remove-operation-1", "digest": "zz"}),
        ("/remove", {"operation_id": 7}),
        ("/bind", {"operation_id": "bind-operation-1", "receipt": None}),
        ("/bind", {"operation_id": "bind-operation-1", "playbook_id": "review-demo"}),
        ("/bind", {"operation_id": "bind-operation-1", "inputs": {}}),
        (
            "/bind",
            {
                "operation_id": "bind-operation-1",
                "inputs": None,
                "receipt": None,
                "playbook_id": None,
            },
        ),
        ("/bind", {"operation_id": ""}),
    ],
)
def test_null_empty_malformed_and_partial_bodies_are_never_a_resume(
    client, auth_cfg, monkeypatch, tail, body
):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    # Leave a bind interrupted under bind-operation-1, so a misread body WOULD have work to do.
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle, "_check_plan", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
        )
        _review_and_bind(c, entity, inputs, hdr)
    before = c.get(url(entity)).json()
    r = c.post(url(entity, tail), json=body, headers=hdr)
    assert r.status_code == 422, r.text
    assert r.headers["cache-control"] == "no-store"
    assert c.get(url(entity)).json() == before
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("tail", ["/bind", "/apply", "/remove"])
def test_a_resume_needs_a_session_csrf_and_the_origin(client, auth_cfg, tail):
    c, entity, _, _ = client
    body = {"operation_id": "any-operation-1"}
    assert c.post(url(entity, tail), json=body).status_code == 401
    hdr = login(c, auth_cfg)
    assert c.post(url(entity, tail), json=body).status_code == 403
    assert (
        c.post(
            url(entity, tail), json=body, headers={**hdr, "Origin": "https://other.example"}
        ).status_code
        == 403
    )
