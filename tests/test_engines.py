"""Engine-provider registry + engine-qualified identity.

These pin the seam #12 (opencode) builds on: a present-providers registry, a
merged scan, and ``parse_key`` as the single id-validation gate (with bare-UUID
back-compat for Claude). The Claude provider is a thin adapter over the existing
scanner/archive modules — those keep their own dedicated tests.
"""

from __future__ import annotations

import pytest

from agent_sessions import engines, scanner

_U1 = "11111111-1111-1111-1111-111111111111"


# ---- registry + scan ----------------------------------------------------------


def test_present_providers_includes_claude(fake_jsonl):
    ids = {p.engine_id for p in engines.present_providers()}
    assert "claude" in ids


def test_scan_all_returns_claude_sessions(fake_jsonl):
    rows = engines.scan_all()
    assert {r.uuid for r in rows} >= {_U1, "55555555-5555-5555-5555-555555555555"}
    assert all(r.engine == "claude" for r in rows)


def test_absent_provider_drops_out(tmp_home, monkeypatch):
    # No ~/.claude/projects under tmp_home and no claude on PATH → claude absent.
    monkeypatch.setattr(engines.shutil, "which", lambda _name: None)
    assert engines.present_providers() == []
    assert engines.scan_all() == []


# ---- engine-qualified identity ------------------------------------------------


def test_session_key_is_engine_qualified(fake_jsonl):
    s = next(r for r in engines.scan_all() if r.uuid == _U1)
    assert engines.session_key(s) == f"claude:{_U1}"


def test_parse_key_qualified():
    prov, native = engines.parse_key(f"claude:{_U1}")
    assert prov.engine_id == "claude" and native == _U1


def test_parse_key_bare_uuid_is_claude_backcompat():
    prov, native = engines.parse_key(_U1)
    assert prov.engine_id == "claude" and native == _U1


def test_canonical_key_normalizes_bare_uuid():
    assert engines.canonical_key(_U1) == f"claude:{_U1}"
    assert engines.canonical_key(f"claude:{_U1}") == f"claude:{_U1}"


@pytest.mark.parametrize(
    "bad",
    [
        "bogus:whatever",  # unknown engine
        "claude:not-a-uuid",  # right engine, wrong native shape
        "not-a-uuid",  # bare, not a claude uuid
        "ses_26484f850ffei7OMJwLTM9MLgn",  # bare ses_ → parsed as claude, fails uuid shape
        "opencode:not-a-ses",  # right engine, wrong native shape
    ],
)
def test_parse_key_rejects_bad_ids(bad):
    with pytest.raises(engines.EngineError):
        engines.parse_key(bad)


# ---- Claude provider launch argv (ws PTY bridge) ------------------------------


def test_claude_launch_argv_resume(monkeypatch):
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")
    argv = engines.ClaudeProvider().launch_argv(_U1, cwd="/tmp/x", bypass=True)
    assert argv == ["claude", "--resume", _U1, "--dangerously-skip-permissions"]
    assert engines.ClaudeProvider().launch_argv(_U1, cwd="/tmp/x", bypass=False) == [
        "claude",
        "--resume",
        _U1,
    ]


def test_claude_new_launch_argv(monkeypatch):
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")
    argv = engines.ClaudeProvider().new_launch_argv(_U1, cwd="/tmp/x", bypass=True)
    assert argv == ["claude", "--session-id", _U1, "--dangerously-skip-permissions"]


def test_claude_archive_moves_the_jsonl(fake_jsonl):
    # Real delegation to the archive module: the live JSONL moves to the archive tree.
    engines.ClaudeProvider().archive(_U1)
    rows = engines.scan_all()
    moved = next(r for r in rows if r.uuid == _U1)
    assert moved.archived is True


# ---- opencode provider (#12) --------------------------------------------------

_OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
_OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"
_OC_FORK = "ses_ffffffffffffffffffffffff"
_OC_ACT = "ses_cccccccccccccccccccccccc"


def test_opencode_present_when_db_readable(opencode_db):
    assert "opencode" in {p.engine_id for p in engines.present_providers()}


def test_opencode_scan_top_level_only(opencode_db):
    ids = {r.uuid for r in engines.scan_all() if r.engine == "opencode"}
    assert ids == {_OC_TOP, _OC_ARCHIVED}  # fork excluded; ephemeral CI row filtered
    assert _OC_FORK not in ids  # parent_id set
    assert _OC_ACT not in ids  # ~/.cache/act CI workdir (#452)


def test_opencode_scan_drops_ephemeral_ci_sessions(opencode_db):
    """#452: an opencode session whose cwd is an ephemeral ``~/.cache/act`` CI
    workdir is dropped at scan time, so it never reaches the sidebar, the resume
    allowlist (``scanned_cwds``), or the new-session picker."""
    sessions = engines.scan_all()
    assert _OC_ACT not in {r.uuid for r in sessions}
    # No ``.cache/act`` cwd may leak into any scan-derived surface.
    assert not any("/.cache/act/" in c for c in scanner.scanned_cwds(sessions))
    assert not any("/.cache/act/" in c for c in scanner.pickable_projects(sessions=sessions))
    # A real opencode session in the same DB still shows up.
    assert _OC_TOP in {r.uuid for r in sessions}


def test_opencode_time_normalized_to_seconds(opencode_db):
    top = next(r for r in engines.scan_all() if r.uuid == _OC_TOP)
    # 1777460564154 ms → ~1777460564.154 s (not left in milliseconds)
    assert 1_700_000_000 < top.last_mtime < 2_000_000_000
    assert abs(top.last_mtime - 1777460564.154) < 1


def test_opencode_archived_and_title(opencode_db):
    rows = {r.uuid: r for r in engines.scan_all() if r.engine == "opencode"}
    assert rows[_OC_ARCHIVED].archived is True
    assert rows[_OC_TOP].archived is False
    assert rows[_OC_TOP].first_user_message == "OC top one"  # native opencode title


def test_opencode_fail_soft_missing_db(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / "nope.db"))
    prov = engines.OpenCodeProvider()
    assert prov.is_present() is False
    assert prov.scan() == []


def test_opencode_fail_soft_corrupt_db(tmp_home, monkeypatch):
    bad = tmp_home / "corrupt.db"
    bad.write_text("this is not a sqlite database")
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(bad))
    prov = engines.OpenCodeProvider()
    assert prov.is_present() is False
    assert prov.scan() == []


def test_parse_key_opencode():
    prov, native = engines.parse_key(f"opencode:{_OC_TOP}")
    assert prov.engine_id == "opencode" and native == _OC_TOP


def test_opencode_archive_unarchive_via_sidecar(tmp_path, monkeypatch):
    # opencode.db stays read-only; the archive flag rides the engine-agnostic sidecar.
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "metadata.json"))
    engines.OpenCodeProvider().archive(_OC_TOP)
    assert engines._metadata.get(f"opencode:{_OC_TOP}").archived is True
    engines.OpenCodeProvider().unarchive(_OC_TOP)
    assert engines._metadata.get(f"opencode:{_OC_TOP}").archived is False


def test_opencode_launch_argv(monkeypatch):
    monkeypatch.setattr(engines.base, "OPENCODE_BIN", "opencode")
    argv = engines.OpenCodeProvider().launch_argv(_OC_TOP, cwd="/tmp/other", bypass=True)
    assert argv == ["opencode", "/tmp/other", "--session", _OC_TOP]


def test_supports_new_agrees_with_new_launch_argv():
    # Invariant (#64 review): /api/config advertises new_session_engines from
    # supports_new, and the ws new-session path calls new_launch_argv. A provider that
    # claims supports_new but whose new_launch_argv raises NotImplementedError would
    # offer a new-session option that closes the ws with 4404. Keep the two in lockstep.
    for prov in engines.all_providers():
        if not getattr(prov, "supports_new", False):
            continue
        argv = prov.new_launch_argv("00000000-0000-0000-0000-000000000000", cwd="/tmp", bypass=True)
        assert isinstance(argv, list) and argv, f"{prov.engine_id} new_launch_argv must yield argv"


def test_opencode_advertises_new_session_via_reconcile():
    # #127: opencode now supports new-session via launch-then-reconcile. supports_new is
    # True and new_launch_argv yields a bare `opencode <dir>` (NO --session: opencode
    # mints its own id, which the reconcile discovers).
    p = engines.OpenCodeProvider()
    assert p.supports_new is True
    argv = p.new_launch_argv("new-x", cwd="/tmp/proj", bypass=True)
    assert argv == [engines.OPENCODE_BIN, "/tmp/proj"]
    assert "--session" not in argv  # never pins an id; opencode creates a fresh one
