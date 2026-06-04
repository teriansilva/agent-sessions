"""VT sidecar client — flag-gating + fail-safe behavior (#273 sub-step 2).

The load-bearing guarantee: with AGENT_SESSIONS_VT_SCROLLBACK off (the default + prod), every entry
point is a no-op / returns None, so behavior is byte-identical to today and the attach path stays on
the transcript. With the flag on but no sidecar reachable, calls fail soft (None) so the caller
falls back. No Node sidecar is spawned here — these pin the Python side.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib

from agent_sessions import scrollback, vtsidecar


async def _close_server(server):
    # asyncio.Server.wait_closed() regressed in CPython 3.12.0–3.12.3 (CI runs 3.12.3) and can hang
    # forever; bound it so a test can never wedge the suite. The loop teardown reclaims the rest.
    server.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.wait_closed(), 2)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_VT_SCROLLBACK", raising=False)
    assert vtsidecar.enabled() is False

    async def go():
        assert await vtsidecar.snapshot("claude:x", 80, 40) is None
        assert await vtsidecar.feed("claude:x", b"hi") is False
        assert await vtsidecar.health() is None
        await vtsidecar.ensure_started()  # no spawn
        await vtsidecar.end("claude:x")  # no-op

    asyncio.run(go())


def test_enabled_flag_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")
    assert vtsidecar.enabled() is True
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "0")
    assert vtsidecar.enabled() is False


def test_note_session_end_is_safe_without_loop_or_flag(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_VT_SCROLLBACK", raising=False)
    vtsidecar.note_session_end("claude:x")  # disabled + no running loop → must not raise


def test_drop_buffer_invokes_the_teardown_hook(monkeypatch):
    # The new hook in scrollback._drop_buffer always calls vtsidecar.note_session_end (which itself
    # gates on the flag). This pins that the wiring is present without needing a sidecar.
    calls = []
    monkeypatch.setattr(vtsidecar, "note_session_end", lambda k: calls.append(k))
    scrollback._BUFFERS["claude:drop"] = bytearray(b"x")
    scrollback._TOTALS["claude:drop"] = 1
    scrollback._drop_buffer("claude:drop")
    assert calls == ["claude:drop"]
    assert "claude:drop" not in scrollback._BUFFERS  # original behavior preserved


def test_enabled_but_no_bundle_fails_soft(monkeypatch):
    # Flag on, but the sidecar bundle path doesn't exist → ensure_started spawns nothing and
    # snapshot returns None (no socket), so the caller falls back to the transcript. Never raises.
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_JS", "/nonexistent/vt-sidecar/server.mjs")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_SOCK", "/nonexistent/dir/vt.sock")

    async def go():
        await vtsidecar.ensure_started()  # bundle missing → no spawn, no raise
        assert await vtsidecar.snapshot("claude:x", 50, 40) is None  # no socket → fallback
        assert await vtsidecar.feed("claude:x", b"hi") is False

    asyncio.run(go())


def _seed_ring(key, data=b"some ring bytes"):
    scrollback._LOADED_FROM_DISK.add(key)  # skip disk hydrate
    scrollback._BUFFERS[key] = bytearray(data)
    scrollback._TOTALS[key] = len(data)


def test_vt_snapshot_payload_uses_sidecar_when_enabled(monkeypatch):
    # Attach helper (#273): flag on + a ring + a warm mirror → snapshot the LIVE mirror; the result
    # is framed as scroll-up (clear + rows). This is what supersedes the transcript on the attach.
    monkeypatch.setattr(vtsidecar, "enabled", lambda: True)

    async def fake_live_snapshot(key, cols, rows):
        assert key == "claude:vt" and cols == 50
        return b"FAITHFUL\r\nROWS"

    monkeypatch.setattr(vtsidecar, "live_snapshot", fake_live_snapshot)
    _seed_ring("claude:vt")
    try:

        async def go():
            out = await scrollback._vt_snapshot_payload("claude:vt", 50, 40)
            assert out is not None
            assert out.startswith(scrollback._CLEAN_LOAD_CLEAR)
            assert b"FAITHFUL\r\nROWS" in out

        asyncio.run(go())
    finally:
        scrollback._BUFFERS.clear()
        scrollback._TOTALS.clear()


def test_vt_snapshot_payload_none_when_mirror_cold(monkeypatch):
    # Cold mirror (session never opened this process / just deployed): live_snapshot returns None →
    # _vt_snapshot_payload returns None so the caller falls back to the CLEAN transcript, never the
    # dup-prone ring replay. This is the safety property of the live-mirror switch (#273).
    monkeypatch.setattr(vtsidecar, "enabled", lambda: True)

    async def cold(key, cols, rows):
        return None

    monkeypatch.setattr(vtsidecar, "live_snapshot", cold)
    _seed_ring("claude:cold")
    try:

        async def go():
            assert await scrollback._vt_snapshot_payload("claude:cold", 50, 40) is None

        asyncio.run(go())
    finally:
        scrollback._BUFFERS.clear()
        scrollback._TOTALS.clear()


def test_live_mirror_feeds_are_ordered_and_gated(monkeypatch, tmp_path):
    # The mirror's correctness hinges on feeds reaching the sidecar strictly IN ORDER (a reorder
    # corrupts the emulator) and ONLY for sessions a client has opened (note_resize). Drive a real
    # unix socket that records the op stream and assert: open precedes feeds, feeds arrive in
    # submission order, and a feed for an un-opened key is dropped.
    import json as _json

    sock = str(tmp_path / "vt.sock")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_SOCK", sock)
    received: list[tuple] = []

    async def go():
        async def handle(reader, writer):
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = _json.loads(line)
                data = req.get("data")
                payload = base64.b64decode(data).decode() if data else None
                received.append((req["op"], req.get("key"), payload))
                writer.write((_json.dumps({"id": req["id"], "ok": True}) + "\n").encode())
                await writer.drain()

        server = await asyncio.start_unix_server(handle, path=sock)
        client = vtsidecar._Sidecar()
        try:
            client.note_resize("claude:m", 80, 24)  # open
            for i in range(5):
                client.note_feed("claude:m", f"chunk{i}".encode())
            client.note_feed("claude:ungated", b"DROP")  # never opened → must be dropped
            await asyncio.wait_for(client._mirror_q.join(), 5)
        finally:
            await client.stop()
            await _close_server(server)

    asyncio.run(go())
    assert received[0] == ("open", "claude:m", None), "open precedes feeds"
    feeds = [r for r in received if r[0] == "feed"]
    assert [r[2] for r in feeds] == [f"chunk{i}" for i in range(5)], "feeds arrive in order"
    assert all(r[1] != "claude:ungated" for r in received), "un-opened session's feed dropped"


def test_width_change_resets_the_mirror_height_change_does_not(monkeypatch, tmp_path):
    # #293 garble-proofing: a WIDTH change means the mirror's scrollback was authored at the old
    # width; reflowing absolute-positioned TUI to a new width garbles. So a width change must DROP
    # the emulator (end) and rebuild single-width (open) — never reflow mixed-width content. A
    # height-only change (mobile address bar) keeps the buffer and just reopens.
    import json as _json

    sock = str(tmp_path / "vt.sock")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_SOCK", sock)
    ops: list[tuple] = []

    async def go():
        async def handle(reader, writer):
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = _json.loads(line)
                ops.append((req["op"], req.get("cols")))
                writer.write((_json.dumps({"id": req["id"], "ok": True}) + "\n").encode())
                await writer.drain()

        server = await asyncio.start_unix_server(handle, path=sock)
        client = vtsidecar._Sidecar()
        try:
            client.note_resize("claude:m", 80, 24)  # initial open @80
            client.note_resize("claude:m", 80, 40)  # height-only change → just reopen, no end
            client.note_resize("claude:m", 50, 40)  # WIDTH change → end + reopen @50
            await asyncio.wait_for(client._mirror_q.join(), 5)
        finally:
            await client.stop()
            await _close_server(server)

    asyncio.run(go())
    op_names = [o[0] for o in ops]
    # Exactly ONE end, and it precedes the @50 reopen (the width change), not the height change.
    assert op_names.count("end") == 1, f"width change should reset exactly once: {op_names}"
    end_idx = op_names.index("end")
    assert ops[end_idx + 1] == ("open", 50), "the reset reopens at the NEW width"
    # The height-only change (80→80, 24→40) did NOT trigger an end before its reopen.
    assert ops[:3] == [("open", 80), ("open", 80), ("end", None)], ops[:3]


def test_node_bin_prefers_explicit_path(monkeypatch, tmp_path):
    # The installer records the resolved Node (system OR vendored under $PREFIX/.toolchain) in
    # AGENT_SESSIONS_VT_SIDECAR_NODE so the sidecar runs even when the unit PATH lacks it (#273).
    fake = tmp_path / "node"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_NODE", str(fake))
    assert vtsidecar._node_bin() == str(fake)
    # An absolute path that doesn't exist → None (don't claim a runnable Node we can't find).
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_NODE", "/no/such/node")
    assert vtsidecar._node_bin() is None


def test_dirty_mirror_snapshots_none(monkeypatch):
    # Fail-safe (#273): a desynced (dirty) mirror — a dropped feed, a failed op, or a sidecar
    # restart — must NOT be served as faithful. live_snapshot returns None so the caller falls back
    # to the transcript. Returns early on the dirty check, so no socket is needed.
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")

    async def go():
        client = vtsidecar._Sidecar()
        client._geom["claude:d"] = (80, 24)
        client._dirty.add("claude:d")
        assert await client.live_snapshot("claude:d", 80, 24) is None

    asyncio.run(go())


def test_note_resize_recovers_a_dirty_mirror(monkeypatch):
    # Recovery (#273): reopening a dirty key clears the dirty flag and enqueues end→open, so the
    # stale emulator is dropped and a clean one is rebuilt from the live feed forward. Asserted
    # before any await, so the pump task hasn't drained the queue yet.
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")

    async def go():
        client = vtsidecar._Sidecar()
        client._dirty.add("claude:r")
        client.note_resize("claude:r", 80, 24)
        assert "claude:r" not in client._dirty, "reopen clears dirty"
        assert client._geom["claude:r"] == (80, 24)
        ops = [client._mirror_q.get_nowait()[0] for _ in range(client._mirror_q.qsize())]
        assert ops == ["end", "open"], "drops the stale emulator, then reopens clean"
        await client.stop()

    asyncio.run(go())


def test_dirty_reopen_stays_dirty_when_queue_full(monkeypatch):
    # Fail-safe under overload (Hermes #273): if a dirty-mirror reopen can't enqueue its recovery
    # ops (queue saturated), the key must STAY dirty so live_snapshot returns None — not go
    # non-dirty and then serve the stale emulator as faithful.
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")

    async def go():
        client = vtsidecar._Sidecar()
        client._mirror_q = asyncio.Queue(maxsize=1)  # pre-set so _ensure_mirror_pump keeps it
        client._mirror_q.put_nowait(("filler", {}, None))  # saturate → put_nowait raises QueueFull
        client._dirty.add("claude:s")
        client._geom["claude:s"] = (80, 24)
        client.note_resize("claude:s", 80, 24)  # recovery ops can't enqueue
        assert "claude:s" in client._dirty, "stays dirty when the recovery ops couldn't enqueue"
        assert await client.live_snapshot("claude:s", 80, 24) is None

    asyncio.run(go())


def test_rebuild_reads_response_larger_than_64kb(monkeypatch, tmp_path):
    # Regression (#273): a rebuilt snapshot is one newline-terminated JSON line and easily exceeds
    # asyncio's default 64 KB StreamReader limit. Before sizing the read buffer, readline()
    # overflowed on any real-sized scrollback → the read loop tore down → rebuild() returned None →
    # transcript fallback (VT scroll-up only ever worked for tiny test sessions). Drives a REAL unix
    # socket returning a ~256 KB result and asserts the client reads it whole.
    import json as _json

    sock = str(tmp_path / "vt.sock")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SCROLLBACK", "1")
    monkeypatch.setenv("AGENT_SESSIONS_VT_SIDECAR_SOCK", sock)
    big = "X" * (256 * 1024)  # well past the 64 KB default line limit

    async def go():
        async def handle(reader, writer):
            line = await reader.readline()
            req = _json.loads(line)
            resp = _json.dumps({"id": req["id"], "ok": True, "result": big}) + "\n"
            writer.write(resp.encode())
            await writer.drain()

        server = await asyncio.start_unix_server(handle, path=sock)
        client = vtsidecar._Sidecar()
        try:
            out = await client.rebuild("claude:big", b"ring bytes", 80, 40)
            assert out is not None, "rebuild returned None — large response was not read whole"
            assert out == big.encode()
        finally:
            await client.stop()
            await _close_server(server)

    asyncio.run(go())


def test_vt_snapshot_payload_none_when_disabled_or_empty(monkeypatch):
    monkeypatch.setattr(vtsidecar, "enabled", lambda: True)
    scrollback._LOADED_FROM_DISK.add("claude:empty")  # no ring entry → None (falls back)
    try:

        async def go():
            assert await scrollback._vt_snapshot_payload("claude:empty", 50, 40) is None
            monkeypatch.setattr(vtsidecar, "enabled", lambda: False)
            _seed_ring("claude:off")
            assert await scrollback._vt_snapshot_payload("claude:off", 50, 40) is None  # flag off

        asyncio.run(go())
    finally:
        scrollback._BUFFERS.clear()
        scrollback._TOTALS.clear()
