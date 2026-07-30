"""Orchestrator notifications — the in-app bell and the push subscription store (#726 Ph3).

**In-app first, push second.** The bell is the channel that always works: no permission prompt,
no third-party service, no iOS home-screen requirement. Web Push is an *extra* that wakes the
operator when the tab is closed. So a notification is created here unconditionally, and the
push send is a best-effort side effect — a failed or absent push must never mean the operator
never hears about an escalation.

**A subscription endpoint is a capability, not an identifier.** Anyone holding one can push to
that browser. They are stored ``0600``, never returned to the client, and never logged — the
API answers with an opaque local id and the endpoint's origin at most.

**Notification bodies carry no session content.** Same rule as the push payload: title, project,
and a link. The operator taps through and the app fetches evidence from this server under their
cookie. Keeping the two stores consistent means there is no "safe" surface that quietly holds
transcript text.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("agent_sessions.notifications")

NOTIFY_MAX = 200  # bounded ring — the bell is a recent-activity surface, not an archive
TITLE_MAX = 120
BODY_MAX = 240


def _store_path(env: str, default: str) -> Path:
    return Path(os.environ.get(env, str(Path.home() / ".config" / "agent-sessions" / default)))


def _notifications_path() -> Path:
    return _store_path("AGENT_SESSIONS_NOTIFICATIONS", "notifications.json")


def _subs_path() -> Path:
    return _store_path("AGENT_SESSIONS_PUSH_SUBS", "push-subscriptions.json")


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise a whole read-modify-write against this store.

    Every mutation here is read → mutate → replace, with no lock, and the temp file name was
    shared. Two writers interleave and one snapshot silently overwrites the other's — a
    mark-read racing an orchestrator `add`, or a 410-prune racing a `subscribe`. The shared
    temp name made it worse than a lost update: both writers open the SAME `<path>.tmp`, so
    one `os.replace` pulls the file out from under the other, which then fails with
    FileNotFoundError.

    Sidecar lock file, never the store itself — the store's inode is what `os.replace` swaps,
    so a lock held on it would not be the same lock after a write. Same reasoning, and the
    same shape, as the orchestrator ledger's lock.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# Hosts a browser push endpoint may never point at. The endpoint is attacker-supplied — the
# API takes it from the client and the server later POSTs to it — so an unvalidated one is a
# blind SSRF primitive aimed at whatever the app can reach: loopback, the metadata service,
# other boxes on the LAN. Real push services are public hosts, so a public-address policy costs
# nothing legitimate.
def assert_pushable_endpoint(endpoint: str) -> None:
    """Refuse a subscription endpoint that is not a known push-service host.

    Same allowlist the send path enforces (``webpush.assert_allowed_target``), applied here so
    a bad registration fails immediately with a clear error rather than silently never
    receiving a push. Raises ``ValueError`` to match the route's 400 handling.
    """
    from . import webpush  # deferred, as elsewhere in this module

    try:
        webpush.assert_allowed_target(endpoint)
    except webpush.PushError as e:
        raise ValueError(str(e)) from None


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: a shared ".tmp" lets one writer's os.replace unlink the file
    # another is still writing into. The lock above makes this belt-and-braces, but the
    # cost is nil and it keeps _write correct if it is ever called unlocked.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(rows, indent=2, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --- notifications ----------------------------------------------------------------------


def add(
    *,
    title: str,
    project: str,
    session_id: str,
    engine: str,
    reason: str = "",
    action_id: str = "",
    path: Path | None = None,
) -> dict:
    """Record one notification. Deliberately takes named, bounded fields rather than a free
    dict: it is the same enforcement trick as ``webpush.build_payload`` — a caller cannot slip
    screen text in without changing this signature."""
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        rec = {
            "id": hashlib.sha256(f"{action_id}{session_id}{time.time()}".encode()).hexdigest()[:16],
            "ts": time.time(),
            "read": False,
            "title": str(title)[:TITLE_MAX],
            "project": str(project)[:BODY_MAX],
            "reason": str(reason)[:BODY_MAX],
            "session_id": session_id,
            "engine": engine,
            "action_id": action_id,
        }
        rows.append(rec)
        _write(p, rows[-NOTIFY_MAX:])
        return rec


def listing(path: Path | None = None) -> dict:
    rows = sorted(_read(path or _notifications_path()), key=lambda r: -float(r.get("ts") or 0))
    return {"notifications": rows, "unread": sum(1 for r in rows if not r.get("read"))}


def mark_read(ids: list[str] | None = None, path: Path | None = None) -> int:
    """Mark the given ids read, or all of them when ``ids`` is None. Returns the count."""
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        n = 0
        for r in rows:
            if (ids is None or r.get("id") in ids) and not r.get("read"):
                r["read"] = True
                n += 1
        if n:
            _write(p, rows)
        return n


# --- push subscriptions -----------------------------------------------------------------


def _sub_id(endpoint: str) -> str:
    """Stable opaque id for an endpoint. The endpoint itself never leaves the server, so the
    client needs something else to unsubscribe with."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


def subscribe(subscription: dict, path: Path | None = None) -> dict:
    """Store one browser's push subscription. Idempotent per endpoint."""
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys") or {}
    if not isinstance(endpoint, str):
        raise ValueError("endpoint must be an https URL")
    assert_pushable_endpoint(endpoint)
    if not isinstance(keys.get("p256dh"), str) or not isinstance(keys.get("auth"), str):
        raise ValueError("subscription is missing p256dh/auth keys")
    # DECODE the key material now, not at send time. A row like {"p256dh":"a","auth":"b"} is
    # two perfectly good strings and passed the old check, then blew up inside the encryption
    # path with a binascii error — outside the httpx exception boundary, so it aborted the
    # whole fanout and every later device went unnotified. Reject it at the door instead.
    from . import webpush  # deferred, same as fanout — avoids an import cycle

    webpush.assert_usable_keys(keys["p256dh"], keys["auth"])
    p = path or _subs_path()
    with _locked(p):
        rows = [r for r in _read(p) if r.get("endpoint") != endpoint]
        rec = {
            "id": _sub_id(endpoint),
            "endpoint": endpoint,
            "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
            "created_at": time.time(),
        }
        rows.append(rec)
        _write(p, rows)
    return public_subscription(rec)


def public_subscription(rec: dict) -> dict:
    """The client-safe view: an id and the endpoint's ORIGIN, never the full URL. The origin is
    enough to show "Firefox / Chrome" in a device list; the path is the capability."""
    parts = urlsplit(rec.get("endpoint", ""))
    return {
        "id": rec.get("id", ""),
        "origin": f"{parts.scheme}://{parts.netloc}" if parts.netloc else "",
        "created_at": rec.get("created_at"),
    }


def list_subscriptions(path: Path | None = None) -> list[dict]:
    return [public_subscription(r) for r in _read(path or _subs_path())]


def unsubscribe(sub_id: str, path: Path | None = None) -> bool:
    p = path or _subs_path()
    with _locked(p):
        rows = _read(p)
        keep = [r for r in rows if r.get("id") != sub_id]
        if len(keep) == len(rows):
            return False
        _write(p, keep)
        return True


def drop_endpoint(endpoint: str, path: Path | None = None) -> None:
    """Prune a subscription the push service reported gone (404/410)."""
    p = path or _subs_path()
    with _locked(p):
        rows = _read(p)
        keep = [r for r in rows if r.get("endpoint") != endpoint]
        if len(keep) != len(rows):
            _write(p, keep)


def fanout(notification: dict, base_url: str = "", path: Path | None = None) -> dict:
    """Push one notification to every subscribed device. Blocking — call under to_thread.

    Best-effort by design: the bell entry already exists, so a dead push service degrades the
    experience rather than losing the message. A ``410 Gone`` prunes that subscription.
    """
    from . import webpush

    rows = _read(path or _subs_path())
    if not rows:
        return {"sent": 0, "pruned": 0, "failed": 0}
    uuid = notification.get("session_id", "")
    engine = notification.get("engine", "")
    url = f"{base_url}/s/{engine}/{uuid.split(':', 1)[-1]}" if uuid else f"{base_url}/pulse"
    # Title + project + link ONLY. This is the third-party boundary (#726).
    payload = webpush.build_payload(
        title=notification.get("title", "Pulse"),
        project=notification.get("project", ""),
        url=url,
    )
    sent = pruned = failed = 0
    for row in rows:
        try:
            webpush.send(row, payload)
            sent += 1
        except webpush.SubscriptionGone:
            drop_endpoint(row.get("endpoint", ""), path)
            pruned += 1
        except webpush.PushError:
            failed += 1  # never fatal — the bell entry stands on its own
        except Exception:  # noqa: BLE001 — see below
            # Deliberately broad. "Best-effort" has to mean it: a row that fails in a way we
            # did not anticipate (malformed key material surviving from before validation,
            # a codec error, a DNS change making the endpoint unresolvable) must cost only
            # THAT device. Letting it propagate skipped every later device in the list, which
            # is the opposite of best-effort — one bad row silenced the whole fleet.
            #
            # Nothing about the row is logged: the endpoint is a capability, and anyone
            # holding it can push to that device.
            log.warning("push fanout: a subscription failed unexpectedly; skipping it")
            failed += 1
    return {"sent": sent, "pruned": pruned, "failed": failed}
