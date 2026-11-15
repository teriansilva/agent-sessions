"""Launch-scoped, same-user Memory reads over a private Unix socket (#2036).

Only this server constructs worker principals. The client sends a private file's random token,
never a project id, account home, pathname or operator flag. Old inboxes remain compatibility
producers. Capabilities survive an app restart but not their launch, removed notebook or replaced
directory. Turning capture/access off only affects new launches, as the file contract does.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import stat
import struct
import time
from pathlib import Path

from . import engines, memory
from . import memory_service as service

log = logging.getLogger(__name__)
REQUEST_MAX = 8192
RESPONSE_MAX = 48 * 1024


def root() -> Path:
    return memory.root() / ".channels"


def socket_path() -> Path:
    from . import ptybridge

    tag = hashlib.sha256(str(memory.root().resolve()).encode()).hexdigest()[:12]
    return ptybridge.runtime_dir() / f"memory-{tag}.sock"


def _connect():
    memory._ensure_root()
    with memory._dirfd(memory.root()) as fd:
        os.close(memory._open_dir(".channels", fd, create=True))
    path = root() / "state.db"
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS caps(id TEXT PRIMARY KEY,digest TEXT NOT NULL,"
        "phys_key TEXT NOT NULL,worker_id TEXT,kind TEXT NOT NULL,bindings TEXT NOT NULL,"
        "created REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS caps_token ON caps(digest) WHERE revoked=0")
    conn.execute("CREATE INDEX IF NOT EXISTS caps_launch ON caps(phys_key)")
    return conn


def _identity(path: str) -> list[int]:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise OSError("foreign directory")
        return [st.st_dev, st.st_ino]
    finally:
        os.close(fd)


def bind(session_key: str, env: dict, *, kind: str, worker_id: str | None = None) -> None:
    """Called once for a new launch, after its inboxes exist. No new env/argv secret."""
    inbox = Path(env[memory.ENV_INBOX])
    cap_id = inbox.name
    scopes = [inbox.parent.parent.name]
    paths = [env[memory.ENV_READ], str(inbox)]
    if memory.ENV_SPECIALIST_READ in env:
        scopes.append(Path(env[memory.ENV_SPECIALIST_INBOX]).parent.parent.name)
        paths.extend([env[memory.ENV_SPECIALIST_READ], env[memory.ENV_SPECIALIST_INBOX]])
    service.worker(scopes)
    bindings = {"scopes": scopes, "paths": paths, "identities": [_identity(p) for p in paths]}
    phys_key = engines.physical_key(session_key)
    token = secrets.token_hex(32)
    with contextlib.closing(_connect()) as conn, memory._txn(conn):
        conn.execute("UPDATE caps SET revoked=1 WHERE phys_key=?", (phys_key,))
        conn.execute(
            "INSERT INTO caps VALUES(?,?,?,?,?,?,?,0)",
            (
                cap_id,
                hashlib.sha256(token.encode()).hexdigest(),
                phys_key,
                worker_id,
                kind,
                json.dumps(bindings),
                time.time(),
            ),
        )
        with memory._dirfd(root()) as fd:
            memory._write_at(
                fd, cap_id + ".json", json.dumps({"socket": str(socket_path()), "token": token})
            )


def revoke_launch(phys_key: str) -> None:
    if not (root() / "state.db").is_file():
        return
    try:
        with contextlib.closing(_connect()) as conn:
            conn.execute("UPDATE caps SET revoked=1 WHERE phys_key=?", (phys_key,))
    except (sqlite3.Error, OSError, memory.MemoryStoreError):
        log.warning("memory capability revocation could not be persisted", exc_info=True)


def _live(cap, bindings: dict) -> bool:
    if cap["kind"] == "api":
        from . import native_state, secrets_broker

        if not cap["worker_id"]:
            return False
        config = native_state.read_config(cap["worker_id"])
        env = config.get("memory_env", {})
        return (
            env.get(memory.ENV_INBOX) == bindings["paths"][1]
            and secrets_broker._api_liveness(cap["worker_id"]) == secrets_broker.ALIVE
        )
    from . import ptybridge, reaper

    engine, _, native = cap["phys_key"].partition(":")
    sock = ptybridge.socket_path(engine, native)
    if ptybridge.probe_master(sock) != ptybridge.ALIVE:
        return False
    pid = reaper._find_master_pid(engine, native)
    if pid is None:
        return False
    fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    return len(raw) <= 1024 * 1024 and (
        memory.ENV_INBOX + "=" + bindings["paths"][1]
    ).encode() in raw.split(b"\0")


def handle(req: object) -> dict:
    try:
        if not isinstance(req, dict) or set(req) - {"token", "op", "query", "slug", "notebook"}:
            raise memory.MemoryStoreError("invalid request", 422)
        token = req.get("token")
        if not isinstance(token, str) or len(token) != 64:
            raise memory.MemoryStoreError("memory capability unavailable", 403)
        with contextlib.closing(_connect()) as conn:
            cap = conn.execute(
                "SELECT * FROM caps WHERE digest=? AND revoked=0",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if cap is None:
                raise memory.MemoryStoreError("memory capability ended", 403)
            bindings = json.loads(cap["bindings"])
            if any(
                _identity(p) != i
                for p, i in zip(bindings["paths"], bindings["identities"], strict=True)
            ):
                raise memory.MemoryStoreError("memory directory changed; restart the agent", 403)
            principal = service.worker(bindings["scopes"])
            for scope in principal.scopes:
                service.authorize(principal, scope)
            if not _live(cap, bindings):
                raise memory.MemoryStoreError("memory launch is not live", 403)
            if req.get("op") == "search":
                result = service.search(principal, req.get("query"), limit=10)
            elif req.get("op") == "read":
                book = req.get("notebook", "project")
                if book not in {"project", "mine"} or (
                    book == "mine" and len(principal.scopes) != 2
                ):
                    raise memory.MemoryStoreError("not an offered notebook", 403)
                result = service.read(
                    principal, principal.scopes[int(book == "mine")], req.get("slug")
                )
            else:
                raise memory.MemoryStoreError("unknown operation", 422)
        response = {"ok": True, "result": result}
        if len(json.dumps(response, ensure_ascii=False).encode()) + 1 > RESPONSE_MAX:
            raise memory.MemoryStoreError("memory result too large", 422)
        return response
    except memory.MemoryStoreError as exc:
        return {"ok": False, "error": str(exc), "status": exc.status}
    except Exception:  # noqa: BLE001 — no store/path/credential detail crosses the socket
        log.warning("memory broker request failed", exc_info=True)
        return {"ok": False, "error": "memory unavailable", "status": 503}


async def _client(reader, writer) -> None:
    try:
        peer = writer.get_extra_info("socket")
        if (
            peer is None
            or struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            != os.getuid()
        ):
            return
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            if len(raw) > REQUEST_MAX or not raw.endswith(b"\n"):
                return
            req = json.loads(raw)
        except (ValueError, TimeoutError):
            return
        reply = await asyncio.to_thread(handle, req)
        writer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def serve() -> None:
    """A kernel owner lock prevents a second app from replacing a live socket."""

    def initialize():
        with contextlib.closing(_connect()):
            pass

    await asyncio.to_thread(initialize)
    lock = os.open(
        root() / "broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    sock = None
    path = socket_path()
    ours = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise OSError("unexpected memory socket path")
            path.unlink()  # the owner lock proves the previous broker is gone
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        os.chmod(path, 0o600)
        ours = path.lstat().st_ino
        server = await asyncio.start_unix_server(_client, sock=sock, limit=REQUEST_MAX + 2)
        async with server:
            await server.serve_forever()
    except OSError:
        log.warning("memory broker unavailable", exc_info=True)
    finally:
        if sock is not None:
            sock.close()
        with contextlib.suppress(OSError):
            if ours is not None and path.lstat().st_ino == ours:
                path.unlink()
        os.close(lock)
