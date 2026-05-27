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

# Sidebar body: the session list, or the squeezed Session Overview map (#139).
SIDEBAR_VIEWS: tuple[str, ...] = ("list", "overview")
DEFAULT_SIDEBAR_VIEW = "list"


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


def coerce_sidebar_view(value: object) -> str:
    """Narrow any input to a known sidebar view, falling back to the default."""
    return value if isinstance(value, str) and value in SIDEBAR_VIEWS else DEFAULT_SIDEBAR_VIEW


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _set(key: str, value: str, path: Path | None = None) -> str:
    """Persist a single pref key. Read-modify-write under an exclusive flock so a concurrent
    writer (or a different key) can't clobber the rest of the document."""
    path = path or _default_path()
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
            data[key] = value
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return value


def get_theme(path: Path | None = None) -> str:
    """The persisted theme, or the default when unset/unreadable/invalid."""
    return coerce_theme(_load(path or _default_path()).get("theme"))


def set_theme(theme: str, path: Path | None = None) -> str:
    """Persist a theme (invalid input → default). Preserves other keys (e.g. sidebar_view)."""
    return _set("theme", coerce_theme(theme), path)


def get_sidebar_view(path: Path | None = None) -> str:
    """The persisted sidebar view (list|overview), or the default when unset/invalid."""
    return coerce_sidebar_view(_load(path or _default_path()).get("sidebar_view"))


def set_sidebar_view(view: str, path: Path | None = None) -> str:
    """Persist the sidebar view (invalid input → default). Preserves other keys (e.g. theme)."""
    return _set("sidebar_view", coerce_sidebar_view(view), path)
