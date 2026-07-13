"""A tiny stream multiplexer for the Home Free full-app tunnel (#579 P1).

The E2E ``Transport`` (handshake.py) is a single ordered, reliable, encrypted
byte channel. To carry the box's whole app — many concurrent ``/api`` requests
plus the terminal ``/ws`` — over that one channel, we multiplex independent
**streams** through it.

The mux is deliberately **transport-agnostic**: it emits opaque frame bytes via
an ``on_send`` callback and consumes them via :meth:`Mux.feed`. The caller wires
those to ``transport.encrypt`` → relay WebSocket and back, so the relay still
only ever sees ciphertext (no relay change). That also makes the mux testable by
feeding two instances straight into each other.

Wire — one frame is ``stream_id(u32) · type(u8) · len(u24) · payload[len]``:

======  ====  =====================================================
type    code  payload
======  ====  =====================================================
OPEN    1     opaque open-info (caller-defined; e.g. an HTTP request line)
DATA    2     stream bytes (subject to the per-stream send window)
END     3     empty — half-close (no more DATA from this sender)
RESET   4     1-byte error code — abort the stream both directions
WINDOW  5     u32 credit increment — the peer may send this many more bytes
======  ====  =====================================================

Flow control is credit-based per stream: a receiver advertises
``INITIAL_WINDOW`` and only replenishes (via ``WINDOW``) as its consumer reads,
so an un-read stream drains the sender's window and applies real backpressure
instead of buffering without bound. Stream ids follow the HTTP/2 convention —
the initiator opens odd ids, the responder even — so the two sides never collide.
"""

from __future__ import annotations

import asyncio
from collections import deque

OPEN, DATA, END, RESET, WINDOW = 1, 2, 3, 4, 5

INITIAL_WINDOW = 256 * 1024  # per-stream receive credit advertised to the peer
MAX_CHUNK = 16 * 1024  # DATA payload cap — keep frames small for fair interleaving
_U24_MAX = (1 << 24) - 1


def _encode(stream_id: int, ftype: int, payload: bytes = b"") -> bytes:
    if len(payload) > _U24_MAX:
        raise ValueError("mux frame payload too large")
    return stream_id.to_bytes(4, "big") + bytes([ftype]) + len(payload).to_bytes(3, "big") + payload


def _decode(frame: bytes) -> tuple[int, int, bytes]:
    if len(frame) < 8:
        raise ValueError("short mux frame")
    stream_id = int.from_bytes(frame[:4], "big")
    ftype = frame[4]
    length = int.from_bytes(frame[5:8], "big")
    # Require an exact length — reject short AND overlong frames, so trailing or
    # coalesced bytes can't be silently dropped (matches the TS decoder).
    if len(frame) != 8 + length:
        raise ValueError("mux frame length mismatch")
    return stream_id, ftype, frame[8 : 8 + length]


class StreamReset(Exception):
    """Raised on a stream whose peer sent RESET (or the mux was closed)."""

    def __init__(self, code: int = 0) -> None:
        super().__init__(f"stream reset (code {code})")
        self.code = code


class Stream:
    """One logical bidirectional channel over the mux."""

    def __init__(self, mux: Mux, stream_id: int, open_info: bytes = b"") -> None:
        self._mux = mux
        self.id = stream_id
        self.open_info = open_info
        # send side: how many bytes the peer will still accept
        self._send_window = INITIAL_WINDOW
        self._send_waiters: deque[asyncio.Future] = deque()
        self._send_ended = False
        # receive side: buffered bytes waiting for our consumer + EOF/reset state
        self._recv_buf = bytearray()
        self._recv_eof = False
        self._recv_reset: int | None = None
        self._data_ready = asyncio.Event()

    # ── outbound ──────────────────────────────────────────────────────
    async def write(self, data: bytes) -> None:
        """Send bytes, awaiting when the peer's window is exhausted (backpressure)."""
        if self._send_ended:
            raise StreamReset(0)
        view = memoryview(data)
        while view:
            while self._send_window <= 0:
                if self._recv_reset is not None:
                    raise StreamReset(self._recv_reset)
                fut = asyncio.get_event_loop().create_future()
                self._send_waiters.append(fut)
                await fut
            n = min(len(view), self._send_window, MAX_CHUNK)
            self._mux._emit(_encode(self.id, DATA, bytes(view[:n])))
            self._send_window -= n
            view = view[n:]

    async def end(self) -> None:
        """Half-close: no more outbound DATA."""
        if not self._send_ended:
            self._send_ended = True
            self._mux._emit(_encode(self.id, END))

    def reset(self, code: int = 0) -> None:
        """Abort the stream in both directions."""
        self._mux._emit(_encode(self.id, RESET, bytes([code & 0xFF])))
        self._mux._drop(self.id)
        self._fail(code)

    # ── inbound ───────────────────────────────────────────────────────
    async def read(self, max_bytes: int = MAX_CHUNK) -> bytes:
        """Return up to ``max_bytes`` of received data; ``b""`` at clean EOF.

        Reading is what replenishes the peer's send window — so a consumer that
        stops reading applies backpressure rather than letting us buffer forever.
        """
        while not self._recv_buf and not self._recv_eof and self._recv_reset is None:
            self._data_ready.clear()
            await self._data_ready.wait()
        if self._recv_reset is not None and not self._recv_buf:
            raise StreamReset(self._recv_reset)
        if not self._recv_buf:
            return b""  # clean EOF
        n = min(max_bytes, len(self._recv_buf))
        out = bytes(self._recv_buf[:n])
        del self._recv_buf[:n]
        self._mux._emit(_encode(self.id, WINDOW, n.to_bytes(4, "big")))  # replenish
        return out

    # ── mux-internal callbacks ────────────────────────────────────────
    def _on_data(self, payload: bytes) -> None:
        self._recv_buf.extend(payload)
        self._data_ready.set()

    def _on_end(self) -> None:
        self._recv_eof = True
        self._data_ready.set()

    def _on_window(self, credit: int) -> None:
        self._send_window += credit
        while self._send_waiters and self._send_window > 0:
            self._send_waiters.popleft().set_result(None)

    def _fail(self, code: int) -> None:
        self._recv_reset = code
        self._data_ready.set()
        while self._send_waiters:
            w = self._send_waiters.popleft()
            if not w.done():
                w.set_result(None)


class Mux:
    """Multiplexes :class:`Stream` s over one ordered byte channel.

    ``on_send(frame_bytes)`` ships a frame (wire it to ``transport.encrypt`` +
    the relay socket); :meth:`feed` consumes an inbound frame. ``on_stream`` (if
    given) fires when the peer opens a stream.
    """

    def __init__(self, *, is_initiator: bool, on_send, on_stream=None) -> None:
        self._on_send = on_send
        self._on_stream = on_stream
        self._streams: dict[int, Stream] = {}
        self._next_id = 1 if is_initiator else 2
        self._closed = False

    def open(self, open_info: bytes = b"") -> Stream:
        """Open a new outbound stream."""
        sid = self._next_id
        self._next_id += 2
        s = Stream(self, sid, open_info)
        self._streams[sid] = s
        self._emit(_encode(sid, OPEN, open_info))
        return s

    def feed(self, frame: bytes) -> None:
        """Dispatch one inbound frame."""
        if self._closed:
            return
        sid, ftype, payload = _decode(frame)
        if ftype == OPEN:
            s = Stream(self, sid, payload)
            self._streams[sid] = s
            if self._on_stream is not None:
                self._on_stream(s)
            return
        s = self._streams.get(sid)
        if s is None:
            return  # unknown/closed stream — ignore (peer may have raced a RESET)
        if ftype == DATA:
            s._on_data(payload)
        elif ftype == END:
            s._on_end()
        elif ftype == WINDOW:
            # WINDOW carries a u32 credit — a wrong-length payload is a malformed
            # control frame; abort the stream rather than crash or inflate credit.
            if len(payload) != 4:
                s.reset()
            else:
                s._on_window(int.from_bytes(payload, "big"))
        elif ftype == RESET:
            self._drop(sid)
            s._fail(payload[0] if payload else 0)

    def close(self) -> None:
        """Tear down every stream (e.g. the transport dropped)."""
        self._closed = True
        for s in list(self._streams.values()):
            s._fail(0)
        self._streams.clear()

    # internal
    def _emit(self, frame: bytes) -> None:
        if not self._closed:
            self._on_send(frame)

    def _drop(self, sid: int) -> None:
        self._streams.pop(sid, None)
