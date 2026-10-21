"""#1191 acceptance regressions not pinned by a slice of their own.

* A probe never fires, and no agent is dispatched, on bind, apply, status, remove or a fleet
  update (load, save and review are pinned in `test_playbook_review`), and nothing is ever
  scheduled: a deployed ritual creates no automation, however much time passes.
* reference → managed adoption survives a crash between the adoption and its record.
"""

import os
import time

import pytest

from agent_sessions import automations_store, headless_dispatch, mission_probes, projects
from agent_sessions.playbooks import apply, fleet, lifecycle, remove, review, store
from test_playbook_fleet import fleet_of_two as _fleet_of_two
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup
from test_playbook_rituals import _apply as _ritual_apply
from test_playbook_rituals import ritual_demo as _ritual_demo

setup = _review_setup
ritual_demo = _ritual_demo
fleet_of_two = _fleet_of_two


@pytest.fixture
def no_effects(monkeypatch):
    """Fence every probe entry point (`probe_one` covers the forge kinds' own client too), both
    transports, agent dispatch and automation creation. A breach is RECORDED as well as raised,
    and asserted at teardown, so a broad `except` in the code under test cannot swallow it."""
    breaches = []

    def forbidden(name):
        def call(*args, **kwargs):
            breaches.append(name)
            pytest.fail(f"the deployment lifecycle must never {name}")

        return call

    monkeypatch.setattr(mission_probes, "probe_one", forbidden("probe"))
    monkeypatch.setattr(mission_probes, "_probe_http", forbidden("probe over http"))
    monkeypatch.setattr(mission_probes, "_probe_git_local", forbidden("probe git"))
    monkeypatch.setattr(headless_dispatch, "dispatch", forbidden("dispatch an agent"))
    monkeypatch.setattr(automations_store, "create", forbidden("create an automation"))
    yield breaches
    assert breaches == [], breaches


def test_bind_apply_status_and_remove_never_probe_dispatch_or_schedule(ritual_demo, no_effects):
    entity, inputs = ritual_demo
    _ritual_apply(entity, inputs)  # bind + apply of a playbook with connections and a ritual
    assert apply.status(entity.id)["state"] == "applied"
    plan = remove.plan(entity.id, key=KEY)
    remove.remove(entity.id, "remove-op-10000", plan["digest"], key=KEY)


def test_a_fleet_review_and_update_never_probe_dispatch_or_schedule(fleet_of_two, no_effects):
    contents = files()
    contents["template/RULES.md"] = "Check {{endpoint}} carefully.\n"
    from test_playbook_fleet import _write

    _write(contents)
    digest = fleet.plan("review-demo", key=KEY)["digest"]
    fleet.update("review-demo", "fleet-operation-one", digest, key=KEY)


def test_a_deployed_ritual_is_never_scheduled_even_when_the_scheduler_ticks_long_after(
    ritual_demo, no_effects, monkeypatch
):
    import asyncio

    from agent_sessions import automation_loop

    entity, inputs = ritual_demo
    _ritual_apply(entity, inputs)
    # The suite disables the loop by default (conftest); a tick would return before any due check.
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    now = time.time()
    later = now + 400 * 24 * 3600  # well past any daily or weekly slot
    sched = automation_loop.Scheduler(clock=lambda: later, started_at=now)
    try:
        out = asyncio.run(sched.tick())  # the real due-check path
    finally:
        sched.release()
    assert out["owner"] is True  # the tick really ran its due check
    assert out["fired"] == []
    assert automations_store.list_all() == []
    assert [r["scheduled"] for r in apply.status(entity.id)["rituals"]] == [False]


def _load_save_review(setup):
    from agent_sessions.playbooks import loader

    _, body = setup
    loader.load_bundle(store.local_root() / "review-demo")  # load
    changed = files()
    changed["template/RULES.md"] += "Updated instructions\n"
    revision = store.update_playbook("review-demo", body["revision"], changed)["revision"]  # save
    plan = review.build("review-demo", {**body, "revision": revision}, key=KEY)  # review
    review.confirm(plan, plan.public["digest"], [t["id"] for t in plan.public["targets"]], key=KEY)


def test_load_save_and_review_never_probe_dispatch_or_schedule(setup, no_effects):
    _load_save_review(setup)


@pytest.mark.parametrize(
    "entry",
    [
        "agent_sessions.playbooks.loader.load_bundle",
        "agent_sessions.playbooks.store.update_playbook",
        "agent_sessions.playbooks.review.build",
    ],
)
def test_the_fence_catches_a_forge_probe_at_load_save_and_review(
    setup, no_effects, monkeypatch, entry
):
    """Negative control per entry point: a `forge_*` probe added there is caught and recorded."""
    import importlib

    module_name, attr = entry.rsplit(".", 1)
    module = importlib.import_module(module_name)
    real = getattr(module, attr)

    def probing(*a, **kw):
        mission_probes.probe_one({}, {"probe": "forge_merged"})
        return real(*a, **kw)

    monkeypatch.setattr(module, attr, probing)
    with pytest.raises(pytest.fail.Exception, match="must never probe"):
        _load_save_review(setup)
    assert no_effects == ["probe"]
    no_effects.clear()


def test_the_fence_catches_a_forge_probe_that_bypasses_both_transports(
    ritual_demo, no_effects, monkeypatch
):
    """Negative control under the REAL fence: a `forge_*` probe never touches `_probe_http` or
    `_probe_git_local` (it has its own client), yet `probe_one` is fenced and the breach logged."""
    real = apply.apply

    def probing_apply(*a, **kw):
        mission_probes.probe_one({}, {"probe": "forge_merged"})
        return real(*a, **kw)

    monkeypatch.setattr(apply, "apply", probing_apply)
    entity, inputs = ritual_demo
    with pytest.raises(pytest.fail.Exception, match="must never probe"):
        _ritual_apply(entity, inputs)
    assert no_effects == ["probe"]
    no_effects.clear()  # the control's own, expected breach


def _adoption_bundle(disposition):
    contents = files()
    contents["playbook.toml"] = contents["playbook.toml"].replace(
        '[[materials]]\npath = "RULES.md"\ndisposition = "managed"\ntemplate = true\n',
        f'[[materials]]\npath = "RULES.md"\ndisposition = "{disposition}"\n',
    )
    contents["template/RULES.md"] = "Shared rules.\n"
    tree = store.tree_from_files(contents)
    for relative, data in tree.files.items():
        path = store.local_root() / "review-demo" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return tree.digest()


def test_reference_to_managed_adoption_survives_a_crash_between_adoption_and_record(
    setup, monkeypatch
):
    folder, body = setup
    (folder / "RULES.md").write_text("Shared rules.\n")  # the operator's identical file
    revision = _adoption_bundle("reference")
    entity = projects.create("Adopt", folders=[str(folder)], default_folder=str(folder))
    inputs = {**body, "revision": revision, "bindings": [], "project_id": entity.id}

    def cycle(n, *, crash=False):
        plan = review.build("review-demo", inputs, key=KEY)
        receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
        bound = lifecycle.bind(
            entity.id, "review-demo", inputs, receipt["receipt"], f"bind-op-{n}0000", key=KEY
        )
        op = f"apply-op-{n}0000"
        if not crash:
            return apply.apply(entity.id, op, bound["receipt"], key=KEY)
        with monkeypatch.context() as patch:
            # After every effect settled, before ownership is recorded: the adoption's crash point.
            patch.setattr(
                apply, "_ownership", lambda *a, **kw: (_ for _ in ()).throw(OSError("crash"))
            )
            with pytest.raises(OSError):
                apply.apply(entity.id, op, bound["receipt"], key=KEY)
        # Crashed after the adoption settled on disk, before its record: still a reference.
        assert record(entity.id)["files"]["RULES.md"]["disposition"] == "reference"
        assert apply.status(entity.id)["state"] == "interrupted"
        return apply.apply(entity.id, op, bound["receipt"], key=KEY)  # the same-id retry

    cycle(1)
    assert record(entity.id)["files"]["RULES.md"]["disposition"] == "reference"
    inode = os.stat(folder / "RULES.md").st_ino
    inputs["revision"] = _adoption_bundle("managed")
    assert cycle(2, crash=True)["state"] == "applied"
    owned = record(entity.id)["files"]["RULES.md"]
    assert owned["disposition"] == "managed" and owned["inode"][1] == inode
    assert os.stat(folder / "RULES.md").st_ino == inode  # adopted in place, never rewritten
    later = remove.plan(entity.id, key=KEY)
    assert {c["path"]: c["action"] for c in later["changes"]}["RULES.md"] == "remove"
