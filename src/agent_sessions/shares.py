"""Public share links (#1404): the grant store and the public authorization boundary.

A public link lets someone with no BattleLab account open ONE session — API sessions view-only or
"can send messages", terminal sessions view-only — optionally behind a password and an expiry the
operator sets when creating it. The rules this module holds are pinned in
``docs/invariants/public-shares.md``; the short form:

* **The token is never stored.** A link carries ``secrets.token_urlsafe(32)``; the row keeps
  ``template_secrets.keyed_digest("public-share", token)``. The digest is keyed by the
  template-secrets key file, and each row records that key's id: a replaced key ends every link,
  for a new unlock AND for a visitor already holding a cookie (fail closed). Because only the
  digest is kept, the URL is shown to the operator once, at creation.
* **A cookie never grants access on its own.** Unlocking trades the token (+ password) for a
  share cookie that names the ``share_id``; every public request re-reads the row through
  :meth:`ShareStore.active` (not revoked, not expired) before doing anything.
* **Archive ends a link, or does not happen.** Every archive path runs the provider's archive
  inside :func:`archiving`, which takes the session's lock, revokes its links (raising
  :class:`SharesUnavailable` when it cannot, so the archive aborts) and holds the lock until the
  move is done. Creating a link takes the same lock and refuses an archived or unknown session, so
  no link can be minted in the window. Unarchiving does not revive a link.
* **Revoke is fenced against a racing send.** :meth:`ShareStore.fence` is a per-share lock that
  :meth:`ShareStore.revoke` takes too; the send routes (P2) re-check the share inside it at the
  mutation seam, so a revoke that wins blocks the effect.
* **Open streams hear about a revoke in-process** through :meth:`ShareStore.on_end`. This assumes
  one Uvicorn process, as the CLI and the systemd unit run it; a multi-worker deployment would
  need cross-process invalidation. The streams' periodic re-check is the fallback.

The store is a 0600 SQLite file (``AGENT_SESSIONS_SHARES_DB``), same discipline as ``missions.db``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from . import auth, template_secrets

log = logging.getLogger("agent_sessions.shares")

MODES = ("view", "send")
KINDS = ("api", "terminal")
DIGEST_LABEL = "public-share"
# A share id is public (it names the row in operator lists and in the share cookie); the token
# is the secret. Both are url-safe base64 from `secrets`.
_SHARE_ID_BYTES = 12
_TOKEN_BYTES = 32
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_TITLE = 200
MAX_PASSWORD = 256
# The longest a link may be set to live. Expiry itself is optional ("never"); this only bounds a
# picked date so a typo cannot mint a link for the year 9999 that sorts oddly in the list.
MAX_EXPIRY_S = 10 * 365 * 86400
# Revoked and expired rows are kept this long (for the operator's "why did my link stop"),
# then swept.
_SWEEP_GRACE_S = 7 * 86400
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  share_id        TEXT PRIMARY KEY,
  token_digest    TEXT NOT NULL UNIQUE,
  session_key     TEXT NOT NULL,
  kind            TEXT NOT NULL,
  mode            TEXT NOT NULL,
  title           TEXT NOT NULL DEFAULT '',
  password_hash   TEXT,
  expires_at      REAL,
  created_at      REAL NOT NULL,
  revoked_at      REAL,
  revoked_reason  TEXT,
  open_count      INTEGER NOT NULL DEFAULT 0,
  last_opened_at  REAL,
  -- The template-secrets key id the digest was made under. A row whose key is no longer the
  -- current one is dead: rotating the key ends every link, cookies included.
  key_id          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS shares_by_session ON shares(session_key);
-- Turns a visitor sent through a `send` link (#1404 P2): the operation id the SERVER minted for
-- them, the visitor's self-chosen name, and the client's retry key (so a retried send reuses the
-- same operation instead of sending twice). Kept after the link ends: attribution is history.
CREATE TABLE IF NOT EXISTS share_turns (
  operation_id  TEXT PRIMARY KEY,
  session_key   TEXT NOT NULL,
  share_id      TEXT NOT NULL,
  client_key    TEXT NOT NULL,
  author        TEXT NOT NULL,
  created_at    REAL NOT NULL,
  UNIQUE (share_id, client_key)
);
CREATE INDEX IF NOT EXISTS share_turns_by_session ON share_turns(session_key);
-- Images a visitor uploaded through a link: the only uploads that visitor may attach.
CREATE TABLE IF NOT EXISTS share_uploads (
  share_id      TEXT NOT NULL,
  stored        TEXT NOT NULL,
  created_at    REAL NOT NULL,
  PRIMARY KEY (share_id, stored)
);
"""


class ShareError(Exception):
    """A refused create — the message is safe to show the operator."""


class SharesUnavailable(Exception):
    """The share store could not end a session's links. An archive must not proceed past this."""


@dataclass(frozen=True)
class Share:
    share_id: str
    session_key: str
    kind: str  # "api" | "terminal"
    mode: str  # "view" | "send"
    title: str
    has_password: bool
    expires_at: float | None
    created_at: float
    revoked_at: float | None
    revoked_reason: str | None
    open_count: int
    last_opened_at: float | None
    key_id: str = ""

    def public(self) -> dict:
        """The operator-facing row. Never carries the token, its digest or the password hash."""
        out = asdict(self)
        out.pop("key_id", None)
        return out


def db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_SHARES_DB",
            str(Path.home() / ".config" / "agent-sessions" / "shares.db"),
        )
    )


def _touch_0600(p: Path) -> None:
    """Create the file 0600 before sqlite3 can create it under the process umask (the
    ``missions.db`` rule): a password hash and a token digest are not for other local users."""
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def marker_path(db: Path) -> Path:
    """The store's "was ever initialized" marker, written beside the database the first time it
    is opened. It tells "no link was ever made" (no database, no marker) from "the database went
    missing" (marker, no database) — the second must refuse an archive, or a restored database
    would bring back links that were never revoked (Hermes on #1414)."""
    return db.with_name(db.name + ".initialized")


def home_marker_path(db: Path) -> Path:
    """The SECOND initialized-marker, kept outside the store's own directory (Hermes on #1414:
    moving the directory took the co-located marker with it). It lives in the app home
    (``AGENT_SESSIONS_HOME``, where the env file is), one file per store path, named by the
    path's hash — so pointing ``AGENT_SESSIONS_SHARES_DB`` somewhere new starts a fresh store
    instead of tripping an old store's marker."""
    home = Path(os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions")
    # `abspath`, not `resolve`: the name must be the same whether or not the database exists.
    tag = hashlib.sha256(os.path.abspath(db).encode()).hexdigest()
    return home.expanduser() / "shares-initialized" / tag[:32]


def _store_was_initialized(db: Path) -> bool:
    return marker_path(db).exists() or home_marker_path(db).exists()


def digest(token: str) -> str:
    return template_secrets.keyed_digest(DIGEST_LABEL, token)


class ShareStore:
    """The share rows plus the in-process pieces that hang off them (fences, end listeners).

    ``clock`` is injectable so tests drive expiry without sleeping. ``path=None`` reads
    ``AGENT_SESSIONS_SHARES_DB`` per connection, so the suite's env pin is enough."""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time):
        self._path = path
        self._clock = clock
        self._fences: dict[str, threading.Lock] = {}
        self._session_locks: dict[str, threading.Lock] = {}
        self._fences_lock = threading.Lock()
        self._listeners: list[Callable[[str], None]] = []
        self._listeners_lock = threading.Lock()
        self._schema_ready: set[str] = set()

    # -- connection --------------------------------------------------------------------

    @contextlib.contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        p = self._path or db_path()
        if not p.exists() and _store_was_initialized(p):
            # The store existed and went missing. Recreating it empty would let an archive
            # "revoke" nothing and a restored file bring the links back (#1414 independent
            # review): EVERY open refuses, so routes and archive fail closed alike.
            raise SharesUnavailable("the public-link store is missing")
        # Both markers first: a crash part-way never leaves a database without them.
        _touch_0600(home_marker_path(p))
        _touch_0600(marker_path(p))
        _touch_0600(p)
        con = sqlite3.connect(str(p), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            # A revoked row's password hash must not linger in a freed page (set explicitly:
            # the default is a build option — see missions._connect).
            con.execute("PRAGMA secure_delete=ON")
            if str(p) not in self._schema_ready:
                con.executescript(_SCHEMA)
                self._schema_ready.add(str(p))
            yield con
        finally:
            con.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> Share:
        return Share(
            share_id=r["share_id"],
            session_key=r["session_key"],
            kind=r["kind"],
            mode=r["mode"],
            title=r["title"] or "",
            has_password=bool(r["password_hash"]),
            expires_at=r["expires_at"],
            created_at=r["created_at"],
            revoked_at=r["revoked_at"],
            revoked_reason=r["revoked_reason"],
            open_count=int(r["open_count"] or 0),
            last_opened_at=r["last_opened_at"],
            key_id=r["key_id"] or "",
        )

    # -- create / list -----------------------------------------------------------------

    def create(
        self,
        *,
        session_key: str,
        kind: str,
        mode: str,
        title: str = "",
        password: str | None = None,
        password_hash: str | None = None,
        expires_at: float | None = None,
    ) -> tuple[Share, str]:
        """Mint a link. Returns ``(share, token)`` — the ONLY time the token exists in the clear.

        ``password_hash`` (from :func:`hash_share_password`) lets a caller do the slow PBKDF2
        before it takes the session lock; ``password`` hashes here. A terminal link is view-only
        whatever was asked. This does NOT check that the session exists or is live — the route
        does that under :meth:`session_lock`, which an archive holds too."""
        if kind not in KINDS:
            raise ShareError("this session can't be shared publicly")
        if mode not in MODES:
            raise ShareError("access must be 'view' or 'send'")
        if kind == "terminal" and mode != "view":
            raise ShareError("terminal sessions can only be shared view only")
        now = self._clock()
        if expires_at is not None:
            if expires_at <= now:
                raise ShareError("the expiry is in the past")
            if expires_at > now + MAX_EXPIRY_S:
                raise ShareError("the expiry is too far away")
        pw_hash = password_hash if password_hash is not None else hash_share_password(password)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        share_id = secrets.token_urlsafe(_SHARE_ID_BYTES)
        title = (title or "").strip()[:MAX_TITLE]
        token_digest = digest(token)  # creates the key file on first use, so read the id after
        kid = template_secrets.current_kid()
        with self._con() as con:
            con.execute(
                "INSERT INTO shares (share_id, token_digest, session_key, kind, mode, title,"
                " password_hash, expires_at, created_at, key_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    share_id,
                    token_digest,
                    session_key,
                    kind,
                    mode,
                    title,
                    pw_hash,
                    expires_at,
                    now,
                    kid,
                ),
            )
        self.sweep()
        share = self.get(share_id)
        assert share is not None
        return share, token

    def get(self, share_id: str) -> Share | None:
        if not SHARE_ID_RE.match(share_id or ""):
            return None
        with self._con() as con:
            r = con.execute("SELECT * FROM shares WHERE share_id = ?", (share_id,)).fetchone()
        return self._row(r) if r else None

    def _live(self, share: Share | None) -> bool:
        if share is None or share.revoked_at is not None:
            return False
        if share.expires_at is not None and share.expires_at <= self._clock():
            return False
        # Made under a key that is no longer the current one: the operator replaced the
        # template-secrets key, which ends every link — already-unlocked visitors included.
        current = template_secrets.current_kid()
        if current in ("bad", template_secrets.NO_KEY_KID):
            return False  # no usable key now: nothing can be proven live
        return bool(share.key_id) and share.key_id == current

    def active(self, share_id: str) -> Share | None:
        """The share if it may be used RIGHT NOW (not revoked, not expired), else None. Every
        public request goes through this — a valid cookie for a dead share grants nothing."""
        share = self.get(share_id)
        return share if self._live(share) else None

    def list_active(self, session_key: str | None = None) -> list[Share]:
        with self._con() as con:
            if session_key is None:
                rows = con.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM shares WHERE session_key = ? ORDER BY created_at DESC",
                    (session_key,),
                ).fetchall()
        return [s for s in (self._row(r) for r in rows) if self._live(s)]

    # -- unlock ------------------------------------------------------------------------

    def resolve_token(self, token: str) -> tuple[Share, str | None] | None:
        """``(share, password_hash)`` for a live link's token, else None. Unknown, revoked and
        expired all answer the same None, so the public route cannot tell them apart either."""
        if not TOKEN_RE.match(token or ""):
            return None
        try:
            d = digest(token)
        except template_secrets.SecretKeyUnavailable:
            log.warning("public share: template-secrets key unusable; refusing every link")
            return None
        with self._con() as con:
            r = con.execute("SELECT * FROM shares WHERE token_digest = ?", (d,)).fetchone()
        if r is None:
            return None
        share = self._row(r)
        if not self._live(share):
            return None
        return share, r["password_hash"]

    def note_open(self, share_id: str) -> None:
        with self._con() as con:
            con.execute(
                "UPDATE shares SET open_count = open_count + 1, last_opened_at = ?"
                " WHERE share_id = ?",
                (self._clock(), share_id),
            )

    # -- revoke ------------------------------------------------------------------------

    def session_lock(self, session_key: str) -> threading.Lock:
        """The per-SESSION lock that link creation and :func:`archiving` both take, so a link can
        never be minted between an archive's revoke and its move."""
        with self._fences_lock:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = self._session_locks[session_key] = threading.Lock()
            return lock

    def fence(self, share_id: str) -> threading.Lock:
        """The per-share lock that revoke takes. A send path holds it across its re-check AND its
        effect, so a revoke cannot land between the two.

        **Take it only in a worker thread, never on the event loop.** A send holds it in a worker
        thread while its facade coroutine runs on the loop (``routes/public_api._fenced``), so
        anything on the loop that blocked on this lock would deadlock both. :meth:`revoke` takes
        it, so every revoke runs off the loop too (the routes and :func:`archiving` use
        ``asyncio.to_thread``)."""
        with self._fences_lock:
            lock = self._fences.get(share_id)
            if lock is None:
                lock = self._fences[share_id] = threading.Lock()
            return lock

    def revoke(self, share_id: str, *, reason: str = "revoked") -> bool:
        """End one link. True when it was live. Takes the share's fence, so it waits for an
        in-flight send to finish and blocks the next one."""
        with self.fence(share_id):
            with self._con() as con:
                cur = con.execute(
                    "UPDATE shares SET revoked_at = ?, revoked_reason = ?"
                    " WHERE share_id = ? AND revoked_at IS NULL",
                    (self._clock(), reason, share_id),
                )
                ended = cur.rowcount > 0
        if ended:
            self._notify(share_id)
        return ended

    def revoke_all(self, *, reason: str = "revoked") -> int:
        return sum(1 for s in self.list_active() if self.revoke(s.share_id, reason=reason))

    def revoke_for_session(self, session_key: str, *, reason: str) -> int:
        """End every live link to ``session_key``.

        No database AND no initialized-marker means no link was ever made: answer 0 without
        creating either, so archiving doesn't start depending on a store the operator never used
        (#1414 independent review). A marker without its database means the store went missing:
        raise, so the archive refuses rather than completing with links nobody revoked (Hermes on
        #1414)."""
        p = self._path or db_path()
        if not p.exists() and not _store_was_initialized(p):
            return 0  # (a marker without its database raises in `_con` below)
        with self._con() as con:
            ids = [
                r["share_id"]
                for r in con.execute(
                    "SELECT share_id FROM shares WHERE session_key = ? AND revoked_at IS NULL",
                    (session_key,),
                ).fetchall()
            ]
        return sum(1 for sid in ids if self.revoke(sid, reason=reason))

    # -- visitor turns and uploads (P2) ------------------------------------------------

    def record_turn(
        self, *, share_id: str, session_key: str, client_key: str, author: str
    ) -> tuple[str, bool]:
        """The operation id for a visitor's send, minted HERE (never chosen by the visitor, so
        one can't claim an operator turn by reusing its id). ``client_key`` is the visitor's retry
        key: the same key on the same link returns the same operation id. Returns
        ``(operation_id, fresh)``. A retry under a different name keeps the first name."""
        with self._con() as con:
            row = con.execute(
                "SELECT operation_id, session_key FROM share_turns"
                " WHERE share_id = ? AND client_key = ?",
                (share_id, client_key),
            ).fetchone()
            if row is not None:
                if row["session_key"] != session_key:
                    raise ShareError("that retry key belongs to another session")
                return row["operation_id"], False
            op = str(uuid.uuid4())
            con.execute(
                "INSERT INTO share_turns (operation_id, session_key, share_id, client_key, author,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (op, session_key, share_id, client_key, author, self._clock()),
            )
            return op, True

    def authors(self, session_key: str) -> dict[str, dict]:
        """``{operation_id: {"name", "share_id"}}`` for every visitor turn of a session.

        A store that was never initialized has no visitor turns (``{}``, nothing created). A store
        that was initialized and went missing raises :class:`SharesUnavailable`: "no visitor turns"
        is a claim this can't make then, and the caller must show attribution as UNKNOWN rather
        than label every turn the operator's (Hermes on #1423)."""
        p = self._path or db_path()
        if not p.exists() and not _store_was_initialized(p):
            return {}
        with self._con() as con:
            rows = con.execute(
                "SELECT operation_id, author, share_id FROM share_turns WHERE session_key = ?",
                (session_key,),
            ).fetchall()
        return {r["operation_id"]: {"name": r["author"], "share_id": r["share_id"]} for r in rows}

    def reserve_upload(self, share_id: str, cap: int) -> str | None:
        """Take one of the link's ``cap`` upload slots BEFORE any byte is accepted; return the
        slot id, or None when the link has used them all. The count and the claim are one
        ``BEGIN IMMEDIATE`` transaction, so concurrent uploads serialize on the database and can't
        all see the old count (Hermes on #1423). The slot holds a placeholder name that can never
        match a stored upload, until :meth:`fill_upload` or :meth:`release_upload`."""
        slot = f"pending:{uuid.uuid4()}"
        with self._con() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                # A slot a crashed upload never filled or released would count forever: an
                # upload finishes in seconds, so a pending slot this old is abandoned.
                con.execute(
                    "DELETE FROM share_uploads WHERE stored LIKE 'pending:%' AND created_at < ?",
                    (self._clock() - PENDING_UPLOAD_TTL_S,),
                )
                (n,) = con.execute(
                    "SELECT COUNT(*) FROM share_uploads WHERE share_id = ?", (share_id,)
                ).fetchone()
                if n >= cap:
                    con.execute("ROLLBACK")
                    return None
                con.execute(
                    "INSERT INTO share_uploads (share_id, stored, created_at) VALUES (?,?,?)",
                    (share_id, slot, self._clock()),
                )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return slot

    def fill_upload(self, share_id: str, slot: str, stored: str) -> bool:
        """Record the stored name in the reserved slot. False when the slot is gone — the caller
        must then remove the file it wrote, or it would sit on disk outside the cap."""
        with self._con() as con:
            cur = con.execute(
                "UPDATE share_uploads SET stored = ? WHERE share_id = ? AND stored = ?",
                (stored, share_id, slot),
            )
            return cur.rowcount > 0

    def count_upload_rows(self, share_id: str) -> int:
        with self._con() as con:
            (n,) = con.execute(
                "SELECT COUNT(*) FROM share_uploads WHERE share_id = ?", (share_id,)
            ).fetchone()
        return int(n)

    def release_upload(self, share_id: str, slot: str) -> None:
        """Hand an unused slot back (the write failed or was refused)."""
        with self._con() as con:
            con.execute(
                "DELETE FROM share_uploads WHERE share_id = ? AND stored = ?", (share_id, slot)
            )

    def note_upload(self, share_id: str, stored: str) -> None:
        with self._con() as con:
            con.execute(
                "INSERT OR IGNORE INTO share_uploads (share_id, stored, created_at) VALUES (?,?,?)",
                (share_id, stored, self._clock()),
            )

    def owns_upload(self, share_id: str, stored: str) -> bool:
        with self._con() as con:
            row = con.execute(
                "SELECT 1 FROM share_uploads WHERE share_id = ? AND stored = ?",
                (share_id, stored),
            ).fetchone()
        return row is not None

    # -- end listeners (open streams) --------------------------------------------------

    def on_end(self, fn: Callable[[str], None]) -> Callable[[], None]:
        """Register ``fn(share_id)`` to run when a link is revoked. Returns the unsubscribe.
        In-process only (see the module docstring)."""
        with self._listeners_lock:
            self._listeners.append(fn)

        def _off() -> None:
            with self._listeners_lock, contextlib.suppress(ValueError):
                self._listeners.remove(fn)

        return _off

    def _notify(self, share_id: str) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(share_id)
            except Exception:  # a listener's failure must not undo or block the revoke
                log.exception("public share: end listener failed")

    # -- sweep -------------------------------------------------------------------------

    def sweep(self) -> int:
        """Delete rows that have been revoked or expired for longer than the grace period."""
        cutoff = self._clock() - _SWEEP_GRACE_S
        with self._con() as con:
            cur = con.execute(
                "DELETE FROM shares WHERE (revoked_at IS NOT NULL AND revoked_at < ?)"
                " OR (expires_at IS NOT NULL AND expires_at < ?)",
                (cutoff, cutoff),
            )
            return cur.rowcount


# The process's store. Routes and the archive hooks share it, so a revoke reaches the fences and
# listeners the public routes registered.
_STORE: ShareStore | None = None
_STORE_LOCK = threading.Lock()


def store() -> ShareStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ShareStore()
        return _STORE


def reset_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def hash_share_password(password: str | None) -> str | None:
    """Validate and hash a link password (``None``/empty = no password). Slow (PBKDF2), so the
    create route runs it before taking the session lock."""
    if password is None or password == "":
        return None
    if len(password) > MAX_PASSWORD:
        raise ShareError("the password is too long")
    return auth.hash_password(password)


# How long an archive (or a create) waits for the session's lock before giving up. Holders keep
# it for a provider scan or a move, so anything longer is a wedge, not a queue.
SESSION_LOCK_TIMEOUT_S = 60


@contextlib.asynccontextmanager
async def archiving(session_key: str):
    """Wrap a provider's archive of ``session_key``: take the session's lock (off the event loop),
    end every link to it, yield for the move, release.

    Fail closed: when the links cannot be ended — or the lock cannot be had within
    ``SESSION_LOCK_TIMEOUT_S`` — this raises :class:`SharesUnavailable` and the body never runs,
    so the session is NOT archived while a link to it stays live. Holding the lock across the move
    is what stops a concurrent create from minting a link in between; the body must not ``await``
    (it runs on the loop with a thread lock held).

    Cancellation-safe: if this coroutine is cancelled while the worker thread is still waiting,
    the thread's eventual acquisition is released by a done-callback, so a cancelled archive can
    never strand the lock (#1414 independent review)."""
    st = store()
    lock = st.session_lock(session_key)
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, lock.acquire, True, SESSION_LOCK_TIMEOUT_S)
    try:
        got = await asyncio.shield(fut)
    except asyncio.CancelledError:
        fut.add_done_callback(lambda f: f.cancelled() or not f.result() or lock.release())
        raise
    if not got:
        raise SharesUnavailable("this session is busy — try again")
    try:
        try:
            # Off the loop: `revoke` takes each link's fence, which a send holds in a worker
            # thread WHILE its facade call runs on the loop — taking it here, on the loop, would
            # deadlock the two (#1404 P2).
            n = await asyncio.to_thread(st.revoke_for_session, session_key, reason="archived")
        except Exception as e:
            log.error("public share: could not end links for a session being archived")
            raise SharesUnavailable("could not end this session's public links") from e
        if n:
            log.info("public share: ended %d link(s) for an archived session", n)
        yield
    finally:
        lock.release()


# A reserved-but-unfilled upload slot older than this is abandoned (a crash between reserve and
# fill/release) and stops counting against the link's cap. It must stay far above the longest an
# upload request may run (`routes/public_api.UPLOAD_READ_TIMEOUT_S`): expiring a LIVE request's
# slot would let its file land outside the cap (Hermes on #1423). A test pins the margin.
PENDING_UPLOAD_TTL_S = 600

# ---- visitor names --------------------------------------------------------------------------

# A visitor on a `send` link types a name before they can send (#1404): their turns are
# attributed to it. It is SELF-CHOSEN — a label, not an identity — and it is rendered as text, so
# the rules only keep it short, printable and on one line.
MAX_VISITOR_NAME = 40


def clean_visitor_name(raw: object) -> str | None:
    """The name to store, or None when ``raw`` is not acceptable: not a string, empty after
    trimming, longer than ``MAX_VISITOR_NAME`` characters, or carrying a control, format or other
    non-printing character (a bidi override or zero-width joiner could disguise it as someone
    else's). Inner whitespace collapses to single spaces."""
    if not isinstance(raw, str):
        return None
    name = " ".join(unicodedata.normalize("NFC", raw).split())
    if not name or len(name) > MAX_VISITOR_NAME:
        return None
    if any(unicodedata.category(ch)[0] in ("C", "Z") and ch != " " for ch in name):
        return None
    return name


# ---- unlock throttle ----------------------------------------------------------------------

# Wrong-password budget: per (share, client IP) — 5 failures, then a 15-minute pause for that
# client; per share overall — 20 failures, then the share pauses for everyone for 15 minutes. The
# overall cap is what stops a botnet from spreading guesses across addresses.
PER_CLIENT_FAILS = 5
PER_SHARE_FAILS = 20
PAUSE_S = 15 * 60


class UnlockThrottle:
    """Attempts are RESERVED before the password is checked, so concurrent guesses cannot all pass
    the budget check while the slow PBKDF2 runs. A correct password hands its reservation back."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._client: dict[tuple[str, str], list[float]] = {}
        self._share: dict[str, list[float]] = {}

    def _window(self, ts: list[float], now: float) -> list[float]:
        return [t for t in ts if now - t < PAUSE_S]

    def _prune(self, now: float) -> None:
        for d in (self._client, self._share):
            for k in [k for k, ts in d.items() if not self._window(ts, now)]:
                d.pop(k, None)

    def _paused(self, share_id: str, ip: str, now: float) -> bool:
        c = self._window(self._client.get((share_id, ip), []), now)
        s = self._window(self._share.get(share_id, []), now)
        return len(c) >= PER_CLIENT_FAILS or len(s) >= PER_SHARE_FAILS

    def paused(self, share_id: str, ip: str) -> bool:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return self._paused(share_id, ip, now)

    def reserve(self, share_id: str, ip: str) -> float | None:
        """Take one attempt from both budgets, atomically with the check. Returns the reservation
        (hand it to :meth:`release` or :meth:`succeed`), or None when paused."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            if self._paused(share_id, ip, now):
                return None
            self._client[(share_id, ip)] = self._window(
                self._client.get((share_id, ip), []), now
            ) + [now]
            self._share[share_id] = self._window(self._share.get(share_id, []), now) + [now]
            return now

    def _drop(self, share_id: str, ip: str, stamp: float) -> None:
        for key, d in (((share_id, ip), self._client), (share_id, self._share)):
            ts = d.get(key)
            if ts and stamp in ts:
                ts.remove(stamp)

    def release(self, share_id: str, ip: str, stamp: float) -> None:
        """Give a reservation back without counting it (no password was offered)."""
        with self._lock:
            self._drop(share_id, ip, stamp)

    def succeed(self, share_id: str, ip: str, stamp: float) -> None:
        """A correct password: its reservation is returned and the client's count clears."""
        with self._lock:
            self._drop(share_id, ip, stamp)
            self._client.pop((share_id, ip), None)

    def tries_left(self, share_id: str, ip: str) -> int:
        now = self._clock()
        with self._lock:
            c = self._window(self._client.get((share_id, ip), []), now)
            return max(0, PER_CLIENT_FAILS - len(c))


# ---- log redaction ------------------------------------------------------------------------

# `/p/<token>` in any logged text becomes `/p/<redacted>`. The token is the whole credential for
# a password-less link, and access logs, error reports and proxy logs outlive any revoke.
_P_PATH = re.compile(r"/p/[A-Za-z0-9_-]+")
REDACTED_PATH = "/p/<redacted>"


def redact(text: str) -> str:
    return _P_PATH.sub(REDACTED_PATH, text)


_P_UNREDACTED = re.compile(r"/p/(?!<redacted>)[A-Za-z0-9_-]+")


class RedactShareTokens(logging.Filter):
    """Masks share tokens in a record WITHOUT changing its shape. uvicorn's ``AccessFormatter``
    unpacks ``record.args`` into five values, so the access line keeps its tuple and only the
    path entry is masked (#1414 independent review: nulling ``args`` turned every viewer hit
    into a logging traceback). Only when the path is split across the format string and its
    arguments (``"/p/%s" % token``) — which uvicorn's access line never does — is the message
    formatted first and stored whole."""

    @staticmethod
    def _mask(v):
        return redact(v) if isinstance(v, str) and "/p/" in v else v

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "/p/" in record.msg:
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._mask(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._mask(v) for k, v in record.args.items()}
        try:
            text = record.getMessage()
        except Exception:  # a malformed record is not ours to break; leave it to the handler
            return True
        if _P_UNREDACTED.search(text):
            record.msg = redact(text)
            record.args = ()
        return True


_FILTER = RedactShareTokens()
# The loggers that write a request path: uvicorn's access line and uvicorn's error logger (which
# reports invalid requests and protocol errors). The app's own code never logs a request path.
_REDACTED_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn")


def install_log_redaction() -> None:
    """Attach the filter — idempotent. A LOGGER filter survives uvicorn's ``dictConfig`` (which
    replaces handlers, not logger filters), so this can run in ``create_app`` before uvicorn
    configures logging. A logger filter sees only records logged on that logger, so each name
    gets its own; the root logger's handlers get it too, for anything that propagates there."""
    for name in _REDACTED_LOGGERS:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
    for h in logging.getLogger().handlers:
        if _FILTER not in h.filters:
            h.addFilter(_FILTER)
