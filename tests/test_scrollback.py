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
    assert scrollback._is_same_width_continuation(4, scrollback._LAST_COLS.get(key), 80) is True
    # ...but a DIFFERENT client width is still not a continuation (would garble).
    assert scrollback._is_same_width_continuation(4, scrollback._LAST_COLS.get(key), 100) is False


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
    assert scrollback._is_same_width_continuation(1, scrollback._LAST_COLS.get(key), 40) in (
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
    assert scrollback._is_same_width_continuation(5, None, 40) is False


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
    assert scrollback._is_same_width_continuation(1, scrollback.ring_cols(key), 40) is False
    # A SECOND 40-col attach doesn't launder the marker (ring is still mixed).
    scrollback.note_attach_width(key, 40)
    assert scrollback.ring_cols(key) is None
    # Only a reset makes the ring single-width again.
    scrollback._reset_ring(key)
    scrollback.note_cols(key, 40, persist=True)
    assert scrollback.ring_cols(key) == 40
