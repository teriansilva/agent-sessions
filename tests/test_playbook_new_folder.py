"""#1187 new-folder mode: review an absent target, CREATE it exclusively, BIND adopts only it."""

import os

import pytest

from agent_sessions import projects
from agent_sessions.playbooks import apply, lifecycle, review, store, targets
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY
from test_playbook_review import setup as _review_setup

setup = _review_setup


@pytest.fixture
def new_folder(setup, tmp_path):
    _, body = setup
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "fresh-project"
    inputs = {**body, "destination": str(target), "create": True, "bindings": []}
    plan = review.build("review-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    return target, inputs, receipt["receipt"], plan


def _bind_and_apply(target, inputs, receipt, n="1"):
    entity = projects.create("Fresh", folders=[str(target)], default_folder=str(target))
    bound = lifecycle.bind(entity.id, "review-demo", inputs, receipt, f"bind-op-{n}0000", key=KEY)
    return entity, bound


def test_the_full_new_folder_journey(new_folder):
    target, inputs, receipt, plan = new_folder
    assert plan.public["destination"]["create"] is True and not target.exists()
    created = targets.create("review-demo", inputs, receipt, key=KEY)
    assert target.is_dir() and created["inode"] == target.stat().st_ino
    entity, bound = _bind_and_apply(target, inputs, receipt)
    result = apply.apply(entity.id, "apply-op-10000", bound["receipt"], key=KEY)
    assert result["state"] == "applied"
    assert (target / "RULES.md").read_text().startswith("Check ")
    stored = record(entity.id)
    assert stored["destination"]["inode"] == created["inode"]
    assert "create" not in stored["inputs"]  # an ordinary existing-folder deployment from here


def test_a_target_someone_else_created_after_the_review_is_never_adopted(new_folder):
    target, inputs, receipt, _ = new_folder
    target.mkdir()  # between REVIEW and CREATE, even an empty directory
    with pytest.raises(store.StoreError):
        targets.create("review-demo", inputs, receipt, key=KEY)
    with pytest.raises(store.Conflict, match="created no folder"):
        _bind_and_apply(target, inputs, receipt)


def test_a_target_replaced_between_create_and_bind_is_refused(new_folder):
    target, inputs, receipt, _ = new_folder
    targets.create("review-demo", inputs, receipt, key=KEY)
    os.rename(target, target.with_name("ours-moved-aside"))
    target.mkdir()
    with pytest.raises(store.Conflict, match="replaced"):
        _bind_and_apply(target, inputs, receipt)


def test_a_target_replaced_between_create_and_apply_is_refused(new_folder):
    target, inputs, receipt, _ = new_folder
    targets.create("review-demo", inputs, receipt, key=KEY)
    entity, bound = _bind_and_apply(target, inputs, receipt)
    os.rename(target, target.with_name("ours-moved-aside"))
    target.mkdir()
    with pytest.raises(store.StoreError):
        apply.apply(entity.id, "apply-op-10000", bound["receipt"], key=KEY)
    assert list(target.iterdir()) == []


def test_content_the_review_did_not_see_refuses_adoption(new_folder):
    target, inputs, receipt, _ = new_folder
    targets.create("review-demo", inputs, receipt, key=KEY)
    (target / "RULES.md").write_text("someone else's file\n")
    with pytest.raises(store.Conflict, match="did not see"):
        _bind_and_apply(target, inputs, receipt)
    assert (target / "RULES.md").read_text() == "someone else's file\n"


def test_a_create_retry_answers_for_the_same_folder_only(new_folder):
    target, inputs, receipt, _ = new_folder
    first = targets.create("review-demo", inputs, receipt, key=KEY)
    assert targets.create("review-demo", inputs, receipt, key=KEY) == first
    os.rename(target, target.with_name("aside"))
    target.mkdir()
    with pytest.raises(store.Conflict, match="replaced"):
        targets.create("review-demo", inputs, receipt, key=KEY)


def test_create_mode_review_refuses_an_existing_name_or_a_project(new_folder, setup):
    target, inputs, _, _ = new_folder
    target.mkdir()
    with pytest.raises(store.Conflict, match="already exists"):
        review.build("review-demo", inputs, key=KEY)
    folder, _ = setup
    entity = projects.create("Other", folders=[str(folder)], default_folder=str(folder))
    with pytest.raises(store.Conflict, match="before its project exists"):
        review.build("review-demo", {**inputs, "project_id": entity.id}, key=KEY)


def test_the_create_target_route_needs_a_session_csrf_and_origin(new_folder, auth_cfg):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    target, inputs, receipt, _ = new_folder
    c = TestClient(create_app(replace(auth_cfg, secret_key=KEY)), base_url=auth_cfg.origin)
    url = "/api/playbooks/review-demo/review/create-target"
    body = {"inputs": inputs, "receipt": receipt}
    assert c.post(url, json=body).status_code == 401
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    assert c.post(url, json=body).status_code == 403
    assert not target.exists()
    made = c.post(url, json=body, headers=hdr)
    assert made.status_code == 200, made.text
    assert made.json()["inode"] == target.stat().st_ino
    assert made.headers["cache-control"] == "no-store"


def _returns_promptly(fn, seconds=5):
    """Run `fn` on a thread; a hang (e.g. a FIFO open) fails the test instead of the suite."""
    import threading

    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the test thread
            outcome["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "the call blocked (a planted FIFO must never be waited on)"
    return outcome


def test_a_fifo_planted_at_the_ledger_entry_is_refused_not_waited_on(new_folder):
    target, inputs, receipt, _ = new_folder
    digest = targets._signed_digest(receipt, KEY)
    with targets._ledger():
        pass  # the ledger directory exists
    os.mkfifo(store.local_root() / targets.LEDGER_DIR / f"{digest}.json", 0o600)
    out = _returns_promptly(lambda: targets.create("review-demo", inputs, receipt, key=KEY))
    assert isinstance(out.get("error"), store.Conflict)
    out = _returns_promptly(lambda: _bind_and_apply(target, inputs, receipt))
    assert isinstance(out.get("error"), store.StoreError)


def test_a_fifo_planted_at_a_fleet_journal_is_refused_not_waited_on(setup):
    from agent_sessions.playbooks import fleet

    with fleet._locked("review-demo") as fd:
        os.mkfifo("fleet-operation-one.json", 0o600, dir_fd=fd)
    out = _returns_promptly(lambda: fleet.operation("review-demo", "fleet-operation-one"))
    assert isinstance(out.get("error"), store.Conflict)


def test_create_holds_off_a_playbook_deletion_until_it_is_recorded(new_folder, monkeypatch):
    from agent_sessions.playbooks import destination

    target, inputs, receipt, _ = new_folder
    monkeypatch.setattr(store, "LOCK_WAIT_S", 0.2)
    real, attempts = destination.create_target, []

    def delete_then_create(*a, **kw):
        # Between planning and publication: a deletion must wait on the held shared lock.
        try:
            store.delete_playbook("review-demo", inputs["revision"])
            attempts.append("deleted")
        except store.Busy:
            attempts.append("busy")
        return real(*a, **kw)

    monkeypatch.setattr(destination, "create_target", delete_then_create)
    created = targets.create("review-demo", inputs, receipt, key=KEY)
    assert attempts == ["busy"] and target.is_dir() and created["path"] == str(target)
    assert (store.local_root() / "review-demo").is_dir()  # the source survived


def test_a_create_retry_refuses_once_the_source_is_gone(new_folder):
    target, inputs, receipt, _ = new_folder
    targets.create("review-demo", inputs, receipt, key=KEY)
    store.delete_playbook("review-demo", inputs["revision"])
    with pytest.raises(store.Conflict, match="changed or is gone"):
        targets.create("review-demo", inputs, receipt, key=KEY)


def test_an_unreviewed_intermediate_directory_refuses_adoption(new_folder):
    target, inputs, receipt, plan = new_folder
    assert any(m["path"].startswith("docs/") for m in plan.public["materials"])  # nested material
    targets.create("review-demo", inputs, receipt, key=KEY)
    (target / "docs").mkdir()  # the leaf stays absent; its parent did not exist at review
    with pytest.raises(store.Conflict, match="did not see"):
        _bind_and_apply(target, inputs, receipt)
    assert list((target / "docs").iterdir()) == []
