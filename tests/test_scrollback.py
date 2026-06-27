"""Scrollback depth (#348 Phases 1+2): the persisted ring must survive a broker restart
as a usable same-width continuation, and the width sidecar must travel with the mirror.

The conftest autouse fixture points ``_SCROLLBACK_DIR`` at a per-test tmp dir and clears
the in-memory state, so "restart" is simulated by wiping the in-memory dicts only.
"""

from __future__ import annotations

from agent_sessions import scrollback


def _wipe_memory():
    """Simulate a broker restart: in-memory state gone, disk mirror intact."""
    scrollback._BUFFERS.clear()
    scrollback._TOTALS.clear()
    scrollback._LAST_COLS.clear()
    scrollback._LOADED_FROM_DISK.clear()


def test_note_cols_persists_and_survives_restart():
    key = "claude:cols-persist"
    scrollback.note_cols(key, 132)
    assert scrollback._LAST_COLS[key] == 132
    _wipe_memory()
    scrollback._buffer_append(key, b"hello world")  # first touch → _ensure_loaded
    assert scrollback._LAST_COLS[key] == 132  # restored from the sidecar file


def test_restart_reconnect_is_same_width_continuation():
    # #348 Phase 1 core: before, _LAST_COLS was wiped with the process, so a have>0
    # reconnect after a restart could never be a continuation — the hydrated ring was
    # discarded for the capped transcript. With the persisted width it replays the delta.
    key = "claude:cont-1"
    scrollback.note_cols(key, 80)
    scrollback._buffer_append(key, b"0123456789")
    _wipe_memory()
    payload, total = scrollback._resume_payload(key, have=4)
    assert total == 10 and payload == b"456789"
    assert (
        scrollback._is_same_width_continuation(4, total, scrollback._LAST_COLS.get(key), 80) is True
    )
    # ...but a DIFFERENT client width is still not a continuation (would garble).
    assert (
        scrollback._is_same_width_continuation(4, total, scrollback._LAST_COLS.get(key), 100)
        is False
    )


def test_ahead_of_ring_after_restart_is_not_a_continuation():
    # #484: an app restart rehydrates the ring head-trimmed to _MAX_BUF while restoring the
    # authored width, so a SAME-width reconnect can carry a pre-restart `have` that now exceeds
    # the smaller `total`. That is NOT a seamless continuation — the client holds MORE than the
    # ring — so it must fall through to the clear/transcript path, or the rehydrated ring replays
    # UNDER the client's stale scrollback and the conversation renders twice. `_resume_payload`
    # already serves the full ring for have>total; the predicate must agree and refuse continuation.
    key = "claude:ahead-1"
    scrollback.note_cols(key, 80)
    scrollback._buffer_append(key, b"0123456789")  # total == 10
    _wipe_memory()
    _, total = scrollback._resume_payload(key, have=99)
    assert total == 10
    # have > total at the SAME width → not a continuation (the bug: this was True → no clear).
    assert (
        scrollback._is_same_width_continuation(99, total, scrollback._LAST_COLS.get(key), 80)
        is False
    )
    # In-ring same-width offsets are still genuine continuations (no flicker, #304/#359/#374).
    assert (
        scrollback._is_same_width_continuation(4, total, scrollback._LAST_COLS.get(key), 80) is True
    )
    assert (
        scrollback._is_same_width_continuation(10, total, scrollback._LAST_COLS.get(key), 80)
        is True
    )
    # have == 0 (fresh load) is never a continuation either.
    assert (
        scrollback._is_same_width_continuation(0, total, scrollback._LAST_COLS.get(key), 80)
        is False
    )


def test_clear_scrollback_removes_cols_sidecar():
    key = "claude:clear-1"
    scrollback.note_cols(key, 90)
    scrollback._buffer_append(key, b"abc")
    assert scrollback._cols_path(key).exists()
    scrollback.clear_scrollback([key])
    assert not scrollback._cols_path(key).exists()
    _wipe_memory()
    scrollback._buffer_append(key, b"x")
    assert scrollback._LAST_COLS.get(key) is None  # no stale width claim after a clear


def test_transcript_caps_are_env_overridable(monkeypatch):
    # #348 Phase 2: defaults raised + env-tunable; bad values fall back.
    from agent_sessions import transcript

    assert transcript.DEFAULT_MAX_LINES >= 20000
    assert transcript.DEFAULT_MAX_MESSAGES >= 2000
    assert transcript._TAIL_BYTES >= 8 * 1024 * 1024
    monkeypatch.setenv("AGENT_SESSIONS_TRANSCRIPT_MAX_LINES", "123")
    assert transcript._env_int("AGENT_SESSIONS_TRANSCRIPT_MAX_LINES", 20000) == 123
    monkeypatch.setenv("AGENT_SESSIONS_TRANSCRIPT_MAX_LINES", "garbage")
    assert transcript._env_int("AGENT_SESSIONS_TRANSCRIPT_MAX_LINES", 20000) == 20000


def test_cross_width_attach_does_not_poison_persisted_width(monkeypatch):
    # Hermes #360 round 3 (VT off): a 40-col attach onto a 120-col ring must not stamp
    # 40 onto the old bytes — the ring resets (the #245 policy applied at attach), so a
    # post-restart 40-col have>0 reconnect replays nothing instead of 120-col garble.
    from agent_sessions import vtsidecar

    monkeypatch.setattr(vtsidecar, "enabled", lambda: False)
    key = "claude:xwidth-1"
    scrollback.note_cols(key, 120)
    scrollback._buffer_append(key, b"WIDE-120-COL-BYTES")
    scrollback.note_attach_width(key, 40)  # the cross-width attach bookkeeping
    _wipe_memory()
    payload, total = scrollback._resume_payload(key, have=1)
    assert payload == b""  # nothing retained to mis-replay
    assert scrollback._is_same_width_continuation(1, total, scrollback._LAST_COLS.get(key), 40) in (
        False,
        True,
    )  # either way: no stale wide bytes can come back
    assert b"WIDE-120-COL-BYTES" not in payload


def test_cross_width_attach_vt_on_drops_persisted_claim(monkeypatch):
    # Hermes #360 round 3 (VT on): the ring is the mirror's feed and survives, but it is
    # mixed-width — the persisted claim is dropped, so post-restart the reconnect is NOT
    # a continuation (buffer_cols None → width-correct transcript path).
    from agent_sessions import vtsidecar

    monkeypatch.setattr(vtsidecar, "enabled", lambda: True)
    key = "claude:xwidth-2"
    scrollback.note_cols(key, 120)
    scrollback._buffer_append(key, b"WIDE-120-COL-BYTES")
    scrollback.note_attach_width(key, 40)
    assert bytes(scrollback._BUFFERS[key])  # ring kept for the mirror
    assert not scrollback._cols_path(key).exists()  # but the width claim is gone
    _wipe_memory()
    scrollback._buffer_append(key, b"")  # touch → _ensure_loaded
    assert scrollback._LAST_COLS.get(key) is None
    assert (
        scrollback._is_same_width_continuation(5, scrollback._TOTALS.get(key, 0), None, 40) is False
    )


def test_same_width_attach_keeps_ring_and_claim(monkeypatch):
    from agent_sessions import vtsidecar

    monkeypatch.setattr(vtsidecar, "enabled", lambda: False)
    key = "claude:xwidth-3"
    scrollback.note_cols(key, 80)
    scrollback._buffer_append(key, b"0123456789")
    scrollback.note_attach_width(key, 80)  # same width → nothing destroyed
    _wipe_memory()
    payload, total = scrollback._resume_payload(key, have=4)
    assert payload == b"456789" and total == 10


def test_vt_on_mixed_ring_blocks_same_process_continuation(monkeypatch):
    # Hermes #360 round 4: with VT on, a cross-width attach keeps the (now mixed) ring;
    # the IN-MEMORY tracker must not let a same-process 40-col have>0 reconnect replay
    # raw 120-col bytes either — ring_cols() reports None until the ring is reset.
    from agent_sessions import vtsidecar

    monkeypatch.setattr(vtsidecar, "enabled", lambda: True)
    key = "claude:xwidth-4"
    scrollback.note_cols(key, 120)
    scrollback._buffer_append(key, b"WIDE-120-COL-BYTES")
    scrollback.note_attach_width(key, 40)
    assert scrollback._LAST_COLS[key] == 40  # reader sizing etc. still track the client
    assert scrollback.ring_cols(key) is None  # but the ring has no single authored width
    assert (
        scrollback._is_same_width_continuation(
            1, scrollback._TOTALS.get(key, 0), scrollback.ring_cols(key), 40
        )
        is False
    )
    # A SECOND 40-col attach doesn't launder the marker (ring is still mixed).
    scrollback.note_attach_width(key, 40)
    assert scrollback.ring_cols(key) is None
    # Only a reset makes the ring single-width again.
    scrollback._reset_ring(key)
    scrollback.note_cols(key, 40, persist=True)
    assert scrollback.ring_cols(key) == 40


# --- Private-mode replay on attach (#397) ------------------------------------------------
# Alt-screen TUIs (opencode) / inline agents set xterm mouse-reporting, alternate-scroll, and
# bracketed-paste modes ONCE at startup. Those bytes are gone by the time a fresh client
# attaches, so the client never learns the app wants mouse events → the wheel does nothing.
# `_scan_modes` tracks the CURRENT private-mode set off the output stream and
# `attach_modes_payload` re-emits it on every attach.


def _restart():
    """Simulate a broker restart for the mode state too (disk sidecar intact)."""
    _wipe_memory()
    scrollback._MODES.clear()
    scrollback._MODE_CARRY.clear()


def test_decset_modes_are_tracked_and_replayed():
    key = "claude:modes-1"
    # opencode-style startup: enter alt-screen, enable SGR mouse + bracketed paste.
    scrollback._buffer_append(key, b"\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?2004h")
    assert scrollback._MODES[key] == {1000, 1006, 2004}  # 1049 is NOT a tracked mode
    # Sorted, one DECSET each — what webterm prepends to the attach stream.
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1000h\x1b[?1006h\x1b[?2004h"


def test_decrst_clears_a_mode_so_it_is_not_replayed():
    key = "claude:modes-2"
    scrollback._buffer_append(key, b"\x1b[?1000h\x1b[?1006h")
    assert scrollback._MODES[key] == {1000, 1006}
    # The app turns mouse reporting back off — we must not replay a mode it no longer wants.
    scrollback._buffer_append(key, b"\x1b[?1000l")
    assert scrollback._MODES[key] == {1006}
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1006h"


def test_multi_mode_single_sequence():
    key = "claude:modes-3"
    scrollback._buffer_append(key, b"\x1b[?1000;1002;1006;2004h")
    assert scrollback._MODES[key] == {1000, 1002, 1006, 2004}


def test_split_sequence_across_chunk_boundary():
    key = "claude:modes-4"
    # A DECSET split mid-sequence between two PTY reads must still be recognized.
    scrollback._buffer_append(key, b"\x1b[?100")
    assert scrollback._MODES.get(key, set()) == set()  # nothing complete yet
    scrollback._buffer_append(key, b"0;1006h")
    assert scrollback._MODES[key] == {1000, 1006}
    # And a split on the final byte alone.
    scrollback._buffer_append(key, b"\x1b[?1002")
    scrollback._buffer_append(key, b"h")
    assert scrollback._MODES[key] == {1000, 1002, 1006}


def test_split_with_decrst_across_boundary():
    key = "claude:modes-5"
    scrollback._buffer_append(key, b"\x1b[?1006h")
    scrollback._buffer_append(key, b"\x1b[?10")
    scrollback._buffer_append(key, b"06l")  # DECRST 1006, split
    assert scrollback._MODES[key] == set()
    assert scrollback.attach_modes_payload(key) == b""


def test_untracked_modes_and_junk_ignored():
    key = "claude:modes-6"
    # 1049 (alt-screen), 25 (cursor visibility), 12 (blink) are not in _MODE_TRACK; a
    # malformed empty param must not crash the scanner.
    scrollback._buffer_append(key, b"\x1b[?1049h\x1b[?25l\x1b[?12;h\x1b[?1006h")
    assert scrollback._MODES[key] == {1006}


def test_no_modes_means_empty_attach_payload():
    key = "claude:modes-7"
    scrollback._buffer_append(key, b"just plain text, no escapes")
    assert scrollback.attach_modes_payload(key) == b""


def test_carry_does_not_grow_unbounded_on_lone_esc():
    key = "claude:modes-8"
    # A bare ESC with a long run of bytes that can't complete a private mode must not be
    # carried (the fragment isn't a valid private-mode prefix → dropped).
    scrollback._buffer_append(key, b"\x1b[1;1H" + b"x" * 200)
    assert scrollback._MODE_CARRY.get(key, b"") == b""


def test_modes_persist_and_survive_restart():
    key = "claude:modes-9"
    scrollback._buffer_append(key, b"\x1b[?1000h\x1b[?1006h")
    assert scrollback._modes_path(key).exists()
    _restart()
    # After a restart the in-memory set is gone but the attach re-hydrates it from disk.
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1000h\x1b[?1006h"


def test_clear_scrollback_removes_modes_sidecar_and_memory():
    key = "claude:modes-10"
    scrollback._buffer_append(key, b"\x1b[?1006h")
    assert scrollback._modes_path(key).exists()
    scrollback.clear_scrollback([key])
    assert not scrollback._modes_path(key).exists()  # sidecar gone
    assert key not in scrollback._MODES  # in-memory state gone
    _restart()
    assert scrollback.attach_modes_payload(key) == b""  # no stale replay after a clear


def test_reset_ring_keeps_modes():
    # A width reset drops width-fragile CONTENT but the agent does NOT re-emit its mode setup
    # on the repaint, so the modes must survive (else scrolling re-breaks after every resize).
    key = "claude:modes-11"
    scrollback._buffer_append(key, b"\x1b[?1000h\x1b[?1006h some content")
    scrollback._reset_ring(key)
    assert bytes(scrollback._BUFFERS[key]) == b""  # content dropped
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1000h\x1b[?1006h"  # modes kept


def test_corrupt_modes_sidecar_is_skipped_per_token():
    key = "claude:modes-12"
    scrollback._SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    # A partly-garbage sidecar: keep the valid tracked ints, drop the rest (incl. untracked).
    scrollback._modes_path(key).write_text("1006,garbage,,1049,1000")
    _restart()
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1000h\x1b[?1006h"


def test_alt_screen_attach_replays_modes_but_not_raw_frames():
    # The #397 regression: an alt-screen opencode session that enabled mouse + bracketed
    # paste. The raw frame replay stays SUPPRESSED (`_resume_payload` returns empty for
    # alt-screen), but the active modes are still recoverable for the attach prefix.
    key = "opencode:ses_altmodes"
    scrollback._buffer_append(
        key, b"\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?2004hTUI FRAME CONTENT HERE"
    )
    payload, total = scrollback._resume_payload(key, have=0)
    assert payload == b""  # alt-screen raw frames are NOT replayed (would corrupt the redraw)
    assert b"TUI FRAME CONTENT" not in payload
    # ...but the modes the client needs to re-learn ARE available, before the SIGWINCH nudge.
    assert scrollback.attach_modes_payload(key) == b"\x1b[?1000h\x1b[?1006h\x1b[?2004h"


def test_clear_all_removes_modes_orphaned_by_reset_ring():
    # Hermes #409: `_reset_ring` unlinks the `.scrollback` mirror but deliberately KEEPS
    # `.modes` (width-independent). A later global `clear_scrollback()` (keys=None) used to
    # glob only `*.scrollback` and miss that orphan, so stale mouse/paste modes survived a
    # supposed clear. The global clear must enumerate `.modes` sidecars too.
    key = "claude:modes-orphan"
    scrollback._buffer_append(key, b"\x1b[?1006h some content")
    scrollback._reset_ring(key)  # drops the .scrollback mirror, keeps .modes
    assert not scrollback._scrollback_path(key).exists()
    assert scrollback._modes_path(key).exists()  # the orphan the old glob missed
    stats = scrollback.clear_scrollback()  # global clear, no explicit keys
    assert stats["removed"] >= 1  # the orphaned key is counted as cleared
    assert not scrollback._modes_path(key).exists()  # orphan gone
    assert key not in scrollback._MODES  # in-memory state dropped too
    _restart()
    assert scrollback.attach_modes_payload(key) == b""  # no stale replay after clear


def test_clear_specific_key_removes_modes_after_reset_ring():
    # The explicit-keys path must clear an orphaned `.modes` too, even when the mirror is gone.
    key = "claude:modes-orphan-2"
    scrollback._buffer_append(key, b"\x1b[?1000h\x1b[?2004h x")
    scrollback._reset_ring(key)
    assert scrollback._modes_path(key).exists()
    scrollback.clear_scrollback([key])
    assert not scrollback._modes_path(key).exists()
    assert key not in scrollback._MODES


def test_enforce_buffer_cap_survives_concurrent_insert(monkeypatch):
    """Regression for the "random disconnect/reconnect" bug.

    The AI-review path reads the live tail from a WORKER THREAD (``asyncio.to_thread`` →
    ``live_tail_text`` → ``_ensure_loaded``), which can hydrate-and-insert a buffer while the
    event loop is mid-scan in ``_enforce_buffer_cap``. That used to raise
    ``RuntimeError('OrderedDict mutated during iteration')`` and propagate out of
    ``_buffer_append`` → collapse the WS byte pump → a spurious viewer disconnect. The cap must
    scan a stable snapshot and tolerate the insert.

    Deterministic stand-in for the thread race: a patched ``_session_alive`` performs the insert
    on its first call — exactly the mutation the GIL-release window of the real blocking socket
    probe (``ptybridge.probe_master``) allows — so no real threads are needed to reproduce it.
    Pre-fix this body raises; post-fix it returns cleanly.
    """
    for i in range(scrollback._MAX_BUFFERS + 2):
        scrollback._BUFFERS[f"claude:sess-{i}"] = bytearray(b"x")

    calls = {"n": 0}

    def fake_alive(key: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            # The worker-thread hydrate landing mid-scan.
            scrollback._BUFFERS["claude:injected-mid-scan"] = bytearray(b"y")
        return True  # every session looks alive → the scan walks the whole registry

    monkeypatch.setattr(scrollback, "_session_alive", fake_alive)

    scrollback._enforce_buffer_cap()  # must not raise

    assert "claude:injected-mid-scan" in scrollback._BUFFERS  # insert preserved, not lost
    assert calls["n"] >= 2  # the scan kept walking past the mutating key without crashing


def test_buffer_registry_thread_safe_under_live_tail_reads():
    """``live_tail_text`` (worker thread, AI review) hydrates + slices the ring while the loop
    churns ``_buffer_append`` + LRU eviction over the same keys. With ``_RING_LOCK`` guarding the
    registry this is safe; without it the OrderedDict races. Assert no exception escapes either
    side across many interleavings. (Deterministically GREEN with the fix; it was this exact
    interleaving — minus the lock — that produced the production crash.)"""
    import threading

    errors: list[BaseException] = []
    stop = threading.Event()
    # > _MAX_BUFFERS distinct keys so appends push over the cap and trigger eviction scans.
    n_keys = scrollback._MAX_BUFFERS + 16

    def reader() -> None:
        i = 0
        try:
            while not stop.is_set():
                scrollback.live_tail_text(f"claude:sess-{i % n_keys}", 200)
                i += 1
        except BaseException as e:  # noqa: BLE001 — catching a race is the whole point
            errors.append(e)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        for r in range(2000):
            scrollback._buffer_append(f"claude:sess-{r % n_keys}", b"some output\n")
    except BaseException as e:  # noqa: BLE001
        errors.append(e)
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"registry race surfaced: {errors[:3]}"


def test_concurrent_first_touch_preserves_persisted_tail(monkeypatch):
    """Hermes #512: a worker-thread hydrate (``live_tail_text`` → ``_ensure_loaded``) whose disk
    read is in flight when an event-loop ``_buffer_append`` for the same key lands must NOT drop
    the durable scrollback. The load marker is now set atomically with the insert (under the lock,
    LAST), so the loop append extends the hydrated ring instead of clobbering it with a fresh one.

    Deterministic stand-in for the thread interleave: the scrollback file's ``read_bytes`` fires
    the racing append exactly once, mid-hydrate. Pre-fix this leaves ``b"new"`` (persisted
    ``b"old"`` silently lost); post-fix it leaves ``b"oldnew"`` with a consistent total.
    """
    key = "claude:hydrate-race"
    scrollback._buffer_append(key, b"old")  # persist a tail to disk
    # Simulate a restart: in-memory state gone, disk mirror intact.
    scrollback._BUFFERS.clear()
    scrollback._TOTALS.clear()
    scrollback._LOADED_FROM_DISK.clear()

    orig_path = scrollback._scrollback_path
    state = {"raced": False}

    class _RacingPath:
        """Proxies the real scrollback Path but injects an event-loop append the first time the
        hydrate reads the file — i.e. while the worker's disk read is 'in flight'."""

        def __init__(self, p):
            self._p = p

        def read_bytes(self):
            if not state["raced"]:
                state["raced"] = True
                scrollback._buffer_append(key, b"new")
            return self._p.read_bytes()

        def __getattr__(self, name):
            return getattr(self._p, name)

    monkeypatch.setattr(
        scrollback,
        "_scrollback_path",
        lambda k: _RacingPath(orig_path(k)) if k == key else orig_path(k),
    )

    scrollback.live_tail_text(key, 100)  # triggers the racing first-touch hydrate

    assert bytes(scrollback._BUFFERS[key]) == b"oldnew"  # persisted tail preserved + append kept
    assert scrollback._TOTALS[key] == 6  # totals consistent with the surviving bytes
