"""Websocket ↔ PTY bridge for the per-session terminal (issue #49, Phase 2b).

Bridges a browser xterm.js websocket to a `dtach`-backed PTY (see `ptybridge`).
The agent runs under a persistent dtach master, so closing the browser detaches
(the agent keeps running) and reconnecting re-attaches — survives tab close and
app redeploys. Replaces the ttyd transport; no Zellij involved.

Wire protocol (we own both ends):
- client → server: JSON text frames — ``{"t":"i","d":"<input>"}`` for keystrokes,
  ``{"t":"r","cols":C,"rows":R}`` for resize. (Raw binary frames are also written
  through as input, for robustness.)
- server → client: raw **binary** frames = PTY output, forwarded verbatim.

Backpressure is natural: each output chunk is ``await``-sent before the next read,
so a slow client throttles the read loop (the PTY buffer fills and the agent
blocks on write) rather than growing an unbounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import struct
import termios


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except OSError:  # master closed / child gone
        return b""


# Per-session scrollback. dtach has no scrollback, so on reattach an inline agent
# (claude/codex/gemini) only repaints its near-empty current frame, not the history
# — the terminal looks empty. We keep a capped ring of recent PTY output per session
# in the app process and replay it on connect, so reconnecting shows the conversation
# again. Survives browser disconnect (in-process); lost on app restart (rebuilds on
# the next live output). Keyed by the engine-qualified session id.
_MAX_BUF = 256 * 1024
_BUFFERS: dict[str, bytearray] = {}


def _buffer_append(key: str, data: bytes) -> None:
    buf = _BUFFERS.setdefault(key, bytearray())
    buf.extend(data)
    if len(buf) > _MAX_BUF:
        del buf[: len(buf) - _MAX_BUF]


async def run(
    ws, argv: list[str], *, cwd: str, buf_key: str | None = None, cols: int = 80, rows: int = 24
) -> None:
    """Attach ``ws`` to the PTY of ``argv`` (a built dtach create-or-attach command).

    ``ws`` must already be ``accept``ed. Spawns the dtach client on a fresh PTY with
    ``cwd`` as its working dir, then pumps both directions until either side closes.
    On exit the dtach *client* is terminated (a detach); the dtach *master* keeps the
    agent alive for the next reconnect.
    """
    master, slave = os.openpty()
    _set_winsize(slave, rows, cols)
    # A real color terminal: without TERM, Ink-based agents (claude) disable color.
    # The web frontend is xterm.js, which is a 256-color / truecolor terminal.
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own session → the slave becomes the controlling tty
            close_fds=True,
        )
    except OSError:
        os.close(master)
        os.close(slave)
        with contextlib.suppress(Exception):
            await ws.close(code=4500)
        return
    os.close(slave)  # parent keeps only the master end
    loop = asyncio.get_event_loop()

    # Replay the session's scrollback first, so a reattach shows the prior history
    # (dtach itself has none) before the live redraw + new output arrive.
    if buf_key and _BUFFERS.get(buf_key):
        with contextlib.suppress(Exception):
            await ws.send_bytes(bytes(_BUFFERS[buf_key]))

    async def pump_out() -> None:
        while True:
            data = await loop.run_in_executor(None, _read, master)
            if not data:
                break
            if buf_key is not None:
                _buffer_append(buf_key, data)
            await ws.send_bytes(data)  # awaited → natural backpressure

    async def pump_in() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is not None:
                try:
                    obj = json.loads(text)
                except (ValueError, TypeError):
                    continue
                kind = obj.get("t")
                if kind == "i":
                    with contextlib.suppress(OSError):
                        os.write(master, obj.get("d", "").encode("utf-8", "replace"))
                elif kind == "r":
                    with contextlib.suppress(ValueError, TypeError):
                        _set_winsize(master, int(obj.get("rows", rows)), int(obj.get("cols", cols)))
                        # TIOCSWINSZ on the master doesn't reliably deliver SIGWINCH to
                        # the dtach client here, so dtach never forwards the new size to
                        # the agent's own pty (the terminal stayed a fixed size on window
                        # resize). Nudge the dtach client directly so it re-reads the tty
                        # size and resizes the program → the live agent re-renders wider.
                        with contextlib.suppress(ProcessLookupError, OSError):
                            proc.send_signal(signal.SIGWINCH)
            elif msg.get("bytes") is not None:
                with contextlib.suppress(OSError):
                    os.write(master, msg["bytes"])

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(OSError):
            os.close(master)
        # Detach (don't kill the agent): terminate our dtach client; the master persists.
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=3)
        with contextlib.suppress(Exception):
            await ws.close()
