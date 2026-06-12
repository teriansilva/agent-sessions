"""Engine autodiscovery (#65 Phase 3).

Probe for the agent CLIs (claude, opencode, codex, gemini) and resolve each to a path
with a defined precedence: an explicit ``AGENT_SESSIONS_*_BIN`` (kept only if it still
executes) > ``PATH`` > known install dirs (+ the npm global bin for gemini-cli). The
``doctor`` command writes ONLY the ``*_BIN`` lines of the app env file — everything
else in the file is preserved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from . import envfile

ENGINES = ("claude", "opencode", "codex", "gemini")

# Command name on PATH + known install dirs to probe as a last resort.
_DIRS: dict[str, list[str]] = {
    "claude": ["~/.local/bin"],
    "opencode": ["~/.opencode/bin", "~/.local/bin"],
    "codex": ["~/.codex/bin", "~/.local/bin"],
    "gemini": ["~/.local/bin"],
}


def envvar(name: str) -> str:
    return f"AGENT_SESSIONS_{name.upper()}_BIN"


def default_env_path() -> Path:
    home = os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    return Path(home).expanduser() / "env"


def _is_exec(p: str | os.PathLike[str]) -> bool:
    path = Path(p)
    return path.is_file() and os.access(path, os.X_OK)


def _npm_global_bin() -> str | None:
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        out = subprocess.run(  # noqa: S603
            [npm, "prefix", "-g"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = out.stdout.strip()
    return f"{prefix}/bin" if out.returncode == 0 and prefix else None


def resolve(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve one engine's binary, or None if not present. Precedence: explicit env
    (if it executes) > PATH > known dirs."""
    env = os.environ if env is None else env
    explicit = env.get(envvar(name))
    if explicit and _is_exec(explicit):
        return explicit
    on_path = shutil.which(name)
    if on_path:
        return on_path
    dirs = list(_DIRS[name])
    if name == "gemini":
        npm = _npm_global_bin()
        if npm:
            dirs.append(npm)
    for d in dirs:
        cand = Path(d).expanduser() / name
        if _is_exec(cand):
            return str(cand)
    return None


def discover(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    env = os.environ if env is None else env
    return {name: resolve(name, env) for name in ENGINES}


def write_env_bins(env_path: Path, bins: Mapping[str, str | None]) -> None:
    """Rewrite only the ``*_BIN`` lines of ``env_path`` from ``bins`` (found → set,
    not-found → drop), preserving every other line. Secure atomic write (see envfile)."""
    envfile.update(env_path, {envvar(name): path for name, path in bins.items()})
