"""Single-owner, authenticated ephemeral sign-in transport (#1259).

Only binary PTY frames and strict resize messages pass through. This module never registers a
session or sends bytes to scrollback, transcript capture, logging, review, or recap. Disconnect
terminates the transient service before releasing ownership; reconnect never replays sign-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import WebSocket

from . import manager, process, storage


async def serve(ws: WebSocket, operation_id: str) -> None:
    fence = storage.locked("worker", wait=0)
    owned = False
    started = False
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
        gen = await asyncio.to_thread(
            manager.generation, item["plugin_id"], item["request"]["generation_id"]
        )
        prov = manager.provider(item["plugin_id"], gen)
        cwd = manager.workspace(item["plugin_id"], item["request"]["generation_id"])
        async with process.spawn(prov, "signin", cwd=cwd, operation_id=operation_id) as terminal:

            async def output():
                while data := await terminal.read():
                    await ws.send_bytes(data)
                return await terminal.proc.wait()

            async def input_():
                while True:
                    frame = await ws.receive()
                    if frame["type"] == "websocket.disconnect":
                        return
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

            reader, writer = asyncio.create_task(output()), asyncio.create_task(input_())
            try:
                done, _ = await asyncio.wait((reader, writer), return_when=asyncio.FIRST_COMPLETED)
                # A disconnected owner cannot claim success from a simultaneous process exit.
                if writer in done:
                    writer.result()
                elif reader.result() == 0:
                    state, error = "complete", None
                else:
                    state, error = "failed", "the sign-in command exited without success"
            finally:
                for task in (reader, writer):
                    task.cancel()
                for task in (reader, writer):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await process.drain(task)
    except process.CleanupError:
        state, error = "cleanup_pending", "temporary process cleanup is pending"
    except asyncio.CancelledError:
        raise
    except Exception:
        state, error = "failed", "sign-in could not complete; start a new attempt"
    finally:
        try:
            if started:
                await process.drain(
                    asyncio.to_thread(manager._set_operation, operation_id, state, error=error)
                )
        finally:
            if owned:
                fence.__exit__(None, None, None)
            with contextlib.suppress(Exception):
                await ws.close(code=1000 if state == "complete" else 4409)
