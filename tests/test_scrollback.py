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
    scrollback._SANITIZE_CARRY.clear()


def test_codex_live_output_drops_app_clear_scrollback():
    data = b"\x1b[H\x1b[2J\x1b[3Jcodex frame"
    assert scrollback.sanitize_live_output("codex:abc", data) == b"\x1b[H\x1b[2Jcodex frame"


def test_codex_live_output_drops_split_clear_scrollback():
    key = "codex:split"
    assert scrollback.sanitize_live_output(key, b"\x1b[H\x1b[2J\x1b[") == b"\x1b[H\x1b[2J"
    assert scrollback.sanitize_live_output(key, b"3Jcodex frame") == b"codex frame"


def test_non_codex_live_output_keeps_clear_scrollback():
    data = b"\x1b[H\x1b[2J\x1b[3Jclaude frame"
    assert scrollback.sanitize_live_output("claude:abc", data) == data


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


def test_cross_width_attach_does_not_poison_persisted_width():
    # Hermes #360 round 3: a 40-col attach onto a 120-col ring must not stamp 40 onto the old
    # bytes — the ring resets (the #245 policy applied at attach), so a post-restart 40-col
    # have>0 reconnect replays nothing instead of 120-col garble.
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


def test_same_width_attach_keeps_ring_and_claim():
    key = "claude:xwidth-3"
    scrollback.note_cols(key, 80)
    scrollback._buffer_append(key, b"0123456789")
    scrollback.note_attach_width(key, 80)  # same width → nothing destroyed
    _wipe_memory()
    payload, total = scrollback._resume_payload(key, have=4)
    assert payload == b"456789" and total == 10


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
        hydrate opens the file for reading — i.e. while the worker's disk read is 'in flight'.
        (#652 T4 reads the tail via ``open()``/``seek()`` rather than ``read_bytes()``, so the
        injection hooks ``open``; ``state['raced']`` fires it exactly once, so the append's own
        persist-``open`` and the re-entrant hydrate don't re-trigger it.)"""

        def __init__(self, p):
            self._p = p

        def open(self, *args, **kwargs):
            if not state["raced"]:
                state["raced"] = True
                scrollback._buffer_append(key, b"new")
            return self._p.open(*args, **kwargs)

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


# --- First-output AI-review kick (#552) -------------------------------------------------
# A brand-new session's first title/summary must not wait for the periodic review interval:
# the FIRST reviewable output wakes the review loop. Edge-triggered (once per key) so chatty
# output never re-wakes, and suppressed during the post-attach replay burst.


def test_first_output_kicks_ai_review_once(monkeypatch):
    key = "claude:first-out-1"
    scrollback._LAST_OUTPUT_AT.pop(key, None)
    kicks = []
    monkeypatch.setattr(scrollback, "_kick_review_on_first_output", lambda: kicks.append(1))
    scrollback._buffer_append(key, b"hello")  # first output → one kick
    scrollback._buffer_append(key, b" world")  # more output → no re-kick (edge)
    assert kicks == [1]
    assert key in scrollback._LAST_OUTPUT_AT


def test_no_review_kick_during_attach_replay_grace(monkeypatch):
    # The dtach replay burst right after an attach is not new agent activity (#195): it must
    # not stamp the working signal, and so must not fire a premature review kick either.
    key = "claude:grace-1"
    scrollback._LAST_OUTPUT_AT.pop(key, None)
    kicks = []
    monkeypatch.setattr(scrollback, "_kick_review_on_first_output", lambda: kicks.append(1))
    scrollback.note_attach(key)  # opens the suppress window
    scrollback._buffer_append(key, b"replay burst")  # inside grace → no stamp, no kick
    assert kicks == []
    assert key not in scrollback._LAST_OUTPUT_AT


def test_kick_helper_wakes_the_review_loop(monkeypatch):
    from agent_sessions import ai_review_loop

    called = []
    monkeypatch.setattr(ai_review_loop, "request_review_soon", lambda: called.append(1))
    scrollback._kick_review_on_first_output()
    assert called == [1]


def test_strip_fallback_does_not_leak_control_string_payloads():
    """Hermes on PR #618: the escape-strip fallback shares the renderer's blind spot. An
    unterminated OSC (a tail slice cuts one routinely) left the ESC to be eaten as a stray C0
    and the payload — OSC 52 is the clipboard — rendered as visible text."""
    assert "SECRET" not in scrollback._stripped_tail_text(b"ok \x1b]52;c;SECRET", 200)
    assert "secret" not in scrollback._stripped_tail_text(b"ok \x1bPsecret\x1b\\", 200)
    assert scrollback._stripped_tail_text(b"ok \x1b]0;title\x07done", 200) == "ok done"


# ---- Hermes on PR #618: a tail slice that BEGINS inside a control string -------------------


def _leak_ring(payload_terminator: bytes) -> bytes:
    return b"pre \x1b]52;c;" + b"SECRET" * 40 + payload_terminator + b"after\r\n"


def test_tail_slice_beginning_inside_a_control_string_never_leaks_its_payload(monkeypatch):
    """The introducer sits BEHIND the review's tail slice, so the parser handed only the slice
    cannot know the bytes are an OSC 52 (clipboard) payload rather than screen text. Only the
    accessor holds the whole ring, so only it can look back and drop the stranded fragment."""
    monkeypatch.setattr(scrollback, "_SCREEN_TAIL_BYTES", 64)
    for name, terminator in (
        ("bel", b"\x07"),
        ("st", b"\x1b\\"),
        ("abandoned", b"\x1b[1m"),  # agent never terminated it; a new sequence begins
    ):
        key = f"claude:leak-{name}"
        scrollback._BUFFERS[key] = bytearray(_leak_ring(terminator))
        out = scrollback.live_tail_text(key, 200)
        assert "SECRET" not in out, name
        assert "after" in out, name


def test_head_cut_leak_is_sealed_on_the_rendered_path_too(monkeypatch):
    monkeypatch.setattr(scrollback, "_SCREEN_TAIL_BYTES", 64)
    key = "claude:leak-rendered"
    scrollback._BUFFERS[key] = bytearray(_leak_ring(b"\x07"))
    scrollback._LAST_COLS[key] = 80
    scrollback._LAST_ROWS[key] = 24
    out = scrollback.live_tail_text(key, 200)
    assert "SECRET" not in out
    assert "after" in out


def test_a_slice_that_is_control_payload_end_to_end_yields_nothing(monkeypatch):
    # Better to review nothing than to review a secret; the caller falls back to transcript-only.
    monkeypatch.setattr(scrollback, "_SCREEN_TAIL_BYTES", 64)
    key = "claude:leak-all"
    scrollback._BUFFERS[key] = bytearray(b"pre \x1b]52;c;" + b"SECRET" * 100)
    assert scrollback.live_tail_text(key, 200) == ""


def test_an_ordinary_tail_slice_is_not_trimmed(monkeypatch):
    monkeypatch.setattr(scrollback, "_SCREEN_TAIL_BYTES", 64)
    key = "claude:no-control-string"
    scrollback._BUFFERS[key] = bytearray(b"x" * 200 + b"visible text\r\n")
    assert "visible text" in scrollback.live_tail_text(key, 200)


def test_introducer_straddling_the_cut_is_still_detected():
    # ESC at start-1, `]` at start: the two-byte introducer spans the boundary.
    ring = b"a\x1b]52;c;SECRET\x07ok"
    start = ring.index(b"]")
    assert scrollback.vtscreen.starts_inside_control_string(ring, start)
    assert b"SECRET" not in scrollback.vtscreen.drop_open_control_prefix(ring[start:])


def test_starts_inside_control_string_is_false_once_terminated():
    ring = b"\x1b]0;title\x07 plain text here"
    assert not scrollback.vtscreen.starts_inside_control_string(ring, len(ring) - 5)
    assert not scrollback.vtscreen.starts_inside_control_string(b"no escapes at all", 5)
    assert not scrollback.vtscreen.starts_inside_control_string(b"anything", 0)


# --- #652 T1: amortized ring trim -------------------------------------------------------


def test_ring_trim_is_amortized_overshoot_then_snaps_back(monkeypatch):
    # The ring tolerates an overshoot of up to _MAX_BUF // _RING_TRIM_DIVISOR before trimming,
    # then drops back to EXACTLY _MAX_BUF — so the O(cap) memmove runs once per slack bytes,
    # not on every chunk. Offsets stay consistent throughout (readers use the actual length).
    monkeypatch.setattr(scrollback, "_MAX_BUF", 100)  # slack = 100 // 8 = 12 → trim past 112
    key = "claude:trim"
    scrollback._buffer_append(key, b"a" * 100)
    assert len(scrollback._BUFFERS[key]) == 100  # at cap, not over → no trim

    scrollback._buffer_append(key, b"b" * 10)  # 110 ≤ 112 → overshoot tolerated
    assert len(scrollback._BUFFERS[key]) == 110
    # ring_start is derived from the ACTUAL length, so resume offsets are still consistent.
    _, total = scrollback._resume_payload(key, 0)
    assert total == 110

    scrollback._buffer_append(key, b"c" * 5)  # 115 > 112 → trim back to the cap
    buf = scrollback._BUFFERS[key]
    assert len(buf) == 100
    assert buf[-1:] == b"c"
    assert scrollback._TOTALS[key] == 115  # monotonic byte count unaffected by trimming


# --- #652 T6: _scan_modes fast-path -----------------------------------------------------


def test_scan_modes_fast_path_skips_plain_chunks_but_still_tracks_and_carries():
    scrollback._MODES.clear()
    scrollback._MODE_CARRY.clear()
    # Plain chunk, no ESC and no carry → early return, no _MODES entry materialized.
    scrollback._scan_modes("k", b"just some plain output, no escapes")
    assert "k" not in scrollback._MODES
    # A real DECSET is still tracked (mouse mode 1000).
    scrollback._scan_modes("k", b"\x1b[?1000h")
    assert scrollback._MODES.get("k") == {1000}
    # A sequence split across chunks: the carry means the 2nd (ESC-free) chunk is NOT skipped.
    scrollback._scan_modes("k2", b"\x1b[?10")  # partial → carried
    assert scrollback._MODE_CARRY.get("k2")
    scrollback._scan_modes("k2", b"00h")  # completes despite having no ESC of its own
    assert scrollback._MODES.get("k2") == {1000}
    assert "k2" not in scrollback._MODE_CARRY  # carry consumed


# --- #652 T2/T4: bounded tail read ------------------------------------------------------


def test_read_file_tail_returns_bounded_tail(tmp_path):
    p = tmp_path / "mirror.bin"
    p.write_bytes(bytes(range(256)) * 10)  # 2560 bytes
    # n < size → exactly the last n bytes, without loading the whole file.
    assert scrollback._read_file_tail(p, 100) == (bytes(range(256)) * 10)[-100:]
    # n >= size → the whole file.
    assert scrollback._read_file_tail(p, 10_000) == bytes(range(256)) * 10


def test_hydrate_reads_only_tail_of_oversized_mirror(monkeypatch):
    # A mirror larger than _MAX_BUF hydrates to exactly the last _MAX_BUF bytes (T4),
    # via the bounded tail read rather than loading the whole file.
    monkeypatch.setattr(scrollback, "_MAX_BUF", 50)
    key = "claude:big"
    path = scrollback._scrollback_path(key)
    scrollback._ensure_scrollback_dir()
    path.write_bytes(b"x" * 30 + b"y" * 80)  # 110 bytes on disk, cap is 50
    scrollback._LOADED_FROM_DISK.discard(key)
    scrollback._ensure_loaded(key)
    assert bytes(scrollback._BUFFERS[key]) == b"y" * 50  # last 50 bytes only
