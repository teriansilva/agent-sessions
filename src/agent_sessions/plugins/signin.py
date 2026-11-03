"""Single-owner, authenticated ephemeral sign-in transport (#1259).

Only binary PTY frames and strict resize messages pass through. This module never registers a
session or sends bytes to scrollback, transcript capture, logging, review, or recap.

A deliberate close ends the sign-in: the transient service is terminated before ownership is
released. A socket that is LOST instead (#1846) — a phone browser drops it while the operator
approves the sign-in in another tab or app — leaves the vendor command running for a bounded
grace window. The same authenticated operator may attach again in that window; it receives only
the bytes produced while nobody was attached (in memory, bounded), never a replay of the sign-in.
One socket at a time: an attach while another is connected is refused, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import WebSocket

from . import manager, process, storage

GRACE_SECONDS = 300  # within the unit's own lifetime (process.SIGNIN_SECONDS), which still applies
MAX_DETACHED = 256 * 1024  # output produced while no socket is attached
CLOSED_BY_OPERATOR = 4000  # the SPA's explicit close: end now, no grace
BUSY = 4423  # another socket owns this sign-in right now; the caller may retry
# Close codes that mean the operator closed it. Anything else (1001 going away, 1006 lost, ...) is
# a lost connection and starts the grace window.
_DELIBERATE = frozenset({1000, 1005, CLOSED_BY_OPERATOR})

_LIVE: dict[str, _Live] = {}


class _Live:
    """A running sign-in that may be between sockets. Event-loop confined."""

    def __init__(self) -> None:
        self.socket: WebSocket | None = None
        # Ownership, reserved BEFORE a socket is handed over (Hermes on #1849): set for the first
        # socket from the start, and for an accepted attach before its flush; cleared only when
        # the owner is lost. While set, every other attach is refused, so none can queue.
        self.owned = True
        self.pending = bytearray()
        self.offers: asyncio.Queue[tuple[WebSocket, asyncio.Future]] = asyncio.Queue()
        self.offered: list[tuple[WebSocket, asyncio.Future]] = []  # every socket ever offered
        self.closing = asyncio.Event()  # the operator closed it while no socket could say so
        self.ended = asyncio.Event()  # the outcome is durable

    async def deliver(self, data: bytes) -> None:
        if self.socket is not None:
            # A dead socket is noticed by its receive loop; until then its bytes are held.
            with contextlib.suppress(Exception):
                await self.socket.send_bytes(data)
                return
        if len(self.pending) + len(data) > MAX_DETACHED:
            raise process.ProcessError("sign-in output exceeded its limit while disconnected")
        self.pending.extend(data)


async def close(operation_id: str, *, timeout: float = 10) -> bool:
    """End a live sign-in now, on the operator's explicit close (#1846 review): a terminal that
    lost its socket cannot send the 4000 close, so the authenticated cancel route says it instead.
    True when this process held the sign-in; returns once its outcome is durable (bounded).

    Process-local, like the live sign-in itself: the app serves from ONE uvicorn process
    (`cli.serve`). A cancel that reached a process without it would leave the sign-in to its grace
    window and unit lifetime, which still bound it."""
    live = _LIVE.get(operation_id)
    if live is None:
        return False
    live.closing.set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(live.ended.wait(), timeout)
    return True


async def _attach(live: _Live, ws: WebSocket) -> None:
    """Offer this socket to the running sign-in and hold the handler until it is let go."""
    if live.owned or not live.offers.empty():
        await ws.close(code=BUSY)
        return
    live.owned = True  # reserved now, in the same loop step as the check
    released: asyncio.Future = asyncio.get_running_loop().create_future()
    live.offered.append((ws, released))
    live.offers.put_nowait((ws, released))
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.shield(released)


async def _input(terminal, ws: WebSocket) -> int | None:
    """Forward one socket's frames until it closes; return its close code."""
    while True:
        frame = await ws.receive()
        if frame["type"] == "websocket.disconnect":
            return frame.get("code")
        if frame.get("bytes") is not None:
            await terminal.write(frame["bytes"])
        elif frame.get("text") is not None:
            if len(frame["text"]) > 128:
                raise process.ProcessError("invalid terminal control")
            control = json.loads(frame["text"])
            if not isinstance(control, dict) or set(control) != {"rows", "cols"}:
                raise process.ProcessError("invalid terminal control")
            terminal.resize(control["rows"], control["cols"])
        else:
            raise process.ProcessError("invalid terminal frame")


async def _flush(live: _Live, sock: WebSocket) -> None:
    """Send what was produced while detached. A batch leaves the buffer only once sent, so a
    socket lost mid-flush leaves it whole for the next attach."""
    while live.pending:
        data = bytes(live.pending)
        await sock.send_bytes(data)
        del live.pending[: len(data)]


async def _drive(live: _Live, terminal, ws: WebSocket) -> tuple[str, str | None]:
    """Pump one sign-in across its sockets until it ends; return the operation outcome."""
    loop = asyncio.get_running_loop()

    async def output():
        while data := await terminal.read():
            await live.deliver(data)
        return await terminal.proc.wait()

    def outcome(code: int) -> tuple[str, str | None]:
        if code == 0:
            return "complete", None
        return "failed", "the sign-in command exited without success"

    reader = asyncio.create_task(output())
    closing = asyncio.create_task(live.closing.wait())
    closed = ("interrupted", "sign-in closed; start a new attempt")
    current: tuple[WebSocket, asyncio.Future | None] | None = (ws, None)
    live.socket = ws
    deadline: float | None = None
    try:
        while True:
            if current is not None:
                sock = current[0]
                writer = asyncio.create_task(_input(terminal, sock))
                try:
                    done, _ = await asyncio.wait(
                        (reader, writer, closing), return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    if not writer.done():
                        writer.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await process.drain(writer)
                if closing in done:
                    return closed
                # A socket that closed cannot claim success from a simultaneous process exit.
                if writer in done:
                    code = writer.result()
                    live.socket = None
                    live.owned = False
                    if current[1] is not None:
                        current[1].set_result(None)
                    current = None
                    if code in _DELIBERATE:
                        return closed
                    deadline = loop.time() + GRACE_SECONDS
                    continue
                return outcome(reader.result())
            offer = asyncio.create_task(live.offers.get())
            try:
                done, _ = await asyncio.wait(
                    (reader, offer, closing),
                    timeout=max(0.0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not offer.done():
                    offer.cancel()
            if closing in done:
                return closed
            if offer in done:
                sock, released = offer.result()
                live.owned = True  # (already reserved by `_attach`; an owner from here on)
                # What was produced while detached goes first; the reader keeps appending until
                # the buffer is empty, and only then writes to the socket directly. A batch leaves
                # the buffer only once sent: a socket lost during this flush is one more lost
                # socket (Hermes on #1849), and the next attach still receives everything.
                # The replay is raced against Close and the vendor's own exit (Hermes on #1849): a
                # stalled browser must not hold up an explicit Close.
                flush = asyncio.create_task(_flush(live, sock))
                done, _ = await asyncio.wait(
                    (flush, closing, reader), return_when=asyncio.FIRST_COMPLETED
                )
                if not flush.done():
                    flush.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await process.drain(flush)
                    released.set_result(None)
                    return closed if closing in done else outcome(reader.result())
                if flush.exception() is not None:  # any send failure is a lost socket
                    live.owned = False
                    released.set_result(None)
                    deadline = loop.time() + GRACE_SECONDS
                    continue
                current = (sock, released)
                live.socket = sock
                deadline = None
                continue
            if reader in done:
                # Nobody watching, but the vendor's own exit is still its verdict.
                return outcome(reader.result())
            return "interrupted", "sign-in disconnected; start a new attempt"
    finally:
        live.socket = None
        closing.cancel()
        if not reader.done():
            reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await process.drain(reader)
        live.pending = bytearray()


async def serve(ws: WebSocket, operation_id: str) -> None:
    live = _LIVE.get(operation_id)
    if live is not None:
        await _attach(live, ws)
        return
    fence = storage.locked("worker", wait=0)
    owned = False
    started = False
    live = _Live()
    state, error = "interrupted", "sign-in disconnected; start a new attempt"
    try:
        acquiring = asyncio.create_task(asyncio.to_thread(fence.__enter__))
        try:
            await asyncio.shield(acquiring)
        except asyncio.CancelledError:
            await process.drain(acquiring)
            owned = True
            raise
        owned = True
        item = await asyncio.to_thread(manager.claim_signin, operation_id)
        started = True
        _LIVE[operation_id] = live
        gen = await asyncio.to_thread(
            manager.generation, item["plugin_id"], item["request"]["generation_id"]
        )
        prov = manager.provider(item["plugin_id"], gen)
        cwd = manager.workspace(item["plugin_id"], item["request"]["generation_id"])
        async with process.spawn(prov, "signin", cwd=cwd, operation_id=operation_id) as terminal:
            state, error = await _drive(live, terminal, ws)
    except process.CleanupError:
        state, error = "cleanup_pending", "temporary process cleanup is pending"
    except asyncio.CancelledError:
        raise
    except Exception:
        state, error = "failed", "sign-in could not complete; start a new attempt"
    finally:
        if _LIVE.get(operation_id) is live:
            del _LIVE[operation_id]
        try:
            if started:
                await process.drain(
                    asyncio.to_thread(manager._set_operation, operation_id, state, error=error)
                )
        finally:
            live.ended.set()
            if owned:
                fence.__exit__(None, None, None)
            code = 1000 if state == "complete" else 4409
            # Every socket this sign-in ever had, including a late offer nobody took, is closed
            # only after the outcome is durable; then its handler is let go.
            for sock in [ws, *(s for s, _ in live.offered)]:
                with contextlib.suppress(Exception):
                    await sock.close(code=code)
            for _, released in live.offered:
                if not released.done():
                    released.set_result(None)
