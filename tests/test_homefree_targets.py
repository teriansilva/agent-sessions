"""Regression: the PTY writer must not drop bytes under backpressure (#542 review)."""

import asyncio
import os

from agent_sessions.homefree.targets import _NonblockingWriter


def test_nonblocking_writer_loses_no_bytes_under_backpressure():
    async def scenario():
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        os.set_blocking(read_fd, False)
        loop = asyncio.get_running_loop()
        writer = _NonblockingWriter(write_fd, loop)

        # 1 MiB is far larger than a pipe's kernel buffer, so a single os.write
        # cannot take it all — the tail must be buffered, never dropped.
        payload = os.urandom(1_000_000)
        writer.write(payload)
        assert writer.pending > 0, "expected the oversized write to buffer a remainder"

        got = bytearray()

        async def drain():
            while len(got) < len(payload):
                try:
                    chunk = os.read(read_fd, 65536)
                except BlockingIOError:
                    await asyncio.sleep(0)  # let the writer's add_writer callback run
                    continue
                if chunk:
                    got.extend(chunk)
                await asyncio.sleep(0)

        await asyncio.wait_for(drain(), 10)
        assert bytes(got) == payload  # every byte delivered, nothing lost
        assert writer.pending == 0

        writer.close()
        os.close(read_fd)
        os.close(write_fd)

    asyncio.run(scenario())


def test_nonblocking_writer_survives_closed_fd():
    async def scenario():
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        loop = asyncio.get_running_loop()
        writer = _NonblockingWriter(write_fd, loop)
        os.close(read_fd)  # reader gone → writes will EPIPE/EBADF
        writer.write(b"data after reader closed")  # must not raise
        writer.close()
        os.close(write_fd)

    asyncio.run(scenario())
