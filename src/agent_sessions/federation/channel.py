"""The peer channel: preamble, Noise XX, mux, and calls over streams (#1246 §3).

Order on the wire, both transports alike (a direct WebSocket now, the relay leg later):

1. **Preamble**: one clear-text frame, canonical JSON, at most :data:`PREAMBLE_MAX` bytes. Its
   exact bytes are the Noise prologue, so a preamble changed in transit fails the handshake.
2. **Handshake**: three frames (:mod:`noise`). ``peer`` mode runs XX, ``pair`` mode XXpsk3.
3. **Trust check**: the caller's, between :func:`respond` / :func:`initiate` returning and
   :class:`Channel` starting. Nothing in this module sends an application frame before it.
4. **Transport**: one Noise message carries one mux frame. A call is one mux stream: the opener
   sends ``{"verb", "args"}`` as the open-info and half-closes; the answerer writes one JSON
   document and half-closes.

A channel carries **named verbs only** (:class:`Verb`). An unknown verb, a verb outside the
channel's mode, or arguments its validator refuses resets the stream and counts as a violation.
There is no path from here to an HTTP route, a shell or a file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ..homefree import mux as hf_mux
from . import noise

log = logging.getLogger(__name__)

WIRE_VERSION = 1
MODE_PEER = "peer"
MODE_PAIR = "pair"
MODES = (MODE_PEER, MODE_PAIR)

PREAMBLE_MAX = 256
HANDSHAKE_TIMEOUT_S = 10.0
#: The largest mux frame a channel sends or accepts, header included. One mux frame travels in
#: one Noise message, and a Noise message is at most 65535 bytes INCLUDING its 16-byte tag, so
#: 65519 is the hard ceiling. 32 KiB is the explicit cap, well inside it: the mux cuts DATA into
#: 16 KiB chunks, and an OPEN carries at most ``OPEN_INFO_MAX``. Larger answers are chunked.
FRAME_MAX = 32 * 1024
#: The same cap on the wire: one frame plus the AEAD tag.
WIRE_MAX = FRAME_MAX + noise.TAGLEN
OPEN_INFO_MAX = 16 * 1024
ANSWER_MAX = 256 * 1024
CALL_TIMEOUT_S = 20.0
HANDLER_TIMEOUT_S = 30.0
#: Frames waiting for the socket. Flow control bounds DATA; this bounds everything else.
SEND_QUEUE_MAX = 2048

# Stream reset codes (one byte on the wire).
RESET_REFUSED = 1  # unknown verb, wrong mode, bad arguments
RESET_BUSY = 2  # a budget said no
RESET_OVERRUN = 3  # the sender ignored the flow-control window

_MUX_HEADER = 8


class ChannelClosed(Exception):
    """The transport ended, or the channel was closed."""


class HandshakeRefused(Exception):
    """This side will not complete the handshake. ``reason`` is for logs, never for the wire."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CallError(Exception):
    """A call did not produce an ``ok`` answer. ``code`` is a short stable word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VerbError(Exception):
    """Raised by a verb handler to answer ``{"ok": false, "code": ...}``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BadArgs(Exception):
    """Raised by a verb's validator. The stream is reset; the reason stays local."""


class FrameTransport(Protocol):
    """An ordered, reliable carrier of whole binary frames."""

    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Budgets:
    streams: int = 32  # concurrent inbound calls
    calls_per_minute: int = 600
    #: Bytes this side reads from, and answers to, the peer per minute. Reading past it waits
    #: (backpressure, bounded memory); an answer past it is refused as ``busy``.
    bytes_per_minute: int = 64 * 1024 * 1024
    violations: int = 8  # then the channel closes


@dataclass(frozen=True)
class Verb:
    """One callable name. ``validate`` returns the cleaned arguments or raises :class:`BadArgs`;
    ``handle(ctx, args)`` returns a JSON-able dict or raises :class:`VerbError`."""

    name: str
    modes: frozenset[str]
    validate: Callable[[dict], dict]
    handle: Callable[[Any, dict], Awaitable[dict]]


@dataclass
class Established:
    """A finished handshake. The caller decides trust from ``remote_static`` before it builds a
    :class:`Channel`."""

    initiator: bool
    mode: str
    invite: str | None
    remote_static: bytes
    handshake_hash: bytes
    send: noise.CipherState
    recv: noise.CipherState


def preamble(mode: str, invite: str | None = None) -> bytes:
    doc: dict[str, object] = {"v": WIRE_VERSION, "mode": mode}
    if invite is not None:
        doc["invite"] = invite
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("ascii")


def _parse_preamble(raw: bytes) -> tuple[str, str | None]:
    if len(raw) > PREAMBLE_MAX:
        raise HandshakeRefused("preamble too large")
    try:
        doc = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HandshakeRefused("preamble is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("v") != WIRE_VERSION:
        raise HandshakeRefused("unsupported wire version")
    mode = doc.get("mode")
    invite = doc.get("invite")
    if mode == MODE_PEER and set(doc) == {"v", "mode"}:
        return MODE_PEER, None
    if mode == MODE_PAIR and set(doc) == {"v", "mode", "invite"} and isinstance(invite, str):
        if 8 <= len(invite) <= 64 and invite.isascii() and invite.replace("-", "").isalnum():
            return MODE_PAIR, invite
    raise HandshakeRefused("malformed preamble")


async def initiate(
    transport: FrameTransport,
    static: X25519PrivateKey,
    *,
    mode: str,
    expect_static: bytes,
    invite: str | None = None,
    psk: bytes | None = None,
) -> Established:
    """Dial side. Refuses, BEFORE sending this side's static key, a responder whose static key is
    not ``expect_static`` (the pinned peer key, or the key in the invite)."""
    pre = preamble(mode, invite)
    hs = noise.HandshakeState(initiator=True, static=static, prologue=pre, psk=psk)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
            await transport.send(pre)
            await transport.send(hs.write_message())
            hs.read_message(await transport.recv())
            if hs.rs != expect_static:
                raise HandshakeRefused("the responder is not the expected peer")
            await transport.send(hs.write_message())
    except TimeoutError as exc:
        raise HandshakeRefused("handshake timed out") from exc
    except ChannelClosed as exc:
        raise HandshakeRefused("closed during the handshake") from exc
    except noise.NoiseError as exc:
        raise HandshakeRefused(f"handshake failed: {exc}") from exc
    send, recv = hs.split()
    return Established(True, mode, invite, hs.rs or b"", hs.handshake_hash, send, recv)


async def respond(
    transport: FrameTransport,
    static: X25519PrivateKey,
    *,
    psk_for: Callable[[str, str | None], bytes | None],
) -> Established:
    """Accept side. ``psk_for(mode, invite)`` returns the invite secret for ``pair`` mode, ``None``
    for ``peer`` mode, or raises :class:`HandshakeRefused` (no such invite, pairing closed)."""
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
            pre = await transport.recv()
            mode, invite = _parse_preamble(pre)
            psk = psk_for(mode, invite)
            if (psk is None) != (mode == MODE_PEER):
                raise HandshakeRefused("pairing secret missing")
            hs = noise.HandshakeState(initiator=False, static=static, prologue=pre, psk=psk)
            hs.read_message(await transport.recv())
            await transport.send(hs.write_message())
            hs.read_message(await transport.recv())
    except TimeoutError as exc:
        raise HandshakeRefused("handshake timed out") from exc
    except ChannelClosed as exc:
        raise HandshakeRefused("closed during the handshake") from exc
    except noise.NoiseError as exc:
        raise HandshakeRefused(f"handshake failed: {exc}") from exc
    send, recv = hs.split()
    return Established(False, mode, invite, hs.rs or b"", hs.handshake_hash, send, recv)


class _AdmittingMux(hf_mux.Mux):
    """The Home Free mux behind a federation-owned admission boundary.

    The stock mux trusts its counterpart: it allocates a stream for every OPEN and appends every
    DATA frame. That is right for this operator's own browser and wrong for another party, so
    every inbound frame is judged HERE, before the stock mux allocates or buffers anything. The
    wire format is unchanged; Home Free is untouched.

    ``admit(open_info_len)`` is asked before a peer-opened stream exists and answers ``None`` to
    admit it or a reset code to refuse it. ``violation()`` is told about a frame no honest peer
    sends."""

    def __init__(self, *, admit, violation, **kwargs) -> None:
        super().__init__(**kwargs)
        self._admit = admit
        self._violation = violation
        # Stream ids the other side may open: the parity this side does not use, increasing.
        self._peer_parity = self._next_id % 2 ^ 1
        self._peer_high = 0
        self._peer_open: set[int] = set()

    @property
    def peer_streams(self) -> int:
        return len(self._peer_open)

    def _refuse(self, stream_id: int, code: int) -> None:
        self._emit(hf_mux._encode(stream_id, hf_mux.RESET, bytes([code])))

    def feed(self, frame: bytes) -> None:
        if self._closed:
            return
        stream_id, ftype, payload = hf_mux._decode(frame)  # ValueError: the caller closes
        if ftype == hf_mux.OPEN:
            # A reused id, an id of this side's parity, or one that goes backwards: never honest.
            if stream_id % 2 != self._peer_parity or stream_id <= self._peer_high:
                self._violation()
                return self._refuse(stream_id, RESET_REFUSED)
            self._peer_high = stream_id
            code = self._admit(len(payload))
            if code is not None:
                return self._refuse(stream_id, code)  # nothing was allocated
            self._peer_open.add(stream_id)
        elif ftype == hf_mux.DATA:
            stream = self._streams.get(stream_id)
            if stream is not None:
                if stream._recv_eof:  # DATA after END
                    self._violation()
                    return stream.reset(RESET_REFUSED)
                if len(stream._recv_buf) + len(payload) > hf_mux.INITIAL_WINDOW:
                    self._violation()
                    return stream.reset(RESET_OVERRUN)
        super().feed(frame)

    def _drop(self, sid: int) -> None:
        self._peer_open.discard(sid)
        super()._drop(sid)

    def close(self) -> None:
        self._peer_open.clear()
        super().close()


class _Bucket:
    """A token bucket: ``per_minute`` tokens, refilled continuously, burst of one minute's worth."""

    def __init__(self, per_minute: int) -> None:
        self._cap = float(per_minute)
        self._tokens = float(per_minute)
        self._at = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._cap, self._tokens + (now - self._at) * self._cap / 60.0)
        self._at = now

    def take(self, amount: float = 1.0) -> bool:
        self._refill()
        if self._tokens < amount:
            return False
        self._tokens -= amount
        return True

    def debt(self, amount: float) -> float:
        """Spend ``amount`` unconditionally and return how long to wait until the bucket is no
        longer overdrawn (0 when it still holds tokens)."""
        self._refill()
        self._tokens -= amount
        return 0.0 if self._tokens >= 0 else -self._tokens * 60.0 / self._cap


@dataclass
class Stats:
    calls_in: int = 0
    calls_out: int = 0
    refused: int = 0  # violations: unknown verb, wrong mode, bad arguments, overrun
    busy: int = 0  # budget refusals
    opened_at: float = field(default_factory=time.time)


class Channel:
    """An established, trusted channel. Build it only after the trust check."""

    def __init__(
        self,
        transport: FrameTransport,
        established: Established,
        *,
        verbs: dict[str, Verb],
        context: Any = None,
        budgets: Budgets | None = None,
    ) -> None:
        self._transport = transport
        self._est = established
        self._verbs = verbs
        self.context = context
        self._budgets = budgets or Budgets()
        self._bucket = _Bucket(self._budgets.calls_per_minute)
        self._bytes_in = _Bucket(self._budgets.bytes_per_minute)
        self._bytes_out = _Bucket(self._budgets.bytes_per_minute)
        self._mux = _AdmittingMux(
            is_initiator=established.initiator,
            on_send=self._enqueue,
            on_stream=self._on_stream,
            admit=self._admit,
            violation=self._count_violation,
        )
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._handlers: set[asyncio.Task] = set()
        self._closed = asyncio.Event()
        self._close_reason = ""
        self.stats = Stats()

    # ── facts ─────────────────────────────────────────────────────────
    @property
    def mode(self) -> str:
        return self._est.mode

    @property
    def initiator(self) -> bool:
        return self._est.initiator

    @property
    def remote_static(self) -> bytes:
        return self._est.remote_static

    @property
    def handshake_hash(self) -> bytes:
        return self._est.handshake_hash

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def close_reason(self) -> str:
        return self._close_reason

    # ── lifecycle ─────────────────────────────────────────────────────
    async def run(self) -> None:
        """Pump the channel until it ends. Returns normally on any end; never raises."""
        writer = asyncio.create_task(self._writer())
        closing = asyncio.create_task(self._closed.wait())
        try:
            while not self._closed.is_set():
                # A local close() must end the pump even while the other side is silent.
                receiving = asyncio.create_task(self._transport.recv())
                await asyncio.wait({receiving, closing}, return_when=asyncio.FIRST_COMPLETED)
                if not receiving.done():
                    receiving.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ChannelClosed):
                        await receiving
                    break
                try:
                    wire = receiving.result()
                except ChannelClosed:
                    self._end("transport closed")
                    break
                if len(wire) > WIRE_MAX:
                    self._end("oversize frame")
                    break
                # The byte budget is backpressure: past it this side simply reads later, so the
                # peer's sends stall in its own socket and nothing accumulates here.
                wait = self._bytes_in.debt(len(wire))
                if wait > 0:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(wait):
                            await self._closed.wait()
                    if self._closed.is_set():
                        break
                try:
                    frame = self._est.recv.decrypt(b"", wire)
                    self._mux.feed(frame)
                except (noise.NoiseError, ValueError):
                    # Undecryptable or malformed: the stream of frames can no longer be trusted.
                    self._end("bad frame")
                    break
        finally:
            self._end("stopped")
            closing.cancel()
            for task in list(self._handlers):
                task.cancel()
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer
            with contextlib.suppress(Exception):
                await self._transport.close()

    def close(self, reason: str = "closed") -> None:
        self._end(reason)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def _end(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        self._mux.close()
        self._out.put_nowait(None)

    def _enqueue(self, frame: bytes) -> None:
        if self._closed.is_set():
            return
        if len(frame) > FRAME_MAX or self._out.qsize() >= SEND_QUEUE_MAX:
            self._end("send overflow")
            return
        self._out.put_nowait(frame)

    async def _writer(self) -> None:
        # The ONE place that encrypts: Noise nonces are a counter, so the order frames are
        # encrypted in must be the order they reach the socket.
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            try:
                await self._transport.send(self._est.send.encrypt(b"", frame))
            except (ChannelClosed, noise.NoiseError):
                self._end("transport closed")
                return

    # ── outbound calls ────────────────────────────────────────────────
    async def call(self, verb: str, args: dict | None = None, *, timeout: float = CALL_TIMEOUT_S):
        """Call ``verb`` on the other side and return its ``result`` dict.

        Raises :class:`CallError` with the answer's code, ``"refused"`` when the other side reset
        the stream, ``"timeout"``, or ``"closed"``."""
        if self._closed.is_set():
            raise CallError("closed")
        info = json.dumps({"verb": verb, "args": args or {}}, separators=(",", ":")).encode()
        if len(info) > OPEN_INFO_MAX:
            raise CallError("too_large")
        self.stats.calls_out += 1
        stream = self._mux.open(info)
        try:
            async with asyncio.timeout(timeout):
                await stream.end()
                body = bytearray()
                while True:
                    chunk = await stream.read()
                    if not chunk:
                        break
                    body += chunk
                    if len(body) > ANSWER_MAX:
                        stream.reset(RESET_OVERRUN)
                        raise CallError("too_large")
        except TimeoutError:
            stream.reset(RESET_BUSY)
            raise CallError("timeout") from None
        except hf_mux.StreamReset as exc:
            if self._closed.is_set():
                raise CallError("closed") from None
            raise CallError("busy" if exc.code == RESET_BUSY else "refused") from None
        try:
            doc = json.loads(bytes(body))
        except ValueError:
            raise CallError("bad_answer") from None
        if not isinstance(doc, dict):
            raise CallError("bad_answer")
        if doc.get("ok") is True and isinstance(doc.get("result"), dict):
            return doc["result"]
        code = doc.get("code")
        raise CallError(code if isinstance(code, str) and len(code) <= 64 else "bad_answer")

    # ── inbound calls ─────────────────────────────────────────────────
    def _count_violation(self) -> None:
        self.stats.refused += 1
        if self.stats.refused >= self._budgets.violations:
            self._end("too many violations")

    def _violation(self, stream: hf_mux.Stream) -> None:
        stream.reset(RESET_REFUSED)
        self._count_violation()

    def _admit(self, open_info_len: int) -> int | None:
        """Asked BEFORE a peer-opened stream is allocated: ``None`` admits it."""
        if open_info_len > OPEN_INFO_MAX:
            self._count_violation()
            return RESET_REFUSED
        if self._mux.peer_streams >= self._budgets.streams or not self._bucket.take():
            self.stats.busy += 1
            return RESET_BUSY
        return None

    def _on_stream(self, stream: hf_mux.Stream) -> None:
        try:
            doc = json.loads(stream.open_info)
        except ValueError:
            return self._violation(stream)
        if not isinstance(doc, dict) or set(doc) != {"verb", "args"}:
            return self._violation(stream)
        name, raw = doc["verb"], doc["args"]
        verb = self._verbs.get(name) if isinstance(name, str) else None
        if verb is None or self.mode not in verb.modes or not isinstance(raw, dict):
            return self._violation(stream)
        try:
            args = verb.validate(raw)
        except BadArgs:
            return self._violation(stream)
        self.stats.calls_in += 1
        task = asyncio.create_task(self._serve(stream, verb, args))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _serve(self, stream: hf_mux.Stream, verb: Verb, args: dict) -> None:
        try:
            try:
                async with asyncio.timeout(HANDLER_TIMEOUT_S):
                    answer: dict = {"ok": True, "result": await verb.handle(self, args)}
            except VerbError as exc:
                answer = {"ok": False, "code": exc.code}
            except TimeoutError:
                answer = {"ok": False, "code": "timeout"}
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("federation verb %s failed", verb.name)
                answer = {"ok": False, "code": "internal"}
            body = json.dumps(answer, separators=(",", ":")).encode()
            if len(body) > ANSWER_MAX:
                body = b'{"ok":false,"code":"too_large"}'
            elif not self._bytes_out.take(len(body)):
                self.stats.busy += 1
                body = b'{"ok":false,"code":"busy"}'
            await stream.write(body)
            await stream.end()
        except hf_mux.StreamReset:
            pass  # the caller gave up, or the channel closed
        finally:
            # Answered: forget the stream even if the caller never half-closes its side, so a
            # caller cannot park finished streams in the mux.
            self._mux._drop(stream.id)
