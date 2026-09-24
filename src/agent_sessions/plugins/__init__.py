"""Agents as plugins (#853): one declarative manifest per agent instead of a provider class.

P1 ships the contract — schema, validator, executable provenance and `PluginProvider` — and a
loader that reads manifests from two places:

- **first-party**: `plugins/first_party/<id>/plugin.toml` inside this package — reviewed, in-tree;
- **local**: `<plugins home>/<id>/plugin.toml` — the operator's directory, trust level `local`.

Loading is **fail-soft per plugin**: one invalid manifest becomes a `problems` entry naming the
field, and every other plugin loads.

**The live roster reads `load_first_party()` only** (#853 P2). Local manifests stay unwired until
install (P5) and operator confirmation (P6) exist: without them, the only way a local plugin could
run would be an unconfirmed file drop.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import kinds, provenance
from .manifest import MANIFEST_NAMES, Manifest, ManifestError, load_fd, load_file, parse
from .provider import PluginProvider, read_record, register_layout

__all__ = [
    "FIRST_PARTY_DIR",
    "LoadResult",
    "Manifest",
    "ManifestError",
    "PluginProvider",
    "kinds",
    "load_all",
    "load_first_party",
    "load_file",
    "parse",
    "plugin_state_home",
    "plugins_home",
    "provenance",
    "read_record",
    "register_layout",
]

FIRST_PARTY_DIR = Path(__file__).parent / "first_party"


def _app_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions").expanduser()


def plugins_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("AGENT_SESSIONS_PLUGINS_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return _app_home(env) / "plugins"


def plugin_state_home(env: Mapping[str, str] | None = None) -> Path:
    """Where install / confirmation records live: BattleLab's own state, a SIBLING of the plugins
    directory and never inside it — so nothing unpacked into a plugin tree can be a record."""
    env = os.environ if env is None else env
    explicit = env.get("AGENT_SESSIONS_PLUGIN_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return _app_home(env) / "plugin-state"


def _usable_state_dir(state: Path, *plugin_dirs: Path) -> Path | None:
    """The state dir, unless it sits inside (or is) a directory plugins are loaded from — then no
    record is trusted at all rather than one a bundle could have shipped."""
    s = os.path.realpath(state)
    for d in plugin_dirs:
        r = os.path.realpath(d)
        if s == r or s.startswith(r.rstrip("/") + "/"):
            return None
    return state


@dataclass
class LoadResult:
    providers: dict[str, PluginProvider] = field(default_factory=dict)
    #: key → reason. Key is the plugin directory (or id) the problem belongs to.
    problems: dict[str, str] = field(default_factory=dict)


def _manifest_path(d: Path) -> Path:
    found = [d / n for n in MANIFEST_NAMES if (d / n).exists() or (d / n).is_symlink()]
    if not found:
        raise ManifestError("", f"no {' or '.join(MANIFEST_NAMES)} in {d.name}/")
    if len(found) > 1:
        raise ManifestError("", f"both {' and '.join(MANIFEST_NAMES)} in {d.name}/; keep one")
    return found[0]


def _load_local_manifest(p: Path, *, source: str) -> Manifest:
    """A local manifest decides what runs, so it is read through `open_verified`: every directory
    walked and the file itself checked on their descriptors, and the manifest parsed from that same
    descriptor — never re-opened by path after the check (Hermes on PR #1112)."""
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ManifestError("", f"{p.name} must be a regular file, not a symlink")
    try:
        fd, _ = provenance.open_verified(str(p))
    except provenance.ProvenanceError as e:
        raise ManifestError("", f"{p.name} must be writable only by the operator ({e})") from None
    try:
        return load_fd(fd, name=p.name, source=source)
    finally:
        os.close(fd)


def _load_dir(
    base: Path, trust: str, result: LoadResult, env, home, state_dir: Path | None
) -> None:
    try:
        if not base.is_dir():
            return
        entries = sorted(base.iterdir())
    except OSError as e:
        # An unreadable plugins directory is one problem, not a crashed loader (review of #1112).
        result.problems[f"{trust}:{base}"] = f"cannot read the plugins directory ({e.strerror})"
        return
    reserved = set(kinds.RESERVED_IDS)
    try:
        reserved |= {p.name for p in FIRST_PARTY_DIR.iterdir() if p.is_dir()}
    except OSError:
        pass
    for d in entries:
        if d.name.startswith(".") or d.name == "__pycache__":
            continue
        key = f"{trust}:{d.name}"
        try:
            if d.is_symlink() or not d.is_dir():
                raise ManifestError("", f"{d.name} must be a plugin directory")
            p = _manifest_path(d)
            if trust == provenance.LOCAL:
                m = _load_local_manifest(p, source=f"{trust}:{p}")
            else:
                m = load_file(p, source=f"{trust}:{p}")
            if m.id != d.name:
                raise ManifestError(
                    "identity.id", f"{m.id!r} does not match its directory {d.name!r}"
                )
            provenance.check_vocabulary(m, trust)
            if trust == provenance.LOCAL and m.id in reserved:
                raise ManifestError("identity.id", f"{m.id!r} is reserved for an in-tree engine")
            if m.id in result.providers:
                raise ManifestError(
                    "identity.id", f"{m.id!r} is already provided by an in-tree plugin"
                )
            if m.session_id.legacy_bare_id and any(
                p.manifest.session_id.legacy_bare_id for p in result.providers.values()
            ):
                raise ManifestError(
                    "session_id.legacy_bare_id", "another plugin already claims bare ids"
                )
            if trust == provenance.LOCAL and m.session_id.legacy_bare_id:
                raise ManifestError(
                    "session_id.legacy_bare_id", "only an in-tree plugin may claim bare ids"
                )
            # A local plugin's root is the directory it was loaded from (its record and any install
            # live beside its manifest); an in-tree plugin's root is its slot in the plugins home.
            root = d if trust == provenance.LOCAL else plugins_home(env) / m.id
            result.providers[m.id] = PluginProvider(
                m, trust=trust, root=root, env=env, home=home, state_dir=state_dir
            )
        except (ManifestError, provenance.ProvenanceError, OSError) as e:
            result.problems[key] = str(e)
        except Exception as e:  # noqa: BLE001 — fail-soft per plugin is the loader's contract
            # Anything the validator did not anticipate is still one plugin's problem: a single
            # malformed manifest must never stop the roster from returning (Hermes on PR #1112).
            result.problems[key] = f"could not be loaded ({type(e).__name__})"


def _ordered(result: LoadResult) -> LoadResult:
    result.providers = dict(
        sorted(result.providers.items(), key=lambda kv: (kv[1].manifest.display.order, kv[0]))
    )
    return result


def load_first_party(
    *,
    first_party_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> LoadResult:
    """The in-tree manifests only, in roster order — what `engines.registry` is built from."""
    result = LoadResult()
    fp = first_party_dir or FIRST_PARTY_DIR
    try:
        state = _usable_state_dir(plugin_state_home(env), fp, plugins_home(env))
    except (OSError, RuntimeError) as e:
        result.problems["plugins"] = f"cannot locate the plugin state directory ({e})"
        state = None
    _load_dir(fp, provenance.FIRST_PARTY, result, env, home, state)
    return _ordered(result)


def load_all(
    *,
    first_party_dir: Path | None = None,
    local_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    state_dir: Path | None = None,
) -> LoadResult:
    """Load every manifest, first-party before local. Never raises for a bad manifest — nor for
    a plugins or state directory it cannot even name or read."""
    result = LoadResult()
    fp = first_party_dir or FIRST_PARTY_DIR
    try:
        loc = local_dir or plugins_home(env)
        state = _usable_state_dir(state_dir or plugin_state_home(env), fp, loc)
    except (OSError, RuntimeError) as e:  # e.g. `~otheruser` that expanduser cannot resolve
        result.problems["plugins"] = f"cannot locate the plugins directories ({e})"
        loc, state = None, None
    _load_dir(fp, provenance.FIRST_PARTY, result, env, home, state)
    if loc is not None:
        _load_dir(loc, provenance.LOCAL, result, env, home, state)
    return _ordered(result)
