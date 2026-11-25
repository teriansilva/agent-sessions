"""Recorder for the `shell_backspace_*.raw` rings (#2058). Not run by the tests.

    python3 tests/fixtures/shell_backspace_record.py <output dir>

Each ring is every byte a real `bash` wrote to a private PTY of a fixed geometry while this
script typed at it, one key at a time, the way a person at a keyboard does. The shell is
`/bin/bash --norc --noprofile -i` under `env -i` with only `PS1='$ '`, `TERM` and `PATH` set, in
`/`, so no host name, user name or path is in any byte. See `shell_backspace.PROVENANCE.md`.

It also writes the two `claude_permission_bash_backspace_*.raw` rings: the checked-in claude
permission capture followed by a tail (`ADVERSARIAL`) that uses backspaces to rewrite part of the
dialog in place.

With `--tmux` it writes, beside every ring, `<name>.tmux.txt`: what tmux shows after being fed the
same bytes at the same geometry (a private server on its own socket, never the operator's), as
`capture-pane -p` prints it, trailing blank rows dropped.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

DEL = b"\x7f"  # what the Backspace key sends

#: name -> (rows, cols, TERM, keys). Every `bytes` item is typed one byte at a time.
RINGS: dict[str, tuple[int, int, str, list[bytes]]] = {
    # The line editor erases at the end of a line with `BS SP BS`, on a terminal with `el`...
    "shell_backspace_edit": (24, 80, "xterm", [b"echo pasword", DEL * 4, b"sword\r"]),
    # ...and on one without it (the same bytes, without bracketed paste).
    "shell_backspace_edit_dumb": (24, 80, "dumb", [b"echo pasword", DEL * 4, b"sword\r"]),
    # A program writes BS at column 0, and BS past the start of what it wrote.
    "shell_backspace_col0": (24, 80, "xterm", [b"printf '\\b\\bab\\b\\b\\b\\bX\\n'\r"]),
    # A line typed past the right edge of a 20-column screen, erased back across the wrap,
    # retyped. The editor moves up itself: it does not rely on reverse wrap.
    "shell_backspace_wrap": (
        6,
        20,
        "xterm",
        [b"echo abcdefghijklmnopqrstuvwxyz", DEL * 20, b"REST\r"],
    ),
    # The typed line ends exactly in the last column, then one character is erased.
    "shell_backspace_margin": (6, 20, "xterm", [b"echo abcdefghijklm", DEL, b"\r"]),
}


def _drain(fd: int, quiet: float) -> bytes:
    out = b""
    while select.select([fd], [], [], quiet)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


#: Tails appended to `claude_permission_bash.raw` (100 x 40).
ADVERSARIAL = {
    # Go to the end of `1. Yes` (row 20) and of `3. No` (row 23) and rewrite each label over
    # itself with backspaces. The screen then offers `1. No` and `3. Yes`.
    "claude_permission_bash_backspace_swap": (b"\x1b[20;10H\x08\x08\x08No \x1b[23;9H\x08\x08Yes"),
    # Go to the end of the command (row 16) and rewrite its last five characters: the dialog
    # now asks about `... touch probe-dir-1213/x.sh `, with every option as claude drew it.
    "claude_permission_bash_backspace_command": b"\x1b[16;57H\x08\x08\x08\x08\x08x.sh ",
}


def record(rows: int, cols: int, term: str, keys: list[bytes]) -> bytes:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = ["/usr/bin/env", "-i", "PS1=$ ", f"TERM={term}", "PATH=/usr/bin:/bin"]
    proc = subprocess.Popen(
        [*env, "/bin/bash", "--norc", "--noprofile", "-i"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd="/",
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    out = _drain(master, 1.0)  # the first prompt
    for burst in keys:
        for i in range(len(burst)):
            os.write(master, burst[i : i + 1])
            out += _drain(master, 0.15)
        out += _drain(master, 0.5)
    os.write(master, b"exit\r")
    proc.wait(timeout=10)
    os.close(master)
    return out  # nothing is read after `exit` is typed: the ring ends at the last prompt


def tmux_view(raw_path: Path, rows: int, cols: int) -> str:
    sock = f"bs2058-{uuid.uuid4().hex[:8]}"
    base = ["tmux", "-u", "-L", sock, "-f", "/dev/null"]
    shell = f"stty raw -echo; cat {raw_path}; sleep 30"
    try:
        subprocess.run(
            [*base, "new-session", "-d", "-x", str(cols), "-y", str(rows), "sh", "-c", shell],
            check=True,
        )
        time.sleep(1.0)
        shown = subprocess.run(
            [*base, "capture-pane", "-p"], check=True, capture_output=True, text=True
        ).stdout
        return shown.rstrip("\n") + "\n"
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True)


#: BS in the pending-autowrap state, on a 5 x 3 screen. Printed with `--tmux`, never asserted:
#: tmux is not the reference #2058 names for this state (see the provenance file).
MARGIN = {
    "no BS": b"abcdeX",
    "one BS": b"abcde\x08X",
    "two BS": b"abcde\x08\x08X",
    "seven BS": b"abcde" + b"\x08" * 7 + b"X",
    "one BS, two characters": b"abcde\x08XY",
    "the erase idiom": b"abcde\x08 \x08Z",
}


def main() -> None:
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[tuple[str, int, int]] = []
    for name, (rows, cols, term, keys) in RINGS.items():
        (out_dir / f"{name}.raw").write_bytes(record(rows, cols, term, keys))
        made.append((name, rows, cols))
    dialog = (Path(__file__).parent / "claude_permission_bash.raw").read_bytes()
    for name, tail in ADVERSARIAL.items():
        (out_dir / f"{name}.raw").write_bytes(dialog + tail)
        made.append((name, 40, 100))
    for name, rows, cols in made:
        path = (out_dir / f"{name}.raw").resolve()
        print(f"{name}: {path.stat().st_size} bytes, {rows}x{cols}")
        if "--tmux" in sys.argv:
            path.with_suffix(".tmux.txt").write_text(tmux_view(path, rows, cols))
    if "--tmux" in sys.argv:
        probe = (out_dir / "margin-probe.bin").resolve()
        for label, data in MARGIN.items():
            probe.write_bytes(data)
            print(f"margin, {label}: {data!r} -> {tmux_view(probe, 3, 5).splitlines()}")
        probe.unlink()


if __name__ == "__main__":
    main()
