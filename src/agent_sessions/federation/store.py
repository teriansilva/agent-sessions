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
* **The kind of a grant** (schema 3, #2056): ``grants.kind`` and ``remote_shares.kind`` (``api``
  or ``terminal``), stored when granted; every row an earlier build wrote is ``api``.
* **Input from a peer** (schema 4, #2057): ``suggestions`` (a message a peer proposes, waiting
  for the operator) and ``peer_turns`` (the operation id and the author of every turn a peer
  caused). A suggestion's text is read by ``federation/`` and its routes and by nothing else. A
  grant that ends withdraws its pending suggestions in the same transaction.
* **Every write that ends or changes a grant holds that grant's fence** (``fence.py``) around its
  transaction, taken before ``BEGIN``; one that cannot get it commits nothing and raises
  :class:`Busy`. The methods here take the fences themselves, so no caller can commit around a
  delivery in flight.
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
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import fence, identity

SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000
DB_FILE = "federation.db"
PEERS_MAX = 64
NAME_MAX = 64
ENDPOINTS_MAX = 4
STATES = ("trusted", "revoked")
#: Live grants one peer may hold. The receiving side refuses a longer list (``remote.py``).
GRANTS_PER_PEER_MAX = 200
GRANT_STATES = ("live", "revoked", "expired")
#: What a grant shares (#2056): an API session's projection, or a terminal session's screen as a
#: redacted text frame. It is what the operator agreed to when granting, so it is STORED, and the
#: per-call check also asks the session what it is today; a verb serves only where both agree.
KIND_API = "api"
KIND_TERMINAL = "terminal"
GRANT_KINDS = (KIND_API, KIND_TERMINAL)
GRANT_EVENTS = ("granted", "changed", "revoked", "expired", "peer_revoked", "archived")
GRANT_ACTORS = ("operator", "system")
PRINCIPAL_PEER = "peer:"
#: What a level includes. A level this build does not know ranks below every known one.
LEVEL_RANK = {"view": 0, "suggest": 1, "send": 2}
SUGGESTION_STATES = ("pending", "approving", "approved", "rejected", "withdrawn", "expired")
#: Pending suggestions one grant may hold; the next is refused (``busy``).
PENDING_PER_GRANT_MAX = 5
#: A pending suggestion nobody decided expires after this, or with its grant.
SUGGESTION_TTL_S = 24 * 3600
PEER_TURN_VIAS = ("send", "suggestion")
#: How often a fenced write re-reads which fences it needs before it gives up as busy.
FENCE_ROUNDS = 3

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
# Schema 3 (#2056): the kind of a grant, on the owner's rows and in the mirror. Every row an
# earlier build wrote is an API grant (the only kind it could make), which is the default. The
# CHECK makes the database the arbiter: a kind this build does not know is never stored.
_SCHEMA_V3 = (
    "ALTER TABLE grants ADD COLUMN kind TEXT NOT NULL DEFAULT 'api'"
    " CHECK (kind IN ('api', 'terminal'))",
    "ALTER TABLE remote_shares ADD COLUMN kind TEXT NOT NULL DEFAULT 'api'"
    " CHECK (kind IN ('api', 'terminal'))",
)
# Schema 4 (#2057): what a peer proposes or sends.
_SCHEMA_V4 = (
    # ``operation_id`` is minted with the row and never changes: an approval, its replay after a
    # crash and every retry submit under this one id. ``text`` is what the peer proposed;
    # ``approved_text`` is what the operator approved (their edit, or the same text).
    # ``notified`` says a bell row may exist for it (set with the insert, cleared on retraction).
    """
    CREATE TABLE suggestions (
        suggestion_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        client_key TEXT NOT NULL,
        actor TEXT NOT NULL,
        text TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN
            ('pending', 'approving', 'approved', 'rejected', 'withdrawn', 'expired')),
        approved_text TEXT,
        approved_by TEXT,
        approved_at REAL,
        edited INTEGER NOT NULL DEFAULT 0,
        handoff TEXT,
        receipt_at REAL,
        end_reason TEXT,
        notified INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        UNIQUE (grant_id, client_key)
    )
    """,
    "CREATE INDEX suggestions_by_grant ON suggestions (grant_id, state)",
    "CREATE INDEX suggestions_by_session ON suggestions (session_key, created_at)",
    # One row per turn a peer caused, written BEFORE the submit. ``client_key`` is the peer's
    # retry key for a sent message, and ``suggestion:<id>`` for an approved suggestion.
    """
    CREATE TABLE peer_turns (
        operation_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        client_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        actor TEXT NOT NULL,
        via TEXT NOT NULL CHECK (via IN ('send', 'suggestion')),
        edited INTEGER NOT NULL DEFAULT 0,
        handoff TEXT,
        receipt_at REAL,
        created_at REAL NOT NULL,
        UNIQUE (grant_id, client_key)
    )
    """,
    "CREATE INDEX peer_turns_by_session ON peer_turns (session_key)",
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


class Busy(StoreError):
    """A grant's fence could not be taken inside the wait: a delivery under it is in flight.
    NOTHING was committed. Retryable."""

    def __init__(self, message: str = "a delivery under this grant is in flight — try again"):
        super().__init__(message, status=503)


class NotGranted(StoreError):
    """The grant does not serve this request (any more). The reason stays local."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, status=404)


class Full(StoreError):
    """The grant already holds as many pending suggestions as it may."""

    def __init__(self) -> None:
        super().__init__(
            f"at most {PENDING_PER_GRANT_MAX} suggestions can wait under one grant", status=429
        )


class StateConflict(Conflict):
    """A decision lost: the suggestion is already in ``state``."""

    def __init__(self, state: str) -> None:
        super().__init__(f"this suggestion is already {state}")
        self.state = state


class _Refence(Exception):
    """The fences a write needs changed between the read and the transaction: take them again."""


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
    kind: str

    @property
    def peer_id(self) -> str:
        return self.principal[len(PRINCIPAL_PEER) :]

    def live(self, now: float) -> bool:
        return self.state == "live" and self.expires_at > now


@dataclass(frozen=True)
class Suggestion:
    """A message a peer proposes for a session. ``text`` is the peer's; nothing outside
    ``federation/`` and its routes reads it."""

    suggestion_id: str
    grant_id: str
    session_key: str
    principal: str
    client_key: str
    actor: str
    text: str
    operation_id: str
    state: str
    approved_text: str | None
    approved_by: str | None
    approved_at: float | None
    edited: bool
    handoff: str | None
    receipt_at: float | None
    end_reason: str | None
    notified: bool
    created_at: float
    updated_at: float
    expires_at: float

    @property
    def peer_id(self) -> str:
        return self.principal[len(PRINCIPAL_PEER) :]


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
    kind: str


def principal_of(peer_id: str) -> str:
    return PRINCIPAL_PEER + peer_id


def new_grant_id() -> str:
    """Random and public: it is how a peer addresses a grant. 22 URL-safe characters."""
    return secrets.token_urlsafe(16)


def level_includes(level: object, need: str) -> bool:
    """Whether a grant at ``level`` serves a request that needs ``need``."""
    return LEVEL_RANK.get(level, -1) >= LEVEL_RANK[need]  # type: ignore[arg-type]


def _suggestion(row: sqlite3.Row) -> Suggestion:
    return Suggestion(
        suggestion_id=row["suggestion_id"],
        grant_id=row["grant_id"],
        session_key=row["session_key"],
        principal=row["principal"],
        client_key=row["client_key"],
        actor=row["actor"],
        text=row["text"],
        operation_id=row["operation_id"],
        state=row["state"],
        approved_text=row["approved_text"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        edited=bool(row["edited"]),
        handoff=row["handoff"],
        receipt_at=row["receipt_at"],
        end_reason=row["end_reason"],
        notified=bool(row["notified"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


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
        kind=row["kind"],
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
        kind=row["kind"],
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
                    # A new file gets every step; an existing one the steps it lacks, in place.
                    for step, statements in (
                        (1, _SCHEMA_V1),
                        (2, _SCHEMA_V2),
                        (3, _SCHEMA_V3),
                        (4, _SCHEMA_V4),
                    ):
                        if v < step:
                            for stmt in statements:
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

    # ── fences ────────────────────────────────────────────────────────
    @property
    def directory(self) -> Path:
        return self.db_path.parent

    def _with_fences(self, ids_of, work, *, wait: float | None = None):
        """Run ``work(con)`` in ONE write transaction while holding the fence of every grant
        ``ids_of(con)`` names (``fence.py``; grant-id order, before ``BEGIN``).

        ``ids_of`` is asked twice: before the fences are taken (a plain read, no lock held
        across the wait) and again INSIDE the transaction. If the second answer names a grant
        whose fence is not held (a grant was raised or created in between), nothing is written
        and the whole step runs again. A fence that cannot be had inside ``wait`` raises
        :class:`Busy`: nothing was committed."""
        budget = fence.REVOKE_WAIT_S if wait is None else wait
        for _round in range(FENCE_ROUNDS):
            with self._connect() as con:
                ids = set(ids_of(con))
            try:
                with fence.hold(self.directory, ids, timeout=budget):
                    with self._write() as con:
                        if not set(ids_of(con)) <= ids:
                            raise _Refence()
                        return work(con)
            except _Refence:
                continue
            except fence.FenceBusy:
                raise Busy() from None
            except OSError as exc:
                # A fence that cannot even be opened is not a fence that is free: fail closed.
                raise StoreError(
                    f"a grant's fence could not be taken ({type(exc).__name__})", status=503
                ) from exc
        raise Busy()

    @staticmethod
    def _input_grant_ids(con: sqlite3.Connection, where: str, params: tuple) -> list[str]:
        """The live grants matching ``where`` that can deliver something (every level but
        ``view``, a level this build does not know included). ``where`` is a fixed fragment."""
        rows = con.execute(
            "SELECT grant_id FROM grants WHERE state = 'live' AND level != 'view'"  # noqa: S608
            f" AND {where}",
            params,
        ).fetchall()
        return [r["grant_id"] for r in rows]

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

    def revoke(self, peer_id: str, *, wait: float | None = None) -> Peer:
        """End trust in this peer AND everything it was granted, in ONE transaction: its live
        grants end (``peer_revoked``) and what it shared with this instance leaves the mirror.
        Idempotent in what it shows: a revoked peer stays revoked. Any process may call it.

        EVERY successful revoke moves the row's revision, also one of a peer that is already
        revoked. The revision is what a pairing confirmation is bound to (:meth:`fence`), so a
        revoke always invalidates a confirmation given before it; re-pairing a revoked peer
        starts from exactly that state.

        It holds the fence of each of the peer's ``suggest`` and ``send`` grants (grant-id
        order) around that transaction, so it lands before a delivery or after it. A ``view``
        grant has no effect to fence: revoking a peer that only views waits for nothing. When a
        fence cannot be had, NOTHING is committed and :class:`Busy` is raised."""
        principal = principal_of(peer_id)

        def work(con: sqlite3.Connection) -> sqlite3.Row:
            now = time.time()
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
                con, "principal = ?", (principal,), reason="peer_revoked", actor="system", now=now
            )
            con.execute("DELETE FROM remote_shares WHERE peer_id = ?", (peer_id,))
            return con.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()

        return _peer(
            self._with_fences(
                lambda con: self._input_grant_ids(con, "principal = ?", (principal,)),
                work,
                wait=wait,
            )
        )

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
            # The same transaction: what the peer proposed under this grant and nobody decided
            # is withdrawn with it. (An ``approving`` row is settled by its replay, which
            # re-checks the grant inside the fence and withdraws: ``inputs.resume_approving``.)
            self._withdraw_pending(con, row["grant_id"], reason, now)
        if rows:
            self._bump(con)
        return [_grant(r) for r in rows]

    @staticmethod
    def _withdraw_pending(con: sqlite3.Connection, grant_id: str, reason: str, now: float) -> None:
        con.execute(
            "UPDATE suggestions SET state = 'withdrawn', end_reason = ?, updated_at = ?"
            " WHERE grant_id = ? AND state = 'pending'",
            (f"grant_{reason}", now, grant_id),
        )

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
        kind: str,
        now: float | None = None,
        wait: float | None = None,
    ) -> Grant:
        """Insert a live grant. ONE transaction that re-reads the peer's state: a grant is never
        created for a peer whose revoke already committed, and a revoke that commits later ends
        it. The session rules (exists, its kind, not archived, the archive fence, not
        mission-held for ``send``) are the caller's (``grants.create``). A terminal grant is
        ``view`` only, here too.

        The same transaction ends this peer's grants whose expiry passed; those that can
        deliver are ended under their fences, like every other end."""
        if kind not in GRANT_KINDS:
            raise Invalid("unknown grant kind")
        if kind == KIND_TERMINAL and level != "view":
            raise Invalid("a terminal grant is view only")
        now = time.time() if now is None else now
        principal = principal_of(peer_id)

        def work(con: sqlite3.Connection) -> sqlite3.Row:
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
                " revision, state, created_at, kind) VALUES (?, ?, ?, ?, ?, 1, 'live', ?, ?)",
                (grant_id, session_key, principal, level, expires_at, now, kind),
            )
            self._event(con, grant_id, "granted", "operator", now)
            self._bump(con)
            return con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()

        return _grant(
            self._with_fences(
                lambda con: self._input_grant_ids(
                    con, "principal = ? AND expires_at <= ?", (principal, now)
                ),
                work,
                wait=wait,
            )
        )

    @staticmethod
    def _known(con: sqlite3.Connection, grant_id: str) -> list[str]:
        """``[grant_id]`` when the store holds such a grant: no fence file for an id it never
        minted."""
        row = con.execute("SELECT 1 FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        return [grant_id] if row is not None else []

    def change_grant(
        self,
        grant_id: str,
        *,
        expires_at: float,
        revision: int,
        level: str | None = None,
        now: float | None = None,
        wait: float | None = None,
    ) -> Grant:
        """A new expiry, and optionally a new level, for a live grant. ``revision`` is the one
        the operator saw; EVERY change bumps it, so a peer holding the old one is refused until
        it lists again. Under the grant's fence, whether the change extends, lowers or raises:
        it cannot slip between a delivery's re-check and its submit. A level below ``suggest``
        withdraws the grant's pending suggestions in the same transaction."""
        now = time.time() if now is None else now

        def work(con: sqlite3.Connection) -> sqlite3.Row:
            row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise NotFound("unknown grant")
            if row["state"] != "live" or row["expires_at"] <= now:
                raise Conflict("this grant has ended")
            if row["revision"] != revision:
                raise Conflict("this grant changed since it was read")
            new_level = row["level"] if level is None else level
            if row["kind"] == KIND_TERMINAL and new_level != "view":
                raise Invalid("a terminal grant is view only")
            con.execute(
                "UPDATE grants SET expires_at = ?, level = ?, revision = revision + 1"
                " WHERE grant_id = ?",
                (expires_at, new_level, grant_id),
            )
            if not level_includes(new_level, "suggest"):
                self._withdraw_pending(con, grant_id, "lowered", now)
            self._event(con, grant_id, "changed", "operator", now)
            self._bump(con)
            return con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()

        return _grant(self._with_fences(lambda con: self._known(con, grant_id), work, wait=wait))

    def revoke_grant(
        self, grant_id: str, *, now: float | None = None, wait: float | None = None
    ) -> Grant:
        """End one grant. Idempotent. Any process may call it (a store write, as for a peer).
        Under the grant's fence: it lands before a delivery or after it, and when the fence
        cannot be had it commits nothing and raises :class:`Busy`."""
        now = time.time() if now is None else now

        def work(con: sqlite3.Connection) -> sqlite3.Row:
            known = con.execute("SELECT 1 FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if known is None:
                raise NotFound("unknown grant")
            self._end_grants(
                con, "grant_id = ?", (grant_id,), reason="revoked", actor="operator", now=now
            )
            return con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()

        return _grant(self._with_fences(lambda con: self._known(con, grant_id), work, wait=wait))

    def end_grants_for_session(
        self, session_key: str, *, now: float | None = None, wait: float | None = None
    ) -> list[Grant]:
        """End every live grant of a session being archived, under each grant's fence. No store
        means nothing was ever granted: answer without creating one. A store that cannot be
        read or written, or a fence that cannot be had (:class:`Busy`), raises, and the caller
        (``shares.archiving``) then refuses to archive."""
        if not self.db_path.exists():
            return []  # never used: an archive creates nothing here
        now = time.time() if now is None else now

        def ids_of(con: sqlite3.Connection) -> list[str]:
            rows = con.execute(
                "SELECT grant_id FROM grants WHERE state = 'live' AND session_key = ?",
                (session_key,),
            ).fetchall()
            return [r["grant_id"] for r in rows]

        return self._with_fences(
            ids_of,
            lambda con: self._end_grants(
                con, "session_key = ?", (session_key,), reason="archived", actor="system", now=now
            ),
            wait=wait,
        )

    def expire_due(self, *, now: float | None = None) -> list[Grant]:
        """The sweep: end live grants whose expiry passed, each under its own fence. Use-time
        checks never wait for it, so it never waits either: a grant whose fence is held (a
        delivery admitted before the expiry is still unwinding) is ended on a later round."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            due = [
                r["grant_id"]
                for r in con.execute(
                    "SELECT grant_id FROM grants WHERE state = 'live' AND expires_at <= ?"
                    " ORDER BY grant_id",
                    (now,),
                ).fetchall()
            ]
        ended: list[Grant] = []
        for grant_id in due:  # the common round has none: no fence and no write lock taken

            def work(con: sqlite3.Connection, grant_id: str = grant_id) -> list[Grant]:
                # Re-read inside the fence: a change may have extended it meanwhile.
                return self._end_grants(
                    con,
                    "grant_id = ? AND expires_at <= ?",
                    (grant_id, now),
                    reason="expired",
                    actor="system",
                    now=now,
                )

            try:
                ended += self._with_fences(
                    lambda con, grant_id=grant_id: self._known(con, grant_id),
                    work,
                    wait=fence.SWEEP_WAIT_S,
                )
            except Busy:
                continue
        return ended

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

    # ── input from a peer (#2057) ─────────────────────────────────────
    @staticmethod
    def _serving(
        con: sqlite3.Connection,
        grant_id: str,
        peer_id: str,
        need: str,
        now: float,
        revision: int | None = None,
    ) -> sqlite3.Row:
        """The grant row, when it serves ``peer_id`` at ``need`` right now. Inside the caller's
        transaction, so the answer and the write that follows are one step. Input from a peer
        is for API sessions only: a terminal grant (view only, #2056) never serves here."""
        row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        peer = con.execute("SELECT state FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
        if peer is None or peer["state"] != "trusted":
            raise NotGranted("peer not trusted")
        if row is None or row["principal"] != principal_of(peer_id):
            raise NotGranted("unknown grant")
        if row["state"] != "live" or row["expires_at"] <= now:
            raise NotGranted("grant ended")
        if revision is not None and row["revision"] != revision:
            raise NotGranted("stale revision")
        if row["kind"] != KIND_API:
            raise NotGranted(f"a {row['kind']} grant takes no input")
        if not level_includes(row["level"], need):
            raise NotGranted("level too low")
        return row

    def add_suggestion(
        self,
        *,
        grant_id: str,
        peer_id: str,
        revision: int,
        client_key: str,
        text: str,
        actor: str,
        now: float | None = None,
    ) -> tuple[Suggestion, bool]:
        """Record what a peer proposes. ``(suggestion, fresh)``.

        ONE ``BEGIN IMMEDIATE`` transaction: the grant is re-read (live, this peer's, the
        revision it holds, at least ``suggest``, the peer trusted), the retry key is looked up
        (the same ``client_key`` on the same grant returns the SAME suggestion), the pending
        ones are counted, and the row is inserted. Concurrent suggestions therefore cannot pass
        :data:`PENDING_PER_GRANT_MAX`, and one that races a revoke is either inserted before it
        (and withdrawn by it, in its transaction) or refused. The operation id is minted here
        and never changes."""
        now = time.time() if now is None else now
        with self._write() as con:
            grant = self._serving(con, grant_id, peer_id, "suggest", now, revision)
            row = con.execute(
                "SELECT * FROM suggestions WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
            if row is not None:
                return _suggestion(row), False
            pending = con.execute(
                "SELECT COUNT(*) FROM suggestions WHERE grant_id = ? AND state = 'pending'",
                (grant_id,),
            ).fetchone()[0]
            if pending >= PENDING_PER_GRANT_MAX:
                raise Full()
            suggestion_id = new_grant_id()
            con.execute(
                "INSERT INTO suggestions (suggestion_id, grant_id, session_key, principal,"
                " client_key, actor, text, operation_id, state, created_at, updated_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    suggestion_id,
                    grant_id,
                    grant["session_key"],
                    grant["principal"],
                    client_key,
                    actor,
                    text,
                    str(uuid.uuid4()),
                    now,
                    now,
                    min(now + SUGGESTION_TTL_S, grant["expires_at"]),
                ),
            )
            row = con.execute(
                "SELECT * FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
            ).fetchone()
        return _suggestion(row), True

    def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
            ).fetchone()
        return _suggestion(row) if row else None

    def list_suggestions(
        self, *, session_key: str | None = None, limit: int = 100
    ) -> list[Suggestion]:
        """Suggestions with their text, newest first: what the OPERATOR's screen shows."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM suggestions WHERE (? IS NULL OR session_key = ?)"
                " ORDER BY created_at DESC, suggestion_id LIMIT ?",
                (session_key, session_key, limit),
            ).fetchall()
        return [_suggestion(r) for r in rows]

    def suggestion_states(self, grant_id: str, *, limit: int = 50) -> list[dict]:
        """What the PEER reads of its own suggestions under one grant: ids and states, no text."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT suggestion_id, client_key, state, handoff, created_at, updated_at"
                " FROM suggestions WHERE grant_id = ? ORDER BY created_at DESC, suggestion_id"
                " LIMIT ?",
                (grant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def approving_suggestions(self) -> list[Suggestion]:
        """Approved by the operator and not yet known delivered: what a replay resumes."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM suggestions WHERE state = 'approving' ORDER BY approved_at"
            ).fetchall()
        return [_suggestion(r) for r in rows]

    def _move(
        self,
        con: sqlite3.Connection,
        suggestion_id: str,
        source: str,
        assignments: str,
        params: tuple,
        *,
        extra: str = "",
        extra_params: tuple = (),
    ) -> Suggestion:
        """Compare-and-set: move a suggestion out of ``source`` inside the caller's transaction,
        or raise :class:`StateConflict` naming the state that won. ``assignments`` and ``extra``
        are fixed fragments written in this module."""
        cur = con.execute(
            f"UPDATE suggestions SET {assignments}"  # noqa: S608 — fixed fragments
            f" WHERE suggestion_id = ? AND state = ?{extra}",
            (*params, suggestion_id, source, *extra_params),
        )
        row = con.execute(
            "SELECT * FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("unknown suggestion")
        if cur.rowcount == 0:
            raise StateConflict(row["state"])
        return _suggestion(row)

    def begin_approval(
        self,
        suggestion_id: str,
        *,
        text: str,
        approver: str,
        edited: bool,
        now: float | None = None,
    ) -> Suggestion:
        """THE approval: ``pending → approving``, recording the final text and who approved, in
        ONE transaction that also re-reads the grant (live, at least ``suggest``, its peer
        trusted). Called by ``inputs.decide_approve`` and by nothing else (a source scan pins
        it). A suggestion already decided, withdrawn or expired raises :class:`StateConflict`."""
        now = time.time() if now is None else now
        with self._write() as con:
            row = con.execute(
                "SELECT * FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
            ).fetchone()
            if row is None:
                raise NotFound("unknown suggestion")
            if row["state"] != "pending":
                raise StateConflict(row["state"])
            if row["expires_at"] <= now:
                raise StateConflict("expired")
            self._serving(
                con, row["grant_id"], row["principal"][len(PRINCIPAL_PEER) :], "suggest", now
            )
            return self._move(
                con,
                suggestion_id,
                "pending",
                "state = 'approving', approved_text = ?, approved_by = ?, approved_at = ?,"
                " edited = ?, updated_at = ?",
                (text, approver, now, int(bool(edited)), now),
            )

    def finish_approval(
        self, suggestion_id: str, *, handoff: str | None, now: float | None = None
    ) -> Suggestion:
        """``approving → approved`` with the runtime's receipt, after the submit returned."""
        now = time.time() if now is None else now
        with self._write() as con:
            return self._move(
                con,
                suggestion_id,
                "approving",
                "state = 'approved', handoff = ?, receipt_at = ?, updated_at = ?",
                (handoff, now, now),
            )

    def withdraw_approving(
        self, suggestion_id: str, *, reason: str, now: float | None = None
    ) -> Suggestion:
        """``approving → withdrawn``: a replay found the approval's grant gone. Never a submit."""
        now = time.time() if now is None else now
        with self._write() as con:
            return self._move(
                con,
                suggestion_id,
                "approving",
                "state = 'withdrawn', end_reason = ?, updated_at = ?",
                (reason, now),
            )

    def reject_suggestion(
        self, suggestion_id: str, *, approver: str, now: float | None = None
    ) -> Suggestion:
        """The operator declines: ``pending → rejected``."""
        now = time.time() if now is None else now
        with self._write() as con:
            return self._move(
                con,
                suggestion_id,
                "pending",
                "state = 'rejected', approved_by = ?, end_reason = 'rejected', updated_at = ?",
                (approver, now),
            )

    def withdraw_suggestion(
        self, suggestion_id: str, *, grant_id: str, now: float | None = None
    ) -> Suggestion:
        """The peer takes its own pending suggestion back: ``pending → withdrawn``. Only under
        the grant it was made under; any other id is unknown."""
        now = time.time() if now is None else now
        with self._write() as con:
            mine = con.execute(
                "SELECT 1 FROM suggestions WHERE suggestion_id = ? AND grant_id = ?",
                (suggestion_id, grant_id),
            ).fetchone()
            if mine is None:
                raise NotFound("unknown suggestion")  # also: another grant's, whatever its state
            return self._move(
                con,
                suggestion_id,
                "pending",
                "state = 'withdrawn', end_reason = 'peer', updated_at = ?",
                (now,),
                extra=" AND grant_id = ?",
                extra_params=(grant_id,),
            )

    def expire_suggestions(self, *, now: float | None = None) -> int:
        """Pending suggestions nobody decided in time become ``expired``."""
        if not self.db_path.exists():
            return 0  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            due = con.execute(
                "SELECT 1 FROM suggestions WHERE state = 'pending' AND expires_at <= ? LIMIT 1",
                (now,),
            ).fetchone()
        if due is None:
            return 0  # the common round: no write lock taken
        with self._write() as con:
            return con.execute(
                "UPDATE suggestions SET state = 'expired', end_reason = 'expired', updated_at = ?"
                " WHERE state = 'pending' AND expires_at <= ?",
                (now, now),
            ).rowcount

    def unretracted(self) -> list[str]:
        """Suggestions that are no longer pending and may still have a bell row."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT suggestion_id FROM suggestions WHERE notified = 1 AND state != 'pending'"
            ).fetchall()
        return [r["suggestion_id"] for r in rows]

    def mark_retracted(self, suggestion_ids: list[str]) -> None:
        with self._write() as con:
            con.executemany(
                "UPDATE suggestions SET notified = 0 WHERE suggestion_id = ?"
                " AND state != 'pending'",
                [(s,) for s in suggestion_ids],
            )

    def record_peer_turn(
        self,
        *,
        grant_id: str,
        session_key: str,
        peer_id: str,
        client_key: str,
        actor: str,
        via: str,
        operation_id: str | None = None,
        edited: bool = False,
        now: float | None = None,
    ) -> tuple[str, bool]:
        """The operation id of a turn a peer causes, recorded with its author BEFORE the submit.
        ``(operation_id, fresh)``. The same ``client_key`` on the same grant returns the same
        id (a retry never mints a second one, across a restart too); a retry under another actor
        label keeps the first. The id is minted here, or is the suggestion's own; a peer never
        chooses one."""
        now = time.time() if now is None else now
        with self._write() as con:
            row = con.execute(
                "SELECT operation_id, session_key FROM peer_turns"
                " WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
            if row is not None:
                if row["session_key"] != session_key:
                    raise Conflict("that retry key belongs to another session")
                return row["operation_id"], False
            op = operation_id or str(uuid.uuid4())
            con.execute(
                "INSERT INTO peer_turns (operation_id, grant_id, session_key, client_key,"
                " principal, actor, via, edited, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    op,
                    grant_id,
                    session_key,
                    client_key,
                    principal_of(peer_id),
                    actor,
                    via,
                    int(bool(edited)),
                    now,
                ),
            )
            return op, True

    def note_peer_turn(
        self, operation_id: str, *, handoff: str | None, now: float | None = None
    ) -> bool:
        """The runtime's receipt for a peer's turn, as it was answered. A record, never a rule.
        True when this is the FIRST receipt recorded for the turn (a replay's is not)."""
        now = time.time() if now is None else now
        with self._write() as con:
            first = con.execute(
                "UPDATE peer_turns SET handoff = ?, receipt_at = ?"
                " WHERE operation_id = ? AND receipt_at IS NULL",
                (handoff, now, operation_id),
            ).rowcount
            if not first:
                con.execute(
                    "UPDATE peer_turns SET handoff = ? WHERE operation_id = ?",
                    (handoff, operation_id),
                )
        return bool(first)

    def get_peer_turn(self, grant_id: str, client_key: str) -> dict | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM peer_turns WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
        return dict(row) if row else None

    def peer_authors(self, session_key: str) -> dict[str, dict]:
        """``{operation_id: {"name", "peer_id", "grant_id", "actor", "via", "edited"}}`` for every
        turn a peer caused in a session. ``name`` is the peer's display name on THIS instance."""
        if not self.db_path.exists():
            return {}  # never used: no peer ever caused a turn
        with self._connect() as con:
            rows = con.execute(
                "SELECT t.operation_id, t.grant_id, t.principal, t.actor, t.via, t.edited,"
                " p.name AS name FROM peer_turns t"
                " LEFT JOIN peers p ON 'peer:' || p.peer_id = t.principal"
                " WHERE t.session_key = ?",
                (session_key,),
            ).fetchall()
        return {
            r["operation_id"]: {
                "name": r["name"] or "",
                "peer_id": r["principal"][len(PRINCIPAL_PEER) :],
                "grant_id": r["grant_id"],
                "actor": r["actor"],
                "via": r["via"],
                "edited": bool(r["edited"]),
            }
            for r in rows
        }

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
                " expires_at, revision, seen_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        r["kind"],
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
