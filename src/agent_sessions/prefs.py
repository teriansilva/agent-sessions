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


def coerce_str_list(value: object, cap: int = 2000) -> list[str]:
    """Narrow any input to a bounded list of unique strings (drops non-strings/dupes).
    Used for the overview's expanded/excluded path lists (#144)."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in value:
        if isinstance(v, str) and v not in seen:
            seen.add(v)
            out.append(v)
            if len(out) >= cap:
                break
    return out


def coerce_str_map(
    value: object, cap: int = 500, key_max: int = 4096, val_max: int = 80
) -> dict[str, str]:
    """Narrow any input to a bounded {str: str} map for custom project names (#148).
    Non-string keys/values are dropped; keys over key_max are dropped; values are trimmed
    and capped at val_max; an empty (after-trim) value drops the entry (clears the name).
    Applied on BOTH write and read so a malformed persisted map can't crash the app."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or len(k) > key_max:
            continue
        name = v.strip()[:val_max]
        if not name:
            continue
        out[k] = name
        if len(out) >= cap:
            break
    return out


def _set(key: str, value: str | list[str] | dict[str, str], path: Path | None = None):
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


def get_overview_expanded(path: Path | None = None) -> list[str]:
    """Project cwds whose overview cluster is expanded (default: none → collapsed) (#144)."""
    return coerce_str_list(_load(path or _default_path()).get("overview_expanded"))


def set_overview_expanded(cwds: object, path: Path | None = None) -> list[str]:
    """Persist the expanded-cluster cwds. Preserves other keys."""
    return _set("overview_expanded", coerce_str_list(cwds), path)


def get_overview_excluded(path: Path | None = None) -> list[str]:
    """Project cwds hidden from the overview map (#144)."""
    return coerce_str_list(_load(path or _default_path()).get("overview_excluded"))


def set_overview_excluded(cwds: object, path: Path | None = None) -> list[str]:
    """Persist the excluded-project cwds. Preserves other keys."""
    return _set("overview_excluded", coerce_str_list(cwds), path)


def get_project_names(path: Path | None = None) -> dict[str, str]:
    """Per-cwd custom display names for projects (#148). Normalized on read."""
    return coerce_str_map(_load(path or _default_path()).get("project_names"))


def set_project_names(names: object, path: Path | None = None) -> dict[str, str]:
    """Persist the custom project-name map (normalized; empty names drop entries)."""
    return _set("project_names", coerce_str_map(names), path)
