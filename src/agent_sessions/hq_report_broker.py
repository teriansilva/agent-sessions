"""The report broker (#1882, P1a of #1878): how a running agent tells HQ about ITSELF.

    agent ──(battlelab_hq MCP)──▶ hq-report-<tag>.sock ──▶ handle() ──▶ agent_reports.file()

**A capability is one launch, and it can only report.** It is minted where a new pty agent
process is born — an interactive terminal LAUNCH (`routes/terminal.py`) and an unattended
dispatch (`headless_dispatch.py`), the seams that mint memory paths and the secrets capability —
and never on an attach. It is stored as a keyed digest (`template_secrets.keyed_digest`, its own
label) with the launch's identity: session key, physical key, engine. Its id IS the launch
generation a report and an answer are bound to. The token itself is a 0600 FILE in the launch's
private RAM-backed directory; the environment and the MCP payload (argv) carry only its PATH.

**Separate from the secrets broker on every axis** (#1878 D1): its own table and database, its
own socket and its own digest label, and one verb, ``report``. A report token is refused by the
secrets broker and a secrets token here, because neither digest exists in the other's table.

**Valid only while its launch is live** — the dtach master answers, and it is the master this
capability was first seen with (socket inode + mtime, the secrets broker's identity rule) —
checked on every call. A new LAUNCH of the session revokes every earlier capability of it
(whatever the "Sessions can report to HQ" switch says) and ends their reports. A launch whose
master is gone is revoked by the periodic pass, and its report ends (`agent_reports`).

The agent never names a session: the broker maps the token to the launch's physical key.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from . import agent_reports, template_secrets

log = logging.getLogger(__name__)

ENV_SOCKET = "BATTLELAB_HQ_SOCKET"
#: The PATH of this launch's token file — never the token (it rides argv in an MCP payload).
ENV_TOKEN_FILE = "BATTLELAB_HQ_TOKEN_FILE"  # noqa: S105 — a variable NAME
ENV_KEYS = frozenset({ENV_SOCKET, ENV_TOKEN_FILE})
#: The capability id, in the LAUNCH's own process environment — inherited by its dtach master —
#: so a bind can ask the kernel whether a master belongs to this capability (`_master_carries`).
#: An identifier, not the token: it authorizes nothing, but stays out of logs and responses.
ENV_CAP = "BATTLELAB_HQ_REPORT_CAP"
#: Every name a launch's environment may carry for this tool; scrubbed from any other process.
ALL_ENV_KEYS = ENV_KEYS | {ENV_CAP}
#: The most of a master's ``/proc/<pid>/environ`` a bind reads (it is the launch's environment).
ENVIRON_MAX = 1 << 20
TOKEN_FILE = ".token"  # noqa: S105 — a file NAME

LAUNCH_GRACE_S = 300.0
RECONCILE_EVERY_S = 30.0
#: One request line, in bytes. The largest VALID report — 280 + 280 + 4 × 80 characters, every
#: one an astral-plane character — is 3,520 bytes as UTF-8 (what `hq_report_mcp` sends) and
#: 10,560 as escaped surrogate pairs, 12 bytes each (a client on json's ASCII default), plus the
#: token and the envelope. Both fit, so a valid report is never refused for its encoding, and a
#: longer one reaches the field caps (`too_long`) rather than the transport.
REQUEST_MAX = 16384

#: Refusal codes: the only words a refusal carries.
REFUSALS = frozenset(
    {"unknown_capability", "launch_ended", "launch_unconfirmed", "bad_request", "store_unavailable"}
    | agent_reports.REFUSALS
)
_CAPABILITY_LABEL = "hq-report-capability"


class Refused(Exception):
    def __init__(self, code: str) -> None:
        assert code in REFUSALS
        super().__init__(code)
        self.code = code


# ---- paths ----------------------------------------------------------------------------------


def _store_dir() -> Path:
    return agent_reports.store_path().parent


def db_path() -> Path:
    return _store_dir() / "hq-report-broker.db"


def socket_path() -> Path:
    """In THIS instance's runtime directory, named after its store (so a test or staging
    instance on the same host never binds or removes another instance's socket)."""
    from . import ptybridge

    tag = hashlib.sha256(str(_store_dir().resolve()).encode()).hexdigest()[:10]
    return ptybridge.runtime_dir() / f"hq-report-{tag}.sock"


def tokens_root() -> Path:
    from . import secrets_broker

    return secrets_broker.runtime_root() / "hq-report"


# ---- the capability store -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS caps(
    id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, session_key TEXT NOT NULL,
    phys_key TEXT NOT NULL, engine TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL,
    master TEXT);
CREATE INDEX IF NOT EXISTS caps_phys ON caps(phys_key, revoked_at);
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


# ---- the launch's private token directory -----------------------------------------------------


def _launch_dir(cap_id: str) -> Path:
    return tokens_root() / cap_id


def _prepare_launch_dir(cap_id: str) -> Path | None:
    """This launch's private directory on RAM-backed storage, or None (no capability then)."""
    from . import secrets_broker

    try:
        root = secrets_broker.runtime_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        secrets_broker._private_dir(root)
        secrets_broker._private_dir(tokens_root())
        if not secrets_broker._ram_check(tokens_root()):
            return None
        tfd = secrets_broker._open_nofollow_dir(tokens_root())
        try:
            os.close(secrets_broker._private_child(tfd, cap_id))
        finally:
            os.close(tfd)
        return _launch_dir(cap_id)
    except (secrets_broker.Refused, OSError):
        return None


def _write_token(launch_dir: Path, token: str) -> Path:
    from . import secrets_broker

    dfd = secrets_broker._open_nofollow_dir(launch_dir)
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


def _remove_launch(cap_id: str) -> bool:
    """Remove a launch's token and directory. True when no token file remains. A failure is
    logged, never taken for success: the master-environment fence (`_bind`) keeps a survivor
    off any other launch's master."""
    d = _launch_dir(cap_id)
    gone = True
    try:
        os.unlink(d / TOKEN_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        gone = False
        log.warning("hq report: could not remove the token of launch %s", cap_id, exc_info=True)
    with contextlib.suppress(OSError):
        os.rmdir(d)
    return gone


def _session_tag(phys_key: str) -> str:
    """A capability id starts with its session's tag, so a session's launch directories are
    found on the filesystem alone — no capability store needed (`_retire_session_launches`)."""
    return hashlib.sha256(phys_key.encode()).hexdigest()[:12] + "-"


def _retire_session_launches(phys_key: str) -> None:
    """THE GENERATION FENCE, independent of the capability store: remove every launch directory
    (and its token) of this session. A capability whose token file is gone is dead
    (`_token_current`), whatever its row says — so a relaunch retires its predecessors even when
    the store cannot be written (locked, failing). Filesystem only; never raises."""
    tag = _session_tag(phys_key)
    try:
        names = os.listdir(tokens_root())
    except FileNotFoundError:
        return  # no launch directory was ever made: nothing to retire
    except OSError:
        # NOT a success: the predecessors may keep their tokens. Logged, and the launch goes on —
        # the master-environment fence (`_bind`) still keeps any survivor off the replacement.
        log.warning("hq report: could not list launches to retire %s", phys_key, exc_info=True)
        return
    for name in names:
        if name.startswith(tag):
            _remove_launch(name)


#: The errors that prove a file or directory is NOT THERE — the only "absent" (#1882). Every
#: other `OSError` is UNKNOWN: it refuses the current request and changes nothing.
ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})


def _token_current(cap: sqlite3.Row, token: str) -> bool | None:
    """Does this capability's token file still exist in its private launch directory, holding
    exactly the presented token? Read without following a link. ``True``; ``False`` when it is
    ABSENT (``ENOENT``/``ENOTDIR``) or holds another token (MISMATCH) — the launch was retired;
    ``None`` for anything else (``EACCES``, ``EIO``, a directory that is not ours) — UNKNOWN,
    never a reason to revoke."""
    from . import secrets_broker

    try:
        dfd = secrets_broker._open_nofollow_dir(_launch_dir(cap["id"]))
    except OSError as e:
        return False if e.errno in ABSENT_ERRNOS else None
    except secrets_broker.Refused:
        return None  # there, but not a private directory of ours: unknown
    try:
        fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as e:
        return False if e.errno in ABSENT_ERRNOS else None
    finally:
        os.close(dfd)
    try:
        raw = os.read(fd, 1024)
    except OSError:
        return None
    finally:
        os.close(fd)
    return hmac.compare_digest(raw, token.encode("ascii", "replace"))


def _both(a: bool | None, b: bool | None) -> bool | None:
    """Tri-state AND: any ``False`` is False; otherwise any ``None`` is None."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


# ---- minting -----------------------------------------------------------------------------------

_server_running = False


def enabled() -> bool:
    """Settings → HQ → "Sessions can report to HQ" (off by default, #1882). Read at mint only: it
    governs NEW launches, never a running one."""
    from . import prefs

    try:
        return prefs.get_hq_reports()
    except Exception:  # noqa: BLE001 — an unreadable switch adds no tool
        return False


def launch_env(
    session_key: str, phys_key: str, *, engine: str, manifest, fenced: bool = False
) -> dict[str, str]:
    """The report capability for ONE new pty agent process, or ``{}``. Never raises: a launch
    never fails for the report tool. Minted only for an agent whose manifest declares how its CLI
    loads MCP tools (`tools.mcp`) — asked of the manifest, never of an engine name.

    Every earlier capability of this session ends FIRST, whatever the switch or the manifest
    says: a LAUNCH happens only when the session has no live master, so they belong to processes
    that are gone, and their reports end with them (`launch_ended`). ``fenced``: the caller holds
    `session_input.launch_fence` already (an unattended dispatch), so the revocation is ordered
    against PTY writes by it and must not take the fence a second time (`_revoke`).

    TWO ORDERS, on purpose. HERE the predecessors' token files go FIRST, then their rows: their
    processes are gone, so nobody legitimate loses a tool, and a token whose file is gone is
    refused (`_token_current`) even if the row's revocation then fails — a store error can never
    leave an old token to claim the replacement's master. Outside a relaunch (archive, reconcile)
    the capability may still be LIVE, so `_revoke` commits the row first and removes the token only
    after: a busy fence there changes nothing and the agent keeps its tool."""
    from . import agent_tools

    _retire_session_launches(phys_key)
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        log.warning("hq report: the capability store cannot be opened", exc_info=True)
        return {}
    try:
        try:
            _revoke_session(conn, phys_key, fenced=fenced)
        except Exception:  # noqa: BLE001 — their tokens are gone already: dead whatever the rows say
            log.warning("hq report: revoking %s's earlier launches failed", phys_key, exc_info=True)
        if agent_tools.mcp_kind(manifest) is None or not enabled() or not _server_running:
            return {}
        cap_id = _session_tag(phys_key) + secrets.token_hex(8)
        launch_dir = _prepare_launch_dir(cap_id)
        if launch_dir is None:
            return {}
        token = secrets.token_urlsafe(32)
        try:
            with _txn(conn):
                conn.execute(
                    "INSERT INTO caps(id, digest, session_key, phys_key, engine, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        cap_id,
                        template_secrets.keyed_digest(_CAPABILITY_LABEL, token),
                        session_key,
                        phys_key,
                        engine,
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
            _remove_launch(cap_id)  # never a token file without its row
            raise
        return {ENV_SOCKET: str(socket_path()), ENV_TOKEN_FILE: str(token_file)}
    except Exception:  # noqa: BLE001 — a launch never fails for the report tool
        log.warning("hq report: no capability for %s", session_key, exc_info=True)
        return {}
    finally:
        conn.close()


def scrub_process_environment() -> None:
    """The app's own environment never carries a launch's capability."""
    for key in ALL_ENV_KEYS:
        os.environ.pop(key, None)


# ---- liveness ----------------------------------------------------------------------------------

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def _master_identity(phys_key: str) -> tuple[str, Path | None]:
    """``(verdict-free identity, socket path)`` of the session's dtach socket, cheaply (lstat
    only, no probe): ``"<inode>:<mtime_ns>"``, ``"absent"``, ``"unresolvable"`` or
    ``"unreadable"``. Only a socket that is NOT THERE (``ENOENT``, or a missing parent:
    ``ENOTDIR``) is absent; any other failure (``EACCES``, ``EIO``, …) is unreadable — unknown,
    never "the launch ended", so a passing fault never revokes a live capability."""
    from . import ptybridge

    engine, _, native = phys_key.partition(":")
    if not native:
        return "unresolvable", None
    try:
        sock = ptybridge.socket_path(engine, native)
    except ptybridge.PtyBridgeError:
        return "unresolvable", None
    try:
        st = os.lstat(sock)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return "absent", sock
        return "unreadable", sock
    return f"{st.st_ino}:{st.st_mtime_ns}", sock


def _bind(conn: sqlite3.Connection, cap: sqlite3.Row, identity: str, carries: bool | None) -> str:
    """Bind an unbound capability to the master it first sees live — ONLY when that master's own
    environment carries THIS capability's id (``carries``, read from the kernel by
    `_master_carries`), and only while it is the session's NEWEST capability, never revoked.

    THE GENERATION FENCE (#1882, Hermes on #1884): a capability id is minted into the environment
    of its own launch, which its dtach master inherits; a replacement master carries its own id,
    or none — so an older launch's token can never claim it, however quickly the relaunch came and
    whatever the token directory and the capability store did at that relaunch. ``None`` (the
    environment could not be read, no master found) is UNKNOWN: refused, never bound. ``False`` —
    a master readable and NOT carrying this id, including an older build's launch that predates
    the variable — is DEAD: it can never be shown to be this capability's master, so it is
    revoked rather than retried for ever. A capability that loses reads DEAD."""
    if carries is None:
        return UNKNOWN
    if not carries:
        return DEAD
    with _txn(conn):
        bound = conn.execute(
            "UPDATE caps SET master = ? WHERE id = ? AND master IS NULL AND revoked_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM caps AS newer"
            " WHERE newer.phys_key = caps.phys_key AND newer.rowid > caps.rowid)",
            (identity, cap["id"]),
        ).rowcount
    if bound:
        return ALIVE
    row = conn.execute("SELECT master, revoked_at FROM caps WHERE id = ?", (cap["id"],)).fetchone()
    if row is None or row["revoked_at"] is not None or row["master"] is None:
        return DEAD  # revoked, or a newer launch exists: this master is not this launch's
    return ALIVE if row["master"] == identity else DEAD  # a concurrent first request bound it


def _liveness_impl(conn: sqlite3.Connection, cap: sqlite3.Row) -> str:
    """Is this LAUNCH's agent process running? The first live answer binds the capability to
    the master's socket identity (`_bind`: only the session's newest capability may); any later
    master on the same path reads as DEAD."""
    from . import ptybridge

    identity, sock = _master_identity(cap["phys_key"])
    if identity in ("unreadable", "unresolvable") or sock is None:
        return UNKNOWN  # keep everything; the next pass asks again
    if identity == "absent":
        return DEAD
    verdict = ptybridge.probe_master(sock)
    if verdict == ptybridge.UNKNOWN:
        return UNKNOWN
    if verdict == ptybridge.DEAD:
        return DEAD
    identity, _ = _master_identity(cap["phys_key"])
    if identity in ("absent", "unresolvable", "unreadable"):
        return UNKNOWN
    if cap["master"] is None:
        return _bind(conn, cap, identity, _master_carries(cap["phys_key"], cap["id"]))
    return ALIVE if cap["master"] == identity else DEAD


def _master_carries(phys_key: str, cap_id: str) -> bool | None:
    """Does the session's dtach master carry ``ENV_CAP=<cap_id>`` in its OWN environment, read
    from the kernel (``/proc/<pid>/environ``, bounded, never following a link)? ``None`` when there
    is no master to read or its environment cannot be read."""
    from . import reaper

    engine, _, native = phys_key.partition(":")
    try:
        pid = reaper._find_master_pid(engine, native)
    except Exception:  # noqa: BLE001 — unreadable is unknown, never a bind
        return None
    if pid is None:
        return None
    try:
        fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        raw = b""
        while len(raw) <= ENVIRON_MAX:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > ENVIRON_MAX:
        return None
    want = f"{ENV_CAP}={cap_id}".encode()
    return want in raw.split(b"\0")


def master_env(hq_env: dict[str, str]) -> dict[str, str]:
    """The variable a launch adds to its OWN environment (and so its dtach master's) for the
    capability `launch_env` minted: ``{ENV_CAP: <id>}``, or nothing without a capability. The id
    is the token file's launch directory."""
    token_file = hq_env.get(ENV_TOKEN_FILE) if hq_env else None
    if not token_file:
        return {}
    return {ENV_CAP: Path(token_file).parent.name}


#: The probe every caller uses (tests substitute a table; `_liveness_impl` stays real).
_liveness = _liveness_impl


def generation_current(phys_key: str, generation: str) -> bool:
    """Is ``generation`` still THIS session's launch? Its capability exists, is not revoked,
    and — once bound — its master's socket identity is unchanged. No probe (cheap enough for the
    actuator's in-fence fingerprint). Fails closed: unknown is not current."""
    return generation_state(phys_key, generation) is True


def generation_state(phys_key: str, generation: str) -> bool | None:
    """`generation_current`, tri-state: ``None`` when it cannot be told — the capability store
    cannot be read, or the master's socket cannot be (``"unreadable"``). An AUTHORIZATION asks
    `generation_current` (unknown refuses); a CLEANUP asks this and ends nothing on ``None``, so a
    passing read fault never retires a live launch's report."""
    if not generation:
        return False
    try:
        conn = _connect()
    except Exception:  # noqa: BLE001
        return None
    try:
        cap = conn.execute(
            "SELECT * FROM caps WHERE id = ? AND phys_key = ? AND revoked_at IS NULL",
            (generation, phys_key),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if cap is None:
        return False
    if cap["master"] is None:
        return True
    identity, _ = _master_identity(phys_key)
    if identity in ("unreadable", "unresolvable"):
        return None
    return identity == cap["master"]


# ---- a request ---------------------------------------------------------------------------------


def handle(token: object, op: object, report: object = None) -> dict:
    """One request from an agent: ``{"ok": True, "state": ...}`` or ``{"ok": False, "error":
    <code>}``. Never raises, never says more than a refusal code."""
    try:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise Refused("unknown_capability")
        if op != "report":
            raise Refused("bad_request")
        try:
            conn = _connect()
        except sqlite3.Error:
            raise Refused("store_unavailable") from None
        try:
            return _handle(conn, token, report)
        finally:
            conn.close()
    except Refused as r:
        return {"ok": False, "error": r.code}
    except agent_reports.Refused as r:
        return {"ok": False, "error": r.code}
    except Exception:  # noqa: BLE001 — an agent never sees a traceback
        log.warning("hq report broker: a request failed", exc_info=True)
        return {"ok": False, "error": "store_unavailable"}


def _handle(conn: sqlite3.Connection, token: str, report: object) -> dict:
    try:
        digest = template_secrets.keyed_digest(_CAPABILITY_LABEL, token)
    except template_secrets.SecretKeyUnavailable:
        raise Refused("store_unavailable") from None
    cap = conn.execute(
        "SELECT * FROM caps WHERE digest = ? AND revoked_at IS NULL", (digest,)
    ).fetchone()
    if cap is None:
        raise Refused("unknown_capability")
    held = _token_current(cap, token)
    if held is None:
        # The token file could not be READ (not shown absent): unknown — refused, nothing changed.
        raise Refused("launch_unconfirmed")
    if not held:
        # Its token file is gone (a relaunch retired it) or no longer this token: dead, before
        # any bind — whatever the row says. Revoked when the store allows; refused regardless.
        with contextlib.suppress(Exception):
            _revoke(conn, cap)
        raise Refused("launch_ended")
    state = _liveness(conn, cap)
    if state == DEAD:
        _revoke(conn, cap)
        raise Refused("launch_ended")
    if state == UNKNOWN:
        raise Refused("launch_unconfirmed")
    # THE LAUNCH'S identity, from the capability row — never anything the agent sent. Asked
    # again UNDER the filing fence: a revocation commits under it (`_revoke`), so one that landed
    # after the check above refuses instead of filing for a launch that has ended.
    stored = agent_reports.file(
        report,
        session_key=cap["session_key"],
        phys_key=cap["phys_key"],
        engine=cap["engine"],
        generation=cap["id"],
        still_current=lambda: _both(
            _token_current(cap, token), generation_state(cap["phys_key"], cap["id"])
        ),
    )
    return {"ok": True, "state": stored["state"]}


# ---- revocation --------------------------------------------------------------------------------


def _revoke(conn: sqlite3.Connection, cap: sqlite3.Row, *, fenced: bool = False) -> None:
    """End one capability and its report. The revocation moves `generation_current`, which a
    report answer's in-fence fingerprint reads, so it commits under the write fence like every
    report mutation (`agent_reports._fenced`); ``fenced``: the caller already holds it. A busy
    fence raises `session_input.AuthorityFenceBusy` with NOTHING changed — the row still active
    and the token still on disk, so the live agent keeps its tool and the next pass retries. The
    token goes only after the revocation committed.

    An UNBOUND capability (``master IS NULL``) is revoked WITHOUT the fence. That is safe because
    filing needs the liveness bind first (`_liveness_impl` → `_bind`), so an unbound capability
    has never filed a report, and no answer's fingerprint can read its generation: nothing a PTY
    write is ordered against changes. The UPDATE is conditional on ``master IS NULL`` — one that
    was bound in the meantime falls through to the fenced path. So a busy fence at a relaunch can
    no longer leave an old, never-bound token active to claim the replacement's master."""
    from . import session_input

    if cap["master"] is None:
        with _txn(conn):
            done = conn.execute(
                "UPDATE caps SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
                " AND master IS NULL",
                (time.time(), cap["id"]),
            ).rowcount
        row = conn.execute("SELECT * FROM caps WHERE id = ?", (cap["id"],)).fetchone()
        if done or row is None or row["revoked_at"] is not None:
            _remove_launch(cap["id"])
            # No report can carry an unbound generation; ended best-effort all the same.
            agent_reports.end_quietly(
                cap["phys_key"], "launch_ended", generation=cap["id"], fenced=fenced
            )
            return
        cap = row  # bound since it was read: the fenced revocation below
    with contextlib.nullcontext() if fenced else session_input.mutation_fence():
        with _txn(conn):
            conn.execute(
                "UPDATE caps SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (time.time(), cap["id"]),
            )
    _remove_launch(cap["id"])
    agent_reports.end_quietly(cap["phys_key"], "launch_ended", generation=cap["id"], fenced=fenced)


def _revoke_session(conn: sqlite3.Connection, phys_key: str, *, fenced: bool = False) -> None:
    """Every live capability of the session. Unbound ones first — they never need the fence —
    so a busy fence can stop only a BOUND one, whose master identity no replacement shares."""
    caps = conn.execute(
        "SELECT * FROM caps WHERE phys_key = ? AND revoked_at IS NULL", (phys_key,)
    ).fetchall()
    for cap in sorted(caps, key=lambda c: c["master"] is not None):
        _revoke(conn, cap, fenced=fenced)


def revoke_launch(phys_key: str) -> None:
    """The session's runtime is being torn down (archive): its capabilities end now. Blocking."""
    try:
        conn = _connect()
        try:
            _revoke_session(conn, phys_key)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — teardown never blocks on this; the pass retries
        log.warning("hq report broker: revoking %s failed", phys_key, exc_info=True)


def reconcile_reports() -> None:
    """The needs-you sync's pass (#1882): probe only the launches that hold a current report, so
    a report whose agent exited ends at the next sync. A report whose own answer was DELIVERED
    ends here too, bound to its id — the self-heal for a settlement whose end failed (a busy
    fence, an unreadable store). Raises nothing."""
    try:
        reporting = set(agent_reports.current_map())
        if not reporting:
            return
        _end_delivered_answers()
        conn = _connect()
        try:
            for cap in conn.execute("SELECT * FROM caps WHERE revoked_at IS NULL").fetchall():
                if cap["phys_key"] not in reporting:
                    continue
                try:
                    state = _liveness(conn, cap)
                except Exception:  # noqa: BLE001 — unknowable this pass
                    state = UNKNOWN
                if state == DEAD:
                    _revoke(conn, cap)
        finally:
            conn.close()
        for phys, rep in agent_reports.current_map().items():
            gen = str(rep.get("generation") or "")
            if generation_state(phys, gen) is False:  # unknown ends nothing
                agent_reports.end_quietly(phys, "launch_ended", generation=gen)
    except Exception:  # noqa: BLE001
        log.warning("hq report broker: reconciling reports failed", exc_info=True)


def _end_delivered_answers() -> None:
    """End every current report that was ANSWERED: by its own delivered option, or by an
    ordinary answer/relay delivered to the session after it was filed — each bound to its id. An
    unreadable ledger ends nothing (the next pass retries)."""
    from . import orchestrator_ledger

    try:
        answered = orchestrator_ledger.delivered_report_ids()
    except orchestrator_ledger.LedgerUnreadable:
        return  # unknown ends nothing
    for phys, rep in agent_reports.current_map().items():
        try:
            done = rep["id"] in answered or orchestrator_ledger.answered_after(
                phys, float(rep["at"])
            )
        except orchestrator_ledger.LedgerUnreadable:
            return
        if done:
            agent_reports.end_quietly(phys, "answered", report_id=rep["id"])


def reconcile(*, startup: bool = False) -> None:
    """Probe every live capability: a DEAD launch (bound, or past its grace, or at startup) is
    revoked and its report ends. Then any current report whose launch is no longer current ends
    too (an orphan). An UNKNOWN probe keeps everything. Raises nothing."""
    try:
        conn = _connect()
        try:
            for cap in conn.execute("SELECT * FROM caps WHERE revoked_at IS NULL").fetchall():
                try:
                    state = _liveness(conn, cap)
                except Exception:  # noqa: BLE001 — unknowable this pass
                    state = UNKNOWN
                if state == DEAD and (
                    startup
                    or cap["master"] is not None
                    or time.time() - cap["created_at"] > LAUNCH_GRACE_S
                ):
                    _revoke(conn, cap)
        finally:
            conn.close()
        for phys, rep in agent_reports.current_map().items():
            gen = str(rep.get("generation") or "")
            if generation_state(phys, gen) is False:  # unknown ends nothing
                agent_reports.end_quietly(phys, "launch_ended", generation=gen)
    except Exception:  # noqa: BLE001 — the next pass retries
        log.warning("hq report broker: reconcile failed", exc_info=True)
    # Ended reports past the history bound go from idle sessions too: agent text on disk never
    # outlives the bound because nobody wrote to that session since (#1882).
    try:
        agent_reports.sweep()
    except Exception:  # noqa: BLE001 — the next pass retries
        log.warning("hq report broker: sweeping expired reports failed", exc_info=True)


# ---- the socket --------------------------------------------------------------------------------


async def _client(reader, writer) -> None:
    import asyncio

    from . import secrets_broker

    try:
        if secrets_broker._peer_uid(writer) != os.geteuid():
            return
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            over = len(line) > REQUEST_MAX
        except TimeoutError:
            return
        except ValueError:  # past the stream limit: too long, and said so
            line, over = b"", True
        if not line and not over:
            return
        if over:
            reply = {"ok": False, "error": "too_long"}
        else:
            try:
                req = json.loads(line)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                reply = {"ok": False, "error": "bad_request"}
            else:
                reply = await asyncio.to_thread(
                    handle, req.get("token"), req.get("op"), req.get("report")
                )
        with contextlib.suppress(ConnectionError):
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def serve() -> None:
    """Serve the broker until cancelled: a startup reconcile, the socket, a periodic pass. A
    socket another process still answers on is never taken over; failing to bind leaves the tool
    unavailable (launches get no capability)."""
    import asyncio

    from . import secrets_broker

    global _server_running
    await asyncio.to_thread(reconcile, startup=True)
    path = socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if await asyncio.to_thread(secrets_broker._answers, path):
                log.warning("hq report broker: %s is served by another process", path)
                return
            path.unlink()
        server = await asyncio.start_unix_server(
            _client, sock=secrets_broker._bound_private(path), limit=REQUEST_MAX + 2
        )
        ours = os.lstat(path).st_ino
    except OSError:
        log.warning("hq report broker: cannot serve; agents get no report tool", exc_info=True)
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
                path.unlink()
