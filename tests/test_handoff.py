"""Cross-engine handoff (#597, Phase 1 — Quick mode).

Pins the issue's acceptance contract: seed transport (never argv; PTY bracketed paste,
atomic single redemption), the prepare/commit lifecycle (side-effect-free prepare,
handle TTL + bind, double-commit refused), the capability matrix (shell excluded,
gemini/antigravity Phase 3, same-engine allowed, one capability source for UI + server),
source validation before any transcript read, and the provenance state machine
(aliveness gate; no dangling link on spawn failure; backlink only on the resolved real
id; reconcile fail-safe preserved).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import discover, engines, handoff, metadata
from agent_sessions.main import create_app

_SRC = "11111111-1111-1111-1111-111111111111"  # fake_jsonl session in /home/user/claude/repo-a


@pytest.fixture(autouse=True)
def _reset_handoff():
    handoff.reset_for_tests()
    yield
    handoff.reset_for_tests()


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
    return c.get("/api/config").json()["csrf"]


def _hdr(csrf, cfg):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _present_all(monkeypatch):
    """Pretend every engine binary is installed (capability tests isolate the flag logic)."""
    monkeypatch.setattr(discover, "resolve", lambda e: f"/usr/bin/{e}")


# ---- seed builder ---------------------------------------------------------------------------


def test_quick_seed_is_engine_neutral_and_carries_the_tail(fake_jsonl):
    seed, meta = handoff.build_quick_seed(
        "claude", _SRC, title="first message on repo-a", cwd="/home/user/claude/repo-a"
    )
    assert "[user] first message on repo-a" in seed
    assert "claude session" in seed  # provenance labelled
    assert meta["mode"] == "quick" and meta["turns"] == 1
    assert meta["bytes"] == len(seed.encode()) and meta["cap"] == handoff.SEED_CAP_BYTES


def test_quick_seed_empty_transcript_raises_409(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.build_quick_seed("claude", _SRC)
    assert ei.value.status == 409


def test_quick_seed_caps_long_transcripts(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="x" * 1024, kind="text") for _ in range(40)]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES
    assert meta["turns"] <= handoff.SEED_MAX_TURNS


def test_quick_seed_single_oversized_turn_is_truncated(fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    turns = [transcript.Turn(role="user", text="y" * (handoff.SEED_CAP_BYTES * 2), kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, meta = handoff.build_quick_seed("claude", _SRC)
    assert len(seed.encode()) <= handoff.SEED_CAP_BYTES + 8  # ellipsis slack
    assert meta["turns"] == 1


def test_quick_seed_strips_control_bytes_paste_breakout(fake_jsonl, monkeypatch):
    # An ESC in transcript content could terminate the bracketed paste early and smuggle
    # raw key input into the target agent — the builder strips every control byte.
    from agent_sessions import transcript

    evil = "before \x1b[201~\x1b[5;5H rm -rf \x07 after"
    turns = [transcript.Turn(role="user", text=evil, kind="text")]
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: turns))
    seed, _ = handoff.build_quick_seed("claude", _SRC)
    assert "\x1b" not in seed and "\x07" not in seed
    assert "before" in seed and "after" in seed


# ---- capability matrix ------------------------------------------------------------------------


def test_seed_start_capability_matrix():
    cases = {
        "claude": (True, None),
        "codex": (True, None),
        "opencode": (True, None),
        "gemini": (False, "no seed-capable start yet"),
        "antigravity": (False, "no seed-capable start yet"),
        "shell": (False, "not an agent engine"),
    }
    for engine_id, expected in cases.items():
        prov = engines.get(engine_id)
        assert handoff.seed_start_state(prov, present=True) == expected, engine_id
    # An uninstalled but otherwise capable engine is refused with its own reason.
    assert handoff.seed_start_state(engines.get("claude"), present=False) == (
        False,
        "not installed",
    )


def test_api_engines_carries_the_capability_and_reason(auth_cfg, tmp_home, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/engines").json()["engines"]}
    assert rows["claude"]["supports_seed_start"] is True
    assert rows["claude"]["seed_reason"] is None
    assert rows["gemini"]["supports_seed_start"] is False
    assert rows["gemini"]["seed_reason"] == "no seed-capable start yet"
    assert rows["shell"]["supports_seed_start"] is False
    assert rows["shell"]["seed_reason"] == "not an agent engine"


# ---- transport: the seed never touches argv ----------------------------------------------------


def test_seed_never_appears_in_any_launch_argv(fake_jsonl):
    sentinel = "first message on repo-a"  # the seed body's distinctive content
    seed, _ = handoff.build_quick_seed("claude", _SRC, title=sentinel)
    assert sentinel in seed
    for engine_id in ("claude", "codex", "opencode"):
        prov = engines.get(engine_id)
        argv = prov.new_launch_argv(
            "new-99999999-9999-9999-9999-999999999999"
            if getattr(prov, "new_session_reconciles", False)
            else _SRC,
            cwd="/tmp",
            bypass=True,
        )
        joined = "\x00".join(argv)
        assert sentinel not in joined and "Handoff" not in joined


# ---- handle store lifecycle -------------------------------------------------------------------


def test_handle_commit_binds_and_double_commit_409():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed body", cwd="/tmp")
    res = handoff.commit(h)
    assert res["engine"] == "claude" and res["cwd"] == "/tmp"
    assert res["id"] == f"claude:{res['native']}"
    assert not res["native"].startswith("new-")  # pinned-id engine
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit(h)
    assert ei.value.status == 409


def test_handle_commit_mints_placeholder_for_reconciling_engines():
    h = handoff.create_handle("claude:" + _SRC, "codex", "quick", "seed body", cwd="/tmp")
    res = handoff.commit(h)
    assert res["native"].startswith("new-")  # codex mints its own id → placeholder launch


def test_seed_claim_ack_is_single_delivery():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "the seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.has_pending_seed(key) is True
    # Claimants are serialized: while a claim is outstanding, a second viewer gets None.
    assert handoff.claim_seed(key) == "the seed"
    assert handoff.claim_seed(key) is None
    # A retry-ack releases the claim WITHOUT consuming — the seed survives for a retry.
    handoff.ack_seed(key, "retry")
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "the seed"
    # A delivered-ack consumes it — the single-delivery guarantee.
    handoff.ack_seed(key, "delivered")
    assert handoff.claim_seed(key) is None
    assert handoff.has_pending_seed(key) is False


def test_seed_abort_ack_consumes_without_retry():
    # A partial PTY write polluted the target's input — the claim is settled as consumed
    # (never retried blindly), which is explicit and logged at the delivery layer.
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "partial", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.claim_seed(key) == "partial"
    handoff.ack_seed(key, "abort")
    assert handoff.has_pending_seed(key) is False
    assert handoff.claim_seed(key) is None


def test_handle_expires_on_ttl(monkeypatch):
    monkeypatch.setattr(handoff, "HANDLE_TTL_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed", cwd="/tmp")
    time.sleep(0.1)
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit(h)
    assert ei.value.status == 404


def test_unknown_handle_404():
    with pytest.raises(handoff.HandoffError) as ei:
        handoff.commit("nope")
    assert ei.value.status == 404


# ---- provenance state machine ------------------------------------------------------------------


def test_mark_spawned_pinned_id_writes_both_sides(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    handoff.mark_spawned(key)
    handoff.mark_spawned(key)  # idempotent — a replay writes nothing twice
    assert metadata.get(key).handoff_from == src
    assert metadata.get(key).handoff_mode == "quick"
    assert metadata.get(key).handoff_at != ""
    assert metadata.get(src).handoff_to == key
    # PR #701 review P1: provenance publication must NOT evict the still-unredeemed seed —
    # the aliveness gate (8 s) can beat the injector's readiness wait (up to 45 s).
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "seed"
    handoff.ack_seed(key, "delivered")
    # Only now — seed consumed AND provenance+backlink written — is the entry released.
    assert handoff.has_pending_seed(key) is False
    assert handoff.arm_watch(key) is False


def test_mark_spawned_placeholder_defers_backlink_until_reconcile(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "codex", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]  # codex:new-<uuid>
    handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src
    # Backlink absent until the REAL id exists — never a placeholder backlink.
    assert metadata.get(src).handoff_to == ""
    real = "codex:99999999-9999-9999-9999-999999999999"
    handoff.note_reconciled(key, real)
    assert metadata.get(src).handoff_to == real
    # P1: spawn + reconcile both done, but the seed is still unredeemed — it survives.
    assert handoff.claim_seed(key) == "seed"
    handoff.ack_seed(key, "delivered")
    assert handoff.has_pending_seed(key) is False


def test_reconcile_before_spawn_watch_still_backlinks_real_id(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "codex", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    real = "codex:88888888-8888-8888-8888-888888888888"
    handoff.note_reconciled(key, real)  # reconcile wins the race
    assert metadata.get(src).handoff_to == ""  # still gated on aliveness
    handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src
    assert metadata.get(src).handoff_to == real


def test_abort_spawn_leaves_no_dangling_link(tmp_home):
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    handoff.abort_spawn(key)
    assert metadata.get(key).handoff_from == ""
    assert metadata.get(src).handoff_to == ""
    assert handoff.has_pending_seed(key) is False
    handoff.mark_spawned(key)  # post-abort replay is a no-op, not a resurrection
    assert metadata.get(key).handoff_from == ""


def test_note_reconciled_without_handoff_is_a_noop(tmp_home):
    # The reconcile coroutine calls this for EVERY placeholder converge; a plain
    # picker-started session (no handoff) must never gain provenance.
    handoff.note_reconciled("codex:new-77777777-7777-7777-7777-777777777777", "codex:" + _SRC)
    assert metadata.get("codex:" + _SRC).handoff_to == ""


def test_arm_watch_is_single_shot():
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    assert handoff.arm_watch(key) is True
    assert handoff.arm_watch(key) is False  # a ws reconnect never arms a second watch
    assert handoff.arm_watch("claude:99999999-9999-9999-9999-999999999999") is False


# ---- routes: prepare ---------------------------------------------------------------------------


def _prepare(c, csrf, cfg, **over):
    body = {"source_id": f"claude:{_SRC}", "target_engine": "codex", "mode": "quick"}
    body.update(over)
    return c.post("/api/handoff/prepare", json=body, headers=_hdr(csrf, cfg))


def test_prepare_returns_handle_preview_meta(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handle"]
    assert "[user] first message on repo-a" in body["preview"]
    assert body["meta"]["turns"] == 1 and body["meta"]["mode"] == "quick"


def test_prepare_requires_csrf(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.post(
        "/api/handoff/prepare",
        json={"source_id": f"claude:{_SRC}", "target_engine": "codex"},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_prepare_rejects_bad_source_id(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg, source_id="claude:nope").status_code == 404
    assert _prepare(c, csrf, auth_cfg, source_id="martian:123").status_code == 404


def test_prepare_rejects_shell_source(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, source_id=f"shell:{_SRC}")
    assert r.status_code == 422
    assert "shell" in r.json()["detail"]


def test_prepare_rejects_unsupported_targets(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    for target in ("shell", "gemini", "antigravity"):
        r = _prepare(c, csrf, auth_cfg, target_engine=target)
        assert r.status_code == 422, target
        assert "target engine unavailable" in r.json()["detail"]
    assert _prepare(c, csrf, auth_cfg, target_engine="martian").status_code == 404


def test_prepare_rejects_uninstalled_target(auth_cfg, fake_jsonl, monkeypatch):
    monkeypatch.setattr(discover, "resolve", lambda e: None)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 422
    assert "not installed" in r.json()["detail"]


def test_prepare_rejects_ai_mode_as_phase_2(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg, mode="ai").status_code == 422


def test_prepare_rejects_unscanned_source(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, source_id="claude:99999999-9999-9999-9999-999999999999")
    assert r.status_code == 404


def test_prepare_rejects_out_of_scope_source_before_transcript_read(
    auth_cfg, fake_jsonl, monkeypatch
):
    # Root scope (#465/#467): a scoped-out session is not readable through handoff either,
    # and the refusal happens before any transcript bytes are read.
    from agent_sessions import prefs, project_dirs, transcript

    _present_all(monkeypatch)
    monkeypatch.setattr(project_dirs, "effective_roots", lambda: ["/somewhere/else"])
    monkeypatch.setattr(prefs, "get_folder_exclusions", lambda path=None: [])

    def _boom(engine_id):
        raise AssertionError("transcript read attempted for an out-of-scope source")

    monkeypatch.setattr(transcript, "adapter_for", _boom)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert _prepare(c, csrf, auth_cfg).status_code == 404


def test_prepare_empty_transcript_is_409(auth_cfg, fake_jsonl, monkeypatch):
    from agent_sessions import transcript

    _present_all(monkeypatch)
    monkeypatch.setattr(transcript, "adapter_for", lambda e: (lambda native, home: []))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg)
    assert r.status_code == 409
    assert "empty" in r.json()["detail"]


def test_prepare_allows_same_engine_handoff(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = _prepare(c, csrf, auth_cfg, target_engine="claude")
    assert r.status_code == 200
    r2 = c.post("/api/handoff", json={"handle": r.json()["handle"]}, headers=_hdr(csrf, auth_cfg))
    assert r2.status_code == 200
    assert r2.json()["engine"] == "claude"


# ---- routes: commit ----------------------------------------------------------------------------


def test_commit_returns_target_and_seed_becomes_redeemable(auth_cfg, fake_jsonl, monkeypatch):
    _present_all(monkeypatch)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg).json()["handle"]
    r = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "codex" and body["native"].startswith("new-")
    rows = {r["id"]: r for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert body["cwd"] == rows[f"claude:{_SRC}"]["cwd"]  # the source session's cwd
    assert handoff.has_pending_seed(body["id"]) is True


def test_commit_unknown_or_expired_handle_404(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/handoff", json={"handle": "bogus"}, headers=_hdr(csrf, auth_cfg))
    assert r.status_code == 404


# ---- ws launch integration ---------------------------------------------------------------------


def test_ws_new_session_redeems_the_handoff_seed(auth_cfg, fake_jsonl, monkeypatch):
    """The committed handoff's ws launch passes the seed source to the PTY bridge (never
    argv) and arms exactly one spawn watch; the dead-master watch then aborts the handoff
    with no dangling sidecar link (the spawn-failure acceptance case)."""
    from agent_sessions import ptybridge, relaunch, sessions, webterm

    _present_all(monkeypatch)
    captured: dict = {}

    import contextlib

    from agent_sessions.routes import terminal as terminal_routes

    async def fake_run(ws, argv, **kwargs):
        captured["argv"] = argv
        captured["seed_key"] = kwargs.get("seed_key")
        # Await the spawn watch on ITS OWN loop, then tell the test — the client holds
        # the connection open until this frame, so the loop can't be torn down first.
        for t in list(terminal_routes._HANDOFF_WATCHES):
            with contextlib.suppress(Exception):
                await t
        await ws.send_text("WATCH-DONE")

    monkeypatch.setattr(webterm, "run", fake_run)
    monkeypatch.setattr(sessions, "open_action", lambda e, n: (sessions.LAUNCH, None))
    monkeypatch.setattr(relaunch, "blocked", lambda key: False)
    monkeypatch.setattr(relaunch, "note_exit", lambda *a, **k: None)
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.05)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: False)  # dies instantly
    monkeypatch.setattr(
        ptybridge, "launch_argv", lambda *, engine, session_id, launch_argv: ["/bin/true"]
    )
    monkeypatch.setattr(engines.base, "CLAUDE_BIN", "/bin/true")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    handle = _prepare(c, csrf, auth_cfg, target_engine="claude").json()["handle"]
    res = c.post("/api/handoff", json={"handle": handle}, headers=_hdr(csrf, auth_cfg)).json()
    key, cwd = res["id"], res["cwd"]

    cookie = c.cookies.get("agent_sessions")
    headers = {"Origin": auth_cfg.origin, "Cookie": f"agent_sessions={cookie}"}
    with c.websocket_connect(f"/ws/term/{key}?new=1&cwd={cwd}", headers=headers) as ws:
        # Skip the route's control frames (role/seq/…) until the bridge's done signal.
        for _ in range(10):
            if ws.receive_text() == "WATCH-DONE":
                break
        else:
            raise AssertionError("WATCH-DONE never arrived")
    assert captured["seed_key"] == key  # the bridge got the seed source…
    assert all("Handoff" not in a and "repo-a" not in a for a in captured["argv"])  # …not argv
    assert handoff.arm_watch(key) is False  # the launch armed the single watch
    # The watch (instant-exit master) aborted the handoff: seed gone, NO provenance.
    assert handoff.has_pending_seed(key) is False
    assert metadata.get(key).handoff_from == ""
    assert metadata.get(f"claude:{_SRC}").handoff_to == ""


# ---- PTY injection ------------------------------------------------------------------------------

_CHILD = r"""
import os, sys, time, tty
tty.setraw(0)
os.write(1, b"\x1b[?2004h")   # arm bracketed paste = the readiness signal
buf = b""
end = time.time() + 8
while time.time() < end and b"\r" not in buf:
    try:
        buf += os.read(0, 65536)
    except OSError:
        break
os.write(1, b"GOT[" + buf + b"]")
"""


def _fake_ws(collected):
    class FakeWS:
        async def receive(self):
            await asyncio.sleep(10)
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            collected.append(b)

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    return FakeWS()


def test_webterm_injects_seed_as_bracketed_paste_after_readiness(tmp_path, monkeypatch):
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_SETTLE_S", 0.05)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "SEED-BODY-42", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", _CHILD],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    blob = b"".join(out)
    got = blob[blob.find(b"GOT[") :]
    # The child received one bracketed paste with the exact seed, then the CR submit.
    assert b"\x1b[200~SEED-BODY-42\x1b[201~" in got
    assert b"\r" in got
    assert handoff.claim_seed(key) is None  # consumed exactly once


def test_webterm_seed_times_out_fail_safe_when_tui_never_arms_paste(tmp_path, monkeypatch):
    # A TUI that never arms bracketed paste gets NO injection (unseeded beats spraying raw
    # bytes into a half-booted TUI) — and the unconsumed seed survives for the next attach.
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.02)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 0.2)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "NEVER-SENT", cwd="/tmp")
    key = handoff.commit(h)["id"]
    out: list[bytes] = []
    asyncio.run(
        webterm.run(
            _fake_ws(out),
            [sys.executable, "-c", "import time; time.sleep(0.6)"],
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    assert b"NEVER-SENT" not in b"".join(out)
    assert handoff.has_pending_seed(key) is True  # not consumed — next attach can inject


def test_injector_cancelled_before_readiness_leaves_seed_pending(tmp_path, monkeypatch):
    # PR #701 review P2 (pre-redemption half): every await in the injector sits BEFORE
    # redemption, so a viewer that disconnects while the TUI is still booting cancels the
    # injector WITHOUT consuming the seed — the next attach injects it.
    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_POLL_S", 0.05)
    monkeypatch.setattr(webterm, "_SEED_READY_TIMEOUT_S", 30.0)  # far beyond the ws lifetime
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "STILL-PENDING", cwd="/tmp")
    key = handoff.commit(h)["id"]

    class DroppingWS:
        async def receive(self):
            await asyncio.sleep(0.2)  # viewer vanishes while the TUI is still booting
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, b):
            pass

        async def send_text(self, t):
            pass

        async def close(self, code=None):
            pass

    asyncio.run(
        webterm.run(
            DroppingWS(),
            [sys.executable, "-c", "import time; time.sleep(5)"],  # never arms 2004
            cwd=str(tmp_path),
            buf_key=key,
            seed_key=key,
        )
    )
    assert handoff.has_pending_seed(key) is True


def test_deliver_seed_full_delivery_acks_consumed(tmp_home):
    # Happy path of the claim/ack delivery: one full paste+CR write, seed consumed exactly
    # once, a second delivery attempt finds nothing to claim.
    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "ATOMIC-SEED", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    try:
        assert webterm._deliver_seed(w, key, None) is True
        assert os.read(r, 65536) == b"\x1b[200~ATOMIC-SEED\x1b[201~\r"
        assert webterm._deliver_seed(w, key, None) is False  # single delivery held
    finally:
        os.close(r)
        os.close(w)


def test_deliver_seed_zero_write_failure_leaves_seed_for_retry(tmp_home):
    # Round-2 P1b (clean-failure half): a delivery that wrote NOTHING acks "retry" — the
    # seed survives for the next attach instead of being consumed by a dead fd.
    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "RETRYABLE", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    os.close(r)
    os.close(w)  # both ends gone → the first os.write raises before any byte lands
    assert webterm._deliver_seed(w, key, None) is False
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "RETRYABLE"  # claim was released for the retry


def test_deliver_seed_partial_write_aborts_the_claim_explicitly(tmp_home):
    # Round-2 P1b (partial half): a write that landed SOME bytes must not silently
    # half-lose the seed OR blindly retry into a polluted prompt — it acks "abort"
    # (consumed, logged), and no further claim is possible.
    import fcntl
    import threading

    from agent_sessions import webterm

    seed = "P" * 8000  # > the shrunken pipe capacity below → the write blocks mid-way
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ: capacity 4096 < len(paste)
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(webterm._deliver_seed(w, key, None)))
    t.start()
    time.sleep(0.3)  # the writer has filled the pipe and is blocked mid-payload
    os.close(r)  # reader vanishes → the blocked write raises EPIPE with bytes already sent
    t.join(timeout=10)
    os.close(w)
    assert result == [False]
    assert handoff.has_pending_seed(key) is False  # aborted — consumed, never blind-retried
    assert handoff.claim_seed(key) is None


def test_seed_delivery_backpressure_does_not_stall_the_event_loop(tmp_home):
    # Round-2 P1a: the blocking PTY write runs in a worker thread, so a target that stops
    # draining input stalls only that thread — the event loop keeps ticking. The pipe is
    # shrunk below the payload size so the write genuinely blocks until the reader drains.
    import fcntl

    from agent_sessions import webterm

    seed = "B" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ

    async def main() -> None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, webterm._deliver_seed, w, key, None)
        ticks = 0
        drained = b""
        while not fut.done():
            await asyncio.sleep(0.01)  # the loop is alive while the write is blocked
            ticks += 1
            if ticks > 5:  # let it sit blocked a few ticks first, then drain
                drained += os.read(r, 4096)
            assert ticks < 1000, "delivery never completed"
        assert await fut is True
        # Drain the remainder and check the payload ends with the submitting CR.
        while not drained.endswith(b"\x1b[201~\r"):
            drained += os.read(r, 65536)
        assert ticks > 5  # the loop demonstrably ran while the writer was blocked

    try:
        asyncio.run(main())
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is False  # delivered → consumed


def test_wedged_delivery_times_out_and_settles_the_claim(tmp_home, monkeypatch):
    # Round-3 P1: a target that keeps the PTY open but never drains input can't pin the
    # delivery worker forever — the write deadline fires and the claim settles (abort,
    # since bytes already landed in the pipe).
    import fcntl

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.3)
    seed = "W" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ: payload > capacity, and nobody ever drains
    try:
        start = time.monotonic()
        assert webterm._deliver_seed(w, key, None) is False
        assert time.monotonic() - start < 5  # bounded, not wedged
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is False  # partial → aborted explicitly
    assert handoff.claim_seed(key) is None


def test_wedged_delivery_with_zero_bytes_leaves_seed_for_retry(tmp_home, monkeypatch):
    # Round-3 P1 (zero-byte half): the pipe is ALREADY full before the first chunk, so the
    # deadline fires with nothing written → retry-ack, seed survives for the next attach.
    import fcntl

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.3)
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "UNWRITTEN", cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)
    os.write(w, b"j" * 4096)  # pre-fill to capacity — no room for even one chunk
    try:
        assert webterm._deliver_seed(w, key, None) is False
    finally:
        os.close(r)
        os.close(w)
    assert handoff.has_pending_seed(key) is True
    assert handoff.claim_seed(key) == "UNWRITTEN"


def test_delivery_stays_bounded_with_a_concurrent_writer(tmp_home, monkeypatch):
    # Round-4 P1: select-writability is only a snapshot — a concurrent writer (pump_in in
    # production) can consume the window before the delivery write. Every writer to the
    # PTY is therefore serialized through the bridge's write_lock, and the writability
    # re-check happens UNDER that lock — the deadline holds however aggressively the
    # (lock-respecting, like pump_in) competitor floods the fd, and the claim settles.
    import contextlib
    import fcntl
    import threading

    from agent_sessions import webterm

    monkeypatch.setattr(webterm, "_SEED_WRITE_TIMEOUT_S", 0.5)
    seed = "C" * 8000
    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", seed, cwd="/tmp")
    key = handoff.commit(h)["id"]
    r, w = os.pipe()
    fcntl.fcntl(w, 1031, 4096)  # F_SETPIPE_SZ
    stop = threading.Event()
    write_lock = threading.Lock()  # shared with the competitor, exactly like pump_in

    def competitor() -> None:
        # Flood the same pipe under the shared lock, grabbing capacity whenever possible
        # (a non-blocking second fd so the competitor itself can never wedge the test).
        w2 = os.dup(w)
        os.set_blocking(w2, False)
        try:
            while not stop.is_set():
                with write_lock, contextlib.suppress(BlockingIOError, OSError):
                    os.write(w2, b"z" * 2048)
                time.sleep(0.001)
        finally:
            os.close(w2)

    t = threading.Thread(target=competitor)
    t.start()
    try:
        start = time.monotonic()
        webterm._deliver_seed(w, key, None, write_lock)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        t.join(timeout=10)
        os.close(r)
        os.close(w)
    assert elapsed < 5  # the deadline held despite the competing writer
    # The claim is settled either way (delivered, retried-pending, or aborted) — never
    # stuck "claimed": a fresh claim attempt must not dead-lock on a stale claim.
    assert handoff.claim_seed(key) in (None, seed)


def test_saturated_pool_cancellation_reclaims_the_queued_fd(tmp_home):
    # Round-4 P2: a delivery cancelled while still QUEUED behind a saturated pool never
    # runs its finally-close — the wrapper must reclaim + close the dup'd fd, and the
    # never-claimed seed must survive for the next attach.
    import threading

    from agent_sessions import webterm

    h = handoff.create_handle("claude:" + _SRC, "claude", "quick", "QUEUED", cwd="/tmp")
    key = handoff.commit(h)["id"]
    gate = threading.Event()
    pool = webterm._seed_executor()
    blockers = [pool.submit(gate.wait, 30) for _ in range(webterm._SEED_MAX_DELIVERY_WORKERS)]
    r, w = os.pipe()
    fd = os.dup(w)

    async def main() -> None:
        task = asyncio.ensure_future(webterm._deliver_seed_via_pool(fd, key, None))
        await asyncio.sleep(0.2)  # the job sits queued behind the saturated pool
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(main())
    finally:
        gate.set()
        for b in blockers:
            b.result(timeout=10)
        os.close(r)
        os.close(w)
    with pytest.raises(OSError):
        os.fstat(fd)  # the dup was reclaimed and closed — no leak
    assert handoff.has_pending_seed(key) is True  # never claimed — next attach delivers


def test_seed_deliveries_run_on_a_dedicated_bounded_pool():
    # Round-3 P1: deliveries must never occupy the event loop's SHARED default executor
    # (pump_out's PTY reads live there) — they get their own small pool.
    import threading

    from agent_sessions import webterm

    pool = webterm._seed_executor()
    assert pool is webterm._seed_executor()  # one shared instance
    assert pool._max_workers == webterm._SEED_MAX_DELIVERY_WORKERS
    name = pool.submit(lambda: threading.current_thread().name).result(timeout=10)
    assert name.startswith("handoff-seed")


def test_spawn_watch_retries_failed_publication_in_production_path(tmp_home, monkeypatch):
    # Round-3 P2: the retry vehicle is the spawn watch ITSELF (armed once per target —
    # reconnects can't re-arm), so a transient sidecar-write failure heals without any
    # manual state-machine poke. Driven through the real watcher coroutine.
    from agent_sessions import ptybridge, relaunch
    from agent_sessions.routes import terminal as terminal_routes

    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    native = key.partition(":")[2]
    monkeypatch.setattr(relaunch, "_INSTANT_EXIT_S", 0.01)
    monkeypatch.setattr(terminal_routes, "_PUBLISH_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(ptybridge, "session_exists", lambda e, n: True)
    real_patch = metadata.patch
    calls = {"n": 0}

    def flaky(k, **fields):
        calls["n"] += 1
        if calls["n"] == 2:  # the backlink write fails once, transiently
            raise OSError("disk full")
        return real_patch(k, **fields)

    monkeypatch.setattr(metadata, "patch", flaky)
    asyncio.run(terminal_routes._handoff_spawn_watch("claude", native))
    assert metadata.get(key).handoff_from == src
    assert metadata.get(src).handoff_to == key  # the watch's retry healed the backlink
    assert calls["n"] == 3  # 1 target + 1 failed backlink + 1 retried backlink, no dupes


def test_provenance_publication_is_retryable_after_a_failed_sidecar_write(tmp_home, monkeypatch):
    # Round-2 P2: publication flags are set only after each sidecar patch succeeds — a
    # failed backlink write leaves retryable state, and a later mark_spawned performs
    # exactly the missing write (never a duplicate of the succeeded one).
    src = "claude:" + _SRC
    h = handoff.create_handle(src, "claude", "quick", "seed", cwd="/tmp")
    key = handoff.commit(h)["id"]
    real_patch = metadata.patch
    calls = {"n": 0}

    def flaky(k, **fields):
        calls["n"] += 1
        if calls["n"] == 2:  # the source-backlink write fails once
            raise OSError("disk full")
        return real_patch(k, **fields)

    monkeypatch.setattr(metadata, "patch", flaky)
    with pytest.raises(OSError):
        handoff.mark_spawned(key)
    assert metadata.get(key).handoff_from == src  # target side landed
    assert metadata.get(src).handoff_to == ""  # backlink didn't — but stays retryable
    handoff.mark_spawned(key)  # retry publishes ONLY the missing backlink
    assert metadata.get(src).handoff_to == key
    assert calls["n"] == 3  # 1 target + 1 failed backlink + 1 retried backlink


# ---- rows carry provenance ----------------------------------------------------------------------


def test_session_rows_carry_handoff_provenance(auth_cfg, fake_jsonl):
    src = f"claude:{_SRC}"
    tgt = "claude:22222222-2222-2222-2222-222222222222"
    metadata.patch(tgt, handoff_from=src, handoff_mode="quick", handoff_at="2026-07-16T00:00:00")
    metadata.patch(src, handoff_to=tgt)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert rows[tgt]["handoff_from"] == src
    assert rows[src]["handoff_to"] == tgt
    assert rows[src]["handoff_from"] == ""  # unset stays an empty string, never null/missing


def test_stale_handoff_link_is_tolerated(auth_cfg, fake_jsonl):
    # A backlink whose peer no longer exists must not break the list (read-time tolerance).
    src = f"claude:{_SRC}"
    metadata.patch(src, handoff_to="codex:deadbeef-dead-dead-dead-deaddeadbeef")
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/sessions?limit=50")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["sessions"]}
    assert rows[src]["handoff_to"].startswith("codex:")
