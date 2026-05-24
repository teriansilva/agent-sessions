"""Self-update (#65 Phase 5).

`check()` compares the running version to the latest on the chosen channel (the highest
`v*` tag for ``stable``, the remote ``main`` HEAD for ``main``). `apply()` performs the
update with **no user-supplied input** — it re-runs the installer detached, which builds
the channel's latest into a fresh atomic release, flips `current`, restarts, health-checks,
and rolls back on failure. The HTTP endpoints are authed + CSRF + origin-gated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .version import get_version

_DEFAULT_REPO = "https://github.com/teriansilva/agent-sessions.git"


def _repo_url() -> str:
    return os.environ.get("AGENT_SESSIONS_REPO") or _DEFAULT_REPO


def _channel() -> str:
    return os.environ.get("AGENT_SESSIONS_CHANNEL") or "stable"


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


def check() -> dict[str, object]:
    cur = get_version()
    channel = _channel()
    latest = latest_ref(channel, _repo_url())
    norm = latest.lstrip("v") if (latest and channel != "main") else latest
    available = bool(latest) and norm != cur and (latest or "") not in cur
    return {"current": cur, "channel": channel, "latest": latest, "update_available": available}


def installer_path() -> Path | None:
    p = _home() / "current" / "src" / "install.sh"
    return p if p.exists() else None


def apply() -> bool:
    """Run the installer detached to upgrade to the channel's latest (no user input).
    Returns False if the installer isn't found (e.g. a dev checkout, not an install)."""
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
    return True


def autoupdate() -> str:
    """Check the channel and apply only if an update is available (the timer entrypoint).
    Returns a short status string: 'up-to-date', 'applied', or 'unavailable'."""
    info = check()
    if not info["update_available"]:
        return "up-to-date"
    return "applied" if apply() else "unavailable"
