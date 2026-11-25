"""Secure, atomic edits to the app env file (KEY=value lines).

The env file holds secrets (`AGENT_SESSIONS_SECRET_KEY`, the password hash), so writes
must never expose them: the temp file is created 0600 from the first byte (umask-
independent) and renamed into place atomically. Only the named keys are touched —
every other line is preserved verbatim.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


def update(path: Path, updates: Mapping[str, str | None]) -> None:
    """Set (``KEY=value``) or drop (value ``None``) each key in ``updates``, preserving
    all other lines. Atomic rename; result mode 0600."""
    lines = path.read_text().splitlines() if path.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else None
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val is not None:
                out.append(f"{key}={val}")
            # None → omit the line (unset the key)
        else:
            out.append(ln)
    for key, val in updates.items():
        if key not in seen and val is not None:
            out.append(f"{key}={val}")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.replace(tmp, path)  # atomic
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
