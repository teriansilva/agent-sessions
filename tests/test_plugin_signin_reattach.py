"""The sign-in drive loop across lost sockets (#1846), in memory: no vendor process needed."""

import asyncio

import pytest

from agent_sessions.plugins import signin

CLOSED = ("interrupted", "sign-in closed; start a new attempt")


class Terminal:
    def __init__(self):
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.written = bytearray()
        loop = asyncio.get_running_loop()
        self.exit = loop.create_future()
        terminal = self

        class Proc:
            async def wait(self):
                return await terminal.exit

        self.proc = Proc()

    async def read(self) -> bytes:
        return await self.out.get()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    def resize(self, rows, cols):
        pass


class Socket:
    def __init__(self, *, broken=False, gate: asyncio.Event | None = None):
        self.frames: asyncio.Queue[dict] = asyncio.Queue()
        self.sent = bytearray()
        self.broken = broken
        self.gate = gate  # a send that waits for this: a slow flush
        self.closed: int | None = None

    async def close(self, code=1000):
        self.closed = code

    async def receive(self):
        return await self.frames.get()

    async def send_bytes(self, data: bytes) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.broken:
            raise OSError("socket lost during the flush")
        self.sent.extend(data)

    def lose(self, code=1001):
        self.frames.put_nowait({"type": "websocket.disconnect", "code": code})


async def attach(live, sock):
    """The real attach path: an offer is held until the sign-in lets it go."""
    handler = asyncio.create_task(signin._attach(live, sock))
    await settle()
    return handler


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_a_socket_lost_during_the_flush_keeps_the_bytes_for_the_next_attach():
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose()
    await settle()
    term.out.put_nowait(b"printed while away")
    await settle()
    assert bytes(live.pending) == b"printed while away"
    handler = await attach(live, Socket(broken=True))
    # One more lost socket: the sign-in waits on, its handler is let go, nothing is dropped.
    assert not drive.done()
    assert handler.done()
    assert bytes(live.pending) == b"printed while away"
    good = Socket()
    await attach(live, good)
    assert bytes(good.sent) == b"printed while away"
    good.frames.put_nowait({"type": "websocket.receive", "bytes": b"code\n"})
    await settle()
    assert bytes(term.written) == b"code\n"
    good.lose(signin.CLOSED_BY_OPERATOR)
    assert await asyncio.wait_for(drive, 5) == CLOSED


@pytest.mark.anyio
async def test_closing_while_detached_ends_the_sign_in_at_once():
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose(1006)
    await settle()
    assert not drive.done()
    live.closing.set()
    assert await asyncio.wait_for(drive, 1) == CLOSED


@pytest.mark.anyio
async def test_closing_while_attached_ends_it_too():
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    await settle()
    live.closing.set()
    assert await asyncio.wait_for(drive, 1) == CLOSED


@pytest.mark.anyio
async def test_no_second_socket_is_taken_while_the_first_owns_the_sign_in_from_its_start():
    """Hermes on #1849: `_LIVE` is published before `_drive` runs. An attach in that window must
    be refused, never queued to take over when the first socket drops."""
    live = signin._Live()  # as published by `serve`, before the vendor command even starts
    second = Socket()
    handler = await attach(live, second)
    assert second.closed == signin.BUSY and handler.done() and live.offers.empty()
    term, first = Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose()
    await settle()
    term.out.put_nowait(b"held")
    await settle()
    assert bytes(second.sent) == b""  # the refused socket never receives sign-in output
    live.closing.set()
    assert await asyncio.wait_for(drive, 1) == CLOSED


@pytest.mark.anyio
async def test_no_second_socket_is_taken_during_a_flush():
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose()
    await settle()
    term.out.put_nowait(b"held")
    await settle()
    gate = asyncio.Event()
    flushing = Socket(gate=gate)
    await attach(live, flushing)
    rival = Socket()
    await attach(live, rival)
    assert rival.closed == signin.BUSY and live.offers.empty()
    gate.set()
    await settle()
    assert bytes(flushing.sent) == b"held" and bytes(rival.sent) == b""
    live.closing.set()
    assert await asyncio.wait_for(drive, 1) == CLOSED


@pytest.mark.anyio
async def test_closing_during_a_stalled_replay_ends_the_sign_in_at_once():
    """Hermes on #1849 (round 3): a reconnected browser that stops reading must not hold up an
    explicit Close. The replay send never returns here; Close still ends the sign-in."""
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose()
    await settle()
    term.out.put_nowait(b"held")
    await settle()
    stalled = Socket(gate=asyncio.Event())  # never set: the send hangs
    handler = await attach(live, stalled)
    assert not drive.done()
    live.closing.set()
    assert await asyncio.wait_for(drive, 1) == CLOSED
    await settle()
    assert handler.done()  # its handler is let go too


@pytest.mark.anyio
async def test_the_vendor_exiting_during_a_stalled_replay_is_still_its_verdict():
    live, term, first = signin._Live(), Terminal(), Socket()
    drive = asyncio.create_task(signin._drive(live, term, first))
    first.lose()
    await settle()
    term.out.put_nowait(b"Login successful")
    await settle()
    await attach(live, Socket(gate=asyncio.Event()))
    term.out.put_nowait(b"")  # the PTY closed
    term.exit.set_result(0)
    assert await asyncio.wait_for(drive, 1) == ("complete", None)
