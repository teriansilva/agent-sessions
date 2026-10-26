"""An API session started from a specialist (#1693 S4b): the create re-validates the version,
every generation reserves (or renews) its slot and carries its skills and notes, and the slot is
freed when the session is proven to run nothing. Drives the real runtime over the fake worker.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import memory, native_runtime, native_state, projects, skill_bundles
from agent_sessions import specialists_store as store
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


ENGINE = ENGINES["codex"]


def _skill() -> dict:
    from agent_sessions.gallery import library
    from agent_sessions.gallery import skills as lib_skills

    if not lib_skills.storage.skills_root().joinpath("tdd").exists():
        library.create_skill("tdd", {"SKILL.md": "---\nname: tdd\ndescription: x\n---\nbody"})
    return {"id": "tdd", "revision": lib_skills.get("tdd")["revision"]}


def _spec(**over) -> dict:
    body = {
        "title": "Implementer",
        "summary": "",
        "skills": [_skill()],
        "engine": ENGINE,
        "model": "default",
        "effort": None,
        "capacity": 1,
    }
    body.update(over)
    return store.create(body)


def _config(worker: str) -> dict:
    return json.loads((native_state.root() / "workers" / worker / "config.json").read_text())


async def _create(folder, spec, **kw):
    return await runtime.create_session(
        ENGINE, str(folder), operation_id=ident(), specialist=(spec["id"], 1), **kw
    )


async def _first_turn(key: str) -> dict:
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="one")
    snap, _ = await settle(key, turn)
    return snap


async def test_a_create_from_a_specialist_carries_its_skills_and_notes(host, project):  # noqa: F811
    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    spec = _spec()
    key = (await _create(project, spec))["session_key"]
    snap = await _first_turn(key)
    cfg = _config(snap["native"]["worker"])
    [slot] = store.active()
    assert (slot["specialist_id"], slot["version"], slot["session_key"]) == (spec["id"], 1, key)
    assert cfg["skills_bundle"] == str(skill_bundles.path_of(slot["reservation_id"]))
    assert set(memory.SPECIALIST_ENV_KEYS) <= set(cfg["memory_env"])


@pytest.mark.parametrize(
    "over, why",
    [
        ({"engine": ENGINES["claude"]}, "runs on"),
        ({"model": "gpt-6"}, "the model"),
        ({"effort": "high"}, "effort"),
    ],
)
async def test_a_create_for_another_agent_model_or_effort_is_refused(
    host,  # noqa: F811
    project,  # noqa: F811
    over,
    why,
):
    spec = _spec(skills=[], **over)
    with pytest.raises(runtime.StructuredError) as e:
        await _create(project, spec)
    assert e.value.status == 422 and why in e.value.detail
    assert store.active() == []


async def test_at_capacity_the_create_starts_no_worker_and_leaves_no_ownership(
    host,  # noqa: F811
    project,  # noqa: F811
):
    from agent_sessions import native_ownership

    spec = _spec(skills=[])
    await _create(project, spec)
    op = ident()
    with pytest.raises(runtime.StructuredError) as e:
        await runtime.create_session(
            ENGINE, str(project), operation_id=op, specialist=(spec["id"], 1)
        )
    assert e.value.status == 409 and "at capacity" in e.value.detail
    assert len(store.active()) == 1
    # The slot is taken before ownership: a refused create leaves no pending ownership behind
    # (an unresolved creation would refuse every console launch of this agent's source).
    assert native_ownership.lookup(f"{ENGINE}:{op}") is None


async def test_a_stop_frees_the_slot_and_its_bundle(host, project):  # noqa: F811
    spec = _spec()
    key = (await _create(project, spec))["session_key"]
    await _first_turn(key)
    [slot] = store.active()
    await runtime.stop(key)
    assert store.get_reservation(slot["reservation_id"])["state"] == "released"
    assert not skill_bundles.path_of(slot["reservation_id"]).exists()


async def test_every_generation_keeps_the_sessions_own_slot_so_a_stop_still_frees_it(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    spec = _spec()
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    key = (await _create(project, spec))["session_key"]
    first = (await _first_turn(key))["native"]["worker"]
    for _ in range(200):
        if native_state.read_lifecycle(first).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 60.0)
    [before] = store.active()
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    snap, turn = await settle(key, nxt)
    assert turn["reply"] == "echo:two" and snap["native"]["worker"] != first
    [after] = store.active()
    # The same slot, renewed (its rev moved, so a reconcile answer from the gap can't free it).
    assert after["reservation_id"] == before["reservation_id"] and after["rev"] > before["rev"]
    assert _config(snap["native"]["worker"])["skills_bundle"].endswith(after["reservation_id"])
    await runtime.stop(key)
    assert store.get_reservation(after["reservation_id"])["state"] == "released"


async def test_a_replay_with_another_specialist_conflicts(host, project):  # noqa: F811
    spec = _spec(skills=[], capacity=2)
    op = ident()
    await runtime.create_session(ENGINE, str(project), operation_id=op, specialist=(spec["id"], 1))
    with pytest.raises(runtime.StructuredError) as e:
        await runtime.create_session(ENGINE, str(project), operation_id=op)
    assert e.value.status == 409
    assert len(store.active()) == 1


async def _released(rid: str, timeout: float = 15.0) -> bool:
    for _ in range(int(timeout / 0.05)):
        if store.get_reservation(rid)["state"] == "released":
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_generation_that_never_becomes_ready_frees_its_slot(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    async def never(_worker_id):
        raise native_runtime.NativeError(503, "the native worker stopped: boom")

    monkeypatch.setattr(native_runtime, "_wait_ready", never)
    spec = _spec()
    # The route's create: it answers at launch, and the readiness waiter settles the failure.
    await _create(project, spec, wait_ready=False)
    [row] = store.active()
    rid = row["reservation_id"]
    assert await _released(rid)
    assert not skill_bundles.path_of(rid).exists()


async def test_a_launch_that_fails_before_any_spawn_frees_its_slot(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(native_state, "write_config", broken)
    spec = _spec()
    with pytest.raises(Exception):  # noqa: B017 — the store error surfaces as the create's own
        await _create(project, spec)
    assert store.active() == []
    assert host.launches == []


async def test_an_archived_specialist_starts_no_further_generation(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    spec = _spec(skills=[])
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    key = (await _create(project, spec))["session_key"]
    first = (await _first_turn(key))["native"]["worker"]
    for _ in range(200):
        if native_state.read_lifecycle(first).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    store.set_archived(spec["id"], True, expect_revision=spec["revision"])
    launches = len(host.launches)
    with pytest.raises(runtime.StructuredError) as e:
        await runtime.submit_turn(key, operation_id=ident(), text="two")
    assert e.value.status == 409 and "archived" in e.value.detail
    assert len(host.launches) == launches


async def test_enabling_skip_permissions_keeps_the_slot_a_stop_frees_it(host, project):  # noqa: F811
    spec = _spec(skills=[])
    key = (await _create(project, spec))["session_key"]
    await _first_turn(key)
    [slot] = store.active()
    sid = key.split(":", 1)[1]
    # The permission restart's stop (`discard=False`) keeps it; the operator's Stop frees it.
    assert native_runtime._stop_sync(ENGINE, sid, discard=False) == "gone"
    assert store.get_reservation(slot["reservation_id"])["state"] != "released"
    await runtime.stop(key)
    assert store.get_reservation(slot["reservation_id"])["state"] == "released"


@pytest.mark.parametrize("broken", ["_entrypoint", "interpreter"])
async def test_a_launch_refused_in_its_setup_frees_its_slot(
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
    broken,
):
    """Review 7104: a refusal anywhere before the spawn (a changed binary's provenance, the
    interpreter) releases the slot the create took — nothing ran for it."""

    def refuse(*_a, **_k):
        raise native_runtime.NativeError(409, "the agent's entrypoint changed")

    monkeypatch.setattr(native_runtime, broken, refuse)
    spec = _spec(skills=[])
    with pytest.raises(runtime.StructuredError):
        await _create(project, spec)
    assert store.active() == []
    assert host.launches == []
