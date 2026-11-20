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
* **Working together** (schema 5, #2059): ``access_requests`` (a peer asks for a higher level;
  only a record, it never changes a grant), ``peer_decisions`` (the identity of a decision a peer
  gave, written BEFORE the runtime is called), ``decision_authors`` (who settled a request) and
  ``notes`` with ``note_changes`` (plain-text notes between the two operators, with a per-grant
  monotonic change number). A note's text is read by ``federation/`` and its routes and by
  nothing else: it never reaches an agent or a model (``docs/invariants/federation-notes.md``).
  A grant that ends ends its pending access request in the same transaction, and no note read
  or write that a peer can cause succeeds for a grant that is not live.
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

SCHEMA_VERSION = 5
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
ACCESS_STATES = ("pending", "granted", "declined", "expired", "withdrawn")
#: A pending access request nobody answered expires after this, or with its grant.
ACCESS_TTL_S = 24 * 3600
#: Access requests one grant may make in 24 hours (a decline is not an invitation to ask again
#: at once); the next is refused.
ACCESS_PER_DAY_MAX = 5
ACCESS_NOTE_MAX = 280
AUTHOR_OWNER = "owner"
NOTE_TEXT_MAX = 2000
#: Notes one grant's thread holds; past it the oldest are dropped (a change of its own).
NOTES_PER_GRANT_MAX = 500
#: Notes one author may add to one thread in an hour.
NOTES_PER_AUTHOR_HOUR = 30
#: Change rows kept per grant. A reader whose cursor is older gets the whole thread again.
NOTE_CHANGES_KEPT = 2000
NOTE_CHANGES = ("add", "edit", "hide", "delete", "evict")
#: After a grant ended its notes stay readable by THIS instance's operator for this long (by
#: the operator's routes, by no verb), then the sweep deletes them.
NOTES_GRACE_S = 7 * 86400
DECISION_OUTCOMES = ("accepted", "settled", "not_pending")

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

# Schema 5 (#2059): working together on a shared session.
_SCHEMA_V5 = (
    # A peer asks for a higher level. ONLY a record: nothing here changes a grant. ``notified``
    # says a bell row may exist for it (as for a suggestion).
    """
    CREATE TABLE access_requests (
        request_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        level TEXT NOT NULL,
        note TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('pending', 'granted', 'declined', 'expired', 'withdrawn')),
        end_reason TEXT,
        notified INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    # At most one pending request per grant: the database is the arbiter.
    "CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (grant_id)"
    " WHERE state = 'pending'",
    "CREATE INDEX access_requests_by_session ON access_requests (session_key, created_at)",
    # The identity of a decision a peer gave, written BEFORE the runtime is called.
    # ``decision_id`` is minted with the row and never changes: a retry with the same
    # ``client_key`` replays exactly this decision. ``choice`` is the canonical JSON of what the
    # peer chose (a kind and option indexes; never a payload). ``digest`` names the exact request
    # the runtime presented. ``outcome`` is NULL until it is known.
    """
    CREATE TABLE peer_decisions (
        decision_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        session_key TEXT NOT NULL,
        client_key TEXT NOT NULL,
        principal TEXT NOT NULL,
        actor TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        choice TEXT NOT NULL,
        kind TEXT NOT NULL,
        outcome TEXT CHECK (outcome IS NULL OR outcome IN
            ('accepted', 'settled', 'not_pending')),
        outcome_at REAL,
        created_at REAL NOT NULL,
        UNIQUE (grant_id, client_key)
    )
    """,
    "CREATE INDEX peer_decisions_by_request ON peer_decisions (session_key, turn_id, request_id)",
    # Who settled a request: ``owner``, or ``peer:<id>`` with its actor label. One row per
    # request, written once; the first write stays.
    """
    CREATE TABLE decision_authors (
        session_key TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        author TEXT NOT NULL,
        actor TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        at REAL NOT NULL,
        PRIMARY KEY (session_key, turn_id, request_id, digest)
    )
    """,
    "CREATE INDEX decision_authors_by_session ON decision_authors (session_key, at)",
    # Notes between the two operators of a grant. ``author`` is ``owner`` or ``peer:<id>``.
    # ``seen`` says the OTHER side read it. A deleted note keeps its row with an empty text (a
    # tombstone); ``hidden`` is the owner's, and removes the note from every read by the peer.
    """
    CREATE TABLE notes (
        note_id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL,
        author TEXT NOT NULL,
        actor TEXT NOT NULL,
        client_key TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at REAL NOT NULL,
        edited_at REAL,
        deleted INTEGER NOT NULL DEFAULT 0,
        hidden INTEGER NOT NULL DEFAULT 0,
        seen INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL,
        UNIQUE (grant_id, author, client_key),
        UNIQUE (grant_id, position)
    )
    """,
    # One row per change to a thread. ``seq`` is per grant and only ever grows.
    """
    CREATE TABLE note_changes (
        grant_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        note_id TEXT NOT NULL,
        change TEXT NOT NULL CHECK (change IN ('add', 'edit', 'hide', 'delete', 'evict')),
        at REAL NOT NULL,
        PRIMARY KEY (grant_id, seq)
    )
    """,
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


class RateLimited(StoreError):
    """A bound on how often something may be added was reached."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=429)


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
class AccessRequest:
    """A peer's request for a higher level on one grant. A record: it changes nothing."""

    request_id: str
    grant_id: str
    session_key: str
    principal: str
    level: str
    note: str
    state: str
    end_reason: str | None
    notified: bool
    created_at: float
    updated_at: float
    expires_at: float

    @property
    def peer_id(self) -> str:
        return self.principal[len(PRINCIPAL_PEER) :]


@dataclass(frozen=True)
class PeerDecision:
    """The stored identity of one decision a peer gave (``peer_decisions``)."""

    decision_id: str
    grant_id: str
    session_key: str
    client_key: str
    principal: str
    actor: str
    turn_id: str
    request_id: str
    digest: str
    choice: str
    kind: str
    outcome: str | None
    outcome_at: float | None
    created_at: float

    @property
    def peer_id(self) -> str:
        return self.principal[len(PRINCIPAL_PEER) :]


@dataclass(frozen=True)
class Note:
    """One note of a grant's thread. ``text`` is read by ``federation/`` and its routes only."""

    note_id: str
    grant_id: str
    author: str
    actor: str
    text: str
    created_at: float
    edited_at: float | None
    deleted: bool
    hidden: bool
    seen: bool
    position: int


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


def _access(row: sqlite3.Row) -> AccessRequest:
    return AccessRequest(
        request_id=row["request_id"],
        grant_id=row["grant_id"],
        session_key=row["session_key"],
        principal=row["principal"],
        level=row["level"],
        note=row["note"],
        state=row["state"],
        end_reason=row["end_reason"],
        notified=bool(row["notified"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _decision(row: sqlite3.Row) -> PeerDecision:
    return PeerDecision(
        decision_id=row["decision_id"],
        grant_id=row["grant_id"],
        session_key=row["session_key"],
        client_key=row["client_key"],
        principal=row["principal"],
        actor=row["actor"],
        turn_id=row["turn_id"],
        request_id=row["request_id"],
        digest=row["digest"],
        choice=row["choice"],
        kind=row["kind"],
        outcome=row["outcome"],
        outcome_at=row["outcome_at"],
        created_at=row["created_at"],
    )


def _note_row(row: sqlite3.Row) -> Note:
    return Note(
        note_id=row["note_id"],
        grant_id=row["grant_id"],
        author=row["author"],
        actor=row["actor"],
        text=row["text"],
        created_at=row["created_at"],
        edited_at=row["edited_at"],
        deleted=bool(row["deleted"]),
        hidden=bool(row["hidden"]),
        seen=bool(row["seen"]),
        position=row["position"],
    )


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
                        (5, _SCHEMA_V5),
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
            # Also the same transaction (#2059): a pending access request ends with its grant.
            # Notes need no write here: every read or write a peer can cause re-reads the grant
            # inside its own transaction (`_serving`), so an ended grant serves none.
            self._end_access(con, row["grant_id"], f"grant_{reason}", now)
        if rows:
            self._bump(con)
        return [_grant(r) for r in rows]

    @staticmethod
    def _end_access(con: sqlite3.Connection, grant_id: str, reason: str, now: float) -> None:
        con.execute(
            "UPDATE access_requests SET state = 'expired', end_reason = ?, updated_at = ?"
            " WHERE grant_id = ? AND state = 'pending'",
            (reason, now, grant_id),
        )

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
            # A pending access request is GRANTED by this change when the grant now has the
            # level it asked for (#2059). The request never changed anything by itself: this
            # is the operator's own grant change, recorded on the request.
            asked = con.execute(
                "SELECT request_id, level FROM access_requests"
                " WHERE grant_id = ? AND state = 'pending'",
                (grant_id,),
            ).fetchone()
            if asked is not None and asked["level"] in LEVEL_RANK:
                if level_includes(new_level, asked["level"]):
                    con.execute(
                        "UPDATE access_requests SET state = 'granted', updated_at = ?"
                        " WHERE request_id = ? AND state = 'pending'",
                        (now, asked["request_id"]),
                    )
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

    def suggestion_state(self, suggestion_id: str) -> str | None:
        """One suggestion's state and nothing else of it (no text): what the bell is judged
        on."""
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT state FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)
            ).fetchone()
        return row["state"] if row else None

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

    def operation_status(self, grant_id: str, client_key: str) -> dict | None:
        """What became of ONE message a peer submitted under ``grant_id`` with ``client_key``
        (``input.status``, #2061): a sent message's stored receipt, a suggestion's state, or
        ``None`` when this grant has no such key. Keyed by the grant AND the key, so a key of
        another grant is simply not found. No text, no operation id."""
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            turn = con.execute(
                "SELECT handoff FROM peer_turns"
                " WHERE grant_id = ? AND client_key = ? AND via = 'send'",
                (grant_id, client_key),
            ).fetchone()
            if turn is not None:
                return {"kind": "send", "handoff": turn["handoff"]}
            row = con.execute(
                "SELECT suggestion_id, state, handoff FROM suggestions"
                " WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": "suggest",
            "suggestion_id": row["suggestion_id"],
            "state": row["state"],
            "handoff": row["handoff"],
        }

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

    # ── access requests (#2059) ───────────────────────────────────────
    def add_access_request(
        self,
        *,
        grant_id: str,
        peer_id: str,
        revision: int,
        level: str,
        note: str,
        now: float | None = None,
    ) -> tuple[AccessRequest, bool]:
        """Record that a peer asks for ``level`` on its grant. ``(request, fresh)``.

        ONE ``BEGIN IMMEDIATE`` transaction: the grant is re-read (live, this peer's, the
        revision it holds, the peer trusted), a request whose time ran out is expired, and a
        request that is still pending is returned as it is (``fresh`` false: a second request
        replaces nothing). The level must be above the grant's current one (:class:`Invalid`).
        At most :data:`ACCESS_PER_DAY_MAX` requests per grant in 24 hours
        (:class:`RateLimited`). **It writes no grant column**: only the operator's grant
        change does, and that change is what marks a request ``granted``."""
        now = time.time() if now is None else now
        with self._write() as con:
            grant = self._serving(con, grant_id, peer_id, "view", now, revision)
            con.execute(
                "UPDATE access_requests SET state = 'expired', end_reason = 'expired',"
                " updated_at = ? WHERE grant_id = ? AND state = 'pending' AND expires_at <= ?",
                (now, grant_id, now),
            )
            row = con.execute(
                "SELECT * FROM access_requests WHERE grant_id = ? AND state = 'pending'",
                (grant_id,),
            ).fetchone()
            if row is not None:
                return _access(row), False
            if LEVEL_RANK.get(level, -1) <= LEVEL_RANK.get(grant["level"], len(LEVEL_RANK)):
                raise Invalid("that level is not above the grant's current one")
            recent = con.execute(
                "SELECT COUNT(*) FROM access_requests WHERE grant_id = ? AND created_at > ?",
                (grant_id, now - ACCESS_TTL_S),
            ).fetchone()[0]
            if recent >= ACCESS_PER_DAY_MAX:
                raise RateLimited("too many access requests under this grant today")
            request_id = new_grant_id()
            con.execute(
                "INSERT INTO access_requests (request_id, grant_id, session_key, principal,"
                " level, note, state, created_at, updated_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    request_id,
                    grant_id,
                    grant["session_key"],
                    grant["principal"],
                    level,
                    note,
                    now,
                    now,
                    min(now + ACCESS_TTL_S, grant["expires_at"]),
                ),
            )
            row = con.execute(
                "SELECT * FROM access_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return _access(row), True

    def get_access_request(self, request_id: str) -> AccessRequest | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM access_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return _access(row) if row else None

    def list_access_requests(
        self, *, session_key: str | None = None, limit: int = 100
    ) -> list[AccessRequest]:
        """Access requests with the peer's note, newest first: what the OPERATOR's screen
        shows."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM access_requests WHERE (? IS NULL OR session_key = ?)"
                " ORDER BY created_at DESC, request_id LIMIT ?",
                (session_key, session_key, limit),
            ).fetchall()
        return [_access(r) for r in rows]

    def access_status(self, grant_id: str, *, now: float | None = None) -> dict | None:
        """What the PEER reads of its newest request under one grant: id, level, state and
        times. A pending one whose time ran out reads ``expired`` whether or not the sweep ran."""
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            row = con.execute(
                "SELECT request_id, level, state, created_at, updated_at, expires_at"
                " FROM access_requests WHERE grant_id = ?"
                " ORDER BY created_at DESC, request_id LIMIT 1",
                (grant_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        if out["state"] == "pending" and out["expires_at"] <= now:
            out["state"] = "expired"
        return out

    def withdraw_access_request(
        self, *, grant_id: str, peer_id: str, revision: int, now: float | None = None
    ) -> bool:
        """THE PEER takes its pending request back (#2062): ``pending → withdrawn``. ONE
        ``BEGIN IMMEDIATE`` transaction that re-reads the grant exactly as the request did.
        It narrows: no grant column is written and nothing is created. ``True`` when a pending
        request was withdrawn; ``False`` when there was none (already answered, or never
        asked), which is not an error."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._serving(con, grant_id, peer_id, "view", now, revision)
            cur = con.execute(
                "UPDATE access_requests SET state = 'withdrawn', end_reason = 'peer',"
                " updated_at = ? WHERE grant_id = ? AND state = 'pending' AND expires_at > ?",
                (now, grant_id, now),
            )
            return cur.rowcount > 0

    def note_counts(self, grant_ids: list[str]) -> dict[str, int]:
        """``{grant_id: how many notes its thread shows}`` (tombstones excluded) for the
        OPERATOR's own listing of its grants (#2062). A number per grant and nothing of any
        note: no text, no author, no time."""
        if not grant_ids or not self.db_path.exists():
            return {}  # never used: reading creates nothing
        marks = ",".join("?" for _ in grant_ids)
        with self._connect() as con:
            rows = con.execute(
                "SELECT grant_id, COUNT(*) AS n FROM notes"  # noqa: S608 — placeholders only
                f" WHERE deleted = 0 AND grant_id IN ({marks}) GROUP BY grant_id",
                grant_ids,
            ).fetchall()
        return {r["grant_id"]: int(r["n"]) for r in rows}

    def decline_access_request(self, request_id: str, *, now: float | None = None) -> AccessRequest:
        """The operator declines: ``pending → declined``. Compare-and-set; a request that
        already ended raises :class:`StateConflict` naming how."""
        now = time.time() if now is None else now
        with self._write() as con:
            cur = con.execute(
                "UPDATE access_requests SET state = 'declined', end_reason = 'declined',"
                " updated_at = ? WHERE request_id = ? AND state = 'pending' AND expires_at > ?",
                (now, request_id, now),
            )
            row = con.execute(
                "SELECT * FROM access_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise NotFound("unknown access request")
            if cur.rowcount == 0:
                raise StateConflict("expired" if row["state"] == "pending" else row["state"])
        return _access(row)

    def expire_access_requests(self, *, now: float | None = None) -> int:
        """Pending access requests nobody answered in time become ``expired``."""
        if not self.db_path.exists():
            return 0  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            due = con.execute(
                "SELECT 1 FROM access_requests WHERE state = 'pending' AND expires_at <= ?"
                " LIMIT 1",
                (now,),
            ).fetchone()
        if due is None:
            return 0  # the common round: no write lock taken
        with self._write() as con:
            return con.execute(
                "UPDATE access_requests SET state = 'expired', end_reason = 'expired',"
                " updated_at = ? WHERE state = 'pending' AND expires_at <= ?",
                (now, now),
            ).rowcount

    def unretracted_access(self) -> list[str]:
        """Access requests that are no longer pending and may still have a bell row."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT request_id FROM access_requests WHERE notified = 1 AND state != 'pending'"
            ).fetchall()
        return [r["request_id"] for r in rows]

    def mark_access_retracted(self, request_ids: list[str]) -> None:
        with self._write() as con:
            con.executemany(
                "UPDATE access_requests SET notified = 0 WHERE request_id = ?"
                " AND state != 'pending'",
                [(r,) for r in request_ids],
            )

    # ── decisions a peer gives (#2059) ────────────────────────────────
    def record_peer_decision(
        self,
        *,
        grant_id: str,
        session_key: str,
        peer_id: str,
        client_key: str,
        actor: str,
        turn_id: str,
        request_id: str,
        digest: str,
        choice: str,
        kind: str,
        now: float | None = None,
    ) -> tuple[PeerDecision, bool]:
        """The identity of a decision a peer gives, persisted BEFORE the runtime is called.
        ``(row, fresh)``. ONE transaction that re-reads the grant (live, this peer's, at least
        ``send``, the peer trusted). The same ``client_key`` on the same grant returns the row
        that is already there, unchanged: the ``decision_id`` is minted once and a retry never
        gets another. Whether a retry names the same request and choice is the caller's check."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._serving(con, grant_id, peer_id, "send", now)
            row = con.execute(
                "SELECT * FROM peer_decisions WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
            if row is not None:
                return _decision(row), False
            decision_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO peer_decisions (decision_id, grant_id, session_key, client_key,"
                " principal, actor, turn_id, request_id, digest, choice, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    grant_id,
                    session_key,
                    client_key,
                    principal_of(peer_id),
                    actor,
                    turn_id,
                    request_id,
                    digest,
                    choice,
                    kind,
                    now,
                ),
            )
            row = con.execute(
                "SELECT * FROM peer_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return _decision(row), True

    def get_peer_decision(self, grant_id: str, client_key: str) -> PeerDecision | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM peer_decisions WHERE grant_id = ? AND client_key = ?",
                (grant_id, client_key),
            ).fetchone()
        return _decision(row) if row else None

    def peer_decision(self, decision_id: str) -> PeerDecision | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM peer_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return _decision(row) if row else None

    def open_peer_decisions(self, *, limit: int = 50) -> list[PeerDecision]:
        """Decisions whose outcome was never recorded (a crash, a lost answer), oldest first."""
        if not self.db_path.exists():
            return []  # never used: reading creates nothing
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM peer_decisions WHERE outcome IS NULL ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decision(r) for r in rows]

    def note_peer_decision(
        self, decision_id: str, outcome: str, *, now: float | None = None
    ) -> bool:
        """What became of a peer's decision. True when this is the FIRST outcome recorded for
        it (a replay's is not, and never overwrites the first)."""
        if outcome not in DECISION_OUTCOMES:
            raise Invalid("unknown outcome")
        now = time.time() if now is None else now
        with self._write() as con:
            return bool(
                con.execute(
                    "UPDATE peer_decisions SET outcome = ?, outcome_at = ?"
                    " WHERE decision_id = ? AND outcome IS NULL",
                    (outcome, now, decision_id),
                ).rowcount
            )

    def record_decision_author(
        self,
        *,
        session_key: str,
        turn_id: str,
        request_id: str,
        digest: str,
        author: str,
        actor: str,
        decision_id: str,
        kind: str,
        now: float | None = None,
    ) -> dict:
        """WHO settled one request, written once: the first write stays, and what is returned
        is always the STORED row, so every caller reports the same author whatever it meant to
        write."""
        now = time.time() if now is None else now
        key = (session_key, turn_id, request_id, digest)
        with self._write() as con:
            con.execute(
                "INSERT OR IGNORE INTO decision_authors (session_key, turn_id, request_id,"
                " digest, author, actor, decision_id, kind, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, author, actor, decision_id, kind, now),
            )
            row = con.execute(
                "SELECT * FROM decision_authors WHERE session_key = ? AND turn_id = ?"
                " AND request_id = ? AND digest = ?",
                key,
            ).fetchone()
        return dict(row)

    def decision_author(
        self, session_key: str, turn_id: str, request_id: str, digest: str
    ) -> dict | None:
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM decision_authors WHERE session_key = ? AND turn_id = ?"
                " AND request_id = ? AND digest = ?",
                (session_key, turn_id, request_id, digest),
            ).fetchone()
        return dict(row) if row else None

    def decision_author_in_turn(
        self, session_key: str, turn_id: str, request_id: str
    ) -> dict | None:
        """The newest stored author of a request of ONE turn, for a caller that does not know
        its digest. Never by request id alone: native request ids repeat from turn to turn."""
        if not self.db_path.exists():
            return None  # never used: reading creates nothing
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM decision_authors WHERE session_key = ? AND turn_id = ?"
                " AND request_id = ? ORDER BY at DESC, rowid DESC LIMIT 1",
                (session_key, turn_id, request_id),
            ).fetchone()
        return dict(row) if row else None

    def decision_authors(self, session_key: str, *, limit: int = 20) -> list[dict]:
        """The newest settled requests of a session with who settled each, oldest first:
        ``{"request_id", "turn_id", "kind", "author", "actor", "decision_id", "name", "at"}``.
        ``name`` is the
        peer's display name on THIS instance (empty for the owner)."""
        if not self.db_path.exists():
            return []  # never used: nobody ever decided through here
        with self._connect() as con:
            rows = con.execute(
                "SELECT d.request_id, d.turn_id, d.kind, d.author, d.actor, d.decision_id, d.at,"
                " p.name AS name"
                " FROM decision_authors d"
                " LEFT JOIN peers p ON 'peer:' || p.peer_id = d.author"
                " WHERE d.session_key = ? ORDER BY d.at DESC, d.rowid DESC LIMIT ?",
                (session_key, limit),
            ).fetchall()
        return [{**dict(r), "name": r["name"] or ""} for r in reversed(rows)]

    def session_shared_ever(self, session_key: str) -> bool:
        """Whether this store ever held a grant for the session (in any state)."""
        if not self.db_path.exists():
            return False  # never used: reading creates nothing
        with self._connect() as con:
            return (
                con.execute(
                    "SELECT 1 FROM grants WHERE session_key = ? LIMIT 1", (session_key,)
                ).fetchone()
                is not None
            )

    # ── notes between the two operators (#2059) ───────────────────────
    @staticmethod
    def _live_grant(con: sqlite3.Connection, grant_id: str, now: float) -> sqlite3.Row:
        """The grant row while it is live: what an OWNER-side note write needs. Notes are for
        API grants only: a terminal grant (#2056) has no thread, from either side."""
        row = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        if row is None:
            raise NotFound("unknown grant")
        if row["kind"] != KIND_API:
            raise Invalid(f"a {row['kind']} grant has no notes")
        if row["state"] != "live" or row["expires_at"] <= now:
            raise NotGranted("grant ended")
        return row

    def _reach(
        self, con: sqlite3.Connection, grant_id: str, peer: tuple[str, int] | None, now: float
    ) -> sqlite3.Row:
        """The live grant a note call is made under, inside the caller's transaction. ``peer``
        is ``(peer_id, revision)`` for a call a PEER causes: the grant must then serve that
        peer with that revision right now. ``None`` is this instance's own operator."""
        if peer is None:
            return self._live_grant(con, grant_id, now)
        return self._serving(con, grant_id, peer[0], "view", now, peer[1])

    @staticmethod
    def _note_change(
        con: sqlite3.Connection, grant_id: str, note_id: str, change: str, now: float
    ) -> int:
        """The next number of the grant's change counter, with its row. The counter never goes
        back: the newest rows are the ones kept, so ``MAX(seq)`` is always the last one given."""
        seq = (
            con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM note_changes WHERE grant_id = ?", (grant_id,)
            ).fetchone()[0]
            + 1
        )
        con.execute(
            "INSERT INTO note_changes (grant_id, seq, note_id, change, at) VALUES (?, ?, ?, ?, ?)",
            (grant_id, seq, note_id, change, now),
        )
        con.execute(
            "DELETE FROM note_changes WHERE grant_id = ? AND seq <= ?",
            (grant_id, seq - NOTE_CHANGES_KEPT),
        )
        return seq

    def add_note(
        self,
        *,
        grant_id: str,
        author: str,
        actor: str,
        client_key: str,
        text: str,
        peer: tuple[str, int] | None = None,
        now: float | None = None,
    ) -> tuple[Note, bool]:
        """Add a note to a grant's thread. ``(note, fresh)``. ONE transaction: the grant is
        re-read (:meth:`_reach`), the retry key is looked up (the same ``client_key`` from the
        same author returns the note that is already there; for the PEER a note the owner hid
        is :class:`NotFound`, as it is to its edit and delete), the author's hourly bound is
        counted (:class:`RateLimited`), the note and its change are written, and the oldest
        notes past :data:`NOTES_PER_GRANT_MAX` are dropped, each with a change of its own."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._reach(con, grant_id, peer, now)
            row = con.execute(
                "SELECT * FROM notes WHERE grant_id = ? AND author = ? AND client_key = ?",
                (grant_id, author, client_key),
            ).fetchone()
            if row is not None:
                if peer is not None and row["hidden"]:
                    # A note the owner hid is gone from every later read by the peer, a
                    # repeat of its own add included: the one answer of `_own_note`, never
                    # the row and its text. Nothing is written and nothing un-hides.
                    raise NotFound("unknown note")
                return _note_row(row), False
            recent = con.execute(
                "SELECT COUNT(*) FROM notes WHERE grant_id = ? AND author = ? AND created_at > ?",
                (grant_id, author, now - 3600),
            ).fetchone()[0]
            if recent >= NOTES_PER_AUTHOR_HOUR:
                raise RateLimited("too many notes in the last hour")
            note_id = new_grant_id()
            seq = self._note_change(con, grant_id, note_id, "add", now)
            con.execute(
                "INSERT INTO notes (note_id, grant_id, author, actor, client_key, text,"
                " created_at, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (note_id, grant_id, author, actor, client_key, text, now, seq),
            )
            over = (
                con.execute(
                    "SELECT COUNT(*) FROM notes WHERE grant_id = ?", (grant_id,)
                ).fetchone()[0]
                - NOTES_PER_GRANT_MAX
            )
            if over > 0:
                oldest = con.execute(
                    "SELECT note_id FROM notes WHERE grant_id = ? ORDER BY position LIMIT ?",
                    (grant_id, over),
                ).fetchall()
                for gone in oldest:
                    con.execute("DELETE FROM notes WHERE note_id = ?", (gone["note_id"],))
                    self._note_change(con, grant_id, gone["note_id"], "evict", now)
            row = con.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return _note_row(row), True

    @staticmethod
    def _own_note(
        con: sqlite3.Connection, grant_id: str, note_id: str, author: str, *, peer: bool
    ) -> sqlite3.Row:
        """A note of this grant written by ``author``. Anything else (another grant's, the
        other side's, one the owner hid from the peer) is unknown: one answer."""
        row = con.execute(
            "SELECT * FROM notes WHERE note_id = ? AND grant_id = ?", (note_id, grant_id)
        ).fetchone()
        if row is None or row["author"] != author or (peer and row["hidden"]):
            raise NotFound("unknown note")
        return row

    def edit_note(
        self,
        *,
        grant_id: str,
        note_id: str,
        author: str,
        text: str,
        peer: tuple[str, int] | None = None,
        now: float | None = None,
    ) -> Note:
        """The author changes their OWN note's text. A deleted note is not edited
        (:class:`Conflict`)."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._reach(con, grant_id, peer, now)
            row = self._own_note(con, grant_id, note_id, author, peer=peer is not None)
            if row["deleted"]:
                raise Conflict("that note was deleted")
            if row["text"] != text:
                con.execute(
                    "UPDATE notes SET text = ?, edited_at = ? WHERE note_id = ?",
                    (text, now, note_id),
                )
                self._note_change(con, grant_id, note_id, "edit", now)
            row = con.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return _note_row(row)

    def tombstone_note(
        self,
        *,
        grant_id: str,
        note_id: str,
        author: str,
        peer: tuple[str, int] | None = None,
        now: float | None = None,
    ) -> Note:
        """The author deletes their OWN note. The row stays as a tombstone with no text."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._reach(con, grant_id, peer, now)
            row = self._own_note(con, grant_id, note_id, author, peer=peer is not None)
            if not row["deleted"]:
                con.execute(
                    "UPDATE notes SET deleted = 1, text = '', edited_at = ? WHERE note_id = ?",
                    (now, note_id),
                )
                self._note_change(con, grant_id, note_id, "delete", now)
            row = con.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return _note_row(row)

    def hide_note(self, *, grant_id: str, note_id: str, now: float | None = None) -> Note:
        """THE OWNER hides any note of the thread: gone from every later read by the peer. It
        narrows, so it needs no live grant; hiding again changes nothing."""
        now = time.time() if now is None else now
        with self._write() as con:
            row = con.execute(
                "SELECT * FROM notes WHERE note_id = ? AND grant_id = ?", (note_id, grant_id)
            ).fetchone()
            if row is None:
                raise NotFound("unknown note")
            if not row["hidden"]:
                con.execute("UPDATE notes SET hidden = 1 WHERE note_id = ?", (note_id,))
                self._note_change(con, grant_id, note_id, "hide", now)
            row = con.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
        return _note_row(row)

    def mark_notes_seen(
        self,
        *,
        grant_id: str,
        reader: str,
        upto: int,
        peer: tuple[str, int] | None = None,
        now: float | None = None,
    ) -> int:
        """``reader`` (``owner`` or ``peer:<id>``) has read the other side's notes up to the
        change number ``upto``. Not a change to the thread. Returns how many became seen."""
        now = time.time() if now is None else now
        with self._write() as con:
            self._reach(con, grant_id, peer, now)
            return con.execute(
                "UPDATE notes SET seen = 1 WHERE grant_id = ? AND author != ? AND seen = 0"
                " AND position <= ?",
                (grant_id, reader, upto),
            ).rowcount

    @staticmethod
    def _unread(con: sqlite3.Connection, grant_id: str, reader: str) -> int:
        return con.execute(
            "SELECT COUNT(*) FROM notes WHERE grant_id = ? AND author != ? AND seen = 0"
            " AND deleted = 0 AND hidden = 0",
            (grant_id, reader),
        ).fetchone()[0]

    def unread_notes(self, grant_id: str, reader: str) -> int:
        """Notes of the OTHER side that ``reader`` has not read and could still read."""
        if not self.db_path.exists():
            return 0  # never used: reading creates nothing
        with self._connect() as con:
            return self._unread(con, grant_id, reader)

    def note_page(
        self,
        *,
        grant_id: str,
        after_seq: int,
        limit: int,
        reader: str,
        cursor: str | None = None,
        peer: tuple[str, int] | None = None,
        now: float | None = None,
    ) -> dict:
        """What changed in a thread after the change number ``after_seq``, in ONE read
        transaction that also re-reads the grant (:meth:`_reach`).

        ``{"reset", "seq", "newest", "entries", "more", "unread"}``. ``entries`` is a list of
        ``(seq, note_id, Note | None)``: the note as it is NOW, or ``None`` when its row is
        gone (evicted). A reader that only ever asks "what changed since" therefore receives an
        edit, a hide and a tombstone of an old note, and an eviction.

        ``reset`` is true when the changes after ``after_seq`` are no longer all retained (or
        the cursor is from a future this thread never had), and when ``cursor`` continues such
        a listing: ``entries`` are then the CURRENT notes in thread order after the note
        ``cursor``, each with its own position as ``seq``, and the caller replaces what it has.
        A ``cursor`` that no longer names a note starts the listing again."""
        now = time.time() if now is None else now
        with self._read() as con:
            self._reach(con, grant_id, peer, now)
            bounds = con.execute(
                "SELECT COALESCE(MAX(seq), 0) AS newest, MIN(seq) AS floor FROM note_changes"
                " WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            newest, floor = bounds["newest"], bounds["floor"]
            unread = self._unread(con, grant_id, reader)
            start = None
            if cursor is not None:
                at = con.execute(
                    "SELECT position FROM notes WHERE note_id = ? AND grant_id = ?",
                    (cursor, grant_id),
                ).fetchone()
                start = at["position"] if at is not None else 0
            lost = after_seq > newest or (floor is not None and after_seq + 1 < floor)
            if start is not None or lost:
                rows = con.execute(
                    "SELECT * FROM notes WHERE grant_id = ? AND position > ?"
                    " ORDER BY position LIMIT ?",
                    (grant_id, start or 0, limit + 1),
                ).fetchall()
                page = rows[:limit]
                first_page = start is None or start == 0
                return {
                    "reset": True,
                    # The first page fixes the number the caller continues from; a later page
                    # hands the caller's own number back.
                    "seq": newest if first_page or after_seq > newest else after_seq,
                    "newest": newest,
                    "entries": [(r["position"], r["note_id"], _note_row(r)) for r in page],
                    "more": len(rows) > limit,
                    "unread": unread,
                }
            changes = con.execute(
                "SELECT seq, note_id FROM note_changes WHERE grant_id = ? AND seq > ?"
                " ORDER BY seq LIMIT ?",
                (grant_id, after_seq, limit),
            ).fetchall()
            last: dict[str, int] = {}
            for change in changes:
                last[change["note_id"]] = change["seq"]  # the newest change of each note wins
            entries = []
            for note_id, seq in sorted(last.items(), key=lambda item: item[1]):
                row = con.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
                entries.append((seq, note_id, _note_row(row) if row is not None else None))
            covered = changes[-1]["seq"] if changes else max(after_seq, 0)
            return {
                "reset": False,
                "seq": covered if changes else newest,
                "newest": newest,
                "entries": entries,
                "more": bool(changes) and covered < newest,
                "unread": unread,
            }

    def owner_notes(
        self, grant_id: str, *, now: float | None = None
    ) -> tuple[Grant, list[Note], int]:
        """``(grant, notes, newest change number)``: the whole thread for THIS instance's
        operator, hidden notes included. Readable
        while the grant is live and for :data:`NOTES_GRACE_S` after it ended; no verb calls
        this."""
        now = time.time() if now is None else now
        if not self.db_path.exists():
            raise NotFound("unknown grant")
        with self._read() as con:
            grant = con.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if grant is None:
                raise NotFound("unknown grant")
            ended = grant["ended_at"]
            if ended is None and grant["expires_at"] <= now:
                ended = grant["expires_at"]  # expired and not swept yet
            if ended is not None and ended + NOTES_GRACE_S <= now:
                raise NotFound("this share's notes were deleted")
            rows = con.execute(
                "SELECT * FROM notes WHERE grant_id = ? ORDER BY position", (grant_id,)
            ).fetchall()
            newest = con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM note_changes WHERE grant_id = ?", (grant_id,)
            ).fetchone()[0]
        return _grant(grant), [_note_row(r) for r in rows], int(newest)

    def note_grants_unread(
        self, reader: str = AUTHOR_OWNER, *, now: float | None = None
    ) -> dict[str, int]:
        """``{grant_id: notes of the other side that ``reader`` has not read}`` over the LIVE
        grants: for the owner (every grant), what its bell row for a thread is decided on; for
        ``peer:<id>`` (that peer's grants), which threads it is hinted about."""
        if not self.db_path.exists():
            return {}  # never used: reading creates nothing
        now = time.time() if now is None else now
        with self._connect() as con:
            rows = con.execute(
                "SELECT n.grant_id AS grant_id, COUNT(*) AS unread FROM notes n"
                " JOIN grants g ON g.grant_id = n.grant_id"
                " WHERE g.state = 'live' AND g.expires_at > ? AND n.author != ?"
                " AND (? = 'owner' OR g.principal = ?)"
                " AND n.seen = 0 AND n.deleted = 0 AND n.hidden = 0 GROUP BY n.grant_id",
                (now, reader, reader, reader),
            ).fetchall()
        return {r["grant_id"]: int(r["unread"]) for r in rows}

    def purge_notes(self, *, now: float | None = None) -> int:
        """The sweep: delete the notes (and their changes) of grants that ended more than
        :data:`NOTES_GRACE_S` ago. Returns how many notes went."""
        if not self.db_path.exists():
            return 0  # never used: reading creates nothing
        now = time.time() if now is None else now
        ended = (
            "SELECT grant_id FROM grants WHERE state != 'live' AND ended_at IS NOT NULL"
            " AND ended_at <= ?"
        )
        with self._connect() as con:
            due = con.execute(
                f"SELECT 1 FROM notes WHERE grant_id IN ({ended}) LIMIT 1",  # noqa: S608 — fixed
                (now - NOTES_GRACE_S,),
            ).fetchone()
            stale = con.execute(
                f"SELECT 1 FROM note_changes WHERE grant_id IN ({ended}) LIMIT 1",  # noqa: S608
                (now - NOTES_GRACE_S,),
            ).fetchone()
        if due is None and stale is None:
            return 0  # the common round: no write lock taken
        with self._write() as con:
            gone = con.execute(
                f"DELETE FROM notes WHERE grant_id IN ({ended})",  # noqa: S608 — fixed fragment
                (now - NOTES_GRACE_S,),
            ).rowcount
            con.execute(
                f"DELETE FROM note_changes WHERE grant_id IN ({ended})",  # noqa: S608
                (now - NOTES_GRACE_S,),
            )
        return gone

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
