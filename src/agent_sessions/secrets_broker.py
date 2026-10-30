"""The secrets broker (#1786 P2): how a running agent asks for a secret it has been given access to.

An agent never reads the variables store. It asks the app over a private AF_UNIX socket, with the
capability its launch was given, and gets back a PATH to a private copy — never the value:

    agent ──(battlelab_secrets MCP | $BATTLELAB_SECRETS_HELPER)──▶ secrets-<tag>.sock ──▶ handle()

**A capability is one launch.** It is minted where a new agent process is born (a terminal LAUNCH,
an unattended dispatch, an API worker generation — the same seams as project memory), never on an
attach, and kept only as a keyed digest (`template_secrets.keyed_digest`), with the launch's
identity: session key, physical key, engine, project, and for an API generation its worker id.
The token itself is handed over as a 0600 FILE in the launch's private RAM-backed directory —
the environment and every MCP payload (argv!) carry only its path.
Agent processes survive an app restart (`KillMode=process`), so capabilities do too. One is valid
only while its launch is LIVE — the dtach master answers, or the API worker's invocation is the
active one — and that is checked on every call.

**Policy is re-read on every call**, never cached. A secret is offered to a launch only when the
launch's project — resolved again now, and still the one the capability was minted for — holds it
in its own record with agent access on, or binds the global record by its explicit ``ref`` and the
GLOBAL record has agent access on. That is the store's own binding rule: a global secret reaches
no project that does not bind it.

**One fence orders grants against revocation.** ``access_fence`` (an ``flock`` on
``secrets-access.lock`` beside the store, so it holds across app instances) is held across a
grant's policy check, its audit row and the copy it publishes. Every revoker — a committed
variables-store write (`template_vars` calls ``reconcile`` after its commit, under its own lock:
store lock → fence, never the other way), a session's runtime cleanup, the periodic pass and app
start — takes the same fence, re-judges every copy and deletes what policy no longer allows. So no
copy is published after a revoker's deletion. Nothing recalls bytes an agent already read.

**The audit is fail closed.** A grant first commits a ``pending`` row, then publishes the copy,
then settles the row ``materialized`` (or ``failed``). A settlement that cannot be written removes
the copy it just published; startup settles leftover ``pending`` rows ``interrupted`` after
removing their copies. No copy is published without a committed row; when the audit store cannot
be written the request is refused and only the app log records it. Refusals are logged while the
store is writable. The log keeps the newest ``AUDIT_PER_SECRET`` grant rows and, separately, the
newest ``AUDIT_PER_SECRET`` refusals per secret.

**Copies live on RAM-backed storage only**: ``$XDG_RUNTIME_DIR/agent-sessions/secrets/<launch>/``,
directories 0700 and files 0600; the root must be tmpfs or ramfs (``statfs``), or nothing is
materialized. This is not a sandbox: an agent runs as the operator's user and could read the key
and the store directly; the broker scopes, audits and revokes what BattleLab hands out.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from . import template_secrets, vault_files

log = logging.getLogger(__name__)

ENV_SOCKET = "BATTLELAB_SECRETS_SOCKET"
#: The PATH of this launch's token file (0600, in its private RAM-backed directory) — never the
#: token itself: an MCP payload rides argv (`--mcp-config`, `-c …env=`), which every user on the
#: host can read in /proc, and an API worker's config is persisted (independent review of #1796).
ENV_TOKEN_FILE = "BATTLELAB_SECRETS_TOKEN_FILE"  # noqa: S105 — a variable NAME
ENV_HELPER = "BATTLELAB_SECRETS_HELPER"
#: This launch's own copy directory, created at mint when the runtime root is RAM-backed. An
#: agent CLI that confines commands to its working directories (claude) is given exactly this
#: directory (`--add-dir`), never the copies root (#1786 smoke).
ENV_DIR = "BATTLELAB_SECRETS_DIR"
#: What every launch gets with a capability. The helper and the directory only where they exist.
ENV_KEYS = frozenset({ENV_SOCKET, ENV_TOKEN_FILE, ENV_DIR})
ALL_ENV_KEYS = frozenset({ENV_SOCKET, ENV_TOKEN_FILE, ENV_HELPER, ENV_DIR})
TOKEN_FILE = ".token"  # noqa: S105 — a file NAME
#: The helper console script, installed beside the app's interpreter (`[project.scripts]`).
HELPER_NAME = "battlelab-secrets"
RUNTIME_ENV = "AGENT_SESSIONS_SECRETS_RUNTIME"

AUDIT_PER_SECRET = 1000
#: A capability whose launch never came alive is revoked by the periodic pass after this long.
LAUNCH_GRACE_S = 300.0
RECONCILE_EVERY_S = 30.0
REQUEST_MAX = 4096
TMPFS_MAGIC = 0x01021994
RAMFS_MAGIC = 0x858458F6

#: Refusal codes: the only words a refusal ever carries (never input echoed back).
REFUSALS = frozenset(
    {
        "unknown_capability",
        "launch_ended",
        "no_project",
        "project_changed",
        "not_offered",
        "access_off",
        "not_bound",
        "needs_reentry",
        "store_unavailable",
        "audit_unavailable",
        "storage_unavailable",
        "bad_request",
        "launch_unconfirmed",
    }
)
_CAPABILITY_LABEL = "secrets-capability"
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")


class Refused(Exception):
    def __init__(self, code: str) -> None:
        assert code in REFUSALS
        super().__init__(code)
        self.code = code


# ---- paths ----------------------------------------------------------------------------------


def _store_dir() -> Path:
    from . import template_vars

    return template_vars.store_path().parent


def db_path() -> Path:
    return _store_dir() / "secrets-broker.db"


def runtime_root() -> Path:
    override = os.environ.get(RUNTIME_ENV)
    if override:
        return Path(override)
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.geteuid()}"
    return Path(base) / "agent-sessions"


def socket_path() -> Path:
    """The broker's socket, in THIS instance's runtime directory (`ptybridge.runtime_dir`, the
    one its dtach sockets use) and named after its store, so a test, a staging or a dev instance
    on the same host never binds — or removes — another instance's socket."""
    import hashlib

    from . import ptybridge

    tag = hashlib.sha256(str(_store_dir().resolve()).encode()).hexdigest()[:10]
    return ptybridge.runtime_dir() / f"secrets-{tag}.sock"


def copies_root() -> Path:
    return runtime_root() / "secrets"


def helper_path() -> str | None:
    """The helper's absolute path in the app's own environment, or None when it is not installed
    (an agent without the MCP channel then gets no helper — never a guessed command)."""
    candidate = Path(sys.executable).parent / HELPER_NAME
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _ram_backed(path: Path) -> bool:
    """True when ``path`` lives on tmpfs or ramfs (``statfs`` f_type), never assumed."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    buf = ctypes.create_string_buffer(256)
    if libc.statfs(os.fsencode(str(path)), buf) != 0:
        return False
    f_type = ctypes.c_long.from_buffer(buf).value & 0xFFFFFFFF
    return f_type in (TMPFS_MAGIC, RAMFS_MAGIC)


#: Tests point the runtime root at a plain directory; production never sets this.
_ram_check = _ram_backed


def _private_dir(path: Path) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise Refused("storage_unavailable")


# ---- the audit / capability store ------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS caps(
    id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, session_key TEXT NOT NULL,
    phys_key TEXT NOT NULL, engine TEXT NOT NULL, project_id TEXT NOT NULL, kind TEXT NOT NULL,
    worker_id TEXT, cwd TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
    master TEXT);
CREATE TABLE IF NOT EXISTS audit(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, var_id TEXT, name TEXT,
    project_id TEXT, engine TEXT, session_key TEXT, cap_id TEXT, outcome TEXT NOT NULL,
    reason TEXT, marker TEXT, path TEXT);
CREATE INDEX IF NOT EXISTS audit_var ON audit(var_id, seq);
CREATE TABLE IF NOT EXISTS copies(
    cap_id TEXT NOT NULL, name TEXT NOT NULL, var_id TEXT NOT NULL, marker TEXT NOT NULL,
    path TEXT NOT NULL, PRIMARY KEY(cap_id, name));
"""


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    if fresh:
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@contextlib.contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


_thread_fence = threading.RLock()


@contextlib.contextmanager
def access_fence() -> Iterator[None]:
    """The one fence between grants and revocation (see the module docstring). Re-entrant in a
    thread; an ``flock`` across processes."""
    with _thread_fence:
        if getattr(_held, "depth", 0):
            _held.depth += 1
            try:
                yield
            finally:
                _held.depth -= 1
            return
        lock = _store_dir() / "secrets-access.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.depth = 1
            try:
                yield
            finally:
                _held.depth = 0
        finally:
            os.close(fd)


_held = threading.local()


def _audit(conn: sqlite3.Connection, cap: sqlite3.Row | None, **row) -> int:
    """One audit row in its own transaction, trimmed per secret in the same transaction."""
    with _txn(conn):
        cur = conn.execute(
            "INSERT INTO audit(at, var_id, name, project_id, engine, session_key, cap_id, outcome,"
            " reason, marker, path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                row.get("var_id"),
                row.get("name"),
                cap["project_id"] if cap is not None else None,
                cap["engine"] if cap is not None else None,
                cap["session_key"] if cap is not None else None,
                cap["id"] if cap is not None else None,
                row["outcome"],
                row.get("reason"),
                row.get("marker"),
                row.get("path"),
            ),
        )
        if not row.get("var_id"):
            # A refusal for a name the store does not know (a typo, a looping agent) is bounded
            # too (Hermes 7421): the newest AUDIT_PER_SECRET such rows are kept.
            conn.execute(
                "DELETE FROM audit WHERE var_id IS NULL AND seq NOT IN (SELECT seq FROM audit"
                " WHERE var_id IS NULL ORDER BY seq DESC LIMIT ?)",
                (AUDIT_PER_SECRET,),
            )
        if row.get("var_id"):
            # Refusals and grants are bounded separately: any project's agent can be refused
            # a secret by name, and those rows must never evict its grant history.
            refused = int(row.get("outcome") == "refused")
            conn.execute(
                "DELETE FROM audit WHERE var_id = ? AND (outcome = 'refused') = ? AND seq NOT IN"
                " (SELECT seq FROM audit WHERE var_id = ? AND (outcome = 'refused') = ?"
                " ORDER BY seq DESC LIMIT ?)",
                (row["var_id"], refused, row["var_id"], refused, AUDIT_PER_SECRET),
            )
    return int(cur.lastrowid)


def access_log(var_id: str, limit: int = 50) -> list[dict]:
    """The newest requests for one secret, for the Library page: never a value or a path."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT at, project_id, engine, session_key, outcome, reason FROM audit"
            " WHERE var_id = ? ORDER BY seq DESC LIMIT ?",
            (var_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---- minting a launch's capability ----------------------------------------------------------

_server_running = False


def _project_for(session_key: str, cwd: str) -> str | None:
    """The project ENTITY a session resolves to now (explicit assignment, else its folder) — the
    same resolution project memory uses; an implicit folder group has no secrets."""
    from . import metadata, projects

    meta = metadata.get(metadata.resolve_key(session_key))
    ref = projects.resolve(cwd, meta.project_id, projects.load(), alias=meta.project_alias)
    return ref.id if ref.kind == "project" else None


def launch_env(
    session_key: str,
    phys_key: str,
    cwd: str,
    *,
    engine: str,
    kind: str,
    worker_id: str | None = None,
) -> dict[str, str]:
    """The secrets variables for ONE new agent process, or ``{}``. Never raises: a launch never
    fails for secrets. Nothing is offered outside a project, while the broker is not serving, or
    without a private RAM-backed launch directory (the token file and every copy live there)."""
    try:
        if not _server_running:
            return {}
        pid = _project_for(session_key, cwd)
        if pid is None:
            return {}
        cap_id = secrets.token_hex(8)
        launch_dir = _prepare_launch_dir(cap_id)
        if launch_dir is None:
            return {}
        token = secrets.token_urlsafe(32)
        conn = _connect()
        try:
            # A LAUNCH happens only when the session has no live master (the single-writer lock):
            # every earlier capability of this session belongs to a process that is gone, so it
            # ends now — an unprobed one can never bind to this new master (independent review).
            with access_fence():
                for old in conn.execute(
                    "SELECT * FROM caps WHERE phys_key = ? AND revoked_at IS NULL", (phys_key,)
                ).fetchall():
                    _revoke(conn, old)
            with _txn(conn):
                conn.execute(
                    "INSERT INTO caps(id, digest, session_key, phys_key, engine, project_id, kind,"
                    " worker_id, cwd, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        cap_id,
                        template_secrets.keyed_digest(_CAPABILITY_LABEL, token),
                        session_key,
                        phys_key,
                        engine,
                        pid,
                        kind,
                        worker_id,
                        cwd,
                        time.time(),
                    ),
                )
            try:
                token_file = _write_token(launch_dir, token)
            except BaseException:
                with _txn(conn):
                    conn.execute("DELETE FROM caps WHERE id = ?", (cap_id,))
                raise
        except BaseException:
            _remove_launch(cap_id)  # never a token file or directory without its row
            raise
        finally:
            conn.close()
        env = {
            ENV_SOCKET: str(socket_path()),
            ENV_TOKEN_FILE: str(token_file),
            ENV_DIR: str(launch_dir),
        }
        helper = helper_path()
        if helper is not None:
            env[ENV_HELPER] = helper
        return env
    except Exception:  # noqa: BLE001 — a launch never fails for secrets
        log.warning("secrets unavailable for %s", session_key, exc_info=True)
        return {}


def _write_token(launch_dir: Path, token: str) -> Path:
    dfd = _open_nofollow_dir(launch_dir)
    try:
        fd = os.open(
            TOKEN_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dfd,
        )
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return launch_dir / TOKEN_FILE


def scrub_process_environment() -> None:
    """The app's own environment never carries a launch's capability (inherited by every child)."""
    for key in ALL_ENV_KEYS:
        os.environ.pop(key, None)


# ---- liveness (tri-state: a slow probe never revokes a live launch) ---------------------------

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def _liveness_impl(conn: sqlite3.Connection, cap: sqlite3.Row) -> str:
    """Is this LAUNCH's agent process running? ALIVE / DEAD / UNKNOWN (a probe that timed out —
    never read as dead). A terminal launch is bound, the first time it answers, to its dtach
    socket's inode and MODIFICATION time — set when the master binds it, never changed by the
    chmod dtach does on every attach and detach (only ctime moves; independent review of #1796),
    and different for any later master on the same path. A new LAUNCH of the session also revokes
    its earlier capabilities outright (`launch_env`)."""
    if cap["kind"] == "api":
        return _api_liveness(cap["worker_id"])
    from . import ptybridge

    engine, _, native = cap["phys_key"].partition(":")
    if not native:
        return DEAD
    try:
        sock = ptybridge.socket_path(engine, native)
    except ptybridge.PtyBridgeError:
        return DEAD
    verdict = ptybridge.probe_master(sock)
    if verdict == ptybridge.UNKNOWN:
        return UNKNOWN
    if verdict == ptybridge.DEAD:
        return DEAD
    try:
        st = os.lstat(sock)
    except OSError:
        return UNKNOWN
    identity = f"{st.st_ino}:{st.st_mtime_ns}"
    if cap["master"] is None:
        with _txn(conn):
            conn.execute(
                "UPDATE caps SET master = ? WHERE id = ? AND master IS NULL", (identity, cap["id"])
            )
        return ALIVE
    return ALIVE if cap["master"] == identity else DEAD


#: The probe every caller uses (tests substitute a table for it; `_liveness_impl` stays real).
_liveness = _liveness_impl


def _api_liveness(worker_id: str | None) -> str:
    if not worker_id:
        return DEAD
    from . import native_runtime

    identity = native_runtime._identity(worker_id)
    if identity is None:
        return DEAD
    observation = native_runtime.HOST.show(identity.worker)
    if observation is None:
        return UNKNOWN  # systemd did not answer in time: never a revocation
    if observation.active_state == "active" and observation.invocation_id == identity.invocation_id:
        return ALIVE
    return DEAD


# ---- policy ----------------------------------------------------------------------------------


def _offered(pid: str) -> tuple[dict[str, dict], dict[str, str]]:
    """``(offered, refused)`` for project ``pid`` from ONE strict read: the records whose value an
    agent of this project may request (keyed by the name the project knows it by), and why the
    project's other secrets are not offered."""
    from . import template_vars as store

    try:
        records, damaged = store._read(store.store_path())
    except Exception:  # noqa: BLE001 — unreadable or newer store: nothing is offered
        raise Refused("store_unavailable") from None
    if damaged:
        raise Refused("store_unavailable")
    globals_ = {r["name"]: r for r in records if store._is_global(r)}
    offered: dict[str, dict] = {}
    refused: dict[str, str] = {}
    for rec in records:
        if rec["scope"] != store.SCOPE_PROJECT or rec["project_id"] != pid:
            continue
        if rec["kind"] not in store.SECRET_KINDS:
            continue
        holder = rec
        if rec.get("ref") == store.REF_GLOBAL:
            holder = globals_.get(rec["name"])
            if holder is None or holder["kind"] != rec["kind"]:
                refused[rec["name"]] = "not_bound"
                continue
        if holder.get("unset"):
            refused[rec["name"]] = "needs_reentry"
        elif not holder.get("agent_access"):
            refused[rec["name"]] = "access_off"
        else:
            offered[rec["name"]] = holder
    return offered, refused


def _marker(rec: dict) -> str:
    """What makes a copy current: the holding record's identity and its VALUE's revision — the
    file's revision and blob identity, or a digest of the sealed envelope (a fresh nonce on every
    seal). Never `updated_at`: a label, project or access edit bumps it without changing the
    bytes, and must not destroy a copy an agent is using (independent review of #1803)."""
    if rec["kind"] == "secret_file":
        # The revision restarts at 1 after a clear, so the blob's own identity is part of it.
        rev = f"{rec['file']['rev']}.{vault_files.identity(rec['id'], rec['file']['rev'])}"
    else:
        sealed = json.dumps(rec.get("secret"), sort_keys=True, separators=(",", ":"))
        rev = hashlib.sha256(sealed.encode()).hexdigest()[:32]
    return f"{rec['id']}:{rev}"


def _still_current(rec: dict, marker: str) -> bool:
    """Whether a copy's marker still matches; an unknowable one (a vault directory that is not
    ours) is a mismatch, so the copy is destroyed and the rest of the pass goes on."""
    try:
        return _marker(rec) == marker
    except Exception:  # noqa: BLE001 — fail closed
        log.warning("secrets broker: a copy's revision could not be checked; it is removed")
        return False


def _describe(name: str, rec: dict, secrets_now: list[str]) -> dict:
    """What an agent sees of one offered secret. Labels were checked when saved, but a secret
    stored LATER may appear in one, so they are redacted against the inventory as of this call."""
    definition = rec.get("definition") or {}
    description = definition.get("help") or definition.get("label") or ""
    out = {
        "name": name,
        "kind": rec["kind"],
        "description": template_secrets.redact_text(description, secrets_now),
    }
    if rec["kind"] == "secret_file":
        out["file_name"] = template_secrets.redact_text(rec["file"]["name"], secrets_now)
        out["size"] = rec["file"]["size"]
    return out


def _value(rec: dict) -> bytes | None:
    from . import template_vars as store

    if rec["kind"] == "secret_file":
        return store.file_bytes(rec)
    text = store._decrypt(rec)
    return None if text is None else text.encode("utf-8")


# ---- copies (descriptor-relative; a link or a planted entry is never followed) ---------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _launch_dir(cap_id: str) -> Path:
    return copies_root() / cap_id


def _open_nofollow_dir(path: Path) -> int:
    fd = os.open(path, _DIR_FLAGS)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise Refused("storage_unavailable")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _private_child(parent_fd: int, name: str) -> int:
    """``name`` under ``parent_fd`` as a private directory of ours, created as needed, opened
    without following a link."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        os.close(fd)
        raise Refused("storage_unavailable")
    return fd


def _private_launch(cap_id: str) -> Path:
    """This launch's private directory on RAM-backed storage, created as needed; refuses
    (``storage_unavailable``) anywhere else."""
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_dir(root)
    _private_dir(copies_root())
    if not _ram_check(copies_root()):
        raise Refused("storage_unavailable")
    cfd = _open_nofollow_dir(copies_root())
    try:
        os.close(_private_child(cfd, cap_id))
    finally:
        os.close(cfd)
    return _launch_dir(cap_id)


def _prepare_launch_dir(cap_id: str) -> Path | None:
    try:
        return _private_launch(cap_id)
    except (Refused, OSError):
        return None  # no capability: nothing could be materialized for it anyway


def _copy_name(name: str, rec: dict) -> str:
    """A copy keeps the file's own name (a tool may require it, e.g. ``AuthKey_<ID>.p8``), in a
    directory of its secret's name; a text secret is a file named after the secret."""
    if rec["kind"] == "secret_file":
        return rec["file"]["name"]
    return name


def _destroy_at(dfd: int, name: str) -> None:
    """Remove one entry of a directory we hold: a plain file of ours is truncated first; a link
    is unlinked (never followed); a directory is emptied the same way and removed; anything
    else is left. Never raises: one odd entry never stops a revocation."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return
    try:
        if stat.S_ISDIR(st.st_mode):
            sub = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            try:
                for inner in os.listdir(sub):
                    _destroy_at(sub, inner)
            finally:
                os.close(sub)
            os.rmdir(name, dir_fd=dfd)
            return
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid():
            fd = os.open(
                name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
            try:
                os.ftruncate(fd, 0)
            finally:
                os.close(fd)
        os.unlink(name, dir_fd=dfd)
    except OSError:
        log.warning("secrets broker: an entry of a launch directory could not be removed")


def _walk(cap_id: str, *names: str) -> int | None:
    """A descriptor on ``copies_root/cap_id/names…``, every step without following a link, or
    None when any step is missing, a link, or not ours."""
    try:
        fd = _open_nofollow_dir(copies_root())
    except (OSError, Refused):
        return None
    for part in (cap_id, *names):
        try:
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except OSError:
            os.close(fd)
            return None
        os.close(fd)
        fd = nxt
    return fd


def _destroy_copy(cap_id: str, name: str) -> None:
    """Every copy of secret ``name`` in launch ``cap_id``: its directory and all it holds."""
    fd = _walk(cap_id)
    if fd is None:
        return
    try:
        _destroy_at(fd, name)
    finally:
        os.close(fd)


def _remove_launch(cap_id: str) -> None:
    fd = _walk(cap_id)
    if fd is not None:
        try:
            for name in os.listdir(fd):
                _destroy_at(fd, name)
        finally:
            os.close(fd)
    try:
        cfd = _open_nofollow_dir(copies_root())
    except (OSError, Refused):
        return
    try:
        with contextlib.suppress(OSError):
            os.rmdir(cap_id, dir_fd=cfd)
    finally:
        os.close(cfd)


def _publish_copy(cap_id: str, name: str, base: str, data: bytes) -> Path:
    launch = _private_launch(cap_id)
    lfd = _walk(cap_id)
    if lfd is None:
        raise Refused("storage_unavailable")
    try:
        # Earlier copies of this secret (another revision or file name), or anything an agent
        # planted under its name, go first; then a fresh private directory.
        _destroy_at(lfd, name)
        hfd = _private_child(lfd, name)
    finally:
        os.close(lfd)
    try:
        tmp = f".tmp-{secrets.token_hex(8)}"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=hfd,
        )
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
            os.rename(tmp, base, src_dir_fd=hfd, dst_dir_fd=hfd)
        except BaseException:
            # A partial write (tmpfs full) or a failed rename never leaves bytes behind
            # (Hermes 7421): the agent can read its launch directory.
            _destroy_at(hfd, tmp)
            raise
    finally:
        os.close(hfd)
    return launch / name / base


# ---- a request -------------------------------------------------------------------------------

#: Test hook: called between the policy check and publishing a copy (the revocation barrier).
_before_publish = None


def handle(token: object, op: object, name: object = None) -> dict:
    """One request from an agent. Returns ``{"ok": True, ...}`` or ``{"ok": False, "error":
    <code>}``; never raises, and never says more than a refusal code.

    The token is looked up and the launch's liveness probed BEFORE the fence, so an unknown token
    or a slow probe never queues behind (or holds) the fence; policy and the grant run inside it."""
    try:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise Refused("unknown_capability")
        if op not in ("list", "path"):
            raise Refused("bad_request")
        if op == "path" and (not isinstance(name, str) or not _NAME_RE.fullmatch(name)):
            raise Refused("bad_request")
        try:
            conn = _connect()
        except sqlite3.Error:
            log.warning("secrets broker: the audit store cannot be opened; request refused")
            raise Refused("audit_unavailable") from None
        try:
            return _handle(conn, token, op, name)  # type: ignore[arg-type]
        finally:
            conn.close()
    except Refused as r:
        return {"ok": False, "error": r.code}
    except Exception:  # noqa: BLE001 — an agent never sees a traceback
        log.warning("secrets broker: a request failed", exc_info=True)
        return {"ok": False, "error": "store_unavailable"}


def _handle(conn: sqlite3.Connection, token: str, op: str, name: str | None) -> dict:
    try:
        digest = template_secrets.keyed_digest(_CAPABILITY_LABEL, token)
    except template_secrets.SecretKeyUnavailable:
        raise Refused("store_unavailable") from None
    cap = conn.execute(
        "SELECT * FROM caps WHERE digest = ? AND revoked_at IS NULL", (digest,)
    ).fetchone()
    if cap is None:
        raise Refused("unknown_capability")
    state = _liveness(conn, cap)
    if state == DEAD:
        with access_fence():
            _revoke(conn, cap)
        raise Refused("launch_ended")
    if state == UNKNOWN:
        raise Refused("launch_unconfirmed")
    with access_fence():
        cap = conn.execute(
            "SELECT * FROM caps WHERE id = ? AND revoked_at IS NULL", (cap["id"],)
        ).fetchone()
        if cap is None:
            raise Refused("unknown_capability")  # revoked while we probed
        now_pid = _project_for(cap["session_key"], cap["cwd"])
        if now_pid is None:
            raise Refused("no_project")
        if now_pid != cap["project_id"]:
            raise Refused("project_changed")
        offered, refused = _offered(cap["project_id"])
        if op == "list":
            try:
                secrets_now = template_secrets.redaction_values_for_labels()
            except template_secrets.RedactionUnavailable:
                raise Refused("store_unavailable") from None
            return {
                "ok": True,
                "secrets": [_describe(n, r, secrets_now) for n, r in sorted(offered.items())],
            }
        assert name is not None
        rec = offered.get(name)
        if rec is None:
            code = refused.get(name, "not_offered")
            _log_refusal(conn, cap, name, code)
            raise Refused(code)
        return _grant(conn, cap, name, rec)


def _log_refusal(conn: sqlite3.Connection, cap: sqlite3.Row, name: str, code: str) -> None:
    from . import template_vars as store

    var_id = None
    with contextlib.suppress(Exception):
        records, _ = store._read(store.store_path())
        holder = next(
            (r for r in records if r["name"] == name and r["project_id"] == cap["project_id"]),
            None,
        )
        if holder is None or holder.get("ref") == store.REF_GLOBAL:
            # A project that does not bind the name is logged against the library secret of
            # that name, so its log shows the refusal too (bounded per secret like the rest).
            holder = next((r for r in records if store._is_global(r) and r["name"] == name), None)
        var_id = holder["id"] if holder is not None else None
    try:
        _audit(conn, cap, var_id=var_id, name=name, outcome="refused", reason=code)
    except sqlite3.Error:
        log.warning("secrets broker: a refusal (%s) could not be logged", code)


def _grant(conn: sqlite3.Connection, cap: sqlite3.Row, name: str, rec: dict) -> dict:
    # The marker first: a re-publish between the two then always reads as a mismatch, never as
    # old bytes under the new blob's identity.
    marker = _marker(rec)
    data = _value(rec)
    if data is None:
        _log_refusal(conn, cap, name, "needs_reentry")
        raise Refused("needs_reentry")
    base = _copy_name(name, rec)
    target = str(_launch_dir(cap["id"]) / name / base)
    try:
        seq = _audit(
            conn, cap, var_id=rec["id"], name=name, outcome="pending", marker=marker, path=target
        )
    except sqlite3.Error:
        log.warning("secrets broker: the audit store cannot be written; request refused")
        raise Refused("audit_unavailable") from None
    if _before_publish is not None:
        _before_publish()
    try:
        path = _publish_copy(cap["id"], name, base, data)
    except Refused as r:
        _settle(conn, seq, "failed", r.code)
        raise
    except OSError:
        _settle(conn, seq, "failed", "storage_unavailable")
        raise Refused("storage_unavailable") from None
    try:
        with _txn(conn):
            conn.execute("UPDATE audit SET outcome = 'materialized' WHERE seq = ?", (seq,))
            conn.execute(
                "INSERT OR REPLACE INTO copies(cap_id, name, var_id, marker, path)"
                " VALUES(?,?,?,?,?)",
                (cap["id"], name, rec["id"], marker, str(path)),
            )
    except sqlite3.Error:
        # Never a copy without its settled row: the one just published goes.
        _destroy_copy(cap["id"], name)
        log.warning("secrets broker: a grant could not be settled; its copy was removed")
        raise Refused("audit_unavailable") from None
    return {"ok": True, "path": str(path)}


def _settle(conn: sqlite3.Connection, seq: int, outcome: str, reason: str) -> None:
    try:
        with _txn(conn):
            conn.execute(
                "UPDATE audit SET outcome = ?, reason = ? WHERE seq = ?", (outcome, reason, seq)
            )
    except sqlite3.Error:
        log.warning("secrets broker: a failed grant could not be settled")


# ---- revocation ------------------------------------------------------------------------------


def _revoke(conn: sqlite3.Connection, cap: sqlite3.Row) -> None:
    _remove_launch(cap["id"])
    with _txn(conn):
        conn.execute("UPDATE caps SET revoked_at = ? WHERE id = ?", (time.time(), cap["id"]))
        conn.execute("DELETE FROM copies WHERE cap_id = ?", (cap["id"],))


def revoke_launch(phys_key: str) -> None:
    """A session's runtime is being torn down (archive): its capabilities and copies go now.
    Blocking — async callers run it off the event loop (`runtime_cleanup`)."""
    try:
        with access_fence():
            conn = _connect()
            try:
                for cap in conn.execute(
                    "SELECT * FROM caps WHERE phys_key = ? AND revoked_at IS NULL", (phys_key,)
                ).fetchall():
                    _revoke(conn, cap)
            finally:
                conn.close()
    except Exception:  # noqa: BLE001 — teardown never blocks on secrets; the pass retries
        log.warning("secrets broker: revoking %s failed", phys_key, exc_info=True)


def _copy_parts(path: str) -> tuple[str, str] | None:
    """``(cap_id, secret)`` of a copy path under the copies root, or None."""
    try:
        rel = Path(path).relative_to(copies_root())
    except ValueError:
        return None
    parts = rel.parts
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def reconcile(*, startup: bool = False, probe: bool = True) -> None:
    """Re-judge launches and copies against the CURRENT store, under the fence.

    ``probe=False`` is the store's post-commit revoker: policy only — it runs right after a
    variables write, OUTSIDE that write's fences, and never probes a process (a slow probe must
    not hold anything). The periodic pass and app start also probe liveness: a DEAD launch is
    revoked (a never-live one only after its grace), an UNKNOWN one is left alone. At ``startup``
    every ``pending`` row's copy is destroyed and the row settled ``interrupted``. One cap's
    trouble never stops the rest. Raises nothing."""
    try:
        conn = _connect()
        try:
            states: dict[str, str] = {}
            if probe:
                # Probed BEFORE the fence: a slow liveness probe never holds up grants or revokers.
                for cap in conn.execute("SELECT * FROM caps WHERE revoked_at IS NULL").fetchall():
                    try:
                        states[cap["id"]] = _liveness(conn, cap)
                    except Exception:  # noqa: BLE001 — unknowable this pass
                        states[cap["id"]] = UNKNOWN
            with access_fence():
                if startup:
                    _settle_pending(conn)
                offers: dict[str, dict[str, dict]] = {}
                for cap in conn.execute("SELECT * FROM caps WHERE revoked_at IS NULL").fetchall():
                    try:
                        _reconcile_cap(
                            conn, cap, offers, startup=startup, state=states.get(cap["id"])
                        )
                    except Exception:  # noqa: BLE001 — the next pass retries this one
                        log.warning("secrets broker: reconciling a launch failed", exc_info=True)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the next pass retries
        log.warning("secrets broker: reconcile failed", exc_info=True)


def _settle_pending(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT seq, path, cap_id, name FROM audit WHERE outcome = 'pending'"
    ).fetchall():
        parts = _copy_parts(row["path"]) if row["path"] else None
        newer = conn.execute(
            "SELECT 1 FROM audit WHERE cap_id = ? AND name = ? AND seq > ?"
            " AND outcome = 'materialized'",
            (row["cap_id"], row["name"], row["seq"]),
        ).fetchone()
        if newer is not None:
            # A later grant of the same secret to the same launch succeeded: its copy is the one
            # on disk, and this stale row must not take it away (Hermes 7421).
            with _txn(conn):
                conn.execute(
                    "UPDATE audit SET outcome = 'interrupted', reason = 'superseded' WHERE seq = ?",
                    (row["seq"],),
                )
            continue
        if parts is not None:
            _destroy_copy(*parts)
            with _txn(conn):
                conn.execute(
                    "DELETE FROM copies WHERE cap_id = ? AND name = ?", (parts[0], parts[1])
                )
        with _txn(conn):
            conn.execute(
                "UPDATE audit SET outcome = 'interrupted', reason = 'copy removed' WHERE seq = ?",
                (row["seq"],),
            )


def _reconcile_cap(
    conn: sqlite3.Connection, cap: sqlite3.Row, offers: dict, *, startup: bool, state: str | None
) -> None:
    copies = conn.execute("SELECT * FROM copies WHERE cap_id = ?", (cap["id"],)).fetchall()
    if state is not None:
        if state == DEAD and (
            startup
            or copies
            or cap["master"] is not None
            or time.time() - cap["created_at"] > LAUNCH_GRACE_S
        ):
            _revoke(conn, cap)
            return
    _sweep_untracked(cap["id"], {c["name"] for c in copies})
    if not copies:
        return
    if cap["project_id"] not in offers:
        try:
            offers[cap["project_id"]] = _offered(cap["project_id"])[0]
        except Refused:
            offers[cap["project_id"]] = {}
    offered = offers[cap["project_id"]]
    try:
        same_project = _project_for(cap["session_key"], cap["cwd"]) == cap["project_id"]
    except Exception:  # noqa: BLE001 — unknowable: treat as moved
        same_project = False
    for copy in copies:
        rec = offered.get(copy["name"])
        if same_project and rec is not None and _still_current(rec, copy["marker"]):
            continue
        _destroy_copy(cap["id"], copy["name"])
        with _txn(conn):
            conn.execute(
                "DELETE FROM copies WHERE cap_id = ? AND name = ?", (cap["id"], copy["name"])
            )


def _sweep_untracked(cap_id: str, tracked: set[str]) -> None:
    """Destroy every entry of a launch directory no settled copy accounts for — a secret
    directory left by a failed grant, a stray temporary, anything planted — except the token
    file. Runs under the fence (no grant is mid-publish)."""
    fd = _walk(cap_id)
    if fd is None:
        return
    try:
        for entry in os.listdir(fd):
            if entry == TOKEN_FILE:
                continue
            if entry in tracked:
                sub = _walk(cap_id, entry)
                if sub is None:
                    continue
                try:
                    for inner in os.listdir(sub):
                        if inner.startswith(".tmp-"):
                            _destroy_at(sub, inner)
                finally:
                    os.close(sub)
                continue
            _destroy_at(fd, entry)
    finally:
        os.close(fd)


def after_store_commit() -> None:
    """The variables store committed a write (`template_vars`, after releasing its fences): its
    agents' copies are re-judged against it now, policy only."""
    if db_path().exists():
        reconcile(probe=False)


# ---- the socket ------------------------------------------------------------------------------


def _peer_uid(writer) -> int | None:
    import socket as socket_mod
    import struct

    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        creds = sock.getsockopt(
            socket_mod.SOL_SOCKET, socket_mod.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


async def _client(reader, writer) -> None:
    import asyncio

    try:
        if _peer_uid(writer) != os.geteuid():
            return
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except (TimeoutError, ValueError):
            return
        if len(line) > REQUEST_MAX:
            reply = {"ok": False, "error": "bad_request"}
        else:
            try:
                req = json.loads(line)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                reply = {"ok": False, "error": "bad_request"}
            else:
                reply = await asyncio.to_thread(
                    handle, req.get("token"), req.get("op"), req.get("name")
                )
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def _answers(path: Path) -> bool:
    """A live listener on ``path`` (another broker): it is never unlinked or replaced."""
    import socket as socket_mod

    s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _bound_private(path: Path):
    """A listening-ready AF_UNIX socket bound at ``path`` and made 0600 BEFORE it listens.

    Never through ``os.umask``: the umask is process-global, and narrowing it around the bind
    made every file or directory another thread created in that moment unreadable (a directory
    without its execute bit — flaky tests, and the same window in the running app). The instant
    between bind and chmod is harmless: a socket accepts no connection before ``listen``, which
    asyncio calls only after it is handed this socket, and every peer is checked for our uid."""
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
    except BaseException:
        sock.close()
        raise
    return sock


async def serve() -> None:
    """Serve the broker until cancelled: startup reconciliation first, then the socket, then a
    periodic pass. A socket some other process still answers on is never taken over; a failure to
    bind leaves secrets unavailable (launches get no capability)."""
    import asyncio

    global _server_running
    await asyncio.to_thread(reconcile, startup=True)
    path = socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if await asyncio.to_thread(_answers, path):
                log.warning("secrets broker: %s is served by another process; not serving", path)
                return
            path.unlink()
        server = await asyncio.start_unix_server(
            _client, sock=_bound_private(path), limit=REQUEST_MAX + 2
        )
        ours = os.lstat(path).st_ino
    except (OSError, Refused):
        log.warning("secrets broker: cannot serve; agents get no secrets tool", exc_info=True)
        return
    _server_running = True
    try:
        async with server:
            while True:
                await asyncio.sleep(RECONCILE_EVERY_S)
                await asyncio.to_thread(reconcile)
    finally:
        _server_running = False
        with contextlib.suppress(OSError):
            if os.lstat(path).st_ino == ours:
                path.unlink()  # only the socket THIS server bound
