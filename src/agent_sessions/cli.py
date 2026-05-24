"""``agent-sessions`` console entrypoint.

Subcommands today: ``serve`` (run the web server) and ``version``. The installer
(issue #65) calls ``serve`` from the systemd unit; later phases add ``doctor`` /
``discover-engines`` and ``reset-password`` as further subcommands here.
"""

from __future__ import annotations

import argparse
import os

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-sessions", description="agent-sessions server + tools")
    p.add_argument("--version", action="version", version=f"agent-sessions {__version__}")
    sub = p.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="Run the web server")
    serve.add_argument(
        "--host",
        default=os.environ.get("AGENT_SESSIONS_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1 — put a reverse proxy in front for TLS)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENT_SESSIONS_PORT", "8765")),
        help="Bind port (default: 8765, or $AGENT_SESSIONS_PORT)",
    )

    sub.add_parser("version", help="Print the version and exit")

    doc = sub.add_parser(
        "doctor",
        aliases=["discover-engines"],
        help="Discover installed agent CLIs and record their paths in the env",
    )
    doc.add_argument("--env", default=None, help="env file to update (default: <home>/env)")
    doc.add_argument("--dry-run", action="store_true", help="print findings without writing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "version":
        print(__version__)
        return 0

    if args.cmd in ("doctor", "discover-engines"):
        from pathlib import Path

        from . import discover

        found = discover.discover()
        for name in discover.ENGINES:
            path = found[name]
            print(f"  {name:9} {('→ ' + path) if path else '— not found'}")
        if not args.dry_run:
            env_path = Path(args.env).expanduser() if args.env else discover.default_env_path()
            if env_path.exists() or env_path.parent.is_dir():
                discover.write_env_bins(env_path, found)
                print(f"updated {env_path}")
            else:
                print(f"(no env file at {env_path} — skipped write)")
        return 0

    if args.cmd == "serve":
        import uvicorn

        from .main import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    _build_parser().print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
