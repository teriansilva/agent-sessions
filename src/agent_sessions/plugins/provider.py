"""`PluginProvider` — an `EngineProvider` built from a manifest instead of a class (#853 P1).

Everything engine-specific comes from the manifest: the id shape, the argv (assembled here by the
launch *kind*, never taken from the manifest as a list), the capability flags (default-deny), the
store location. The entrypoint comes from `provenance`, re-verified before every argv is handed out.

Store READERS are built-in kinds registered by layout (`register_layout`). P1 ships the contract
with none registered: a plugin whose layout has no reader yet lists no rows (and says so through
`scan_problem`), exactly as a missing store would. P2 registers the seven layouts as it re-expresses
each engine, one equivalence-tested engine at a time.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from .. import metadata as _metadata
from ..engines.base import EngineError
from . import provenance
from .manifest import ArgvStep, Manifest

_NEW_PLACEHOLDER_RE = re.compile(
    r"^new-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RECORD_MAX = 16 * 1024

#: layout kind → reader(provider) -> list[Session]. Built-in code only; see module docstring.
_LAYOUT_READERS: dict[str, Callable[[PluginProvider], list]] = {}


def register_layout(kind: str, reader: Callable[[PluginProvider], list]) -> None:
    from . import kinds

    if kind not in kinds.STORE_LAYOUTS:
        raise ValueError(f"unknown store layout {kind!r}")
    _LAYOUT_READERS[kind] = reader


def read_record(state_dir: Path | None, plugin_id: str) -> provenance.Record | None:
    """The plugin's install / confirmation record, or None when there is none.

    Records live in BattleLab's own state directory (`plugin_state_home`), never beside a plugin's
    manifest: a bundle that could ship its own `record.json` could confirm itself (independent
    review of PR #1112). The record is what makes a binary *managed* or a confirmation *valid*, so
    it is held to the same ownership rule as the entrypoint and read from the verified descriptor.
    Only the install and confirm code paths (P5/P6) write it.
    """
    if state_dir is None:
        return None
    p = state_dir / f"{plugin_id}.json"
    try:
        fd, _ = provenance.open_verified(str(p))
    except FileNotFoundError:
        return None
    except provenance.ProvenanceError as e:
        raise provenance.ProvenanceError(
            f"{p} is not a plain file only the operator can write ({e})"
        ) from None
    try:
        data = os.read(fd, _RECORD_MAX + 1)
    finally:
        os.close(fd)
    try:
        doc = json.loads(data.decode("utf-8")) if len(data) <= _RECORD_MAX else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        doc = None
    allowed = {
        "install_entrypoint",
        "install_sha256",
        "confirmed_path",
        "confirmed_sha256",
        "manifest_sha256",
    }
    if (
        not isinstance(doc, dict)
        or set(doc) - allowed
        or not all(v is None or isinstance(v, str) for v in doc.values())
    ):
        raise provenance.ProvenanceError(f"{p} is malformed")
    return provenance.Record(**doc)


class PluginProvider:
    """Satisfies `engines.base.EngineProvider` from a validated manifest."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        trust: str,
        root: Path,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
        state_dir: Path | None = None,
    ):
        if trust not in provenance.TRUST_LEVELS:
            raise ValueError(f"unknown trust level {trust!r}")
        self.manifest = manifest
        self.trust = trust
        self.root = root
        #: Where this plugin's install / confirmation record lives — app state, outside every
        #: plugin tree. None means no record can exist (nothing managed, nothing confirmed).
        self.state_dir = state_dir
        self._env = env
        self._home = home
        self._cached: tuple[tuple, provenance.Entrypoint] | None = None

        m = manifest
        self.engine_id = m.id
        self.id_pattern = m.session_id.pattern
        # The attribute names the rest of the app already reads (registry / handoff / newSession).
        # Each is a manifest capability and therefore default-deny.
        self.supports_new = m.can("new")
        self.supports_orchestrator_input = m.can("orchestrator_input")
        self.expects_raw_tty = m.can("raw_tty")
        self.supports_seed_start = m.can("seed_start")
        self.new_session_reconciles = m.session_id.mint == "adopt"

    # --- environment ---------------------------------------------------------------------------

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else Path.home()

    def store_root(self) -> Path | None:
        s = self.manifest.store
        if s is None:
            return None
        if s.env_override and self.env.get(s.env_override):
            return Path(self.env[s.env_override])
        return self.home / s.root[2:] if s.root.startswith("~/") else Path(s.root)

    def store_path(self, name: str) -> Path | None:
        root = self.store_root()
        s = self.manifest.store
        if root is None or s is None or name not in s.paths:
            return None
        return root / s.paths[name]

    # --- the entrypoint (§2b) ------------------------------------------------------------------

    def entrypoint(self) -> provenance.Entrypoint | None:
        """Resolve — or re-verify the cached resolution of — the file argv[0] will be.

        Raises `ProvenanceError` when a candidate exists but may not run.
        """
        record = read_record(self.state_dir, self.engine_id)
        env_var = self.manifest.binary.env_var
        # Everything resolution reads is in the key: the override, the record, and the home the
        # `~/` search paths expand against.
        key = (self.env.get(env_var) if env_var else None, record, str(self.home))
        if (
            self._cached is not None
            and self._cached[0] == key
            # A first-party adopted binary is re-resolved every time (cheap: never hashed). Vendor
            # installers retarget a symlink and keep the old version on disk, so a cached path
            # would keep launching the stale version (independent review of PR #1112).
            and not (
                self._cached[1].state == provenance.ADOPTED and self.trust == provenance.FIRST_PARTY
            )
        ):
            ep = self._cached[1]
            try:
                return provenance.reverify(ep, record, trust=self.trust)
            except provenance.ProvenanceError:
                self._cached = None
                if not (ep.state == provenance.ADOPTED and self.trust == provenance.FIRST_PARTY):
                    raise
        ep = provenance.resolve(
            self.manifest,
            trust=self.trust,
            root=self.root,
            record=record,
            env=self.env,
            home=self.home,
        )
        self._cached = (key, ep) if ep is not None else None
        return ep

    def _entry_path(self) -> str:
        try:
            ep = self.entrypoint()
        except provenance.ProvenanceError as e:
            raise EngineError(f"{self.engine_id}: refusing to launch — {e}") from None
        if ep is None:
            raise EngineError(f"{self.engine_id}: no binary found")
        return ep.path

    # --- EngineProvider ------------------------------------------------------------------------

    def is_present(self) -> bool:
        root = self.store_root()
        if root is not None and root.is_dir():
            return True
        try:
            return self.entrypoint() is not None
        except provenance.ProvenanceError:
            return False

    def scan(self) -> list:
        reader = _LAYOUT_READERS.get(self.manifest.store.layout) if self.manifest.store else None
        if reader is None:
            return []
        return reader(self)

    def scan_problem(self) -> str | None:
        if self.manifest.store is None:
            return None
        if self.manifest.store.layout not in _LAYOUT_READERS:
            return f"store layout {self.manifest.store.layout!r} has no reader in this build yet"
        return None

    def _check_native(self, native: str) -> None:
        if not self.manifest.session_id.accepts(native):
            raise EngineError(f"{self.engine_id}: malformed session id")

    @staticmethod
    def _check_cwd(cwd: str) -> None:
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\x00" in cwd:
            raise EngineError("cwd must be an absolute path")

    def _assemble(
        self, step: ArgvStep, native: str, *, cwd: str, bypass: bool, new: bool
    ) -> list[str]:
        launch = self.manifest.launch
        argv = [self._entry_path(), *launch.base_args]
        k = step.kind
        if k in ("flag", "pin-flag"):
            argv += [step.flag, native]
        elif k == "subcommand":
            argv += [step.subcommand, native]
        elif k == "positional-dir":
            self._check_cwd(cwd)
            argv.append(cwd)
            if step.flag:
                argv += [step.flag, native]
        elif k == "cwd-flag":
            self._check_cwd(cwd)
            argv += [step.flag, cwd]
        elif k not in ("fresh", "bare"):  # pragma: no cover — the validator admits no other kind
            raise EngineError(f"{self.engine_id}: unknown launch kind {k!r}")
        if bypass and (launch.bypass_on == "both" or new):
            argv += list(launch.bypass)
        return argv

    def launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        self._check_native(native_id)
        return self._assemble(
            self.manifest.launch.resume, native_id, cwd=cwd, bypass=bypass, new=False
        )

    def new_launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        step = self.manifest.launch.new
        if step is None:
            raise NotImplementedError(f"{self.engine_id} cannot start a new session")
        if self.manifest.session_id.mint == "adopt":
            # The engine mints its own id; ours is only the placeholder the bridge keys by, and the
            # engine never sees it.
            if not _NEW_PLACEHOLDER_RE.fullmatch(native_id or ""):
                raise EngineError(f"{self.engine_id}: malformed new-session placeholder")
        else:
            self._check_native(native_id)
        return self._assemble(step, native_id, cwd=cwd, bypass=bypass, new=True)

    def archive(self, native_id: str) -> None:
        self._check_native(native_id)
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id: str) -> None:
        self._check_native(native_id)
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
