"""Cross-instance ownership (#293) — the take-over arbitration matrix.

These exercise ``owner.py`` directly against a real (tmp) runtime dir, since the
whole point of the module is that arbitration is anchored on disk and therefore
correct across two app processes sharing ``AGENT_SESSIONS_RUNTIME_DIR``. Two
distinct ``conn_id``s claiming the same ``(engine, sid)`` is exactly the
prod+staging case — there is no separate "instance" coupling to mock.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import owner

ENG, SID = "claude", "ses_abc-1.2"  # a dot in the id — guards the _paths naming


@pytest.fixture(autouse=True)
def _runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))


def _claim(conn_id, fp, tab, *, label="", force=False):
    return owner._claim_sync(ENG, SID, conn_id=conn_id, fp=fp, tab_id=tab, label=label, force=force)


def _age_record(seconds: float) -> None:
    """Backdate the on-disk ``last_seen`` so the holder reads as stale/gone."""
    owner_p, _ = owner._paths(ENG, SID)
    rec = json.loads(owner_p.read_text())
    rec["last_seen"] = rec["last_seen"] - seconds
    owner_p.write_text(json.dumps(rec))


def test_first_caller_becomes_owner():
    role, displaced = _claim("c1", "fpA", "t1", label="Mac · Chrome")
    assert role == "owner"
    assert displaced is None
    rec = owner.read_owner(ENG, SID)
    assert rec["conn_id"] == "c1"
    assert rec["label"] == "Mac · Chrome"
    assert owner.owns(ENG, SID, "c1")


def test_same_device_reconnect_reclaims_and_preserves_since():
    role, _ = _claim("c1", "fpA", "t1")
    since0 = owner.read_owner(ENG, SID)["since"]
    # Same (fp, tab) reconnecting with a NEW conn_id — it's the holder coming back.
    role2, displaced = _claim("c1b", "fpA", "t1")
    assert role2 == "owner"
    assert displaced is None
    rec = owner.read_owner(ENG, SID)
    assert rec["conn_id"] == "c1b"
    assert rec["since"] == since0  # continuous possession, not reset


def test_different_live_device_is_passive():
    _claim("c1", "fpA", "t1", label="Mac · Chrome")
    role, holder = _claim("c2", "fpB", "t2")
    assert role == "passive"
    assert holder["conn_id"] == "c1"
    assert holder["label"] == "Mac · Chrome"  # surfaced for the gate
    assert owner.owns(ENG, SID, "c1")  # holder unchanged
    assert not owner.owns(ENG, SID, "c2")


def test_same_fingerprint_second_tab_is_passive_without_churn():
    # #434 Phase 3: one session open in TWO tabs of the SAME browser (same fp, different
    # tab) must settle, not storm. The second tab lands passive (so the client streams it
    # read-only via the take-over banner — never blank) and, crucially, does NOT displace
    # the first: a passive claim never writes the record, so the owner's lease keeps
    # beating and there is no claim/demote war.
    role_a, _ = _claim("tabA", "fpX", "tA", label="Windows · Chrome")
    assert role_a == "owner"
    role_b, holder = _claim("tabB", "fpX", "tB", label="Windows · Chrome")
    assert role_b == "passive"
    assert holder["conn_id"] == "tabA"  # tab A still owns; tab B did not steal it
    # No churn: tab A was never displaced, so its heartbeat still succeeds.
    assert owner._heartbeat_sync(ENG, SID, "tabA") is True
    assert owner.owns(ENG, SID, "tabA")
    assert not owner.owns(ENG, SID, "tabB")


def test_force_takes_over_a_live_holder_and_returns_displaced():
    _claim("c1", "fpA", "t1")
    role, displaced = _claim("c2", "fpB", "t2", force=True)
    assert role == "owner"
    assert displaced is not None and displaced["conn_id"] == "c1"
    assert owner.owns(ENG, SID, "c2")
    assert not owner.owns(ENG, SID, "c1")


def test_stale_holder_does_not_block_a_new_device():
    # Holder crashed without releasing; lease expired → its ghost must not gate.
    _claim("c1", "fpA", "t1")
    _age_record(owner.LEASE_S + 5)
    role, displaced = _claim("c2", "fpB", "t2")  # NO force
    assert role == "owner"  # gone holder auto-cleared
    assert displaced is not None and displaced["conn_id"] == "c1"
    assert owner.owns(ENG, SID, "c2")


def test_heartbeat_keeps_a_holder_live_then_fails_after_takeover():
    _claim("c1", "fpA", "t1")
    assert owner._heartbeat_sync(ENG, SID, "c1") is True
    # A different device can't steal a freshly-beating holder without force.
    assert _claim("c2", "fpB", "t2")[0] == "passive"
    # Explicit take-over, then the old holder's heartbeat must fail (it lost it).
    _claim("c2", "fpB", "t2", force=True)
    assert owner._heartbeat_sync(ENG, SID, "c1") is False
    assert owner._heartbeat_sync(ENG, SID, "c2") is True


def test_release_clears_only_the_matching_connection():
    _claim("c1", "fpA", "t1")
    _claim("c2", "fpB", "t2", force=True)  # c2 now owns
    # The displaced c1 cleaning up must NOT wipe c2's ownership.
    assert owner._release_sync(ENG, SID, "c1") is False
    assert owner.owns(ENG, SID, "c2")
    # The real owner releasing clears it.
    assert owner._release_sync(ENG, SID, "c2") is True
    assert owner.read_owner(ENG, SID) is None


def test_two_new_devices_race_exactly_one_owner():
    # Both attach (no force) against an empty record; the flock serialises them,
    # so the first writes and the second sees a live other → passive. Never two.
    r1, _ = _claim("c1", "fpA", "t1")
    r2, holder = _claim("c2", "fpB", "t2")
    assert {r1, r2} == {"owner", "passive"}
    assert holder["conn_id"] == "c1"


def test_async_wrappers_roundtrip():
    async def go():
        role, _ = await owner.claim(ENG, SID, conn_id="c1", fp="fpA", tab_id="t1")
        assert role == "owner"
        assert await owner.heartbeat(ENG, SID, "c1") is True
        assert await owner.release(ENG, SID, "c1") is True
        assert owner.read_owner(ENG, SID) is None

    asyncio.run(go())


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("  yes ", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_takeover_enabled_truthiness(monkeypatch, value, expected):
    # #434: the flag is OFF for anything but an explicit truthy value, so an unset/empty/
    # false env (the default everywhere, including every script install) never enables it.
    monkeypatch.setenv("AGENT_SESSIONS_TAKEOVER", value)
    assert owner.takeover_enabled() is expected


def test_takeover_enabled_defaults_off_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_TAKEOVER", raising=False)
    assert owner.takeover_enabled() is False
