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
    escalation: bool = False,
    activity_at: float | None = None,
    path: Path | None = None,
) -> dict | None:
    """Record one notification, or return ``None`` when an equivalent one is already pending.

    Deliberately takes named, bounded fields rather than a free dict: it is the same enforcement
    trick as ``webpush.build_payload`` — a caller cannot slip screen text in without changing
    this signature."""
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        # Announce an unresolved situation ONCE — but only for escalations. The orchestrator's
        # only dedupe is "at most one
        # LIVE action per session"; an escalation nobody acts on expires, the session reads as
        # free again, and the next pass re-escalates the identical situation — so the bell filled
        # with the same handful of alerts (measured: 200 rows, 54 distinct titles). Read state
        # cannot help, because nothing on that path ever consults it.
        #
        # `None` rather than the existing row: the caller fans out a push on whatever comes back,
        # so returning the old record would suppress the bell entry and still re-send the push —
        # the louder half of the problem. Nothing is lost either way; the ledger already holds the
        # durable record of every proposal.
        #
        # Scoped to escalations because `notify == "all"` also announces autonomous actions, and
        # collapsing those would stop the operator seeing what was done on their behalf — which
        # is the entire reason that mode exists.
        if escalation:
            for r in rows:
                # BOTH sides must be escalations, and the stored row must SAY so. Gating only
                # the incoming record left an autonomous `notify=all` notice able to swallow a
                # later escalation for the same session — an escalation silently lost, which is
                # the one thing the bell exists to prevent. A legacy row predating this field
                # has unprovable provenance, so it fails toward ANNOUNCING rather than
                # suppressing.
                if r.get("escalation") is not True:
                    continue
                # Session identity only. `title` used to be part of this key, and it is
                # authored by the MODEL — regenerated from scratch every pass. Measured on the
                # live store: across the 8 sessions announced more than once, the title had
                # been rewritten in 8 of 8, while `activity_at` had not moved in 6 of 8. So
                # three quarters of the repeats were one unchanged situation announced two or
                # three times because the wording drifted:
                #
                #     01:37  Awaiting user input to set Opus override on /admin/a…
                #     03:36  Awaiting user input on Opus override for #870
                #     04:42  Awaiting user input: set Opus override on /admin/ai
                #
                # Testing equality on a string a language model rewrites for free cannot work,
                # and it short-circuited the discriminator below that actually does (#760).
                if r.get("session_id") != session_id:
                    continue
                # "Has this session done anything since I told you?" — the ONLY discriminator,
                # now that the model-authored half is gone, and the one that
                # separates the SAME unresolved situation, re-proposed every TTL, from a
                # genuinely new one. A session that escalated is waiting on the operator, so
                # it emits nothing and its clock stands still; anything that could constitute a
                # different situation (a deploy failing, a new prompt) has to produce output
                # first, which moves it.
                #
                # Either side missing means unprovable, and unprovable fails toward ANNOUNCING:
                # suppressing on a guess loses an escalation, announcing twice repeats one.
                stored = r.get("activity_at")
                if (
                    not isinstance(stored, int | float)
                    or not isinstance(activity_at, int | float)
                    or stored != activity_at
                ):
                    continue
                # Re-link, don't just drop. The row still carries the FIRST proposal's id;
                # once that expired and this equivalent one was recorded, a later
                # `dismiss_for_action` on the new id would find nothing and leave the row
                # stranded. `ts`, `read` and the text are untouched so it does not resurface
                # as new — only the pointer moves, under this same lock.
                if action_id:
                    r["action_id"] = action_id
                    # The situation is live again, so the row is an alert again. Without this a
                    # row retired when its previous action expired would stay invisible while
                    # the equivalence check above keeps suppressing new ones — the situation
                    # would be unresolved, re-proposed every TTL, and announced nowhere.
                    r["retired"] = False
                    _write(p, rows)
                return None
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
            # Durable provenance: equivalence is escalation-to-escalation only, and a row has
            # to carry what it was for that to be checkable on the next pass.
            "escalation": bool(escalation),
            "activity_at": activity_at if isinstance(activity_at, int | float) else None,
        }
        rows.append(rec)
        _write(p, _evict(rows))
        return rec


def _evict(rows: list[dict]) -> list[dict]:
    """Trim to ``NOTIFY_MAX``, dropping retired rows before live ones.

    A plain ``rows[-NOTIFY_MAX:]`` evicts by age alone, so a settled row the operator can no
    longer act on can push out an escalation that is still waiting on them. Retired rows are
    only kept for the dedupe memo, which makes them the cheapest thing in the store to lose.
    """
    if len(rows) <= NOTIFY_MAX:
        return rows
    over = len(rows) - NOTIFY_MAX
    drop: set[int] = set()
    for i, r in enumerate(rows):  # oldest first — `rows` is append-ordered
        if len(drop) >= over:
            break
        if r.get("retired"):
            drop.add(i)
    kept = [r for i, r in enumerate(rows) if i not in drop]
    return kept[-NOTIFY_MAX:]


def _terminal_action_ids(rows: list[dict]) -> set[str]:
    """Of the actionable rows in ``rows``, which point at an action that has already settled?

    Read WITHOUT the notifications lock held — see :func:`listing`.

    Fails toward SHOWING, in three separate ways, because hiding an escalation the operator
    never saw is the one outcome this module exists to prevent: a row with no ``action_id``, an
    id the ledger has never heard of, and a ledger that cannot be read at all are all treated as
    "still live". A single corrupt ledger record must not empty the bell.
    """
    wanted = {
        str(r.get("action_id"))
        for r in rows
        if r.get("escalation") is True and not r.get("retired") and r.get("action_id")
    }
    if not wanted:
        return set()
    try:
        from . import orchestrator_ledger as ledger

        latest = ledger.latest_by_id()
        return {
            aid
            for aid in wanted
            if (rec := latest.get(aid)) is not None and rec.get("state") in ledger.TERMINAL_STATES
        }
    except Exception:  # noqa: BLE001 — an unreadable ledger must not retire anything
        log.debug("notifications: could not reconcile against the ledger", exc_info=True)
        return set()


def retire_for_actions(
    action_ids: list[str] | set[str],
    path: Path | None = None,
    *,
    escalations_only: bool = True,
) -> int:
    """Retire the bell rows raised for actions that have settled. Returns the count retired.

    Retiring is a flag, not a delete, and that distinction is the whole safety of this change.
    A row does double duty: it is the operator's alert AND the "I already told you about this"
    memo that :func:`add` matches on to stop one unresolved situation being announced every TTL
    (#760). Deleting on settlement would drop the memo and bring that volume regression back, so
    the row stays in the store and only leaves the *view*.

    ``escalations_only`` separates the two callers, and they genuinely want different things:

    * **Automatic** settlement (the ledger hook, read-time reconciliation) leaves everything else
      alone. Under ``notify: all`` the store also carries informational notices of what was done
      autonomously; those are a log rather than a queue, nothing waits on them, and clearing them
      because the action ended would delete the operator's only record that it happened.
    * **An explicit decision** in Pulse — approve or reject — clears whatever was raised for that
      action, informational row included. The operator has dealt with it; leaving a row behind is
      the second dismissal, in a second place, that this whole area exists to remove.
    """
    ids = {str(a) for a in action_ids if a}
    if not ids:
        return 0
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        n = 0
        for r in rows:
            if r.get("retired") or r.get("action_id") not in ids:
                continue
            if escalations_only and r.get("escalation") is not True:
                continue
            r["retired"] = True
            n += 1
        if n:
            _write(p, rows)
        return n


def listing(path: Path | None = None) -> dict:
    """The bell: rows still awaiting the operator, plus the unread count over that same set.

    Both halves come from ONE filtered list. Computing the count separately is how a badge ends
    up disagreeing with the list it labels, and the operator trusts the badge.

    **Reconciles on read.** A row whose action has already settled is retired here, so the bell
    heals itself from a settlement path nobody instrumented, from a notifications-store write
    that failed after the ledger write succeeded, and from rows that predate this behaviour —
    no migration required. Expiry is only ever *read* from the ledger, never inferred from the
    clock: :func:`orchestrator_ledger.expire_due` owns that decision, and guessing it here would
    let the bell hide something the ledger still considers live.

    The ledger read happens BEFORE the lock is taken. `listing` is the one path that holds the
    notifications lock and wants ledger data, so taking them in that order under the lock would
    invert against the settlement hook and risk a deadlock.
    """
    p = path or _notifications_path()
    rows = _read(p)
    stale = _terminal_action_ids(rows)
    if stale:
        retire_for_actions(stale, p)
        rows = _read(p)
    visible = sorted(
        (r for r in rows if not r.get("retired")), key=lambda r: -float(r.get("ts") or 0)
    )
    return {"notifications": visible, "unread": sum(1 for r in visible if not r.get("read"))}


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


def dismiss(ids: list[str] | None = None, path: Path | None = None) -> int:
    """Remove notifications by id, or every one when ``ids`` is None. Returns the count removed.

    Deliberately a DELETE, not another read-flag: "mark read" answers "have I seen this", which
    is a different question from "is this still on my list". Without a way to remove rows the
    bell was an append-only ring that could only be emptied by waiting for 200 newer ones to
    evict the old — so a saturated bell showed 99+ with no operator action that could change it.
    """
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        if ids is None:
            n = len(rows)
            if n:
                _write(p, [])
            return n
        drop = set(ids)
        keep = [r for r in rows if r.get("id") not in drop]
        n = len(rows) - len(keep)
        if n:
            _write(p, keep)
        return n


def dismiss_for_action(action_id: str, path: Path | None = None) -> int:
    """Drop the rows raised for one orchestrator action. Returns the count removed.

    The bell and the ledger are separate stores, so deciding an escalation in Pulse used to
    leave its alert sitting in the bell forever — the operator had already dealt with it and
    still had to clear it a second time, in a second place. Keyed on ``action_id`` because that
    is the only field tying the two together.
    """
    if not action_id:
        return 0
    p = path or _notifications_path()
    with _locked(p):
        rows = _read(p)
        keep = [r for r in rows if r.get("action_id") != action_id]
        n = len(rows) - len(keep)
        if n:
            _write(p, keep)
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
