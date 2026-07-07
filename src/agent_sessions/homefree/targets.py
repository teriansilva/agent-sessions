"""Local endpoints the agent bridges a decrypted session to.

- :class:`PtyShellTarget` — the recovery shell: spawns the user's shell in a PTY.
  Shell-free by construction (literal argv, never a shell-string interpreter),
  matching the engine-launcher invariant.
- :class:`EchoTarget` — a deterministic test/loopback endpoint.

A target receives decrypted bytes via ``feed`` and emits bytes back through the
``on_output`` callback given to ``start``.
"""

from __future__ import annotations

import asyncio
import os
import pty
import subprocess
from collections.abc import Callable

OnOutput = Callable[[bytes], None]


class LocalTarget:
    async def start(self, on_output: OnOutput) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def feed(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class EchoTarget(LocalTarget):
    """Emits ``prefix + input`` for each fed chunk. Deterministic; for tests."""

    def __init__(self, prefix: bytes = b"echo:") -> None:
        self._prefix = prefix
        self._on: OnOutput | None = None

    async def start(self, on_output: OnOutput) -> None:
        self._on = on_output

    async def feed(self, data: bytes) -> None:
        if self._on is not None:
            self._on(self._prefix + data)

    async def close(self) -> None:
        self._on = None


def default_shell_argv() -> list[str]:
    """The recovery shell's argv — literal, never wrapped through `sh -c`."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell]


class _NonblockingWriter:
    """Loss-free writes to a non-blocking fd.

    A single ``os.write`` to a non-blocking fd may write only part of the buffer
    (or raise ``BlockingIOError`` when the kernel buffer is full). Dropping the
    unwritten tail would corrupt the decrypted bridge stream, so we buffer the
    remainder and drain it via ``loop.add_writer`` when the fd is writable again.
    """

    def __init__(self, fd: int, loop: asyncio.AbstractEventLoop) -> None:
        self._fd = fd
        self._loop = loop
        self._buf = bytearray()
        self._registered = False

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        self._flush()

    def _flush(self) -> None:
        while self._buf:
            try:
                n = os.write(self._fd, self._buf)
            except BlockingIOError:
                break  # kernel buffer full — resume on the next writable callback
            except OSError:
                self._buf.clear()  # fd gone (shell exited) — drop buffered input
                break
            if n <= 0:
                break
            del self._buf[:n]
        self._update_registration()

    def _update_registration(self) -> None:
        need = bool(self._buf)
        if need and not self._registered:
            self._loop.add_writer(self._fd, self._flush)
            self._registered = True
        elif not need and self._registered:
            self._loop.remove_writer(self._fd)
            self._registered = False

    @property
    def pending(self) -> int:
        return len(self._buf)

    def close(self) -> None:
        if self._registered:
            try:
                self._loop.remove_writer(self._fd)
            except (ValueError, OSError):
                pass
            self._registered = False
        self._buf.clear()


class PtyShellTarget(LocalTarget):
    """Spawns ``argv`` attached to a PTY and bridges its bytes."""

    def __init__(self, argv: list[str] | None = None, *, env: dict | None = None) -> None:
        self._argv = list(argv) if argv else default_shell_argv()
        self._env = env
        self._on: OnOutput | None = None
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._writer: _NonblockingWriter | None = None

    async def start(self, on_output: OnOutput) -> None:
        self._on = on_output
        self._loop = asyncio.get_running_loop()
        master, slave = pty.openpty()
        # Literal argv — no shell interpreter. TERM set for a usable recovery shell.
        env = dict(self._env) if self._env is not None else dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        self._proc = subprocess.Popen(  # noqa: S603 - literal argv, shell-free
            self._argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._writer = _NonblockingWriter(master, self._loop)
        self._loop.add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        assert self._master is not None
        try:
            data = os.read(self._master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # EOF — the shell exited
            if self._loop is not None:
                self._loop.remove_reader(self._master)
            return
        if self._on is not None:
            self._on(data)

    async def feed(self, data: bytes) -> None:
        # Loss-free even under PTY backpressure: partial/EAGAIN writes are
        # buffered and drained on the next writable callback (see #542 review).
        if self._writer is not None:
            self._writer.write(data)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._master is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._master)
            except (ValueError, OSError):
                pass
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None
