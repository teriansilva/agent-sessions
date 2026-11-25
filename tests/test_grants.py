"""Equipment grants (#1951 slice B, D11/D15): issued only after the reservation, narrowed to the
session's project (and to an approval's pin), revoked as a whole with the reservation that settles
them, by the archive teardown and by the boot sweep (which runs before any recovery and revokes
unknown liveness), and refused by the broker the moment they are revoked. `grants.audit()` is the
one invariant, checked after every path here.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import uuid

import pytest

from agent_sessions import grants, mission_approval, secrets_broker, specialist_capacity
from agent_sessions import specialist_launch as launch
from agent_sessions import specialists_store as store
from test_secrets_broker import _bind, _global_file, _global_secret, broker  # noqa: F401

_REAL_EQUIPMENT_OF = mission_approval.equipment_of

ATLAS = [
    {"kind": "secret", "id": "v-token", "name": "forgejo_token", "scope": "global"},
    {"kind": "secret_file", "id": "v-key", "name": "asc_api_key", "scope": "global"},
    {"kind": "memory", "id": "atlas", "name": "project memory", "scope": "project"},
]


@pytest.fixture(autouse=True)
def _no_shared_pass_wait(monkeypatch):
    """Each `ensure_swept` call here is its own pass (production shares one per SWEEP_RETRY_S)."""
    monkeypatch.setattr(grants, "SWEEP_RETRY_S", 0.0)


def _k(label: str) -> str:
    return f"claude:{uuid.uuid5(uuid.NAMESPACE_DNS, label)}"


def _spec(capacity: int = 2) -> dict:
    return store.create(
        {
            "title": "Implementer",
            "summary": "",
            "skills": [],
            "engine": "claude",
            "model": "claude-opus-5",
            "effort": None,
            "capacity": capacity,
        }
    )


@pytest.fixture
def equipment(monkeypatch):
    """Project resolution and the library's offer by table; no step run unless a test adds one."""
    offer = {"atlas": list(ATLAS), "beta": [ATLAS[0] | {"id": "v-beta"}]}
    where = {}
    monkeypatch.setattr(grants, "_project_of", lambda key, cwd: where.get(key, "atlas"))
    monkeypatch.setattr(
        mission_approval,
        "equipment_of",
        lambda pid: None if offer.get(pid) == "unreadable" else list(offer.get(pid, [])),
    )
    from agent_sessions import missions

    runs: dict = {}
    approvals: dict = {}
    monkeypatch.setattr(missions, "step_run_of_session", lambda key, **_: runs.get(key))
    monkeypatch.setattr(missions, "approval", lambda mid, **_: approvals.get(mid))
    events: list = []
    monkeypatch.setattr(
        missions, "append_event", lambda mid, kind, **kw: events.append((mid, kind, kw)) or 1
    )

    def lost_access_row(mid, *, session_key, grant_set, text, meta, **_):
        # The store's contract (pinned by its own test): one row per grant set.
        if any(e[2]["meta"].get("grant_set") == grant_set for e in events):
            return False
        meta = {**meta, "access": "revoked", "grant_set": grant_set}
        events.append((mid, "session", {"session_key": session_key, "text": text, "meta": meta}))
        return True

    monkeypatch.setattr(missions, "ensure_lost_access_event", lost_access_row)
    return {"offer": offer, "where": where, "runs": runs, "approvals": approvals, "events": events}


def _take(spec, label, **kw) -> launch.Claim:
    return launch.for_new("claude", _k(label), spec["id"], 1, model="claude-opus-5", **kw)


def _clean() -> None:
    assert grants.audit() == []


# ---- the ledger: issued after the reservation, narrowed --------------------------------------


def test_a_set_is_issued_under_the_reservation_with_the_projects_equipment(equipment):
    spec = _spec()
    claim = _take(spec, "a")
    g = grants.issue(claim, claim.session_key, "/w")
    assert g["reservation_id"] == claim.reservation_id and g["issued_rev"] == claim.rev
    assert sorted(i["name"] for i in g["items"]) == sorted(i["name"] for i in ATLAS)
    assert g["project_id"] == "atlas" and g["revoked_at"] is None
    assert grants.granted_memory(g, "atlas") is True
    assert grants.granted_memory(g, "beta") is False  # moved since its set was issued
    assert grants.granted_memory(g, None) is False
    _clean()


def test_never_another_projects_equipment_and_nothing_outside_a_project(equipment):
    spec = _spec()
    beta = _take(spec, "b")
    equipment["where"][beta.session_key] = "beta"
    g = grants.issue(beta, beta.session_key, "/w")
    assert [i["id"] for i in g["items"]] == ["v-beta"]
    loose = _take(spec, "c")
    equipment["where"][loose.session_key] = None
    g2 = grants.issue(loose, loose.session_key, "/w")
    assert g2["items"] == [] and g2["project_id"] is None
    assert grants.granted_memory(g2, None) is False


def test_an_unreadable_library_issues_nothing(equipment):
    equipment["offer"]["atlas"] = "unreadable"
    claim = _take(_spec(), "a")
    assert grants.issue(claim, claim.session_key, "/w")["items"] == []


def test_an_approval_bound_step_gets_at_most_its_pin(equipment):
    spec = _spec(capacity=3)
    claim = _take(spec, "a")
    equipment["runs"][claim.session_key] = {
        "mission_id": "m1",
        "step_id": "o1.cv",
        "episode": 1,
        "authority": "approval",
        "approval_id": "apr-1",
    }
    equipment["approvals"]["m1"] = {
        "id": "apr-1",
        "revoked_at": None,
        "pins": {"equipment": {"atlas": [ATLAS[0]]}},
    }
    g = grants.issue(claim, claim.session_key, "/w")
    assert [i["name"] for i in g["items"]] == ["forgejo_token"]  # added since: never issued
    assert g["approval_id"] == "apr-1" and grants.granted_memory(g, "atlas") is False
    # A superseded or revoked approval pins nothing: fail closed. The approval in force pins
    # EVERYTHING here, so only the check that the run's approval is that one keeps it out.
    everything = {"equipment": {"atlas": list(ATLAS)}}
    other = _take(spec, "b")
    equipment["runs"][other.session_key] = equipment["runs"][claim.session_key]
    equipment["approvals"]["m1"] = {"id": "apr-2", "revoked_at": None, "pins": everything}
    assert grants.issue(other, other.session_key, "/w")["items"] == []
    third = _take(spec, "c")
    equipment["runs"][third.session_key] = equipment["runs"][claim.session_key]
    equipment["approvals"]["m1"] = {"id": "apr-1", "revoked_at": 1.0, "pins": everything}
    assert grants.issue(third, third.session_key, "/w")["items"] == []


@pytest.mark.parametrize("unreadable", ["run", "approval"])
def test_an_unreadable_run_or_approval_issues_nothing(equipment, monkeypatch, unreadable):
    """Whether a session is bound to an approval, or what that approval pins, can't be read: it
    is treated as bound with nothing pinned (never the project's whole offer)."""
    from agent_sessions import missions

    claim = _take(_spec(), "a")
    equipment["runs"][claim.session_key] = {
        "mission_id": "m1",
        "step_id": "o1.cv",
        "episode": 1,
        "authority": "approval",
        "approval_id": "apr-1",
    }
    equipment["approvals"]["m1"] = {
        "id": "apr-1",
        "revoked_at": None,
        "pins": {"equipment": {"atlas": list(ATLAS)}},
    }

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    name = "step_run_of_session" if unreadable == "run" else "approval"
    monkeypatch.setattr(missions, name, boom)
    assert grants.issue(claim, claim.session_key, "/w")["items"] == []


def test_an_operator_started_step_gets_the_projects_offer(equipment):
    claim = _take(_spec(), "a")
    equipment["runs"][claim.session_key] = {"authority": "operator", "approval_id": None}
    assert len(grants.issue(claim, claim.session_key, "/w")["items"]) == len(ATLAS)


def test_a_refused_reservation_leaves_zero_grant_rows(equipment):
    spec = _spec(capacity=1)
    _take(spec, "a")
    with pytest.raises(launch.Refused):
        _take(spec, "b")
    assert [g["session_key"] for g in store.grant_sets()] == []


def test_two_launches_for_the_last_slot_leave_the_loser_with_no_grants(equipment):
    spec = _spec(capacity=1)
    out: dict = {}
    barrier = threading.Barrier(2)

    def go(label):
        barrier.wait()
        try:
            c = _take(spec, label)
            out[label] = grants.issue(c, c.session_key, "/w")
        except launch.Refused as exc:
            out[label] = exc

    ts = [threading.Thread(target=go, args=(n,)) for n in ("x", "y")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    winners = [v for v in out.values() if isinstance(v, dict)]
    assert len(winners) == 1
    assert [g["set_id"] for g in store.grant_sets()] == [winners[0]["set_id"]]
    _clean()


def test_a_slot_changed_since_the_launch_took_it_issues_nothing(equipment):
    spec = _spec()
    claim = _take(spec, "a")
    # A re-take of the same session (a double tap) moves `rev`: the earlier attempt issues nothing.
    store.reserve(spec["id"], 1, claim.session_key, max_instances=6)
    with pytest.raises(grants.GrantRefused):
        grants.issue(claim, claim.session_key, "/w")
    assert store.grant_sets() == []


def test_a_set_is_issued_only_to_a_starting_slot_of_its_own_session(equipment):
    claim = _take(_spec(), "a")
    with pytest.raises(store.GrantRefused):  # another session's key, at the right rev
        store.issue_grants(
            claim.reservation_id,
            rev=claim.rev,
            session_key=_k("someone-else"),
            project_id="atlas",
            approval_id=None,
            items=[],
        )
    con = sqlite3.connect(store.db_path())  # live at the same rev (no path does this; the guard)
    con.execute(
        "UPDATE specialist_reservations SET state = 'live' WHERE reservation_id = ?",
        (claim.reservation_id,),
    )
    con.commit()
    con.close()
    with pytest.raises(grants.GrantRefused):
        grants.issue(claim, claim.session_key, "/w")
    assert store.grant_sets() == []


def test_a_set_that_can_not_be_read_back_is_revoked_and_the_launch_refused(equipment, monkeypatch):
    spec = _spec()
    _take(spec, "a")
    retake = _take(spec, "a")  # no owner token: nothing else could revoke what it was issued
    monkeypatch.setattr(store, "grant_set", lambda set_id: None)
    with pytest.raises(grants.GrantRefused):
        grants.issue(retake, retake.session_key, "/w")
    [g] = store.grant_sets()
    assert g["revoked_reason"] == "released"


def test_settling_a_slot_never_rereads_every_set_it_ever_had(equipment, monkeypatch):
    """A long-lived API session renews one slot per generation. Deleting the superseded copies
    asks the broker ONCE per settlement, with set ids only (no items read), so the launch path
    doesn't slow down with every generation."""
    spec = _spec()
    claim, _g = _issued(spec, "a")
    for _ in range(5):
        claim = launch.for_generation(
            claim.session_key,
            {"id": spec["id"], "version": 1},
            {"reservation_id": claim.reservation_id, "owner": claim.owner},
        )
        grants.issue(claim, claim.session_key, "/w")
    calls = []
    real_revoke, real_sets = secrets_broker.revoke_grant_sets, store.grant_sets
    monkeypatch.setattr(secrets_broker, "revoke_grant_sets", lambda ids: calls.append(ids) or 0)

    def no_full_read(**kw):
        raise AssertionError("after_release read the sets with their items")

    monkeypatch.setattr(store, "grant_sets", no_full_read)
    launch.for_generation(
        claim.session_key,
        {"id": spec["id"], "version": 1},
        {"reservation_id": claim.reservation_id, "owner": claim.owner},
    )
    assert len(calls) == 1 and len(calls[0]) == 6  # every revoked set, in one call
    monkeypatch.setattr(secrets_broker, "revoke_grant_sets", real_revoke)
    monkeypatch.setattr(store, "grant_sets", real_sets)
    launch.abandon(claim)
    _clean()


def test_a_grant_write_failing_mid_set_leaves_no_row(equipment, monkeypatch):
    claim = _take(_spec(), "a")
    seen = []

    def boom(kind, item_id, name):
        seen.append(name)
        if len(seen) == 2:
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_before_grant_item", boom)
    with pytest.raises(grants.GrantRefused):
        grants.issue(claim, claim.session_key, "/w")
    assert store.grant_sets() == [] and len(seen) == 2
    launch.abandon(claim)
    _clean()


# ---- revocation at the reservation's own settlement points -----------------------------------


def _issued(spec, label, equipment=None):
    claim = _take(spec, label)
    return claim, grants.issue(claim, claim.session_key, "/w")


def _state(set_id):
    return store.grant_set(set_id)["revoked_reason"] or "open"


def test_a_failed_launch_after_the_reservation_revokes_its_set_whole(equipment):
    """Spawn error / cancel during launch: the launch's `finally` abandons the claim."""
    claim, g = _issued(_spec(), "a")
    launch.abandon(claim)
    assert _state(g["set_id"]) == "released"
    _clean()


def test_a_natural_end_or_a_proved_stop_revokes_with_the_release(equipment):
    claim, g = _issued(_spec(), "a")
    launch.release_slot({"reservation_id": claim.reservation_id, "owner": claim.owner})
    assert _state(g["set_id"]) == "released"
    _clean()


def test_reconcile_proving_the_session_dead_revokes(equipment, monkeypatch):
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)

    async def dead(key):
        return specialist_capacity.DEAD, "gone"

    monkeypatch.setattr(specialist_capacity, "liveness", dead)
    asyncio.run(specialist_capacity.reconcile())
    assert _state(g["set_id"]) == "reclaimed"
    _clean()


def test_the_operator_releasing_a_held_slot_revokes(equipment):
    claim, g = _issued(_spec(), "a")
    r = store.get_reservation(claim.reservation_id)
    assert store.hold(claim.reservation_id, "unknown", seen_rev=r["rev"])
    r = store.get_reservation(claim.reservation_id)
    assert specialist_capacity.release_held(claim.reservation_id, seen_rev=r["rev"])
    assert _state(g["set_id"]) == "released"
    _clean()


def test_a_new_generation_or_a_retake_supersedes_the_earlier_set(equipment):
    spec = _spec()
    claim, g1 = _issued(spec, "a")
    again = launch.for_generation(
        claim.session_key,
        {"id": spec["id"], "version": 1},
        {"reservation_id": claim.reservation_id, "owner": claim.owner},
    )
    assert again.reservation_id == claim.reservation_id and again.rev > claim.rev
    assert _state(g1["set_id"]) == "superseded"
    g2 = grants.issue(again, again.session_key, "/w")
    assert _state(g2["set_id"]) == "open"
    retake = _take(spec, "a")  # a re-take of the same session
    assert retake.owner is None and _state(g2["set_id"]) == "superseded"
    _clean()


def test_archive_revokes_the_sessions_sets(equipment):
    claim, g = _issued(_spec(), "a")
    assert grants.revoke_session([claim.session_key]) == [g["set_id"]]
    assert _state(g["set_id"]) == "archived"
    _clean()


# ---- the broker asks the set on every call ----------------------------------------------------


def _cap_for(g, key):
    env = secrets_broker.launch_env(
        key, key, "/work", engine="claude", kind="api", grant_set=g["set_id"]
    )
    return env, open(env[secrets_broker.ENV_TOKEN_FILE]).read()


def _broker_world(broker, monkeypatch, equipment):  # noqa: F811
    _global_secret()
    _global_file()
    _bind("atlas", "forgejo_token", "secret")
    _bind("atlas", "asc_api_key", "secret_file")
    # The library's REAL offer, with the records' real ids: the broker serves a grant only for the
    # very record it names, so a made-up id would (rightly) be refused.
    equipment["offer"]["atlas"] = _REAL_EQUIPMENT_OF("atlas")
    spec = _spec()
    claim = _take(spec, "a1")
    broker["projects"][claim.session_key] = "atlas"
    broker["live"].add(claim.session_key)
    return spec, claim


def test_the_broker_offers_only_what_the_set_issued(broker, monkeypatch, equipment):  # noqa: F811
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    real = {i["name"]: i for i in equipment["offer"]["atlas"]}
    equipment["offer"]["atlas"] = [real["forgejo_token"]]  # the set issues the token only
    g = grants.issue(claim, claim.session_key, "/w")
    assert [(i["id"], i["scope"]) for i in g["items"]] == [(real["forgejo_token"]["id"], "global")]
    _env, token = _cap_for(g, claim.session_key)
    listing = secrets_broker.handle(token, "list")
    assert [s["name"] for s in listing["secrets"]] == ["forgejo_token"]
    assert secrets_broker.handle(token, "path", "asc_api_key") == {
        "ok": False,
        "error": "not_granted",
    }
    assert secrets_broker.handle(token, "path", "forgejo_token")["ok"] is True


@pytest.mark.parametrize(
    "drift",
    [
        {"id": "some-other-record"},
        {"scope": "project"},
        {"kind": "secret_file"},
        "rebound",
    ],
    ids=["other-id", "other-scope", "other-kind", "rebound-after-issue"],
)
def test_the_broker_serves_a_grant_only_for_the_record_it_names(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
    drift,
):
    """D-P6-5: a name is not a grant. A set that issued `forgejo_token` as ANOTHER record (id,
    scope or kind), or a name the operator rebound to another record after issue, is refused
    `not_granted`, never the record the project holds under that name now."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    [real] = [i for i in _REAL_EQUIPMENT_OF("atlas") if i["name"] == "forgejo_token"]
    issued = real if drift == "rebound" else real | drift
    equipment["offer"]["atlas"] = [issued]
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    if drift == "rebound":
        assert secrets_broker.handle(token, "path", "forgejo_token")["ok"] is True
        offered = secrets_broker._offered

        def rebound(pid):
            out, refused = offered(pid)
            out["forgejo_token"] = out["forgejo_token"] | {"id": "v-rebound"}
            return out, refused

        monkeypatch.setattr(secrets_broker, "_offered", rebound)
    assert secrets_broker.handle(token, "path", "forgejo_token") == {
        "ok": False,
        "error": "not_granted",
    }
    assert secrets_broker.handle(token, "list")["secrets"] == []


def test_an_unreadable_ledger_refuses_the_call_and_revokes_nothing(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
):
    """Fail closed for THIS call (never the project's whole offer), and a transient read error is
    not proof of revocation: the capability serves again once the ledger reads."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    read = store.grant_set

    def unreadable(set_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "grant_set", unreadable)
    narrowed = []
    real_narrow = secrets_broker._narrow
    monkeypatch.setattr(secrets_broker, "_narrow", lambda *a: narrowed.append(1) or real_narrow(*a))
    assert secrets_broker.handle(token, "path", "forgejo_token") == {
        "ok": False,
        "error": "store_unavailable",
    }
    assert secrets_broker.handle(token, "list") == {"ok": False, "error": "store_unavailable"}
    secrets_broker.reconcile(probe=False)  # the broker's pass revokes nothing on it either
    assert narrowed == []  # refused as unreadable, never by an accident further down
    monkeypatch.setattr(store, "grant_set", read)
    assert secrets_broker.handle(token, "path", "forgejo_token")["ok"] is True
    _clean()


def test_a_capability_whose_set_row_is_gone_is_refused_not_given_the_projects_offer(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
):
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    con = sqlite3.connect(store.db_path())
    con.execute("DELETE FROM specialist_grants WHERE set_id = ?", (g["set_id"],))
    con.execute("DELETE FROM specialist_grant_sets WHERE set_id = ?", (g["set_id"],))
    con.commit()
    con.close()
    assert secrets_broker.handle(token, "path", "forgejo_token") == {
        "ok": False,
        "error": "grant_revoked",
    }
    launch.abandon(claim)


def test_a_revoked_set_is_refused_at_once_and_its_copies_go(broker, monkeypatch, equipment):  # noqa: F811
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    # Marked revoked in the ledger only (the copies not yet deleted): the broker refuses already.
    assert store.revoke_grant_set(g["set_id"], "test")
    assert secrets_broker.handle(token, "path", "forgejo_token")["error"] == "grant_revoked"
    import os

    assert not os.path.exists(path)
    _clean()


def test_the_broker_pass_finishes_a_revocation_whose_cleanup_failed(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
):
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    secrets_broker.handle(token, "path", "forgejo_token")
    monkeypatch.setattr(secrets_broker, "revoke_grant_sets", lambda set_ids: 0)  # cleanup "fails"
    launch.abandon(claim)
    assert grants.audit() != []  # the copy is still there…
    secrets_broker.reconcile(probe=False)
    _clean()  # …until the broker's own pass


def test_a_release_deletes_the_copies_made_for_its_sets_at_once(broker, monkeypatch, equipment):  # noqa: F811
    """`grants.after_release`: the slot's release revokes its sets in its transaction, and the
    copies BattleLab materialized for them go in that same call, not at the broker's next pass."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    import os

    assert os.path.exists(path)
    launch.abandon(claim)
    assert not os.path.exists(path)
    _clean()


@pytest.mark.parametrize("how", ["renew", "retake"])
def test_a_renew_or_retake_deletes_the_superseded_sets_copies_at_once(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
    how,
):
    """The renew / re-take revokes the earlier set in its transaction, and the copies made for it
    go right after: an earlier attempt that may still run can't read them off disk."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    import os

    if how == "renew":
        launch.for_generation(
            claim.session_key,
            {"id": spec["id"], "version": 1},
            {"reservation_id": claim.reservation_id, "owner": claim.owner},
        )
    else:
        assert _take(spec, "a1").owner is None
    assert _state(g["set_id"]) == "superseded"
    assert not os.path.exists(path)
    assert grants.audit() == []


@pytest.mark.parametrize("branch", ["released", "probed"])
def test_the_boot_sweep_deletes_the_copies_of_what_it_revokes(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
    probe,
    branch,
):
    """Both revoking branches of the sweep delete the set's copies in the same pass (a retried
    set's session may still run and could read a copy's path directly)."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]
    import os

    if branch == "released":  # an older build released the slot without revoking (v2)
        con = sqlite3.connect(store.db_path())
        con.execute(
            "UPDATE specialist_reservations SET state = 'released' WHERE reservation_id = ?",
            (claim.reservation_id,),
        )
        con.commit()
        con.close()
    else:
        monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
        probe[claim.session_key] = specialist_capacity.DEAD
    assert asyncio.run(grants.sweep())["revoked"] == 1
    assert not os.path.exists(path)
    assert grants.audit() == []


def _open_sets() -> list[str]:
    return [g["set_id"] for g in store.grant_sets(open_only=True)]


def test_the_archive_teardown_revokes_the_sessions_sets(equipment, monkeypatch):
    from agent_sessions import runtime_cleanup
    from test_runtime_cleanup import _wire

    claim, g = _issued(_spec(), "a")
    _wire(monkeypatch, phys=claim.session_key, outcome="gone")
    native = claim.session_key.partition(":")[2]
    asyncio.run(runtime_cleanup.cleanup_runtime("claude", native, physical_key=claim.session_key))
    assert _state(g["set_id"]) == "archived" and _open_sets() == []


def test_a_proved_api_containment_revokes_the_sessions_sets(equipment, monkeypatch):
    """`contain_native` (archive of an API session): the worker is proved gone, so its sets end,
    whatever the stop's own slot release could reach."""
    import types

    from agent_sessions import runtime_cleanup, structured_runtime

    claim, g = _issued(_spec(), "a")
    api = types.SimpleNamespace(manifest=types.SimpleNamespace(runtime="api"))
    monkeypatch.setattr(runtime_cleanup.engines, "get_any", lambda engine: api)

    async def stopped(key):
        return {"containment": "gone"}

    monkeypatch.setattr(structured_runtime, "stop", stopped)
    native = claim.session_key.partition(":")[2]
    asyncio.run(runtime_cleanup.contain_native("claude", native))
    assert _state(g["set_id"]) == "archived" and _open_sets() == []


def test_a_proved_stop_of_a_retaken_slot_revokes_its_sets(equipment):
    """A re-take holds no owner token, so the stop's slot release frees nothing (reconcile
    settles the slot on its own proof); the stop's proof that nothing runs still ends its
    equipment."""
    from agent_sessions import native_runtime

    spec = _spec()
    first = _take(spec, "a")
    retake = _take(spec, "a")
    assert retake.owner is None
    g = grants.issue(retake, retake.session_key, "/w")
    record = {
        "session_key": retake.session_key,
        "specialist_slot": {"reservation_id": retake.reservation_id, "owner": retake.owner},
    }
    native_runtime._release_specialist_slot(record)
    assert _state(g["set_id"]) == "stopped"
    assert store.get_reservation(first.reservation_id)["state"] != "released"  # reconcile's
    _clean()


def test_an_unreadable_ledger_never_stops_the_cleanup_of_a_dead_launch(
    broker,  # noqa: F811
    monkeypatch,
    equipment,
):
    """The broker's pass: an unreadable grant ledger decides nothing about the grant, but the
    launch's death still revokes its capability and deletes its copies."""
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    path = secrets_broker.handle(token, "path", "forgejo_token")["path"]

    def unreadable(set_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "grant_set", unreadable)
    broker["live"].discard(claim.session_key)
    secrets_broker.reconcile(probe=True)
    import os

    assert not os.path.exists(path)
    launch.abandon(claim)


def test_a_launch_without_a_ledger_keeps_the_projects_offer(broker, monkeypatch, equipment):  # noqa: F811
    _global_secret()
    _bind("atlas", "forgejo_token", "secret")
    env = secrets_broker.launch_env("claude:a1", "claude:a1", "/w", engine="claude", kind="api")
    token = open(env[secrets_broker.ENV_TOKEN_FILE]).read()
    assert secrets_broker.handle(token, "path", "forgejo_token")["ok"] is True


# ---- the boot sweep ---------------------------------------------------------------------------


@pytest.fixture
def probe(monkeypatch):
    answers: dict = {}

    async def liveness(key):
        a = answers.get(key, specialist_capacity.UNKNOWN)
        if callable(a):
            a = a()
        return a, ""

    monkeypatch.setattr(specialist_capacity, "liveness", liveness)
    return answers


def test_unknown_liveness_is_revoked_and_raises_one_decision(equipment, probe, monkeypatch):
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)  # the old app
    equipment["runs"][claim.session_key] = {
        "mission_id": "m1",
        "step_id": "o1.cv",
        "episode": 2,
        "authority": "approval",
        "approval_id": "apr-1",
    }
    counts = asyncio.run(grants.sweep())
    assert counts["revoked"] == 1 and counts["announced"] == 1
    assert _state(g["set_id"]) == "boot_unknown"
    [(mid, kind, kw)] = equipment["events"]
    assert (mid, kind) == ("m1", "session") and kw["meta"]["access"] == "revoked"
    assert "o1.cv" in kw["text"] and "secret forgejo_token" in kw["text"]
    assert "not stopped" in kw["text"]
    [lost] = grants.lost_access([claim.session_key])
    assert lost["set_id"] == g["set_id"]
    # Once per set: a second sweep (another restart) raises nothing new.
    asyncio.run(grants.sweep())
    assert len(equipment["events"]) == 1
    _clean()


def test_a_dead_session_is_revoked_silently_and_a_live_one_kept(equipment, probe, monkeypatch):
    spec = _spec()
    dead, gd = _issued(spec, "dead")
    live, gl = _issued(spec, "live")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    probe[dead.session_key] = specialist_capacity.DEAD
    probe[live.session_key] = specialist_capacity.LIVE
    counts = asyncio.run(grants.sweep())
    assert (counts["revoked"], counts["kept"], counts["announced"]) == (1, 1, 0)
    assert _state(gd["set_id"]) == "boot_dead" and _state(gl["set_id"]) == "open"
    assert equipment["events"] == []
    _clean()


def test_a_launch_under_way_in_a_live_process_is_left_to_its_launch(equipment, probe):
    claim, g = _issued(_spec(), "a")  # this process, just now
    counts = asyncio.run(grants.sweep())
    assert counts["undecided"] == 1 and _state(g["set_id"]) == "open"
    assert counts["retry"] == [g["set_id"]]  # left for now, decided again by the retry


def test_a_young_launch_the_sweep_can_not_judge_is_decided_once_its_grace_is_past(
    equipment, probe, monkeypatch
):
    """A `starting` slot of a process that never reads gone (another PID namespace) is left to
    its launch only within the grace; the retry probes it after, and unknown revokes."""
    import time as _time

    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.UNKNOWN_PROCESS)
    counts = asyncio.run(grants.sweep())
    assert counts["retry"] == [g["set_id"]] and _state(g["set_id"]) == "open"
    later = _time.time() + grants.STARTING_GRACE_S + 1
    counts = asyncio.run(grants.sweep(now=later, only=set(counts["retry"])))
    assert counts["retry"] == [] and _state(g["set_id"]) == "boot_unknown"
    _clean()


def test_a_released_reservation_with_an_open_set_is_revoked(equipment, probe):
    claim, g = _issued(_spec(), "a")
    con = sqlite3.connect(store.db_path())  # an older build released it without revoking (v2)
    con.execute(
        "UPDATE specialist_reservations SET state = 'released' WHERE reservation_id = ?",
        (claim.reservation_id,),
    )
    con.commit()
    con.close()
    assert grants.audit() != []
    asyncio.run(grants.sweep())
    assert _state(g["set_id"]) == "released"
    _clean()


def test_a_launch_racing_the_sweep_wins_with_its_newer_state(equipment, probe, monkeypatch):
    """The sweep reads the reservation, then a new generation renews it before the sweep settles:
    its compare-and-set on the `rev` it read refuses, and the new launch's set stands."""
    spec = _spec()
    claim, g1 = _issued(spec, "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    after: dict = {}

    def renew_meanwhile():
        c = launch.for_generation(
            claim.session_key,
            {"id": spec["id"], "version": 1},
            {"reservation_id": claim.reservation_id, "owner": claim.owner},
        )
        after["g"] = grants.issue(c, c.session_key, "/w")
        return specialist_capacity.UNKNOWN

    probe[claim.session_key] = renew_meanwhile
    counts = asyncio.run(grants.sweep())
    assert counts["revoked"] == 0 and counts["skipped"] == 1
    assert _state(g1["set_id"]) == "superseded"  # by the renew, not by the sweep
    assert _state(after["g"]["set_id"]) == "open"
    _clean()


def test_a_launch_going_live_during_the_sweep_keeps_its_set(equipment, probe, monkeypatch):
    """The sweep reads the reservation, then the launch goes live (reconcile's `mark_live`)
    before the sweep settles: that moves `rev` WITHOUT superseding the set, so only the sweep's
    compare-and-set on the `rev` it read keeps an older unknown from revoking a live session. The
    set is decided again at its new `rev` (the retry), where it answers live."""
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)

    def live_meanwhile():
        r = store.get_reservation(claim.reservation_id)
        assert store.mark_live(claim.reservation_id, seen_rev=r["rev"])
        return specialist_capacity.UNKNOWN

    probe[claim.session_key] = live_meanwhile
    counts = asyncio.run(grants.sweep())
    assert (counts["revoked"], counts["undecided"]) == (0, 1)
    assert counts["retry"] == [g["set_id"]] and _state(g["set_id"]) == "open"
    assert store.get_reservation(claim.reservation_id)["state"] == "live"
    probe[claim.session_key] = specialist_capacity.LIVE
    counts = asyncio.run(grants.sweep(only=set(counts["retry"])))
    assert (counts["kept"], counts["retry"]) == (1, []) and _state(g["set_id"]) == "open"
    _clean()


def test_a_hold_during_the_sweep_leaves_the_set_to_the_retry_not_kept(
    equipment, probe, monkeypatch
):
    """Capacity reconcile HOLDS the slot (its own unknown answer) between the sweep's read and its
    settle: `rev` moved, the set is still open, and the sweep is not done until the retry decides
    it at the new `rev`, where unknown liveness revokes."""
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)

    def held_meanwhile():
        r = store.get_reservation(claim.reservation_id)
        assert store.hold(claim.reservation_id, "unknown", seen_rev=r["rev"])
        return specialist_capacity.UNKNOWN

    probe[claim.session_key] = held_meanwhile
    _restart(monkeypatch)
    asyncio.run(grants.ensure_swept())
    assert not grants.swept() and _state(g["set_id"]) == "open"
    probe[claim.session_key] = specialist_capacity.UNKNOWN
    asyncio.run(grants.ensure_swept())
    assert grants.swept() and _state(g["set_id"]) == "boot_unknown"
    _clean()


def test_a_set_whose_reservation_row_is_gone_is_revoked(equipment, probe):
    claim, g = _issued(_spec(), "a")
    con = sqlite3.connect(store.db_path())
    con.execute(
        "DELETE FROM specialist_reservations WHERE reservation_id = ?", (claim.reservation_id,)
    )
    con.commit()
    con.close()
    counts = asyncio.run(grants.sweep())
    assert counts["revoked"] == 1 and _state(g["set_id"]) == "released"
    _clean()


def test_store_work_past_the_budget_is_left_to_the_retry(equipment, probe, monkeypatch):
    """The deadline covers the sweep's store work too: a store that hangs leaves the set to the
    retry rather than stalling boot."""
    import time as _time

    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 0.1)
    monkeypatch.setattr(grants, "SWEEP_STORE_GRACE_S", 0.2)
    read = store.get_reservation

    def slow(rid):
        _time.sleep(1.5)
        return read(rid)

    monkeypatch.setattr(store, "get_reservation", slow)

    async def bounded():
        t0 = _time.monotonic()
        out = await grants.sweep()
        return out, _time.monotonic() - t0

    counts, took = asyncio.run(bounded())
    assert counts["retry"] == [g["set_id"]] and counts["failed"] == 1
    assert took < 1.2


def test_a_decision_that_failed_to_write_is_raised_by_the_next_pass(equipment, probe, monkeypatch):
    """The set is revoked; the thread row fails to write. The sweep is still COMPLETE (the
    revocation is the security property); the decision stays owed (marked only after the row is
    written) and a later pass raises it, once."""
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    equipment["runs"][claim.session_key] = {
        "mission_id": "m1",
        "step_id": "o1.cv",
        "episode": 1,
        "authority": "approval",
        "approval_id": "apr-1",
    }
    from agent_sessions import missions

    write = missions.ensure_lost_access_event

    def busy(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(missions, "ensure_lost_access_event", busy)
    counts = asyncio.run(grants.sweep())
    assert (counts["revoked"], counts["announced"], counts["unannounced"]) == (1, 0, 1)
    assert (counts["failed"], counts["retry"]) == (0, [])
    assert store.grant_set(g["set_id"])["announced_at"] is None
    monkeypatch.setattr(missions, "ensure_lost_access_event", write)
    counts = asyncio.run(grants.sweep(only=set()))  # what `ensure_swept` runs for owed decisions
    assert (counts["announced"], counts["unannounced"]) == (1, 0)
    asyncio.run(grants.sweep())
    assert [e[2]["meta"]["grant_set"] for e in equipment["events"]] == [g["set_id"]]
    _clean()


def test_a_decision_for_an_archived_mission_is_settled_not_retried(equipment, probe, monkeypatch):
    """Its mission refuses the row (archived, or being archived): nobody to tell. The set is
    marked and the sweep is done, not retried on every pass forever."""
    claim, g = _issued(_spec(), "a")
    _restart(monkeypatch)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    equipment["runs"][claim.session_key] = {"mission_id": "m1", "step_id": "o1.cv", "episode": 1}
    from agent_sessions import missions

    def archiving(*a, **kw):  # transient: retried, not marked
        raise missions.MissionError("mission m1: an archive change is in progress", status=409)

    monkeypatch.setattr(missions, "ensure_lost_access_event", archiving)
    assert asyncio.run(grants.ensure_swept()) is True  # complete: a notice never gates recovery
    assert store.grant_set(g["set_id"])["announced_at"] is None  # owed

    def archived(*a, **kw):  # final: nobody to tell
        raise missions.MissionError("mission m1 is archived", status=410)

    monkeypatch.setattr(missions, "ensure_lost_access_event", archived)
    asyncio.run(grants.ensure_swept())
    assert grants.swept() and _state(g["set_id"]) == "boot_unknown"
    assert store.grant_set(g["set_id"])["announced_at"] is not None
    _clean()


def test_callers_share_a_failed_pass_instead_of_reprobing_at_once(monkeypatch):
    calls = _failing_sweep(monkeypatch, fails=99)
    monkeypatch.setattr(grants, "SWEEP_RETRY_S", 60.0)
    assert asyncio.run(grants.ensure_swept()) is False
    assert asyncio.run(grants.ensure_swept()) is False
    assert len(calls) == 1  # the second caller shared the first pass


def test_a_store_newer_than_this_build_settles_the_sweep_and_serves_no_grant(monkeypatch):
    """Nothing pre-boot can be served from a store this build can't read: the gate does not
    hold the supervisor, automations and recovery hostage to it."""

    def newer(**kw):
        raise store.StoreUnsupported(99)

    monkeypatch.setattr(store, "grant_sets", newer)
    assert asyncio.run(grants.ensure_swept()) is True


def test_a_dispatch_recovery_deferred_for_the_sweep_runs_once_it_completed(monkeypatch):
    from agent_sessions import mission_dispatch_recover, missions

    order: list = []
    monkeypatch.setattr(missions, "unsettled_dispatches", lambda **_: order.append("recover") or [])
    monkeypatch.setattr(mission_dispatch_recover, "_recovery_deferred", False)
    _failing_sweep(monkeypatch, fails=1)
    asyncio.run(mission_dispatch_recover.recover_once())
    assert order == []
    asyncio.run(mission_dispatch_recover.recover_deferred())  # the sweep completes on its pass
    assert order == ["recover"]
    asyncio.run(mission_dispatch_recover.recover_deferred())
    assert order == ["recover"]  # once


def test_a_failed_mark_after_the_row_never_writes_a_second_row(probe, monkeypatch):
    """The durable mission store: the row commits, then the mark fails (or the app dies before
    it). The next pass finds the row by its grant set and only marks it: ONE row, ever."""
    from agent_sessions import missions

    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    run = {"mission_id": mid, "step_id": "o1.cv", "episode": 1, "authority": "operator"}
    monkeypatch.setattr(missions, "step_run_of_session", lambda key, **_: run)
    before = missions.event_count(mid)
    mark = store.mark_grant_announced

    def dies(set_id, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "mark_grant_announced", dies)
    counts = asyncio.run(grants.sweep())
    assert counts["unannounced"] == 1 and missions.event_count(mid) == before + 1
    monkeypatch.setattr(store, "mark_grant_announced", mark)
    counts = asyncio.run(grants.sweep(only=set()))
    assert counts["unannounced"] == 0 and counts["announced"] == 0  # found, not written again
    assert store.grant_set(g["set_id"])["announced_at"] is not None
    assert missions.event_count(mid) == before + 1
    asyncio.run(grants.sweep())
    assert missions.event_count(mid) == before + 1
    _clean()


def _restart(monkeypatch) -> None:
    """From here on this test process is a NEW app process: what it took before is pre-boot."""
    token = f"{store._process()}-rebooted"
    monkeypatch.setattr(store, "_process", lambda: token)


def test_a_retried_sweep_retries_only_what_failed(equipment, probe, monkeypatch):
    """A boot sweep that failed on one set retries THAT set only: a session the boot sweep proved
    live is not probed again at runtime, where a transient unknown would revoke it while it runs,
    and a set this boot issued is never the sweep's."""
    spec = _spec(capacity=3)
    failing, gf = _issued(spec, "failing")
    healthy, gh = _issued(spec, "healthy")
    for c in (failing, healthy):  # past their launch: live
        r = store.get_reservation(c.reservation_id)
        assert store.mark_live(c.reservation_id, seen_rev=r["rev"])
    _restart(monkeypatch)
    probe[failing.session_key] = specialist_capacity.DEAD
    probe[healthy.session_key] = specialist_capacity.LIVE
    read = store.get_reservation
    broken = {failing.reservation_id}

    def flaky(rid):
        if rid in broken:
            raise sqlite3.OperationalError("disk I/O error")
        return read(rid)

    monkeypatch.setattr(store, "get_reservation", flaky)
    asyncio.run(grants.ensure_swept())
    assert not grants.swept() and _state(gh["set_id"]) == "open"
    # At runtime: the store recovered, the healthy session's probe blips, and a new launch of
    # this boot (live, unknown to the probe) holds a set.
    broken.clear()
    probe[healthy.session_key] = specialist_capacity.UNKNOWN
    fresh, gn = _issued(spec, "fresh")
    r = store.get_reservation(fresh.reservation_id)
    assert store.mark_live(fresh.reservation_id, seen_rev=r["rev"])
    asyncio.run(grants.ensure_swept())
    assert grants.swept()
    assert _state(gf["set_id"]) == "boot_dead"
    assert _state(gh["set_id"]) == "open" and _state(gn["set_id"]) == "open"
    _clean()


def test_a_sweep_whose_ledger_read_failed_never_sweeps_this_boots_sets(
    equipment, probe, monkeypatch
):
    spec = _spec()
    old, go = _issued(spec, "old")
    r = store.get_reservation(old.reservation_id)
    assert store.mark_live(old.reservation_id, seen_rev=r["rev"])
    _restart(monkeypatch)
    probe[old.session_key] = specialist_capacity.DEAD
    sets = store.grant_sets
    calls = []

    def unreadable_once(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return sets(**kw)

    monkeypatch.setattr(store, "grant_sets", unreadable_once)
    asyncio.run(grants.ensure_swept())
    assert not grants.swept()
    fresh, gn = _issued(spec, "fresh")  # this boot's launch, live, unknown to the probe
    r = store.get_reservation(fresh.reservation_id)
    assert store.mark_live(fresh.reservation_id, seen_rev=r["rev"])
    asyncio.run(grants.ensure_swept())
    assert grants.swept()
    assert _state(go["set_id"]) == "boot_dead" and _state(gn["set_id"]) == "open"
    _clean()


def test_the_sweep_probes_a_bounded_number_at_once(equipment, probe, monkeypatch):
    spec = _spec(capacity=8)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    monkeypatch.setattr(grants, "SWEEP_CONCURRENCY", 2)
    keys = []
    for n in range(5):
        c, _g = _issued(spec, f"s{n}")
        keys.append(c.session_key)
    now = {"in": 0, "max": 0}

    async def liveness(key):
        now["in"] += 1
        now["max"] = max(now["max"], now["in"])
        await asyncio.sleep(0.05)
        now["in"] -= 1
        return specialist_capacity.LIVE, ""

    monkeypatch.setattr(specialist_capacity, "liveness", liveness)
    counts = asyncio.run(grants.sweep())
    assert counts["kept"] == 5 and now["max"] == 2


def test_a_sweep_never_waits_past_its_deadline(equipment, probe, monkeypatch):
    """Probes that never answer: the whole sweep ends at its deadline and, unknown, revokes."""
    spec = _spec(capacity=8)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    monkeypatch.setattr(grants, "PROBE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 0.3)
    monkeypatch.setattr(grants, "SWEEP_CONCURRENCY", 1)
    issued = [_issued(spec, f"s{n}")[1] for n in range(3)]

    async def silent(key):
        await asyncio.Event().wait()

    monkeypatch.setattr(specialist_capacity, "liveness", silent)

    async def bounded():
        return await asyncio.wait_for(grants.sweep(), 10)

    counts = asyncio.run(bounded())
    assert counts["revoked"] == 3
    assert {_state(g["set_id"]) for g in issued} == {"boot_unknown"}
    _clean()


# ---- the order: the sweep completes before any recovery or relaunch ----------------------------


def test_dispatch_recovery_waits_for_the_sweep(monkeypatch):
    from agent_sessions import mission_dispatch_recover, missions

    order = []

    async def sweep(**_):
        await asyncio.sleep(0)
        order.append("sweep")
        return {"kept": 0, "revoked": 0, "skipped": 0, "announced": 0, "failed": 0}

    monkeypatch.setattr(grants, "sweep", sweep)
    monkeypatch.setattr(missions, "unsettled_dispatches", lambda **_: order.append("recover") or [])
    asyncio.run(mission_dispatch_recover.recover_once())
    asyncio.run(mission_dispatch_recover.recover_once())
    assert order == ["sweep", "recover", "recover"]  # once per process, always first


def test_every_recovery_and_trigger_path_asks_for_the_sweep_first():
    """A recovery added later must ask too: the boot order is a property of each path, not of
    the order `main` happens to start them in."""
    import inspect

    from agent_sessions import automation_loop, main, mission_dispatch_recover, mission_runner
    from agent_sessions import mission_supervisor_loop as sup

    for fn in (
        mission_runner.sweep,
        mission_dispatch_recover.recover_once,
        mission_dispatch_recover.rebind_api_members,
        sup.run,
        automation_loop.Scheduler.run,
    ):
        src = inspect.getsource(fn)
        gated = "if not await grants.ensure_swept(" in src or "grants.wait_until_swept(" in src
        assert gated, fn.__qualname__  # proceeds only once the sweep COMPLETED
    life = inspect.getsource(main.create_app)
    # The call itself (never a comment naming it), after discovery: a sweep before the sessions
    # are re-adopted would read every live agent as unknown and revoke its equipment.
    sweep = life.index("await grants.ensure_swept()")
    assert life.index("await registry.discover()") < sweep
    assert sweep < life.index("mission_dispatch_recover")
    assert sweep < life.index("automation_loop.start")
    assert sweep < life.index("start_reconciliation")


def test_a_sweep_that_could_not_read_is_tried_again(monkeypatch):
    calls = []

    async def sweep(**_):
        calls.append(1)
        return {"kept": 0, "revoked": 0, "skipped": 0, "announced": 0, "failed": len(calls) == 1}

    monkeypatch.setattr(grants, "sweep", sweep)
    asyncio.run(grants.ensure_swept())
    assert not grants.swept()
    asyncio.run(grants.ensure_swept())
    asyncio.run(grants.ensure_swept())
    assert grants.swept() and len(calls) == 2


# ---- the schema ---------------------------------------------------------------------------------


def test_rollback_to_2_deletes_nothing_and_this_build_converges(equipment):
    claim, g = _issued(_spec(), "a")
    assert store.rollback_schema(2) == {"from": 3, "to": 2}
    con = sqlite3.connect(store.db_path())
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    con.close()
    store.reset_schema_cache_for_test()
    assert store.grant_set(g["set_id"])["set_id"] == g["set_id"]  # re-upgraded, kept
    with pytest.raises(store.StoreError):
        store.rollback_schema(1)


def test_the_cli_rolls_the_store_back_and_says_nothing_was_deleted(equipment, capsys):
    from agent_sessions import cli

    _issued(_spec(), "a")
    assert cli.main(["specialists-schema", "rollback", "--to", "2"]) == 0
    assert "Nothing was deleted" in capsys.readouterr().out
    assert cli.main(["specialists-schema", "rollback", "--to", "2"]) == 2  # already there


def test_an_api_worker_proved_ended_is_revoked_without_a_decision(equipment, probe, monkeypatch):
    """A worker that idled out reads `unknown` to the probe; its own unit evidence proves it ended,
    so nothing may still run and no decision is raised."""
    from agent_sessions import structured_runtime

    claim, g = _issued(_spec(), "a")
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    equipment["runs"][claim.session_key] = {"mission_id": "m1", "step_id": "o1.cv", "episode": 1}

    async def ended(key, release):
        release("w1")
        return "w1"

    monkeypatch.setattr(structured_runtime, "release_ended", ended)
    counts = asyncio.run(grants.sweep())
    assert (counts["revoked"], counts["announced"]) == (1, 0)
    assert _state(g["set_id"]) == "boot_dead" and equipment["events"] == []


# ---- recovery waits for a COMPLETED sweep (D-P6-3) --------------------------------------------


def _failing_sweep(monkeypatch, fails: int) -> list:
    calls: list = []

    async def sweep(**_):
        calls.append(1)
        bad = len(calls) <= fails
        return {
            "kept": 0,
            "revoked": 0,
            "skipped": 0,
            "announced": 0,
            "failed": int(bad),
            "retry": ["g-unsettled"] if bad else [],
        }

    monkeypatch.setattr(grants, "sweep", sweep)
    monkeypatch.setattr(grants, "SWEEP_RETRY_S", 0.0)
    return calls


def test_dispatch_recovery_waits_for_completion_and_recovers_nothing_without_it(monkeypatch):
    from agent_sessions import mission_dispatch_recover, missions

    order: list = []
    monkeypatch.setattr(missions, "unsettled_dispatches", lambda **_: order.append("recover") or [])
    monkeypatch.setattr(mission_dispatch_recover, "_recovery_deferred", False)
    calls = _failing_sweep(monkeypatch, fails=99)
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    # Deferred at once (boot housekeeping after it is not held up), never run ungated.
    assert order == [] and len(calls) == 1 and not grants.swept()
    assert mission_dispatch_recover._recovery_deferred is True
    grants.reset_for_test()
    monkeypatch.setattr(mission_dispatch_recover, "_recovery_deferred", False)
    calls = _failing_sweep(monkeypatch, fails=0)
    asyncio.run(mission_dispatch_recover.recover_once())
    assert order == ["recover"] and len(calls) == 1 and grants.swept()


def test_periodic_launchers_skip_their_pass_until_the_sweep_completed(monkeypatch):
    from agent_sessions import mission_dispatch_recover, mission_runner, missions

    touched: list = []

    async def admitted(fn, *a, **kw):
        touched.append(fn.__name__)
        return []

    monkeypatch.setattr(missions, "run_admitted", admitted)
    monkeypatch.setattr(mission_runner, "enabled", lambda: True)
    monkeypatch.setattr(mission_runner, "_closing", False)  # another test may have drained it
    _failing_sweep(monkeypatch, fails=99)
    assert asyncio.run(mission_dispatch_recover.rebind_api_members()) == 0
    assert asyncio.run(mission_runner.sweep()) == 0
    assert touched == []  # neither read its worklist
    grants.reset_for_test()
    _failing_sweep(monkeypatch, fails=0)
    asyncio.run(mission_dispatch_recover.rebind_api_members())
    asyncio.run(mission_runner.sweep())
    assert len(touched) == 2


def test_the_supervisor_and_the_scheduler_start_only_after_completion(monkeypatch):
    from agent_sessions import automation_loop, mission_dispatch_recover, missions
    from agent_sessions import mission_supervisor_loop as sup

    seen: list = []
    calls = _failing_sweep(monkeypatch, fails=2)
    # Boot recovery was deferred for the incomplete sweep: the supervisor runs it, once, after
    # the completed attempt and before anything of its own.
    monkeypatch.setattr(mission_dispatch_recover, "_recovery_deferred", True)
    monkeypatch.setattr(
        missions,
        "unsettled_dispatches",
        lambda **_: seen.append(("recovery", len(calls))) or [],
    )

    async def reconciled():
        seen.append(("supervisor", len(calls)))
        raise asyncio.CancelledError

    monkeypatch.setattr(sup, "_reconcile_delivered", reconciled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sup.run())
    assert seen == [("recovery", 3), ("supervisor", 3)]  # after the third, completed, attempt
    assert mission_dispatch_recover._recovery_deferred is False

    grants.reset_for_test()
    calls = _failing_sweep(monkeypatch, fails=2)
    sched = automation_loop.Scheduler(registry=None)

    async def tick():
        seen.append(("scheduler", len(calls)))
        sched._stopping = True

    monkeypatch.setattr(automation_loop, "loop_enabled", lambda: True)
    monkeypatch.setattr(sched, "tick", tick)
    monkeypatch.setattr(automation_loop, "INTERVAL_S", 0)
    monkeypatch.setattr(sched, "release", lambda: None)
    asyncio.run(sched.run())
    assert seen[-1] == ("scheduler", 3)


def test_every_refusal_the_broker_can_give_has_words_for_the_agent():
    """A refusal without its own words falls back to "not reachable; try again later", which
    would send an agent whose grant was revoked into a retry loop."""
    from agent_sessions import secrets_mcp

    assert set(secrets_broker.REFUSALS) <= set(secrets_mcp.MEANING)
    assert "start the session again" in secrets_mcp.MEANING["grant_revoked"]


def test_owed_decisions_survive_a_pass_whose_ledger_read_failed(monkeypatch):
    """After completion, a pass that can't read the ledger knows nothing settled: the owed
    decisions stay owed, and the next call asks again."""
    passes: list = []
    answers = [
        {"failed": 0, "unannounced": 1, "retry": []},  # complete, one decision owed
        {"failed": 1, "unannounced": 0, "retry": None},  # the ledger can't be read
        {"failed": 0, "unannounced": 0, "retry": []},  # raised
    ]

    async def sweep(**_):
        passes.append(1)
        return {"kept": 0, "revoked": 0, "skipped": 0, "announced": 0, **answers[len(passes) - 1]}

    monkeypatch.setattr(grants, "sweep", sweep)
    for _ in range(4):
        assert asyncio.run(grants.ensure_swept()) is True
    assert len(passes) == 3  # the third pass happened; nothing asked after it


# ---- owed decisions after completion: bounded, rate-limited, never blocking --------------------


def _completed_with_owed(monkeypatch, last_pass):
    calls: list = []

    async def sweep(**kw):
        calls.append(kw)
        return {
            "kept": 0,
            "revoked": 0,
            "skipped": 0,
            "announced": 0,
            "unannounced": 1,
            "failed": 0,
            "retry": [],
        }

    monkeypatch.setattr(grants, "sweep", sweep)
    monkeypatch.setattr(grants, "_swept", True)
    monkeypatch.setattr(grants, "_announce_owed", True)
    monkeypatch.setattr(grants, "_last_pass", last_pass)
    return calls


def test_owed_decisions_are_raised_at_most_once_per_retry_interval(monkeypatch):
    import time as _time

    monkeypatch.setattr(grants, "SWEEP_RETRY_S", 60.0)
    calls = _completed_with_owed(monkeypatch, last_pass=_time.monotonic())
    assert asyncio.run(grants.ensure_swept()) is True
    assert calls == []  # within the interval: no pass
    monkeypatch.setattr(grants, "_last_pass", _time.monotonic() - 61)
    assert asyncio.run(grants.ensure_swept()) is True
    assert calls == [{"only": set(), "spare_own": True}]  # owed decisions only, no probe


def test_a_caller_never_waits_behind_an_owed_decision_pass(monkeypatch):
    _completed_with_owed(monkeypatch, last_pass=None)

    async def scenario():
        grants._sweep_lock = asyncio.Lock()
        await grants._sweep_lock.acquire()  # another caller is raising the owed decisions
        try:
            return await asyncio.wait_for(grants.ensure_swept(), 1.0)
        finally:
            grants._sweep_lock.release()

    assert asyncio.run(scenario()) is True


def test_owed_decisions_share_the_passes_budget(equipment, probe, monkeypatch):
    """Past the pass's budget, an owed decision is left owed, not written (boot is bounded)."""
    claim, g = _issued(_spec(), "a")
    assert store.revoke_grant_set(g["set_id"], "boot_unknown")
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 0.0)
    monkeypatch.setattr(grants, "SWEEP_STORE_GRACE_S", 0.0)
    asked: list = []
    monkeypatch.setattr(grants, "_announce_lost_access", lambda g: asked.append(g) or True)
    counts = asyncio.run(grants.sweep(only=set()))
    assert counts["unannounced"] == 1 and asked == []


def test_revoking_sets_twice_touches_nothing_the_second_time(broker, monkeypatch, equipment):  # noqa: F811
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _cap_for(g, claim.session_key)
    assert secrets_broker.revoke_grant_sets([g["set_id"]]) == 1
    assert secrets_broker.revoke_grant_sets([g["set_id"]]) == 0  # only what is left to revoke
    launch.abandon(claim)


def test_boot_recovery_defers_rather_than_wait_behind_a_running_pass(monkeypatch):
    from agent_sessions import mission_dispatch_recover, missions

    order: list = []
    monkeypatch.setattr(missions, "unsettled_dispatches", lambda **_: order.append("recover") or [])
    monkeypatch.setattr(mission_dispatch_recover, "_recovery_deferred", False)
    calls = _failing_sweep(monkeypatch, fails=0)

    async def scenario():
        grants._sweep_lock = asyncio.Lock()
        await grants._sweep_lock.acquire()  # the supervisor's pass is running
        try:
            return await asyncio.wait_for(mission_dispatch_recover.recover_once(), 1.0)
        finally:
            grants._sweep_lock.release()

    assert asyncio.run(scenario()) == 0
    assert order == [] and calls == [] and mission_dispatch_recover._recovery_deferred is True


def _mission_state(mid: str, column: str) -> None:
    from agent_sessions import missions

    con = missions._ready()
    try:
        con.execute(f"UPDATE missions SET {column} = 1.0 WHERE id = ?", (mid,))  # noqa: S608
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize(
    ("columns", "status"),
    [
        (("archived_at",), 410),
        (("archived_at", "archiving_at"), 410),  # a finished archive keeps its stamp
        (("archiving_at",), 409),
        (("archived_at", "unarchiving_at"), 409),  # mid-unarchive: archived_at stays set
    ],
)
def test_the_lost_access_row_respects_the_missions_archive_state(columns, status):
    """The store's own contract: an archived mission takes no row (410, final); one mid-archive or
    mid-unarchive refuses for now (409, retried); a missing one is `MissionNotFound`."""
    from agent_sessions import missions

    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    before = missions.event_count(mid)
    for column in columns:
        _mission_state(mid, column)
    with pytest.raises(missions.MissionError) as e:
        missions.ensure_lost_access_event(
            mid, session_key=_k("a"), grant_set="g1", text="lost", meta={}
        )
    assert e.value.status == status and missions.event_count(mid) == before
    with pytest.raises(missions.MissionNotFound):
        missions.ensure_lost_access_event(
            missions.new_id(), session_key=_k("a"), grant_set="g1", text="lost", meta={}
        )


def test_a_decision_owed_during_an_unarchive_is_raised_once_the_mission_is_back(probe, monkeypatch):
    """The real mission store, through the real lifecycle state: mid-unarchive (`archived_at` AND
    `unarchiving_at` set) refuses for now and the decision stays owed; once the mission is
    restored, the next call writes exactly one row and marks the set."""
    from agent_sessions import missions

    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    claim, g = _issued(_spec(), "a")
    _restart(monkeypatch)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    run = {"mission_id": mid, "step_id": "o1.cv", "episode": 1, "authority": "operator"}
    monkeypatch.setattr(missions, "step_run_of_session", lambda key, **_: run)
    _mission_state(mid, "archived_at")
    _mission_state(mid, "unarchiving_at")
    before = missions.event_count(mid)
    assert asyncio.run(grants.ensure_swept()) is True  # complete: the revocation is committed
    assert _state(g["set_id"]) == "boot_unknown"
    assert store.grant_set(g["set_id"])["announced_at"] is None  # owed, not discarded
    con = missions._ready()  # the unarchive finishes
    try:
        con.execute(
            "UPDATE missions SET archived_at = NULL, unarchiving_at = NULL WHERE id = ?", (mid,)
        )
        con.commit()
    finally:
        con.close()
    asyncio.run(grants.ensure_swept())
    assert store.grant_set(g["set_id"])["announced_at"] is not None
    assert missions.event_count(mid) == before + 1
    _clean()


def test_a_blocked_notice_writer_never_holds_the_pass_past_its_budget(probe, monkeypatch):
    """The REAL mission writer, blocked by the REAL mission write lock (another writer holds it):
    the owed notice is bounded by the pass's budget, the pass returns with the decision owed, and
    the admitted worker that already began finishes on its own once the lock is free (one row,
    then marked), its admission slot released."""
    import time as _time

    from agent_sessions import missions

    mid = missions.create_mission("ship it", cwd="/tmp")["id"]
    claim, g = _issued(_spec(), "a")
    assert store.revoke_grant_set(g["set_id"], "boot_unknown")
    run = {"mission_id": mid, "step_id": "o1.cv", "episode": 1, "authority": "operator"}
    monkeypatch.setattr(missions, "step_run_of_session", lambda key, **_: run)
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 0.05)
    monkeypatch.setattr(grants, "SWEEP_STORE_GRACE_S", 0.05)
    before = missions.event_count(mid)

    async def bounded():
        t0 = _time.monotonic()
        # The outer 5 s only keeps an unbounded pass (the regression) from hanging the suite.
        return await asyncio.wait_for(grants.sweep(only=set()), 5), _time.monotonic() - t0

    with missions._write_lock:  # another writer holds the mission store
        counts, took = asyncio.run(bounded())
    assert counts["unannounced"] == 1 and took < 1.0
    # Lock free: a later pass raises the owed decision, and a worker that already began (if the
    # executor started it in time) writes at most the same one row: exactly one either way.
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 30.0)
    monkeypatch.setattr(grants, "SWEEP_STORE_GRACE_S", 15.0)
    asyncio.run(grants.sweep(only=set()))
    assert store.grant_set(g["set_id"])["announced_at"] is not None
    assert missions.event_count(mid) == before + 1
    _clean()


@pytest.mark.parametrize("final", ["archived", "missing"])
def test_a_final_mission_refusal_settles_the_decision_for_good(
    equipment, probe, monkeypatch, final
):
    """Archived (410) or gone (`MissionNotFound`): nobody to tell, so the set is marked and never
    re-announced every SWEEP_RETRY_S."""
    from agent_sessions import missions

    claim, g = _issued(_spec(), "a")
    _restart(monkeypatch)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    equipment["runs"][claim.session_key] = {"mission_id": "m1", "step_id": "o1.cv", "episode": 1}

    def refuse(*a, **kw):
        if final == "missing":
            raise missions.MissionNotFound("m1")
        raise missions.MissionError("mission m1 is archived", status=410)

    monkeypatch.setattr(missions, "ensure_lost_access_event", refuse)
    assert asyncio.run(grants.ensure_swept()) is True
    assert store.grant_set(g["set_id"])["announced_at"] is not None
    assert grants._announce_owed is False


def test_waiting_for_the_sweep_never_spins(monkeypatch):
    _failing_sweep(monkeypatch, fails=10**6)
    monkeypatch.setattr(grants, "SWEEP_RETRY_S", 0.05)
    asked: list = []
    real = grants.ensure_swept

    async def counted(**kw):
        asked.append(1)
        return await real(**kw)

    monkeypatch.setattr(grants, "ensure_swept", counted)

    async def bounded():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(grants.wait_until_swept(), 0.5)

    asyncio.run(bounded())
    assert 1 <= len(asked) <= 20  # one attempt per SWEEP_RETRY_S, not a hot loop


def test_a_set_issued_for_another_project_serves_nothing(broker, monkeypatch, equipment):  # noqa: F811
    spec, claim = _broker_world(broker, monkeypatch, equipment)
    g = grants.issue(claim, claim.session_key, "/w")
    _env, token = _cap_for(g, claim.session_key)
    con = sqlite3.connect(store.db_path())
    con.execute(
        "UPDATE specialist_grant_sets SET project_id = 'beta' WHERE set_id = ?", (g["set_id"],)
    )
    con.commit()
    con.close()
    assert secrets_broker.handle(token, "path", "forgejo_token") == {
        "ok": False,
        "error": "not_granted",
    }
    launch.abandon(claim)


def test_a_slow_notice_never_costs_another_set_its_probe(equipment, probe, monkeypatch):
    """A lost-access write that is slow (a busy mission store) is raised after every probe, in
    its own budget: it never holds a probe slot or spends the probes' deadline, so a live session
    queued behind it is still probed and kept."""
    import time as _time

    from agent_sessions import missions

    spec = _spec()
    first, g1 = _issued(spec, "first")
    second, g2 = _issued(spec, "second")
    _restart(monkeypatch)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    monkeypatch.setattr(grants, "SWEEP_CONCURRENCY", 1)
    # The deadline has real slack for a loaded runner's probes and store writes; the notice still
    # sleeps past it, so a notice that held the probe slot would cost the second set its probe.
    monkeypatch.setattr(grants, "SWEEP_DEADLINE_S", 3.0)
    equipment["runs"][first.session_key] = {"mission_id": "m1", "step_id": "o1.cv", "episode": 1}
    probe[first.session_key] = specialist_capacity.UNKNOWN
    probe[second.session_key] = specialist_capacity.LIVE
    record = missions.ensure_lost_access_event

    def slow(*a, **kw):
        _time.sleep(4.0)
        return record(*a, **kw)

    monkeypatch.setattr(missions, "ensure_lost_access_event", slow)
    counts = asyncio.run(grants.sweep(spare_own=True))
    assert _state(g1["set_id"]) == "boot_unknown" and _state(g2["set_id"]) == "open"
    assert (counts["kept"], counts["announced"], counts["retry"]) == (1, 1, [])
    _clean()


def test_a_retry_after_an_unreadable_pass_still_probes_only_what_failed(
    equipment, probe, monkeypatch
):
    """Pass 1 keeps A live and fails on B; pass 2 can't read the ledger; pass 3 probes B only.
    A session the boot pass proved live is never re-probed at runtime."""
    spec = _spec()
    a, ga = _issued(spec, "a")
    b, gb = _issued(spec, "b")
    _restart(monkeypatch)
    monkeypatch.setattr(store, "process_state", lambda token: store.GONE)
    probed: list = []

    def answer(key, verdict):
        def f():
            probed.append(key)
            return verdict

        return f

    probe[a.session_key] = answer(a.session_key, specialist_capacity.LIVE)
    probe[b.session_key] = answer(b.session_key, specialist_capacity.DEAD)
    read, sets = store.get_reservation, store.grant_sets
    fail = {"reservation": {b.reservation_id}, "ledger": False}

    def get_reservation(rid):
        if rid in fail["reservation"]:
            raise sqlite3.OperationalError("disk I/O error")
        return read(rid)

    def grant_sets(**kw):
        if fail["ledger"]:
            raise sqlite3.OperationalError("database is locked")
        return sets(**kw)

    monkeypatch.setattr(store, "get_reservation", get_reservation)
    monkeypatch.setattr(store, "grant_sets", grant_sets)
    assert asyncio.run(grants.ensure_swept()) is False  # A kept, B failed
    fail.update(reservation=set(), ledger=True)
    assert asyncio.run(grants.ensure_swept()) is False  # the ledger can't be read
    fail["ledger"] = False
    probed.clear()
    probe[a.session_key] = answer(a.session_key, specialist_capacity.UNKNOWN)  # a blip
    assert asyncio.run(grants.ensure_swept()) is True
    assert probed == [b.session_key]
    assert _state(ga["set_id"]) == "open" and _state(gb["set_id"]) == "boot_dead"
    _clean()


def test_periodic_launchers_skip_rather_than_wait_behind_a_running_pass(monkeypatch):
    from agent_sessions import mission_dispatch_recover, mission_runner, missions

    touched: list = []

    async def admitted(fn, *a, **kw):
        touched.append(fn.__name__)
        return []

    monkeypatch.setattr(missions, "run_admitted", admitted)
    monkeypatch.setattr(mission_runner, "enabled", lambda: True)
    monkeypatch.setattr(mission_runner, "_closing", False)
    _failing_sweep(monkeypatch, fails=0)

    async def scenario():
        grants._sweep_lock = asyncio.Lock()
        await grants._sweep_lock.acquire()  # the supervisor's own pass is running
        try:
            a = await asyncio.wait_for(mission_dispatch_recover.rebind_api_members(), 1.0)
            b = await asyncio.wait_for(mission_runner.sweep(), 1.0)
            return a, b
        finally:
            grants._sweep_lock.release()

    assert asyncio.run(scenario()) == (0, 0) and touched == []
