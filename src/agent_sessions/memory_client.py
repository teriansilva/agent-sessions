"""Small shared-index client for MCP and the battlelab-memory command (#2036)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
from pathlib import Path

from .memory import ENV_INBOX


class Unavailable(RuntimeError):
    pass


def request(inbox: str, op: str, **args) -> dict:
    path = Path(inbox)
    if not path.is_absolute() or len(path.parts) < 4 or path.parent.name != "inbox":
        raise Unavailable("memory launch is unavailable")
    descriptor = path.parent.parent.parent / ".channels" / (path.name + ".json")
    try:
        fd = os.open(descriptor, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or st.st_uid != os.getuid()
                or st.st_mode & 0o077
            ):
                raise Unavailable("memory capability is unavailable")
            data = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(data) > 4096:
            raise ValueError
        binding = json.loads(data)
        message = json.dumps({"op": op, "token": binding["token"], **args}).encode() + b"\n"
        if len(message) > 8192:
            raise Unavailable("memory request too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(20)
            client.connect(binding["socket"])
            client.sendall(message)
            response = bytearray()
            while len(response) <= 48 * 1024:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
        if len(response) > 48 * 1024:
            raise Unavailable("memory result too large")
        reply = json.loads(response)
        if reply.get("ok") is not True:
            raise Unavailable(str(reply.get("error") or "memory unavailable"))
        return reply["result"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Unavailable("shared memory service unavailable; restart the agent or retry") from None


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search or read this launch's shared BattleLab memory"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("search").add_argument("query")
    read = commands.add_parser("read")
    read.add_argument("slug")
    read.add_argument("--notebook", choices=["project", "mine"], default="project")
    args = parser.parse_args(argv)
    try:
        params = (
            {"query": args.query}
            if args.command == "search"
            else {"slug": args.slug, "notebook": args.notebook}
        )
        print(
            json.dumps(
                request(os.environ.get(ENV_INBOX, ""), args.command, **params), ensure_ascii=False
            )
        )
        return 0
    except Unavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
