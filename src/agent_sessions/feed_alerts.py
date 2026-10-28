"""Publisher alerts (#1730 P2): tell the operator before a feed they publish lapses.

The publisher host's own alarms are failed systemd units nobody watches. This install already
fetches both feeds' cuts and statements, so it can warn, through the bell and push, when the
cut it holds (the agent catalog or the gallery) is close to unusable:
* a cut ends in under 14 days if it is long-lived (only the offline release key can recut it),
  or in under 2 days if it is a 7-day cut;
* a long cut's current freshness statement expires in under 48 h (renewal has stalled for
  about 5 days), or no current statement makes it usable at all.

Off by default (`prefs.get_feed_alerts`): only the operator who publishes can act on these. Each
condition is announced once per cut (and per statement), keyed on server facts, never on text,
and the announced keys are recorded durably (`prefs`), so a dismissed or evicted bell row is not
re-pushed while its condition lasts. Once a condition clears its key is forgotten, so the next
episode is announced again. Before each check both feeds are refreshed (turning these alerts on
means checking the feeds every 6 hours, whatever the catalog's daily-check setting), so a stale
local copy is never mistaken for a stalled publisher. Sweeps never overlap. A failure here never
touches the feeds: alerts are best effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time

from . import notifications, prefs

log = logging.getLogger(__name__)

INTERVAL = 6 * 3600
LONG_CONTENT_WARN = 14 * 86400
SHORT_CONTENT_WARN = 2 * 86400
STATEMENT_WARN = 48 * 3600
NAMES = {"agents": "agent catalog", "gallery": "gallery"}
#: A local read that fails is retried once after this long before it counts as unavailable, so
#: a refresh holding the feed's lock (2 s) is never reported as damaged trust records.
RETRY_AFTER = 5


def _agents():
    from .plugins import feed

    snapshot = feed.historical()
    if snapshot is None:
        return None
    return snapshot, (lambda now: feed.statement_expiry(snapshot, now=now))


def _gallery():
    from .gallery import trust

    record = trust.current()
    if record is None:
        return None
    parsed = trust.checked(record, historical=True)
    return parsed, (lambda now: trust.statement_expiry(parsed, now=now))


def _read(read, now):
    """`(cut, statement expiry or None)` for a feed, or None if nothing is held. Raises if the
    trust evidence can't be read twice in a row (damage, an unsafe file, a stuck lock)."""
    from .plugins import freshness
    from .plugins.provenance import ProvenanceError

    for attempt in (0, 1):
        try:
            held = read()
            if held is None:
                return None
            cut, statement_expiry = held
            # Only a long cut needs a statement: a short one never reads that evidence.
            if not freshness.is_long(cut.issued_at, cut.expires_at):
                return cut, None
            return cut, statement_expiry(now)
        except (ValueError, OSError, ProvenanceError):
            if attempt:
                raise
            time.sleep(RETRY_AFTER)
    return None


def evaluate(*, now: int | None = None) -> list[dict]:
    """The alerts due now: `{key, title, reason}` each. Reads only local, verified evidence."""
    from .plugins import freshness
    from .plugins.provenance import ProvenanceError

    now = int(time.time()) if now is None else now
    alerts = []
    for name, read in (("agents", _agents), ("gallery", _gallery)):
        label = NAMES[name]
        try:
            held = _read(read, now)
        except (ValueError, OSError, ProvenanceError):
            alerts.append(
                {
                    "key": f"feed-alert:{name}:unavailable",
                    "title": f"The {label}'s trust records are unavailable",
                    "reason": "Restore its trust records; it can't be refreshed until then.",
                }
            )
            continue
        if held is None:
            continue
        cut, expires = held
        long = freshness.is_long(cut.issued_at, cut.expires_at)
        left = cut.expires_at - now
        if left <= 0:
            alerts.append(
                {
                    "key": f"feed-alert:{name}:{cut.sequence}:expired",
                    "title": f"The {label} has expired",
                    "reason": f"Cut {cut.sequence} ended; sign and publish a new one.",
                }
            )
            continue
        if left <= (LONG_CONTENT_WARN if long else SHORT_CONTENT_WARN):
            days = max(1, left // 86400)
            alerts.append(
                {
                    "key": f"feed-alert:{name}:{cut.sequence}:content",
                    "title": f"The {label} ends in about {days} day{'s' if days != 1 else ''}",
                    "reason": f"Cut {cut.sequence}: sign and publish the next one before it ends.",
                }
            )
        if not long:
            continue
        if expires is None:
            alerts.append(
                {
                    "key": f"feed-alert:{name}:{cut.sequence}:no-statement",
                    "title": f"The {label} has no current freshness statement",
                    "reason": "Its daily renewal on the publisher isn't reaching this install.",
                }
            )
        # A statement can't outlive its cut: one capped by the cut's own end is no stall (the
        # content alert covers that window).
        elif expires - now <= STATEMENT_WARN and expires < cut.expires_at:
            alerts.append(
                {
                    "key": f"feed-alert:{name}:{cut.sequence}:statement:{expires}",
                    "title": f"The {label}'s freshness statement expires within 48 hours",
                    "reason": "Its daily renewal on the publisher has stalled; check its timer.",
                }
            )
    return alerts


ANNOUNCED_MAX = 200


def _announced() -> list[str]:
    value = prefs.get_feed_alerts_announced()
    return value if isinstance(value, list) else []


def _remember(key: str) -> None:
    keys = [k for k in _announced() if k != key] + [key]
    prefs.set_feed_alerts_announced(keys[-ANNOUNCED_MAX:])


def _pushed() -> list[str]:
    value = prefs.get_feed_alerts_pushed()
    return value if isinstance(value, list) else []


def _remember_pushed(key: str) -> None:
    keys = [k for k in _pushed() if k != key] + [key]
    prefs.set_feed_alerts_pushed(keys[-ANNOUNCED_MAX:])


def _retracting() -> list[str]:
    value = prefs.get_feed_alerts_retracting()
    return value if isinstance(value, list) else []


def _feed_of(key: str) -> str | None:
    """`agents` / `gallery` from a `feed-alert:<feed>:...` key; None for anything else."""
    parts = key.split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "feed-alert" else None


def _forget_cleared(due: list[dict]) -> None:
    """Forget every condition that cleared, and retract its bell row, so its next episode
    (another outage, another lapse) gets a fresh row and a fresh push. A feed that couldn't be
    read is unknown, not cleared: everything remembered for it is kept.

    A cleared key is recorded as RETRACTING before anything else (Hermes 7276, 7281; review r8):
    its row belongs to a finished episode, so it must never stand in for the next one. It stays
    pending until its row is gone; the next sweep retries it, and a recurrence retracts it before
    writing its own row."""
    keep = {alert["key"] for alert in due}
    unreadable = {_feed_of(key) for key in keep if key.endswith(":unavailable")}

    def still(key: str) -> bool:  # due now, or its feed couldn't be read (unknown, not cleared)
        return key in keep or (_feed_of(key) is not None and _feed_of(key) in unreadable)

    keys, pushed, pending = _announced(), _pushed(), _retracting()
    cleared = [
        k
        for k in dict.fromkeys(keys + pushed + pending)
        if not still(k) and _feed_of(k) is not None
    ]
    intent = list(dict.fromkeys([k for k in pending if still(k)] + cleared))[-ANNOUNCED_MAX:]
    trimmed_pushed = [k for k in pushed if still(k)]
    trimmed_keys = [k for k in keys if still(k)]
    # ONE atomic write ends the episodes, and only then is any row touched (independent review
    # r8): a crash or a full disk leaves either every record as it was (the episode goes on, its
    # row untouched) or every cleared key pending, which `_retract_stale` finishes before the next
    # episode writes or pushes. Never a row gone with its records left behind.
    if (intent, trimmed_pushed, trimmed_keys) != (pending, pushed, keys):
        prefs.set_feed_alert_records(
            announced=trimmed_keys, pushed=trimmed_pushed, retracting=intent
        )
    done = set()
    for key in cleared:
        try:
            notifications.dismiss_for_action(key)
            done.add(key)
        except Exception:  # noqa: BLE001
            log.warning("could not retract cleared feed alert %s; retrying next sweep", key)
    if done:
        prefs.set_feed_alerts_retracting([k for k in _retracting() if k not in done])


def _retract_stale(key: str) -> bool:
    """Retract a finished episode's row before this episode writes its own. Whether the bell is
    clear of it now; if not, the next sweep tries again."""
    if key not in _retracting():
        return True
    try:
        notifications.dismiss_for_action(key)
        prefs.set_feed_alerts_retracting([k for k in _retracting() if k != key])
        return True
    except Exception:  # noqa: BLE001
        log.warning("could not retract the previous episode of feed alert %s", key)
        return False


def _bell_row(key: str) -> dict | None:
    """This alert's bell row, or None. An unreadable bell answers None: the announcement is then
    repeated rather than lost (the same direction `notifications.add` fails)."""
    try:
        rows = notifications.listing()["notifications"]
    except Exception:  # noqa: BLE001
        log.exception("could not read the bell to check for a feed alert")
        return None
    return next((r for r in rows if r.get("action_id") == key), None)


def _push(row: dict) -> bool:
    """Deliver one bell row through `fanout`, which reads the subscriptions STRICTLY: an
    unreadable list raises (retried by the next sweep), never "nobody to tell". Delivered once
    any device received it, or there is genuinely nobody subscribed: one permanently broken
    subscription must not re-push every healthy phone every sweep."""
    try:
        result = notifications.fanout(row) or {}
    except Exception:  # noqa: BLE001 — unreadable subscriptions, a dead push service: retry
        log.exception("could not push a feed alert")
        return False
    return result.get("sent", 0) >= 1 or not result.get("failed")


def announce(alert: dict) -> bool:
    """One bell row and one delivered push per condition (Hermes 7266).
    * The bell row: written once. The durable announced record is the authority, so a row the
      operator dismissed (or the bell evicted) is never written again while the condition lasts.
      A row found without a record (a crash after the write) repairs the record.
    * The push: recorded separately, only once it was delivered. A bell row is no proof of
      delivery: a crash between the row and the push, or a failed fan-out, is retried by the next
      sweep for as long as the condition (and its row) lasts.
    Returns whether a new bell row was written."""
    key = alert["key"]
    # A row left by this condition's previous episode is retracted first, never adopted: the
    # operator may have read it, and this is new news (Hermes 7281). Until that succeeds this
    # episode writes no row of its own; its push still goes out.
    clear = _retract_stale(key)
    row = _bell_row(key) if clear else None
    created = False
    if clear and key not in _announced():
        written = row is not None
        if row is None:
            # A bell that can't take the row (full, unwritable) must not cost the phone its
            # warning (Hermes 7298): the push below still goes out, and the row is retried by the
            # next sweep, unremembered, without pushing again.
            try:
                row = notifications.add(
                    title=alert["title"],
                    project="",
                    session_id="",
                    engine="",
                    reason=alert["reason"],
                    action_id=key,
                )
                created = bool(row)
                written = True  # None: an equivalent row is already pending
            except Exception:  # noqa: BLE001
                log.warning("could not write the bell row for feed alert %s", key)
        if written:
            with contextlib.suppress(Exception):
                _remember(key)
    if key not in _pushed():
        # The row may be gone (dismissed, evicted) before a failed push could be retried; the
        # still-due alert itself carries everything the push needs (Hermes 7276).
        target = row or {
            "title": alert["title"],
            "reason": alert["reason"],
            "project": "",
            "engine": "",
            "session_id": "",
            "action_id": key,
        }
        if _push(target):
            with contextlib.suppress(Exception):
                _remember_pushed(key)
    return created


def _refresh_feeds() -> None:
    """Fetch both feeds first, so a stale local copy is never mistaken for a stalled publisher:
    the gallery has no automatic refresh, and the agent catalog's can be turned off. Failures
    are the alerts' to report (an old cut or statement stays old)."""
    from . import agent_catalog_refresh
    from .gallery import gallery_client

    with contextlib.suppress(Exception):
        asyncio.run(agent_catalog_refresh.refresh())
    with contextlib.suppress(Exception):
        asyncio.run(gallery_client.refresh())


_SWEEPING = threading.Lock()


def sweep(*, now: int | None = None) -> list[dict]:
    """One pass, blocking: refresh both feeds, then announce what is due. Returns the alerts.
    Never two at once (the loop and a just-enabled check): a second caller skips."""
    if not prefs.get_feed_alerts():
        return []
    if not _SWEEPING.acquire(blocking=False):
        return []
    try:
        _refresh_feeds()
        due = evaluate(now=now)
        for alert in due:
            try:
                announce(alert)
            except Exception:  # noqa: BLE001 — a full bell must not stop the sweep
                log.exception("could not record feed alert %s", alert["key"])
        with contextlib.suppress(Exception):
            _forget_cleared(due)
        return due
    finally:
        _SWEEPING.release()


def enabled_loop() -> bool:
    """The same kill switch as the loop, for the check run when the setting is turned on."""
    return os.environ.get("AGENT_SESSIONS_FEED_ALERTS_LOOP") != "0"


async def run() -> None:
    if not enabled_loop():
        return
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001
            log.exception("feed alert sweep failed")
        await asyncio.sleep(INTERVAL)
