"""The batched fleet update: frozen set and plans, per-project outcomes, same-id retry."""

import pytest

from agent_sessions import projects
from agent_sessions.playbooks import apply, fleet, lifecycle, store
from test_playbook_fleet import _new_revision
from test_playbook_fleet import fleet_of_two as _fleet_of_two
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup
fleet_of_two = _fleet_of_two
OP = "fleet-operation-one"


def _reviewed():
    revision = _new_revision()
    return revision, fleet.plan("review-demo", key=KEY)["digest"]


def _outcomes(result):
    return {row["project_id"]: row["outcome"] for row in result["projects"]}


def test_a_reviewed_batch_updates_every_project_and_a_replay_does_not_rerun(fleet_of_two):
    revision, digest = _reviewed()
    result = fleet.update("review-demo", OP, digest, key=KEY)
    assert set(_outcomes(result).values()) == {"applied"}
    for entity, folder in fleet_of_two:
        assert (folder / "RULES.md").read_text() == "Check https://ops.example/health carefully.\n"
        assert record(entity.id)["inputs"]["revision"] == revision
    assert fleet.operation("review-demo", OP) == result
    assert fleet.update("review-demo", OP, digest, key=KEY) == result
    assert not any(p["update_available"] for p in fleet.projects("review-demo")["projects"])


def test_a_fleet_changed_since_its_review_refuses_before_freezing(fleet_of_two):
    _, digest = _reviewed()
    (_, folder), _ = fleet_of_two
    (folder / "RULES.md").write_text("an operator edit after the review\n")
    with pytest.raises(store.Conflict, match="review it again"):
        fleet.update("review-demo", OP, digest, key=KEY)
    with pytest.raises(store.StoreError):
        fleet.operation("review-demo", OP)  # nothing was frozen


def test_a_crash_after_one_project_succeeds_retries_only_the_rest(fleet_of_two, monkeypatch):
    _, digest = _reviewed()
    real, calls = apply.apply, []

    def crash_on_second(pid, *a, **kw):
        calls.append(pid)
        if len(calls) == 2:
            raise OSError("simulated crash")
        return real(pid, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply, "apply", crash_on_second)
        with pytest.raises(OSError):
            fleet.update("review-demo", OP, digest, key=KEY)
    states = _outcomes(fleet.operation("review-demo", OP))
    assert sorted(states.values()) == ["applied", "not-attempted"]
    done = next(pid for pid, s in states.items() if s == "applied")
    before = record(done)["apply_history"]
    result = fleet.update("review-demo", OP, digest, key=KEY)
    assert set(_outcomes(result).values()) == {"applied"}
    assert record(done)["apply_history"] == before  # the finished project was not touched again


def test_a_crash_between_bind_and_apply_reconciles_by_replay(fleet_of_two, monkeypatch):
    _, digest = _reviewed()
    with monkeypatch.context() as patch:
        patch.setattr(
            apply, "apply", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash after bind"))
        )
        with pytest.raises(OSError):
            fleet.update("review-demo", OP, digest, key=KEY)
    result = fleet.update("review-demo", OP, digest, key=KEY)
    assert set(_outcomes(result).values()) == {"applied"}


def test_a_project_changed_after_the_freeze_is_stale_and_never_retried(fleet_of_two, monkeypatch):
    _, digest = _reviewed()
    real, calls = apply.apply, []

    def crash_first(pid, *a, **kw):
        calls.append(pid)
        if len(calls) == 1:
            raise OSError("simulated crash")
        return real(pid, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(apply, "apply", crash_first)
        with pytest.raises(OSError):
            fleet.update("review-demo", OP, digest, key=KEY)
    first, second = sorted(fleet_of_two, key=lambda d: d[0].id)
    (second[1] / "RULES.md").write_text("an operator edit after the freeze\n")
    states = _outcomes(fleet.update("review-demo", OP, digest, key=KEY))
    assert states == {first[0].id: "applied", second[0].id: "stale"}
    assert (second[1] / "RULES.md").read_text() == "an operator edit after the freeze\n"
    assert _outcomes(fleet.update("review-demo", OP, digest, key=KEY))[second[0].id] == "stale"


def test_a_refused_project_is_failed_and_a_retry_completes_it(fleet_of_two, monkeypatch):
    _, digest = _reviewed()
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle,
            "bind",
            lambda *a, **kw: (_ for _ in ()).throw(store.Conflict("busy elsewhere")),
        )
        result = fleet.update("review-demo", OP, digest, key=KEY)
    assert set(_outcomes(result).values()) == {"failed"}
    assert all(row["detail"] == "busy elsewhere" for row in result["projects"])
    assert set(_outcomes(fleet.update("review-demo", OP, digest, key=KEY)).values()) == {"applied"}


def test_a_retry_never_enrols_a_project_deployed_after_the_freeze(
    fleet_of_two, tmp_path, monkeypatch
):
    _, digest = _reviewed()
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle,
            "bind",
            lambda *a, **kw: (_ for _ in ()).throw(store.Conflict("not now")),
        )
        fleet.update("review-demo", OP, digest, key=KEY)
    third = tmp_path / "third"
    third.mkdir()
    entity = projects.create("Late", folders=[str(third)], default_folder=str(third))
    assert entity.id not in _outcomes(fleet.operation("review-demo", OP))
    assert entity.id not in _outcomes(fleet.update("review-demo", OP, digest, key=KEY))


def test_an_operation_id_is_never_reused_for_another_review(fleet_of_two):
    _, digest = _reviewed()
    fleet.update("review-demo", OP, digest, key=KEY)
    with pytest.raises(store.Conflict, match="another request"):
        fleet.update("review-demo", OP, "f" * 64, key=KEY)


def test_the_update_routes_need_a_session_csrf_and_origin(fleet_of_two, auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _new_revision()
    from dataclasses import replace

    # The fixture deployed with the tests' signing key; the app must verify with the same one.
    c = TestClient(create_app(replace(auth_cfg, secret_key=KEY)), base_url=auth_cfg.origin)
    body = {"digest": "0" * 64, "operation_id": OP}
    assert c.post("/api/playbooks/review-demo/fleet/update", json=body).status_code == 401
    assert c.get(f"/api/playbooks/review-demo/fleet/{OP}").status_code == 401
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    assert c.post("/api/playbooks/review-demo/fleet/update", json=body).status_code == 403
    # The digest comes from the app's own combined review (it signs with its own key).
    reviewed = c.post("/api/playbooks/review-demo/fleet/review", json={}, headers=hdr).json()
    body = {"digest": reviewed["digest"], "operation_id": OP}
    done = c.post("/api/playbooks/review-demo/fleet/update", json=body, headers=hdr)
    assert done.status_code == 200, done.text
    assert set(_outcomes(done.json()).values()) == {"applied"}
    got = c.get(f"/api/playbooks/review-demo/fleet/{OP}")
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    assert c.get("/api/playbooks/review-demo/fleet/unknown-operation").status_code == 404


def test_a_crash_after_binds_durable_intent_is_recovered_by_the_fleet_retry(
    fleet_of_two, monkeypatch
):
    _, digest = _reviewed()
    with monkeypatch.context() as patch:
        # `_check_plan` runs after bind has durably written its intent, before the variable write.
        patch.setattr(
            lifecycle,
            "_check_plan",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("crash after the intent")),
        )
        with pytest.raises(OSError):
            fleet.update("review-demo", OP, digest, key=KEY)
    stuck = [e.id for e, _ in fleet_of_two if record(e.id)["state"] == "binding_intent"]
    assert stuck, "the crash must leave a pending bind"
    result = fleet.update("review-demo", OP, digest, key=KEY)
    assert set(_outcomes(result).values()) == {"applied"}
    assert all(record(e.id)["state"] == "applied" for e, _ in fleet_of_two)


def test_a_bind_refused_before_any_intent_reviews_afresh_on_retry(fleet_of_two, monkeypatch):
    _, digest = _reviewed()
    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle,
            "bind",
            lambda *a, **kw: (_ for _ in ()).throw(store.Conflict("the review expired")),
        )
        assert set(_outcomes(fleet.update("review-demo", OP, digest, key=KEY)).values()) == {
            "failed"
        }
    assert set(_outcomes(fleet.update("review-demo", OP, digest, key=KEY)).values()) == {"applied"}
