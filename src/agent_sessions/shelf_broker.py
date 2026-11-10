"""The shelf broker (#1740 P1a): how a mission's agent puts an item on the mission's shelf.

    agent ──(battlelab_shelf MCP)──▶ shelf-<tag>.sock ──▶ handle() ──▶ mission_shelf.deliver()

**A capability is one launch of one mission session, and it can only add.** It is minted where a
new pty agent process is born for a session that belongs to a mission — an interactive terminal
LAUNCH of a mission's session (`routes/terminal.py`) and a mission dispatch or spawn
(`headless_dispatch.py`, told the mission by `mission_dispatch.run`) — and never on an attach. It is
stored as a keyed digest (`template_secrets.keyed_digest`, its own label) in the missions store
(`mission_shelf_caps`, cascading with the mission) beside the launch's identity: the mission, the
session key and the physical key. The token itself is a 0600 FILE in the launch's private
RAM-backed directory; the environment and the MCP payload (argv) carry only its PATH.

**The agent never names a mission or a session**: the broker maps the token to the launch, and
`mission_shelf.deliver` re-checks, inside its write transaction, that the session is on that
mission's roster now. A session detached from the mission, a mission archived, or a capability
retired by a relaunch of the same session can add nothing.

**Separate from the secrets and HQ brokers on every axis**: its own table, socket, digest label
and verb (``add``). Nothing it accepts can act: an item is something the operator reviews.
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
from pathlib import Path

from . import mission_shelf, missions, template_secrets

log = logging.getLogger(__name__)

ENV_SOCKET = "BATTLELAB_SHELF_SOCKET"
#: The PATH of this launch's token file — never the token (it rides argv in an MCP payload).
ENV_TOKEN_FILE = "BATTLELAB_SHELF_TOKEN_FILE"  # noqa: S105 — a variable NAME
ENV_KEYS = frozenset({ENV_SOCKET, ENV_TOKEN_FILE})
ALL_ENV_KEYS = ENV_KEYS
TOKEN_FILE = ".token"  # noqa: S105 — a file NAME
#: One request line, in bytes: the largest valid item is 64 KB of text, which JSON may escape up
#: to six bytes per character for control characters — every valid item fits.
REQUEST_MAX = 512 * 1024
_CAPABILITY_LABEL = "shelf-capability"

#: Refusal codes the broker itself adds to the shelf's own.
REFUSALS = frozenset(mission_shelf.REFUSALS)


class Refused(Exception):
    def __init__(self, code: str) -> None:
        assert code in REFUSALS
        super().__init__(code)
        self.code = code


# ---- paths ----------------------------------------------------------------------------------


def socket_path() -> Path:
    """In THIS instance's runtime directory, named after its missions store (so a test or staging
    instance on the same host never binds or removes another instance's socket)."""
    from . import ptybridge

    tag = hashlib.sha256(str(missions._db_path().resolve()).encode()).hexdigest()[:10]
    return ptybridge.runtime_dir() / f"shelf-{tag}.sock"


def tokens_root() -> Path:
    from . import secrets_broker

    return secrets_broker.runtime_root() / "shelf"


def _launch_dir(cap_id: str) -> Path:
    return tokens_root() / cap_id


def _session_tag(phys_key: str) -> str:
    """A capability id starts with its session's tag, so a relaunch finds its predecessors'
    directories on the filesystem alone (`_retire_session_launches`)."""
    return hashlib.sha256(phys_key.encode()).hexdigest()[:12] + "-"


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


def _remove_launch(cap_id: str) -> None:
    d = _launch_dir(cap_id)
    try:
        os.unlink(d / TOKEN_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("shelf: could not remove the token of launch %s", cap_id, exc_info=True)
    with contextlib.suppress(OSError):
        os.rmdir(d)


def _retire_session_launches(phys_key: str) -> None:
    """Remove every launch directory (and token) of this session: a capability whose token file
    is gone is dead (`_token_current`), whatever its row says. Filesystem only; never raises."""
    tag = _session_tag(phys_key)
    try:
        names = os.listdir(tokens_root())
    except OSError:
        return
    for name in names:
        if name.startswith(tag):
            _remove_launch(name)


_ABSENT = frozenset({errno.ENOENT, errno.ENOTDIR})


def _token_current(cap_id: str, token: str) -> bool | None:
    """Does the capability's token file still exist, holding exactly this token? ``False`` when
    it is absent or holds another (retired); ``None`` when it cannot be read (unknown)."""
    from . import secrets_broker

    try:
        dfd = secrets_broker._open_nofollow_dir(_launch_dir(cap_id))
    except OSError as e:
        return False if e.errno in _ABSENT else None
    except secrets_broker.Refused:
        return None
    try:
        fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as e:
        return False if e.errno in _ABSENT else None
    finally:
        os.close(dfd)
    try:
        raw = os.read(fd, 1024)
    except OSError:
        return None
    finally:
        os.close(fd)
    return hmac.compare_digest(raw, token.encode("ascii", "replace"))


# ---- minting -----------------------------------------------------------------------------------

_server_running = False


def set_running_for_test(value: bool) -> None:
    global _server_running
    _server_running = value


def launch_env(
    session_key: str,
    phys_key: str,
    *,
    engine: str,
    manifest,
    mission_id: str | None = None,
) -> dict[str, str]:
    """The shelf capability for ONE new pty agent process, or ``{}``. Never raises: a launch
    never fails for the shelf tool.

    ``mission_id``: the mission a dispatch or spawn launches for (the session is adopted only once
    it runs). Without one the session's CURRENT mission is asked — an interactive relaunch of a
    session a mission holds. No mission, no capability: the tool is handed only to mission
    sessions. Minted only for an agent whose manifest declares an MCP channel (`tools.mcp`).

    Every earlier capability of this session ends FIRST, whatever else this decides: a LAUNCH
    happens only when the session has no live master, so they belong to processes that are gone."""
    from . import agent_tools

    _retire_session_launches(phys_key)
    try:
        with missions._write_lock:
            con = missions._ready()
            try:
                con.execute(
                    "UPDATE mission_shelf_caps SET revoked_at=? "
                    "WHERE phys_key=? AND revoked_at IS NULL",
                    (time.time(), phys_key),
                )
            finally:
                con.close()
    except Exception:  # noqa: BLE001 — their tokens are gone already: dead whatever the rows say
        log.warning("shelf: revoking %s's earlier launches failed", phys_key, exc_info=True)
    try:
        if agent_tools.mcp_kind(manifest) is None or not _server_running:
            return {}
        mid = mission_id or missions.session_mission(session_key)
        if mid is None and phys_key != session_key:
            mid = missions.session_mission(phys_key)
        if not mid:
            return {}
        cap_id = _session_tag(phys_key) + secrets.token_hex(8)
        launch_dir = _prepare_launch_dir(cap_id)
        if launch_dir is None:
            return {}
        token = secrets.token_urlsafe(32)
        try:
            with missions._write_lock:
                con = missions._ready()
                try:
                    con.execute(
                        "INSERT INTO mission_shelf_caps (id, digest, mission_id, session_key,"
                        " phys_key, engine, created_at) VALUES (?,?,?,?,?,?,?)",
                        (
                            cap_id,
                            template_secrets.keyed_digest(_CAPABILITY_LABEL, token),
                            mid,
                            session_key,
                            phys_key,
                            engine,
                            time.time(),
                        ),
                    )
                finally:
                    con.close()
            try:
                token_file = _write_token(launch_dir, token)
            except BaseException:
                with missions._write_lock:
                    con = missions._ready()
                    try:
                        con.execute("DELETE FROM mission_shelf_caps WHERE id=?", (cap_id,))
                    finally:
                        con.close()
                raise
        except BaseException:
            _remove_launch(cap_id)  # never a token file without its row
            raise
        return {ENV_SOCKET: str(socket_path()), ENV_TOKEN_FILE: str(token_file)}
    except Exception:  # noqa: BLE001 — a launch never fails for the shelf tool
        log.warning("shelf: no capability for %s", session_key, exc_info=True)
        return {}


def scrub_process_environment() -> None:
    """The app's own environment never carries a launch's capability."""
    for key in ALL_ENV_KEYS:
        os.environ.pop(key, None)


# ---- a request ---------------------------------------------------------------------------------


def handle(token: object, op: object, item: object = None) -> dict:
    """One request from an agent: ``{"ok": True, "id", "kind"}`` or ``{"ok": False, "error":
    <code>}``. Never raises, never says more than a refusal code."""
    try:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise Refused("unknown_capability")
        if op != "add":
            raise Refused("bad_request")
        try:
            digest = template_secrets.keyed_digest(_CAPABILITY_LABEL, token)
        except template_secrets.SecretKeyUnavailable:
            raise Refused("store_unavailable") from None
        try:
            con = missions._ready()
            try:
                cap = con.execute(
                    "SELECT * FROM mission_shelf_caps WHERE digest=? AND revoked_at IS NULL",
                    (digest,),
                ).fetchone()
            finally:
                con.close()
        except (sqlite3.Error, missions.MissionError):
            raise Refused("store_unavailable") from None
        if cap is None:
            raise Refused("unknown_capability")
        held = _token_current(cap["id"], token)
        if held is None:
            raise Refused("store_unavailable")
        if not held:
            raise Refused("unknown_capability")
        out = mission_shelf.deliver(
            cap["mission_id"],
            item,
            session_key=cap["session_key"],
            phys_key=cap["phys_key"],
        )
        return {"ok": True, "id": out["id"], "kind": out["kind"]}
    except Refused as r:
        return {"ok": False, "error": r.code}
    except mission_shelf.ShelfRefused as r:
        return {"ok": False, "error": r.code}
    except Exception:  # noqa: BLE001 — an agent never sees a traceback
        log.warning("shelf broker: a request failed", exc_info=True)
        return {"ok": False, "error": "store_unavailable"}


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
        except ValueError:  # past the stream limit
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
                    handle, req.get("token"), req.get("op"), req.get("item")
                )
        with contextlib.suppress(ConnectionError):
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def serve() -> None:
    """Serve the broker until cancelled. A socket another process still answers on is never taken
    over; failing to bind leaves the tool unavailable (launches get no capability)."""
    import asyncio

    from . import secrets_broker

    global _server_running
    path = socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if await asyncio.to_thread(secrets_broker._answers, path):
                log.warning("shelf broker: %s is served by another process", path)
                return
            path.unlink()
        server = await asyncio.start_unix_server(
            _client, sock=secrets_broker._bound_private(path), limit=REQUEST_MAX + 2
        )
        ours = os.lstat(path).st_ino
    except OSError:
        log.warning("shelf broker: cannot serve; agents get no shelf tool", exc_info=True)
        return
    _server_running = True
    try:
        async with server:
            await asyncio.Event().wait()
    finally:
        _server_running = False
        with contextlib.suppress(OSError):
            if os.lstat(path).st_ino == ours:
                path.unlink()


def _revoke_ids(ids: list[str]) -> None:
    """End these capabilities: rows revoked, then their token files gone."""
    if not ids:
        return
    with missions._write_lock:
        con = missions._ready()
        try:
            con.executemany(
                "UPDATE mission_shelf_caps SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                [(time.time(), i) for i in ids],
            )
        finally:
            con.close()
    for cap_id in ids:
        _remove_launch(cap_id)


def revoke_session(mission_id: str, session_key: str) -> None:
    """The session left the mission (detach): its shelf capabilities end now. Membership already
    refuses it; this ends the capability itself (#1740 P1b). Never raises."""
    try:
        con = missions._ready()
        try:
            ids = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM mission_shelf_caps WHERE revoked_at IS NULL AND mission_id=?"
                    " AND (session_key=? OR phys_key=? OR phys_key IN (SELECT physical_key FROM"
                    " session_runtime_bindings WHERE logical_key=?))",
                    (mission_id, session_key, session_key, session_key),
                ).fetchall()
            ]
        finally:
            con.close()
        _revoke_ids(ids)
    except Exception:  # noqa: BLE001 — a detach never fails for this
        log.warning("shelf: revoking a detached session's capabilities failed", exc_info=True)


def revoke_launch(phys_key: str) -> None:
    """The session's runtime is torn down (archive): its shelf capabilities end now."""
    try:
        con = missions._ready()
        try:
            ids = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM mission_shelf_caps WHERE revoked_at IS NULL AND phys_key=?",
                    (phys_key,),
                ).fetchall()
            ]
        finally:
            con.close()
        _revoke_ids(ids)
    except Exception:  # noqa: BLE001 — teardown never fails for this
        log.warning("shelf: revoking %s's capabilities failed", phys_key, exc_info=True)
