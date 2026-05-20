"""Open-or-switch wrapper around ``zellij action``.

**Shell-free by construction.** Every call uses ``subprocess.run([...], check=True)``
with a literal argv list — no shell-enabled subprocess, no inline shell wrappers,
no string interpolation that reaches a shell. UUID / cwd / tabname are validated
before they reach subprocess. The contract is pinned by ``tests/test_zellij.py``
and by a CI grep in ``.forgejo/workflows/pr-validate.yml``.

Tab name convention: ``<short-uuid>:<sanitized-title>``. **Tab lookup is by
short-UUID prefix only**, so renaming a session in the sidebar never produces a
duplicate Zellij tab when the existing one still carries the old title.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable


def _resolve_bin(env_var: str, name: str, fallback: str) -> str:
    """Pick a binary path: explicit env override → PATH lookup → literal fallback.

    Keeps the app honest about *which* binary it will exec, and lets the deploy
    pin exact paths via env (e.g. AGENT_SESSIONS_CLAUDE_BIN) without hardcoding
    a home-dir path that differs per host.
    """
    return os.environ.get(env_var) or shutil.which(name) or fallback


ZELLIJ_BIN = _resolve_bin("AGENT_SESSIONS_ZELLIJ_BIN", "zellij", "/usr/local/bin/zellij")
CLAUDE_BIN = _resolve_bin("AGENT_SESSIONS_CLAUDE_BIN", "claude", "claude")
ZELLIJ_SESSION = os.environ.get("AGENT_SESSIONS_ZELLIJ_SESSION", "agent-main")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TAB_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._:-]")
_TAB_MAX = 64


class ZellijError(RuntimeError):
    """Raised on bad input or non-zero `zellij action` exit."""


def sanitize_tab_name(short_uuid: str, title: str) -> str:
    """Compose a tab name from the short UUID prefix + sanitized title, truncated to 64."""
    if not re.match(r"^[0-9a-f]{8}$", short_uuid):
        raise ZellijError(f"bad short uuid: {short_uuid!r}")
    safe_title = _TAB_SAFE_RE.sub("_", title).strip() or "session"
    raw = f"{short_uuid}:{safe_title}"
    return raw[:_TAB_MAX]


def list_tabs(*, _runner=subprocess.run) -> list[str]:
    """Return current Zellij tab names by parsing ``zellij list-tabs``.

    Tolerates the session not existing yet (returns []).
    """
    try:
        cp = _runner(
            [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "query-tab-names"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]


def open_or_switch(
    *,
    uuid: str,
    cwd: str,
    title: str,
    allowed_cwds: Iterable[str],
    _runner=subprocess.run,
) -> str:
    """Switch to the existing Zellij tab for ``uuid`` or create a new one.

    Returns the tab name that's now active.

    All boundaries are validated argv-side; no shell layer is ever invoked.
    """
    if not _UUID_RE.match(uuid):
        raise ZellijError(f"uuid does not match UUID shape: {uuid!r}")
    if cwd not in set(allowed_cwds):
        raise ZellijError(f"cwd not in scanned session set: {cwd!r}")

    short = uuid[:8]
    desired_tab = sanitize_tab_name(short, title)

    # Lookup by short-UUID prefix so a rename can't create a duplicate.
    prefix = f"{short}:"
    existing = [t for t in list_tabs(_runner=_runner) if t.startswith(prefix)]

    if existing:
        _runner(
            [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "go-to-tab-name", existing[0]],
            check=True,
            timeout=5,
        )
        # Best-effort live rename when the sidebar title diverges from the tab.
        if existing[0] != desired_tab:
            _runner(
                [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "rename-tab", desired_tab],
                check=False,
                timeout=5,
            )
        return desired_tab

    # New tab. Note: ``--`` separates zellij flags from the command argv list.
    _runner(
        [
            ZELLIJ_BIN,
            "--session",
            ZELLIJ_SESSION,
            "action",
            "new-tab",
            "--cwd",
            cwd,
            "--name",
            desired_tab,
            "--",
            CLAUDE_BIN,
            "--resume",
            uuid,
        ],
        check=True,
        timeout=10,
    )
    return desired_tab


__all__ = ["open_or_switch", "list_tabs", "sanitize_tab_name", "ZellijError"]
