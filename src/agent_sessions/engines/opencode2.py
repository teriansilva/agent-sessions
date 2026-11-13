"""opencode 2.x store kind (#1986): a separate agent beside opencode 1.x, never sharing its data.

opencode 2.x keeps the SAME default paths as 1.x (`~/.local/share/opencode/opencode.db`), with a
new schema (`session_v2`, `session_message`, `session_part`). Left to its defaults it would open
and migrate the 1.x database, which the `opencode` agent still reads. So this kind owns two rules:

* **Isolation (`agent_env`).** Every process BattleLab starts for this agent gets
  ``XDG_DATA_HOME`` / ``XDG_STATE_HOME`` / ``XDG_CACHE_HOME`` / ``XDG_CONFIG_HOME`` forced to an
  XDG tree under the agent's own store root, and the launch is REFUSED when that tree would resolve
  to (or inside, or around) a 1.x store. Every spawn seam asks `plugins.provider.forced_env`, and
  ``tests/test_opencode2.py`` pins that each one does.
* **Read-only.** BattleLab never writes the 2.x database: archive/rename/sticky are sidecar flags
  (``metadata.json``), exactly as for 1.x. Any sqlite failure degrades to "no rows" for listing.

Shared with 1.x only where the code is truly the same (the ``ses_`` id pattern, the directory
match); there is no dual-identity class, so the isolation invariant stays testable at this seam.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, is_ephemeral_cwd
from . import base
from .opencode import _in_dir

# Columns this reader depends on, pinned so a schema rename fails loud in the fixture test rather
# than silently dropping rows (it would fail soft to no rows in production).
OPENCODE2_SCHEMA = (
    "id",
    "parent_id",
    "directory",
    "title",
    "time_created",
    "time_updated",
    "time_archived",
)

#: The XDG variables forced on every process, each to ``<store root>/<subdir>``. opencode 2.x
#: derives all of its paths (`opencode debug paths`: data, state, cache, config, log, db) from them.
XDG_DIRS = {
    "XDG_DATA_HOME": "data",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
}
#: Where opencode puts its own directory under each XDG base (`<base>/opencode`).
VENDOR_DIR = "opencode"
#: The 1.x layout whose store this agent must never resolve into.
_ONE_X_LAYOUT = "opencode-sqlite"


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def _overlaps(a: Path, b: Path) -> bool:
    """Do two directories name the same place, or does one contain the other?"""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _one_x_dirs(home: Path) -> list[Path]:
    """Every directory a 1.x store lives in on this host: the loaded `opencode` agent's store
    (wherever its manifest or an override puts it), plus 2.x's own default, which IS the 1.x
    default — the place 2.x would write with nothing forced."""
    out = [home / ".local/share" / VENDOR_DIR]
    from . import registry

    try:
        out.append(Path(registry.store_for_layout(_ONE_X_LAYOUT, "db", home)).parent)
    except base.EngineError:
        pass  # no 1.x agent loaded: its default above still applies
    return out


def isolation_env(root: Path, db: Path | None, home: Path) -> dict[str, str]:
    """The forced XDG environment for a 2.x process whose store root is ``root``, or raise.

    Refuses (`EngineError`) when:
      * the declared db is not where 2.x will put it under the forced data home (the manifest and
        the env would disagree about which database this agent owns), or
      * any XDG tree, the root or the db resolves to, inside, or around a 1.x store directory.
    Creates the private subdirectories (0700) so 2.x never falls back to a default.
    """
    root = Path(root)
    if not root.is_absolute():
        raise base.EngineError("opencode2: the store root must be absolute")
    expected_db = root / XDG_DIRS["XDG_DATA_HOME"] / VENDOR_DIR / "opencode.db"
    if db is None or Path(db) != expected_db:
        raise base.EngineError(
            "opencode2: the store's db must be <root>/data/opencode/opencode.db, where the forced "
            "data home puts it"
        )
    forbidden = [_real(d) for d in _one_x_dirs(home)]
    candidates = [_real(root), _real(expected_db.parent)]
    candidates += [_real(root / sub / VENDOR_DIR) for sub in XDG_DIRS.values()]
    for c in candidates:
        for f in forbidden:
            if _overlaps(c, f):
                raise base.EngineError(
                    f"opencode2: refusing to run — {c} overlaps the opencode 1.x store {f}"
                )
    env: dict[str, str] = {}
    for var, sub in XDG_DIRS.items():
        d = root / sub
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[var] = str(d)
    return env


class OpenCode2Provider:
    """opencode 2.x: top-level sessions in ``session_v2`` of its OWN database
    (``<root>/data/opencode/opencode.db``), resumed via ``opencode --standalone <dir> --session
    <id>``. Read-only and fail-soft, like the 1.x reader."""

    engine_id = "opencode2"
    id_pattern = base._SES_RE

    # --- the database -------------------------------------------------------------------------

    def _db(self) -> Path:
        return Path(base._opencode2_db())

    def _query_rows(self) -> list:
        """Top-level sessions, RAISING on a real read failure. An absent db is a valid empty."""
        db = self._db()
        try:
            os.stat(db)
        except FileNotFoundError:
            return []
        cols = ", ".join(OPENCODE2_SCHEMA)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA busy_timeout=500")
            return con.execute(
                f"SELECT {cols} FROM session_v2 WHERE parent_id IS NULL"  # noqa: S608 fixed cols
            ).fetchall()
        finally:
            con.close()

    def _query(self) -> list:
        try:
            return self._query_rows()
        except (sqlite3.Error, OSError):
            return []

    def _db_readable(self) -> bool:
        db = self._db()
        if not db.exists():
            return False
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("SELECT 1 FROM session_v2 LIMIT 1")
                return True
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def store_present(self) -> bool:
        return self._db_readable()

    def is_present(self) -> bool:
        return self.store_present()

    # --- isolation ----------------------------------------------------------------------------

    def agent_env(self) -> dict[str, str]:
        """The environment every process for this agent MUST run with (see module docstring)."""
        owner = getattr(self, "owner", None)
        if owner is None:
            raise base.EngineError(f"{self.engine_id}: the store kind is not attached")
        root = owner.store_root()
        if root is None:
            raise base.EngineError(f"{self.engine_id}: declares no store root")
        return isolation_env(root, owner.store_path("db"), owner.home)

    # --- rows ---------------------------------------------------------------------------------

    def _row(self, record) -> Session | None:
        sid, _parent, directory, title, time_created, time_updated, time_archived = record
        if not isinstance(sid, str) or not self.id_pattern.match(sid):
            return None
        if is_ephemeral_cwd(directory or ""):
            return None
        return Session(
            engine=self.engine_id,
            uuid=sid,
            cwd=directory or "",
            last_mtime=(time_updated or 0) / 1000.0,  # epoch ms, like 1.x
            first_user_message=title or "",
            archived=time_archived is not None,
            created_at=(time_created or time_updated or 0) / 1000.0,
        )

    def _build(self, records) -> list[Session]:
        rows = (self._row(record) for record in records)
        return [row for row in rows if row is not None]

    def scan(self) -> list[Session]:
        return self._build(self._query())

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        try:
            rows = self._query_rows()
        except (sqlite3.Error, OSError) as e:
            return [], [f"{self.engine_id}: its database could not be read ({type(e).__name__})"]
        return self._build(rows), []

    def lookup(self, native_id: str) -> Session | None:
        if not self.id_pattern.match(native_id or ""):
            return None
        db = self._db()
        if not db.exists():
            return None
        cols = ", ".join(OPENCODE2_SCHEMA)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("PRAGMA busy_timeout=500")
                record = con.execute(
                    f"SELECT {cols} FROM session_v2 WHERE id = ? AND parent_id IS NULL",  # noqa: S608 fixed cols
                    (native_id,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return self._row(record) if record is not None else None

    # --- new-session adoption (the engine mints its own `ses_` id) ----------------------------

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """Top-level ids in ``cwd`` before a launch, or ``None`` when the read FAILED (a failed
        read must never look like an empty baseline — the 1.x rule, #127)."""
        try:
            rows = self._query_rows()
        except sqlite3.Error:
            return None
        here = _in_dir(cwd)
        return {
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in rows
            if isinstance(sid, str) and self.id_pattern.match(sid) and here(directory)
        }

    def reconcile_new_session(self, cwd: str, snapshot: set[str]) -> str | list[str] | None:
        """The one new id in ``cwd`` since ``snapshot``; a list when ambiguous; None if none yet."""
        here = _in_dir(cwd)
        new_ids = [
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in self._query()
            if isinstance(sid, str)
            and self.id_pattern.match(sid)
            and here(directory)
            and sid not in snapshot
        ]
        if not new_ids:
            return None
        if len(new_ids) > 1:
            return new_ids
        return new_ids[0]

    # --- sidecar-only mutations ---------------------------------------------------------------

    def archive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
