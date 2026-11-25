"""Tests for the paged transcript-history endpoint (#348 Phase 3):
``GET /api/sessions/{sid}/history``.

Covers the issue's contract: cursor stability across width changes, page budgets
(lines + bytes, env-overridable), the no-adapter empty shape, single-inflight (429),
auth, and that paging always terminates at the oldest turn (``has_more`` false).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_sessions import history, transcript
from agent_sessions.main import create_app
from agent_sessions.routes import history as history_routes

SID = "12121212-3434-5656-7878-909090909090"


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


def _write_jsonl(home, sid=SID, n_messages=40):
    """A Claude session JSONL with ``n_messages`` alternating user/assistant turns, each
    carrying a unique marker (``M000`` …) so page boundaries are assertable."""
    proj = home / ".claude" / "projects" / "-home-user-claude-proj"
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        rec = {"type": role, "message": {"role": role, "content": f"M{i:03d} marker message"}}
        lines.append(json.dumps(rec))
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def _markers(ansi: str) -> list[str]:
    return [f"M{i:03d}" for i in range(1000) if f"M{i:03d}" in ansi]


# ---- auth ---------------------------------------------------------------------


def test_history_requires_auth(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history")
    assert r.status_code == 401


def test_unknown_engine_404s(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions/nosuch:abc/history")
    assert r.status_code == 404


# ---- shape --------------------------------------------------------------------


def test_no_adapter_engine_returns_empty_shape(auth_cfg, tmp_home, monkeypatch):
    """An engine without a transcript adapter answers 200 + the empty end-of-history
    shape — never an error (the client shows the start-of-history pill)."""
    monkeypatch.setattr(transcript, "adapter_for", lambda _eng: None)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history")
    assert r.status_code == 200
    assert r.json() == {"ansi": "", "cursor": None, "has_more": False}


def test_missing_transcript_is_empty_not_error(auth_cfg, tmp_home):
    """Adapter present but no transcript on disk → same clean empty shape (fail-soft)."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history")
    assert r.status_code == 200
    assert r.json() == {"ansi": "", "cursor": None, "has_more": False}


# ---- reconciled (alias-backed) sessions -----------------------------------------


def _opencode_db(tmp_home, session_id, texts):
    import sqlite3

    db = tmp_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, data TEXT)")
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        mid = f"msg_{i:03d}"
        conn.execute(
            "INSERT INTO message VALUES (?,?,?)", (mid, session_id, json.dumps({"role": role}))
        )
        conn.execute(
            "INSERT INTO part VALUES (?,?,?)",
            (f"{mid}_p0", mid, json.dumps({"type": "text", "text": text})),
        )
    conn.commit()
    conn.close()


def test_alias_backed_opencode_session_serves_real_transcript(auth_cfg, tmp_home, monkeypatch):
    """Hermes #365 finding 2: after opencode new-session reconcile (#127) an alias maps
    placeholder→real for LIVE resources. The transcript adapter keys off the REAL native
    id (``message.session_id``), so the history route must NOT map the requested real id
    back to the placeholder — that returned empty history for a session whose transcript
    exists."""
    from agent_sessions import metadata

    real = "ses_realreal0000000000000000"
    placeholder = "new-11111111-1111-1111-1111-111111111111"
    metadata.set_alias(f"opencode:{placeholder}", f"opencode:{real}")
    _opencode_db(tmp_home, real, [f"OC{i:03d} opencode marker" for i in range(12)])
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/opencode:{real}/history?cols=80&before=12")
    assert r.status_code == 200
    page = r.json()
    assert "OC000" in page["ansi"]  # the REAL transcript, not the empty placeholder one


def test_alias_backed_codex_session_serves_real_transcript(auth_cfg, tmp_home, monkeypatch):
    """Same as above for codex (it reconciles via the placeholder alias too): the rollout
    glob keys off the REAL uuid, so the route must query it as requested."""
    from agent_sessions import metadata

    real = "9f8e7d6c-5b4a-4321-aedc-ba0987654321"
    placeholder = "new-22222222-2222-2222-2222-222222222222"
    metadata.set_alias(f"codex:{placeholder}", f"codex:{real}")
    d = tmp_home / ".codex" / "sessions" / "2026"
    d.mkdir(parents=True, exist_ok=True)
    recs = [
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": [{"type": "text", "text": f"CX{i:03d} codex marker"}],
                },
            }
        )
        for i in range(12)
    ]
    (d / f"rollout-2026-06-10-{real}.jsonl").write_text("\n".join(recs) + "\n")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/codex:{real}/history?cols=80&before=12")
    assert r.status_code == 200
    assert "CX000" in r.json()["ansi"]


# ---- paging + cursor ----------------------------------------------------------


def test_paging_walks_to_oldest_and_terminates(auth_cfg, tmp_home, monkeypatch):
    """Follow cursors from the first (no-``before`` fallback) page to the oldest turn:
    every page advances by the fixed turn step, the union covers everything older than
    the newest page window in order, and the final page reports has_more=false with a
    null cursor."""
    _write_jsonl(tmp_home, n_messages=40)
    # Small turn step so the walk takes many pages.
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "4")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    seen: list[str] = []
    before: int | None = None
    for _ in range(100):  # hard stop — paging must terminate well before this
        q = "?cols=80" + (f"&before={before}" if before is not None else "")
        r = c.get(f"/api/sessions/claude:{SID}/history{q}")
        assert r.status_code == 200
        page = r.json()
        seen = _markers(page["ansi"]) + seen
        if not page["has_more"]:
            assert page["cursor"] is None
            break
        assert page["cursor"] is not None and page["cursor"] >= 0
        new_before = page["cursor"]
        assert before is None or new_before < before  # strictly older — guarantees progress
        before = new_before
    else:
        raise AssertionError("paging did not terminate")
    # Oldest-first, contiguous from M000, no duplicates.
    assert seen[0] == "M000"
    assert seen == sorted(set(seen))


def test_cursor_is_width_stable(auth_cfg, tmp_home, monkeypatch):
    """The same ``before`` cursor returns the same TURNS at any width — a cursor is a
    turn index, never a rendered-line offset (the issue's hard requirement)."""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "6")
    _write_jsonl(tmp_home, n_messages=30)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    narrow = c.get(f"/api/sessions/claude:{SID}/history?before=20&cols=40").json()
    wide = c.get(f"/api/sessions/claude:{SID}/history?before=20&cols=300").json()
    # Rendering differs (wrapping), but the turn window and the next cursor must not.
    window = [f"M{i:03d}" for i in range(14, 20)]
    assert _markers(narrow["ansi"]) == _markers(wide["ansi"]) == window
    assert narrow["cursor"] == wide["cursor"] == 14
    assert narrow["has_more"] == wide["has_more"] is True


def test_cursor_width_stable_with_long_wrapping_turns(auth_cfg, tmp_home, monkeypatch):
    """Hermes #365 finding 1 repro, locked as a regression: with LONG WRAPPING turns the
    rendered line count per turn differs wildly between cols=40 and cols=300. Turn
    selection must not care — the same ``before`` yields the SAME turn coverage and the
    SAME next cursor at both widths, page after page, all the way to the oldest turn.
    (The pre-fix budget-walk selected only M009 at cols=40 but M006..M009 at cols=300
    for the same cursor, so a resize could skip/duplicate pages.)"""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "4")
    proj = tmp_home / ".claude" / "projects" / "-home-user-claude-proj"
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        body = f"M{i:03d} " + ("wrap " * 60)  # ~305 chars → ~8 lines at 40 cols, 2 at 300
        lines.append(json.dumps({"type": role, "message": {"role": role, "content": body}}))
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    c = _client(auth_cfg)
    _login(c, auth_cfg)

    def walk(cols: int) -> list[tuple[list[str], int | None, bool]]:
        pages = []
        before: int | None = 10  # Hermes's exact starting cursor
        for _ in range(50):
            r = c.get(f"/api/sessions/claude:{SID}/history?before={before}&cols={cols}")
            assert r.status_code == 200
            page = r.json()
            pages.append((_markers(page["ansi"]), page["cursor"], page["has_more"]))
            if not page["has_more"]:
                return pages
            before = page["cursor"]
        raise AssertionError("paging did not terminate")

    narrow, wide = walk(40), walk(300)
    # Identical turn coverage AND identical cursors on every page, despite the wrapping.
    assert narrow == wide
    # And the windows are exactly the fixed turn step: M006..M009, M002..M005, M000..M001.
    assert [p[0] for p in narrow] == [
        [f"M{i:03d}" for i in range(6, 10)],
        [f"M{i:03d}" for i in range(2, 6)],
        [f"M{i:03d}" for i in range(0, 2)],
    ]
    assert [p[1] for p in narrow] == [6, 2, None]


def test_no_before_fallback_serves_older_than_newest_window(auth_cfg, tmp_home, monkeypatch):
    """No ``before`` (the client never received a ``{"t":"hist"}`` frame) → the
    width-independent fallback: everything older than the newest page-sized turn window.
    Approximate by design (a transcript attach always seeds the exact cursor instead)."""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "5")
    _write_jsonl(tmp_home, n_messages=40)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?lines=500&cols=80")
    page = r.json()
    # Fallback boundary = len(turns) - page_turns() = 35; the page is turns[30:35].
    assert _markers(page["ansi"]) == [f"M{i:03d}" for i in range(30, 35)]
    assert page["cursor"] == 30 and page["has_more"] is True


def test_no_before_fallback_is_width_independent_with_wrapped_turns(
    auth_cfg, tmp_home, monkeypatch
):
    """Hermes #365 r2 finding 1, locked as a regression: ``before=None`` must yield the
    SAME boundary at every width, even with long wrapping turns. (The pre-fix
    ``initial_cursor`` walked rendered line counts at the REQUEST width — a probe with
    long turns produced boundary 3948 at cols=40 but 0 at cols=300 — so a resize between
    attach and the first lazy-load made the turns between the two boundaries
    unreachable.)"""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "4")
    proj = tmp_home / ".claude" / "projects" / "-home-user-claude-proj"
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        body = f"M{i:03d} " + ("wrap " * 60)  # ~305 chars → ~8 lines at 40 cols, 2 at 300
        lines.append(json.dumps({"type": role, "message": {"role": role, "content": body}}))
    (proj / f"{SID}.jsonl").write_text("\n".join(lines) + "\n")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    narrow = c.get(f"/api/sessions/claude:{SID}/history?cols=40").json()
    wide = c.get(f"/api/sessions/claude:{SID}/history?cols=300").json()
    # Identical boundary at both widths: turns[22:26] with next cursor 22.
    window = [f"M{i:03d}" for i in range(22, 26)]
    assert _markers(narrow["ansi"]) == _markers(wide["ansi"]) == window
    assert narrow["cursor"] == wide["cursor"] == 22
    assert narrow["has_more"] == wide["has_more"] is True


def test_attach_covers_all_means_empty_first_page(auth_cfg, tmp_home):
    """A transcript shorter than one page window has nothing older to load (the newest
    window is what the attach shows, or more)."""
    _write_jsonl(tmp_home, n_messages=4)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?cols=80")
    assert r.json() == {"ansi": "", "cursor": None, "has_more": False}


# ---- budgets ------------------------------------------------------------------


def test_lines_cap_truncates_render_not_cursor(auth_cfg, tmp_home, monkeypatch):
    """``lines`` bounds the RENDERED OUTPUT only: the page shows its newest turns within
    the cap, while the cursor still steps past the whole fixed-size turn window."""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "10")
    _write_jsonl(tmp_home, n_messages=40)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?before=40&lines=8&cols=80")
    page = r.json()
    # Each turn renders as a blank spacer + one content line; 8 lines ≈ 4 turns shown.
    assert 0 < len(page["ansi"].split("\r\n")) <= 8
    marks = _markers(page["ansi"])
    assert marks[-1] == "M039"  # newest renders survive; oldest were truncated away
    # The cursor stepped by the FULL turn window (10), not by what was rendered (~4).
    assert page["cursor"] == 30
    assert page["has_more"] is True


def test_lines_param_clamped_to_env_cap(auth_cfg, tmp_home, monkeypatch):
    """A request can never exceed AGENT_SESSIONS_HISTORY_PAGE_LINES."""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_LINES", "10")
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "10")
    _write_jsonl(tmp_home, n_messages=40)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?before=40&lines=99999&cols=80")
    page = r.json()
    assert len(page["ansi"].split("\r\n")) <= 10
    assert page["cursor"] == 30  # render cap clamped; the turn step is untouched
    assert page["has_more"] is True


def test_bytes_cap_truncates_render_not_cursor(auth_cfg, tmp_home, monkeypatch):
    """With a tiny rendered-bytes cap the page keeps the NEWEST renders of the window —
    and the cursor STILL steps past the whole window (render truncation never moves it,
    so it cannot reintroduce width-dependent cursors)."""
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_BYTES", "200")
    monkeypatch.setenv("AGENT_SESSIONS_HISTORY_PAGE_TURNS", "10")
    _write_jsonl(tmp_home, n_messages=40)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?before=40&lines=500&cols=80")
    page = r.json()
    marks = _markers(page["ansi"])
    assert marks, "at least one turn must always be served (progress guarantee)"
    assert marks[-1] == "M039"  # newest of the window survives; oldest were truncated
    assert len(marks) < 10  # the byte cap really did truncate the render
    assert page["cursor"] == 30  # …but the cursor stepped by the full window anyway
    assert page["has_more"] is True


# ---- single in-flight ---------------------------------------------------------


def test_concurrent_render_429s(auth_cfg, tmp_home):
    """One in-flight render per session key: a second request while one is rendering is
    answered 429 (the documented choice; the client serializes anyway)."""
    _write_jsonl(tmp_home, n_messages=10)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    key = f"claude:{SID}"
    history_routes._INFLIGHT.add(key)
    try:
        r = c.get(f"/api/sessions/{key}/history?before=10")
        assert r.status_code == 429
    finally:
        history_routes._INFLIGHT.discard(key)
    # And the marker is released after a normal request (try/finally).
    r = c.get(f"/api/sessions/{key}/history?before=10")
    assert r.status_code == 200
    assert key not in history_routes._INFLIGHT


# ---- pure paging unit ---------------------------------------------------------


def test_fetch_page_no_adapter_unit(monkeypatch, tmp_path):
    monkeypatch.setattr(transcript, "adapter_for", lambda _eng: None)
    page = history.fetch_page("claude", SID, home=tmp_path)
    assert (page.ansi, page.cursor, page.has_more) == (b"", None, False)


def test_fetch_page_single_huge_turn_still_progresses(tmp_path, monkeypatch):
    """A turn whose lone render exceeds the budgets is still served (bounded by the
    lines cap) and the cursor moves past it — paging can't wedge."""
    turns = [transcript.Turn("assistant", "word " * 5000)]
    monkeypatch.setattr(transcript, "adapter_for", lambda _eng: lambda _native, _home: turns)
    page = history.fetch_page("claude", SID, before=1, cols=40, lines=5, home=tmp_path)
    assert page.ansi  # progress: the one turn is served, truncated to the lines cap
    assert page.ansi.count(b"\r\n") + 1 <= 5
    assert page.cursor is None and page.has_more is False


def test_wide_client_cols_clamped_not_rejected(auth_cfg, tmp_home):
    # Prod regression (2026-06-10): a 666-col terminal got 422 from the le=500 bound and
    # showed a permanent error pill on every wide session. Wide clients clamp to the ws
    # grid's 500-col envelope; tiny values floor-clamp. Never 422 on geometry.
    _write_jsonl(tmp_home)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get(f"/api/sessions/claude:{SID}/history?cols=666&before=20")
    assert r.status_code == 200 and r.json()["ansi"]  # clamped render, real content
    r2 = c.get(f"/api/sessions/claude:{SID}/history?cols=1")
    assert r2.status_code == 200
