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


def test_ws_resume_rejected_outside_roots(fake_jsonl, auth_cfg, monkeypatch):
    # Hard root scope (#465/#467): a scanned session whose cwd is OUTSIDE the configured roots is
    # NOT resumable via the ws either — otherwise the ws is a back door to scoped-out sessions.
    # _GOOD is scanned at /home/user/claude/demoapp.io; a root elsewhere → out of scope → 4404.
    from agent_sessions import prefs, project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: ["/home/user/claude/other"])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_GOOD}", headers) == 4404


def test_ws_resume_allowed_when_no_roots(fake_jsonl, auth_cfg, monkeypatch):
    # Empty roots ⇒ unscoped (today's behaviour): the scanned session clears the resume gate and
    # only fails later on the unresolvable bare binary (4500) — proving it passed the scope check.
    from agent_sessions import engines, prefs, project_dirs

    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")  # bare name → 4500 past the gate
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_GOOD}", headers) == 4500


def test_ws_new_session_rejected_outside_roots(auth_cfg, tmp_home, monkeypatch):
    # A NEW session may launch only in an in-scope cwd when roots are set (#465/#467): a browsable
    # $HOME dir OUTSIDE the root is rejected (it would be accepted unscoped, being browsable).
    from agent_sessions import prefs, project_dirs

    root = tmp_home / "code"
    outside = tmp_home / "elsewhere"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: [str(root)])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    fresh = "claude:22222222-2222-2222-2222-222222222222"
    assert _close_code(c, f"/ws/term/{fresh}?new=1&cwd={outside}", headers) == 4404


def test_ws_closes_on_unresolvable_binary(fake_jsonl, auth_cfg, monkeypatch):
    # A valid, authed, scanned session whose engine binary resolved to a bare name
    # (not an absolute path) must close deterministically (4500). Regression for #51.
    from agent_sessions import engines

    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")  # bare name → PtyBridgeError
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


def test_buffer_append_records_last_output_at(monkeypatch):
    # #156: every observed byte stamps the key's wall-clock so /api/sessions can flag
    # "agent working". get_last_output_at returns None before the first byte and the
    # latest timestamp after each append.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()
    assert webterm.get_last_output_at("claude:y") is None

    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0)
    webterm._buffer_append("claude:y", b"first")
    assert webterm.get_last_output_at("claude:y") == 1000.0

    monkeypatch.setattr(webterm.time, "time", lambda: 1042.5)
    webterm._buffer_append("claude:y", b"second")
    assert webterm.get_last_output_at("claude:y") == 1042.5

    # _drop_buffer evicts the stamp too — no leak between session lifetimes.
    webterm._drop_buffer("claude:y")
    assert webterm.get_last_output_at("claude:y") is None
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()


def test_attach_replay_grace_suppresses_working_stamp(monkeypatch):
    # #195: bytes ingested within the post-attach grace window are the dtach screen
    # replay, not agent activity — they must fill the scrollback ring but NOT stamp the
    # working signal. Output after the window stamps normally.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()
    webterm._SUPPRESS_OUTPUT_UNTIL.clear()
    k = "claude:z"

    # Attach at t=1000 → grace covers up to t=1000 + _ATTACH_REPLAY_GRACE_S.
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0)
    webterm.note_attach(k)
    webterm._buffer_append(k, b"\x1b[2Jreplayed screen")  # the replay burst
    # Scrollback got the bytes, but the working signal did NOT (still inside the window).
    assert bytes(webterm._BUFFERS[k]) == b"\x1b[2Jreplayed screen"
    assert webterm.get_last_output_at(k) is None

    # A byte still inside the window (just before it closes) is also suppressed.
    grace = webterm._ATTACH_REPLAY_GRACE_S
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0 + grace - 0.01)
    webterm._buffer_append(k, b"more replay")
    assert webterm.get_last_output_at(k) is None

    # Past the window, genuine output stamps the working signal.
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0 + grace + 0.5)
    webterm._buffer_append(k, b"real output")
    assert webterm.get_last_output_at(k) == 1000.0 + grace + 0.5

    webterm._drop_buffer(k)
    assert k not in webterm._SUPPRESS_OUTPUT_UNTIL  # grace state evicted with the buffer
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()
    webterm._SUPPRESS_OUTPUT_UNTIL.clear()


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
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")  # bare name → PtyBridgeError → 4500
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
        raise OSError("stop before pumping")  # → webterm closes + ws.close(4502), returns

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    class FakeWS:
        async def close(self, code=None):
            pass

    asyncio.run(webterm.run(FakeWS(), ["dtach"], cwd=str(tmp_path), lock=lock))
    assert lock.fd in (captured["pass_fds"] or ())
    lock.transfer()


def test_webterm_run_spawn_failure_closes_4502(tmp_path, monkeypatch):
    # #346 Phase A: a transient spawn failure (fork EAGAIN at the cgroup task ceiling)
    # must close 4502 (client retries with backoff) — NOT 4500, which the client treats
    # as terminal and would leave a dead terminal until a page reload.
    import asyncio

    from agent_sessions import webterm

    async def fake_exec(*argv, **kwargs):
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    codes = []

    class FakeWS:
        async def close(self, code=None):
            codes.append(code)

    asyncio.run(webterm.run(FakeWS(), ["dtach"], cwd=str(tmp_path)))
    assert codes == [4502]


def test_webterm_run_spawn_hang_times_out_to_4502(tmp_path, monkeypatch):
    # A spawn that hangs (resource pressure) must not wedge the connection coroutine
    # forever — SPAWN_TIMEOUT_S bounds it, then the same retryable close fires.
    import asyncio

    from agent_sessions import webterm

    async def hung_exec(*argv, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hung_exec)
    monkeypatch.setattr(webterm, "SPAWN_TIMEOUT_S", 0.05)
    codes = []

    class FakeWS:
        async def close(self, code=None):
            codes.append(code)

    asyncio.run(webterm.run(FakeWS(), ["dtach"], cwd=str(tmp_path)))
    assert codes == [4502]


def test_set_winsize_floors_zero_to_one():
    # A 0×0 controlling tty makes Ink-style agents render into nothing (the #293/#292
    # garble at the source). _set_winsize must never pass 0 to TIOCSWINSZ.
    import os
    import struct
    import termios

    from agent_sessions import webterm

    master, slave = os.openpty()
    try:
        webterm._set_winsize(slave, 0, 0)
        rows, cols, _, _ = struct.unpack(
            "HHHH", __import__("fcntl").ioctl(slave, termios.TIOCGWINSZ, b"\0" * 8)
        )
        assert rows >= 1 and cols >= 1  # floored, never 0
    finally:
        os.close(master)
        os.close(slave)


def test_pump_in_drops_degenerate_resize_keeps_valid(tmp_path, monkeypatch):
    # #293 Phase 3: a spurious 0×0 / 1-col resize (mobile layout glitch) must NOT reach the
    # agent pty — sizing it to 0×0 while the VT mirror floors at 2×2 desyncs the widths and
    # garbles scroll-up. A real resize still applies.
    import asyncio
    import json as _json

    from agent_sessions import webterm

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(webterm, "_set_winsize", lambda fd, rows, cols: calls.append((rows, cols)))

    class FakeWS:
        def __init__(self, frames):
            self._frames = list(frames)

        async def receive(self):
            return self._frames.pop(0) if self._frames else {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            pass

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    frames = [
        {"text": _json.dumps({"t": "r", "cols": 0, "rows": 0})},  # degenerate → dropped
        {"text": _json.dumps({"t": "r", "cols": 100, "rows": 40})},  # real → applied
    ]
    # A silent, short-lived agent; buf_key=None so we isolate the pty-sizing path.
    asyncio.run(webterm.run(FakeWS(frames), ["sleep", "1"], cwd=str(tmp_path), buf_key=None))
    assert (0, 0) not in calls  # the degenerate resize never sized the pty
    assert (40, 100) in calls  # the real resize did


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
    monkeypatch.setattr(webterm.scrollback, "_MAX_BUF", 10)
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


_UUID = "11111111-1111-1111-1111-111111111111"


def test_transcript_payload_renders_clear_plus_conversation_for_claude(monkeypatch):
    # #242 PR2: a fresh load resolves the engine's transcript adapter, renders it at the client
    # width, and returns clear + rendered conversation (which scrolls into xterm scrollback)
    # plus the exact history cursor for the {"t":"hist"} frame (#348 / Hermes #365 r2).
    from agent_sessions import transcript, webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    turns = [transcript.Turn("user", "do the thing"), transcript.Turn("assistant", "done")]
    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda eid: (lambda native, home: turns) if eid == "claude" else None,
    )
    res = webterm._transcript_payload(f"claude:{_UUID}", 80)
    assert res is not None
    out, cursor = res
    assert out.startswith(webterm._CLEAN_LOAD_CLEAR)  # clears first
    assert b"do the thing" in out and b"done" in out  # the conversation
    assert cursor == 0  # nothing truncated → the payload covers the whole transcript


def test_transcript_payload_exports_exact_truncation_boundary(monkeypatch):
    # Hermes #365 r2 finding 1: when the attach render truncates, the payload must carry the
    # EXACT first rendered turn index — the {"t":"hist"} cursor the client seeds `before=` from —
    # not leave the server to re-derive the boundary at the (possibly resized) request width.
    from agent_sessions import transcript, webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    # 10 turns, each rendering as a blank spacer + one content line (2 lines/turn).
    turns = [
        transcript.Turn("user" if i % 2 == 0 else "assistant", f"T{i:02d} msg") for i in range(10)
    ]
    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda eid: (lambda native, home: turns) if eid == "claude" else None,
    )
    monkeypatch.setattr(transcript, "DEFAULT_MAX_LINES", 6)  # keeps the newest 3 turns exactly
    res = webterm._transcript_payload(f"claude:{_UUID}", 80)
    assert res is not None
    out, cursor = res
    assert b"T07" in out and b"T09" in out and b"T06" not in out
    assert cursor == 7  # exact: turns[7:] delivered → first lazy page must use before=7


def test_ws_attach_sends_hist_frame_with_exact_boundary_after_seq(tmp_path, monkeypatch):
    """Hermes #365 r2 finding 1, wire-level: a transcript attach must EXPORT its exact turn
    boundary — {"t":"hist","cursor":N} right after the seq frame — so the client's first
    lazy-load request carries `before=N` instead of trusting the server's width-dependent
    re-derivation (a resize between attach and first lazy-load skipped turns)."""
    import asyncio
    import json as _json

    from agent_sessions import transcript, webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    turns = [
        transcript.Turn("user" if i % 2 == 0 else "assistant", f"T{i:02d} msg") for i in range(10)
    ]
    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda eid: (lambda native, home: turns) if eid == "claude" else None,
    )
    monkeypatch.setattr(transcript, "DEFAULT_MAX_LINES", 6)  # newest 3 turns → boundary 7

    sent_text: list[str] = []
    sent_bytes: list[bytes] = []

    class FakeWS:
        async def receive(self):
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            sent_bytes.append(bytes(b))

        async def send_text(self, t):
            sent_text.append(t)

        async def close(self, code=None):
            pass

    asyncio.run(webterm.run(FakeWS(), ["sleep", "1"], cwd=str(tmp_path), buf_key=f"claude:{_UUID}"))
    frames = [_json.loads(t) for t in sent_text]
    kinds = [f.get("t") for f in frames]
    assert "seq" in kinds and "hist" in kinds
    assert kinds.index("hist") == kinds.index("seq") + 1  # hist follows seq directly
    assert next(f for f in frames if f["t"] == "hist") == {"t": "hist", "cursor": 7}
    # And the attach payload itself was the transcript render (clear + newest turns).
    assert sent_bytes and sent_bytes[0].startswith(webterm._CLEAN_LOAD_CLEAR)
    assert b"T09" in sent_bytes[0] and b"T06" not in sent_bytes[0]


def _run_attach_collect_bytes(tmp_path, key, *, cols, have):
    """Drive a single attach through ``webterm.run`` and return the binary frames it sent.

    FakeWS disconnects immediately, so ``run`` sends the resume payload (mode prefix +
    scroll-up) and tears down without entering the live pump — enough to observe whether the
    attach decided on a clean-load clear or a raw continuation."""
    import asyncio

    from agent_sessions import webterm

    sent_bytes: list[bytes] = []

    class FakeWS:
        async def receive(self):
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            sent_bytes.append(bytes(b))

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    asyncio.run(
        webterm.run(FakeWS(), ["sleep", "1"], cwd=str(tmp_path), buf_key=key, cols=cols, have=have)
    )
    return sent_bytes


def test_ws_attach_clears_when_client_is_ahead_of_rehydrated_ring(tmp_path, monkeypatch):
    """#484 caller-level: after an app restart the ring is rehydrated head-trimmed while the
    authored width is restored, so a SAME-width reconnect can carry a pre-restart ``have`` that
    now exceeds the smaller ``total``. The attach must NOT treat that as a continuation — it must
    take the width-correct path whose payload begins with the clean-load clear, wiping the client's
    stale scrollback. Without the fix the predicate said "continuation" → the full ring was replayed
    UNDER the stale screen and the conversation rendered twice."""
    from agent_sessions import scrollback, transcript, webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    scrollback._LAST_COLS.clear()
    scrollback._LOADED_FROM_DISK.clear()
    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    turns = [
        transcript.Turn("user" if i % 2 == 0 else "assistant", f"T{i:02d} msg") for i in range(6)
    ]
    monkeypatch.setattr(
        transcript,
        "adapter_for",
        lambda eid: (lambda native, home: turns) if eid == "claude" else None,
    )

    key = "claude:22222222-2222-2222-2222-222222222222"
    scrollback.note_cols(key, 80)
    scrollback._buffer_append(key, b"OLD-RING-BYTES")  # total == 14, authored at width 80
    assert scrollback.ring_cols(key) == 80

    # have (99) far exceeds total (14) at the SAME width → not a continuation → clean-load clear.
    ahead = _run_attach_collect_bytes(tmp_path, key, cols=80, have=99)
    assert ahead and ahead[0].startswith(webterm._CLEAN_LOAD_CLEAR)  # stale scrollback wiped first
    assert any(b"T05" in b for b in ahead)  # ...then the width-correct transcript render
    assert not any(
        b"OLD-RING-BYTES" in b for b in ahead
    )  # raw ring NOT stacked under the stale screen

    # Contrast: an in-ring same-width offset is still a seamless continuation — raw delta, NO clear
    # (the #304/#359/#374 no-flicker reconnect must not be demoted to a clear).
    cont = _run_attach_collect_bytes(tmp_path, key, cols=80, have=4)
    assert cont and not cont[0].startswith(webterm._CLEAN_LOAD_CLEAR)
    assert cont[0] == b"RING-BYTES"  # exactly the bytes since have=4

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()


def test_transcript_payload_none_when_disabled(monkeypatch):
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", False)
    assert webterm._transcript_payload(f"claude:{_UUID}", 80) is None


def test_transcript_payload_none_without_adapter_or_turns(monkeypatch):
    from agent_sessions import transcript, webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    # no adapter for the engine → fall back (None)
    monkeypatch.setattr(transcript, "adapter_for", lambda eid: None)
    assert webterm._transcript_payload(f"claude:{_UUID}", 80) is None
    # adapter but empty conversation → fall back (None)
    monkeypatch.setattr(transcript, "adapter_for", lambda eid: lambda native, home: [])
    assert webterm._transcript_payload(f"claude:{_UUID}", 80) is None


def test_transcript_payload_none_on_unparseable_key(monkeypatch):
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_TRANSCRIPT_SCROLLBACK", True)
    assert webterm._transcript_payload("no-such-engine:whatever", 80) is None


def test_clean_load_payload_clears_on_a_width_mismatch():
    # #244/#262: the clean-load fallback (no transcript adapter) clears — skipping the width-fragile
    # replay — whenever the client width differs from the buffer's written width. The caller only
    # invokes it on a non-continuation, so `have` is no longer a parameter.
    from agent_sessions import webterm

    clear = webterm._CLEAN_LOAD_CLEAR
    # Width MISMATCH (e.g. mobile after a desktop session) → clear.
    assert webterm._clean_load_payload(100, client_cols=40, buffer_cols=120) == clear
    # Width MATCHES (desktop reload at the same width) → replay, keep scrollback.
    assert webterm._clean_load_payload(100, client_cols=120, buffer_cols=120) is None
    # Unknown buffer width (e.g. right after a restart) is treated as a MISMATCH → clear: never
    # trust bytes of unproven width (the ring is reset + rebuilt at the client width, Hermes #245).
    assert webterm._clean_load_payload(100, client_cols=40, buffer_cols=None) == clear
    # Brand-new session (no output yet) → nothing to clear.
    assert webterm._clean_load_payload(0, client_cols=40, buffer_cols=120) is None


def test_same_width_continuation_gates_raw_vs_transcript():
    # #262: only a have>0 reconnect whose width matches the last-served width keeps the raw
    # byte-delta (a brief same-width blip). Fresh load, cross-width, and post-restart
    # (buffer_cols=None) all return False → the caller renders the transcript, not the raw ring.
    from agent_sessions import webterm

    cont = webterm._is_same_width_continuation
    assert cont(have=120, total=200, buffer_cols=80, cols=80) is True  # same-width blip → raw delta
    assert cont(have=0, total=200, buffer_cols=80, cols=80) is False  # fresh load → transcript
    assert cont(have=120, total=200, buffer_cols=120, cols=40) is False  # cross-width → transcript
    assert (
        cont(have=120, total=200, buffer_cols=None, cols=40) is False
    )  # post-restart → transcript
    # #484: client ahead of the rehydrated (head-trimmed) ring → NOT a continuation, even at the
    # same width — else the full ring replays under the stale scrollback (dup conversation).
    assert cont(have=300, total=200, buffer_cols=80, cols=80) is False


def test_reset_ring_clears_content_keeps_total_and_removes_disk(monkeypatch, tmp_path):
    # #244/#245: a width change resets the retained ring (in-memory + disk mirror) but keeps the
    # monotonic _TOTALS offset, so a stale/mixed-width ring can't be replayed garbled by a later
    # same-width attach (the bug Hermes flagged on the first cut).
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_SCROLLBACK_DIR", tmp_path)
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    webterm._LOADED_FROM_DISK.clear()
    k = "claude:reset"
    webterm._buffer_append(k, b"x" * 50)
    assert len(webterm._BUFFERS[k]) == 50
    assert webterm._TOTALS[k] == 50
    assert webterm._scrollback_path(k).exists()  # mirrored to disk

    webterm._reset_ring(k)
    assert bytes(webterm._BUFFERS[k]) == b""  # content gone — no stale bytes to replay
    assert webterm._TOTALS[k] == 50  # offset preserved → delta-resume math stays valid
    assert not webterm._scrollback_path(k).exists()  # disk mirror removed too

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    webterm._LOADED_FROM_DISK.clear()


def test_buffer_cap_evicts_dead_sessions_oldest_first(monkeypatch):
    # Audit MEDIUM: the retained-buffer set stays bounded. When every retained session
    # is dead (no surviving dtach master), exceeding the cap evicts the oldest first.
    from agent_sessions import webterm

    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    monkeypatch.setattr(webterm.scrollback, "_MAX_BUFFERS", 4)
    monkeypatch.setattr(webterm.scrollback, "_session_alive", lambda k: False)  # all dead → evict

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
    monkeypatch.setattr(webterm.scrollback, "_MAX_BUFFERS", 4)
    live = "claude:live-idle"
    # Only `live` is alive; every other (churning) session is dead/evictable.
    monkeypatch.setattr(webterm.scrollback, "_session_alive", lambda k: k == live)

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
    from agent_sessions.engines import opencode

    monkeypatch.setattr(opencode.discover, "resolve", lambda engine_id: None)
    monkeypatch.setattr(engines.base, "OPENCODE_BIN", "opencode")  # bare → PtyBridgeError → 4500
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
    # new=1 with a cwd that isn't a pickable project AND escapes $HOME → 4404.
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_OC_PLACEHOLDER}?new=1&cwd=/not/a/project"
    assert _close_code(c, url, headers) == 4404


# ---- new-session cwd validation: ~/ folder-picker subdirs (#457) ---------------

_NEW_UUID = "12345678-1234-1234-1234-123456789abc"


def test_ws_new_claude_accepts_browsable_home_dir(fake_jsonl, auth_cfg, monkeypatch):
    # #457: a new-session cwd the ~/ folder picker can browse to (a real dir under $HOME) but
    # that isn't yet a pickable project must PASS validation, not 4404. Force the launch to fail
    # at argv-build (bare-name bin → 4500) to prove validation passed without a real claude/dtach.
    from agent_sessions import engines, scanner

    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "claude")  # bare → PtyBridgeError → 4500
    browsed = fake_jsonl / "fresh-proj"  # real $HOME subdir, no sessions → not pickable
    browsed.mkdir()
    assert str(browsed) not in set(scanner.pickable_projects(home=fake_jsonl))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/claude:{_NEW_UUID}?new=1&cwd={browsed}"
    assert _close_code(c, url, headers) == 4500  # past validation, into launch (not 4404)


def test_ws_new_claude_rejects_cwd_outside_home(fake_jsonl, auth_cfg):
    # #457: a cwd that escapes $HOME and isn't a pickable project is still rejected.
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/claude:{_NEW_UUID}?new=1&cwd=/etc"
    assert _close_code(c, url, headers) == 4404


_CDX_PLACEHOLDER = "codex:new-22222222-2222-2222-2222-222222222222"
_CDX_REAL = "codex:019e2ba1-1590-7003-8e4a-51ab62cec96e"


def test_ws_codex_placeholder_passes_validation_on_new(fake_jsonl, auth_cfg, monkeypatch, tmp_path):
    # codex new-session (#315): the new-<uuid> placeholder passes the ws id gate AND the
    # reconciling-provider placeholder guard on new=1, reaching LAUNCH (forced to 4500 via a
    # bare bin) — proving validation accepted it without needing a real codex/dtach.
    from agent_sessions import engines, scanner

    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(tmp_path / "cdx"))  # empty baseline
    monkeypatch.setattr(engines.base, "CODEX_BIN", "codex")  # bare → PtyBridgeError → 4500
    cwd = next(iter(scanner.pickable_projects()))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_CDX_PLACEHOLDER}?new=1&cwd={cwd}"
    assert _close_code(c, url, headers) == 4500  # past validation + guard, into launch


def test_ws_codex_real_uuid_rejected_on_new(fake_jsonl, auth_cfg, monkeypatch, tmp_path):
    # Regression (Hermes #318): a NON-placeholder (real/arbitrary) codex uuid on new=1 must be
    # REJECTED before launch. codex mints its own id, so a real id here would key the
    # socket/lock/scrollback by an existing session's identity and never reconcile. The cwd is
    # pickable, so the 4404 is the reconciling-provider placeholder guard, not the cwd check.
    from agent_sessions import scanner

    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(tmp_path / "cdx"))
    cwd = next(iter(scanner.pickable_projects()))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_CDX_REAL}?new=1&cwd={cwd}"
    assert _close_code(c, url, headers) == 4404


def test_ws_codex_placeholder_rejected_on_resume(fake_jsonl, auth_cfg):
    # Without new=1 the placeholder isn't a valid id (resume/attach requires a real uuid) → 4404.
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    assert _close_code(c, f"/ws/term/{_CDX_PLACEHOLDER}", headers) == 4404


def test_ws_codex_alias_attach_uses_physical_runtime_and_logical_transcript(
    fake_jsonl, auth_cfg, monkeypatch
):
    # Codex new sessions launch under a placeholder dtach key, then reconcile to the real rollout
    # uuid. Attaching by the real URL must still attach to the placeholder runtime, while transcript
    # replay reads the real/logical key where Codex history is stored.
    from agent_sessions import engines, metadata, scanner, sessions
    from agent_sessions.routes import terminal

    placeholder = "codex:new-141532f2-58f7-4ba3-9d35-dd1f21e60a5b"
    real = "codex:019f45cb-50fa-7fb0-a1c2-1164c47f11f8"
    real_native = real.split(":", 1)[1]
    placeholder_native = placeholder.split(":", 1)[1]
    metadata.set_alias(placeholder, real)

    monkeypatch.setattr(terminal.owner, "takeover_enabled", lambda: False)
    monkeypatch.setattr(
        engines,
        "scan_all",
        lambda: [
            scanner.Session(
                engine="codex",
                uuid=real_native,
                cwd="/tmp/project",
                last_mtime=1.0,
                first_user_message="",
                archived=False,
            )
        ],
    )
    actions = []

    def fake_open_action(engine, native):
        actions.append((engine, native))
        return sessions.ATTACH, None

    seen = {}

    async def fake_run(ws, argv, *, cwd, buf_key=None, transcript_key=None, **kwargs):
        seen.update(
            argv=argv,
            cwd=cwd,
            buf_key=buf_key,
            transcript_key=transcript_key,
            kwargs=kwargs,
        )
        await ws.close(code=1000)

    monkeypatch.setattr(sessions, "open_action", fake_open_action)
    monkeypatch.setattr(terminal.webterm, "run", fake_run)

    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    try:
        with c.websocket_connect(f"/ws/term/{real}", headers=headers) as ws:
            for _ in range(4):
                if seen:
                    break
                ws.receive()
    except WebSocketDisconnect:
        pass

    assert actions == [("codex", placeholder_native)]
    assert seen["cwd"] == "/tmp/project"
    assert seen["buf_key"] == placeholder
    assert seen["transcript_key"] == real


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

    from agent_sessions import ai_review_loop, engines, main, metadata

    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0.001)
    kicks = []
    monkeypatch.setattr(ai_review_loop, "request_review_soon", lambda: kicks.append(1))
    prov = engines.get("opencode")
    placeholder = "new-11111111-1111-1111-1111-111111111111"
    real = "ses_reconciled000000000000000"
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: real)

    ws = _FakeWS()
    asyncio.run(main._reconcile_new_session(ws, prov, placeholder, "/cwd", set()))

    assert metadata.load_aliases() == {f"opencode:{placeholder}": f"opencode:{real}"}
    assert ws.sent and json.loads(ws.sent[-1]) == {"t": "id", "sid": f"opencode:{real}"}
    # The reconciled real session is woken for prompt AI review (#413).
    assert kicks == [1]


def test_reconcile_ambiguous_no_alias_no_converge(tmp_home, monkeypatch):
    # Two new same-cwd ids → ambiguous: never guess. No alias, no converge frame.
    import asyncio

    from agent_sessions import ai_review_loop, engines, main, metadata

    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0.001)
    kicks = []
    monkeypatch.setattr(ai_review_loop, "request_review_soon", lambda: kicks.append(1))
    prov = engines.get("opencode")
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: ["ses_a000", "ses_b000"])

    ws = _FakeWS()
    asyncio.run(main._reconcile_new_session(ws, prov, "new-x", "/cwd", set()))

    assert metadata.load_aliases() == {}  # no alias recorded
    assert ws.sent == []  # no converge frame
    assert kicks == []  # ambiguous → no real session → no review kick


def test_reconcile_timeout_when_row_never_written(tmp_home, monkeypatch):
    # opencode never writes the row (reconcile always None) → poll budget exhausts, the
    # coroutine returns quietly with no alias/frame (session keeps serving on placeholder).
    import asyncio

    from agent_sessions import engines, main, metadata

    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0.0001)
    monkeypatch.setattr(main, "_RECONCILE_MAX_POLLS", 3)
    prov = engines.get("opencode")
    monkeypatch.setattr(prov, "reconcile_new_session", lambda cwd, snap: None)

    ws = _FakeWS()
    asyncio.run(main._reconcile_new_session(ws, prov, "new-x", "/cwd", set()))

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
    from agent_sessions.engines import opencode

    OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"  # the scanned opencode session in opencode_db
    monkeypatch.setattr(opencode.discover, "resolve", lambda engine_id: None)
    monkeypatch.setattr(engines.base, "OPENCODE_BIN", "opencode")  # bare → PtyBridgeError → 4500
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
    from agent_sessions.engines import opencode

    monkeypatch.setattr(opencode.discover, "resolve", lambda engine_id: None)
    monkeypatch.setattr(engines.base, "OPENCODE_BIN", "opencode")  # bare → PtyBridgeError → 4500
    cwd = next(iter(scanner.pickable_projects()))
    c = _client(auth_cfg)
    headers = _login_headers(c, auth_cfg)
    url = f"/ws/term/{_OC_PLACEHOLDER}?new=1&cwd={cwd}"
    assert _close_code(c, url, headers) == 4500
    assert _close_code(c, url, headers) == 4500  # lock released → not 4409 BUSY


# ---- persistent scrollback (#206) --------------------------------------------


# The `_isolate_scrollback` autouse fixture (conftest) points `_SCROLLBACK_DIR` at a
# per-test tmp dir and resets the in-memory ring, so these tests start clean.


def test_scrollback_persists_and_rehydrates_across_restart():
    """#206: output is mirrored to a per-session file; after a (simulated) restart wipes
    the in-memory ring, a fresh attach rehydrates scrollback from disk."""
    from agent_sessions import webterm

    k = "claude:11111111-1111-1111-1111-111111111111"
    webterm._buffer_append(k, b"hello ")
    webterm._buffer_append(k, b"world")
    assert webterm._scrollback_path(k).read_bytes() == b"hello world"  # mirrored to disk

    # Simulate an app restart: in-memory state gone, disk file remains.
    webterm._BUFFERS.clear()
    webterm._TOTALS.clear()
    webterm._LOADED_FROM_DISK.clear()

    payload, total = webterm._resume_payload(k, 0)  # fresh attach
    assert payload == b"hello world"  # restored from disk
    assert total == len(b"hello world")


def test_clear_scrollback_by_key_and_all():
    """#206: clear_scrollback removes the right files and drops the in-memory ring so a
    cleared session is not re-served from memory."""
    from agent_sessions import webterm

    k1 = "claude:11111111-1111-1111-1111-111111111111"
    k2 = "claude:22222222-2222-2222-2222-222222222222"
    webterm._buffer_append(k1, b"one")
    webterm._buffer_append(k2, b"two")

    res = webterm.clear_scrollback([k1])
    assert res["removed"] == 1
    assert not webterm._scrollback_path(k1).exists()
    assert webterm._scrollback_path(k2).exists()
    assert k1 not in webterm._BUFFERS  # ring dropped too

    res_all = webterm.clear_scrollback(None)
    assert res_all["removed"] == 1  # only k2 left
    assert webterm.scrollback_cache_stats()["files"] == 0


def test_drop_buffer_keeps_disk_then_rehydrates():
    """#206: an in-memory eviction (`_drop_buffer`) must NOT delete the durable disk file;
    a later touch rehydrates from it."""
    from agent_sessions import webterm

    k = "claude:33333333-3333-3333-3333-333333333333"
    webterm._buffer_append(k, b"persist me")
    webterm._drop_buffer(k)
    assert webterm._scrollback_path(k).exists()  # disk survives eviction
    assert k not in webterm._BUFFERS

    payload, total = webterm._resume_payload(k, 0)
    assert payload == b"persist me"
    assert total == len(b"persist me")


def test_force_repaint_shrinks_2d_then_restores(monkeypatch):
    """#304/#329: a fresh attach to a dtach session shows nothing (a same-size attach delivers no
    SIGWINCH, so a winch-only-repaint agent like claude never redraws → blank/fragments on switch).
    _force_repaint shrinks the pty in BOTH dims, then restores — SIGWINCHing the dtach client each
    time, mirroring the resize path — to force one clean full repaint. The shrink is 2-D (not a
    1-col nudge) so the intermediate frame can't render byte-identical for a width-stable idle frame
    (#329: that left 'only some sessions' blank). Pin the sequence so the fix can't regress."""
    import asyncio
    import signal as _signal

    from agent_sessions import webterm

    calls: list = []
    monkeypatch.setattr(webterm, "_set_winsize", lambda fd, r, c: calls.append(("size", r, c)))
    monkeypatch.setattr(webterm, "_NUDGE_GAP_S", 0)  # drop the real inter-nudge delay

    class _Proc:
        def send_signal(self, sig):
            calls.append(("sig", sig))

    asyncio.run(webterm._force_repaint(7, _Proc(), 24, 80))
    assert calls == [
        ("size", 24 - webterm._NUDGE_ROWS_DELTA, 80 - webterm._NUDGE_COLS_DELTA),  # 2-D shrink
        ("sig", _signal.SIGWINCH),
        ("size", 24, 80),  # restore the real geometry
        ("sig", _signal.SIGWINCH),
    ]


def test_force_repaint_floors_small_terminals(monkeypatch):
    # On a tiny terminal the shrink must never go below 2 (TIOCSWINSZ 0/1 desyncs agent vs mirror).
    import asyncio

    from agent_sessions import webterm

    calls: list = []
    monkeypatch.setattr(webterm, "_set_winsize", lambda fd, r, c: calls.append((r, c)))
    monkeypatch.setattr(webterm, "_NUDGE_GAP_S", 0)
    asyncio.run(
        webterm._force_repaint(7, type("P", (), {"send_signal": lambda s, x: None})(), 3, 4)
    )
    assert calls[0] == (2, 2)  # floored, not 1 or 0


def test_force_repaint_holds_shrink_until_agent_repaints(monkeypatch):
    """#443: the restore must be a resize the agent can't coalesce with the shrink. When given the
    live byte counter, _force_repaint holds the shrunk geometry until the agent has actually
    repainted it (out_bytes advances) BEFORE restoring — so a busy agent's SIGWINCH debounce can't
    merge shrink+restore into a net-zero geometry change (which left the live region blank). Pin
    that the restore does NOT happen while the agent is still silent on the shrink."""
    import asyncio
    import signal as _signal

    from agent_sessions import webterm

    calls: list = []
    monkeypatch.setattr(webterm, "_set_winsize", lambda fd, r, c: calls.append(("size", r, c)))
    monkeypatch.setattr(webterm, "_NUDGE_GAP_S", 0)  # isolate the output-gate from the settle
    monkeypatch.setattr(webterm, "_NUDGE_POLL_S", 0.001)
    # High timeout: the output gate (not the bounded fallback) must be what releases the restore.
    monkeypatch.setattr(webterm, "_NUDGE_MAX_WAIT_S", 5.0)
    out_bytes = {"n": 0}

    class _Proc:
        def send_signal(self, sig):
            calls.append(("sig", sig))

    restore = ("size", 24, 80)

    async def drive():
        task = asyncio.create_task(webterm._force_repaint(7, _Proc(), 24, 80, out_bytes=out_bytes))
        await asyncio.sleep(0.05)  # let the shrink land; the agent is still "silent"
        assert restore not in calls, "restored before the agent repainted the shrink (coalescable)"
        out_bytes["n"] += 7  # the agent's shrink-repaint arrives → gate releases
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(drive())
    assert calls == [
        ("size", 24 - webterm._NUDGE_ROWS_DELTA, 80 - webterm._NUDGE_COLS_DELTA),  # 2-D shrink
        ("sig", _signal.SIGWINCH),
        restore,  # only AFTER the agent rendered the shrink
        ("sig", _signal.SIGWINCH),
    ]


def test_force_repaint_restores_after_timeout_when_agent_silent(monkeypatch):
    """#443: a wedged/exited agent never repaints — the output gate must be BOUNDED so the restore
    still fires and the nudge can't hang the connection."""
    import asyncio
    import signal as _signal

    from agent_sessions import webterm

    calls: list = []
    monkeypatch.setattr(webterm, "_set_winsize", lambda fd, r, c: calls.append(("size", r, c)))
    monkeypatch.setattr(webterm, "_NUDGE_GAP_S", 0)
    monkeypatch.setattr(webterm, "_NUDGE_POLL_S", 0.005)
    monkeypatch.setattr(webterm, "_NUDGE_MAX_WAIT_S", 0.05)
    out_bytes = {"n": 0}  # never advances → agent is silent

    class _Proc:
        def send_signal(self, sig):
            calls.append(("sig", sig))

    asyncio.run(webterm._force_repaint(7, _Proc(), 24, 80, out_bytes=out_bytes))
    # Restored despite the silence (bounded wait), full shrink→restore sequence intact.
    assert calls == [
        ("size", 24 - webterm._NUDGE_ROWS_DELTA, 80 - webterm._NUDGE_COLS_DELTA),
        ("sig", _signal.SIGWINCH),
        ("size", 24, 80),
        ("sig", _signal.SIGWINCH),
    ]


# ---- #349: resize-vs-nudge coalescing + blank-attach nudge ------------------------


class _FakeProc:
    """Stands in for the dtach client: keeps the slave end open so the master doesn't
    EOF, records signals, and resolves wait() once terminated."""

    def __init__(self, slave_fd):
        import os as _os

        self.pid = 4242
        self._slave_dup = _os.dup(slave_fd)
        self._done = None  # asyncio.Event, created lazily on the running loop
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)

    def _finish(self):
        import asyncio as _aio
        import os as _os

        if self._slave_dup is not None:
            with __import__("contextlib").suppress(OSError):
                _os.close(self._slave_dup)
            self._slave_dup = None
        if self._done is None:
            self._done = _aio.Event()
        self._done.set()

    def terminate(self):
        self._finish()

    def kill(self):
        self._finish()

    async def wait(self):
        import asyncio as _aio

        if self._done is None:
            self._done = _aio.Event()
        await self._done.wait()
        return 0


class _ScriptedWS:
    """Feeds a scripted message sequence to pump_in; sends are no-ops."""

    def __init__(self, script):
        self._script = list(script)  # items: ("sleep", s) | dict ws message

    async def receive(self):
        import asyncio as _aio

        while self._script:
            item = self._script.pop(0)
            if isinstance(item, tuple) and item[0] == "sleep":
                await _aio.sleep(item[1])
                continue
            return item
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, b):
        pass

    async def send_text(self, t):
        pass

    async def close(self, code=None):
        pass


def _run_349(monkeypatch, *, key, have, script, settle=0.05):
    """Drive webterm.run with a fake dtach proc; return the _force_repaint call log."""
    import asyncio

    from agent_sessions import webterm

    calls = []

    async def record_repaint(master, proc, rows, cols, *, out_bytes=None):
        calls.append((rows, cols))

    monkeypatch.setattr(webterm, "_force_repaint", record_repaint)
    monkeypatch.setattr(webterm, "_NUDGE_SETTLE_S", settle)

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(kwargs["stdin"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ws = _ScriptedWS(script)
    asyncio.run(webterm.run(ws, ["dtach"], cwd="/tmp", buf_key=key, have=have))
    return calls


def _resize(cols, rows):
    import json

    return {"type": "websocket.receive", "text": json.dumps({"t": "r", "cols": cols, "rows": rows})}


def test_resize_burst_in_attach_window_yields_one_trailing_repaint(monkeypatch):
    # #349 core: width changes during the attach window must re-arm exactly ONE
    # debounced trailing repaint, restored at the LATEST accepted geometry — not the
    # attach-time grid, and not one repaint per resize.
    from agent_sessions import webterm

    key = "claude:renudge-1"
    webterm._BUFFERS[key] = bytearray(b"0123456789")  # have<total → real delta payload
    webterm._TOTALS[key] = 10  # total≥have so this stays a same-width continuation (#484 guard)
    webterm.scrollback._LAST_COLS[key] = 80  # same-width continuation at cols=80
    try:
        calls = _run_349(
            monkeypatch,
            key=key,
            have=5,
            script=[
                _resize(70, 20),
                _resize(90, 30),
                _resize(100, 40),  # rapid burst — only this final geometry may repaint
                ("sleep", 0.35),  # let the debounced trailing nudge fire
            ],
        )
    finally:
        webterm._drop_buffer(key)
    assert calls == [(40, 100)]


def test_have_resume_with_real_delta_does_not_nudge(monkeypatch):
    # The non-blank have>0 reconnect keeps the #304 no-flicker behavior.
    from agent_sessions import webterm

    key = "claude:renudge-2"
    webterm._BUFFERS[key] = bytearray(b"0123456789")
    webterm._TOTALS[key] = 10  # total≥have so this stays a same-width continuation (#484 guard)
    webterm.scrollback._LAST_COLS[key] = 80
    try:
        calls = _run_349(monkeypatch, key=key, have=5, script=[("sleep", 0.3)])
    finally:
        webterm._drop_buffer(key)
    assert calls == []


def test_blank_have_reconnect_still_nudges(monkeypatch):
    # #349: a have>0 reconnect whose attach delivered NOTHING visible (cold attach
    # after a broker restart: empty ring, no transcript) must nudge — otherwise an
    # idle agent leaves the client on a blank screen until the next input byte.
    from agent_sessions import webterm

    key = "claude:renudge-3"
    webterm._BUFFERS.pop(key, None)  # empty ring, no _LAST_COLS → cold attach
    calls = _run_349(monkeypatch, key=key, have=5, script=[("sleep", 0.3)])
    assert calls == [(24, 80)]


def test_up_to_date_same_width_reconnect_does_not_nudge(monkeypatch):
    # Hermes #359: have == total on a same-width continuation delivers an EMPTY delta —
    # that is "up to date", not "blank"; nudging it would flicker every quiet reconnect.
    from agent_sessions import webterm

    key = "claude:renudge-4"
    webterm._BUFFERS[key] = bytearray(b"0123456789")
    webterm._TOTALS[key] = 10  # have==total: up-to-date same-width continuation (#484 guard)
    webterm.scrollback._LAST_COLS[key] = 80
    try:
        calls = _run_349(monkeypatch, key=key, have=10, script=[("sleep", 0.3)])
    finally:
        webterm._drop_buffer(key)
    assert calls == []


def test_synthetic_attach_payload_ends_with_live_screen_seam(monkeypatch):
    # Operator report ("still a mess"): a transcript/VT attach payload ends at "now",
    # and the dtach replay below shows the same screen again — the boundary must be
    # marked or the duplicate reads as corruption.
    import asyncio

    from agent_sessions import webterm

    key = "claude:seam-1"
    webterm._BUFFERS.pop(key, None)
    monkeypatch.setattr(
        webterm.scrollback, "_transcript_payload", lambda k, c, r: (b"HISTORY-TAIL", 3)
    )
    sent = []

    class WS(_ScriptedWS):
        async def send_bytes(self, b):
            sent.append(bytes(b))

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(kwargs["stdin"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(webterm, "_NUDGE_SETTLE_S", 0.01)
    asyncio.run(webterm.run(WS([("sleep", 0.1)]), ["dtach"], cwd="/tmp", buf_key=key, have=0))
    payload = b"".join(sent)
    assert b"HISTORY-TAIL" in payload
    assert "live screen ↓".encode() in payload
    assert payload.find(b"HISTORY-TAIL") < payload.find("live screen ↓".encode())


def test_alias_backed_attach_uses_logical_key_for_transcript(monkeypatch):
    # Codex/opencode/antigravity new sessions run under a placeholder socket, then converge to
    # the real id. Runtime resources stay keyed by the placeholder, but transcript stores use the
    # real native id. Attaching by the real URL must therefore render history from the logical key.
    import asyncio

    from agent_sessions import webterm

    phys = "codex:new-141532f2-58f7-4ba3-9d35-dd1f21e60a5b"
    logical = "codex:019f45cb-50fa-7fb0-a1c2-1164c47f11f8"
    seen = []
    sent = []

    def transcript_payload(k, c, r):
        seen.append(k)
        return (b"CODEX-HISTORY-TAIL", 3)

    monkeypatch.setattr(webterm.scrollback, "_transcript_payload", transcript_payload)

    class WS(_ScriptedWS):
        async def send_bytes(self, b):
            sent.append(bytes(b))

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(kwargs["stdin"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(webterm, "_NUDGE_SETTLE_S", 0.01)
    asyncio.run(
        webterm.run(
            WS([("sleep", 0.1)]),
            ["dtach"],
            cwd="/tmp",
            buf_key=phys,
            transcript_key=logical,
            have=0,
        )
    )
    assert seen == [logical]
    assert b"CODEX-HISTORY-TAIL" in b"".join(sent)


def test_terminate_then_kill_reaps_sigterm_ignoring_child():
    """#532: a child that survives SIGTERM is escalated to SIGKILL within the bounded wait."""
    import asyncio
    import signal
    import sys

    from agent_sessions import webterm

    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('up', flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        assert (await proc.stdout.readline()).strip() == b"up"  # SIGTERM-ignore installed
        await webterm.terminate_then_kill(proc, timeout=0.3)
        return proc.returncode

    assert asyncio.run(scenario()) == -signal.SIGKILL


def test_webterm_run_teardown_reaps_sigterm_ignoring_client(tmp_path, monkeypatch):
    """#532: the viewer bridge's teardown must never leak a dtach client that ignores SIGTERM.

    The leaked client of the production incident stopped reading its socket and wedged the
    dtach master's broadcast select for every other viewer, so the bridge escalates to
    SIGKILL after the bounded wait. The stand-in installs a SIGTERM-ignore and then writes
    its pid; the fake ws holds the bridge open until that pid file exists, so the teardown's
    SIGTERM provably lands on a process that ignores it.
    """
    import asyncio
    import os
    import sys

    import pytest

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_TERMINATE_WAIT_S", 0.3)
    pidfile = tmp_path / "client.pid"
    argv = [
        sys.executable,
        "-c",
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)",
    ]

    class FakeWS:
        async def receive(self):
            while not pidfile.exists():  # keep the bridge open until the ignore is armed
                await asyncio.sleep(0.02)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            pass

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    asyncio.run(webterm.run(FakeWS(), argv, cwd=str(tmp_path), buf_key=None))
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped by the bridge — a leak would still answer signal 0
