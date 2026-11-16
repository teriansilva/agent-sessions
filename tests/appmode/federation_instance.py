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

import uvicorn

from agent_sessions.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    app = create_app()
    federation = app.state.federation
    federation.addresses = [{"t": "direct", "url": f"ws://127.0.0.1:{args.port}/ws/fed"}]
    federation._name = args.name
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
