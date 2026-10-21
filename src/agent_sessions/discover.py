"""Engine autodiscovery (#65 Phase 3).

Probe for every engine's CLI and resolve each to a path with a defined precedence: an explicit
``AGENT_SESSIONS_*_BIN`` (kept only if it still executes) > ``PATH`` > the manifest's
``binary.search_paths`` (+ the npm global bin where ``binary.search_npm_global``). The
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


def _manifest(name: str):
    from . import engines

    m = engines.manifest_of(name)
    if m is None:
        raise KeyError(name)
    return m


def engine_ids() -> list[str]:
    """Every engine `doctor` looks for: the roster, in display order (#853 P3). No list here.
    Only engines that run a binary — a `chat` engine (#1209) has nothing to discover."""
    from . import engines

    return [e for e in engines.engine_ids() if _manifest(e).binary is not None]


def _bin_name(name: str) -> str:
    """The CLI's binary name — the manifest's `binary.name` (antigravity's is `agy`, shell's is
    `bash`). The PATH/dir probe and the env var key both derive from it."""
    return _manifest(name).binary.name


def _search_dirs(name: str) -> list[str]:
    """Known install dirs to probe as a last resort: the manifest's `binary.search_paths`."""
    return list(_manifest(name).binary.search_paths)


def _searches_npm_global(name: str) -> bool:
    return _manifest(name).binary.search_npm_global


def envvar(name: str) -> str:
    """The `*_BIN` knob `doctor` writes — the manifest's `binary.env_var`, which is also the one
    the launcher reads (§2b), so the two can never name different variables."""
    m = _manifest(name)
    return m.binary.env_var or f"AGENT_SESSIONS_{m.binary.name.upper()}_BIN"


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
    if _manifest(name).binary is None:
        return None  # a `chat` engine (#1209) runs no binary
    binary = _bin_name(name)
    explicit = env.get(envvar(name))
    if explicit and _is_exec(explicit):
        return explicit
    on_path = shutil.which(binary)
    if on_path:
        return on_path
    dirs = _search_dirs(name)
    if _searches_npm_global(name):
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
    return {name: resolve(name, env) for name in engine_ids()}


def write_env_bins(env_path: Path, bins: Mapping[str, str | None]) -> None:
    """Rewrite only the ``*_BIN`` lines of ``env_path`` from ``bins`` (found → set,
    not-found → drop), preserving every other line. Secure atomic write (see envfile)."""
    envfile.update(env_path, {envvar(name): path for name, path in bins.items()})
