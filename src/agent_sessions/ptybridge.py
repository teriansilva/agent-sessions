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
import socket
from collections.abc import Iterable
from pathlib import Path

DTACH_BIN = os.environ.get("AGENT_SESSIONS_DTACH_BIN") or shutil.which("dtach") or "dtach"

# Short non-blocking probe to verify a dtach master is actually accepting connections
# on a `.sock` file (vs an orphan file left by a master that died without unlinking).
# Local UNIX socket; 200 ms is generous and never user-perceptible.
_PROBE_TIMEOUT_S = 0.2

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


def attach_argv(*, engine: str, session_id: str) -> list[str]:
    """Build the **attach-only** ``dtach`` command — no fallback to create.

    ``dtach -a <sock>`` — attach to an already-running master, fail loud (non-zero
    exit) if no master is accepting. The server's `open_action` is the one place
    that decides ATTACH vs LAUNCH; dtach must not silently fall back to "create"
    when its `-A` mode can't connect (that fallback unlinks + re-binds the sock,
    producing a second master on the same path = split-brain). #165.
    """
    # dtach's argv shape is `-a <socket> <options>` — the socket must come immediately
    # after `-a` or dtach parses the next option (e.g. `-z`) as the socket path and fails.
    sock = str(socket_path(engine, session_id))
    return [DTACH_BIN, "-a", sock, "-z", "-E", "-r", "winch"]


def launch_argv(*, engine: str, session_id: str, launch_argv: Iterable[str]) -> list[str]:
    """Build the **create-only** ``dtach`` command — fails loud if a sock exists.

    ``dtach -c <sock> -z -E -r winch <launch_argv…>`` — create the master running
    ``launch_argv``, refusing to start if the sock file is already there. The
    caller must hold the single-writer lock for the session and have unlinked any
    stale sock under that lock (see `unlink_if_stale`). ``launch_argv`` is the
    already-validated absolute-path engine command; no shell is ever involved.
    """
    argv = list(launch_argv)
    if not argv:
        raise PtyBridgeError("launch_argv must not be empty")
    if not argv[0].startswith("/"):
        raise PtyBridgeError(f"launch binary must be an absolute path: {argv[0]!r}")
    sock = str(socket_path(engine, session_id))
    return [DTACH_BIN, "-c", sock, "-z", "-E", "-r", "winch", *argv]


def _master_alive(sock_path: Path) -> bool:
    """True if a dtach master is actually accepting on ``sock_path``.

    Probes via a short non-blocking UNIX connect. Returns False when the file
    doesn't exist, is the wrong type, no one is listening (orphan sock from a
    crashed master that didn't unlink), or the OS rejects the connect for any
    reason. Best-effort and conservative: a transient failure looks dead, which
    is the safe direction (the caller will then take the LAUNCH path under the
    fcntl lock — which is itself idempotent if the master is actually alive).
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(_PROBE_TIMEOUT_S)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def session_exists(engine: str, session_id: str) -> bool:
    """True if a live dtach master is **accepting connections** for this session.

    File presence alone is not enough: a master that crashed (SIGKILL, OOM, host
    reboot mid-write) can leave its `.sock` file behind, and trusting that file
    as "alive" would point a subsequent ATTACH at nothing. We additionally probe
    with a short `connect()` so a stale sock is correctly classified as not-alive,
    letting `open_action` route to LAUNCH instead. #165.
    """
    try:
        p = socket_path(engine, session_id)
    except PtyBridgeError:
        return False
    if not p.is_socket():
        return False
    return _master_alive(p)


def unlink_if_stale(engine: str, session_id: str) -> bool:
    """Remove a `.sock` file whose master is gone; no-op if alive or absent.

    Caller MUST hold the single-writer fcntl lock for the session — that is what
    makes "the sock exists but probe says dead" unambiguously mean "orphan from a
    prior generation" (no one else is racing to launch). Returns True iff we
    removed a stale file (informational for tests / logging).
    """
    try:
        p = socket_path(engine, session_id)
    except PtyBridgeError:
        return False
    if not p.exists():
        return False
    if _master_alive(p):
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True


def list_sessions() -> list[tuple[str, str]]:
    """Every live ``(engine, session_id)`` whose dtach master is accepting.

    Orphan sock files (master dead but file lingered) are filtered out by the
    same connect-probe `session_exists` uses, so callers never see a phantom
    session that would 4500 on the next attach.
    """
    out: list[tuple[str, str]] = []
    try:
        entries = list(runtime_dir().iterdir())
    except OSError:
        return out
    for p in entries:
        if p.suffix != ".sock" or not p.is_socket():
            continue
        if not _master_alive(p):
            continue
        stem = p.stem
        engine, sep, sid = stem.partition("-")
        if sep and engine and sid:
            out.append((engine, sid))
    return out
