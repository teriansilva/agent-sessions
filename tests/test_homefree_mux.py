"""Mux (Home Free full-app tunnel, #579 P1) — framing, concurrency, backpressure,
reset, and the cross-impl wire vectors the TS side also asserts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_sessions.homefree import mux as M
from agent_sessions.homefree.mux import (
    DATA,
    END,
    OPEN,
    RESET,
    WINDOW,
    Mux,
    StreamReset,
    _decode,
    _encode,
)

VECTORS = Path(__file__).resolve().parents[1] / "web" / "src" / "homefree" / "mux.vectors.json"

# Deterministic frames pinning the wire format across Python + TypeScript.
_SAMPLES = [
    ("open", 1, OPEN, b"GET /api/sessions"),
    ("data", 1, DATA, b"hello \x00\xff world"),
    ("end", 3, END, b""),
    ("reset", 5, RESET, bytes([7])),
    ("window", 2, WINDOW, (65536).to_bytes(4, "big")),
    ("data-empty", 4, DATA, b""),
    ("big-id", 0x01020304, OPEN, b"ws /ws/term/claude:abc"),
]


def _run(coro):
    return asyncio.run(coro)


def _link():
    """Wire two muxes so each one's frames feed straight into the other.

    ``feed()`` never synchronously emits (emits only come from app read/write),
    so direct delivery is safe and fully deterministic — no queues, no timing.
    """
    opened: dict[str, list] = {"a": [], "b": []}
    mux: dict[str, Mux] = {}
    mux["a"] = Mux(
        is_initiator=True,
        on_send=lambda f: mux["b"].feed(f),
        on_stream=lambda s: opened["a"].append(s),
    )
    mux["b"] = Mux(
        is_initiator=False,
        on_send=lambda f: mux["a"].feed(f),
        on_stream=lambda s: opened["b"].append(s),
    )
    return mux["a"], mux["b"], opened


# ── framing ───────────────────────────────────────────────────────────
def test_frame_roundtrip():
    for _, sid, ftype, payload in _SAMPLES:
        sid2, t2, p2 = _decode(_encode(sid, ftype, payload))
        assert (sid2, t2, p2) == (sid, ftype, payload)


def test_truncated_or_overlong_frame_rejected():
    with pytest.raises(ValueError):
        _decode(b"\x00\x00\x00\x01")  # header only, no length bytes
    with pytest.raises(ValueError):
        _decode(_encode(1, DATA, b"abcd")[:-1])  # missing a payload byte
    with pytest.raises(ValueError):
        _decode(_encode(1, DATA, b"abcd") + b"z")  # trailing byte past declared length


def test_malformed_window_resets_stream():
    """A WINDOW frame whose payload isn't exactly 4 bytes must abort the stream —
    not crash and not apply a bogus (possibly huge) credit."""

    async def go():
        a, _b, _opened = _link()
        for bad in (b"\x00\x00", b"\x00\x00\x00\x00\x00\x00"):  # short, overlong
            s = a.open(b"w")
            a.feed(_encode(s.id, WINDOW, bad))
            with pytest.raises(StreamReset):
                await s.read()

    _run(go())


def test_wire_vectors_match():
    """Python _encode must match the committed cross-impl vectors verbatim."""
    if not VECTORS.exists():
        pytest.skip("vectors not generated yet")
    data = json.loads(VECTORS.read_text())
    for v in data["frames"]:
        frame = _encode(v["stream_id"], v["type"], bytes.fromhex(v["payload_hex"]))
        assert frame.hex() == v["frame_hex"], v["name"]
        sid, t, p = _decode(bytes.fromhex(v["frame_hex"]))
        assert [sid, t, p.hex()] == [v["stream_id"], v["type"], v["payload_hex"]]


# ── behaviour ─────────────────────────────────────────────────────────
def test_stream_roundtrip_and_eof():
    async def go():
        a, b, opened = _link()
        s = a.open(b"GET /api/x")
        await s.write(b"chunk-one")
        await s.write(b"chunk-two")
        await s.end()
        assert len(opened["b"]) == 1
        rs = opened["b"][0]
        assert rs.open_info == b"GET /api/x"
        got = b""
        while True:
            part = await rs.read()
            if part == b"":
                break
            got += part
        assert got == b"chunk-onechunk-two"

    _run(go())


def test_concurrent_streams_independent():
    async def go():
        a, b, opened = _link()
        s1, s2 = a.open(b"one"), a.open(b"two")
        await s1.write(b"AAA")
        await s2.write(b"BBB")
        await s1.write(b"aaa")
        assert {o.open_info for o in opened["b"]} == {b"one", b"two"}
        by = {o.open_info: o for o in opened["b"]}
        assert await by[b"one"].read(1024) == b"AAAaaa"
        assert await by[b"two"].read(1024) == b"BBB"

    _run(go())


def test_backpressure_blocks_until_read(monkeypatch):
    async def go():
        monkeypatch.setattr(M, "INITIAL_WINDOW", 4096)
        monkeypatch.setattr(M, "MAX_CHUNK", 1024)
        a, b, opened = _link()
        s = a.open(b"bp")
        rs = opened["b"][0]
        # write 3x the window with nobody reading → must block once it drains
        writer = asyncio.ensure_future(s.write(b"x" * (4096 * 3)))
        await asyncio.sleep(0)  # let it push until the window is exhausted
        assert not writer.done(), "write should block once the peer window drains"
        # drain the receiver → each read replenishes the window → writer finishes
        total = 0
        while total < 4096 * 3:
            total += len(await rs.read(8192))
        await asyncio.wait_for(writer, timeout=1)
        assert writer.done() and total == 4096 * 3

    _run(go())


def test_reset_tears_down_one_stream_only():
    async def go():
        a, b, opened = _link()
        s1, s2 = a.open(b"keep"), a.open(b"kill")
        await s1.write(b"alive")
        by = {o.open_info: o for o in opened["b"]}
        s2.reset(9)
        # the reset stream raises on read; the sibling still delivers
        with pytest.raises(StreamReset):
            await by[b"kill"].read()
        assert await by[b"keep"].read(1024) == b"alive"

    _run(go())
