"""Self-update (#65 Phase 5; in-app auto-update #538).

`check()` compares the running version to the latest on the chosen channel (the highest
`v*` tag for ``stable``, the remote ``main`` HEAD for ``main``). `apply()` performs the
update with **no user-supplied input** — it re-runs the installer detached, which builds
the channel's latest into a fresh atomic release, flips `current`, restarts, health-checks,
and rolls back on failure. The HTTP endpoints are authed + CSRF + origin-gated.

Settings (#538): the auto-update opt-in and the release channel are persisted as the fixed
``AGENT_SESSIONS_AUTOUPDATE`` / ``AGENT_SESSIONS_CHANNEL`` keys in the install env file and
read **live** (env file first, process env fallback) — a Settings toggle applies without a
service restart, because the running service's ``os.environ`` snapshot predates the write.
``autoupdate()`` / ``apply_manual()`` share one single-flight lock so the daily loop and the
manual "Update now" can never spawn two installers concurrently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import discover, envfile
from .version import get_version

_DEFAULT_REPO = "https://github.com/teriansilva/agent-sessions.git"

AUTOUPDATE_KEY = "AGENT_SESSIONS_AUTOUPDATE"
CHANNEL_KEY = "AGENT_SESSIONS_CHANNEL"
CHANNELS = ("stable", "main")

# Single-flight (#538): one check/apply at a time, shared between the scheduled loop and
# the manual /api/update/apply path.
_RUN_LOCK = threading.Lock()
# After an installer spawn, the service is about to be restarted by that installer (or the
# spawn failed silently) — the scheduled path skips instead of stacking a second installer.
_SPAWNED_AT: float | None = None
_SPAWN_COOLDOWN_S = 15 * 60
# Recent-runtime status of the last SCHEDULED pass (#538): in-memory by design — a status
# hint for the Settings card, not an audit log. Resets on restart.
_LAST_AUTO: dict[str, object] | None = None


def _repo_url() -> str:
    return os.environ.get("AGENT_SESSIONS_REPO") or _DEFAULT_REPO


def _env_path() -> Path:
    return Path(os.environ.get("AGENT_SESSIONS_ENV_FILE") or discover.default_env_path())


def _env_get(key: str) -> str | None:
    """``KEY=value`` from the install env file — the live source of truth for settings the
    UI changes at runtime (#538). Fail-soft: absent/unreadable file → None."""
    try:
        for ln in _env_path().read_text().splitlines():
            if ln.startswith(f"{key}="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _channel() -> str:
    ch = _env_get(CHANNEL_KEY) or os.environ.get(CHANNEL_KEY) or "stable"
    return ch if ch in CHANNELS else "stable"


def auto_update_enabled() -> bool:
    v = _env_get(AUTOUPDATE_KEY)
    if v is None:
        v = os.environ.get(AUTOUPDATE_KEY) or ""
    return v.strip().lower() in ("1", "true", "yes")


def settings() -> dict[str, object]:
    """The public update settings — what the Settings card shows and POSTs."""
    return {"auto_update": auto_update_enabled(), "channel": _channel()}


def set_settings(
    *, auto_update: bool | None = None, channel: str | None = None
) -> dict[str, object]:
    """Persist the given settings to the env file (only the two fixed keys — this is NOT a
    generic env editor) and return the new public state. Raises ValueError on a channel
    outside ``CHANNELS``; callers validate types before calling."""
    updates: dict[str, str | None] = {}
    if auto_update is not None:
        updates[AUTOUPDATE_KEY] = "1" if auto_update else "0"
    if channel is not None:
        if channel not in CHANNELS:
            raise ValueError(f"channel must be one of {CHANNELS}")
        updates[CHANNEL_KEY] = channel
    if updates:
        path = _env_path()
        path.parent.mkdir(parents=True, exist_ok=True)  # dev checkouts have no install dir yet
        envfile.update(path, updates)
    return settings()


def last_auto() -> dict[str, object] | None:
    return dict(_LAST_AUTO) if _LAST_AUTO else None


def record_auto(result: str) -> None:
    global _LAST_AUTO
    _LAST_AUTO = {"ts": time.time(), "result": result}


def _home() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


def latest_ref(channel: str, repo_url: str) -> str | None:
    """The channel's latest ref on the remote: the highest ``v*`` tag (stable) or the
    ``main`` short SHA. None if git/network is unavailable."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        if channel == "main":
            out = subprocess.run(  # noqa: S603
                [git, "ls-remote", repo_url, "main"], capture_output=True, text=True, timeout=15
            )
            sha = out.stdout.split("\t", 1)[0].strip() if out.returncode == 0 else ""
            return sha[:7] or None
        out = subprocess.run(  # noqa: S603
            [git, "ls-remote", "--tags", "--refs", repo_url, "v*"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        tags = [ln.rsplit("/", 1)[-1] for ln in out.stdout.splitlines() if ln.strip()]
        tags = sorted(tags, key=_semver_key)
        return tags[-1] if tags else None
    except (OSError, subprocess.SubprocessError):
        return None


def _semver_key(tag: str) -> tuple[int, ...]:
    parts = tag.lstrip("v").split(".")
    out = []
    for p in parts:
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def _running_sha(version: str) -> str | None:
    """The commit the running build was built from, parsed from the version's local
    segment: setuptools_scm's ``+g<sha>`` (e.g. ``0.9.1.dev3+g64eefb3``) or the dev
    placeholder ``0.0.0+<sha>``. Returns lowercase hex, or None for a **clean release**
    version that carries no local segment (``0.9.0``) — nothing to compare a SHA against."""
    local = version.partition("+")[2]
    if not local:
        return None
    token = local.split(".", 1)[0]  # drop .dirty / .dYYYYMMDD suffixes
    if token[:1] == "g":  # setuptools_scm prefixes the git SHA with 'g'
        token = token[1:]
    token = token.lower()
    return token if token and all(c in "0123456789abcdef" for c in token) else None


def _main_update_available(cur: str, latest: str | None) -> bool:
    """`main`-channel availability (#583). Compare the running build's commit SHA to the
    remote HEAD SHA — **never** the SHA to the whole version string, which reported "update
    available" forever whenever main HEAD sat on a release tag (a clean ``0.9.0`` never
    *contains* the SHA, so the old ``latest not in cur`` heuristic was always true → a
    reinstall loop). A clean release version has no SHA to compare; treat it as current
    rather than perpetually behind — a false negative only restores "don't update", which
    is safe, and prod's intended posture on a tag is ``stable`` anyway."""
    if not latest:
        return False
    cur_sha = _running_sha(cur)
    if cur_sha is None:
        return False  # clean release sitting on main HEAD → converged, don't churn
    n = min(len(cur_sha), len(latest))
    return cur_sha[:n] != latest.lower()[:n]  # SHA↔SHA, tolerant of differing short lengths


def check() -> dict[str, object]:
    cur = get_version()
    channel = _channel()
    latest = latest_ref(channel, _repo_url())
    if channel == "main":
        available = _main_update_available(cur, latest)
    else:
        norm = latest.lstrip("v") if latest else latest
        available = bool(latest) and norm != cur
    return {"current": cur, "channel": channel, "latest": latest, "update_available": available}


def installer_path() -> Path | None:
    p = _home() / "current" / "src" / "install.sh"
    return p if p.exists() else None


def apply() -> bool:
    """Run the installer detached to upgrade to the channel's latest (no user input).
    Returns False if the installer isn't found (e.g. a dev checkout, not an install)."""
    global _SPAWNED_AT
    inst = installer_path()
    if inst is None:
        return False
    sh = shutil.which("sh") or "/bin/sh"
    env = {**os.environ, "AGENT_SESSIONS_REPO": _repo_url(), "AGENT_SESSIONS_CHANNEL": _channel()}
    # Self-update always moves to the CHANNEL's latest — never a pinned ref. Drop any
    # inherited AGENT_SESSIONS_REF (which the installer would otherwise prefer).
    env.pop("AGENT_SESSIONS_REF", None)
    subprocess.Popen(  # noqa: S603
        [sh, str(inst)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survive the service restart the installer triggers
    )
    _SPAWNED_AT = time.monotonic()
    return True


def apply_manual() -> str:
    """The manual "Update now" path: single-flight with the scheduled loop AND with a
    just-spawned installer. `apply()` returns right after the detached spawn while the
    installer keeps working through its build/restart window — without the cooldown a
    double-click or retried POST would stack a second installer (Hermes #539).
    Returns 'started' | 'busy' | 'unavailable'."""
    if not _RUN_LOCK.acquire(blocking=False):
        return "busy"
    try:
        if _SPAWNED_AT is not None and time.monotonic() - _SPAWNED_AT < _SPAWN_COOLDOWN_S:
            return "busy"
        return "started" if apply() else "unavailable"
    finally:
        _RUN_LOCK.release()


def autoupdate() -> str:
    """Check the channel and apply only if an update is available (the scheduled/CLI
    entrypoint). Returns 'up-to-date', 'applied', 'unavailable', or 'busy' (another
    check/apply holds the single-flight lock, or an installer spawned moments ago is
    still working through its restart window)."""
    if not _RUN_LOCK.acquire(blocking=False):
        return "busy"
    try:
        if _SPAWNED_AT is not None and time.monotonic() - _SPAWNED_AT < _SPAWN_COOLDOWN_S:
            return "busy"
        info = check()
        if not info["update_available"]:
            return "up-to-date"
        return "applied" if apply() else "unavailable"
    finally:
        _RUN_LOCK.release()
