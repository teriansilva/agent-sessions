"""The /ws/term route's auth + validation gates (issue #49 Phase 2b).

The happy-path PTY bridge needs a real dtach + engine binary, so it's validated
on staging; here we pin that an unauthenticated / cross-origin / unknown-session
client is rejected BEFORE the socket is accepted — no raw shell stream is ever
exposed without the same gate as the HTTP routes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_sessions.auth import hash_password  # noqa: F401  (kept for parity w/ conftest)
from agent_sessions.main import create_app

_GOOD = "claude:11111111-1111-1111-1111-111111111111"


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login_headers(c, cfg, origin=None):
    """Log in and return ws headers carrying the session cookie explicitly.

    TestClient's websocket_connect does not reliably forward the cookie jar, so we
    pin the session cookie as a header — exactly what a browser sends."""
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    cookie = c.cookies.get("agent_sessions")
    return {"Origin": origin or cfg.origin, "Cookie": f"agent_sessions={cookie}"}


def _close_code(c, url, headers):
    """The route accepts, then closes with a code on rejection (so the browser gets
    the real code, not a 1006 handshake failure that would reconnect-loop). Connect,
    then read the deliberate close — TestClient may raise WebSocketDisconnect or
    return a {'type':'websocket.close','code':…} message depending on version."""
    try:
        with c.websocket_connect(url, headers=headers) as ws:
            msg = ws.receive()
            if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                return msg.get("code")
            return None
    except WebSocketDisconnect as e:
        return e.code


def test_ws_rejects_unauthenticated(auth_cfg):
    c = _client(auth_cfg)
    assert _close_code(c, f"/ws/term/{_GOOD}", {"Origin": auth_cfg.origin}) == 4401


def test_ws_rejects_bad_origin(auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg, origin="https://evil.example")
    assert _close_code(c, f"/ws/term/{_GOOD}", headers) == 4403


def test_ws_rejects_bad_engine_id(auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, "/ws/term/claude:not-a-uuid", headers) == 4404


def test_ws_rejects_unknown_session(fake_jsonl, auth_cfg):
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    # valid auth + origin + uuid shape, but not in the scanned set → 4404
    code = _close_code(c, "/ws/term/claude:99999999-9999-9999-9999-999999999999", headers)
    assert code == 4404


def test_ws_closes_on_unresolvable_binary(fake_jsonl, auth_cfg, monkeypatch):
    # A valid, authed, scanned session whose engine binary resolved to a bare name
    # (not an absolute path) must close deterministically (4500). Regression for #51.
    from agent_sessions import engines

    monkeypatch.setattr(engines, "CLAUDE_BIN", "claude")  # bare name → PtyBridgeError
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    code = _close_code(c, "/ws/term/claude:11111111-1111-1111-1111-111111111111", headers)
    assert code == 4500


def test_webterm_scrollback_ring_caps():
    # Per-session scrollback ring replays history on reattach; it must stay capped.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._buffer_append("claude:x", b"a" * 100)
    assert len(webterm._BUFFERS["claude:x"]) == 100
    webterm._buffer_append("claude:x", b"b" * (webterm._MAX_BUF + 5000))
    buf = webterm._BUFFERS["claude:x"]
    assert len(buf) == webterm._MAX_BUF  # oldest trimmed
    assert buf[-1:] == b"b"
    webterm._BUFFERS.clear()


def test_claude_new_launch_argv_honors_bypass():
    # Hermes PR #56: the bypass choice must actually affect the launch, not be ignored.
    from agent_sessions import engines

    p = engines.get("claude")
    u = "11111111-1111-1111-1111-111111111111"
    assert "--dangerously-skip-permissions" in p.new_launch_argv(u, cwd="/x", bypass=True)
    assert "--dangerously-skip-permissions" not in p.new_launch_argv(u, cwd="/x", bypass=False)


def test_in_alt_screen_detection():
    from agent_sessions import webterm

    assert webterm._in_alt_screen(b"hi\x1b[?1049hFRAME") is True  # entered, not left
    assert webterm._in_alt_screen(b"hi\x1b[?1049hF\x1b[?1049ldone") is False  # left again → inline
    assert webterm._in_alt_screen(b"plain inline output, no alt") is False  # neither present


def test_ws_busy_rejects_4409(fake_jsonl, auth_cfg, monkeypatch):
    # open_action says the id is held by another writer (no local master) → 4409,
    # never a second relaunch. The client treats 4409 as "retry → attach".
    from agent_sessions import sessions

    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.BUSY, None))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_GOOD}", headers) == 4409


def test_ws_releases_launch_lock_on_launch_failure(fake_jsonl, auth_cfg, monkeypatch):
    # A LAUNCH that then fails to build argv (4500) must release the launch lock —
    # otherwise the id would be wedged BUSY until the app restarts. No master was
    # spawned, so transfer() closes the last fd and the lock frees.
    from agent_sessions import engines, sessionlock

    key = _GOOD
    monkeypatch.setattr(engines, "CLAUDE_BIN", "claude")  # bare name → PtyBridgeError → 4500
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{key}", headers) == 4500
    assert sessionlock.is_locked(key) is False  # launch lock released (no wedge)


def test_webterm_run_passes_lock_fd_to_spawned_master(tmp_path, monkeypatch):
    # The launch lock's fd must be in pass_fds so the dtach master inherits it and
    # holds the flock for its lifetime (the cross-instance / restart guarantee).
    import asyncio

    from agent_sessions import sessionlock, webterm

    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    lock = sessionlock.acquire("claude:passfd")
    assert lock is not None
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["pass_fds"] = kwargs.get("pass_fds")
        raise OSError("stop before pumping")  # → webterm closes + ws.close(4500), returns

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    class FakeWS:
        async def close(self, code=None):
            pass

    asyncio.run(webterm.run(FakeWS(), ["dtach"], cwd=str(tmp_path), lock=lock))
    assert lock.fd in (captured["pass_fds"] or ())
    lock.transfer()


def test_resume_payload_tracks_total_and_serves_full_then_delta():
    # Delta-resume: _TOTALS counts every byte; have=0 → full replay; have within the
    # ring → only the bytes since `have`; have==total → nothing new.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    k = "claude:dr"
    webterm._buffer_append(k, b"hello ")
    webterm._buffer_append(k, b"world")
    assert webterm._TOTALS[k] == 11
    assert webterm._resume_payload(k, 0) == (b"hello world", 11)  # fresh attach → full
    assert webterm._resume_payload(k, 6) == (b"world", 11)  # reconnect → delta
    assert webterm._resume_payload(k, 11) == (b"", 11)  # caught up → nothing
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_resume_payload_full_replay_when_have_fell_behind_ring(monkeypatch):
    # If the client's offset fell behind the capped ring, send the whole ring (it can't
    # reconstruct the gap), not a wrong partial slice.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    monkeypatch.setattr(webterm, "_MAX_BUF", 10)
    k = "claude:dr2"
    webterm._buffer_append(k, b"abcdefghijklmnop")  # 16 bytes → ring trimmed to last 10
    assert webterm._TOTALS[k] == 16
    payload, total = webterm._resume_payload(k, 3)  # 3 < ring_start(6) → full ring
    assert total == 16 and payload == bytes(webterm._BUFFERS[k]) and len(payload) == 10
    assert webterm._resume_payload(k, 12)[0] == b"mnop"  # within ring → delta
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_resume_payload_alt_screen_sends_nothing():
    # Alt-screen TUI: never replay (repaints via SIGWINCH) and never blank — empty
    # payload, but still report the authoritative total.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    k = "opencode:alt"
    webterm._buffer_append(k, b"\x1b[?1049h a tui frame")  # entered alt screen
    payload, total = webterm._resume_payload(k, 0)
    assert payload == b"" and total == webterm._TOTALS[k]
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_buffer_cap_evicts_dead_sessions_oldest_first(monkeypatch):
    # Audit MEDIUM: the retained-buffer set stays bounded. When every retained session
    # is dead (no surviving dtach master), exceeding the cap evicts the oldest first.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    monkeypatch.setattr(webterm, "_MAX_BUFFERS", 4)
    monkeypatch.setattr(webterm, "_session_alive", lambda k: False)  # all dead → evictable

    for i in range(10):
        webterm._buffer_append(f"claude:s{i}", b"y")

    assert len(webterm._BUFFERS) == 4  # bounded
    assert "claude:s0" not in webterm._BUFFERS  # oldest evicted
    assert "claude:s9" in webterm._BUFFERS and "claude:s9" in webterm._TOTALS  # newest kept
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_idle_live_session_never_evicted(monkeypatch):
    # Regression (Hermes #121): an idle/attached LIVE session produces no output to
    # refresh its LRU recency, yet its scrollback must survive churn from other
    # sessions so a later reconnect can still delta-resume. The cap only evicts
    # buffers whose dtach master is gone — never a live one.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    monkeypatch.setattr(webterm, "_MAX_BUFFERS", 4)
    live = "claude:live-idle"
    # Only `live` is alive; every other (churning) session is dead/evictable.
    monkeypatch.setattr(webterm, "_session_alive", lambda k: k == live)

    webterm._buffer_append(live, b"important history")  # written ONCE, then idle
    for i in range(20):  # heavy churn from other sessions, well past the cap
        webterm._buffer_append(f"claude:dead{i}", b"y")

    assert live in webterm._BUFFERS  # live session preserved despite being the oldest + idle
    assert bytes(webterm._BUFFERS[live]) == b"important history"  # buffer intact for resume
    assert "claude:dead0" not in webterm._BUFFERS  # dead sessions evicted instead
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_maybe_evict_ended_drops_dead_keeps_live(monkeypatch):
    # On run-end we drop a session's scrollback only when its dtach master is gone;
    # a still-alive master keeps its buffer so a later reconnect can delta-resume.
    from agent_sessions import ptybridge, webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    key = "claude:11111111-1111-1111-1111-111111111111"
    webterm._buffer_append(key, b"history")

    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: True)
    webterm._maybe_evict_ended(key)
    assert key in webterm._BUFFERS  # master alive → kept

    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)
    webterm._maybe_evict_ended(key)
    assert key not in webterm._BUFFERS  # master gone → reclaimed
    assert key not in webterm._TOTALS
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


# ---- opencode new-session launch-then-reconcile (#127) ------------------------

_OC_PLACEHOLDER = "opencode:new-11111111-1111-1111-1111-111111111111"


def test_ws_opencode_placeholder_passes_validation_on_new(
    fake_jsonl, opencode_db, auth_cfg, monkeypatch
):
    # The new-<uuid> placeholder must pass the ws id-validation gate on new=1 and reach the
    # LAUNCH path (it would 4404 if parse_key rejected it). We force the launch to fail at
    # argv-build (bare-name bin → 4500) to prove validation passed without needing a real
    # opencode/dtach. The launch cwd must be a pickable project.
    from agent_sessions import engines, scanner

    monkeypatch.setattr(engines, "OPENCODE_BIN", "opencode")  # bare name → PtyBridgeError → 4500
    cwd = next(iter(scanner.pickable_projects()))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_OC_PLACEHOLDER}?new=1&cwd={cwd}"
    assert _close_code(c, url, headers) == 4500  # past validation, into launch (not 4404)


def test_ws_opencode_placeholder_rejected_on_resume(fake_jsonl, opencode_db, auth_cfg):
    # Without new=1 the placeholder is not a valid id (resume/attach requires ses_…) → 4404.
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_OC_PLACEHOLDER}", headers) == 4404


def test_ws_opencode_placeholder_rejects_unpickable_cwd(fake_jsonl, opencode_db, auth_cfg):
    # new=1 with a cwd that isn't a pickable project → 4404 (same guard as claude/gemini).
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_OC_PLACEHOLDER}?new=1&cwd=/not/a/project"
    assert _close_code(c, url, headers) == 4404


class _FakeWS:
    """Minimal ws stand-in capturing control frames sent by the reconcile coroutine."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text):
        self.sent.append(text)


def test_reconcile_single_id_persists_alias_and_converges(tmp_home, monkeypatch):
    # The reconcile coroutine: one new id → persist placeholder→real alias + send the
    # {"t":"id","sid":real} converge frame, then stop.
    import asyncio
    import json

    from agent_sessions import engines, main, metadata

    monkeypatch.setattr(main, "_OC_RECONCILE_INTERVAL_S", 0.001)
    prov = engines.get("opencode")
    placeholder = "new-11111111-1111-1111-1111-111111111111"
    real = "ses_reconciled000000000000000"
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: real)

    ws = _FakeWS()
    asyncio.run(main._reconcile_opencode(ws, prov, placeholder, "/cwd", set()))

    assert metadata.load_aliases() == {f"opencode:{placeholder}": f"opencode:{real}"}
    assert ws.sent and json.loads(ws.sent[-1]) == {"t": "id", "sid": f"opencode:{real}"}


def test_reconcile_ambiguous_no_alias_no_converge(tmp_home, monkeypatch):
    # Two new same-cwd ids → ambiguous: never guess. No alias, no converge frame.
    import asyncio

    from agent_sessions import engines, main, metadata

    monkeypatch.setattr(main, "_OC_RECONCILE_INTERVAL_S", 0.001)
    prov = engines.get("opencode")
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: ["ses_a000", "ses_b000"])

    ws = _FakeWS()
    asyncio.run(main._reconcile_opencode(ws, prov, "new-x", "/cwd", set()))

    assert metadata.load_aliases() == {}  # no alias recorded
    assert ws.sent == []  # no converge frame


def test_reconcile_timeout_when_row_never_written(tmp_home, monkeypatch):
    # opencode never writes the row (reconcile always None) → poll budget exhausts, the
    # coroutine returns quietly with no alias/frame (session keeps serving on placeholder).
    import asyncio

    from agent_sessions import engines, main, metadata

    monkeypatch.setattr(main, "_OC_RECONCILE_INTERVAL_S", 0.0001)
    monkeypatch.setattr(main, "_OC_RECONCILE_MAX_POLLS", 3)
    prov = engines.get("opencode")
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: None)

    ws = _FakeWS()
    asyncio.run(main._reconcile_opencode(ws, prov, "new-x", "/cwd", set()))

    assert metadata.load_aliases() == {}
    assert ws.sent == []


def test_ws_opencode_resume_real_id_with_aliased_dead_master(
    fake_jsonl, opencode_db, auth_cfg, monkeypatch
):
    # #127 review (bug 2): an alias placeholder→real must NOT make a real ``ses_…`` URL
    # 4404 when the placeholder master is gone. With the alias set + NO live dtach master,
    # attaching by the real id must RESUME the scanned opencode session (reach launch →
    # 4500 on a bare-name bin), not 4404 — which is what would happen if `native` were
    # overwritten to the placeholder before the resume scan.
    from agent_sessions import engines, metadata

    OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"  # the scanned opencode session in opencode_db
    monkeypatch.setattr(engines, "OPENCODE_BIN", "opencode")  # bare → PtyBridgeError → 4500
    metadata.set_alias(_OC_PLACEHOLDER, f"opencode:{OC_TOP}")  # placeholder → real
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    # real id, no new=1, no live master → must resume (4500), not 4404.
    assert _close_code(c, f"/ws/term/opencode:{OC_TOP}", headers) == 4500


def test_ws_opencode_placeholder_launch_failure_releases_lock(
    fake_jsonl, opencode_db, auth_cfg, monkeypatch
):
    # #127 review (bug 1): a new=1 opencode placeholder arms the reconcile task BEFORE the
    # launch; if the launch then fails (4500), the finally cancels that task — whose
    # CancelledError must NOT bypass lock.transfer(). Proven by reconnecting to the same
    # placeholder: the launch lock was released, so the 2nd attempt LAUNCHes again (4500),
    # not BUSY (4409).
    from agent_sessions import engines, scanner

    monkeypatch.setattr(engines, "OPENCODE_BIN", "opencode")  # bare → PtyBridgeError → 4500
    cwd = next(iter(scanner.pickable_projects()))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_OC_PLACEHOLDER}?new=1&cwd={cwd}"
    assert _close_code(c, url, headers) == 4500
    assert _close_code(c, url, headers) == 4500  # lock released → not 4409 BUSY
