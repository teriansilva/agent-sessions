"""App preferences — small, user-facing settings the UI persists server-side.

Backed by ``~/.config/agent-sessions/prefs.json`` (override: ``AGENT_SESSIONS_PREFS``).
Deliberately a *separate* file from the session metadata sidecar (metadata.py) and the
env file (boot config / secrets): this is per-app UI state, not session data or secrets.

Single-admin app → a flat ``{"theme": …}`` document, no per-user keying. Concurrent
writers serialize on ``fcntl.flock`` (same approach as metadata.py). Reads tolerate a
missing/empty/corrupt file by returning defaults.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

# Mirror of web/src/theme/themes.ts THEME_IDS. Kept in sync by
# tests/test_prefs.py (server) + the SPA registry test (client).
THEMES: tuple[str, ...] = ("royal", "dark", "light")
DEFAULT_THEME = "royal"


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_PREFS",
            str(Path.home() / ".config" / "agent-sessions" / "prefs.json"),
        )
    )


def coerce_theme(value: object) -> str:
    """Narrow any input to a known theme id, falling back to the default."""
    return value if isinstance(value, str) and value in THEMES else DEFAULT_THEME


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def get_theme(path: Path | None = None) -> str:
    """The persisted theme, or the default when unset/unreadable/invalid."""
    return coerce_theme(_load(path or _default_path()).get("theme"))


def set_theme(theme: str, path: Path | None = None) -> str:
    """Persist a theme. Returns the stored (coerced) value. Invalid input → default.

    Read-modify-write under an exclusive flock so a concurrent writer can't clobber
    other keys we might add later.
    """
    path = path or _default_path()
    theme = coerce_theme(theme)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            try:
                data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except json.JSONDecodeError:
                data = {}
            data["theme"] = theme
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return theme
