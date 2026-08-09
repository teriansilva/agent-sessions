"""TOTP 2FA store + verification core (issue #116, Phase 1).

Pins the security-load-bearing properties: confirmed-only enablement, ±1 window verify,
replay rejection that survives a store reload, one-time recovery codes (incl. a concurrent
race), 0600 file perms, and the host clear() escape hatch.
"""

from __future__ import annotations

import json
import re
import threading

import pyotp
import pytest

from agent_sessions import twofactor


@pytest.fixture
def store(tmp_path, monkeypatch):
    p = tmp_path / "2fa.json"
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(p))
    return p


T0 = 1_700_000_000  # fixed base instant for deterministic step math


def _enroll_and_confirm(store, now=T0):
    """Enroll and confirm at ``now``; the replay cursor is seeded with that step."""
    info = twofactor.begin_enrollment("marcus", store)
    code = pyotp.TOTP(info["secret"]).at(now)
    assert twofactor.confirm_enrollment(code, store, now=now) is True
    return info


def test_disabled_by_default(store):
    assert twofactor.is_enabled(store) is False
    assert twofactor.check_totp("000000", store) is False
    assert twofactor.login_totp_outcome("000000", store) == twofactor.TOTP_INVALID


def test_enrollment_is_two_phase_and_confirm_only(store):
    info = twofactor.begin_enrollment("marcus", store)
    assert info["otpauth_uri"].startswith("otpauth://totp/")
    assert len(info["recovery_codes"]) == twofactor.RECOVERY_COUNT
    # Pending — not enabled until a correct code confirms.
    assert twofactor.is_enabled(store) is False
    assert twofactor.confirm_enrollment("000000", store) is False
    assert twofactor.is_enabled(store) is False
    assert twofactor.confirm_enrollment(pyotp.TOTP(info["secret"]).now(), store) is True
    assert twofactor.is_enabled(store) is True


def test_secret_entropy_at_least_160_bits(store):
    info = twofactor.begin_enrollment("marcus", store)
    # base32 → 5 bits/char; 32 chars = 160 bits.
    assert len(info["secret"]) >= 32


def test_match_step_window(store):
    info = twofactor.begin_enrollment("marcus", store)
    secret = info["secret"]
    totp = pyotp.TOTP(secret)
    now = 1_700_000_000  # fixed instant
    cur = now // twofactor.STEP_SECONDS
    # ±1 step all verify; ±2 does not.
    for off in (-1, 0, 1):
        code = totp.at((cur + off) * twofactor.STEP_SECONDS)
        assert twofactor._match_step(secret, code, now=now) == cur + off
    far = totp.at((cur + 2) * twofactor.STEP_SECONDS)
    assert twofactor._match_step(secret, far, now=now) is None
    assert twofactor._match_step(secret, "not-a-code", now=now) is None


def test_login_verify_then_replay_rejected(store):
    info = _enroll_and_confirm(store, now=T0)
    later = T0 + twofactor.STEP_SECONDS  # next step (the confirm code can't be reused)
    code = pyotp.TOTP(info["secret"]).at(later)
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_OK
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_REPLAYED


def test_confirm_code_cannot_be_replayed_at_login(store):
    """Seeding the cursor with the confirm step blocks reusing that same code to log in."""
    info = _enroll_and_confirm(store, now=T0)
    confirm_code = pyotp.TOTP(info["secret"]).at(T0)
    assert twofactor.login_totp_outcome(confirm_code, store, now=T0) == twofactor.TOTP_REPLAYED


def test_replay_cursor_persists_across_reload(store):
    info = _enroll_and_confirm(store, now=T0)
    later = T0 + twofactor.STEP_SECONDS
    code = pyotp.TOTP(info["secret"]).at(later)
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_OK
    # Simulate a process restart: nothing in-memory, only the file. The just-used step must
    # still be rejected (the cursor is persisted, not held in RAM).
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_REPLAYED


def test_recovery_codes_one_time_use(store):
    info = _enroll_and_confirm(store)
    code = info["recovery_codes"][0]
    assert twofactor.recovery_remaining(store) == twofactor.RECOVERY_COUNT
    assert twofactor.verify_recovery_for_login(code, store) is True
    assert twofactor.verify_recovery_for_login(code, store) is False  # consumed
    assert twofactor.recovery_remaining(store) == twofactor.RECOVERY_COUNT - 1
    # Case/format-insensitive: the hyphen-free upper form of another code also works once.
    other = info["recovery_codes"][1].replace("-", "").upper()
    assert twofactor.verify_recovery_for_login(other, store) is True


def test_recovery_code_race_consumes_exactly_once(store):
    """A recovery code used concurrently must succeed at most once (flock-serialized RMW)."""
    info = _enroll_and_confirm(store)
    code = info["recovery_codes"][0]
    results: list[bool] = []
    lock = threading.Lock()

    def attempt():
        ok = twofactor.verify_recovery_for_login(code, store)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    assert twofactor.recovery_remaining(store) == twofactor.RECOVERY_COUNT - 1


def test_check_totp_does_not_consume_or_advance(store):
    info = _enroll_and_confirm(store, now=T0)
    later = T0 + twofactor.STEP_SECONDS
    code = pyotp.TOTP(info["secret"]).at(later)
    # Non-mutating proof check: repeatable, and does NOT block a later login with the same
    # step (check_totp must not advance the replay cursor).
    assert twofactor.check_totp(code, store, now=later) is True
    assert twofactor.check_totp(code, store, now=later) is True
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_OK


def test_regenerate_recovery_replaces_codes(store):
    info = _enroll_and_confirm(store)
    old = info["recovery_codes"][0]
    new = twofactor.regenerate_recovery(store)
    assert new is not None and len(new) == twofactor.RECOVERY_COUNT
    assert old not in new
    # Old codes no longer work; new ones do.
    assert twofactor.verify_recovery_for_login(old, store) is False
    assert twofactor.verify_recovery_for_login(new[0], store) is True


def test_disable_clears_secrets(store):
    _enroll_and_confirm(store)
    twofactor.disable(store)
    assert twofactor.is_enabled(store) is False
    assert twofactor.recovery_remaining(store) == 0
    assert twofactor.regenerate_recovery(store) is None  # can't regen when disabled


def test_file_is_0600(store):
    _enroll_and_confirm(store)
    assert store.exists()
    assert (store.stat().st_mode & 0o777) == 0o600


def test_clear_removes_file(store):
    _enroll_and_confirm(store)
    assert twofactor.clear(store) is True
    assert not store.exists()
    assert twofactor.is_enabled(store) is False
    assert twofactor.clear(store) is False  # already gone


def test_corrupt_store_fails_closed(store):
    # A present-but-unreadable store must NOT downgrade to "disabled" (that would bypass
    # the second factor). is_enabled → True (fail closed); verifies → False (so login is
    # locked until the file is cleared / 2FA disabled with the password).
    store.write_text("this is not json {{{")
    assert twofactor.is_enabled(store) is True
    assert twofactor.login_totp_outcome("123456", store) == twofactor.TOTP_INVALID
    assert twofactor.verify_recovery_for_login("aaaa-bbbb-cccc", store) is False
    assert twofactor.check_totp("123456", store) is False


def test_missing_store_is_disabled():
    # A *missing* file (vs corrupt) means "not enrolled" → disabled (no fail-closed).
    from pathlib import Path

    assert twofactor.is_enabled(Path("/nonexistent/dir/2fa.json")) is False


def test_disable_recovers_from_corrupt_store(store):
    store.write_text("garbage")
    twofactor.disable(store)  # must succeed without reading the corrupt file
    assert twofactor.is_enabled(store) is False


def test_clear_2fa_cli(store, capsys):
    from agent_sessions import cli

    _enroll_and_confirm(store)
    assert twofactor.is_enabled(store) is True
    # The CLI resolves the file from AGENT_SESSIONS_2FA_FILE (set by the `store` fixture).
    assert cli.main(["clear-2fa"]) == 0
    assert "2FA disabled" in capsys.readouterr().out
    assert twofactor.is_enabled(store) is False


def test_replay_is_reported_apart_from_invalid(store):
    """#814: a spent-but-correct code must not read as a wrong one.

    Two login tabs share one 30s code — whichever submits second is the operator's *correct*
    code being refused, and "invalid code" sends them hunting a problem that fixes itself on
    the next tick.
    """
    info = _enroll_and_confirm(store, now=T0)
    later = T0 + twofactor.STEP_SECONDS
    code = pyotp.TOTP(info["secret"]).at(later)
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_OK
    assert twofactor.login_totp_outcome(code, store, now=later) == twofactor.TOTP_REPLAYED
    # A genuinely wrong code stays INVALID...
    assert twofactor.login_totp_outcome("000000", store, now=later) == twofactor.TOTP_INVALID
    # ...and neither refusal moves the cursor past the one step that was actually consumed.
    assert json.loads(store.read_text())["last_step"] == later // twofactor.STEP_SECONDS


def test_totp_shaped_code_never_touches_the_recovery_hashes(store, monkeypatch):
    """#815: a 6-digit TOTP can't be a recovery code, so it must cost zero key derivations.

    Pinned by counting comparisons, not wall-clock. Before the shape gate one wrong code
    walked all RECOVERY_COUNT hashes — 5.25s of PBKDF2 on the production host, which made
    the login page look hung and drove the resubmit that then failed as a replay (#814).
    """
    _enroll_and_confirm(store)
    calls: list[str] = []
    real = twofactor.verify_password

    def counting(candidate, hashed):
        calls.append(candidate)
        return real(candidate, hashed)

    monkeypatch.setattr(twofactor, "verify_password", counting)

    assert twofactor.verify_recovery_for_login("123456", store) is False
    assert calls == []  # zero derivations for a TOTP-shaped code
    # A recovery-*shaped* miss still compares against every stored hash — the gate narrows
    # the input, it doesn't weaken recovery-code brute-force cost.
    assert twofactor.verify_recovery_for_login("dead-beef-cafe", store) is False
    assert len(calls) == twofactor.RECOVERY_COUNT


def test_shape_gate_accepts_every_minted_code(store):
    """The gate is derived from the generator, so the two can't drift apart."""
    info = _enroll_and_confirm(store)
    for code in info["recovery_codes"]:
        assert twofactor._looks_like_recovery(twofactor._normalize_recovery(code)) is True
    # And a real code still verifies through the gate in its forgiving normalized forms.
    assert twofactor.verify_recovery_for_login(info["recovery_codes"][0].upper(), store) is True
    for junk in ("", "123456", "12345678901", "1234567890123", "zzzz-zzzz-zzzz"):
        assert twofactor._looks_like_recovery(twofactor._normalize_recovery(junk)) is False


@pytest.mark.parametrize("nbytes", [4, 6, 8])
def test_generator_and_shape_gate_share_one_length_contract(store, monkeypatch, nbytes):
    """#815: move the entropy constant and both sides must move with it.

    The regression Hermes caught: the shape gate derived its length from RECOVERY_BYTES
    while the formatter hard-cut three 4-char groups, so raising the constant minted codes
    that were then rejected as "not one of ours" (and silently dropped entropy).
    """
    monkeypatch.setattr(twofactor, "RECOVERY_BYTES", nbytes)
    _enroll_and_confirm(store)
    codes = twofactor.regenerate_recovery(store)
    assert codes is not None
    for code in codes:
        normalized = twofactor._normalize_recovery(code)
        assert len(normalized) == nbytes * 2  # no truncation of the minted token
        assert twofactor._looks_like_recovery(normalized) is True
    # A freshly minted code still logs in at a non-default length.
    assert twofactor.verify_recovery_for_login(codes[0], store) is True


def test_default_recovery_code_format_is_unchanged(store):
    """The shipped format stays xxxx-xxxx-xxxx — operators' stored codes keep working."""
    info = _enroll_and_confirm(store)
    for code in info["recovery_codes"]:
        assert re.fullmatch(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", code)
