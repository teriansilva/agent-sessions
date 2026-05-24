"""Runtime version string.

Canonical source is the installed distribution's metadata (the installer pins a
tagged release → that tag shows here). In a source/dev checkout the distribution
version is the placeholder ``0.0.0``; there we append the git short SHA so dev builds
are still identifiable (e.g. ``0.0.0+ab12cd3``).
"""

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

_DEV = "0.0.0"


def _git_sha() -> str | None:
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def get_version() -> str:
    try:
        v = _dist_version("agent-sessions")
    except PackageNotFoundError:
        v = _DEV
    if v and v != _DEV:
        return v
    sha = _git_sha()
    return f"{_DEV}+{sha}" if sha else _DEV
