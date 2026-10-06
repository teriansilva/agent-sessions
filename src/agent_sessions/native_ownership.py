"""Permanent ownership of source histories by structured clients (#1277).

This is identity metadata, never a message/event or credential store. A creation intent precedes
the native handoff; a native id is bound once and never released by archive, disable or restart.
Domain callers validate request metadata and must never include messages or credentials.

Mutation and actual console admission callers already hold the plugin launch lock. Binding
nonblockingly acquires the plugin worker fence and relevant console writer locks before its
short ledger transaction; it never waits for those locks or performs an RPC. Discovery reads
need no launch lock. Every process sharing native stores must share the ledger, admission and
runtime/lock directories; external SSH CLIs do not honor these locks.

Initialization publishes a durable sentinel before the database. A missing/corrupt initialized
database is unavailable, never a fresh empty ledger. No release or pending-clear API exists.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .plugins import kinds, provenance, storage
from .plugins.manifest import MAX_NATIVE_ID_LEN, compile_id_pattern

SCHEMA_VERSION = 1
MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_REQUEST_BYTES = 16 * 1024
MAX_RUNTIME_ENTRIES = 50_000
_APP_KEY = re.compile(r"[a-z][a-z0-9-]{1,23}:[0-9a-f-]{36}")
_SOURCE_LAYOUTS = frozenset(value[0] for value in kinds.API_SOURCE_KINDS.values())
_UNAVAILABLE = "native history ownership is unavailable; console access was refused"


class OwnershipError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class SourceIdentity:
    layout: str
    canonical_path: str
    key: str
    id_pattern: str


@dataclass(frozen=True)
class SourceOwnership:
    bound_native_ids: frozenset[str] = frozenset()
    pending: bool = False


@dataclass(frozen=True)
class Intent:
    app_session_key: str
    operation_id: str
    source: SourceIdentity
    owner_token: str
    request: dict
    native_id: str | None
    created_at: float
    bound_at: float | None

    @property
    def state(self) -> str:
        return "pending" if self.native_id is None else "bound"


def _source_key(layout: str, path: str) -> str:
    return hashlib.sha256(json.dumps([layout, path], separators=(",", ":")).encode()).hexdigest()


def _pattern(value: str):
    # Manifests replace their final '$' with '\\Z' after validating the bounded grammar.
    raw = value[:-2] + "$" if value.endswith(r"\Z") else value
    return compile_id_pattern(raw)


def source_identity(prov) -> SourceIdentity | None:
    """Derive identity from the registered source shape, never a client path or engine alias."""
    manifest = getattr(prov, "manifest", None)
    if (
        manifest is None
        or manifest.runtime != "pty"
        or manifest.identity.kind != "agent"
        or manifest.store is None
        or manifest.store.layout not in _SOURCE_LAYOUTS
    ):
        return None
    try:
        root = prov.store_root()
        if root is None or not Path(root).is_absolute():
            raise ValueError
        path = str(Path(root).resolve(strict=False))
        if not path or "\x00" in path or len(path) > 4096:
            raise ValueError
        pattern = manifest.session_id.pattern.pattern
        _pattern(pattern)
        layout = manifest.store.layout
        return SourceIdentity(layout, path, _source_key(layout, path), pattern)
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RuntimeError,
        provenance.ProvenanceError,
    ):
        raise OwnershipError("unavailable", _UNAVAILABLE) from None


def _source(source: SourceIdentity) -> None:
    if not isinstance(source, SourceIdentity) or (
        not isinstance(source.layout, str)
        or source.layout not in _SOURCE_LAYOUTS
        or not isinstance(source.canonical_path, str)
        or not os.path.isabs(source.canonical_path)
        or os.path.normpath(source.canonical_path) != source.canonical_path
        or "\x00" in source.canonical_path
        or len(source.canonical_path) > 4096
        or source.key != _source_key(source.layout, source.canonical_path)
    ):
        raise OwnershipError("invalid", "invalid native source identity")
    try:
        _pattern(source.id_pattern)
    except (ValueError, TypeError, AttributeError):
        raise OwnershipError("invalid", "invalid native source identity") from None


def _uuid(value: object, name: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise OwnershipError("invalid", f"{name} must be a canonical UUID") from None
    return value


def _app_key(value: object) -> str:
    if not isinstance(value, str) or not _APP_KEY.fullmatch(value):
        raise OwnershipError("invalid", "invalid structured session key")
    _uuid(value.split(":", 1)[1], "session id")
    return value


def _request(value: object) -> str:
    try:
        if not isinstance(value, dict):
            raise ValueError
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > MAX_REQUEST_BYTES:
            raise ValueError

        # Require JSON values without coercing integer keys, tuples or custom subclasses.
        def keys(item, depth=0):
            if depth > 32:
                return False
            if type(item) is dict:
                return all(type(k) is str and keys(v, depth + 1) for k, v in item.items())
            if type(item) is list:
                return all(keys(v, depth + 1) for v in item)
            return item is None or type(item) in (str, int, float, bool)

        if not keys(value):
            raise ValueError
        return encoded
    except (TypeError, ValueError, RecursionError):
        raise OwnershipError(
            "invalid", "creation metadata must be bounded JSON without secrets"
        ) from None


def _paths() -> tuple[Path, Path]:
    root = storage.root()
    return root / "native-ownership.initialized.json", root / "native-ownership" / "ownership.db"


def _private(path: Path, *, missing: bool = False):
    try:
        fd, st = provenance.open_verified(str(path), canonicalize=False)
    except FileNotFoundError:
        if missing:
            return None
        raise
    try:
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
            or st.st_nlink != 1
            or st.st_size > MAX_DATABASE_BYTES
        ):
            raise ValueError("ownership file is not private and bounded")
        return st
    finally:
        os.close(fd)


def _sidecars(db: Path, *, database_exists: bool = True) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        found = _private(db.with_name(db.name + suffix), missing=True)
        if found is not None and (not database_exists or suffix != "-journal"):
            raise ValueError("unexpected ownership journal mode")


_SCHEMA = """
CREATE TABLE identity (ledger_id TEXT PRIMARY KEY NOT NULL);
CREATE TABLE ownership (
 app_session_key TEXT PRIMARY KEY NOT NULL,
 operation_id TEXT UNIQUE NOT NULL,
 source_key TEXT NOT NULL,
 source_layout TEXT NOT NULL,
 source_path TEXT NOT NULL,
 source_pattern TEXT NOT NULL,
 owner_token TEXT NOT NULL,
 request_json TEXT NOT NULL,
 native_id TEXT,
 created_at REAL NOT NULL,
 bound_at REAL,
 UNIQUE(source_key,native_id)
);
CREATE INDEX source_ownership ON ownership(source_key);
"""


def _connect(db: Path) -> sqlite3.Connection:
    before = _private(db)
    _sidecars(db)
    con = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=0.25, isolation_level=None)
    try:
        after = _private(db)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("ownership database changed while opening")
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA synchronous=FULL")
        if con.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("unsupported ownership journal mode")
        # SQLite must refuse growth inside the transaction, leaving existing ownership usable.
        # A size check after commit would instead strand an otherwise valid ledger over its cap.
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        max_pages = MAX_DATABASE_BYTES // page_size
        if max_pages < 1 or con.execute("PRAGMA page_count").fetchone()[0] > max_pages:
            raise ValueError("ownership database exceeds its capacity")
        con.execute(f"PRAGMA max_page_count={max_pages}")
        return con
    except BaseException:
        con.close()
        raise


def _initialize(marker: Path, db: Path) -> dict:
    identity = {"version": SCHEMA_VERSION, "ledger_id": str(uuid.uuid4())}
    storage.write(marker, identity)
    with storage.directory(db.parent) as dfd:
        fd = os.open(db.name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        os.close(fd)
        con = _connect(db)
        try:
            con.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    con.execute(statement)
            con.execute("INSERT INTO identity VALUES (?)", (identity["ledger_id"],))
            con.execute("PRAGMA user_version=1")
            con.execute("COMMIT")
            os.fsync(dfd)
        finally:
            con.close()
    return identity


# The last database generation that passed `quick_check`. The check scans the whole file, so
# repeating it on every read made each session-list scan cost O(ledger) (#1277 review).
_VERIFIED: tuple | None = None


@contextlib.contextmanager
def _ledger(*, initialize: bool = False):
    """No launch lock: callers must never acquire one while holding this short transaction.

    Initialization and creation intents serialize on the ledger's own lock. Reads (and `bind`,
    whose caller already holds launch admission, which also orders every `reserve`) use a plain
    SQLite read transaction instead: an exclusive lock on every discovery read made one slow
    reader hide every source session behind a lock timeout.
    """
    try:
        if initialize:
            with storage.locked("native-ownership"), _open(initialize=True) as con:
                yield con
            return
        try:
            with _open(initialize=False) as con:
                yield con
        except _Initializing:
            # Initialization writes the marker before the database. A reader in that one-time
            # window waits for the initializer instead of reporting initialized state missing.
            with storage.locked("native-ownership"), _open(initialize=False, locked=True) as con:
                yield con
    except OwnershipError:
        raise
    except (OSError, ValueError, sqlite3.Error, provenance.ProvenanceError, storage.StateError):
        raise OwnershipError("unavailable", _UNAVAILABLE) from None


class _Initializing(Exception):
    pass


@contextlib.contextmanager
def _open(*, initialize: bool, locked: bool = False):
    global _VERIFIED
    marker, db = _paths()
    _private(marker, missing=True)
    identity = storage.read(marker)
    present = _private(db, missing=True)
    _sidecars(db, database_exists=present is not None)
    if identity is None and present is None:
        if not initialize:
            yield None
            return
        identity = _initialize(marker, db)
    elif identity is not None and present is None and not (initialize or locked):
        raise _Initializing
    elif identity is None or present is None:
        raise ValueError("initialized ownership state is missing")
    if set(identity) != {"version", "ledger_id"} or (
        type(identity["version"]) is not int or identity["version"] != SCHEMA_VERSION
    ):
        raise ValueError("ownership initialization marker is invalid")
    try:
        _uuid(identity["ledger_id"], "ledger id")
    except OwnershipError:
        raise ValueError("ownership initialization marker is invalid") from None
    con = _connect(db)
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("unsupported ownership schema")
        con.execute("BEGIN IMMEDIATE" if initialize else "BEGIN")
        try:
            # Inside the transaction, so the generation checked is the one this caller reads.
            con.execute("SELECT COUNT(*) FROM identity").fetchone()
            st = os.stat(db)
            generation = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, identity["ledger_id"])
            if generation != _VERIFIED:
                if con.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                    raise ValueError("ownership integrity check failed")
            ids = con.execute("SELECT ledger_id FROM identity LIMIT 2").fetchall()
            if len(ids) != 1 or ids[0][0] != identity["ledger_id"]:
                raise ValueError("ownership database does not match its marker")
            _VERIFIED = generation
            yield con
            _private(db)
            _sidecars(db)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def _intent(row) -> Intent:
    try:
        source = SourceIdentity(
            row["source_layout"], row["source_path"], row["source_key"], row["source_pattern"]
        )
        _source(source)
        _app_key(row["app_session_key"])
        _uuid(row["operation_id"], "operation id")
        _uuid(row["owner_token"], "owner token")
        request = json.loads(row["request_json"])
        if _request(request) != row["request_json"]:
            raise ValueError
        native = row["native_id"]
        if native is not None:
            _native(source, native)
        if (native is None) != (row["bound_at"] is None):
            raise ValueError
        created_at, bound_at = row["created_at"], row["bound_at"]
        if not isinstance(created_at, int | float) or not math.isfinite(created_at):
            raise ValueError
        if bound_at is not None and (
            not isinstance(bound_at, int | float) or not math.isfinite(bound_at)
        ):
            raise ValueError
        return Intent(
            row["app_session_key"],
            row["operation_id"],
            source,
            row["owner_token"],
            request,
            native,
            float(created_at),
            bound_at,
        )
    except (ValueError, TypeError, KeyError, OwnershipError):
        raise OwnershipError("unavailable", _UNAVAILABLE) from None


def _native(source: SourceIdentity, native: object) -> str:
    if (
        not isinstance(native, str)
        or not 0 < len(native) <= MAX_NATIVE_ID_LEN
        or (native.startswith("-") or _pattern(source.id_pattern).fullmatch(native) is None)
    ):
        raise OwnershipError("invalid", "native history id does not match its captured source")
    return native


def reserve(
    app_session_key: str,
    source: SourceIdentity,
    *,
    operation_id: str,
    request: dict,
    owner_token: str,
) -> Intent:
    """Record intent before native creation under caller-held launch admission; repeats recover."""
    _app_key(app_session_key)
    _source(source)
    _uuid(operation_id, "operation id")
    _uuid(owner_token, "owner token")
    encoded = _request(request)
    with _ledger(initialize=True) as con:
        row = con.execute(
            "SELECT * FROM ownership WHERE app_session_key=? OR operation_id=?",
            (app_session_key, operation_id),
        ).fetchone()
        if row is not None:
            previous = _intent(row)
            if (
                previous.app_session_key,
                previous.operation_id,
                previous.source,
                previous.owner_token,
                row["request_json"],
            ) != (app_session_key, operation_id, source, owner_token, encoded):
                raise OwnershipError(
                    "conflict", "creation identity was already used for another request"
                )
            return previous
        if con.execute("SELECT COUNT(*) FROM ownership").fetchone()[0] >= MAX_RECORDS:
            raise OwnershipError("unavailable", "native ownership capacity was reached")
        con.execute(
            "INSERT INTO ownership VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL)",
            (
                app_session_key,
                operation_id,
                source.key,
                source.layout,
                source.canonical_path,
                source.id_pattern,
                owner_token,
                encoded,
                time.time(),
            ),
        )
        return _intent(
            con.execute(
                "SELECT * FROM ownership WHERE app_session_key=?", (app_session_key,)
            ).fetchone()
        )


def _binding_request(current, operation_id: str, owner_token: str, native_id: str) -> Intent:
    if current is None:
        raise OwnershipError("conflict", "native creation has no recorded intent")
    if current.operation_id != operation_id or current.owner_token != owner_token:
        raise OwnershipError("conflict", "native binding requires its original creation owner")
    _native(current.source, native_id)
    if current.native_id is not None and current.native_id != native_id:
        raise OwnershipError("conflict", "native ownership is already bound to another history")
    return current


@contextlib.contextmanager
def _binding_worker_guard():
    """Try only: an existing candidate owns this fence through process-tree cleanup.

    Candidate operations normally acquire worker before launch. Binding already owns launch,
    so waiting here would reverse that order; refusing immediately cannot form that deadlock.
    The native caller must not already hold worker itself. Retain this fence through commit,
    making later candidate process admission observe the permanent native ownership.
    """
    guard = storage.locked("worker", wait=0)
    try:
        guard.__enter__()
    except storage.StateError:
        raise OwnershipError(
            "busy", "native binding refused: plugin worker admission is busy or unavailable"
        ) from None
    except (OSError, ValueError, provenance.ProvenanceError):
        raise OwnershipError("unavailable", _UNAVAILABLE) from None
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


@contextlib.contextmanager
def _console_binding_guard(source: SourceIdentity, native_id: str):
    """Under launch admission, refuse existing or unidentified source console writers.

    A new console master can hold a ``new-*`` writer lock before its socket or first transcript
    appears. Inspect lock names AND sockets, independent of transcript scans. Only a durable
    same-source alias to another native history can exempt such a master. Every matching provider
    alias counts, including retiring providers. No probe result other than DEAD admits binding.
    """
    from . import metadata, ptybridge, sessionlock
    from .engines import base, registry

    try:
        with registry.snapshot_scope(fresh=True, require_current=True) as roster:
            if roster.retirement_problems or roster.problems:
                raise ValueError("console roster is not completely known")
            providers = {
                prov.manifest.id: prov
                for prov in (*roster.providers, *roster.retiring.values())
                if (identity := source_identity(prov)) is not None and identity.key == source.key
            }
        if not providers:
            raise ValueError("native source is no longer available")
        # Attribution never trusts the alias index to be complete (Hermes on #1277): it can be
        # absent, empty, nulled or recreated without an entry. The frame instead rests on an
        # invariant enforced at publication: only a `new-*` placeholder runtime is ever aliased,
        # so a concrete runtime id always writes its OWN history. Every placeholder writer is
        # inventoried below and refuses binding unless a readable alias proves it belongs to
        # another history; a lost alias therefore fails closed.
        _, aliases, _ = metadata.load_checked()
        keys = {f"{engine}:{native_id}" for engine in providers}
        unresolved = set()
        # Admission excludes publication of new aliases while this snapshot is used.
        for directory, suffix, separator in (
            (sessionlock.lock_dir(), ".lock", "_"),
            (ptybridge.runtime_dir(), ".sock", "-"),
        ):
            for index, path in enumerate(directory.iterdir()):
                if index >= MAX_RUNTIME_ENTRIES:
                    raise ValueError("console runtime inventory exceeds its bound")
                for engine in providers:
                    prefix = engine + separator
                    if not path.name.startswith(prefix) or not path.name.endswith(suffix):
                        continue
                    physical_native = path.name[len(prefix) : -len(suffix)]
                    if base._NEW_PLACEHOLDER_RE.fullmatch(physical_native) is None:
                        continue  # a concrete runtime writes its own history (see above)
                    key = f"{engine}:{physical_native}"
                    logical_engine, _, logical_native = aliases.get(key, "").partition(":")
                    if logical_engine in providers and providers[
                        logical_engine
                    ].manifest.session_id.accepts(logical_native):
                        if logical_native != native_id:
                            continue
                    else:
                        unresolved.add(key)
                    keys.add(key)
        # Include each physical alias, not merely the first alias engines.physical_key returns.
        for physical, logical in aliases.items():
            engine, sep, physical_native = physical.partition(":")
            logical_engine, _, logical_native = logical.partition(":")
            if logical_engine not in providers or logical_native != native_id:
                continue
            if (
                engine not in providers
                or not sep
                or not base._NEW_PLACEHOLDER_RE.fullmatch(physical_native)
            ):
                # A concrete runtime aliased to another history breaks the attribution frame.
                raise ValueError("source history has an invalid console alias")
            keys.add(physical)
        with contextlib.ExitStack() as stack:
            for key in sorted(keys):
                guard = sessionlock.acquire(key)
                if guard is None:
                    reason = (
                        "unresolved console creation" if key in unresolved else "console writer"
                    )
                    raise OwnershipError(
                        "busy", f"native binding refused: {reason} is still active"
                    )
                stack.enter_context(guard)
                engine, native = key.split(":", 1)
                if ptybridge.probe_master(ptybridge.socket_path(engine, native)) != ptybridge.DEAD:
                    reason = (
                        "unresolved console creation" if key in unresolved else "console master"
                    )
                    raise OwnershipError("busy", f"native binding refused: {reason} is not stopped")
            yield
    except (OSError, ValueError, metadata.MetadataUnreadable, provenance.ProvenanceError):
        raise OwnershipError("unavailable", _UNAVAILABLE) from None


def bind(app_session_key: str, *, operation_id: str, owner_token: str, native_id: str) -> Intent:
    """Bind under caller-held launch admission, holding console guards through the commit.

    The caller must NOT already hold the plugin worker fence or a console writer lock: this
    method tries them without waiting and releases them before returning. The future native
    worker acquires its lifetime writer lock after this permanent exclusion has committed and
    before submitting any turn. Exact bound repeats only observe metadata and remain readable
    after source removal.
    """
    _app_key(app_session_key)
    _uuid(operation_id, "operation id")
    _uuid(owner_token, "owner token")
    current = _binding_request(lookup(app_session_key), operation_id, owner_token, native_id)
    if current.native_id is not None:
        return current
    with (
        _binding_worker_guard(),
        _console_binding_guard(current.source, native_id),
        _ledger() as con,
    ):
        if con is None:
            raise OwnershipError("unavailable", _UNAVAILABLE)
        row = con.execute(
            "SELECT * FROM ownership WHERE app_session_key=?", (app_session_key,)
        ).fetchone()
        checked = _binding_request(
            None if row is None else _intent(row), operation_id, owner_token, native_id
        )
        if checked.native_id is not None:
            return checked
        if checked.source != current.source:
            raise OwnershipError("conflict", "native creation source changed")
        try:
            con.execute(
                "UPDATE ownership SET native_id=?,bound_at=? WHERE app_session_key=?",
                (native_id, time.time(), app_session_key),
            )
        except sqlite3.IntegrityError:
            raise OwnershipError(
                "conflict", "that native history already belongs to another API session"
            ) from None
        return _intent(
            con.execute(
                "SELECT * FROM ownership WHERE app_session_key=?", (app_session_key,)
            ).fetchone()
        )


def lookup(app_session_key: str) -> Intent | None:
    _app_key(app_session_key)
    with _ledger() as con:
        row = (
            None
            if con is None
            else con.execute(
                "SELECT * FROM ownership WHERE app_session_key=?", (app_session_key,)
            ).fetchone()
        )
        return None if row is None else _intent(row)


def source_snapshot(source: SourceIdentity) -> SourceOwnership:
    _source(source)
    with _ledger() as con:
        if con is None:
            return SourceOwnership()
        rows = con.execute(
            "SELECT * FROM ownership WHERE source_key=? LIMIT ?", (source.key, MAX_RECORDS + 1)
        ).fetchall()
        if len(rows) > MAX_RECORDS:
            raise OwnershipError("unavailable", _UNAVAILABLE)
        intents = [_intent(row) for row in rows]
        return SourceOwnership(
            frozenset(i.native_id for i in intents if i.native_id is not None),
            any(i.native_id is None for i in intents),
        )


def check_console(prov, native_id: str | None = None, *, allow_pending: bool = False) -> None:
    """Guard under launch admission; only an existing-master attachment may allow pending."""
    source = source_identity(prov)
    if source is None:
        return
    view = source_snapshot(source)
    # A pending creation's native id is not known yet, so only a launch that could ADOPT an
    # unknown history (a new console's placeholder reconciled later, or no id at all) is
    # refused. Resuming a concrete existing history cannot be that creation: binding itself
    # refuses a history with a live console writer or socket. Refusing every resume meant one
    # stuck creation took the whole source's console offline (#1277 review).
    concrete = isinstance(native_id, str) and _pattern(source.id_pattern).match(native_id)
    if view.pending and not allow_pending and not concrete:
        raise OwnershipError(
            "pending", "this source has an unresolved API creation; console launch was refused"
        )
    if native_id in view.bound_native_ids:
        raise OwnershipError("owned", "this native history belongs to an API session")
