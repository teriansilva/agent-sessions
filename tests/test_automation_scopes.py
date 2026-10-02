"""Independent grants and ownership incarnations, with real stores and PTY fences (#1019)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import tty
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    automation,
    metadata,
    missions,
    orchestrator,
    prefs,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app

KEY = "claude:11111111-1111-4111-8111-111111111111"
OTHER = "claude:22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def isolated():
    session_input.reset()
    yield
    session_input.reset()


def mission():
    mid = missions.create_mission("scoped ownership", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    return mid


def record(key=KEY, mid=None):
    return {
        "id": "scope-action",
        "state": "proposed",
        "session_id": key,
        "verb": "continue",
        "confidence": 1.0,
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "precondition": {},
        **({"mission_id": mid} if mid else {}),
        "authority": automation.capture(key, mid),
    }


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("tier", ["off", "suggest", "yolo"])
def test_cutover_preserves_both_grants_and_disables_downgrade(enabled, tier):
    path = prefs._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "enabled": enabled,
        "autonomy": tier,
        "auto_ai_directions": True,
        "ai_direction_confidence_min": 0.96,
        "judge_confidence_min": 0.97,
        "allowed_verbs": ["continue", "draft_direction"],
        "prompt": "operator prompt",
    }
    path.write_text(json.dumps({"orchestrator": raw, "theme": "light"}))
    before = prefs.get_orchestrator()
    prefs.ensure_automation_policies()
    after = json.loads(path.read_text())
    assert after["orchestrator"]["enabled"] is False
    assert after["orchestrator"]["prompt"] == "operator prompt"
    assert after["theme"] == "light"
    for scope in ("session", "mission"):
        cfg = prefs.get_automation_policy(scope)
        assert cfg["enabled"] == enabled
        assert cfg["autonomy"] == tier
        assert cfg["revision"] not in ("legacy", "invalid")
        assert "prompt" not in after[prefs.AUTOMATION_BLOCKS[scope]]
    assert prefs.get_mission_orchestration()["auto_ai_directions"] == before["auto_ai_directions"]
    assert prefs.get_mission_orchestration()["judge_confidence_min"] == 0.97
    assert prefs.get_session_assistance()["auto_ai_directions"] is False
    assert prefs.get_session_assistance()["allowed_verbs"] == ["continue"]
    assert prefs.public_automation()["legacy_compatible"] is True
    prefs.ensure_automation_policies()
    assert json.loads(path.read_text()) == after  # restart does not revoke current proposals


def test_diverge_downgrade_reupgrade_never_restores_withdrawn_permission():
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    prefs.set_automation_policy("session", {"enabled": False})
    assert prefs.get_mission_orchestration()["enabled"] is True
    assert prefs.get_orchestrator()["enabled"] is False  # conservative legacy API read
    original = prefs._default_path().read_bytes()
    with pytest.raises(prefs.PolicyConflict):
        prefs.set_orchestrator({"enabled": True})
    assert prefs._default_path().read_bytes() == original
    data = json.loads(original)
    data["orchestrator"].update(enabled=True, autonomy="yolo")  # old binary's explicit save
    prefs._default_path().write_text(json.dumps(data))
    assert prefs.get_session_assistance()["enabled"] is False
    prefs.ensure_automation_policies()
    assert prefs.get_session_assistance()["enabled"] is False
    assert prefs.get_mission_orchestration()["enabled"] is True
    assert json.loads(prefs._default_path().read_text())["orchestrator"]["enabled"] is False


@pytest.mark.parametrize(
    "broken",
    [
        None,
        {},
        {"enabled": True},
        {"enabled": True, "autonomy": "yolo", "revision": "bad", "allowed_verbs": ["answer"]},
    ],
)
def test_partial_or_malformed_scoped_store_never_falls_back_to_legacy(broken):
    p = prefs._default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"orchestrator": {"enabled": True, "autonomy": "yolo"}, "session_assistance": broken}
        )
    )
    for scope in ("session", "mission"):
        cfg = prefs.get_automation_policy(scope)
        assert cfg["enabled"] is False
        assert cfg["revision"] == "invalid"


def test_failed_cutover_keeps_file_and_disables_automation(monkeypatch):
    p = prefs._default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"orchestrator":{"enabled":true,"autonomy":"yolo"}}')
    original = p.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("write failed")

    with monkeypatch.context() as m:
        m.setattr(prefs, "atomic_write_json", fail)
        with pytest.raises(OSError):
            prefs.ensure_automation_policies()
    assert p.read_bytes() == original
    assert prefs.get_session_assistance()["enabled"] is False
    assert prefs.get_mission_orchestration()["enabled"] is False
    prefs.ensure_automation_policies()
    assert prefs.get_mission_orchestration()["enabled"] is True


def test_independent_saves_preserve_each_other_and_compare_revision():
    prefs.ensure_automation_policies()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(prefs.set_automation_policy, "session", {"enabled": True, "notify": "none"})
        b = pool.submit(prefs.set_automation_policy, "mission", {"autonomy": "yolo"})
        session, mission_cfg = a.result(), b.result()
    assert prefs.get_session_assistance()["notify"] == "none"
    assert prefs.get_mission_orchestration()["enabled"] is False
    assert prefs.get_mission_orchestration()["autonomy"] == "yolo"
    prefs.set_automation_policy("session", {"enabled": False}, revision=session["revision"])
    with pytest.raises(prefs.PolicyConflict):
        prefs.set_automation_policy("session", {"enabled": True}, revision=session["revision"])
    assert prefs.get_mission_orchestration()["revision"] == mission_cfg["revision"]


def test_mission_only_fields_and_direction_grant_cannot_cross_to_sessions():
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo", "auto_ai_directions": True})
    for patch in (
        {"auto_ai_directions": True},
        {"judge_confidence_min": 0.95},
        {"allowed_verbs": ["draft_direction"]},
    ):
        with pytest.raises(ValueError):
            prefs.set_automation_policy("session", patch)
    prefs.set_automation_policy("session", {"notify": "none"})
    # A legacy mission-only save has an unambiguous destination even after shared divergence.
    prefs.set_orchestrator({"judge_confidence_min": 0.96})
    assert prefs.get_mission_orchestration()["judge_confidence_min"] == 0.96
    prefs.set_automation_policy("mission", {"autonomy": "suggest"})
    prefs.set_automation_policy("mission", {"autonomy": "yolo"})
    assert prefs.get_mission_orchestration()["auto_ai_directions"] is False
    with pytest.raises(ValueError):
        prefs.set_automation_policy("mission", {"ai_direction_confidence_min": 0.89})


def test_roundtrips_restart_and_history_deletion_cannot_resurrect_a_proposal():
    prefs.ensure_automation_policies()
    standalone = record()
    mid = mission()
    missions.adopt(mid, KEY)
    assert automation.check(standalone)[0] is False
    held = record(mid=mid)
    missions.adopt(mid, KEY, role="sub")  # role update is not a new owner
    assert automation.check(held)[0] is True
    missions.detach(mid, KEY)
    missions.adopt(mid, KEY)
    missions.reset_schema_cache_for_test()
    assert automation.check(held)[0] is False
    current = record(mid=mid)
    missions.delete_mission(mid)  # cascade releases active row; revision must survive
    assert automation.check(current)[0] is False
    assert automation.check(standalone)[0] is False
    now = automation.capture(KEY)
    assert now["generation"] and now["mission_id"] is None


def test_stood_down_mission_retains_exclusive_ownership(monkeypatch):
    prefs.ensure_automation_policies()
    mid = mission()
    missions.adopt(mid, KEY)
    missions.instantiate_objectives(
        mid,
        [{"key": "gate", "title": "Gate", "probe": "forge_pr", "gate": True, "source": "playbook"}],
    )
    assert missions.stand_down(mid, "gate", episode=1)
    prefs.set_automation_policy("mission", {"enabled": False})
    with pytest.raises(automation.AuthorityChanged):
        automation.capture(KEY)
    assert automation.capture(KEY, mid)["scope"] == "mission"
    monkeypatch.setattr(
        orchestrator.pulse,
        "build_cards",
        lambda **k: [{"id": KEY, "engine": "claude", "cwd": "/tmp", "last_activity": time.time()}],
    )
    cards, skipped = orchestrator.eligible_cards()
    assert cards == [] and skipped["scope"] == 1


def test_durable_binding_works_without_alias_and_refuses_ambiguous_reverse():
    mid = mission()
    missions.adopt(mid, KEY)
    physical = "claude:new-11111111-1111-4111-8111-111111111111"
    con = missions._ready()
    try:
        missions._bind_runtime_tx(con, KEY, physical, time.time())
        assert missions.automation_ownership(physical) == missions.automation_ownership(KEY)
        assert metadata.load_aliases() == {}
        missions._bind_runtime_tx(con, OTHER, physical, time.time())
        with pytest.raises(missions.MissionError, match="ambiguous"):
            missions.automation_ownership(physical)
    finally:
        con.close()


def test_append_fence_rejects_adoption_after_model_and_old_actions_stay_untrusted():
    prefs.ensure_automation_policies()
    rec = record()
    mid = mission()
    missions.adopt(mid, KEY)
    assert orchestrator._persist([rec]) == []
    assert ledger.get(rec["id"]) is None
    missions.detach(mid, KEY)
    assert orchestrator._persist([rec]) == []  # same final owner still isn't same authority
    legacy = {k: v for k, v in rec.items() if k != "authority"}
    ledger.append(legacy)
    result = asyncio.run(actuator.deliver(rec["id"]))
    assert result["state"] == "stale"
    assert "fresh proposal" in result["detail"]


@pytest.mark.parametrize(
    "changed_scope, expected", [("session", "stale"), ("mission", "delivered")]
)
def test_queued_write_is_cancelled_only_by_its_own_scope(changed_scope, expected, monkeypatch):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(KEY, master, threading.Lock(), "attached")

    def quiet(*args):
        prefs.set_automation_policy(changed_scope, {"enabled": False})
        return True

    monkeypatch.setattr(session_input, "_wait_quiet", quiet)
    try:
        result = session_input.send_input(
            KEY,
            b"scope-proof",
            policy_scope="session",
            policy_fingerprint=lambda: automation.policy_revision("session"),
        )
        assert result.state == expected
        os.set_blocking(slave, False)
        payload = b""
        with contextlib.suppress(BlockingIOError):
            payload = os.read(slave, 1024)
        assert payload == (b"scope-proof" if expected == "delivered" else b"")
    finally:
        os.close(master)
        os.close(slave)


def test_sibling_process_withdrawal_is_seen_without_a_local_epoch():
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    rec = record()
    before = session_input.current_policy_epoch("session")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_sessions import prefs; "
            "prefs.set_automation_policy('session', {'enabled': False})",
        ],
        check=True,
        timeout=15,
    )
    assert session_input.current_policy_epoch("session") == before
    assert automation.check(rec)[0] is False


def test_scoped_api_returns_accepted_revision_and_rejects_ambiguous_legacy_atomically(auth_cfg):
    prefs.ensure_automation_policies()
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    config = client.get("/api/config").json()
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": config["csrf"]}
    initial = config["automation"]
    assert initial["version"] == 1
    response = client.patch(
        "/api/automation/session",
        headers=headers,
        json={"revision": initial["session"]["revision"], "policy": {"enabled": True}},
    )
    assert response.status_code == 200
    accepted = response.json()
    assert accepted["scope"] == "session" and accepted["policy"]["enabled"] is True
    assert accepted["policy"]["revision"] != initial["session"]["revision"]
    original = prefs._default_path().read_bytes()
    rejected = client.post("/api/prefs", headers=headers, json={"orchestrator": {"enabled": True}})
    assert rejected.status_code == 409
    assert prefs._default_path().read_bytes() == original
    mixed = client.post(
        "/api/prefs", headers=headers, json={"theme": "light", "orchestrator": {"enabled": True}}
    )
    assert mixed.status_code == 422
    assert prefs._default_path().read_bytes() == original
    stale = client.patch(
        "/api/automation/session",
        headers=headers,
        json={"revision": initial["session"]["revision"], "policy": {"enabled": False}},
    )
    assert stale.status_code == 409
    assert client.patch("/api/automation/mission", json={"policy": {}}).status_code == 403


def test_upgrade_matches_fresh_schema_and_generation_writes_roll_back():
    mid = mission()
    missions.adopt(mid, KEY)
    con = missions._ready()
    try:
        fresh = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'automation_owner_%' "
            "OR name='session_automation_generations' ORDER BY name"
        ).fetchall()
        for name in ("insert", "release", "adopt", "delete", "menu"):
            con.execute(f"DROP TRIGGER automation_owner_{name}")
        con.execute("DROP TABLE session_automation_generations")
        con.execute("PRAGMA user_version=31")
    finally:
        con.close()
    missions.reset_schema_cache_for_test()
    before = missions.automation_ownership(KEY)
    assert before["generation"] == [[KEY, 1]]
    con = missions._ready()
    try:
        upgraded = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'automation_owner_%' "
            "OR name='session_automation_generations' ORDER BY name"
        ).fetchall()
        assert [tuple(r) for r in upgraded] == [tuple(r) for r in fresh]
        assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE mission_sessions SET removed_at=1 WHERE session_key=?", (KEY,))
        assert (
            con.execute(
                "SELECT generation FROM session_automation_generations WHERE session_key=?", (KEY,)
            ).fetchone()[0]
            == 2
        )
        con.execute("ROLLBACK")
        assert missions.automation_ownership(KEY) == before
        # Even a direct transaction cannot release ownership without advancing its generation.
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE mission_sessions SET removed_at=1 WHERE session_key=?", (KEY,))
        con.execute("COMMIT")
    finally:
        con.close()
    assert missions.automation_ownership(KEY)["generation"] == [[KEY, 2]]


def test_terminal_release_and_retention_keep_the_generation():
    mid = mission()
    missions.adopt(mid, KEY)
    old = record(mid=mid)
    missions.set_state(mid, "running", "failed")
    released = missions.automation_ownership(KEY)
    assert released["mission_id"] is None
    assert not automation.check(old)[0]
    missions.delete_mission(mid)
    assert missions.automation_ownership(KEY) == released


@pytest.mark.parametrize("scope", ["session", "mission"])
@pytest.mark.parametrize("change", ["same", "other", "roundtrip", "ownership", "sibling"])
def test_real_delivery_checks_original_authority_at_byte_one(scope, change, monkeypatch):
    # This tests withdrawal at byte one, not the production five-second write deadline. The
    # injected policy transaction (or sibling interpreter) can exhaust that deadline under CI
    # load, returning a timeout before the authority guard is reached. Keep a bounded budget
    # with enough room for the interleaving; production timeouts remain unchanged.
    monkeypatch.setattr(
        session_input, "send_input", partial(session_input.send_input, timeout_s=30.0)
    )
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    mid = mission()
    if scope == "mission":
        missions.adopt(mid, KEY)
    rec = record(mid=mid if scope == "mission" else None)
    ledger.append(rec)
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(KEY, master, threading.Lock(), "attached")
    monkeypatch.setattr(actuator, "check_precondition", lambda *a, **k: (True, ""))

    # The real actuator/ledger/seam, with a mutation after its early checks and before byte one.
    def quiet(*args):
        if change == "ownership":
            if scope == "mission":
                missions.detach(mid, KEY)
                missions.adopt(mid, KEY)
            else:
                missions.adopt(mid, KEY)
                missions.detach(mid, KEY)
        elif change == "sibling":
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from agent_sessions import prefs; "
                    f"prefs.set_automation_policy('{scope}', {{'enabled': False}})",
                ],
                check=True,
                timeout=15,
            )
        else:
            changed = (
                ("mission" if scope == "session" else "session") if change == "other" else scope
            )
            prefs.set_automation_policy(changed, {"enabled": False})
            if change == "roundtrip":
                prefs.set_automation_policy(changed, {"enabled": True})
        return True

    monkeypatch.setattr(session_input, "_wait_quiet", quiet)
    try:
        result = asyncio.run(actuator.deliver(rec["id"]))
        assert result["state"] == ("delivered" if change == "other" else "stale"), result
        os.set_blocking(slave, False)
        payload = b""
        with contextlib.suppress(BlockingIOError):
            payload = os.read(slave, 4096)
        assert bool(payload) is (change == "other")
    finally:
        os.close(master)
        os.close(slave)


def test_partial_scoped_block_cannot_enable_defaulted_authority():
    prefs.set_orchestrator({"enabled": True})
    path = prefs._default_path()
    data = json.loads(path.read_text())
    del data["session_assistance"]["confidence_min"]
    path.write_text(json.dumps(data))
    assert prefs.get_session_assistance()["revision"] == "invalid"
    assert prefs.get_mission_orchestration()["enabled"] is True
    with pytest.raises(automation.AuthorityChanged, match="unreadable"):
        automation.capture(KEY)


@pytest.mark.parametrize("scope", ["session", "mission"])
@pytest.mark.parametrize(
    "enabled,tier,auto",
    [(False, "yolo", False), (True, "off", False), (True, "suggest", False), (True, "yolo", True)],
)
def test_delivery_matrix_uses_only_original_scope(scope, enabled, tier, auto, monkeypatch):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    prefs.set_automation_policy(scope, {"enabled": enabled, "autonomy": tier})
    other = "mission" if scope == "session" else "session"
    prefs.set_automation_policy(
        other, {"enabled": not enabled, "autonomy": "off" if auto else "yolo"}
    )
    mid = mission() if scope == "mission" else None
    if mid:
        missions.adopt(mid, KEY)
    rec = record(mid=mid)
    called = []

    async def delivered(aid, **kwargs):
        called.append(aid)
        return {"state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", delivered)
    result = asyncio.run(actuator.deliver_auto(rec))
    assert bool(result) is auto
    assert called == ([rec["id"]] if auto else [])


@pytest.mark.parametrize("change", ["none", "adopt", "roundtrip", "own_policy", "other_policy"])
def test_model_result_keeps_its_original_authority(change, monkeypatch):
    from agent_sessions import review

    prefs.set_ai_review(
        {"enabled": True, "base_url": "https://ai.test/v1", "api_key": "test-only", "model": "test"}
    )
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    mid = mission()
    monkeypatch.setattr(
        orchestrator.pulse,
        "build_cards",
        lambda **kw: [{"id": KEY, "engine": "claude", "cwd": "/tmp", "last_activity": time.time()}],
    )
    monkeypatch.setattr(session_input, "is_live", lambda key: True)
    seen = []

    async def complete(messages):
        seen.append(messages)
        if change in ("adopt", "roundtrip"):
            missions.adopt(mid, KEY)
            if change == "roundtrip":
                missions.detach(mid, KEY)
        elif change in ("own_policy", "other_policy"):
            prefs.set_automation_policy(
                "session" if change == "own_policy" else "mission", {"enabled": False}
            )
        return {"actions": [{"session_id": KEY, "verb": "continue", "confidence": 1.0}]}

    monkeypatch.setattr(review, "complete_json", complete)
    report = asyncio.run(orchestrator.run_pass())
    assert len(seen) == 1
    assert bool(report["actions"]) is (change in ("none", "other_policy"))
    assert bool(ledger.live_actions()) is (change in ("none", "other_policy"))


def test_durable_binding_does_not_bypass_an_unreadable_opt_out_store(monkeypatch):
    con = missions._ready()
    try:
        missions._bind_runtime_tx(con, KEY, OTHER, time.time())
    finally:
        con.close()

    def unreadable(*args, **kwargs):
        raise metadata.MetadataUnreadable("test unreadable sidecar")

    monkeypatch.setattr(metadata, "load_checked", unreadable)
    with pytest.raises(metadata.MetadataUnreadable):
        automation.capture(KEY)


def test_bound_runtime_survives_missing_alias_in_proposal_and_housekeeping(monkeypatch):
    prefs.ensure_automation_policies()
    mid = mission()
    missions.adopt(mid, KEY)
    con = missions._ready()
    try:
        missions._bind_runtime_tx(con, KEY, OTHER, time.time())
    finally:
        con.close()
    monkeypatch.setattr(session_input, "is_live", lambda key: key == OTHER)
    rec = record(mid=mid)
    _, validated = orchestrator._validate_actions(
        {"actions": [{"session_id": KEY, "verb": "continue", "confidence": 1.0}]},
        {KEY: {}},
        cfg=prefs.get_mission_orchestration(),
    )
    assert len(validated) == 1
    ledger.append(rec)
    assert actuator.withdraw_undeliverable() == []
    assert ledger.get(rec["id"])["state"] == "proposed"


def test_session_staleness_setting_does_not_filter_mission_actions(monkeypatch):
    prefs.ensure_automation_policies()
    prefs.set_automation_policy("session", {"stale_hours": 1})
    prefs.set_automation_policy("mission", {"stale_hours": 24})
    monkeypatch.setattr(session_input, "is_live", lambda key: True)
    obj = {"actions": [{"session_id": KEY, "verb": "continue", "confidence": 1.0}]}
    sent = {KEY: {"last_activity": time.time() - 7200}}
    assert orchestrator._validate_actions(obj, sent, cfg=prefs.get_session_assistance())[1] == []
    assert (
        len(orchestrator._validate_actions(obj, sent, cfg=prefs.get_mission_orchestration())[1])
        == 1
    )


def test_rejected_legacy_save_does_not_cancel_either_scope():
    prefs.set_orchestrator({"enabled": True})
    prefs.set_automation_policy("session", {"notify": "none"})
    before = [session_input.current_policy_epoch(s) for s in (None, "session", "mission")]
    with pytest.raises(prefs.PolicyConflict):
        prefs.set_orchestrator({"enabled": False})
    assert [session_input.current_policy_epoch(s) for s in (None, "session", "mission")] == before


def test_mission_launch_fingerprint_ignores_session_policy_and_refuses_unreadable_store():
    prefs.ensure_automation_policies()
    original = session_input.policy_fingerprint()
    prefs.set_automation_policy("session", {"enabled": True})
    assert session_input.policy_fingerprint() == original
    prefs.set_automation_policy("mission", {"enabled": True})
    assert session_input.policy_fingerprint() != original
    prefs._default_path().write_text("{unreadable")
    assert session_input.policy_fingerprint() is None


def test_candidate_scan_reads_the_whole_sidecar_once(monkeypatch):
    prefs.ensure_automation_policies()
    read = metadata.load_checked
    calls = []

    def checked(*args, **kwargs):
        calls.append(1)
        return read(*args, **kwargs)

    monkeypatch.setattr(metadata, "load_checked", checked)
    keys = [f"claude:{i:08d}-1111-4111-8111-111111111111" for i in range(100)]
    assert len(automation.capture_candidates(keys)) == len(keys)
    assert calls == [1], "candidate selection decoded the fleet metadata once per session"


def test_menu_answer_cannot_replace_the_original_escalation_authority():
    from agent_sessions import menu_answer

    rec = record()
    rec.update(verb="escalate", state="escalated")
    ledger.append(rec)
    mid = mission()
    missions.adopt(mid, KEY)
    with pytest.raises(menu_answer.Refused, match="different automation scope"):
        menu_answer.prepare(rec["id"], 1, "Continue")


@pytest.mark.parametrize("when", ["before", "wait", "guard"])
def test_durable_physical_opt_out_refuses_real_delivery_without_alias(monkeypatch, when):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    mid = mission()
    missions.adopt(mid, KEY)
    con = missions._ready()
    try:
        missions._bind_runtime_tx(con, KEY, OTHER, time.time())
    finally:
        con.close()
    assert metadata.load_aliases() == {}
    rec = record(mid=mid)
    ledger.append(rec)
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(OTHER, master, threading.Lock(), "headless")

    def exclude():
        metadata.patch(OTHER, orchestrator_excluded=True)

    if when == "before":
        exclude()
    elif when == "wait":

        def quiet(*args):
            exclude()
            return True

        monkeypatch.setattr(session_input, "_wait_quiet", quiet)
    else:
        original = session_input._write_all

        def write(*args, **kwargs):
            guard = kwargs["final_guard"]

            def withdraw_after_guard():
                verdict = guard()
                assert verdict == (True, "")
                exclude()
                return verdict

            kwargs["final_guard"] = withdraw_after_guard
            return original(*args, **kwargs)

        monkeypatch.setattr(session_input, "_write_all", write)
    try:
        result = asyncio.run(actuator.deliver_auto(rec))
        assert result["state"] == "stale"
        assert ledger.get(rec["id"])["state"] == "stale"
        os.set_blocking(slave, False)
        with pytest.raises(BlockingIOError):
            os.read(slave, 4096)
    finally:
        os.close(master)
        os.close(slave)


def test_durable_opt_out_keeps_logical_metadata_precedence():
    metadata.patch(OTHER, orchestrator_excluded=True)
    assert actuator._action_excluded(KEY, OTHER) is True
    metadata.patch(KEY, orchestrator_excluded=False)
    assert actuator._action_excluded(KEY, OTHER) is False


@pytest.mark.parametrize("failure", ["metadata", "precondition", "final_guard", "uncertain_write"])
def test_post_claim_failure_settles_without_replay(monkeypatch, failure):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    rec = record()
    ledger.append(rec)
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    claim = ledger.claim

    def unreadable(*args, **kwargs):
        raise metadata.MetadataUnreadable("injected after claim")

    def claimed(*args, **kwargs):
        result = claim(*args, **kwargs)
        assert result is not None
        if failure == "metadata":
            monkeypatch.setattr(metadata, "load_checked", unreadable)
        elif failure == "precondition":
            monkeypatch.setattr(actuator, "check_precondition", unreadable)
        return result

    monkeypatch.setattr(ledger, "claim", claimed)
    if failure == "final_guard":
        original = session_input._write_all

        def write(*args, **kwargs):
            kwargs["final_guard"] = unreadable
            return original(*args, **kwargs)

        monkeypatch.setattr(session_input, "_write_all", write)
    elif failure == "uncertain_write":

        def uncertain(*args, **kwargs):
            os.write(master, b"already-sent")
            raise RuntimeError("seam failed after sending")

        monkeypatch.setattr(session_input, "send_input", uncertain)
    try:
        result = asyncio.run(actuator.deliver_auto(rec))
        expected = "indeterminate" if failure == "uncertain_write" else "stale"
        assert result["state"] == ledger.get(rec["id"])["state"] == expected
        with pytest.raises(actuator.NotDeliverable):
            asyncio.run(actuator.deliver(rec["id"]))
        os.set_blocking(slave, False)
        if failure == "uncertain_write":
            assert os.read(slave, 4096) == b"already-sent"
        else:
            with pytest.raises(BlockingIOError):
                os.read(slave, 4096)
    finally:
        os.close(master)
        os.close(slave)


def test_legacy_mission_only_route_uses_mission_policy_and_rechecks_locked_save(
    auth_cfg, monkeypatch
):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    prefs.set_automation_policy("session", {"enabled": False, "autonomy": "off"})
    standalone = prefs.get_session_assistance()
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    patch = {"orchestrator": {"auto_ai_directions": True}}
    assert client.post("/api/prefs", headers=headers, json=patch).status_code == 200
    assert prefs.get_mission_orchestration()["auto_ai_directions"] is True
    assert prefs.get_session_assistance() == standalone
    assert prefs.get_orchestrator()["autonomy"] == "off"
    assert (
        client.post(
            "/api/prefs", headers=headers, json={"orchestrator": {"enabled": True}}
        ).status_code
        == 409
    )

    prefs.set_automation_policy("mission", {"auto_ai_directions": False})
    save = prefs.set_orchestrator

    def race(part):
        prefs.set_automation_policy("mission", {"autonomy": "suggest"})
        return save(part)

    monkeypatch.setattr(prefs, "set_orchestrator", race)
    assert client.post("/api/prefs", headers=headers, json=patch).status_code == 422
    assert prefs.get_mission_orchestration()["auto_ai_directions"] is False
    assert prefs.get_session_assistance() == standalone
