"""Unit tests for session_stream.SessionStream + SessionRegistry (#183).

The stream's drain reads from a PTY; for testability we drive a small subprocess
that writes to its slave fd instead of a real dtach (``ptybridge.attach_argv``
monkeypatched). The registry's discovery uses ``ptybridge.list_sessions`` — we
override that too so the tests don't touch real sockets on disk.

The codebase doesn't depend on pytest-asyncio; tests drive coroutines via
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_sessions import engines as engines_mod
from agent_sessions import session_stream, webterm


def _set_argv(monkeypatch, argv_for_key):
    """Override ptybridge.attach_argv so SessionStream spawns a known producer
    instead of a real dtach. The fake "session" just runs the shell command the
    test supplied."""

    def fake(*, engine: str, session_id: str) -> list[str]:
        return argv_for_key(engine, session_id)

    monkeypatch.setattr(session_stream.ptybridge, "attach_argv", fake)


def _patch_physical_key(monkeypatch, mapping=None):
    """No-alias by default. The opencode placeholder→real test passes a mapping."""
    mapping = mapping or {}
    monkeypatch.setattr(engines_mod, "physical_key", lambda k, aliases=None: mapping.get(k, k))


# ---- SessionStream ------------------------------------------------------------


def test_stream_drains_bytes_into_buffer_ring(monkeypatch):
    marker = b"server-owned-reader-marker-9b4f1c"
    _set_argv(
        monkeypatch,
        lambda engine, sid: ["/bin/sh", "-c", f"printf %s '{marker.decode()}'"],
    )
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()
    # This test is about the drain → ring + working stamp, not the #195 post-attach
    # replay grace (covered in test_webterm). Neutralize the grace so the immediately
    # emitted marker stamps the working signal as before.
    monkeypatch.setattr(webterm.scrollback, "_ATTACH_REPLAY_GRACE_S", 0.0)

    async def run() -> None:
        s = session_stream.SessionStream(engine="claude", session_id="abc")
        await s.start()
        await asyncio.wait_for(s.ended.wait(), timeout=5.0)
        assert marker in bytes(webterm._BUFFERS["claude:abc"])
        assert webterm.get_last_output_at("claude:abc") is not None
        await s.stop()

    asyncio.run(run())


def test_stream_fans_out_to_subscribers(monkeypatch):
    marker = b"fanout-marker-3e2a"
    _set_argv(
        monkeypatch,
        lambda engine, sid: ["/bin/sh", "-c", f"sleep 0.1; printf %s '{marker.decode()}'"],
    )
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()

    async def collect_until(q, want, deadline):
        buf = b""
        while time.time() < deadline:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=deadline - time.time())
            except TimeoutError:
                break
            buf += chunk
            if want in buf:
                return buf
        return buf

    async def run() -> None:
        s = session_stream.SessionStream(engine="claude", session_id="bcast")
        await s.start()
        q1 = s.subscribe()
        q2 = s.subscribe()
        deadline = time.time() + 5
        seen1 = await collect_until(q1, marker, deadline)
        seen2 = await collect_until(q2, marker, deadline)
        assert marker in seen1
        assert marker in seen2
        await asyncio.wait_for(s.ended.wait(), timeout=5.0)
        await s.stop()

    asyncio.run(run())


def test_stream_start_failure_marks_ended(monkeypatch):
    _set_argv(monkeypatch, lambda e, s: ["/this/path/does/not/exist/nowhere"])

    async def run() -> None:
        s = session_stream.SessionStream(engine="claude", session_id="nope")
        await s.start()
        assert s.ended.is_set()

    asyncio.run(run())


# ---- SessionRegistry ----------------------------------------------------------


def test_registry_discover_spawns_streams_for_each_live(monkeypatch):
    monkeypatch.setattr(
        session_stream.ptybridge,
        "list_sessions",
        lambda: [("claude", "a"), ("claude", "b"), ("opencode", "c")],
    )
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "printf .; sleep 5"])
    _patch_physical_key(monkeypatch)
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()
    # Not exercising the #195 replay grace here — neutralize it so the streams' first
    # bytes stamp last_output_at (the discovery assertion below).
    monkeypatch.setattr(webterm.scrollback, "_ATTACH_REPLAY_GRACE_S", 0.0)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        await reg.discover()
        for _ in range(50):
            if all(webterm.get_last_output_at(k) is not None for k in reg.keys()):
                break
            await asyncio.sleep(0.05)
        snap = reg.snapshot()
        ids = {row["id"] for row in snap}
        assert ids == {"claude:a", "claude:b", "opencode:c"}
        # All discovered sessions start as headless (attached=False).
        assert all(row["attached"] is False for row in snap)
        assert all(row["last_output_at"] is not None for row in snap)
        await reg.stop_all()

    asyncio.run(run())


def test_natural_end_closes_master_fd_and_reaps_subprocess(monkeypatch):
    """Hermes #185: when a headless stream's dtach exits on its own (EOF on
    the drain loop), ``_watch_end`` must close the PTY master fd and reap the
    subprocess — not just remove the entry from the registry. Without this the
    long-running app leaks an fd per ended session.
    """
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    # Producer that prints + exits fast — exercises the EOF/natural-end path.
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "printf .; sleep 0.05"])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        q = reg.subscribe_state()

        await reg._ensure_headless("claude", "x")
        added = await asyncio.wait_for(q.get(), timeout=5.0)
        assert added["t"] == "added"

        # Capture references to the underlying resources BEFORE the natural end
        # so we can assert they're cleaned up.
        entry = reg.get("claude:x")
        assert entry is not None and entry["stream"] is not None
        stream = entry["stream"]
        master_fd = stream._master
        proc = stream._proc
        assert master_fd is not None
        assert proc is not None

        # Wait for the producer to exit + the natural-end watcher to clean up.
        removed = await asyncio.wait_for(q.get(), timeout=5.0)
        assert removed == {"t": "removed", "session_id": "claude:x"}

        # The registry no longer references the entry; AND the stream itself
        # has released its master fd + subprocess handle (idempotent stop()
        # ran from _watch_end on the natural-end path).
        assert reg.get("claude:x") is None
        assert stream._master is None
        assert stream._proc is None
        # The subprocess has been reaped (returncode set).
        assert proc.returncode is not None

        await reg.stop_all()

    asyncio.run(run())


def test_registry_emits_added_then_removed_when_headless_stream_ends(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "printf .; sleep 0.05"])
    _patch_physical_key(monkeypatch)
    webterm._BUFFERS.clear()

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        q = reg.subscribe_state()
        await reg._ensure_headless("claude", "x")
        added = await asyncio.wait_for(q.get(), timeout=5.0)
        assert added["t"] == "added"
        assert added["session"]["id"] == "claude:x"
        # Producer exits → watcher emits "removed" (entry isn't attached).
        removed = await asyncio.wait_for(q.get(), timeout=5.0)
        assert removed == {"t": "removed", "session_id": "claude:x"}
        await reg.stop_all()

    asyncio.run(run())


def test_on_attach_then_on_detach_handoff(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "printf .; sleep 30"])
    monkeypatch.setattr(session_stream.ptybridge, "session_exists", lambda *_a, **_kw: True)
    _patch_physical_key(monkeypatch)
    webterm._BUFFERS.clear()
    webterm._LAST_OUTPUT_AT.clear()

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        q = reg.subscribe_state()

        # Headless: registry owns a stream.
        await reg._ensure_headless("claude", "x")
        added = await asyncio.wait_for(q.get(), timeout=5.0)
        assert added["t"] == "added"
        assert added["session"]["attached"] is False
        first_stream = reg.get("claude:x")["stream"]
        assert first_stream is not None

        # Browser attaches → registry stops the stream, emits "updated".
        await reg.on_attach("claude", "x")
        updated = await asyncio.wait_for(q.get(), timeout=5.0)
        assert updated["t"] == "updated"
        assert updated["session"]["attached"] is True
        assert reg.get("claude:x")["stream"] is None
        assert first_stream.ended.is_set()

        # Browser detaches → registry spawns a fresh stream.
        await reg.on_detach("claude", "x")
        updated2 = await asyncio.wait_for(q.get(), timeout=5.0)
        assert updated2["t"] == "updated"
        assert updated2["session"]["attached"] is False
        second_stream = reg.get("claude:x")["stream"]
        assert second_stream is not None
        assert second_stream is not first_stream

        await reg.stop_all()

    asyncio.run(run())


def test_on_detach_drops_entry_when_master_gone(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "sleep 5"])
    monkeypatch.setattr(session_stream.ptybridge, "session_exists", lambda *_a, **_kw: False)
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        q = reg.subscribe_state()
        # Simulate a browser-only attach (no prior headless stream).
        await reg.on_attach("claude", "x")
        added = await asyncio.wait_for(q.get(), timeout=5.0)
        assert added["t"] == "added"
        await reg.on_detach("claude", "x")
        removed = await asyncio.wait_for(q.get(), timeout=5.0)
        assert removed == {"t": "removed", "session_id": "claude:x"}
        await reg.stop_all()

    asyncio.run(run())


def test_handoff_uses_physical_key_for_opencode_alias(monkeypatch):
    # An attach by the LOGICAL real id (``ses_…``) must land on the same
    # registry entry as the LAUNCH that opened the PLACEHOLDER (#127 alias).
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "sleep 30"])
    monkeypatch.setattr(session_stream.ptybridge, "session_exists", lambda *_a, **_kw: True)
    _patch_physical_key(
        monkeypatch,
        {"opencode:ses_real": "opencode:new-placeholder-12345"},
    )

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        # Launch path uses the placeholder.
        await reg.on_attach("opencode", "new-placeholder-12345")
        assert "opencode:new-placeholder-12345" in reg.keys()
        # A subsequent attach by the REAL id resolves to the same physical key.
        await reg.on_attach("opencode", "ses_real")
        assert "opencode:ses_real" not in reg.keys()
        # Only the one placeholder entry exists.
        assert len(reg.snapshot()) == 1
        await reg.stop_all()

    asyncio.run(run())


def test_snapshot_working_flag_decays(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "printf .; sleep 5"])
    _patch_physical_key(monkeypatch)
    # The decay assertion needs the first byte to stamp working; neutralize the #195
    # post-attach replay grace (covered separately in test_webterm).
    monkeypatch.setattr(webterm.scrollback, "_ATTACH_REPLAY_GRACE_S", 0.0)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        await reg._ensure_headless("claude", "x")
        for _ in range(50):
            if webterm.get_last_output_at("claude:x") is not None:
                break
            await asyncio.sleep(0.05)
        assert reg.snapshot()[0]["working"] is True
        webterm._LAST_OUTPUT_AT["claude:x"] = time.time() - session_stream._WORKING_WINDOW_S - 1
        assert reg.snapshot()[0]["working"] is False
        await reg.stop_all()

    asyncio.run(run())


# ---- Per-tab claim lease (#184) ----------------------------------------------


def test_claim_first_caller_becomes_owner(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    monkeypatch.setattr(session_stream.ptybridge, "session_exists", lambda *_a, **_kw: True)
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        role, claim = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        assert role == "owner"
        assert claim is not None
        assert claim.fp == "fpA" and claim.tab_id == "tabA"

    asyncio.run(run())


def test_claim_second_caller_becomes_secondary(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        owner_role, _ = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        assert owner_role == "owner"
        second_role, second_claim = await reg.claim("claude", "x", fp="fpB", tab_id="tabB")
        assert second_role == "secondary"
        assert second_claim is None

    asyncio.run(run())


def test_force_takeover_demotes_prior_owner(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        _, prior = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        assert prior is not None
        # Caller B forces takeover → returns "owner"; prior owner's demoted event fires.
        role, new_claim = await reg.claim("claude", "x", fp="fpB", tab_id="tabB", force=True)
        assert role == "owner"
        assert new_claim is not None and new_claim.matches("fpB", "tabB")
        assert prior.demoted.is_set()

    asyncio.run(run())


def test_stale_owner_lets_next_claim_in(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        _, prior = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        assert prior is not None
        # Backdate the lease so the next claim sees it as stale.
        prior.last_seen = time.time() - session_stream._CLAIM_LEASE_S - 1
        role, new_claim = await reg.claim("claude", "x", fp="fpB", tab_id="tabB")
        # Stale owner: caller B takes over without needing force=True.
        assert role == "owner"
        assert new_claim is not None and new_claim.matches("fpB", "tabB")
        assert prior.demoted.is_set()

    asyncio.run(run())


def test_registry_ref_counted_attachments(monkeypatch):
    """Verify that multiple viewers are tracked and attached only drops to False
    when the last one detaches (#398)."""
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    monkeypatch.setattr(session_stream.ptybridge, "session_exists", lambda *_a, **_kw: True)
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        key = "claude:abc"
        v1, v2 = object(), object()

        # First viewer attaches
        await reg.on_attach("claude", "abc", viewer_id=v1)
        assert reg.get(key)["attached"] is True

        # Second viewer attaches
        await reg.on_attach("claude", "abc", viewer_id=v2)
        assert reg.get(key)["attached"] is True

        # First viewer detaches -> still attached
        await reg.on_detach("claude", "abc", viewer_id=v1)
        assert reg.get(key)["attached"] is True

        # Second viewer detaches -> now detached
        await reg.on_detach("claude", "abc", viewer_id=v2)
        assert reg.get(key)["attached"] is False

        await reg.stop_all()

    asyncio.run(run())


def test_owner_heartbeat_resets_lease(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        _, owner = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        assert owner is not None
        # Backdate the lease.
        owner.last_seen = time.time() - (session_stream._CLAIM_LEASE_S - 1)
        ok = await reg.refresh("claude", "x", fp="fpA", tab_id="tabA")
        assert ok is True
        # Now a foreign tab still gets secondary because the lease was refreshed.
        role, _ = await reg.claim("claude", "x", fp="fpB", tab_id="tabB")
        assert role == "secondary"

    asyncio.run(run())


def test_release_drops_claim_only_if_caller_matches(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        _, owner = await reg.claim("claude", "x", fp="fpA", tab_id="tabA")
        # A non-matching release is a no-op (e.g. a stale tab sending release
        # AFTER another tab force-took over).
        await reg.release("claude", "x", fp="fpZ", tab_id="tabZ")
        assert reg.get("claude:x")["owner"] is owner
        # Owner releases — claim cleared.
        await reg.release("claude", "x", fp="fpA", tab_id="tabA")
        assert reg.get("claude:x")["owner"] is None

    asyncio.run(run())


def test_empty_fp_or_tab_grants_legacy_attach_without_recording(monkeypatch):
    monkeypatch.setattr(session_stream.ptybridge, "list_sessions", lambda: [])
    _patch_physical_key(monkeypatch)

    async def run() -> None:
        reg = session_stream.SessionRegistry()
        role, claim = await reg.claim("claude", "x", fp="", tab_id="")
        assert role == "owner"
        assert claim is None
        # No claim recorded.
        entry = reg.get("claude:x")
        assert entry is not None and entry.get("owner") is None

    asyncio.run(run())


def test_session_stream_sizes_pty_to_avoid_0x0(monkeypatch):
    # #297: a headless SessionStream must size its reader pty (last-known geometry, else 80x24)
    # BEFORE dtach attaches — an unsized openpty() is 0x0, and dtach (-r winch) would relay that
    # to the live agent, collapsing it to 0x0 (renders into nothing, poisons the byte ring).
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "sleep 5"])
    _patch_physical_key(monkeypatch)
    monkeypatch.setattr(webterm.scrollback, "_ATTACH_REPLAY_GRACE_S", 0.0)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        session_stream.webterm, "_set_winsize", lambda fd, rows, cols: calls.append((rows, cols))
    )

    async def run():
        s = session_stream.SessionStream("claude", "sz")
        await s.start()
        await s.stop()

    asyncio.run(run())
    assert calls, "the reader pty must be sized before dtach attaches"
    assert (24, 80) in calls  # sane default (no last-known geometry for a fresh key) — never 0x0
    assert (0, 0) not in calls


# ---- the headless reader records the geometry its PTY really has (#2056) ----------------------
#
# The recorded size is a claim about the bytes written AFTER it (`scrollback.note_geometry`):
# what the ring already holds keeps the geometry it was marked with, or none.


def _fresh_geometry(monkeypatch):
    """The geometry tables are process-wide and not part of conftest's ring isolation."""
    scrollback = webterm.scrollback
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())
    monkeypatch.setattr(scrollback, "_ATTACH_REPLAY_GRACE_S", 0.0)
    return scrollback


def _restart(scrollback) -> None:
    """A new process: every in-memory table is gone, the disk mirror and its sidecars stay."""
    for table in (
        scrollback._BUFFERS,
        scrollback._TOTALS,
        scrollback._LAST_COLS,
        scrollback._LAST_ROWS,
        scrollback._RING_MIXED,
        scrollback._LOADED_FROM_DISK,
        scrollback._GEOM_MARK,
        scrollback._GEOM_POS,
    ):
        table.clear()


def _viewed(scrollback, key: str, rows: int, cols: int, data: bytes) -> None:
    """A viewer had the session open at ``rows × cols`` (the bookkeeping of `webterm.run`) and
    the agent wrote ``data``."""
    scrollback._LAST_ROWS[key] = rows
    scrollback.note_attach_width(key, cols)
    scrollback.note_geometry(key, rows, cols)
    scrollback._buffer_append(key, data)


async def _drained(key: str, marker: bytes) -> None:
    for _ in range(1000):
        if marker in bytes(webterm._BUFFERS.get(key, b"")):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{marker!r} never reached the ring")


def _refused(scrollback, key: str) -> str:
    with pytest.raises(scrollback.ScreenUnavailable) as exc:
        scrollback.screen_export(key)
    return exc.value.reason


# What the program under the reader's PTY sees as its size, written through that PTY.
_REPORT_SIZE = ["/bin/sh", "-c", "printf 'size=%s.' \"$(stty size)\"; sleep 30"]


def test_a_session_never_opened_in_a_browser_has_a_known_geometry_and_a_frame(monkeypatch):
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: _REPORT_SIZE)
    key = "claude:never-opened"

    async def run() -> None:
        s = session_stream.SessionStream("claude", "never-opened")
        assert _refused(scrollback, key) == "no_output"  # before any reader: nothing is known
        await s.start()
        try:
            # Recorded from what the PTY reports, and it is what the program was given.
            assert (scrollback._LAST_ROWS[key], scrollback.ring_cols(key)) == (24, 80)
            assert scrollback._GEOM_MARK[key] == (24, 80, 0)
            await _drained(key, b"size=24 80.")
            export = scrollback.screen_export(key)
            assert (export.rows, export.cols) == (24, 80)
            assert export.grid[0].rstrip() == "size=24 80."
        finally:
            await s.stop()

    asyncio.run(run())
    # Both are persisted claims about the ring, like a viewer's.
    assert scrollback._cols_path(key).read_text() == "80"
    assert scrollback._geom_path(key).read_text() == "24 80 0"


def test_after_a_restart_at_the_same_geometry_the_frame_is_available_at_once(monkeypatch):
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: _REPORT_SIZE)
    key = "claude:restarted"
    _viewed(scrollback, key, 40, 120, b"before the restart\r\n")
    _restart(scrollback)

    async def run() -> None:
        # Only the disk is left, and it says what the retained bytes were drawn at.
        export = scrollback.screen_export(key)
        assert (export.rows, export.cols) == (40, 120)
        s = session_stream.SessionStream("claude", "restarted")
        await s.start()
        try:
            # The reader took the persisted size (not the 24 × 80 default), so the program was
            # not resized and nothing changed: the ring and its mark are as they were.
            await _drained(key, b"size=40 120.")
            assert bytes(scrollback._BUFFERS[key]).startswith(b"before the restart\r\n")
            export = scrollback.screen_export(key)
            assert (export.rows, export.cols) == (40, 120)
            assert [r.rstrip() for r in export.grid[:2]] == ["before the restart", "size=40 120."]
        finally:
            await s.stop()

    asyncio.run(run())


def test_a_reader_at_another_width_than_the_persisted_ring_starts_a_clean_one(monkeypatch):
    """Drawn at 120 × 40, drained at 80 × 24 after the restart: the retained bytes are not
    rendered at the new geometry. The width differs, so the ring's own rule applies (reset), and
    the frame is back as soon as the program has written at the new size."""
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: _REPORT_SIZE)
    key = "claude:other-width"
    _viewed(scrollback, key, 40, 120, b"drawn at 120 columns\r\n")
    _restart(scrollback)
    monkeypatch.setattr(scrollback, "reader_size", lambda key: (24, 80))  # the PTY it really gets

    async def run() -> None:
        s = session_stream.SessionStream("claude", "other-width")
        await s.start()
        try:
            assert (scrollback._LAST_ROWS[key], scrollback.ring_cols(key)) == (24, 80)
            await _drained(key, b"size=24 80.")
            assert b"drawn at 120" not in bytes(scrollback._BUFFERS[key])
            export = scrollback.screen_export(key)
            assert (export.rows, export.cols) == (24, 80)
            assert export.grid[0].rstrip() == "size=24 80."
        finally:
            await s.stop()

    asyncio.run(run())


def test_a_reader_at_another_height_does_not_certify_the_persisted_ring(monkeypatch):
    """Drawn at 80 × 40, drained at 80 × 24 after the restart: same width, so the ring is kept,
    and exactly therefore nothing may be exported while its old bytes are in the rendered tail."""
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: _REPORT_SIZE)
    key = "claude:other-height"
    _viewed(scrollback, key, 40, 80, b"drawn at 40 rows\r\n")
    _restart(scrollback)
    monkeypatch.setattr(scrollback, "reader_size", lambda key: (24, 80))

    async def run() -> None:
        s = session_stream.SessionStream("claude", "other-height")
        await s.start()
        try:
            assert (scrollback._LAST_ROWS[key], scrollback.ring_cols(key)) == (24, 80)
            await _drained(key, b"size=24 80.")
            assert bytes(scrollback._BUFFERS[key]).startswith(b"drawn at 40 rows\r\n")  # kept
            assert _refused(scrollback, key) == "mixed_geometry"
            assert scrollback.live_tail_text(key)  # the forgiving reader is unchanged
        finally:
            await s.stop()
        # The program repaints from a cleared screen (as on a resize): coherent again.
        scrollback._buffer_append(key, b"\x1b[H\x1b[2Jrepainted")
        assert scrollback.screen_export(key).grid[0].rstrip() == "repainted"

    asyncio.run(run())


def test_a_ring_of_unknown_width_is_dropped_by_the_readers_attach_like_by_a_viewers(monkeypatch):
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "sleep 30"])
    key = "claude:unknown-width"
    scrollback._buffer_append(key, b"\x1b[5;1Hwritten at some width\r\n")  # no width on record
    _restart(scrollback)
    assert not scrollback._cols_path(key).exists()

    async def run() -> None:
        s = session_stream.SessionStream("claude", "unknown-width")
        await s.start()
        try:
            # `note_attach_width`: the program re-renders at this width from here on (#245).
            assert not scrollback._BUFFERS[key] and scrollback.ring_cols(key) == 80
            assert _refused(scrollback, key) == "no_output"
        finally:
            await s.stop()

    asyncio.run(run())


def test_a_reader_whose_pty_reports_no_size_records_nothing(monkeypatch):
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: ["/bin/sh", "-c", "sleep 30"])
    monkeypatch.setattr(session_stream.webterm, "_set_winsize", lambda fd, rows, cols: None)
    key = "claude:unsized"

    async def run() -> None:
        s = session_stream.SessionStream("claude", "unsized")
        await s.start()
        await s.stop()

    asyncio.run(run())
    # The PTY reports 0 × 0: what was ASKED for is never recorded in its place.
    assert key not in scrollback._LAST_ROWS and key not in scrollback._LAST_COLS
    assert key not in scrollback._GEOM_MARK


def test_a_later_viewer_attach_or_resize_supersedes_the_readers_geometry(monkeypatch):
    """The viewer bridge's own bookkeeping (`webterm.run`: `_LAST_ROWS`, `note_attach_width` and
    `note_geometry` at attach; `_reset_ring` + `note_cols` on a width change) runs after the
    reader stopped, and its size replaces the reader's."""
    scrollback = _fresh_geometry(monkeypatch)
    _set_argv(monkeypatch, lambda e, s: _REPORT_SIZE)
    key = "claude:then-viewed"

    async def run() -> None:
        s = session_stream.SessionStream("claude", "then-viewed")
        await s.start()
        await _drained(key, b"size=24 80.")
        await s.stop()  # the registry stops the reader before a viewer reads

    asyncio.run(run())
    assert scrollback.screen_export(key).cols == 80

    # A viewer attaches at the same size: a continuation, nothing changes.
    scrollback._LAST_ROWS[key] = 24
    scrollback.note_attach_width(key, 80)
    scrollback.note_geometry(key, 24, 80)
    assert scrollback.screen_export(key).grid[0].rstrip() == "size=24 80."

    # It resizes to another height: the ring is kept (#245 resets on width only), and what the
    # reader's bytes were drawn at is not the geometry any more.
    scrollback._LAST_ROWS[key] = 50
    scrollback.note_geometry(key, 50, 80)
    assert _refused(scrollback, key) == "mixed_geometry"

    # It resizes to another width: the ring is reset and the new size is the geometry.
    scrollback._reset_ring(key)
    scrollback.note_cols(key, 132)
    scrollback._LAST_ROWS[key] = 43
    scrollback.note_geometry(key, 43, 132)
    assert _refused(scrollback, key) == "no_output"  # until the agent repaints at the new size
    scrollback._buffer_append(key, b"repainted")
    export = scrollback.screen_export(key)
    assert (export.rows, export.cols) == (43, 132) and export.grid[0].rstrip() == "repainted"
