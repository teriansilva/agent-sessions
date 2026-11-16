"""`federation.db` — the peers this instance has paired with (#1246 §2).

* **Location** ``<federation dir>/federation.db`` (see :func:`identity.state_dir`), created
  ``0600`` before SQLite can create it under the umask.
* **Public keys only.** The instance's own private key lives in its own file; an invite secret
  never reaches this store.
* **A revoked peer keeps its row.** The key must keep being refused, and the row is the audit
  trail. Pairing with the same key again is a new operator action that moves it back to
  ``trusted`` with a new ``revision``.
* **One store for everything a revoke must end.** Later phases add grants and notes here, so a
  revoke ends them in the same transaction.
* **Versioned by ``PRAGMA user_version``**; a file from a NEWER build is refused, never written.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import identity

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
DB_FILE = "federation.db"
PEERS_MAX = 64
NAME_MAX = 64
ENDPOINTS_MAX = 4
STATES = ("trusted", "revoked")

_SCHEMA = """
CREATE TABLE peers (
    peer_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL UNIQUE,
    name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('trusted', 'revoked')),
    endpoints TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seen_at REAL
)
"""


class StoreError(RuntimeError):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


class StoreUnsupported(StoreError):
    def __init__(self, version: int) -> None:
        super().__init__(
            f"federation store is at schema {version}, newer than this build "
            f"({SCHEMA_VERSION}); it is left untouched",
            status=503,
        )


class NotFound(StoreError):
    def __init__(self) -> None:
        super().__init__("unknown peer", status=404)


class Conflict(StoreError):
    """The peer's row changed after the operator confirmed: the confirmation no longer applies."""

    def __init__(self) -> None:
        super().__init__("this peer changed while pairing; pair again", status=409)


class Invalid(StoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status=422)


@dataclass(frozen=True)
class Peer:
    peer_id: str
    public_key: bytes
    name: str
    state: str
    endpoints: tuple[dict, ...]
    revision: int
    created_at: float
    updated_at: float
    last_seen_at: float | None

    @property
    def trusted(self) -> bool:
        return self.state == "trusted"


def clean_name(raw: object, *, fallback: str = "") -> str:
    """A display label: printable, single line, bounded. Never an identifier."""
    if not isinstance(raw, str):
        raise Invalid("the name must be text")
    text = unicodedata.normalize("NFC", raw)
    # Whitespace (tabs and newlines included) becomes a space BEFORE controls are dropped, so
    # "GPU\tbox" stays two words; then format and control characters (bidi overrides) go.
    text = "".join(" " if ch.isspace() else ch for ch in text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split())
    if not text:
        text = fallback
    if not text:
        raise Invalid("the name is empty")
    if len(text) > NAME_MAX:
        raise Invalid(f"the name is longer than {NAME_MAX} characters")
    return text


def _raw_connect(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return con


def _version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def _peer(row: sqlite3.Row) -> Peer:
    try:
        endpoints = tuple(e for e in json.loads(row["endpoints"]) if isinstance(e, dict))
    except ValueError:
        endpoints = ()
    return Peer(
        peer_id=row["peer_id"],
        public_key=bytes(row["public_key"]),
        name=row["name"],
        state=row["state"],
        endpoints=endpoints,
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
    )


class Store:
    """The peer store in one directory. ``directory`` defaults to :func:`identity.state_dir`,
    resolved at each use, so a process follows its environment and a test can hold two stores."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._schema_lock = threading.Lock()
        self._schema_done = False

    @property
    def db_path(self) -> Path:
        return (self._directory or identity.state_dir()) / DB_FILE

    def _ensure_schema(self, p: Path) -> None:
        p.parent.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.mkdir(p.parent, identity.DIR_MODE)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600))
        con = _raw_connect(p)
        try:
            # Asked BEFORE anything that writes: a file from a newer build is refused untouched.
            v = _version(con)
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            if v == SCHEMA_VERSION:
                return
            con.execute("BEGIN IMMEDIATE")
            try:
                v = _version(
                    con
                )  # re-read under the write lock: another process may have created it
                if v > SCHEMA_VERSION:
                    raise StoreUnsupported(v)
                if v == 0:
                    for stmt in _SCHEMA.split(";"):
                        if stmt.strip():
                            con.execute(stmt)
                    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        p = self.db_path
        with self._schema_lock:
            if not self._schema_done or not p.exists():
                self._ensure_schema(p)
                self._schema_done = True
        con = _raw_connect(p)
        try:
            yield con
        finally:
            con.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def list_peers(self) -> list[Peer]:
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute("SELECT * FROM peers ORDER BY created_at, peer_id").fetchall()
        return [_peer(r) for r in rows]

    def get(self, peer_id: str) -> Peer | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
        return _peer(row) if row else None

    def by_key(self, public_key: bytes) -> Peer | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute("SELECT * FROM peers WHERE public_key = ?", (public_key,)).fetchone()
        return _peer(row) if row else None

    def trusted_key(self, public_key: bytes) -> Peer | None:
        """The peer this key belongs to, only while it is trusted. THE check a handshake makes."""
        peer = self.by_key(public_key)
        return peer if peer is not None and peer.trusted else None

    def any_trusted(self) -> bool:
        if not self.db_path.exists():
            return False  # never used: reading creates nothing
        with self._connect() as con:
            return (
                con.execute("SELECT 1 FROM peers WHERE state = 'trusted' LIMIT 1").fetchone()
                is not None
            )

    def fence(self, public_key: bytes) -> int:
        """What an operator's confirmation is bound to: the revision of this key's row, ``0``
        while there is none. Every change to the row (a revoke above all) moves it, so
        :meth:`trust` can refuse a confirmation given before the change."""
        peer = self.by_key(public_key)
        return 0 if peer is None else peer.revision

    def trust(
        self, public_key: bytes, *, name: str, endpoints: list[dict], expect: int | None = None
    ) -> Peer:
        """Store a peer both operators confirmed. Pairing again with a known key re-trusts it.

        ``expect`` is the :meth:`fence` read when the local operator confirmed. It is compared
        INSIDE the write transaction: if the row changed since (revoked by any process,
        re-paired, renamed), nothing is written and :class:`Conflict` is raised. So a revoke
        that succeeded is never undone by a pairing confirmed before it. The pairing flow
        always passes it; ``None`` (no fence) is for callers with no earlier confirmation.

        The peer cap counts every move INTO the trusted state, a new row and a revoked one
        alike, in the same transaction."""
        pid = identity.peer_id(public_key)
        name = clean_name(name, fallback=pid[:8])
        now = time.time()
        doc = json.dumps(endpoints[:ENDPOINTS_MAX], separators=(",", ":"), sort_keys=True)
        with self._write() as con:
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (pid,)).fetchone()
            if expect is not None and expect != (0 if row is None else row["revision"]):
                raise Conflict()
            if row is None or row["state"] != "trusted":
                count = con.execute(
                    "SELECT COUNT(*) FROM peers WHERE state = 'trusted'"
                ).fetchone()[0]
                if count >= PEERS_MAX:
                    raise StoreError(f"at most {PEERS_MAX} peers", status=409)
            if row is None:
                con.execute(
                    "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision,"
                    " created_at, updated_at) VALUES (?, ?, ?, 'trusted', ?, 1, ?, ?)",
                    (pid, public_key, name, doc, now, now),
                )
            else:
                con.execute(
                    "UPDATE peers SET name = ?, state = 'trusted', endpoints = ?,"
                    " revision = revision + 1, updated_at = ? WHERE peer_id = ?",
                    (name, doc, now, pid),
                )
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (pid,)).fetchone()
        return _peer(row)

    def rename(self, peer_id: str, name: str) -> Peer:
        name = clean_name(name)
        with self._write() as con:
            cur = con.execute(
                "UPDATE peers SET name = ?, revision = revision + 1, updated_at = ?"
                " WHERE peer_id = ?",
                (name, time.time(), peer_id),
            )
            if cur.rowcount == 0:
                raise NotFound()
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
        return _peer(row)

    def revoke(self, peer_id: str) -> Peer:
        """End trust in this peer. Idempotent in what it shows: a revoked peer stays revoked.
        Later phases end its grants in this transaction.

        EVERY successful revoke moves the row's revision, also one of a peer that is already
        revoked. The revision is what a pairing confirmation is bound to (:meth:`fence`), so a
        revoke always invalidates a confirmation given before it; re-pairing a revoked peer
        starts from exactly that state."""
        with self._write() as con:
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
            if row is None:
                raise NotFound()
            if row["state"] != "revoked":
                con.execute(
                    "UPDATE peers SET state = 'revoked', revision = revision + 1, updated_at = ?"
                    " WHERE peer_id = ?",
                    (time.time(), peer_id),
                )
            else:
                con.execute(
                    "UPDATE peers SET revision = revision + 1 WHERE peer_id = ?", (peer_id,)
                )
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
        return _peer(row)

    def set_endpoints(self, peer_id: str, endpoints: list[dict]) -> None:
        doc = json.dumps(endpoints[:ENDPOINTS_MAX], separators=(",", ":"), sort_keys=True)
        with self._write() as con:
            con.execute(
                "UPDATE peers SET endpoints = ?, updated_at = ?"
                " WHERE peer_id = ? AND endpoints != ?",
                (doc, time.time(), peer_id, doc),
            )

    def touch_seen(self, peer_id: str) -> None:
        with self._write() as con:
            con.execute(
                "UPDATE peers SET last_seen_at = ? WHERE peer_id = ?", (time.time(), peer_id)
            )
