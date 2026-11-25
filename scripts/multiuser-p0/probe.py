#!/usr/bin/env python3
"""The probe that takes the vendor CLI's place inside a confinement envelope (#2370).

    probe.py PLAN.json > rows.jsonl

It tries every attack the plan lists, from inside the envelope, and prints one JSON row per
attempt: ``{"id", "probe", "target", "observed", "detail"}``, where ``observed`` is ``allowed`` or
``denied``. It also reports what it inherited (descriptors, environment keys, namespaces), which
the harness compares with what the candidate should pass. It never writes outside the paths the
plan names, never sends a signal other than 0, and refuses PIDs 0 and 1. Lab code for the
multi-user P0 harness; nothing in the app calls it.

Plan keys, each optional, each a list:
``read`` / ``write``: ``{"id", "path"}``. ``symlink``: ``{"id", "link", "target"}`` (the link is
created inside the workspace, then read through). ``signal`` / ``ptrace``: ``{"id", "pid"}``.
``unix``: ``{"id", "path"}``. ``tcp``: ``{"id", "host", "port"}``.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys

TCP_TIMEOUT_S = 2.0


def _row(rid: str, probe: str, target: str, observed: str, detail: str = "") -> dict:
    return {"id": rid, "probe": probe, "target": target, "observed": observed, "detail": detail}


def _denied(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, str(exc.errno))


def try_read(rid: str, path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return _row(rid, "read", path, "allowed")
    except OSError as exc:
        return _row(rid, "read", path, "denied", _denied(exc))


def try_write(rid: str, path: str) -> dict:
    try:
        with open(path, "ab"):
            pass
        return _row(rid, "write", path, "allowed")
    except OSError as exc:
        return _row(rid, "write", path, "denied", _denied(exc))


def try_symlink(rid: str, link: str, target: str) -> dict:
    try:
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(target, link)
    except OSError as exc:
        return _row(rid, "symlink", f"{link} -> {target}", "denied", "create " + _denied(exc))
    row = try_read(rid, link)
    row.update(probe="symlink", target=f"{link} -> {target}")
    return row


def _pid(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 1:
        raise ValueError(f"refusing pid {value!r}")  # never 0, 1 or a negative group
    return value


def try_signal(rid: str, pid: object) -> dict:
    target = _pid(pid)
    try:
        os.kill(target, 0)  # signal 0: permission and existence check only, nothing delivered
        return _row(rid, "signal", str(target), "allowed")
    except OSError as exc:
        return _row(rid, "signal", str(target), "denied", _denied(exc))


def try_ptrace(rid: str, pid: object) -> dict:
    target = _pid(pid)
    path = f"/proc/{target}/mem"
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return _row(rid, "ptrace", path, "allowed")
    except OSError as exc:
        return _row(rid, "ptrace", path, "denied", _denied(exc))


def try_unix(rid: str, path: str) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(TCP_TIMEOUT_S)
        sock.connect(path)
        return _row(rid, "unix", path, "allowed")
    except OSError as exc:
        return _row(rid, "unix", path, "denied", _denied(exc))
    finally:
        sock.close()


def _port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 65536:
        raise ValueError(f"refusing port {value!r}")
    return value


def try_tcp(rid: str, host: str, port: object) -> dict:
    port = _port(port)
    target = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_S):
            return _row(rid, "tcp", target, "allowed")
    except TimeoutError:
        return _row(rid, "tcp", target, "denied", "timeout")
    except OSError as exc:
        return _row(rid, "tcp", target, "denied", _denied(exc))


def inherited() -> dict:
    """What this process received: open descriptors beyond stdio, environment keys, namespaces."""
    fds = {}
    for name in sorted(os.listdir("/proc/self/fd"), key=int):
        if int(name) <= 2:
            continue
        try:
            fds[name] = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue  # the descriptor listdir itself used, already closed
    namespaces = {}
    for ns in sorted(os.listdir("/proc/self/ns")):
        try:
            namespaces[ns] = os.readlink(f"/proc/self/ns/{ns}")
        except OSError as exc:
            namespaces[ns] = "unreadable " + _denied(exc)
    return {"fds": fds, "env_keys": sorted(os.environ), "namespaces": namespaces}


def _carry(row: dict, item: dict) -> dict:
    """Pass the host-side ``target_exists`` check (``mu_p0.prepare_plan``) through to the row."""
    if "target_exists" in item:
        row["target_exists"] = bool(item["target_exists"])
    return row


def run(plan: dict) -> list[dict]:
    rows: list[dict] = []
    for item in plan.get("read", []):
        rows.append(_carry(try_read(item["id"], item["path"]), item))
    for item in plan.get("write", []):
        rows.append(_carry(try_write(item["id"], item["path"]), item))
    for item in plan.get("symlink", []):
        rows.append(_carry(try_symlink(item["id"], item["link"], item["target"]), item))
    for item in plan.get("signal", []):
        rows.append(try_signal(item["id"], item["pid"]))
    for item in plan.get("ptrace", []):
        rows.append(try_ptrace(item["id"], item["pid"]))
    for item in plan.get("unix", []):
        rows.append(try_unix(item["id"], item["path"]))
    for item in plan.get("tcp", []):
        rows.append(try_tcp(item["id"], item["host"], item["port"]))
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: probe.py PLAN.json", file=sys.stderr)
        return 2
    with open(argv[0]) as fh:
        plan = json.load(fh)
    for row in run(plan):
        print(json.dumps(row, sort_keys=True))
    print(json.dumps({"id": "inherited", "probe": "inherited", **inherited()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
