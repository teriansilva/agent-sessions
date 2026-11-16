"""Shared pieces for the federation tests (#1246): an in-memory frame transport pair."""

from __future__ import annotations

import asyncio

from agent_sessions.federation import channel as ch


class PipeEnd:
    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self.closed = False
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise ch.ChannelClosed()
        self.sent.append(data)
        self._outbox.put_nowait(data)

    async def recv(self) -> bytes:
        if self.closed:
            raise ch.ChannelClosed()
        data = await self._inbox.get()
        if data is None:
            self.closed = True
            raise ch.ChannelClosed()
        return data

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._outbox.put_nowait(None)
            self._inbox.put_nowait(None)


def pipe() -> tuple[PipeEnd, PipeEnd]:
    a, b = asyncio.Queue(), asyncio.Queue()
    return PipeEnd(a, b), PipeEnd(b, a)


async def until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
