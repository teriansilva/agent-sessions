"""`federation.db` — the peers this instance has paired with (#1246 §2).

* **Location** ``<federation dir>/federation.db`` (see :func:`identity.state_dir`), created
  ``0600`` before SQLite can create it under the umask.
* **Public keys only.** The instance's own private key lives in its own file; an invite secret
  never reaches this store.
* **A revoked peer keeps its row.** The key must keep being refused, and the row is the audit
  trail. Pairing with the same key again is a new operator action that moves it back to
  ``trusted`` with a new ``revision``.
* **One store for everything a revoke must end.** Grants (schema 2, #2054) live here, so a
  revoke ends them in the same transaction; later phases add notes the same way.
* **Grants** (``grants``, ``grant_events``): what this instance shares with a peer. A grant is
  ``live`` until it is revoked or expires; ``end_reason`` says why (``revoked``, ``expired``,
  ``peer_revoked``, ``archived``). ``grant_events`` is append-only (triggers refuse an update or a
  delete) and ``meta.generation`` moves with every grant change, in the same transaction.
* **The mirror** (``remote_shares``): what a peer says it shares with this instance. A cache for
  listing, replaced per peer in one transaction; never a session row, never read by local code
  outside ``federation/``.
* **Versioned by ``PRAGMA user_version``**; a file from a NEWER build is refused, never written.
  An existing file is copied (``federation.db.bak-v<n>-…``) BEFORE it is migrated.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import identity

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
DB_FILE = "federation.db"
PEERS_MAX = 64
NAME_MAX = 64
ENDPOINTS_MAX = 4
STATES = ("trusted", "revoked")
#: Live grants one peer may hold. The receiving side refuses a longer list (``remote.py``).
GRANTS_PER_PEER_MAX = 200
GRANT_STATES = ("live", "revoked", "expired")
GRANT_EVENTS = ("granted", "changed", "revoked", "expired", "peer_revoked", "archived")
GRANT_ACTORS = ("operator", "system")
PRINCIPAL_PEER = "peer:"

_SCHEMA_V1 = (
    """
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
    """,
)
# Schema 2 (#2054). ``level`` has no CHECK on purpose: ``suggest`` and ``send`` arrive later
# without a migration; the owner rules (``grants.py``) decide which levels this build grants.
_SCHEMA_V2 = (
    """
    CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY,
        session_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        level TEXT NOT NULL,
        expires_at REAL NOT NULL,
        revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('live', 'revoked', 'expired')),
        created_at REAL NOT NULL,
        ended_at REAL,
        end_reason TEXT
    )
    """,
    # At most one live grant per (session, principal): the database is the arbiter.
    "CREATE UNIQUE INDEX grants_one_live ON grants (session_key, principal) WHERE state = 'live'",
    "CREATE INDEX grants_by_principal ON grants (principal, state, grant_id)",
    """
    CREATE TABLE grant_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        grant_id TEXT NOT NULL,
        at REAL NOT NULL,
        event TEXT NOT NULL CHECK (event IN
            ('granted', 'changed', 'revoked', 'expired', 'peer_revoked', 'archived')),
        actor TEXT NOT NULL CHECK (actor IN ('operator', 'system'))
    )
    """,
    "CREATE INDEX grant_events_by_grant ON grant_events (grant_id, id)",
    """
    CREATE TRIGGER grant_events_no_update BEFORE UPDATE ON grant_events
    BEGIN SELECT RAISE(ABORT, 'grant_events is append-only'); END
    """,
    """
    CREATE TRIGGER grant_events_no_delete BEFORE DELETE ON grant_events
    BEGIN SELECT RAISE(ABORT, 'grant_events is append-only'); END
    """,
    """
    CREATE TABLE remote_shares (
        peer_id TEXT NOT NULL,
        grant_id TEXT NOT NULL,
        engine TEXT NOT NULL,
        native_id TEXT NOT NULL,
        title TEXT NOT NULL,
        level TEXT NOT NULL,
        expires_at REAL NOT NULL,
        revision INTEGER NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (peer_id, grant_id)
    )
    """,
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    "INSERT INTO meta (key, value) VALUES ('generation', 0)",
)


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
    def __init__(self, what: str = "unknown peer") -> None:
        super().__init__(what, status=404)


class Conflict(StoreError):
    """The request is well formed and the store's current state refuses it. Without a message
    it is the pairing case (#2049): the peer's row changed after the operator confirmed, so
    the confirmation no longer applies."""

    def __init__(self, message: str = "this peer changed while pairing; pair again") -> None:
        super().__init__(message, status=409)


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


@dataclass(frozen=True)
class Grant:
    """One owner-side grant. ``principal`` is ``peer:<peer_id>`` in this phase."""

    grant_id: str
    session_key: str
    principal: str
    level: str
    expires_at: float
    revision: int
    state: str
    created_at: float
    ended_at: float | None
    end_reason: str | None

    @property
    def peer_id(self) -> str:
        return self.principal[len(PRINCIPAL_PEER) :]

    def live(self, now: float) -> bool:
        return self.state == "live" and self.expires_at > now


@dataclass(frozen=True)
class RemoteShare:
    """One row of the mirror: a session a peer says it shares with this instance."""

    peer_id: str
    grant_id: str
    engine: str
    native_id: str
    title: str
    level: str
    expires_at: float
    revision: int
    seen_at: float


def principal_of(peer_id: str) -> str:
    return PRINCIPAL_PEER + peer_id


def new_grant_id() -> str:
    """Random and public: it is how a peer addresses a grant. 22 URL-safe characters."""
    return secrets.token_urlsafe(16)


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


def _backup(p: Path, version: int) -> Path:
    """A complete copy of an existing store, taken BEFORE its first migration (the house pattern:
    ``specialists_store._backup``). A random suffix: two processes migrating in the same second
    never collide on the name."""
    dest = p.with_name(f"{p.name}.bak-v{version}-{int(time.time())}-{secrets.token_hex(3)}")
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600))
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(
        grant_id=row["grant_id"],
        session_key=row["session_key"],
        principal=row["principal"],
        level=row["level"],
        expires_at=row["expires_at"],
        revision=row["revision"],
        state=row["state"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
        end_reason=row["end_reason"],
    )


def _remote(row: sqlite3.Row) -> RemoteShare:
    return RemoteShare(
        peer_id=row["peer_id"],
        grant_id=row["grant_id"],
        engine=row["engine"],
        native_id=row["native_id"],
        title=row["title"],
        level=row["level"],
        expires_at=row["expires_at"],
        revision=row["revision"],
        seen_at=row["seen_at"],
    )


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
            if v > 0:
                # BEFORE the first migrating statement: a complete copy of the existing file.
                _backup(p, v)
            con.execute("BEGIN IMMEDIATE")
            try:
                v = _version(
                    con
                )  # re-read under the write lock: another process may have created it
                if v > SCHEMA_VERSION:
                    raise StoreUnsupported(v)
                if v < SCHEMA_VERSION:
                    if v == 0:
                        for stmt in _SCHEMA_V1:
                            con.execute(stmt)
                    for stmt in _SCHEMA_V2:  # a new file, and schema 1 in place
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
            # A newer build may have migrated the file after this object checked it.
            v = _version(con)
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            if v < SCHEMA_VERSION:  # replaced under this object by an older file: migrate again
                with self._schema_lock:
                    self._ensure_schema(p)
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

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One consistent read of several statements (a deferred transaction, rolled back)."""
        with self._connect() as con:
            con.execute("BEGIN")
            try:
                yield con
            finally:
                con.execute("ROLLBACK")

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
        """End trust in this peer AND everything it was granted, in ONE transaction: its live
        grants end (``peer_revoked``) and what it shared with this instance leaves the mirror.
        Idempotent in what it shows: a revoked peer stays revoked. Any process may call it.

        EVERY successful revoke moves the row's revision, also one of a peer that is already
        revoked. The revision is what a pairing confirmation is bound to (:meth:`fence`), so a
        revoke always invalidates a confirmation given before it; re-pairing a revoked peer
        starts from exactly that state."""
        now = time.time()
        with self._write() as con:
            row = con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
            if row is None:
                raise NotFound()
            if row["state"] != "revoked":
                con.execute(
                    "UPDATE peers SET state = 'revoked', revision = revision + 1, updated_at = ?"
                    " WHERE peer_id = ?",
                    (now, peer_id),
                )
            else:
                con.execute(
                    "UPDATE peers SET revision = revision + 1 WHERE peer_id = ?", (peer_id,)
                )
            # Also for a peer already revoked: nothing should be live there, and if something
            # is, this is where it ends.
            self._end_grants(
                con,
                "principal = ?",
                (principal_of(peer_id),),
                reason="peer_revoked",
                actor="system",
                now=now,
            )
            con.execute("DELETE FROM remote_shares WHERE peer_id = ?", (peer_id,))
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

    # ── grants (owner side) ───────────────────────────────────────────
    @staticmethod
    def _bump(con: sqlite3.Connection) -> None:
        con.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")

    @staticmethod
    def _event(con: sqlite3.Connection, grant_id: str, event: str, actor: str, now: float) -> None:
        con.execute(
            "INSERT INTO grant_events (grant_id, at, event, actor) VALUES (?, ?, ?, ?)",
            (grant_id, now, event, actor),
        )

    def _end_grants(
        self,
        con: sqlite3.Connection,
        where: str,
        params: tuple,
        *,
        reason: str,
        actor: str,
        now: float,
    ) -> list[Grant]:
        """End every LIVE grant matching ``where`` inside the caller's transaction: the row, its
        event and the generation move together. ``where`` is a fixed fragment written in this
        module, never input. Returns the grants as they were."""
        rows = con.execute(
            f"SELECT * FROM grants WHERE state = 'live' AND {where}",  # noqa: S608 — fixed fragments
            params,
        ).fetchall()
        state = "expired" if reason == "expired" else "revoked"
        for row in rows:
            con.execute(
                "UPDATE grants SET state = ?, ended_at = ?, end_reason = ?"
                " WHERE grant_id = ? AND state = 'live'",
                (state, now, reason, row["grant_id"]),
            )
            self._event(con, row["grant_id"], reason, actor, now)
        if rows:
            self._bump(con)
        return [_grant(r) for r in rows]

    def generation(self) -> int:
        if not self.db_path.exists():
            return 0  # never used: reading creates nothing
        with self._connect() as con:
            return int(con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()[0])

    def create_grant(
        self,
        *,
        session_key: str,
        peer_id: str,
        level: str,
        expires_at: float,
        now: float | None = None,
    ) -> Grant:
        """Insert a live grant. ONE transaction that re-reads the peer's state: a grant is never
        created for a peer whose revoke already committed, and a revoke that commits later ends
        it. The session rules (exists, API, not archived, the archive fence) are the caller's
        (``grants.create``)."""
        now = time.time() if now is None else now
        principal = principal_of(peer_id)
        with self._write() as con:
            peer = con.execute("SELECT state FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
            if peer is None:
                raise NotFound()
            if peer["state"] != "trusted":
                raise Conflict("this peer is no longer trusted")
            # Expiry is checked at use: what already ran out for this peer ends here, so it
            # neither blocks a new grant for the same session nor counts against the cap.
            self._end_grants(
                con,
                "principal = ? AND expires_at <= ?",
                (principal, now),
                reason="expired",
                actor="system",
                now=now,
            )
            if con.execute(
                "SELECT 1 FROM grants WHERE session_key = ? AND principal = ? AND state = 'live'",
                (session_key, principal),
            ).fetchone():
                raise Conflict("this session is already shared with this peer")
            count = con.execute(
                "SELECT COUNT(*) FROM grants WHERE principal = ? AND state = 'live'", (principal,)
            ).fetchone()[0]
            if count >= GRANTS_PER_PEER_MAX:
                raise Conflict(
                    f"at most {GRANTS_PER_PEER_MAX} sessions can be shared with one peer"
                )
            grant_id = new_grant_id()
            con.execute(
                "INSERT INTO grants (grant_id, session_key, principal, level, expires_at,"
                " revision, state, created_at) VALUES (?, ?, ?, ?, ?, 1, 'live', ?)",
                (grant_id, session_key, principal, level, expires_at, now),
            )
            self._event(con, grant_id, "granted", "operator", now)
            self._bump(con)
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return _grant(row)

    def change_grant(
        self, grant_id: str, *, expires_at: float, revision: int, now: float | None = None
    ) -> Grant:
        """A new expiry for a live grant. ``revision`` is the one the operator saw; the change
        bumps it, so a peer holding the old one is refused until it lists again."""
        now = time.time() if now is None else now
        with self._write() as con:
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise NotFound("unknown grant")
            if row["state"] != "live" or row["expires_at"] <= now:
                raise Conflict("this grant has ended")
            if row["revision"] != revision:
                raise Conflict("this grant changed since it was read")
            con.execute(
                "UPDATE grants SET expires_at = ?, revision = revision + 1 WHERE grant_id = ?",
                (expires_at, grant_id),
            )
            self._event(con, grant_id, "changed", "operator", now)
            self._bump(con)
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return _grant(row)

    def revoke_grant(self, grant_id: str, *, now: float | None = None) -> Grant:
        """End one grant. Idempotent. Any process may call it (a store write, as for a peer)."""
        now = time.time() if now is None else now
        with self._write() as con:
            known = con.execute("SELECT 1 FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if known is None:
                raise NotFound("unknown grant")
            self._end_grants(
                con, "grant_id = ?", (grant_id,), reason="revoked", actor="operator", now=now
            )
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return _grant(row)

    def end_grants_for_session(self, session_key: str, *, now: float | None = None) -> list[Grant]:
        """End every live grant of a session being archived. No store means nothing was ever
        granted: answer without creating one. A store that cannot be read or written raises, and
        the caller (``shares.archiving``) then refuses to archive."""
        if not self.db_path.exists():
            return []  # never used: an archive creates nothing here
        now = time.time() if now is None else now
        with self._write() as con:
            return self._end_grants(
                con, "session_key = ?", (session_key,), reason="archived", actor="system", now=now
            )

    def expire_due(self, *, now: float | None = None) -> list[Grant]:
        """The sweep: end live grants whose expiry passed. Use-time checks never wait for it."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            due = con.execute(
                "SELECT 1 FROM grants WHERE state = 'live' AND expires_at <= ? LIMIT 1", (now,)
            ).fetchone()
        if due is None:
            return []  # the common round: no write lock taken
        with self._write() as con:
            return self._end_grants(
                con, "expires_at <= ?", (now,), reason="expired", actor="system", now=now
            )

    def get_grant(self, grant_id: str) -> Grant | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return _grant(row) if row else None

    def grant_and_peer_state(self, grant_id: str, peer_id: str) -> tuple[Grant | None, str | None]:
        """What the per-call check reads, in ONE statement: the grant, and the state of the peer
        that asks (``None`` when this store does not know it). A plain read: no transaction or
        lock outlives it."""
        if not self.db_path.exists():
            return None, None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT g.*, (SELECT state FROM peers WHERE peer_id = ?) AS peer_state"
                " FROM (SELECT 1) LEFT JOIN grants g ON g.grant_id = ?",
                (peer_id, grant_id),
            ).fetchone()
        if row is None:
            return None, None
        return (_grant(row) if row["grant_id"] is not None else None), row["peer_state"]

    def list_grants(
        self, *, session_key: str | None = None, now: float | None = None
    ) -> list[Grant]:
        """The live grants (of one session, or all), oldest first: what the operator sees."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM grants WHERE state = 'live' AND expires_at > ?"
                " AND (? IS NULL OR session_key = ?) ORDER BY created_at, grant_id",
                (now, session_key, session_key),
            ).fetchall()
        return [_grant(r) for r in rows]

    def live_grants_of(
        self, peer_id: str, *, after: str | None = None, now: float | None = None
    ) -> tuple[int, str | None, list[Grant]]:
        """``(generation, the peer's state, its live grants after ``after`` in grant-id order)``,
        read in one transaction so the generation belongs to exactly these rows."""
        if not self.db_path.exists():
            return 0, None, []  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._read() as con:
            generation = int(
                con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()[0]
            )
            peer = con.execute("SELECT state FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
            rows = con.execute(
                "SELECT * FROM grants WHERE principal = ? AND state = 'live' AND expires_at > ?"
                " AND grant_id > ? ORDER BY grant_id",
                (principal_of(peer_id), now, after or ""),
            ).fetchall()
        return generation, (peer["state"] if peer else None), [_grant(r) for r in rows]

    def grant_events(self, grant_id: str) -> list[dict]:
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT at, event, actor FROM grant_events WHERE grant_id = ? ORDER BY id",
                (grant_id,),
            ).fetchall()
        return [{"at": r["at"], "event": r["event"], "actor": r["actor"]} for r in rows]

    def grant_marks(self) -> dict[str, int]:
        """``{principal: id of its newest grant event}``: how the owner process notices a change
        another process (or an archive) wrote, per peer, without reading any grant."""
        if not self.db_path.exists():
            return {}  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT g.principal AS principal, MAX(e.id) AS mark FROM grant_events e"
                " JOIN grants g ON g.grant_id = e.grant_id GROUP BY g.principal"
            ).fetchall()
        return {r["principal"]: int(r["mark"]) for r in rows}

    # ── the mirror (receiving side) ───────────────────────────────────
    def replace_remote(self, peer_id: str, rows: list[dict], *, now: float | None = None) -> bool:
        """Replace everything ``peer_id`` shares with this instance, in ONE transaction. Refused
        (``False``, nothing changed) for a peer that is not trusted any more: its rows already
        left with the revoke."""
        now = time.time() if now is None else now
        with self._write() as con:
            peer = con.execute("SELECT state FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
            if peer is None or peer["state"] != "trusted":
                return False
            con.execute("DELETE FROM remote_shares WHERE peer_id = ?", (peer_id,))
            con.executemany(
                "INSERT INTO remote_shares (peer_id, grant_id, engine, native_id, title, level,"
                " expires_at, revision, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        peer_id,
                        r["grant_id"],
                        r["engine"],
                        r["native_id"],
                        r["title"],
                        r["level"],
                        r["expires_at"],
                        r["revision"],
                        now,
                    )
                    for r in rows
                ],
            )
        return True

    def list_remote(self, peer_id: str | None = None) -> list[RemoteShare]:
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM remote_shares WHERE (? IS NULL OR peer_id = ?)"
                " ORDER BY peer_id, grant_id",
                (peer_id, peer_id),
            ).fetchall()
        return [_remote(r) for r in rows]

    def get_remote(self, peer_id: str, grant_id: str) -> RemoteShare | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM remote_shares WHERE peer_id = ? AND grant_id = ?",
                (peer_id, grant_id),
            ).fetchone()
        return _remote(row) if row else None
