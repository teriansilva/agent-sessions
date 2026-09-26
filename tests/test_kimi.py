"""KimiProvider discovery + launch argv + reconcile + engine-qualified id routing (#714).

The synthetic store reproduces Kimi Code 0.27.0's real on-disk layout, captured from a live
``~/.kimi-code``: a top-level ``session_index.jsonl`` whose rows carry exactly
``{sessionId, sessionDir, workDir}``, plus nested session dirs at
``sessions/wd_<slug>_<hash>/session_<uuid>/state.json``.

Kimi's native id is ``session_<uuid>`` — NOT a bare UUID — so the id-pattern gate gets its own
test: reusing the Claude UUID regex would make ``parse_key`` reject every real session.

Kimi 0.43.1 (which self-updates in place) switched ``state.json`` to a v2 schema (#1030):
``cwd`` instead of ``workDir`` and finite epoch-millisecond timestamps instead of ISO strings.
Both schemas coexist in a live store, so the v1/v2 fixtures here are written side by side on
purpose — the mixed-store tests are the regression gate for the sidebar-vanishing bug.
"""

from __future__ import annotations

import json
import math

import pytest

from agent_sessions import engines, metadata

_SID = "session_25f66293-9603-46af-bbf3-bd79ef84ca54"
_SID2 = "session_aaaabbbb-cccc-dddd-eeee-ffff00001111"
_SID3 = "session_b5470439-98ff-4212-a57e-d730681e1ad9"

# Epoch ms constants mirroring the v1 fixture's ISO stamps so v1 and v2 rows assert the SAME
# converted seconds — one number, two encodings.
_CREATED_MS = 1784470743061  # == 2026-07-19T14:19:03.061Z
_UPDATED_MS = 1784474404500  # == 2026-07-19T15:20:04.500Z


def _write_session(
    root,
    sid,
    cwd,
    *,
    title="New Session",
    bucket="wd_proj_deadbeef",
    state=True,
    v2=False,
    extra_state=None,
):
    """One session dir under the per-workdir bucket. ``state=False`` omits ``state.json`` so the
    'session dir exists but is unreadable' path can be exercised. ``v2=True`` writes the kimi
    0.43.1 schema (#1030): ``cwd`` + epoch-ms timestamps; ``extra_state`` overrides/extends the
    payload for precedence and malformed-value cases."""
    sdir = root / "sessions" / bucket / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "agents" / "main").mkdir(parents=True, exist_ok=True)
    if state:
        if v2:
            payload = {
                "id": sid,
                "version": 2,
                "cwd": cwd,
                "createdAt": _CREATED_MS,
                "updatedAt": _UPDATED_MS,
                "title": title,
                "isCustomTitle": title != "New Session",
                "archived": False,
                "agents": {"main": {"homedir": str(sdir / "agents" / "main")}},
                "custom": {},
            }
        else:
            payload = {
                "createdAt": "2026-07-19T14:19:03.061Z",
                "updatedAt": "2026-07-19T15:20:04.500Z",
                "title": title,
                "isCustomTitle": title != "New Session",
                "agents": {"main": {"homedir": str(sdir / "agents" / "main")}},
                "custom": {},
                "workDir": cwd,
            }
        if extra_state:
            payload.update(extra_state)
        (sdir / "state.json").write_text(json.dumps(payload), encoding="utf-8")
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


# --- v2 state.json (#1030: kimi 0.43.1 renamed workDir→cwd, ISO→epoch-ms) ----------------------


def test_scan_reads_v2_state_json(kimi_home):
    """A 0.43.1-style session (``cwd`` + epoch-ms timestamps, no ``workDir``) lists with its real
    cwd, title and Kimi's own timestamps — ms converted to seconds, not mtime fallbacks."""
    _write_session(kimi_home, _SID3, "/home/u/proj", title="DeepSeek V4.1 Flash numbers", v2=True)
    (row,) = _provider().scan()
    assert row.engine == "kimi"
    assert row.uuid == _SID3
    assert row.cwd == "/home/u/proj"
    assert row.first_user_message == "DeepSeek V4.1 Flash numbers"
    # Values asserted, not just presence: v2 ms → seconds matches the v1 fixture's seconds.
    assert row.created_at == pytest.approx(1784470743.061, abs=0.001)
    assert row.last_mtime == pytest.approx(1784474404.5, abs=0.001)


def test_scan_lists_mixed_v1_and_v2_store(kimi_home):
    """The live-store condition after the 0.43.1 self-update: old sessions in the v1 schema, new
    ones in v2, same bucket. Both must list — a v2-only reader dropping v1 rows (or the reverse,
    the original bug) fails here."""
    _write_session(kimi_home, _SID, "/home/u/proj")  # v1
    _write_session(kimi_home, _SID3, "/home/u/proj", v2=True)  # v2, same bucket
    rows = {r.uuid: r for r in _provider().scan()}
    assert sorted(rows) == sorted([_SID, _SID3])
    assert rows[_SID].cwd == "/home/u/proj"
    assert rows[_SID3].cwd == "/home/u/proj"
    assert rows[_SID].created_at == pytest.approx(1784470743.061, abs=1)
    assert rows[_SID3].created_at == pytest.approx(1784470743.061, abs=0.001)


def test_v2_cwd_takes_precedence_over_work_dir(kimi_home):
    """Precedence is defined and pinned: when a transitional writer carries both fields, the v2
    ``cwd`` (the field the current writer maintains) wins."""
    _write_session(
        kimi_home,
        _SID3,
        "/home/u/proj",
        v2=True,
        extra_state={"workDir": "/home/u/stale"},
    )
    (row,) = _provider().scan()
    assert row.cwd == "/home/u/proj"


def test_v2_empty_cwd_falls_back_to_work_dir(kimi_home):
    """An empty/absent ``cwd`` degrades to v1's ``workDir`` — an old field beats no field."""
    _write_session(
        kimi_home, _SID3, "", v2=True, extra_state={"cwd": "", "workDir": "/home/u/proj"}
    )
    (row,) = _provider().scan()
    assert row.cwd == "/home/u/proj"


def test_v2_no_usable_working_dir_yields_no_row(kimi_home):
    """Neither field usable → no row (the can't-place rule, same as v1)."""
    _write_session(kimi_home, _SID3, "", v2=True, extra_state={"cwd": "", "workDir": ""})
    assert _provider().scan() == []
    _write_session(kimi_home, _SID, "/home/u/other", v2=True, extra_state={"cwd": 42})
    assert _provider().scan() == []


def test_lookup_finds_v2_session(kimi_home):
    """The #1030 404 repro as a unit test: the single-session lookup (what the open tab polls)
    must resolve a v2 session, not just the list scan."""
    _write_session(kimi_home, _SID3, "/home/u/proj", v2=True)
    row = _provider().lookup(_SID3)
    assert row is not None
    assert row.uuid == _SID3
    assert row.cwd == "/home/u/proj"
    assert row.created_at == pytest.approx(1784470743.061, abs=0.001)


def test_v2_boolean_timestamps_fall_back_to_filesystem(kimi_home):
    """``bool`` subclasses ``int`` — a ``true`` timestamp must be treated as malformed (→ mtime /
    birthtime fallback), never converted to 0.001s."""
    from agent_sessions.scanner import fs_created_at

    sdir = _write_session(
        kimi_home,
        _SID3,
        "/home/u/proj",
        v2=True,
        extra_state={"createdAt": True, "updatedAt": True},
    )
    st = (sdir / "state.json").stat()
    (row,) = _provider().scan()
    assert row.created_at == pytest.approx(fs_created_at(st), abs=1)
    assert row.last_mtime == pytest.approx(st.st_mtime, abs=1)


def test_v2_non_finite_and_non_positive_timestamps_fall_back_to_filesystem(kimi_home):
    """NaN/Infinity (json round-trips them) and ≤0 numbers are malformed → filesystem fallback."""
    from agent_sessions.scanner import fs_created_at

    sdir = _write_session(
        kimi_home,
        _SID3,
        "/home/u/proj",
        v2=True,
        extra_state={"createdAt": 0, "updatedAt": float("nan")},
    )
    st = (sdir / "state.json").stat()
    (row,) = _provider().scan()
    assert row.created_at == pytest.approx(fs_created_at(st), abs=1)
    assert row.last_mtime == pytest.approx(st.st_mtime, abs=1)
    # Negative is also rejected outright, not divided into a bogus small epoch.
    sdir2 = _write_session(
        kimi_home,
        _SID,
        "/home/u/other",
        bucket="wd_other_cafe1234",
        v2=True,
        extra_state={"createdAt": -5, "updatedAt": float("inf")},
    )
    st2 = (sdir2 / "state.json").stat()
    rows = {r.uuid: r for r in _provider().scan()}
    assert rows[_SID].created_at == pytest.approx(fs_created_at(st2), abs=1)
    assert rows[_SID].last_mtime == pytest.approx(st2.st_mtime, abs=1)


def test_state_ts_to_epoch_shapes():
    """Direct seam contract: v1 ISO strings, v2 finite ms → seconds, booleans and junk → 0.0."""
    from agent_sessions.engines.kimi import _state_ts_to_epoch

    assert _state_ts_to_epoch("2026-07-19T14:19:03.061Z") == pytest.approx(
        1784470743.061, abs=0.001
    )
    assert _state_ts_to_epoch(_CREATED_MS) == pytest.approx(1784470743.061, abs=0.001)
    assert _state_ts_to_epoch(_UPDATED_MS) == pytest.approx(1784474404.5, abs=0.001)
    assert _state_ts_to_epoch(True) == 0.0
    assert _state_ts_to_epoch(False) == 0.0
    assert _state_ts_to_epoch(0) == 0.0
    assert _state_ts_to_epoch(-1) == 0.0
    assert _state_ts_to_epoch(float("nan")) == 0.0
    assert _state_ts_to_epoch(float("inf")) == 0.0
    assert _state_ts_to_epoch(None) == 0.0
    assert _state_ts_to_epoch("") == 0.0
    assert _state_ts_to_epoch("not-a-timestamp") == 0.0
    assert not math.isinf(_state_ts_to_epoch(_UPDATED_MS))


def test_reconcile_adopts_v2_session_with_index(kimi_home):
    """A v2 session present in the index (0.43.1 keeps ``workDir`` there) is adopted by the
    reconcile diff."""
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    sdir = _write_session(kimi_home, _SID3, "/home/u/proj", v2=True)
    _write_index(
        kimi_home, [{"sessionId": _SID3, "sessionDir": str(sdir), "workDir": "/home/u/proj"}]
    )
    assert prov.reconcile_new_session("/home/u/proj", snap) == _SID3


def test_reconcile_adopts_v2_session_walk_only(kimi_home):
    """Index-missing (or the row lost): the walk + v2-tolerant ``_meta`` still adopt the session
    — the reconcile never stalls on the v2 schema."""
    prov = _provider()
    snap = prov.snapshot_session_ids("/home/u/proj")
    _write_session(kimi_home, _SID3, "/home/u/proj", v2=True)
    assert prov.reconcile_new_session("/home/u/proj", snap) == _SID3


# --- launch argv ------------------------------------------------------------------------------


# #853 P2: argv is the manifest-built provider's (`engines.get("kimi")`); argv[0] is the
# provenance-checked file AGENT_SESSIONS_KIMI_BIN names, never `base.KIMI_BIN`.


def test_launch_argv_resumes_by_id(kimi_home, engine_bin):
    b = engine_bin("kimi")
    assert engines.get("kimi").launch_argv(_SID, cwd="/home/u/proj", bypass=False) == [
        b,
        "-S",
        _SID,
    ]


def test_launch_argv_bypass_adds_yolo(kimi_home, engine_bin):
    engine_bin("kimi")
    assert engines.get("kimi").launch_argv(_SID, cwd="/home/u/proj", bypass=True)[-1] == "-y"


def test_new_launch_argv_does_not_pin_an_id(kimi_home, engine_bin):
    """Kimi has no ``--session-id``; the placeholder must never leak into argv."""
    b = engine_bin("kimi")
    placeholder = "new-12345678-1234-1234-1234-123456789abc"
    argv = engines.get("kimi").new_launch_argv(placeholder, cwd="/home/u/proj", bypass=False)
    assert argv == [b]
    assert placeholder not in argv


def test_launch_argv_is_a_literal_list_no_shell(kimi_home, engine_bin):
    """Shell-free guarantee: argv is a literal list, never a command string."""
    engine_bin("kimi")
    argv = engines.get("kimi").launch_argv(_SID, cwd="/home/u/proj", bypass=True)
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
    """#454 guard, made structural by #853 P4: the frontend keeps NO reconcile set of its own — it
    asks the roster (`session_id.mint`, served from each manifest) — so a client list can no longer
    drift from the server's `new_session_reconciles`. Pin both halves: `newSession.ts` has no
    engine set and reads `mintsOwnId`, and the roster the web tests render with agrees with the
    server for every engine."""
    import json
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "web" / "src"
    src = (web / "lib" / "newSession.ts").read_text(encoding="utf-8")
    assert "RECONCILE_ENGINES" not in src and "new Set(" not in src
    assert "mintsOwnId" in src
    fixture = json.loads((web / "test" / "roster.fixture.json").read_text(encoding="utf-8"))
    web_adopt = {e["id"] for e in fixture["engines"] if e["session_id"]["mint"] == "adopt"}
    backend = {
        p.engine_id for p in engines.all_providers() if getattr(p, "new_session_reconciles", False)
    }
    assert web_adopt == backend


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
