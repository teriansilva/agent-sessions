"""Hand a loopback OAuth callback to a running sign-in (#2106).

Some vendor sign-ins finish on a LOOPBACK callback: the CLI listens on ``127.0.0.1:<port>`` and the
vendor's authorize page redirects the browser there (``copilot login``, ``codex login``, Gemini's
Google sign-in). BattleLab runs the CLI on its own host, so the operator's browser, on another
machine or a phone, lands on its OWN loopback and shows "connection refused". The address it
landed on is still the whole answer: the operator pastes it, and this module makes the same GET
from the BattleLab host, where the CLI is listening.

It is not a general "fetch localhost": a callback is delivered only

* to a sign-in that is running in this process (the caller resolves it), and
* to a port a process inside THAT sign-in's own systemd unit is listening on: the unit's cgroup →
  its pids → their socket inodes → the LISTEN rows of ``/proc/net/tcp{,6}``. BattleLab itself, a
  database, or any other local service is refused, whatever port the pasted address names;
* at the exact address that listener is bound to (a wildcard maps to its family's loopback), and
  never when a listener of ANY other process could also answer there (same address, wildcard,
  SO_REUSEPORT): the address is part of the endpoint's identity, not just the port.

One GET, no redirects followed (``http.client`` never follows), a short timeout, the body read to a
small bound and discarded. The caller learns only the status code. Whether the sign-in worked is
still the CLI's own verdict. The pasted address carries a one-use OAuth code: it is never logged.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

# The plugin package's one door to systemd (only `process` and `runner` run commands).
from . import process

MAX_URL = 4096
TIMEOUT_S = 10
MAX_BODY = 64 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LISTEN = "0A"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC = Path("/proc")


class CallbackError(Exception):
    """The address is not a callback this sign-in is waiting for. Safe to show the operator."""


def parse(url: object) -> tuple[int, str, str]:
    """``(port, request_target, host_header)`` of a pasted loopback callback, or `CallbackError`."""
    if not isinstance(url, str):
        raise CallbackError("paste the address your browser ended on")
    url = url.strip()
    if not url or len(url) > MAX_URL:
        raise CallbackError("paste the address your browser ended on")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise CallbackError("that address contains spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise CallbackError("that is not a web address") from None
    if parts.scheme != "http":
        raise CallbackError("a sign-in callback address starts with http://127.0.0.1 or localhost")
    if parts.username is not None or parts.password is not None:
        raise CallbackError("that address carries a user name; paste it as the browser shows it")
    if (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise CallbackError("a sign-in callback address starts with http://127.0.0.1 or localhost")
    if port is None or not 0 < port < 65536:
        raise CallbackError("that address names no port, so it is not a sign-in callback")
    target = parts.path or "/"
    if not target.startswith("/"):
        raise CallbackError("that is not a web address")
    if parts.query:
        target += "?" + parts.query
    return port, target, parts.netloc


def unit_pids(unit: str) -> set[int]:
    """Every pid in a systemd user unit's cgroup (its sub-cgroups included)."""
    group = process.control_group(unit)
    if group is None:
        return set()
    root = (_CGROUP_ROOT / group.lstrip("/")).resolve()
    if not root.is_relative_to(_CGROUP_ROOT):
        return set()
    pids: set[int] = set()
    for procs in root.rglob("cgroup.procs"):
        try:
            pids.update(int(line) for line in procs.read_text().split())
        except (OSError, ValueError):
            continue
    return pids


def _socket_inodes(pids: set[int]) -> set[str]:
    inodes: set[str] = set()
    for pid in pids:
        fd_dir = _PROC / str(pid) / "fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(fd_dir / name)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    return inodes


def _v4(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)[::-1]
    return ".".join(str(b) for b in raw)


def _v6(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)
    # /proc prints the address as four host-order (little-endian) 32-bit words.
    words = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
    return str(ipaddress.IPv6Address(words))


_ANY_V4, _ANY_V6 = "0.0.0.0", "::"  # noqa: S104 — compared, never bound


def _listen_rows():
    """Every TCP LISTEN socket on this host: ``(inode, family, address, port)``."""
    for table, family, decode in (("tcp", 4, _v4), ("tcp6", 6, _v6)):
        try:
            rows = (_PROC / "net" / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 10 or cols[3] != _LISTEN:
                continue
            addr_hex, port_hex = cols[1].split(":")
            try:
                yield cols[9], family, decode(addr_hex), int(port_hex, 16)
            except ValueError:
                continue


def _destination(family: int, addr: str) -> str | None:
    """The numeric loopback address that reaches THIS listener, or None if loopback cannot.

    A specific address is kept as bound (127.0.0.2 is not 127.0.0.1: two processes can each hold
    the same port on them); only a wildcard is mapped to its family's loopback."""
    if family == 4:
        if addr.startswith("127."):
            return addr
        return "127.0.0.1" if addr == _ANY_V4 else None
    if addr in ("::1", _ANY_V6):
        return "::1"
    if addr.startswith("::ffff:127."):
        return addr.removeprefix("::ffff:")
    return None


def _captures(family: int, addr: str, dest: str) -> bool:
    """Could a listener on ``addr`` answer a connection to ``dest`` (same port)?"""
    if ":" in dest:  # ::1
        return family == 6 and addr in (dest, _ANY_V6)
    if family == 4:
        return addr in (dest, _ANY_V4)
    return addr in (_ANY_V6, "::ffff:" + dest)  # a dual-stack wildcard takes v4 too


def listening(pids: set[int]) -> dict[int, str | None]:
    """``{port: connect_host}`` for every TCP LISTEN socket these pids hold that loopback reaches.

    ``None`` marks a CONTESTED port: a socket NOT held by these pids could also answer a connection
    to that destination (the same address, a wildcard, or SO_REUSEPORT sharing it), so delivering
    there could hand the operator's code to another program (Hermes on #2107)."""
    inodes = _socket_inodes(pids)
    found: dict[int, str | None] = {}
    if not inodes:
        return found
    rows = list(_listen_rows())
    for inode, family, addr, port in rows:
        if inode in inodes and port not in found:
            dest = _destination(family, addr)
            if dest is not None:
                found[port] = dest
    for inode, family, addr, port in rows:
        dest = found.get(port)
        if dest is not None and inode not in inodes and _captures(family, addr, dest):
            found[port] = None
    return found


def _get(host: str, port: int, target: str, host_header: str) -> int:
    """The ONE outbound read: ``GET <target>`` to the sign-in's own loopback listener.

    Inventoried in `tests/test_prompts_registry.py`. No redirects, bounded body, discarded."""
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
    try:
        conn.request("GET", target, headers={"Host": host_header})
        resp = conn.getresponse()
        resp.read(MAX_BODY)
        return resp.status
    except (OSError, http.client.HTTPException):
        raise CallbackError(
            "the sign-in did not answer on that address; start a new attempt"
        ) from None
    finally:
        conn.close()


def deliver(unit: str, url: object, *, pids=unit_pids) -> int:
    """Deliver a pasted callback to the sign-in running as ``unit``; return the HTTP status."""
    port, target, host_header = parse(url)
    ports = listening(pids(unit))
    if port not in ports:
        raise CallbackError(
            f"nothing in this sign-in is listening on port {port}; paste the exact address of "
            "the page that could not be reached"
        )
    dest = ports[port]
    if dest is None:
        raise CallbackError(
            f"another program on the BattleLab machine also listens on port {port}, so the "
            "address is not delivered; start a new sign-in attempt"
        )
    return _get(dest, port, target, host_header)
