"""dtach-backed persistent PTY sessions for the web terminal (issue #49).

The replacement for the ttyd + Zellij stack. Each agent session runs under its
own ``dtach`` master — a transparent single-program detach with **no terminal
UI, no mouse capture, no alt-screen** — so the agent's output reaches xterm.js
raw (native scroll/select/copy) and survives browser disconnects + app
redeploys. One dtach socket per ``{engine}-{session_id}``; identity is the
socket, never a mutable tab label (the Zellij failure mode this removes).

This module is the shell-free session/identity layer: it builds the validated
``dtach`` argv and maps session ids to sockets. The asyncio websocket↔PTY bridge
that attaches to these sockets lives in the web layer.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

DTACH_BIN = os.environ.get("AGENT_SESSIONS_DTACH_BIN") or shutil.which("dtach") or "dtach"

# Only these chars are allowed in the socket filename; everything else is
# squashed so an engine id / session id can never escape the runtime dir or
# inject argv. The real identity is still the full {engine}-{session_id}.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class PtyBridgeError(RuntimeError):
    """Raised when a session descriptor is malformed or unsafe."""


def runtime_dir() -> Path:
    """Directory holding the per-session dtach sockets. Created on demand, 0700."""
    d = Path(
        os.environ.get("AGENT_SESSIONS_RUNTIME_DIR") or (Path.home() / ".agent-sessions" / "pty")
    )
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def socket_path(engine: str, session_id: str) -> Path:
    """Path of the dtach socket for one session. Stable + collision-free.

    The filename is sanitised, but a sanitised collision can't silently merge two
    sessions: the unsanitised ``{engine}\\x00{session_id}`` would have to differ
    only in unsafe chars, which the picker's ids (uuids / ``ses_…`` / engine
    names) never do. Empty parts are rejected outright.
    """
    if not engine or not session_id:
        raise PtyBridgeError(f"empty engine/session id: {engine!r}/{session_id!r}")
    name = f"{_UNSAFE.sub('_', engine)}-{_UNSAFE.sub('_', session_id)}.sock"
    return runtime_dir() / name


def dtach_argv(*, engine: str, session_id: str, launch_argv: Iterable[str]) -> list[str]:
    """Build the create-or-attach ``dtach`` command for a session. Shell-free.

    ``dtach -A <sock> -z -E -r winch <launch_argv…>`` — attach to the existing
    master or create it running ``launch_argv``. ``-z`` drops the suspend key,
    ``-E`` drops dtach's escape char (so Ctrl-\\ etc. pass straight to the agent),
    ``-r winch`` redraws via SIGWINCH on attach. ``launch_argv`` is the already
    validated engine command (absolute binary + args) from the engine descriptor;
    no shell is ever involved.
    """
    argv = list(launch_argv)
    if not argv:
        raise PtyBridgeError("launch_argv must not be empty")
    if not argv[0].startswith("/"):
        # dtach treats the first non-option token as the command; an absolute
        # path guarantees it's not mistaken for a dtach flag and that we launch
        # the intended binary (the bins are off the login PATH anyway).
        raise PtyBridgeError(f"launch binary must be an absolute path: {argv[0]!r}")
    sock = str(socket_path(engine, session_id))
    return [DTACH_BIN, "-A", sock, "-z", "-E", "-r", "winch", *argv]


def session_exists(engine: str, session_id: str) -> bool:
    """True if a live dtach master exists for this session.

    dtach removes its socket when the master (the agent process) exits, so socket
    presence is a reliable liveness signal — and it's keyed by the real session
    id, so it can't drift the way a Zellij tab label could.
    """
    try:
        return socket_path(engine, session_id).is_socket()
    except PtyBridgeError:
        return False


def list_sessions() -> list[tuple[str, str]]:
    """Every live ``(engine, session_id)`` with a dtach socket. Best-effort."""
    out: list[tuple[str, str]] = []
    try:
        entries = list(runtime_dir().iterdir())
    except OSError:
        return out
    for p in entries:
        if p.suffix != ".sock" or not p.is_socket():
            continue
        stem = p.stem
        engine, sep, sid = stem.partition("-")
        if sep and engine and sid:
            out.append((engine, sid))
    return out
