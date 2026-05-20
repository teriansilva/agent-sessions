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
        "ses_26484f850ffei7OMJwLTM9MLgn",  # opencode-shaped id with no provider yet
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
