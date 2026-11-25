"""The deployment lifecycle over HTTP: session, CSRF, Origin, no-store and the round trip."""

import secrets

import pytest
from fastapi.testclient import TestClient

from agent_sessions import projects
from agent_sessions.main import create_app
from agent_sessions.routes import playbooks as routes
from test_playbook_review import setup as _review_setup

setup = _review_setup


@pytest.fixture
def client(setup, auth_cfg):
    folder, body = setup
    c = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    entity = projects.create("Routed project", folders=[str(folder)], default_folder=str(folder))
    return c, entity, {**body, "project_id": entity.id, "bindings": []}, folder


def login(c, auth_cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}


def url(entity, tail=""):
    return routes.DEPLOY.replace("{project}", entity.id) + tail


def test_the_full_lifecycle_round_trips_over_http(client, auth_cfg):
    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    review = c.post("/api/playbooks/review-demo/review", json=inputs, headers=hdr).json()
    receipt = c.post(
        "/api/playbooks/review-demo/review/confirm",
        json={"inputs": inputs, "digest": review["digest"], "targets": ["connection:service"]},
        headers=hdr,
    ).json()["receipt"]
    bound = c.post(
        url(entity, "/bind"),
        json={
            "playbook_id": "review-demo",
            "inputs": inputs,
            "receipt": receipt,
            "operation_id": "bind-operation-1",
        },
        headers=hdr,
    )
    assert bound.status_code == 200, bound.text
    applied = c.post(
        url(entity, "/apply"),
        json={"receipt": bound.json()["receipt"], "operation_id": "apply-operation-1"},
        headers=hdr,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["state"] == "applied" and (folder / "RULES.md").exists()
    status = c.get(url(entity))
    assert status.status_code == 200 and status.json()["state"] == "applied"
    assert status.headers["cache-control"] == "no-store"
    plan = c.post(url(entity, "/remove/plan"), json={}, headers=hdr)
    assert plan.status_code == 200 and not plan.json()["conflicts"]
    removed = c.post(
        url(entity, "/remove"),
        json={"digest": plan.json()["digest"], "operation_id": "remove-operation-1"},
        headers=hdr,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["state"] == "removed" and not (folder / "RULES.md").exists()
    assert c.get(url(entity)).json()["state"] == "removed"


@pytest.mark.parametrize("tail", ["/bind", "/apply", "/remove/plan", "/remove"])
def test_every_write_needs_a_session_csrf_and_the_origin(client, auth_cfg, tail):
    c, entity, _, _ = client
    assert c.post(url(entity, tail), json={}).status_code == 401
    hdr = login(c, auth_cfg)
    assert c.post(url(entity, tail), json={}).status_code == 403
    assert (
        c.post(
            url(entity, tail), json={}, headers={**hdr, "Origin": "https://other.example"}
        ).status_code
        == 403
    )


def test_reads_need_a_session(client):
    c, entity, _, _ = client
    r = c.get(url(entity))
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "tail, body",
    [
        ("/apply", {"receipt": 42, "operation_id": "apply-operation-1"}),
        ("/apply", {"receipt": "x", "operation_id": "BAD"}),
        ("/apply", {"receipt": "x"}),
        ("/remove", {"digest": "x", "operation_id": "remove-operation-1", "extra": 1}),
        ("/bind", {"playbook_id": "review-demo"}),
    ],
)
def test_malformed_requests_are_refused_before_any_effect(client, auth_cfg, tail, body):
    c, entity, _, folder = client
    hdr = login(c, auth_cfg)
    r = c.post(url(entity, tail), json=body, headers=hdr)
    assert 400 <= r.status_code < 500 and r.status_code not in (401, 403), r.text
    assert r.headers["cache-control"] == "no-store"
    assert list(folder.iterdir()) == []


def test_a_project_id_that_is_not_one_is_a_422(client, auth_cfg):
    c, _, _, _ = client
    login(c, auth_cfg)
    assert c.get("/api/projects/NOT%20AN%20ID/playbook").status_code == 422


def _review_and_bind(c, entity, inputs, hdr, playbook="review-demo"):
    review = c.post(f"/api/playbooks/{playbook}/review", json=inputs, headers=hdr)
    assert review.status_code == 200, review.text
    targets = [t["id"] for t in review.json()["targets"] if t["requires_confirmation"]]
    receipt = c.post(
        f"/api/playbooks/{playbook}/review/confirm",
        json={"inputs": inputs, "digest": review.json()["digest"], "targets": targets},
        headers=hdr,
    ).json()["receipt"]
    return receipt, c.post(
        url(entity, "/bind"),
        json={
            "playbook_id": playbook,
            "inputs": inputs,
            "receipt": receipt,
            "operation_id": "bind-operation-1",
        },
        headers=hdr,
    )


def test_an_interrupted_removal_reports_interrupted_and_settles_over_http(
    client, auth_cfg, monkeypatch
):
    from agent_sessions.playbooks import material_write

    c, entity, inputs, folder = client
    hdr = login(c, auth_cfg)
    _, bound = _review_and_bind(c, entity, inputs, hdr)
    c.post(
        url(entity, "/apply"),
        json={"receipt": bound.json()["receipt"], "operation_id": "apply-operation-1"},
        headers=hdr,
    )
    digest = c.post(url(entity, "/remove/plan"), json={}, headers=hdr).json()["digest"]
    real, calls = material_write.remove, []

    def fail_second(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(material_write, "remove", fail_second)
        failed = c.post(
            url(entity, "/remove"),
            json={"digest": digest, "operation_id": "remove-operation-1"},
            headers=hdr,
        )
    assert failed.status_code >= 400
    status = c.get(url(entity)).json()
    assert status["state"] == "interrupted" and status["operation"] == "remove"
    assert status["operation_id"] == "remove-operation-1"
    assert sorted(f["state"] for f in status["files"]) == ["not removed", "removed"]
    retried = c.post(
        url(entity, "/remove"),
        json={"digest": digest, "operation_id": "remove-operation-1"},
        headers=hdr,
    )
    assert retried.status_code == 200 and c.get(url(entity)).json()["state"] == "removed"


def test_an_interrupted_bind_reports_interrupted_never_an_internal_state(
    client, auth_cfg, monkeypatch
):
    from agent_sessions.playbooks import lifecycle

    c, entity, inputs, _ = client
    hdr = login(c, auth_cfg)
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle, "_check_plan", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
        )
        _, bound = _review_and_bind(c, entity, inputs, hdr)
    assert bound.status_code >= 400
    status = c.get(url(entity)).json()
    assert status["state"] == "interrupted" and status["operation"] == "bind"
    assert status["operation_id"] == "bind-operation-1"


def _big_playbook(flows=2, steps=30, items=12):
    from test_playbook_review import files

    contents = files()
    contents.pop("flows/check.toml")
    for f in range(flows):
        body = ["format = 1", f'title = "Flow {f}"']
        for s in range(steps):
            body += ["[[steps]]", f'id = "s{s:02d}"', f'title = "Step {s}"']
            body.append('actor = {kind = "operator"}')
            for i in range(items):
                body += [
                    "[[steps.checklist]]",
                    f'key = "k{s:02d}{i:02d}"',
                    f'title = "Item {i}"',
                    'probe = "http_status"',
                    'probe_args = {url = "{{endpoint}}"}',
                ]
        contents[f"flows/f{f}.toml"] = "\n".join(body) + "\n"
    contents["playbook.toml"] = contents["playbook.toml"].replace(
        'id = "review-demo"', 'id = "big-demo"'
    )
    return contents


def test_a_large_valid_receipt_round_trips_through_bind_and_apply(client, auth_cfg):
    from agent_sessions.playbooks import loader, review, store

    c, entity, inputs, folder = client
    contents = store.tree_from_files(_big_playbook(flows=6))
    loader.validate_named(contents, "big-demo")
    for relative, data in contents.files.items():
        path = store.local_root() / "big-demo" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    hdr = login(c, auth_cfg)
    big = {**inputs, "revision": contents.digest()}
    receipt, bound = _review_and_bind(c, entity, big, hdr, playbook="big-demo")
    assert len(receipt) > 4096  # past the old cap, within the derived one
    assert len(receipt) <= review.RECEIPT_MAX
    assert bound.status_code == 200, bound.text
    applied = c.post(
        url(entity, "/apply"),
        json={"receipt": bound.json()["receipt"], "operation_id": "apply-operation-1"},
        headers=hdr,
    )
    assert applied.status_code == 200, applied.text


def test_the_receipt_bound_covers_the_largest_receipt_confirm_can_sign():
    from itsdangerous import URLSafeTimedSerializer

    from agent_sessions.playbooks import review

    # Random ids: the signer zlib-compresses, so repeated ids would prove nothing about the bound.
    widest = [
        secrets.token_hex(review._TARGET_ID_MAX)[: review._TARGET_ID_MAX]
        for _ in range(review._TARGETS_MAX)
    ]
    signed = URLSafeTimedSerializer("k", salt=review._SALT).dumps(
        {"digest": "0" * 64, "targets": widest}
    )
    assert len(signed) <= review.RECEIPT_MAX
