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

ENGINES = ("claude", "opencode", "codex", "gemini", "antigravity", "kimi", "shell")

# Engines whose CLI binary name differs from the engine id. antigravity's binary is ``agy`` and the
# shell engine's binary is ``bash`` (#636); every other engine's binary matches its id. The
# PATH/dir probe AND the env var key derive from the *binary* name, so antigravity's knob is
# ``AGENT_SESSIONS_AGY_BIN`` and shell's is ``AGENT_SESSIONS_BASH_BIN`` (matches ``base.BASH_BIN``).
_BIN_NAME: dict[str, str] = {"antigravity": "agy", "shell": "bash"}

# Command name on PATH + known install dirs to probe as a last resort.
_DIRS: dict[str, list[str]] = {
    "claude": ["~/.local/bin"],
    "opencode": ["~/.opencode/bin", "~/.local/bin"],
    "codex": ["~/.codex/bin", "~/.local/bin"],
    "gemini": ["~/.local/bin"],
    # agy is a single Go binary from the curl installer (not npm); it lands in ~/.local/bin.
    "antigravity": ["~/.local/bin"],
    # kimi's curl installer drops a single native binary in ~/.kimi-code/bin and only appends that
    # dir to the shell rc — so a service started before the rc was re-sourced won't see it on PATH,
    # making this dir probe the one that usually resolves it. NOT in _NPM_GLOBAL_ENGINES: upstream
    # ships an npm fallback for musl hosts, but its executable path is unverified here (#714).
    "kimi": ["~/.kimi-code/bin", "~/.local/bin"],
    # bash is a base system binary — PATH resolves it; the extra dirs cover a non-PATH shell.
    "shell": ["/bin", "/usr/bin"],
}

_NPM_GLOBAL_ENGINES = frozenset({"codex", "gemini"})


def _bin_name(name: str) -> str:
    return _BIN_NAME.get(name, name)


def envvar(name: str) -> str:
    return f"AGENT_SESSIONS_{_bin_name(name).upper()}_BIN"


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
    binary = _bin_name(name)
    explicit = env.get(envvar(name))
    if explicit and _is_exec(explicit):
        return explicit
    on_path = shutil.which(binary)
    if on_path:
        return on_path
    dirs = list(_DIRS[name])
    if name in _NPM_GLOBAL_ENGINES:
        npm = _npm_global_bin()
        if npm:
            dirs.append(npm)
    for d in dirs:
        cand = Path(d).expanduser() / binary
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
