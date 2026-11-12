"""The artifacts broker (#1932 P1): how an agent publishes an artifact.

    agent ──(battlelab_artifacts MCP)──▶ artifacts-<tag>.sock ──▶ handle() ──▶ artifact_store

**A capability is one launch of one session whose artifacts are turned on.** It is minted where a
new agent process is born — a terminal LAUNCH (`routes/terminal.py`), a mission dispatch or spawn
(`headless_dispatch.py`) and an API-runtime generation (`native_runtime.py`) — and never on an
attach, and only when `artifact_store.effective_mode` says ``private`` or ``public`` for that
session at
that moment. It is stored as a keyed digest (`template_secrets.keyed_digest`, its own label) in the
artifacts store (`artifact_caps`) beside the launch's identity: engine, session key, physical key
and cwd. The token itself is a 0600 FILE in the launch's private RAM-backed directory; the
environment and the MCP payload carry only its PATH.

**The agent never names a session**: the broker maps the token to the launch. A relaunch of the
same session retires every earlier capability (token files first, then rows); archiving the
session's runtime ends them too (`revoke_launch`, from `runtime_cleanup`).

**Separate from the shelf, secrets and HQ brokers on every axis**: its own store, table, socket,
digest label and verbs (``publish``, ``list``). Nothing it accepts can act on the box: an artifact
is a copy the operator opens.
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
import time
from pathlib import Path

from . import artifact_store, template_secrets

log = logging.getLogger(__name__)

ENV_SOCKET = "BATTLELAB_ARTIFACTS_SOCKET"
ENV_TOKEN_FILE = "BATTLELAB_ARTIFACTS_TOKEN_FILE"  # noqa: S105 — a variable NAME
ENV_KEYS = frozenset({ENV_SOCKET, ENV_TOKEN_FILE})
ALL_ENV_KEYS = ENV_KEYS
TOKEN_FILE = ".token"  # noqa: S105 — a file NAME
REQUEST_MAX = 16 * 1024
_CAPABILITY_LABEL = "artifact-capability"
OPS = frozenset({"publish", "list", "share"})


class Refused(Exception):
    def __init__(self, code: str) -> None:
        assert code in artifact_store.REFUSALS
        super().__init__(code)
        self.code = code


# ---- paths ----------------------------------------------------------------------------------


def socket_path() -> Path:
    """In THIS instance's runtime directory, named after its artifacts store (so a test or staging
    instance on the same host never binds or removes another instance's socket)."""
    from . import ptybridge

    tag = hashlib.sha256(str(artifact_store.db_path().resolve()).encode()).hexdigest()[:10]
    return ptybridge.runtime_dir() / f"artifacts-{tag}.sock"


def tokens_root() -> Path:
    from . import secrets_broker

    return secrets_broker.runtime_root() / "artifacts"


def _launch_dir(cap_id: str) -> Path:
    return tokens_root() / cap_id


def _session_tag(phys_key: str) -> str:
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
        log.warning("artifacts: could not remove the token of launch %s", cap_id, exc_info=True)
    with contextlib.suppress(OSError):
        os.rmdir(d)


def _retire_session_launches(phys_key: str) -> None:
    """Remove every launch directory (and token) of this session. Filesystem only; never raises."""
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
    """Does the capability's token file still hold exactly this token? ``False`` when absent or
    another (retired); ``None`` when it cannot be read (unknown)."""
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


def _revoke_rows(where: str, params: tuple) -> list[str]:
    with artifact_store._tx() as con:
        ids = [
            r["id"]
            for r in con.execute(
                f"SELECT id FROM artifact_caps WHERE revoked_at IS NULL AND {where}",  # noqa: S608
                params,
            ).fetchall()
        ]
        con.executemany(
            "UPDATE artifact_caps SET revoked_at=? WHERE id=?", [(time.time(), i) for i in ids]
        )
    return ids


def launch_env(
    session_key: str,
    phys_key: str,
    cwd: str,
    *,
    engine: str,
    manifest,
    mission_id: str | None = None,
) -> dict[str, str]:
    """The artifacts capability for ONE new agent process, or ``{}``. Never raises: a launch never
    fails for the artifacts tool.

    Minted only when the session's effective mode is ``private`` or ``public`` now
    (`artifact_store.effective_mode`) and its agent declares an MCP channel (`tools.mcp`). Every
    earlier
    capability of this session ends FIRST, whatever else this decides: a new agent process means
    the earlier ones are gone."""
    from . import agent_tools

    _retire_session_launches(phys_key)
    try:
        if not artifact_store._absent():  # no store yet: no capability to end, none created
            _revoke_rows("phys_key=?", (phys_key,))
    except Exception:  # noqa: BLE001 — their tokens are gone already: dead whatever the rows say
        log.warning("artifacts: revoking %s's earlier launches failed", phys_key, exc_info=True)
    try:
        if agent_tools.mcp_kind(manifest) is None or not _server_running:
            return {}
        if not artifact_store.enabled():
            return {}  # never touches (or creates) the store with the feature off
        eff = artifact_store.effective_mode([session_key, phys_key], cwd=cwd, mission_id=mission_id)
        if eff.mode not in ("private", "public"):
            return {}
        cap_id = _session_tag(phys_key) + secrets.token_hex(8)
        launch_dir = _prepare_launch_dir(cap_id)
        if launch_dir is None:
            return {}
        token = secrets.token_urlsafe(32)
        try:
            with artifact_store._tx() as con:
                con.execute(
                    "INSERT INTO artifact_caps (id, digest, engine, session_key, phys_key, cwd,"
                    " mode, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        cap_id,
                        template_secrets.keyed_digest(_CAPABILITY_LABEL, token),
                        engine,
                        session_key,
                        phys_key,
                        cwd,
                        eff.mode,
                        time.time(),
                    ),
                )
            try:
                token_file = _write_token(launch_dir, token)
            except BaseException:
                with artifact_store._tx() as con:
                    con.execute("DELETE FROM artifact_caps WHERE id=?", (cap_id,))
                raise
        except BaseException:
            _remove_launch(cap_id)  # never a token file without its row
            raise
        return {ENV_SOCKET: str(socket_path()), ENV_TOKEN_FILE: str(token_file)}
    except Exception:  # noqa: BLE001 — a launch never fails for the artifacts tool
        log.warning("artifacts: no capability for %s", session_key, exc_info=True)
        return {}


def scrub_process_environment() -> None:
    """The app's own environment never carries a launch's capability."""
    for key in ALL_ENV_KEYS:
        os.environ.pop(key, None)


# ---- a request ---------------------------------------------------------------------------------


def _capability(token: object):
    if not isinstance(token, str) or not token or len(token) > 256:
        raise Refused("unknown_capability")
    try:
        digest = template_secrets.keyed_digest(_CAPABILITY_LABEL, token)
    except template_secrets.SecretKeyUnavailable:
        raise Refused("store_unavailable") from None
    try:
        with artifact_store._con() as con:
            cap = con.execute(
                "SELECT * FROM artifact_caps WHERE digest=? AND revoked_at IS NULL", (digest,)
            ).fetchone()
    except artifact_store.ArtifactsUnavailable:
        raise Refused("store_unavailable") from None
    if cap is None:
        raise Refused("unknown_capability")
    held = _token_current(cap["id"], token)
    if held is None:
        raise Refused("store_unavailable")
    if not held:
        raise Refused("unknown_capability")
    if not artifact_store.enabled():
        raise Refused("unknown_capability")  # the global switch turned off since the launch
    return cap


def handle(token: object, op: object, args: object = None) -> dict:
    """One request from an agent. Never raises, never says more than a refusal code (and the
    offending path inside the agent's own workspace, which it named)."""
    try:
        if op not in OPS:
            raise Refused("bad_request")
        cap = _capability(token)
        origin = artifact_store.Origin(
            engine=cap["engine"],
            session_key=cap["session_key"],
            phys_key=cap["phys_key"],
            cwd=cap["cwd"],
        )
        if op == "share":
            if cap["mode"] != "public":
                raise Refused("not_public")
            aid = args.get("artifact_id") if isinstance(args, dict) else None
            owned = artifact_store.owned(artifact_store.validate_id(aid), origin)
            if owned is None:
                raise Refused("unknown_artifact")
            if not owned:
                raise Refused("not_yours")
            return {"ok": True, "artifact": _agent_view(artifact_store.share(aid))}
        if op == "list":
            page = artifact_store.list_artifacts(owner=(origin.session_key, origin.phys_key))
            return {"ok": True, "items": [_agent_view(a) for a in page["items"]]}
        out = artifact_store.publish(origin, args)
        _shelve(out, origin)
        return {
            "ok": True,
            "artifact": _agent_view(out),
            "skipped_hidden": out.get("skipped_hidden", 0),
        }
    except Refused as r:
        return {"ok": False, "error": r.code}
    except artifact_store.ArtifactRefused as r:
        reply: dict = {"ok": False, "error": r.code}
        if r.path:
            reply["path"] = r.path
        return reply
    except artifact_store.ArtifactsUnavailable:
        return {"ok": False, "error": "store_unavailable"}
    except Exception:  # noqa: BLE001 — an agent never sees a traceback
        log.warning("artifacts broker: a request failed", exc_info=True)
        return {"ok": False, "error": "store_unavailable"}


def _shelve(a: dict, origin: artifact_store.Origin) -> None:
    """A mission's session published a NEW artifact (#1932 P4): put it on that mission's shelf as
    a delivered `link` item (#1740's existing kind), so the shelf stays complete without a second
    tool call. A later revision keeps the same link and adds nothing. Best-effort: a shelf that
    refuses (full, no origin configured, the session left the mission) never fails the publish."""
    mid = a["origin"].get("mission_id")
    if not mid or a["revision"] != 1 or not a["url"].startswith(("https://", "http://")):
        return
    item = {"title": a["title"], "why": a["why"] or "published as an artifact", "url": a["url"]}
    try:
        from . import mission_shelf

        mission_shelf.deliver(
            mid, {**item, "kind": "link"}, session_key=origin.session_key, phys_key=origin.phys_key
        )
    except Exception:  # noqa: BLE001 — the artifact is published whatever the shelf says
        log.info("artifacts: %s was not shelved on %s", a["id"], mid, exc_info=True)


def _agent_view(a: dict) -> dict:
    """What the agent hears about an artifact: its id, title, kind, revision and links."""
    return {
        k: a.get(k) for k in ("id", "title", "kind", "revision", "url", "public_url", "visibility")
    }


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
                    handle, req.get("token"), req.get("op"), req.get("args")
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
                log.warning("artifacts broker: %s is served by another process", path)
                return
            path.unlink()
        server = await asyncio.start_unix_server(
            _client, sock=secrets_broker._bound_private(path), limit=REQUEST_MAX + 2
        )
        ours = os.lstat(path).st_ino
    except OSError:
        log.warning("artifacts broker: cannot serve; agents get no artifacts tool", exc_info=True)
        return
    _server_running = True
    # Blobs a publish copied but never committed (a refusal, a crash) are collected once the grace
    # period has passed; a pass at startup bounds how long they can linger.
    await asyncio.to_thread(artifact_store._collect_garbage)
    try:
        async with server:
            await asyncio.Event().wait()
    finally:
        _server_running = False
        with contextlib.suppress(OSError):
            if os.lstat(path).st_ino == ours:
                path.unlink()


def revoke_all() -> None:
    """The global switch went off: every capability ends now, so turning it back on reaches only
    NEW launches (never a running agent's old token). Never raises."""
    try:
        if artifact_store._absent():
            return
        for cap_id in _revoke_rows("1=1", ()):
            _remove_launch(cap_id)
    except Exception:  # noqa: BLE001 — the broker also refuses every request while off
        log.warning("artifacts: revoking every capability failed", exc_info=True)


def revoke_launch(phys_key: str) -> None:
    """The session's runtime is torn down (archive): its artifact capabilities end now. Its
    artifacts stay: they are results, not runtime."""
    try:
        for cap_id in _revoke_rows("phys_key=?", (phys_key,)):
            _remove_launch(cap_id)
    except Exception:  # noqa: BLE001 — teardown never fails for this
        log.warning("artifacts: revoking %s's capabilities failed", phys_key, exc_info=True)
