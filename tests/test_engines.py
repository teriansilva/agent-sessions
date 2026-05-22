"""Engine-provider registry + engine-qualified identity.

These pin the seam #12 (opencode) builds on: a present-providers registry, a
merged scan, and ``parse_key`` as the single id-validation gate (with bare-UUID
back-compat for Claude). The Claude provider is a thin adapter over the existing
scanner/zellij/archive modules — those keep their own dedicated tests.
"""

from __future__ import annotations

import pytest

from agent_sessions import engines

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


# ---- Claude provider delegates (thin adapter) ---------------------------------


def test_claude_open_delegates_to_zellij(monkeypatch):
    captured: dict = {}

    def fake_open(**kw):
        captured.update(kw)
        return "c-tab"

    monkeypatch.setattr(engines.zellij, "open_or_switch", fake_open)
    tab = engines.ClaudeProvider().open_or_switch(
        "abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="t",
        allowed_cwds={"/tmp/x"},
        bypass=True,
    )
    assert tab == "c-tab"
    assert captured["uuid"] == "abcdef12-1234-1234-1234-1234567890ab"
    assert captured["cwd"] == "/tmp/x"
    assert captured["bypass"] is True


def test_claude_new_delegates_to_zellij(monkeypatch):
    captured: dict = {}

    def fake_new(**kw):
        captured.update(kw)
        return "new:demo"

    monkeypatch.setattr(engines.zellij, "new_session", fake_new)
    tab = engines.ClaudeProvider().new_session(
        cwd="/tmp/x", title="demo", allowed_cwds={"/tmp/x"}, bypass=False
    )
    assert tab == "new:demo"
    assert captured["bypass"] is False


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


def test_opencode_present_when_db_readable(opencode_db):
    assert "opencode" in {p.engine_id for p in engines.present_providers()}


def test_opencode_scan_top_level_only(opencode_db):
    ids = {r.uuid for r in engines.scan_all() if r.engine == "opencode"}
    assert ids == {_OC_TOP, _OC_ARCHIVED}  # fork (parent_id set) excluded
    assert _OC_FORK not in ids


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


def test_opencode_open_dispatch_argv(monkeypatch):
    captured: dict = {}

    def fake_open(**kw):
        captured.update(kw)
        return "o-tab"

    monkeypatch.setattr(engines.zellij, "open_engine", fake_open)
    tab = engines.OpenCodeProvider().open_or_switch(
        _OC_TOP, cwd="/tmp/other", title="t", allowed_cwds={"/tmp/other"}, bypass=True
    )
    assert tab == "o-tab"
    assert captured["engine_prefix"] == "o"
    assert captured["short"] == _OC_TOP
    assert captured["argv"][0] == engines.OPENCODE_BIN
    assert "--session" in captured["argv"] and _OC_TOP in captured["argv"]
