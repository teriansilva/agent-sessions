"""Open-or-switch wrapper around ``zellij action``.

**Shell-free by construction.** Every call uses ``subprocess.run([...], check=True)``
with a literal argv list — no shell-enabled subprocess, no inline shell wrappers,
no string interpolation that reaches a shell. UUID / cwd / tabname are validated
before they reach subprocess. The contract is pinned by ``tests/test_zellij.py``
and by a CI grep in ``.forgejo/workflows/pr-validate.yml``.

Tab naming:
- Claude (the original engine) keeps a bare ``<short-uuid>:<title>`` tab name.
- Other engines (opencode, via ``open_engine``/``new_engine``) carry an engine
  prefix, e.g. ``o:<short>:<title>``, so engines can't collide on tab lookup.

**Tab lookup is by the ``<prefix><short>:`` segment only**, so renaming a session
in the sidebar never produces a duplicate Zellij tab when the existing one still
carries the old title. The actual ``claude`` / ``opencode`` argv is built by the
caller (see ``engines.py``) and passed in — this module only manages tabs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
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
_SHORT_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")  # tab-lookup discriminator (engine-agnostic)
_TAB_MAX = 64


class ZellijError(RuntimeError):
    """Raised on bad input or non-zero `zellij action` exit."""


def _safe_title(title: str) -> str:
    return _TAB_SAFE_RE.sub("_", title).strip() or "session"


def sanitize_tab_name(short_uuid: str, title: str) -> str:
    """Compose a (bare) Claude tab name: short UUID prefix + sanitized title, ≤64."""
    if not re.match(r"^[0-9a-f]{8}$", short_uuid):
        raise ZellijError(f"bad short uuid: {short_uuid!r}")
    return f"{short_uuid}:{_safe_title(title)}"[:_TAB_MAX]


def sanitize_engine_tab(engine_prefix: str, short: str, title: str) -> str:
    """Compose an engine-prefixed tab name ``<prefix>:<short>:<title>`` (≤64).

    ``engine_prefix`` empty → bare ``<short>:<title>`` (the Claude convention).
    """
    if not _SHORT_RE.match(short):
        raise ZellijError(f"bad short id: {short!r}")
    head = f"{engine_prefix}:{short}" if engine_prefix else short
    return f"{head}:{_safe_title(title)}"[:_TAB_MAX]


def _query_tab_names(*, _runner=subprocess.run, _attempts: int = 3) -> list[str]:
    """Query Zellij tab names, retrying transient failures before giving up.

    Returns the tab names on success, or ``[]`` only when the session genuinely
    has no tabs / does not exist yet. Raises :class:`ZellijError` if the session
    can't be reached after ``_attempts`` tries.

    The strict contract matters: a *transient* failure (the scripted ``action``
    runs as an ephemeral client and can time out under load) must NOT be confused
    with "no tabs exist" — that mistake makes :func:`_switch_or_new` spawn a
    *duplicate* tab, so a single session ends up with several concurrent
    ``claude --resume`` writers (session mixing) and ambiguous tab names that
    ``go-to-tab-name`` can't reliably focus.
    """
    last: Exception | None = None
    for n in range(_attempts):
        try:
            cp = _runner(
                [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "query-tab-names"],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        except FileNotFoundError:
            return []  # zellij binary missing → nothing to switch to
        except subprocess.TimeoutExpired as e:
            last = e
        else:
            if cp.returncode == 0:
                return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
            err = (cp.stderr or "").lower()
            absent = "not found" in err or "no such" in err or "doesn't exist" in err
            if "session" in err and absent:
                return []  # session genuinely absent
            last = ZellijError(
                f"query-tab-names exit {cp.returncode}: {(cp.stderr or '').strip()!r}"
            )
        if n + 1 < _attempts:
            time.sleep(0.25)
    if isinstance(last, ZellijError):
        raise last
    raise ZellijError(f"query-tab-names unreachable after {_attempts} tries: {last}")


def list_tabs(*, _runner=subprocess.run) -> list[str]:
    """Return current Zellij tab names. Lenient: ``[]`` on any failure.

    For callers that only *display* tabs. Switch logic uses :func:`_query_tab_names`
    directly so a transient failure can't be mistaken for an empty session.
    """
    try:
        return _query_tab_names(_runner=_runner)
    except ZellijError:
        return []


def _claude_argv(*, resume_uuid: str | None, bypass: bool) -> list[str]:
    """Build the claude launch argv. Single shared launch-option path for both
    resume and new-session (one place owns ``--dangerously-skip-permissions``)."""
    argv = [CLAUDE_BIN]
    if resume_uuid is not None:
        argv += ["--resume", resume_uuid]
    if bypass:
        # Skips workspace-trust + tool-permission prompts. Default-on per the
        # operator's request; this is a single-user tool on the operator's host.
        argv.append("--dangerously-skip-permissions")
    return argv


def _switch_or_new(
    *, desired_tab: str, lookup_prefixes: Iterable[str], cwd: str, argv: list[str], _runner
) -> str:
    """Switch to an existing tab matching any ``lookup_prefixes`` or spawn a new one.

    Shared core for every engine. ``argv`` is the already-built, validated launch
    command; no shell is ever invoked. ``--`` separates zellij flags from argv.
    """
    prefixes = tuple(lookup_prefixes)
    # Strict query: a transient failure raises rather than looking like "no tabs",
    # so we never spawn a duplicate tab for a session that already has one.
    names = _query_tab_names(_runner=_runner)
    matches = [(i, n) for i, n in enumerate(names, start=1) if n.startswith(prefixes)]
    if matches:
        # Self-heal any duplicate tabs that earlier races left behind: keep the
        # first match, close the rest (highest index first so lower indices stay
        # valid). Closing a tab kills its pane process, so the redundant
        # `claude --resume` writers stop — one session == one tab from here on.
        for idx, _name in reversed(matches[1:]):
            _runner(
                [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "go-to-tab", str(idx)],
                check=False,
                timeout=5,
            )
            _runner(
                [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "close-tab"],
                check=False,
                timeout=5,
            )
        # With duplicates gone the survivor's name is unambiguous, so go-to-tab-name
        # reliably focuses it (the ambiguity was why switching took 2-3 clicks).
        survivor = matches[0][1]
        _runner(
            [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "go-to-tab-name", survivor],
            check=True,
            timeout=5,
        )
        if survivor != desired_tab:
            _runner(
                [ZELLIJ_BIN, "--session", ZELLIJ_SESSION, "action", "rename-tab", desired_tab],
                check=False,
                timeout=5,
            )
        return desired_tab

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
            *argv,
        ],
        check=True,
        timeout=10,
    )
    return desired_tab


def _spawn_new(*, tab: str, cwd: str, argv: list[str], _runner) -> str:
    """Spawn a brand-new tab running ``argv`` in ``cwd``. Shell-free."""
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
            tab,
            "--",
            *argv,
        ],
        check=True,
        timeout=10,
    )
    return tab


def open_or_switch(
    *,
    uuid: str,
    cwd: str,
    title: str,
    allowed_cwds: Iterable[str],
    bypass: bool = True,
    _runner=subprocess.run,
) -> str:
    """Switch to the existing Zellij tab for a Claude ``uuid`` or create a new one.

    Returns the tab name that's now active. With ``bypass`` (default True) the
    resumed ``claude`` skips the workspace-trust prompt. All boundaries are
    validated argv-side; no shell layer is ever invoked.
    """
    if not _UUID_RE.match(uuid):
        raise ZellijError(f"uuid does not match UUID shape: {uuid!r}")
    if cwd not in set(allowed_cwds):
        raise ZellijError(f"cwd not in scanned session set: {cwd!r}")
    short = uuid[:8]
    return _switch_or_new(
        desired_tab=sanitize_tab_name(short, title),
        lookup_prefixes=[f"{short}:"],
        cwd=cwd,
        argv=_claude_argv(resume_uuid=uuid, bypass=bypass),
        _runner=_runner,
    )


def new_session(
    *,
    cwd: str,
    title: str,
    allowed_cwds: Iterable[str],
    bypass: bool = True,
    _runner=subprocess.run,
) -> str:
    """Spawn a brand-new ``claude`` session (no --resume) in ``cwd``.

    ``cwd`` must be in ``allowed_cwds`` (the picker set). Shell-free argv;
    ``bypass`` (default True) adds ``--dangerously-skip-permissions``.
    """
    if cwd not in set(allowed_cwds):
        raise ZellijError(f"cwd not in allowed project set: {cwd!r}")
    tab = f"new:{_safe_title(title)}"[:_TAB_MAX]
    return _spawn_new(
        tab=tab, cwd=cwd, argv=_claude_argv(resume_uuid=None, bypass=bypass), _runner=_runner
    )


def open_engine(
    *,
    engine_prefix: str,
    short: str,
    cwd: str,
    title: str,
    allowed_cwds: Iterable[str],
    argv: Iterable[str],
    _runner=subprocess.run,
) -> str:
    """Engine-agnostic open-or-switch: caller supplies the launch ``argv`` and the
    tab ``engine_prefix`` (e.g. ``"o"`` for opencode). ``short`` is the per-engine
    lookup discriminator. ``cwd`` must be in ``allowed_cwds``. Shell-free.
    """
    if cwd not in set(allowed_cwds):
        raise ZellijError(f"cwd not in scanned session set: {cwd!r}")
    argv = [str(a) for a in argv]
    if not argv:
        raise ZellijError("empty argv")
    desired = sanitize_engine_tab(engine_prefix, short, title)
    lookup = f"{engine_prefix}:{short}:" if engine_prefix else f"{short}:"
    return _switch_or_new(
        desired_tab=desired, lookup_prefixes=[lookup], cwd=cwd, argv=argv, _runner=_runner
    )


def new_engine(
    *,
    engine_prefix: str,
    cwd: str,
    title: str,
    allowed_cwds: Iterable[str],
    argv: Iterable[str],
    _runner=subprocess.run,
) -> str:
    """Engine-agnostic new-session spawn. Caller supplies the launch ``argv``."""
    if cwd not in set(allowed_cwds):
        raise ZellijError(f"cwd not in allowed project set: {cwd!r}")
    argv = [str(a) for a in argv]
    if not argv:
        raise ZellijError("empty argv")
    head = f"{engine_prefix}:new" if engine_prefix else "new"
    tab = f"{head}:{_safe_title(title)}"[:_TAB_MAX]
    return _spawn_new(tab=tab, cwd=cwd, argv=argv, _runner=_runner)


__all__ = [
    "open_or_switch",
    "new_session",
    "open_engine",
    "new_engine",
    "list_tabs",
    "sanitize_tab_name",
    "sanitize_engine_tab",
    "ZellijError",
]
