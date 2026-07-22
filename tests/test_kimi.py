"""KimiProvider discovery + launch argv + reconcile + engine-qualified id routing (#714).

The synthetic store reproduces Kimi Code 0.27.0's real on-disk layout, captured from a live
``~/.kimi-code``: a top-level ``session_index.jsonl`` whose rows carry exactly
``{sessionId, sessionDir, workDir}``, plus nested session dirs at
``sessions/wd_<slug>_<hash>/session_<uuid>/state.json``.

Kimi's native id is ``session_<uuid>`` — NOT a bare UUID — so the id-pattern gate gets its own
test: reusing the Claude UUID regex would make ``parse_key`` reject every real session.
"""

from __future__ import annotations

import json

import pytest

from agent_sessions import engines, metadata

_SID = "session_25f66293-9603-46af-bbf3-bd79ef84ca54"
_SID2 = "session_aaaabbbb-cccc-dddd-eeee-ffff00001111"


def _write_session(root, sid, cwd, *, title="New Session", bucket="wd_proj_deadbeef", state=True):
    """One session dir under the per-workdir bucket. ``state=False`` omits ``state.json`` so the
    'session dir exists but is unreadable' path can be exercised."""
    sdir = root / "sessions" / bucket / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "agents" / "main").mkdir(parents=True, exist_ok=True)
    if state:
        (sdir / "state.json").write_text(
            json.dumps(
                {
                    "createdAt": "2026-07-19T14:19:03.061Z",
                    "updatedAt": "2026-07-19T15:20:04.500Z",
                    "title": title,
                    "isCustomTitle": title != "New Session",
                    "agents": {"main": {"homedir": str(sdir / "agents" / "main")}},
                    "custom": {},
                    "workDir": cwd,
                }
            ),
            encoding="utf-8",
        )
    return sdir


def _write_index(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    (root / "session_index.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    """A synthetic ``~/.kimi-code`` rooted in tmp — never the operator's real store."""
    root = tmp_path / ".kimi-code"
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(root))
    monkeypatch.setattr(metadata, "_PATH", tmp_path / "metadata.json", raising=False)
    return root


def _provider():
    return engines.KimiProvider()


# --- id pattern -------------------------------------------------------------------------------


def test_id_pattern_accepts_prefixed_and_rejects_bare_uuid():
    """Kimi ids are ``session_<uuid>``. A bare UUID must NOT validate — that mistake would make
    every real session fail ``parse_key`` and 4404 on attach."""
    pat = _provider().id_pattern
    assert pat.match(_SID)
    assert not pat.match("25f66293-9603-46af-bbf3-bd79ef84ca54")
    assert not pat.match("session_not-a-uuid")
    assert not pat.match(f"{_SID}/../escape")


def test_parse_key_routes_engine_qualified_id(kimi_home):
    _write_session(kimi_home, _SID, "/home/u/proj")
    prov, native = engines.parse_key(f"kimi:{_SID}")
    assert prov.engine_id == "kimi"
    assert native == _SID


# --- scan -------------------------------------------------------------------------------------


def test_scan_reads_index_fast_path(kimi_home):
    sdir = _write_session(kimi_home, _SID, "/home/u/proj", title="Refactor the parser")
    _write_index(
        kimi_home, [{"sessionId": _SID, "sessionDir": str(sdir), "workDir": "/home/u/proj"}]
    )
    (row,) = _provider().scan()
    assert row.engine == "kimi"
    assert row.uuid == _SID
    assert row.cwd == "/home/u/proj"
    assert row.first_user_message == "Refactor the parser"
    # Timestamps come from Kimi's own ISO fields, not file mtimes.
    assert row.created_at == pytest.approx(1784470743.061, abs=1)
    assert row.last_mtime == pytest.approx(1784474404.5, abs=1)


def test_scan_falls_back_to_dir_walk_without_index(kimi_home):
    """No index at all (fresh install, or the operator deleted it) still lists sessions."""
    _write_session(kimi_home, _SID, "/home/u/proj")
    assert [r.uuid for r in _provider().scan()] == [_SID]


def test_scan_survives_corrupt_index_and_still_finds_sessions(kimi_home):
    """A truncated/garbage index must degrade to the walk, not empty the sidebar."""
    _write_session(kimi_home, _SID, "/home/u/proj")
    (kimi_home / "session_index.jsonl").write_text('{"sessionId": "trunc', encoding="utf-8")
    assert [r.uuid for r in _provider().scan()] == [_SID]


def test_scan_unions_index_and_walk_without_duplicates(kimi_home):
    """An index row and the dir walk describing the SAME session yield one row, and a session the
    index forgot is still discovered."""
    sdir = _write_session(kimi_home, _SID, "/home/u/proj")
    _write_session(kimi_home, _SID2, "/home/u/other", bucket="wd_other_cafe1234")
    _write_index(
        kimi_home, [{"sessionId": _SID, "sessionDir": str(sdir), "workDir": "/home/u/proj"}]
    )
    assert sorted(r.uuid for r in _provider().scan()) == sorted([_SID, _SID2])


def test_scan_skips_session_with_no_resolvable_cwd(kimi_home):
    """cwd is the launch dir AND the open-path allowlist key, so a session we can't place yields
    no row rather than a bogus empty-cwd one."""
    _write_session(kimi_home, _SID, "/home/u/proj", state=False)  # no state.json → no workDir
    assert _provider().scan() == []


def test_scan_blanks_kimis_placeholder_title(kimi_home):
    """Kimi seeds every session with "New Session"; surfacing it verbatim would fill the sidebar
    with identical rows, so it is treated as 'no title'."""
    _write_session(kimi_home, _SID, "/home/u/proj", title="New Session")
    (row,) = _provider().scan()
    assert row.first_user_message == ""


def test_scan_missing_store_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(tmp_path / "nope"))
    assert _provider().scan() == []


# --- launch argv ------------------------------------------------------------------------------


def test_launch_argv_resumes_by_id(kimi_home, monkeypatch):
    monkeypatch.setattr(engines.base, "KIMI_BIN", "/opt/kimi/bin/kimi")
    assert _provider().launch_argv(_SID, cwd="/home/u/proj", bypass=False) == [
        "/opt/kimi/bin/kimi",
        "-S",
        _SID,
    ]


def test_launch_argv_bypass_adds_yolo(kimi_home, monkeypatch):
    monkeypatch.setattr(engines.base, "KIMI_BIN", "/opt/kimi/bin/kimi")
    assert _provider().launch_argv(_SID, cwd="/home/u/proj", bypass=True)[-1] == "-y"


def test_new_launch_argv_does_not_pin_an_id(kimi_home, monkeypatch):
    """Kimi has no ``--session-id``; the placeholder must never leak into argv."""
    monkeypatch.setattr(engines.base, "KIMI_BIN", "/opt/kimi/bin/kimi")
    argv = _provider().new_launch_argv("new-1234", cwd="/home/u/proj", bypass=False)
    assert argv == ["/opt/kimi/bin/kimi"]
    assert "new-1234" not in argv


def test_launch_argv_is_a_literal_list_no_shell(kimi_home):
    """Shell-free guarantee: argv is a literal list, never a command string."""
    argv = _provider().launch_argv(_SID, cwd="/home/u/proj", bypass=True)
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)
    assert not any(tok in " ".join(argv) for tok in ("&&", "|", ";", "$("))


# --- new-session reconciliation ---------------------------------------------------------------


def test_snapshot_is_cwd_scoped(kimi_home):
    _write_session(kimi_home, _SID, "/home/u/proj")
    _write_session(kimi_home, _SID2, "/home/u/other", bucket="wd_other_cafe1234")
    assert _provider().snapshot_session_ids("/home/u/proj") == {_SID}


def test_snapshot_missing_store_is_empty_baseline(tmp_path, monkeypatch):
    """A fresh Kimi with no store is a legitimate empty baseline (set()), NOT a read failure."""
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(tmp_path / "nope"))
    assert _provider().snapshot_session_ids("/home/u/proj") == set()


def test_reconcile_returns_the_single_new_id(kimi_home):
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    _write_session(kimi_home, _SID, "/home/u/proj")
    assert prov.reconcile_new_session("/home/u/proj", snap) == _SID


def test_reconcile_returns_none_before_kimi_writes(kimi_home):
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    assert prov.reconcile_new_session("/home/u/proj", snap) is None


def test_reconcile_ambiguous_returns_list_and_never_guesses(kimi_home):
    """Two new same-cwd sessions inside the poll window → the caller must fail safe."""
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    _write_session(kimi_home, _SID, "/home/u/proj")
    _write_session(kimi_home, _SID2, "/home/u/proj", bucket="wd_proj_deadbeef")
    got = prov.reconcile_new_session("/home/u/proj", snap)
    assert isinstance(got, list) and sorted(got) == sorted([_SID, _SID2])


def test_reconcile_ignores_new_session_in_another_cwd(kimi_home):
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    _write_session(kimi_home, _SID2, "/home/u/other", bucket="wd_other_cafe1234")
    assert prov.reconcile_new_session("/home/u/proj", snap) is None


def test_reconcile_engines_lockstep_with_frontend():
    """#454 guard: a provider with ``new_session_reconciles`` missing from the frontend's
    ``RECONCILE_ENGINES`` mints a bare UUID and the launch 4404s. Assert the backend flag matches
    the checked-in frontend set."""
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "web"
        / "src"
        / "lib"
        / "newSession.ts"
    ).read_text(encoding="utf-8")
    backend = {
        p.engine_id for p in engines.all_providers() if getattr(p, "new_session_reconciles", False)
    }
    for engine in backend:
        assert (
            f'"{engine}"' in src.split("RECONCILE_ENGINES")[1].split(")")[0]
        ), f"{engine} reconciles server-side but is missing from RECONCILE_ENGINES"


# --- archive ----------------------------------------------------------------------------------


def test_archive_uses_sidecar_and_never_writes_kimis_store(kimi_home):
    """Read-only guarantee: archiving must not touch anything under the Kimi store."""
    _write_session(kimi_home, _SID, "/home/u/proj")
    before = {p: p.stat().st_mtime_ns for p in kimi_home.rglob("*") if p.is_file()}
    prov = _provider()
    prov.archive(_SID)
    assert metadata.get(f"kimi:{_SID}").archived is True
    prov.unarchive(_SID)
    assert metadata.get(f"kimi:{_SID}").archived is False
    after = {p: p.stat().st_mtime_ns for p in kimi_home.rglob("*") if p.is_file()}
    assert before == after
