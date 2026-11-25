#!/usr/bin/env python3
"""Apply a Landlock ruleset to this process, then exec a command (#2370, candidate A).

    landlock_exec.py --ro /usr --ro /etc --rw /work [--connect 443] -- /abs/cmd arg...

Everything not named is denied for the access rights this kernel's ABI handles. ``--connect``
lists the only TCP ports ``connect()`` may reach (ABI 4+); without it, TCP is not restricted by
Landlock, and the harness relies on the network namespace instead. Fail closed: a kernel without
Landlock, a ``--connect`` on an ABI below 4, an unreadable path or any syscall error exits 111
without running the command. Lab code for the multi-user P0 harness; nothing in the app calls it.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446  # x86_64 and aarch64
CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38
REFUSED = 111

FS = {
    "execute": 1 << 0,
    "write_file": 1 << 1,
    "read_file": 1 << 2,
    "read_dir": 1 << 3,
    "remove_dir": 1 << 4,
    "remove_file": 1 << 5,
    "make_char": 1 << 6,
    "make_dir": 1 << 7,
    "make_reg": 1 << 8,
    "make_sock": 1 << 9,
    "make_fifo": 1 << 10,
    "make_block": 1 << 11,
    "make_sym": 1 << 12,
    "refer": 1 << 13,  # ABI 2
    "truncate": 1 << 14,  # ABI 3
    "ioctl_dev": 1 << 15,  # ABI 5
}
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1  # ABI 4
#: Rights that apply to a regular file (a rule on a file may carry only these).
FILE_RIGHTS = FS["execute"] | FS["write_file"] | FS["read_file"] | FS["truncate"] | FS["ioctl_dev"]
READ_RIGHTS = FS["execute"] | FS["read_file"] | FS["read_dir"]


class Refused(Exception):
    pass


def handled_fs(abi: int) -> int:
    """Every filesystem right this ABI knows, so everything not granted is denied."""
    mask = (1 << 13) - 1  # ABI 1: execute … make_sym
    if abi >= 2:
        mask |= FS["refer"]
    if abi >= 3:
        mask |= FS["truncate"]
    if abi >= 5:
        mask |= FS["ioctl_dev"]
    return mask


def parse(argv: list[str]) -> tuple[list[str], list[str], list[int], list[str]]:
    """``(ro, rw, connect_ports, command)``. Paths and the command must be absolute."""
    if "--" not in argv:
        raise Refused("missing -- before the command")
    split = argv.index("--")
    opts, command = argv[:split], argv[split + 1 :]
    ro: list[str] = []
    rw: list[str] = []
    ports: list[int] = []
    it = iter(opts)
    for flag in it:
        value = next(it, None)
        if value is None:
            raise Refused(f"{flag} needs a value")
        if flag == "--ro":
            ro.append(value)
        elif flag == "--rw":
            rw.append(value)
        elif flag == "--connect":
            if not value.isdigit() or not 0 < int(value) < 65536:
                raise Refused(f"bad port {value!r}")
            ports.append(int(value))
        else:
            raise Refused(f"unknown option {flag}")
    for path in [*ro, *rw]:
        if not os.path.isabs(path):
            raise Refused(f"path must be absolute: {path!r}")
    if not command or not os.path.isabs(command[0]):
        raise Refused("the command must be an absolute path")
    return ro, rw, ports, command


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _check(rc: int, what: str) -> int:
    if rc < 0:
        raise Refused(f"{what}: {os.strerror(ctypes.get_errno())}")
    return rc


def abi() -> int:
    """This kernel's Landlock ABI, 0 when Landlock is unavailable."""
    rc = _libc().syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), CREATE_RULESET_VERSION)
    return rc if rc > 0 else 0


def restrict(ro: list[str], rw: list[str], ports: list[int]) -> int:
    """Restrict this process. Returns the ABI it ran on."""
    level = abi()
    if level < 1:
        raise Refused("Landlock is not available on this kernel")
    if ports and level < 4:
        raise Refused(f"--connect needs Landlock ABI 4, this kernel has {level}")
    libc = _libc()
    fs_mask = handled_fs(level)
    net_mask = NET_BIND_TCP | NET_CONNECT_TCP if ports else 0
    attr = struct.pack("=QQ", fs_mask, net_mask) if level >= 4 else struct.pack("=Q", fs_mask)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _check(
        libc.syscall(SYS_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), 0), "create_ruleset"
    )
    try:
        for paths, rights in ((ro, READ_RIGHTS), (rw, fs_mask)):
            for path in paths:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    allowed = rights & fs_mask
                    if not os.path.isdir(path):
                        allowed &= FILE_RIGHTS
                    rule = ctypes.create_string_buffer(struct.pack("=Qi", allowed, fd), 12)
                    _check(
                        libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, rule, 0),
                        f"add_rule {path}",
                    )
                finally:
                    os.close(fd)
        for port in ports:
            rule = ctypes.create_string_buffer(struct.pack("=QQ", NET_CONNECT_TCP, port), 16)
            _check(libc.syscall(SYS_ADD_RULE, ruleset, RULE_NET_PORT, rule, 0), f"add_rule {port}")
        _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        _check(libc.syscall(SYS_RESTRICT_SELF, ruleset, 0), "restrict_self")
    finally:
        os.close(ruleset)
    return level


def main(argv: list[str]) -> int:
    try:
        ro, rw, ports, command = parse(argv)
        restrict(ro, rw, ports)
    except (Refused, OSError) as exc:
        print(f"landlock_exec: refused: {exc}", file=sys.stderr)
        return REFUSED
    try:
        os.execv(command[0], command)  # noqa: S606 - exec of the absolute argv, no shell
    except OSError as exc:  # e.g. the ruleset does not allow executing the command itself
        print(f"landlock_exec: refused: exec {command[0]}: {exc.strerror}", file=sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
