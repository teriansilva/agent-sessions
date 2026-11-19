"""One real BattleLab app for the two-instance pairing E2E (#1246 P2, #2052).

``web/e2e/appmode-federation-pair.spec.ts`` starts two of these and pairs them through the real
Settings → Peers page in two browsers. Everything is the production path: ``create_app``, its
lifespan (which starts the federation dialer), the real routes, the real ``/ws/fed`` door and the
real dialer between the two processes.

The one thing set by hand is what this instance **advertises**. An app derives its direct address
from its configured origin and, by design, advertises nothing for a loopback origin (another host
could not reach it). Two instances on one test machine can only reach each other over loopback, so
the harness says so explicitly, exactly as ``tests/test_federation_routes.py`` does for its
in-process pair. The display name is set too, so the two sides are told apart on screen (both
would otherwise show this machine's hostname).
"""

from __future__ import annotations

import argparse
import ctypes
import signal
import subprocess
import time
from pathlib import Path

import uvicorn

from agent_sessions import engines, ptybridge
from agent_sessions.main import create_app


def _die_with_parent() -> None:
    """In the child, before it becomes ``dtach``: be killed when the parent dies."""
    pr_set_pdeathsig = 1
    ctypes.CDLL(None, use_errno=True).prctl(pr_set_pdeathsig, signal.SIGKILL)


def _host_terminal(native: str) -> subprocess.Popen:
    """A real terminal session for ``web/e2e/appmode-federation-terminal.spec.ts`` (#2060), made
    the way ``tests/test_federation_terminal.py``'s two-instance test makes one: a record in the
    shell provider's own store, and a real ``dtach`` master on the session's own socket running
    ``cat`` where the shell provider runs ``bash`` (so the bytes are the same on every host).

    Nothing else is done to it here. The operator's page opens it as any terminal session: the
    app's own ``/ws/term`` attach is the viewer that records its geometry and drains its output
    into the ring, and what is typed in that page is its input.

    ``-N`` keeps the master in the foreground as this process's child, and the kernel is asked to
    kill it when this process dies (``PR_SET_PDEATHSIG``): ``dtach`` starts its own session, so
    the harness's kill of this process's group would not reach it, and that kill is not one a
    ``finally`` here survives. No master is left behind; ``cat`` ends with its terminal."""
    prov, _native = engines.parse_key(f"shell:{native}")
    prov.on_new_session(native, cwd=str(Path.home()))
    sock = ptybridge.socket_path("shell", native)
    sock.parent.mkdir(parents=True, exist_ok=True)
    master = subprocess.Popen(
        [ptybridge.DTACH_BIN, "-N", str(sock), "-E", "-z", "/bin/cat"],
        preexec_fn=_die_with_parent,  # single-threaded here: nothing else runs yet
    )
    deadline = time.monotonic() + 30
    while not ptybridge.session_exists("shell", native):
        if master.poll() is not None or time.monotonic() > deadline:
            raise SystemExit("the terminal session's dtach master did not come up")
        time.sleep(0.05)
    return master


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--terminal", help="host a real terminal session under this shell id")
    args = parser.parse_args()

    app = create_app()
    federation = app.state.federation
    federation.addresses = [{"t": "direct", "url": f"ws://127.0.0.1:{args.port}/ws/fed"}]
    federation._name = args.name
    master = _host_terminal(args.terminal) if args.terminal else None
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if master is not None:
            master.kill()
            master.wait(timeout=10)


if __name__ == "__main__":
    main()
