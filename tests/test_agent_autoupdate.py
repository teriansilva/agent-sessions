# ruff: noqa: F811 — the imported fixture is the parameter below
"""Unattended agent updates (#1994): real installs and manager commits, a fake fresh catalog.

The offline checks' own runner (`probes.run_offline`) spawns the vendor's `--version` through
systemd-run, which a test host can't; it is replaced here by one that records the exact offline
check set through the real `manager.finish_verification`. Everything else (review, install,
verification record, activation predicate, attempt records, migrations) is the production code.
"""

from __future__ import annotations

import copy
import os
import stat
import time
import uuid

import pytest

from agent_sessions import agent_autoupdate, agent_catalog, agent_catalog_refresh, prefs, update
from agent_sessions.plugins import feed, manager, probes, storage
from test_plugin_manager import recipe  # noqa: F401 — the fixture

_REAL_ACTIVATE = manager.activate_offline


def rid() -> str:
    return str(uuid.uuid4())


def at(raw: dict, version: str) -> dict:
    out = copy.deepcopy(raw)
    out["manifest"]["identity"]["version"] = version
    return out


@pytest.fixture(autouse=True)
def _host(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    for key in ("AGENT_SESSIONS_AUTOUPDATE", "AGENT_SESSIONS_FEED_PUBLISHER"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def bell(monkeypatch):
    rows = []
    monkeypatch.setattr(
        agent_autoupdate.notifications, "add", lambda **fields: rows.append(fields) or fields
    )
    return rows


def serve(monkeypatch, *raws, sequence=7, stale=False, source="remote"):
    entries = tuple(feed.entry(r, signed=True) for r in raws)
    catalog = agent_catalog.Catalog(
        tuple(agent_catalog.Choice(e, "signed") for e in entries),
        sequence,
        int(time.time()) + 86400,
        "d" * 64,
        "b" * 64,
        source,
        stale=stale,
    )
    monkeypatch.setattr(agent_catalog, "current", lambda: catalog)
    return entries


def full_verify(generation: str) -> None:
    row = manager.snapshot()["plugins"]["fixture"]
    prov = manager.provider("fixture", row["generations"][generation])
    manager.record_verification(
        "fixture",
        generation,
        [{"check": c, "passed": True} for c in manager.required_checks(prov)],
    )


def install_active(monkeypatch, raw) -> str:
    """The operator's own first install: reviewed, fully verified and enabled."""
    serve(monkeypatch, raw)
    review = manager.review(plugin_id="fixture")
    assert review["source"] == "signed"
    gen = manager.run_install(manager.begin_install(rid(), review["id"], review["digest"])["id"])
    assert gen["state"] == "installed", gen
    full_verify(gen["id"])
    manager.activate(rid(), "fixture", gen["id"])
    return gen["id"]


def offline(monkeypatch, *, passed=True, during=None):
    """Replace only the process spawn: record the exact offline checks through the manager."""
    runs = []

    async def fake(item):
        runs.append(item["id"])
        assert item["request"] == {
            "plugin_id": "fixture",
            "generation_id": item["request"]["generation_id"],
            "scope": "offline",
        }
        if during is not None:
            during()
        gen = manager.generation(item["plugin_id"], item["request"]["generation_id"])
        prov = manager.provider(item["plugin_id"], gen)
        manager.finish_verification(
            item["id"],
            [
                {"check": c, "passed": passed, **({"version": "1.1.0"} if c == "version" else {})}
                for c in manager.offline_checks(prov)
            ],
        )

    monkeypatch.setattr(probes, "run_offline", fake)
    return runs


def row():
    return manager.snapshot()["plugins"]["fixture"]


def before_activation(monkeypatch, action):
    """Run ``action`` in the real window an operator has: after the checks released the worker
    fence, before the activation commit. (During the check itself every roster change is refused
    as busy, which is the manager's own guarantee.)"""

    def wrapped(*args, **kwargs):
        action()
        return _REAL_ACTIVATE(*args, **kwargs)

    monkeypatch.setattr(manager, "activate_offline", wrapped)


def attempts():
    return list(agent_autoupdate.records()["attempts"].values())


# --- the happy path ------------------------------------------------------------------------------


def test_a_newer_recipe_installs_checks_offline_and_activates(recipe, monkeypatch, bell):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    assert agent_autoupdate.run_pass() == "ok"
    now = row()
    assert now["active"] != old and now["enabled"] is True
    gen = now["generations"][now["active"]]
    assert manager.verification_scope(gen) == "offline"
    assert sorted(r["check"] for r in gen["verification"]["results"]) == manager.offline_checks(
        manager.provider("fixture", gen)
    )
    # The previous generation stays installed for running sessions and Roll back.
    assert old in now["generations"] and manager.generation_root("fixture", old).exists()
    ops = manager.snapshot()["operations"]
    mine = [o for o in ops.values() if o.get("actor") == "auto-update"]
    assert sorted(o["kind"] for o in mine) == ["activate", "install", "verify"]
    assert [a["state"] for a in attempts()] == ["activated"] and len(runs) == 1 and bell == []
    # A second pass on the same cut does nothing.
    assert agent_autoupdate.run_pass() == "ok" and len(runs) == 1


def test_an_older_or_equal_recipe_is_never_an_update(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.2.0"))
    runs = offline(monkeypatch)
    for version in ("1.2.0", "1.1.9"):
        serve(monkeypatch, at(recipe, version))
        agent_autoupdate.run_pass()
    assert runs == [] and attempts() == []


# --- the consent boundary ------------------------------------------------------------------------


def test_offline_evidence_never_satisfies_a_human_activation(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.0.0"))
    offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    review = manager.review(plugin_id="fixture")
    gen = manager.run_install(manager.begin_install(rid(), review["id"], review["digest"])["id"])
    op = manager.begin_offline_verify(rid(), "fixture", gen["id"])
    manager._set_operation(op["id"], "running")
    import asyncio

    asyncio.run(probes.run_offline(op))
    with pytest.raises(manager.ManagerError, match="required verification"):
        manager.activate(rid(), "fixture", gen["id"])
    # Even where the two lists coincide, the scope decides.
    same = manager.offline_checks(manager.provider("fixture", row()["generations"][gen["id"]]))
    monkeypatch.setattr(manager, "required_checks", lambda _prov: list(same))
    with pytest.raises(manager.ManagerError, match="required verification"):
        manager.activate(rid(), "fixture", gen["id"])


def test_the_human_verify_still_requires_its_effects_confirmation(recipe, monkeypatch):
    gen = install_active(monkeypatch, at(recipe, "1.0.0"))
    with pytest.raises(manager.ManagerError, match="confirm verification"):
        manager.begin_action(rid(), "fixture", gen, "verify")


def test_disable_verify_enable_upgrades_an_offline_generation_to_full(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.0.0"))
    offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    agent_autoupdate.run_pass()
    snap = manager.snapshot()
    current = snap["plugins"]["fixture"]["active"]
    manager.deactivate(
        rid(),
        "fixture",
        expected_active=current,
        expected_revision=snap["roster_revision"],
    )
    op = manager.begin_action(rid(), "fixture", current, "verify", confirm_effects=True)
    manager._set_operation(op["id"], "running")
    prov = manager.provider("fixture", row()["generations"][current])
    manager.finish_verification(
        op["id"], [{"check": c, "passed": True} for c in manager.required_checks(prov)]
    )
    assert manager.verification_scope(row()["generations"][current]) == "full"
    manager.activate(rid(), "fixture", current)
    assert row()["enabled"] is True and row()["active"] == current


def test_offline_verification_refuses_local_and_adopted_installations(recipe, monkeypatch):
    review = manager.review(local=at(recipe, "1.0.0"))
    gen = manager.run_install(
        manager.begin_install(rid(), review["id"], review["digest"], confirm_local=True)["id"]
    )
    with pytest.raises(manager.ManagerError, match="catalog-managed"):
        manager.begin_offline_verify(rid(), "fixture", gen["id"])


# --- what never updates --------------------------------------------------------------------------


@pytest.mark.parametrize("why", ["stale", "bundled-only", "disabled", "local"])
def test_nothing_happens_where_it_must_not(recipe, monkeypatch, why):
    runs = offline(monkeypatch)
    if why == "local":
        review = manager.review(local=at(recipe, "1.0.0"))
        gen = manager.run_install(
            manager.begin_install(rid(), review["id"], review["digest"], confirm_local=True)["id"]
        )
        full_verify(gen["id"])
        manager.activate(rid(), "fixture", gen["id"])
    else:
        gen = install_active(monkeypatch, at(recipe, "1.0.0"))
    if why == "disabled":
        snap = manager.snapshot()
        manager.deactivate(
            rid(), "fixture", expected_active=gen, expected_revision=snap["roster_revision"]
        )
    serve(
        monkeypatch,
        at(recipe, "1.1.0"),
        stale=why == "stale",
        source="bundled" if why == "bundled-only" else "remote",
    )
    agent_autoupdate.run_pass()
    assert runs == [] and attempts() == []


def test_paused_means_nothing_installs(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    update.set_settings(auto_update=False)
    serve(monkeypatch, at(recipe, "1.1.0"))
    assert agent_autoupdate.run_pass() == "paused" and runs == [] and attempts() == []


# --- failures are final for that digest ----------------------------------------------------------


def test_a_failed_check_keeps_the_old_version_notifies_once_and_is_never_retried(
    recipe, monkeypatch, bell
):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch, passed=False)
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=7)
    agent_autoupdate.run_pass()
    assert row()["active"] == old and row()["enabled"] is True
    assert [a["state"] for a in attempts()] == ["failed"] and len(runs) == 1
    assert len(bell) == 1 and "didn't pass its checks" in bell[0]["title"]
    assert "you're still on 1.0.0" in bell[0]["title"]
    # The same pass again, then a later unrelated cut still carrying the same digest.
    agent_autoupdate.run_pass()
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=8)
    agent_autoupdate.run_pass()
    assert len(runs) == 1 and len(bell) == 1 and len(attempts()) == 1
    # A different, newer recipe is eligible.
    offline(monkeypatch, passed=True)
    serve(monkeypatch, at(recipe, "1.2.0"), sequence=9)
    agent_autoupdate.run_pass()
    assert row()["active"] != old


# --- busy, restarts and the operator ------------------------------------------------------------


def test_busy_skips_without_consuming_anything(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    with storage.locked("worker", wait=0):
        assert agent_autoupdate.run_pass() == "busy"
    assert runs == []
    assert all(a["install_op"] is None for a in attempts())
    assert agent_autoupdate.run_pass() == "ok" and len(runs) == 1
    assert [a["state"] for a in attempts()] == ["activated"]


class Crash(BaseException):
    """A process death between two durable writes (never caught by product code)."""


@pytest.mark.parametrize("where", ["install", "check", "activate"])
def test_a_restart_mid_attempt_resumes(recipe, monkeypatch, where):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    real = {
        "install": (manager, "_run_install"),
        "check": (probes, "run_offline"),
        "activate": (manager, "activate_offline"),
    }[where]
    original = getattr(*real)

    def crash(*_a, **_k):
        raise Crash

    monkeypatch.setattr(*real, crash)
    with pytest.raises(Crash):
        agent_autoupdate.run_pass()
    monkeypatch.setattr(*real, original)
    # Startup recovery settles whatever the dead process left running.
    monkeypatch.setattr("agent_sessions.plugins.process.stop_operation", lambda _id: True)
    manager.recover()
    assert row()["active"] == old
    assert agent_autoupdate.run_pass() == "ok"
    assert row()["active"] != old and [a["state"] for a in attempts()] == ["activated"]
    assert len(runs) == 1  # the replaced step ran exactly once, after the restart


def test_a_disable_during_the_attempt_wins(recipe, monkeypatch):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))

    def operator_disables():
        snap = manager.snapshot()
        manager._commit_change(
            rid(),
            "fixture",
            "disable",
            None,
            old,
            snap["roster_revision"],
            known_provider=True,
            roster={},
        )

    offline(monkeypatch)
    before_activation(monkeypatch, operator_disables)
    serve(monkeypatch, at(recipe, "1.1.0"))
    agent_autoupdate.run_pass()
    assert row()["enabled"] is False and row()["active"] == old  # never re-enabled
    (attempt,) = attempts()
    assert attempt["state"] == "superseded"
    assert attempt["generation"] in row()["generations"]  # left installed, inactive


def test_pause_during_the_attempt_defers_and_resume_completes(recipe, monkeypatch):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    offline(monkeypatch)
    before_activation(monkeypatch, lambda: update.set_settings(auto_update=False))
    serve(monkeypatch, at(recipe, "1.1.0"))
    agent_autoupdate.run_pass()
    assert row()["active"] == old and [a["state"] for a in attempts()] == ["checked"]
    before_activation(monkeypatch, lambda: None)
    update.set_settings(auto_update=True)
    agent_autoupdate.run_pass()
    assert row()["active"] != old and [a["state"] for a in attempts()] == ["activated"]


def test_roll_back_declines_that_digest_but_not_a_newer_one(recipe, monkeypatch):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=7)
    agent_autoupdate.run_pass()
    manager.activate(rid(), "fixture", old)  # the operator's Roll back
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=8)
    agent_autoupdate.run_pass()
    assert row()["active"] == old and len(runs) == 1
    assert agent_autoupdate.records()["declined"]["fixture"]
    serve(monkeypatch, at(recipe, "1.2.0"), sequence=9)
    agent_autoupdate.run_pass()
    assert row()["active"] != old and len(runs) == 2


def test_another_agents_roster_decision_does_not_supersede_this_update(recipe, monkeypatch):
    """Another agent's activation (an unattended update in the same pass, say) moves the roster
    revision. That is no decision about THIS agent, so it must not cost this agent its update."""
    old = install_active(monkeypatch, at(recipe, "1.0.0"))

    def someone_elses_decision():
        before = manager.snapshot()["roster_revision"]
        manager._commit_change(rid(), "other", "disable", None, known_provider=True, roster={})
        assert manager.snapshot()["roster_revision"] != before

    offline(monkeypatch)
    before_activation(monkeypatch, someone_elses_decision)
    serve(monkeypatch, at(recipe, "1.1.0"))
    agent_autoupdate.run_pass()
    assert row()["active"] != old and [a["state"] for a in attempts()] == ["activated"]


# --- migrations ----------------------------------------------------------------------------------


def _env(tmp_path) -> str:
    path = tmp_path / "env"
    return path.read_text() if path.exists() else ""


def test_the_publisher_pref_moves_to_host_config_then_is_dropped(tmp_path):
    prefs.set_feed_alerts(True)
    assert agent_autoupdate.migrate()["publisher"] == "migrated"
    assert "AGENT_SESSIONS_FEED_PUBLISHER=1" in _env(tmp_path)
    assert prefs.get_feed_alerts() is False and update.feed_publisher() is True


def test_an_interrupted_publisher_migration_finishes_next_start(tmp_path):
    prefs.set_feed_alerts(True)
    (tmp_path / "env").write_text("AGENT_SESSIONS_FEED_PUBLISHER=1\n")  # written, pref not dropped
    agent_autoupdate.migrate()
    assert prefs.get_feed_alerts() is False


def test_an_explicit_host_zero_is_never_overwritten(tmp_path):
    prefs.set_feed_alerts(True)
    (tmp_path / "env").write_text("AGENT_SESSIONS_FEED_PUBLISHER=0\n")
    assert agent_autoupdate.migrate()["publisher"] == "host-config"
    assert _env(tmp_path) == "AGENT_SESSIONS_FEED_PUBLISHER=0\n"


def _read_only(tmp_path, monkeypatch):
    locked = tmp_path / "ro"
    locked.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(locked / "env"))
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if os.access(locked, os.W_OK):  # running as root: the failure can't be produced here
        pytest.skip("the env directory is writable for this user")
    return locked


def test_a_read_only_env_file_keeps_the_publisher_pref_honoured(tmp_path, monkeypatch):
    from agent_sessions import feed_alerts

    locked = _read_only(tmp_path, monkeypatch)
    try:
        prefs.set_feed_alerts(True)
        assert agent_autoupdate.migrate()["publisher"] == "kept"
        assert prefs.get_feed_alerts() is True and feed_alerts.publisher() is True
    finally:
        locked.chmod(0o700)


def test_a_catalog_opt_out_becomes_paused(tmp_path):
    agent_catalog_refresh.configure(False)
    assert agent_autoupdate.migrate()["paused"] == "migrated"
    assert update.auto_update_enabled() is False
    assert agent_catalog_refresh.status()["automatic"] is True  # still checked, just paused


def test_an_unmigrated_catalog_opt_out_still_freezes(tmp_path, monkeypatch, recipe):
    locked = _read_only(tmp_path, monkeypatch)
    try:
        agent_catalog_refresh.configure(False)
        assert agent_autoupdate.migrate()["paused"] == "kept"
        assert agent_catalog_refresh.status()["automatic"] is False
        assert update.auto_update_enabled() is True  # nothing could be written...
        assert agent_autoupdate.run_pass() == "paused"  # ...and still nothing installs
    finally:
        locked.chmod(0o700)


# --- review 8105 regressions (#1999) -------------------------------------------------------------


def test_a_crash_after_the_activation_commit_still_lets_roll_back_decline(recipe, monkeypatch):
    """Activation lands in the manager, the process dies before the ledger says `activated`; a
    restart must settle it so the operator's later Roll back declines that digest for good."""
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=7)

    def commit_then_die(*args, **kwargs):
        _REAL_ACTIVATE(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(manager, "activate_offline", commit_then_die)
    with pytest.raises(Crash):
        agent_autoupdate.run_pass()
    monkeypatch.setattr(manager, "activate_offline", _REAL_ACTIVATE)
    assert row()["active"] != old and [a["state"] for a in attempts()] == ["checked"]
    agent_autoupdate.run_pass()  # the restart's first pass settles it
    assert [a["state"] for a in attempts()] == ["activated"]
    manager.activate(rid(), "fixture", old)  # the operator's Roll back
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=8)
    agent_autoupdate.run_pass()
    assert row()["active"] == old and len(runs) == 1
    assert agent_autoupdate.records()["declined"]["fixture"]


def test_pausing_during_a_pass_stops_installation_and_checks(recipe, monkeypatch):
    old = install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch)
    serve(monkeypatch, at(recipe, "1.1.0"))
    real = agent_autoupdate._attempt

    def paused_just_before(target):
        update.set_settings(auto_update=False)  # the operator taps Pause mid-pass
        return real(target)

    monkeypatch.setattr(agent_autoupdate, "_attempt", paused_just_before)
    assert agent_autoupdate.run_pass() == "paused"
    assert runs == [] and row()["active"] == old and list(row()["generations"]) == [old]
    monkeypatch.setattr(agent_autoupdate, "_attempt", real)
    update.set_settings(auto_update=True)  # Resume
    assert agent_autoupdate.run_pass() == "ok"
    assert row()["active"] != old and len(runs) == 1


def test_a_failed_notice_that_did_not_get_through_is_delivered_later(recipe, monkeypatch):
    install_active(monkeypatch, at(recipe, "1.0.0"))
    runs = offline(monkeypatch, passed=False)
    rows, broken = [], [True]

    def flaky_add(**fields):
        if broken[0]:
            raise OSError("bell unavailable")
        rows.append(fields)
        return fields

    monkeypatch.setattr(agent_autoupdate.notifications, "add", flaky_add)
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=7)
    agent_autoupdate.run_pass()
    assert rows == [] and [a["state"] for a in attempts()] == ["failed"]
    broken[0] = False
    agent_autoupdate.run_pass()  # same cut
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=8)
    agent_autoupdate.run_pass()  # a later cut with the same failed digest
    assert len(rows) == 1 and len(runs) == 1 and len(attempts()) == 1
    assert attempts()[0]["notified"] is True


def _bell_rows(action_prefix="agent-update-failed:"):
    from agent_sessions import notifications

    return [
        r
        for r in notifications._read(notifications._notifications_path())
        if str(r.get("action_id", "")).startswith(action_prefix)
    ]


@pytest.mark.parametrize("dismissed", [False, True])
def test_a_notice_that_landed_before_a_crash_is_never_posted_again(recipe, monkeypatch, dismissed):
    """The real bell store: the row lands, the process dies before the ledger records it, the
    operator may even dismiss it; the next pass must not announce it again (review 8112)."""
    from agent_sessions import notifications

    install_active(monkeypatch, at(recipe, "1.0.0"))
    offline(monkeypatch, passed=False)
    real_add = notifications.add

    def land_then_die(**fields):
        real_add(**fields)
        raise Crash

    monkeypatch.setattr(agent_autoupdate.notifications, "add", land_then_die)
    serve(monkeypatch, at(recipe, "1.1.0"))
    with pytest.raises(Crash):
        agent_autoupdate.run_pass()
    monkeypatch.setattr(agent_autoupdate.notifications, "add", real_add)
    landed = _bell_rows()
    assert len(landed) == 1 and attempts()[0]["notified"] is False
    if dismissed:
        notifications.dismiss([landed[0]["id"]])
        assert _bell_rows() == []
    agent_autoupdate.run_pass()
    serve(monkeypatch, at(recipe, "1.1.0"), sequence=8)
    agent_autoupdate.run_pass()
    assert len(_bell_rows()) == (0 if dismissed else 1)
    assert attempts()[0]["notified"] is True


def test_two_overlapping_announcers_post_one_notice(recipe, monkeypatch):
    """A manual refresh and the background pass both owing the same notice: one row."""
    install_active(monkeypatch, at(recipe, "1.0.0"))
    offline(monkeypatch, passed=False)
    serve(monkeypatch, at(recipe, "1.1.0"))
    real = agent_autoupdate._set_state

    def never_record_notified(key, state, **fields):
        fields.pop("notified", None)
        return real(key, state, **fields)

    monkeypatch.setattr(agent_autoupdate, "_set_state", never_record_notified)
    agent_autoupdate.run_pass()
    agent_autoupdate.retry_notices()
    agent_autoupdate.retry_notices()
    assert len(_bell_rows()) == 1
