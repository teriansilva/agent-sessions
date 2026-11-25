"""Equipment grants through the REAL API runtime (#1951 slice B): every generation of a session
started from a specialist is issued its own grant set after its slot and before anything is
spawned; a refusal to issue refuses the launch and frees the slot; a renewed generation supersedes
the earlier set; a Stop and a failed generation revoke with the slot's release. `grants.audit()`
holds after every path. Drives the real runtime over the fake worker.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import (
    grants,
    memory,
    mission_approval,
    native_runtime,
    native_state,
    projects,
    secrets_broker,
)
from agent_sessions import specialists_store as store
from agent_sessions import structured_runtime as runtime
from test_native_runtime import host, ident, project, settle  # noqa: F401 — fixtures
from test_specialist_launch_api import _config, _create, _first_turn, _released, _spec

pytestmark = pytest.mark.anyio

ITEM = {"kind": "secret", "id": "v-token", "name": "forgejo_token", "scope": "global"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def offer(monkeypatch):
    items = {"now": [ITEM]}

    def equipment_of(pid):
        out = list(items["now"])
        if memory.enabled(pid):
            out.append({"kind": "memory", "id": pid, "name": "project memory", "scope": "project"})
        return out

    monkeypatch.setattr(mission_approval, "equipment_of", equipment_of)
    return items


def _one_open() -> dict:
    [g] = [g for g in store.grant_sets() if g["revoked_at"] is None]
    return g


async def test_a_generation_is_issued_its_set_after_its_slot(host, project, offer):  # noqa: F811
    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    [slot] = store.active()
    g = _one_open()
    assert g["reservation_id"] == slot["reservation_id"] and g["session_key"] == key
    assert g["project_id"] == pid
    assert {i["kind"] for i in g["items"]} == {"secret", "memory"}
    assert _config(snap["native"]["worker"])["memory_env"]  # memory was issued
    await runtime.stop(key)
    assert store.grant_set(g["set_id"])["revoked_reason"] == "released"
    assert grants.audit() == []


async def test_no_memory_is_bound_unless_the_set_issued_it(host, project, offer, monkeypatch):  # noqa: F811
    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    # The set issues the secret only (as an approval pinned before memory was turned on would).
    monkeypatch.setattr(mission_approval, "equipment_of", lambda p: [ITEM])
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    assert _config(snap["native"]["worker"])["memory_env"] == {}
    await runtime.stop(key)


async def test_a_refusal_to_issue_refuses_the_launch_and_frees_the_slot(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    def refuse(*_a, **_k):
        raise grants.GrantRefused("equipment could not be issued: test")

    monkeypatch.setattr(grants, "issue", refuse)
    with pytest.raises(runtime.StructuredError) as e:
        await _create(project, _spec(skills=[]))
    assert e.value.status == 409 and "equipment" in e.value.detail
    assert store.active() == [] and host.launches == []
    assert grants.audit() == []


async def test_a_failed_spawn_after_issue_revokes_the_set_whole(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(native_state, "write_config", broken)  # after the grants, before a spawn
    with pytest.raises(Exception):  # noqa: B017
        await _create(project, _spec(skills=[]))
    [g] = store.grant_sets()
    assert g["revoked_reason"] == "released" and store.active() == []
    assert grants.audit() == []


async def test_a_generation_that_never_becomes_ready_revokes_its_set(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    async def never(_worker_id):
        raise native_runtime.NativeError(503, "the native worker stopped: boom")

    monkeypatch.setattr(native_runtime, "_wait_ready", never)
    await _create(project, _spec(skills=[]), wait_ready=False)
    [row] = store.active()
    assert await _released(row["reservation_id"])
    [g] = store.grant_sets()
    assert g["revoked_at"] is not None
    assert grants.audit() == []


async def test_each_generation_gets_a_new_set_and_ends_the_last(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    key = (await _create(project, _spec(skills=[])))["session_key"]
    first = (await _first_turn(key))["native"]["worker"]
    for _ in range(200):
        if native_state.read_lifecycle(first).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 60.0)
    g1 = _one_open()
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    await settle(key, nxt)
    g2 = _one_open()
    assert g2["set_id"] != g1["set_id"]
    assert store.grant_set(g1["set_id"])["revoked_reason"] == "superseded"
    await runtime.stop(key)
    assert grants.audit() == []


async def test_a_generations_capability_is_tied_to_its_set(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
    tmp_path,
):
    """The REAL launch mints the generation's secrets capability under ITS grant set
    (`launch_env(grant_set=…)`): marking that set revoked in the ledger alone makes the broker
    refuse the capability at once, whatever the project offers."""
    runtime_root = tmp_path / "secrets-run"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(runtime_root))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(secrets_broker, "_server_running", True)
    monkeypatch.setattr(secrets_broker, "_liveness", lambda conn, cap: secrets_broker.ALIVE)
    projects.create("demo", folders=[str(project)])
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    g = _one_open()
    env = _config(snap["native"]["worker"])["secrets_env"]
    token = open(env[secrets_broker.ENV_TOKEN_FILE]).read()
    assert secrets_broker.handle(token, "list")["ok"] is True
    assert store.revoke_grant_set(g["set_id"], "test")
    assert secrets_broker.handle(token, "list") == {"ok": False, "error": "grant_revoked"}
    await runtime.stop(key)
    assert grants.audit() == []


async def test_a_retaken_launch_failing_before_its_spawn_revokes_its_set(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    """A re-take holds no owner token, so the failed launch's `abandon` frees nothing (reconcile
    settles the slot later); the set this attempt was issued ends at once anyway."""
    from agent_sessions import specialist_launch

    real = native_runtime._specialist_claim

    def retaken(record, adapter):
        first = real(record, adapter)
        v = specialist_launch._version(first.specialist_id, first.version)
        again = specialist_launch._take(first.specialist_id, first.version, first.session_key, v)
        assert again.owner is None
        return again

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(native_runtime, "_specialist_claim", retaken)
    monkeypatch.setattr(native_state, "write_config", broken)  # after the grants, before a spawn
    with pytest.raises(Exception):  # noqa: B017
        await _create(project, _spec(skills=[]))
    [g] = store.grant_sets()
    assert g["revoked_reason"] == "released"
    assert grants.audit() == []


async def test_memory_follows_the_project_the_session_is_in_when_bound(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    """Issued for project A, but the session resolves to B by the time memory is bound: none."""
    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    monkeypatch.setattr(grants, "project_of", lambda key, cwd: "another-project")
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    assert {i["kind"] for i in _one_open()["items"]} >= {"memory"}
    assert _config(snap["native"]["worker"])["memory_env"] == {}
    await runtime.stop(key)


async def test_a_revoked_set_ends_the_generations_memory_broker_access(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
):
    """The memory broker asks the generation's grant set on every call: revoked in the ledger
    alone (the boot sweep's boot_unknown), the running session's memory search ends at once."""
    import json as _json
    from pathlib import Path

    from agent_sessions import memory_broker

    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    monkeypatch.setattr(secrets_broker, "_api_liveness", lambda worker_id: secrets_broker.ALIVE)
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    g = _one_open()
    env = _config(snap["native"]["worker"])["memory_env"]
    assert env, "memory was issued and bound"
    cap_id = Path(env[memory.ENV_INBOX]).name
    token = _json.loads((memory_broker.root() / f"{cap_id}.json").read_text())["token"]
    before = memory_broker.handle({"token": token, "op": "search", "query": "anything"})
    assert before.get("error") != "memory launch is not live", before
    assert store.revoke_grant_set(g["set_id"], "boot_unknown")
    after = memory_broker.handle({"token": token, "op": "search", "query": "anything"})
    assert after == {"ok": False, "error": "memory launch is not live", "status": 403}
    await runtime.stop(key)


@pytest.mark.parametrize("ledger", ["unreadable", "row-gone", "other-project"])
async def test_the_memory_broker_fails_closed_on_the_grant_set(
    host,  # noqa: F811
    project,  # noqa: F811
    offer,
    monkeypatch,
    ledger,
):
    """Unreadable: refused for this call, served again once it reads. Row gone, or a set that did
    not issue THIS project's memory: refused."""
    import json as _json
    import sqlite3
    from pathlib import Path

    from agent_sessions import memory_broker

    pid = projects.create("demo", folders=[str(project)]).id
    memory.set_enabled(pid, True)
    monkeypatch.setattr(secrets_broker, "_api_liveness", lambda worker_id: secrets_broker.ALIVE)
    key = (await _create(project, _spec(skills=[])))["session_key"]
    snap = await _first_turn(key)
    g = _one_open()
    env = _config(snap["native"]["worker"])["memory_env"]
    cap_id = Path(env[memory.ENV_INBOX]).name
    token = _json.loads((memory_broker.root() / f"{cap_id}.json").read_text())["token"]
    ask = {"token": token, "op": "search", "query": "anything"}
    refused = {"ok": False, "error": "memory launch is not live", "status": 403}
    assert memory_broker.handle(ask) != refused
    if ledger == "unreadable":
        read = store.grant_set

        def boom(set_id):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "grant_set", boom)
        assert memory_broker.handle(ask) == refused
        monkeypatch.setattr(store, "grant_set", read)
        assert memory_broker.handle(ask) != refused  # not revoked: served again
    else:
        con = sqlite3.connect(store.db_path())
        if ledger == "row-gone":
            con.execute("DELETE FROM specialist_grants WHERE set_id = ?", (g["set_id"],))
            con.execute("DELETE FROM specialist_grant_sets WHERE set_id = ?", (g["set_id"],))
        else:
            con.execute(
                "UPDATE specialist_grants SET item_id = 'another-project'"
                " WHERE set_id = ? AND kind = 'memory'",
                (g["set_id"],),
            )
        con.commit()
        con.close()
        assert memory_broker.handle(ask) == refused
    await runtime.stop(key)
