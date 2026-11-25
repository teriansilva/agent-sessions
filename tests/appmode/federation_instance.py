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
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from agent_sessions import engines, native_runtime, ptybridge
from agent_sessions.main import create_app

TESTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TESTS))
from native_fake_host import FakeHost  # noqa: E402 — a sibling of the test modules, not a package


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


def _host_api_sessions(runtime_dir: str) -> FakeHost:
    """Make this instance able to run real API (structured) sessions, for the specs that share
    one (``web/e2e/appmode-federation-*.spec.ts``: #2055, #2061, #2062), the way
    ``tests/test_native_runtime.py``'s ``host`` fixture does and with no vendor account:

    * the Codex CLI the in-tree ``codex-api`` client drives is ``tests/fake_native.py``, named by
      the documented ``AGENT_SESSIONS_CODEX_BIN`` override. The agent is scripted by the text of
      the turn (``APPROVE`` raises one exact permission request; anything else is echoed), so a
      spec drives it by typing in the page;
    * the service manager is ``FakeHost``: the exact contained command ``systemd-run`` would run,
      started as a child of this process. The CI runner has no systemd user manager, and a unit
      started under a real one would outlive the harness's kill of this process's group.

    Nothing else is substituted: the roster is the shipped one (the in-tree manifests, their
    stores under this instance's own HOME), sessions are created and driven through the app's
    real routes by the page, and each generation is the real ``native_worker`` process.

    ``runtime_dir`` is where the workers' private sockets go. The caller makes it, short (an
    AF_UNIX path is at most 107 bytes, whatever TMPDIR is), and removes it.

    Called before ``create_app``. The workers are plain children in this process's group, so the
    harness's kill of that group ends them with it; a clean exit closes the host, which kills
    the ones it started by the handles it holds."""
    bindir = Path.home() / "engine-bin"
    bindir.mkdir(mode=0o755, exist_ok=True)
    script = bindir / "codex"
    script.write_text(f"#!{sys.executable}\n" + (TESTS / "fake_native.py").read_text())
    script.chmod(0o755)
    os.environ["AGENT_SESSIONS_CODEX_BIN"] = str(script)
    host = FakeHost(runtime_dir)
    native_runtime.HOST = host
    return host


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--terminal", help="host a real terminal session under this shell id")
    parser.add_argument(
        "--api",
        metavar="RUNTIME_DIR",
        help="run real API sessions on a scripted agent; the workers' sockets go in this directory",
    )
    args = parser.parse_args()

    host = _host_api_sessions(args.api) if args.api else None
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
        if host is not None:
            host.close()


if __name__ == "__main__":
    main()
