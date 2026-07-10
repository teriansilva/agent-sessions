"""Engine-provider registry + engine-qualified identity.

These pin the seam #12 (opencode) builds on: a present-providers registry, a
merged scan, and ``parse_key`` as the single id-validation gate (with bare-UUID
back-compat for Claude). The Claude provider is a thin adapter over the existing
scanner/archive modules — those keep their own dedicated tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sessions import engines, scanner

_U1 = "11111111-1111-1111-1111-111111111111"
_SHELL_U = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
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
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / "nope.db"))
    monkeypatch.setattr(engines.shutil, "which", lambda _name: None)
    prov = engines.OpenCodeProvider()
    assert prov.is_present() is False
    assert prov.scan() == []


def test_opencode_binary_without_db_is_present_and_launchable(tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_DB", raising=False)
    oc = tmp_home / ".opencode" / "bin" / "opencode"
    oc.parent.mkdir(parents=True)
    oc.write_text("#!/bin/sh\n")
    oc.chmod(0o755)

    prov = engines.OpenCodeProvider()
    assert prov.is_present() is True
    assert prov.scan() == []
    assert prov.new_launch_argv("new-x", cwd="/tmp/proj", bypass=True) == [str(oc), "/tmp/proj"]


def test_opencode_fail_soft_corrupt_db(tmp_home, monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_OPENCODE_BIN", raising=False)
    bad = tmp_home / "corrupt.db"
    bad.write_text("this is not a sqlite database")
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(bad))
    monkeypatch.setattr(engines.shutil, "which", lambda _name: None)
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
    from agent_sessions.engines import opencode as opencode_mod

    monkeypatch.setattr(opencode_mod.discover, "resolve", lambda _name: None)
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


def test_opencode_advertises_new_session_via_reconcile(monkeypatch):
    # #127: opencode now supports new-session via launch-then-reconcile. supports_new is
    # True and new_launch_argv yields a bare `opencode <dir>` (NO --session: opencode
    # mints its own id, which the reconcile discovers).
    from agent_sessions.engines import opencode as opencode_mod

    monkeypatch.setattr(opencode_mod.discover, "resolve", lambda _name: None)
    p = engines.OpenCodeProvider()
    assert p.supports_new is True
    argv = p.new_launch_argv("new-x", cwd="/tmp/proj", bypass=True)
    assert argv == [engines.OPENCODE_BIN, "/tmp/proj"]
    assert "--session" not in argv  # never pins an id; opencode creates a fresh one


# ---- #611: logical_key — the inverse of physical_key --------------------------------------


def test_logical_key_maps_placeholder_to_the_engines_real_id():
    aliases = {
        "codex:new-d63b0fd4-6043-4f59-8b4b-54b904ce7414": (
            "codex:019f4a49-57a9-76c0-b3d4-6476f4aceef5"
        ),
        "opencode:new-aaaaaaaa-1111-1111-1111-111111111111": (
            "opencode:ses_15873112effeDtw4Xl4qIC4d"
        ),
        "antigravity:new-bbbbbbbb-1111-1111-1111-111111111111": (
            "antigravity:cccccccc-3333-3333-3333-333333333333"
        ),
    }
    for placeholder, real in aliases.items():
        assert engines.logical_key(placeholder, aliases) == real
        # ...and it is the exact inverse of physical_key.
        assert engines.physical_key(real, aliases) == placeholder


def test_logical_key_is_identity_for_pinned_engines_and_unknown_keys():
    aliases = {"codex:new-11111111-1111-1111-1111-111111111111": "codex:real"}
    for key in (
        "claude:11111111-1111-1111-1111-111111111111",
        "gemini:22222222-2222-2222-2222-222222222222",
        "codex:new-99999999-9999-9999-9999-999999999999",  # launched, not yet reconciled
        "codex:019f4a49-57a9-76c0-b3d4-6476f4aceef5",  # already the real id
    ):
        assert engines.logical_key(key, aliases) == key


def test_placeholder_keys_are_rejected_by_parse_key_but_resolve_through_logical_key():
    """The bug #611 fixes: `_plain_transcript` fed the physical key straight to `parse_key`,
    which rejects the placeholder shape for every mint-its-own-id engine — so the transcript
    silently read as empty for the whole life of an in-app-created session."""
    for engine, real in (
        ("codex", "019f4a49-57a9-76c0-b3d4-6476f4aceef5"),
        ("opencode", "ses_15873112effeDtw4Xl4qIC4d"),
        ("antigravity", "cccccccc-3333-3333-3333-333333333333"),
    ):
        placeholder = f"{engine}:new-11111111-1111-1111-1111-111111111111"
        with pytest.raises(engines.EngineError):
            engines.parse_key(placeholder)
        aliases = {placeholder: f"{engine}:{real}"}
        prov, native = engines.parse_key(engines.logical_key(placeholder, aliases))
        assert prov.engine_id == engine
        assert native == real


# ---- shell provider — "terminal as agent" (#636) ------------------------------------------


def _write_shell_record(tmp_home: Path, sid: str, cwd: str, created_at: float = 1_700_000_000.0):
    d = tmp_home / ".claude" / "shell-sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({"id": sid, "cwd": cwd, "created_at": created_at}))


def test_shell_present_when_bash_on_path(tmp_home, monkeypatch):
    monkeypatch.setattr(engines.shutil, "which", lambda n: "/usr/bin/bash" if n == "bash" else None)
    assert engines.ShellProvider().is_present() is True


def test_parse_key_shell():
    prov, native = engines.parse_key(f"shell:{_SHELL_U}")
    assert prov.engine_id == "shell" and native == _SHELL_U


def test_shell_scan_reads_records(tmp_home):
    _write_shell_record(tmp_home, _SHELL_U, "/home/user/proj")
    rows = engines.ShellProvider().scan()
    assert len(rows) == 1
    r = rows[0]
    assert r.engine == "shell" and r.uuid == _SHELL_U and r.cwd == "/home/user/proj"
    assert r.first_user_message == "" and r.archived is False
    assert abs(r.created_at - 1_700_000_000.0) < 1


def test_shell_scan_is_fail_soft_per_record(tmp_home):
    _write_shell_record(tmp_home, _SHELL_U, "/home/user/good")
    d = tmp_home / ".claude" / "shell-sessions"
    (d / "not-a-uuid.json").write_text("{ this is not json")  # junk file
    # a well-formed JSON record but with no usable cwd → skipped
    (d / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.json").write_text(json.dumps({"id": "nope"}))
    rows = engines.ShellProvider().scan()
    assert [r.uuid for r in rows] == [_SHELL_U]  # only the good record survives


def test_shell_scan_appears_in_scan_all(tmp_home, monkeypatch):
    # A shell record surfaces through the merged registry scan like any other engine's row.
    monkeypatch.setattr(engines.shutil, "which", lambda n: "/usr/bin/bash" if n == "bash" else None)
    _write_shell_record(tmp_home, _SHELL_U, "/home/user/proj")
    assert any(r.engine == "shell" and r.uuid == _SHELL_U for r in engines.scan_all())


def test_shell_on_new_session_writes_then_failure_removes(tmp_home):
    p = engines.ShellProvider()
    rec = tmp_home / ".claude" / "shell-sessions" / f"{_SHELL_U}.json"
    p.on_new_session(_SHELL_U, cwd="/home/user/proj")
    assert rec.is_file()
    assert json.loads(rec.read_text())["cwd"] == "/home/user/proj"
    p.on_new_session_failed(_SHELL_U)  # launch was rejected → no phantom row
    assert not rec.exists()


def test_shell_record_path_rejects_non_uuid(tmp_home):
    # Defense in depth: the store never builds a path from anything but our own UUID shape.
    p = engines.ShellProvider()
    assert p._record_path("../../etc/passwd") is None
    p.on_new_session("../../etc/passwd", cwd="/x")  # must be a no-op, not a traversal write
    assert not (tmp_home / ".claude" / "shell-sessions").exists()


def test_shell_launch_argv_is_a_literal_login_shell(monkeypatch):
    # The shell-free launcher contract: a literal argv (bash binary + login flag), NEVER a
    # command string handed to an interpreter (no "-c").
    monkeypatch.setattr(engines.base, "BASH_BIN", "/usr/bin/bash")
    argv = engines.ShellProvider().launch_argv(_SHELL_U, cwd="/tmp/x", bypass=True)
    assert argv == ["/usr/bin/bash", "-l"]
    assert "-c" not in argv


def test_shell_new_launch_argv_is_pinned_and_matches_resume(monkeypatch):
    monkeypatch.setattr(engines.base, "BASH_BIN", "/usr/bin/bash")
    p = engines.ShellProvider()
    assert p.supports_new is True
    assert getattr(p, "new_session_reconciles", False) is False  # pinned id, no reconcile
    new = p.new_launch_argv(_SHELL_U, cwd="/tmp/x", bypass=False)
    assert new == p.launch_argv(_SHELL_U, cwd="/tmp/x", bypass=True) == ["/usr/bin/bash", "-l"]


def test_shell_placeholder_is_rejected_by_parse_key(tmp_home):
    # shell is a pinned-id engine (not a reconcile engine), so the new-<uuid> placeholder shape is
    # NOT accepted for it — only a real UUID key.
    placeholder = f"shell:new-{_U1}"
    assert engines.is_new_session_placeholder(placeholder) is False
    with pytest.raises(engines.EngineError):
        engines.parse_key(placeholder, allow_new_placeholder=True)


def test_shell_archive_via_sidecar_keeps_record_and_override_wins(tmp_home, monkeypatch):
    # Archive rides the engine-agnostic sidecar (like gemini/codex/opencode); the record row
    # intentionally REMAINS, and the row builder's `m.archived if not None else s.archived`
    # override is what flips the effective state. Prove both halves here.
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_home / "metadata.json"))
    _write_shell_record(tmp_home, _SHELL_U, "/home/user/proj")
    p = engines.ShellProvider()
    p.archive(_SHELL_U)
    assert engines._metadata.get(f"shell:{_SHELL_U}").archived is True
    # record still present, and the scan row's NATIVE archived stays False → the sidecar override
    # is the thing that archives the row.
    assert (tmp_home / ".claude" / "shell-sessions" / f"{_SHELL_U}.json").is_file()
    assert p.scan()[0].archived is False
    p.unarchive(_SHELL_U)
    assert engines._metadata.get(f"shell:{_SHELL_U}").archived is False
