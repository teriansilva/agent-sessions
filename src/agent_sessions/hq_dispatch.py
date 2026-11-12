"""HQ's Dispatches (#1837, #1746 P3a): one short line HQ writes about each notification.

Every bell row is a server fact with fixed text, and stays one. This module adds, beside it, a line
HQ writes as the selected contact — without touching the row, its push, or any notification
invariant (``docs/invariants/needs-you-notifications.md``):

- **the row and its push go out first, exactly as today.** A producer hands the event here only
  AFTER its existing push handling (:func:`enqueue`), wrapped on its own: an error is logged and
  swallowed, so it cannot prevent, delay or change a push;
- **a background writer** (:func:`run`) asks the model once per **bundle** of events — one event, or
  several within :data:`WINDOW_S` — and stores the line here, never on the row;
- **a model failure writes nothing.** The fixed text is always the correct fallback.

**The store** is its own file (``AGENT_SESSIONS_HQ_DISPATCHES``, mode 0600) under its OWN flock
(``atomicjson.json_write_lock``), read strictly: a document that exists but cannot be read raises
:class:`StoreUnreadable` and is **never rewritten** — the writer skips, the views say so. Shape::

    {"version": 1,
     "bundles":   {"<bid>": {"opened_at", "seals_at", "sealed", "members": [<tag>, …]}},
     "member_of": {"<tag>": "<bid>"},       # create-only: a tag joins ONE bundle
     "facts":     {"<tag>": {"class", …}},  # the model input, and nothing else
     "display":   {"<tag>": {"class", "title", "reason", "link", "ts"}},  # the view's; never sent
     "claims":    {"<bid>": {"state": attempt|written|failed|gave_up, "at", "code"}},
     "lines":     {"<bid>": {"text", "contact", "at"}},
     "pending":   {"<tag>": {"pid", "start", "at"}},  # a pushing producer's hand-off reservation
     "seen":      [<line at>, "<bid>"],     # the Commander's unread cursor
     "reconcile_floor": <ts>}               # rows at or before it are never reconciled (pruned)

**At most once per bundle.** The claim is create-only, written under the lock before the model
call; an existing claim means another worker has it. Nothing is retried: a failure settles
``failed``, and a claim still ``attempt`` after :data:`GIVE_UP_S` settles ``gave_up``. The worst
case is a missing line, never two.

**Every announced event joins at most one bundle.** Membership and both of the member's snapshots
are one locked write. A lost enqueue (a crash between the row write and here, or an error the
producer swallowed) is found by the writer's reconcile pass from the row's ``dispatch`` tag once
the row is :data:`RECONCILE_GRACE_S` old (its push is done) and within :data:`RECONCILE_S`. The
one stated loss boundary: a failed or interrupted enqueue whose row is dismissed or evicted before
the next pass, or that no pass sees within 24 h.

**Facts only.** ``hq_notify``'s input is the members' ``facts`` snapshots: per-producer derived
fields (class, project, engine label, age, and the mission or automation's own name), never a row's
title or reason, a session's text, a run or escalation reason, or anything third-party. ``display``
is the view's record and is never sent.

Kill switch: ``AGENT_SESSIONS_HQ_DISPATCH_LOOP=0`` stops the writer (enqueues still record).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import queue
import re
import secrets as _secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger("agent_sessions.hq_dispatch")

VERSION = 1
#: A bundle takes events for this long after it opens, then seals (a single event is a bundle of
#: one, sealed this long after it opened). A constant in P3a; per-route windows are P3b.
WINDOW_S = 60
#: …and at most this many members.
BUNDLE_MAX = 10
#: The model call's own cap — below the endpoint's request timeout.
CALL_CAP_S = 60
#: A claim still ``attempt`` this long after it was taken is ``gave_up`` (above the call's cap).
GIVE_UP_S = 120
#: Seconds between writer passes when nothing pokes it.
INTERVAL_S = 10
#: A row's lost enqueue is reconciled only within this long of its announcement.
RECONCILE_S = 24 * 3600
#: …and only once it is at least this old (Hermes 7620, finding 2), so the reconcile pass can never
#: ask the model about an event whose producer is still pushing it. A push is one POST per device,
#: in turn, each under `webpush.PUSH_TIMEOUT_S` (10 s) per httpx phase (connect, write, read,
#: pool), so at most ~40 s for a device that is unreachable at every step, and well under a second
#: for a healthy one. 300 s covers seven worst-case devices in one fanout (or one needs-you sync
#: pushing several episodes to a couple of dead ones) and still recovers a lost enqueue within
#: minutes, far inside the 24 h window. A fanout slower than that can see its line written first:
#: the line is in-app only and the push payload was built before it existed, so it can neither
#: change nor delay the push.
RECONCILE_GRACE_S = 300
#: The persisted hand-off RESERVATION is written by a background worker with ONE try of the
#: store's lock and no wait (Hermes 7625 and 7627, finding 2): nothing may wait ahead of a push. A
#: busy store means no persisted reservation; the in-memory in-flight set still fences this process.
RESERVE_WAIT_S = 0.0
#: The facts only a producer's enqueue knows. A reconciled member is UPGRADED with them by the
#: producer's later enqueue while its bundle is unclaimed; once claimed it stays as it is.
ENQUEUE_ONLY = ("mission", "automation", "outcome")
#: Bundles older than this are pruned with their members, claims and lines…
KEEP_S = 30 * 86400
#: …and the store keeps at most this many bundles, oldest pruned first.
KEEP_MAX = 2000
#: HQ's line: longer output is ``failed``, never truncated into something it did not say.
LINE_MAX = 240
#: The Commander's own labels for a mission or automation, in the model input.
NAME_MAX = 80
#: The view's fixed text, capped at write (the notification store's own limits).
TITLE_MAX = 120
REASON_MAX = 240
#: The model call's token budget.
MAX_TOKENS = 200
#: How many dispatches the list returns.
LIST_MAX = 50

CLASS_NEEDS_YOU = "needs you"
CLASS_ESCALATION = "mission escalation"
CLASS_AUTOMATION = "automation failed"
#: Each class's facts, exactly. A REDUCED (reconciled) member carries a subset: what the row's
#: structured fields give, never anything parsed out of its title or reason.
FACT_KEYS: dict[str, frozenset[str]] = {
    CLASS_NEEDS_YOU: frozenset({"class", "project", "engine", "age_min"}),
    CLASS_ESCALATION: frozenset({"class", "mission", "project", "engine", "age_min"}),
    CLASS_AUTOMATION: frozenset({"class", "automation", "outcome", "age_min"}),
}
#: The reduced shape a reconciled member gets: the row's structured fields only.
REDUCED_KEYS: dict[str, frozenset[str]] = {
    CLASS_NEEDS_YOU: frozenset({"class", "project", "engine", "age_min"}),
    CLASS_ESCALATION: frozenset({"class", "project", "engine", "age_min"}),
    CLASS_AUTOMATION: frozenset({"class", "age_min"}),
}
#: An automation run's outcome, as the automations store spells it.
OUTCOMES = frozenset({"failed", "refused", "partial"})
_CLASS_OF_PREFIX = {
    "needs-you:": CLASS_NEEDS_YOU,
    "escalation:": CLASS_ESCALATION,
    "automation:": CLASS_AUTOMATION,
}

#: A bundle id: server-minted, opaque, the only id a link carries.
BID_RE = re.compile(r"d_[0-9a-f]{16}")
#: A member's link — the row's own Open target (`notifications._link` with no base URL) — must be
#: one of these at write AND at read, or the member shows no link.
LINK_RES = (
    re.compile(r"/s/[a-z0-9][a-z0-9_-]{0,39}/[A-Za-z0-9._:@-]{1,200}"),
    re.compile(r"/mission/automations/[a-z0-9]{1,64}"),
    re.compile(r"/mission"),
)
_CODE_RE = re.compile(r"[a-z0-9_]{1,40}")
_CLAIM_STATES = frozenset({"attempt", "written", "failed", "gave_up"})


class StoreUnreadable(Exception):
    """The dispatch document exists and cannot be read or parsed. It is never rewritten."""


# ---- the store ----------------------------------------------------------------------------------


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_HQ_DISPATCHES",
            str(Path.home() / ".config" / "agent-sessions" / "hq-dispatches.json"),
        )
    )


def _empty() -> dict:
    return {
        "version": VERSION,
        "bundles": {},
        "member_of": {},
        "facts": {},
        "display": {},
        "claims": {},
        "lines": {},
        "pending": {},
        "seen": [0, ""],
        "reconcile_floor": 0,
    }


#: The largest magnitude a stored number may have: far beyond any time, count or age, and small
#: enough that every conversion is exact and cannot overflow (Hermes 7743, finding 2).
NUM_MAX = 2**53


def _num(v: object) -> bool:
    """A finite, bounded number. ``Infinity`` / ``NaN`` (which JSON round-trips) never pass for a
    time: a stored ``reconcile_floor: Infinity`` would switch reconciliation off for good (Hermes
    7681). An integer is compared, never converted, so a 400-digit one is refused, not raised."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return False
    if isinstance(v, int):
        return -NUM_MAX <= v <= NUM_MAX
    return math.isfinite(v) and -NUM_MAX <= v <= NUM_MAX


#: A stored member's age bound, in minutes (beyond any retention window).
AGE_MAX = 10**8


def _valid_stored_facts(f: object) -> bool:
    """A stored member is EXACTLY what `_clean_facts` can produce (Hermes 7743, finding 1): its
    class's keys only, each label a string within `NAME_MAX`, a known outcome, a bounded whole age.
    The claim sends members as stored, so anything else would reach the model."""
    if not isinstance(f, dict):
        return False
    cls = f.get("class")
    allowed = FACT_KEYS.get(cls) if isinstance(cls, str) else None
    if allowed is None or not set(f) <= allowed:
        return False
    for k in LABEL_KEYS:
        if k in f and (not isinstance(f[k], str) or len(f[k]) > NAME_MAX):
            return False
    if "outcome" in f and not (isinstance(f["outcome"], str) and f["outcome"] in OUTCOMES):
        return False
    age = f.get("age_min", 0)
    return isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= AGE_MAX


def _read_strict(path: Path | None = None) -> dict:
    """The document as stored. Missing or empty is an empty store (knowable); anything else that
    cannot be read, parsed or shaped raises :class:`StoreUnreadable` rather than answer empty."""
    p = path or _path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except (OSError, UnicodeDecodeError) as e:
        raise StoreUnreadable(type(e).__name__) from e
    if not text.strip():
        return _empty()
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as e:  # JSONDecodeError, and an over-long integer
        raise StoreUnreadable("not JSON") from e
    try:
        return _shape(raw)
    except StoreUnreadable:
        raise
    except (TypeError, ValueError, OverflowError, KeyError, AttributeError) as e:
        # Whatever a check above did not foresee is still unreadable, never a 500 (Hermes 7743).
        raise StoreUnreadable("a value is malformed") from e


def _shape(raw: object) -> dict:
    """`_read_strict`'s checks on the parsed document: the shaped store, or StoreUnreadable."""
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        raise StoreUnreadable("not a version-1 dispatch document")
    doc = _empty()
    for k in ("bundles", "member_of", "facts", "display", "claims", "lines", "pending"):
        v = raw.get(k, {})
        if not isinstance(v, dict):
            raise StoreUnreadable(f"{k} is not a mapping")
        doc[k] = v
    seen = raw.get("seen", [0, ""])
    if (
        not isinstance(seen, list)
        or len(seen) != 2
        or not _num(seen[0])
        or not isinstance(seen[1], str)
    ):
        raise StoreUnreadable("seen is not a cursor")
    doc["seen"] = [float(seen[0]), seen[1]]
    floor = raw.get("reconcile_floor", 0)
    if not _num(floor):
        raise StoreUnreadable("reconcile_floor is not a time")
    doc["reconcile_floor"] = float(floor)
    for k, v in doc["member_of"].items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise StoreUnreadable("a bundle membership is malformed")
    if not all(_valid_stored_facts(v) for v in doc["facts"].values()):
        raise StoreUnreadable("a member's facts are malformed")
    if not all(isinstance(v, dict) for v in doc["display"].values()):
        raise StoreUnreadable("a display entry is not a mapping")
    for r in doc["pending"].values():
        if (
            not isinstance(r, dict)
            or not isinstance(r.get("pid"), int)
            or isinstance(r.get("pid"), bool)
            or not isinstance(r.get("start"), str)
            or not _num(r.get("at"))
        ):
            raise StoreUnreadable("a hand-off reservation is malformed")
    for bid, b in doc["bundles"].items():
        if (
            not BID_RE.fullmatch(str(bid))
            or not isinstance(b, dict)
            or not _num(b.get("opened_at"))
            or not _num(b.get("seals_at"))
            or not isinstance(b.get("sealed"), bool)
            or not isinstance(b.get("members"), list)
            or not all(isinstance(t, str) for t in b["members"])
        ):
            raise StoreUnreadable("a bundle is malformed")
    for c in doc["claims"].values():
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("state"), str)
            or c["state"] not in _CLAIM_STATES
            or not _num(c.get("at"))
        ):
            raise StoreUnreadable("a claim is malformed")
    for line in doc["lines"].values():
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            raise StoreUnreadable("a line is malformed")
        if not _num(line.get("at")) or not isinstance(line.get("contact"), str):
            raise StoreUnreadable("a line is malformed")
    return doc


def _write(doc: dict, path: Path | None = None) -> None:
    atomic_write_json(path or _path(), doc, mode=0o600)


# Test seam (the acceptance tests kill a real worker at a named point): called with the point's
# name at each step boundary. ``None`` in production.
_POINT: Callable[[str], None] | None = None


def _point(name: str) -> None:
    hook = _POINT
    if hook is not None:
        hook(name)


def _prune(doc: dict, now: float) -> None:
    """Drop bundles older than :data:`KEEP_S`, then the oldest beyond :data:`KEEP_MAX`, each with
    its members' snapshots, its claim and its line. ``seen`` is never touched. A bundle pruned by
    the CAP raises ``reconcile_floor`` past its window, so its rows are never reconciled again."""
    bundles = doc["bundles"]
    order = sorted(bundles, key=lambda b: (float(bundles[b]["opened_at"]), b))
    drop = [b for b in order if now - float(bundles[b]["opened_at"]) > KEEP_S]
    dropped = set(drop)
    keep = [b for b in order if b not in dropped]
    over = keep[: max(0, len(keep) - KEEP_MAX)]
    for b in over:
        doc["reconcile_floor"] = max(
            float(doc["reconcile_floor"]), float(bundles[b]["opened_at"]) + WINDOW_S
        )
    for b in [*drop, *over]:
        for tag in bundles[b]["members"]:
            if doc["member_of"].get(tag) == b:
                del doc["member_of"][tag]
                doc["facts"].pop(tag, None)
                doc["display"].pop(tag, None)
        del bundles[b]
        doc["claims"].pop(b, None)
        doc["lines"].pop(b, None)


def _join(doc: dict, tag: str, facts: dict, display: dict, now: float) -> str:
    """Join ``tag`` to a bundle (the caller holds the lock and writes). Create-only."""
    existing = doc["member_of"].get(tag)
    if isinstance(existing, str) and existing in doc["bundles"]:
        return existing
    bundles = doc["bundles"]
    open_ = [
        b
        for b, rec in bundles.items()
        if not rec["sealed"]
        and 0 <= now - float(rec["opened_at"]) < WINDOW_S
        and len(rec["members"]) < BUNDLE_MAX
    ]
    if open_:
        bid = max(open_, key=lambda b: (float(bundles[b]["opened_at"]), b))
    else:
        bid = "d_" + _secrets.token_hex(8)
        while bid in bundles:
            bid = "d_" + _secrets.token_hex(8)
        bundles[bid] = {
            "opened_at": now,
            "seals_at": now + WINDOW_S,
            "sealed": False,
            "members": [],
        }
    bundles[bid]["members"].append(tag)
    doc["member_of"][tag] = bid
    doc["facts"][tag] = facts
    doc["display"][tag] = display
    return bid


# ---- what a member carries ----------------------------------------------------------------------


def class_of(tag: str) -> str | None:
    for prefix, cls in _CLASS_OF_PREFIX.items():
        if tag.startswith(prefix):
            return cls
    return None


def escalation_tag(mission_id: str, objective_key: str, episode: int) -> str:
    """A mission escalation's event tag: the ``escalate_once`` arbitration key it just won —
    mission, a digest of the objective key (so the tag carries no objective text) and episode.
    The same inputs give the same tag, so a retry or a reconcile pass derives it again."""
    digest = hashlib.sha256(str(objective_key).encode()).hexdigest()[:12]
    return f"escalation:{mission_id}:{digest}:{int(episode)}"


def _engine_label(engine: object) -> str:
    """The engine's label, asked of its manifest (never a name kept here). "" when unknown."""
    if not isinstance(engine, str) or not engine:
        return ""
    try:
        from . import engines

        m = engines.manifest_of(engine)
        return str(m.identity.label) if m is not None else ""
    except Exception:  # noqa: BLE001 — a fact we cannot establish is simply absent
        return ""


def _age_min(ts: object, now: float) -> int:
    return max(0, int((now - float(ts)) // 60)) if _num(ts) else 0


def _clip(value: object, cap: int) -> str:
    return " ".join(value.split())[:cap] if isinstance(value, str) else ""


def reduced_facts(row: dict, now: float | None = None) -> dict:
    """A member's facts from its ROW's structured fields alone: the class from the tag's prefix,
    project, engine label and age — never its title or reason. What a reconciled member gets, and
    the base every producer extends with its own enqueue-only fields."""
    now = time.time() if now is None else now
    cls = class_of(str(row.get("dispatch") or "")) or ""
    out: dict = {"class": cls}
    if cls in (CLASS_NEEDS_YOU, CLASS_ESCALATION):
        project = _clip(row.get("project"), NAME_MAX)
        engine = _engine_label(row.get("engine"))
        if project:
            out["project"] = project
        if engine:
            out["engine"] = engine
    out["age_min"] = _age_min(row.get("ts"), now)
    return out


def display_of(row: dict) -> dict:
    """The view's snapshot of a row: its fixed text (capped), its own Open target and its time.
    Never sent to the model."""
    from . import notifications

    link = notifications._link(row)
    return {
        "class": class_of(str(row.get("dispatch") or "")) or "",
        "title": str(row.get("title") or "")[:TITLE_MAX],
        "reason": str(row.get("reason") or "")[:REASON_MAX],
        "link": link if _valid_link(link) else "",
        "ts": float(row["ts"]) if _num(row.get("ts")) else 0.0,
    }


def _valid_link(link: object) -> bool:
    return isinstance(link, str) and any(r.fullmatch(link) for r in LINK_RES)


#: Every free-form label that can enter the model input (Hermes 7619, finding 1). The engine label
#: is roster-derived, but it passes the same gate: nothing textual reaches the model unchecked.
LABEL_KEYS = ("project", "engine", "mission", "automation")


def _guarded_label(value: object, secrets: list[str] | None) -> str | None:
    """A label through the HQ text boundary, or ``None`` (dropped). Dropped when the secrets
    snapshot is unavailable, when the label is credential-shaped, and when it carried a secret
    (a redacted label says nothing the facts need) — never sent altered."""
    from . import ask_store

    if not isinstance(value, str) or not value.strip() or secrets is None:
        return None
    label = _clip(value, NAME_MAX)
    try:
        guarded = ask_store.guard_text(label, NAME_MAX, secrets=secrets)
    except Exception:  # noqa: BLE001 — a label that cannot be checked is dropped
        return None
    return label if guarded == label else None


def _clean_facts(facts: dict, secrets: list[str] | None) -> dict:
    """Exactly the class's keys, each a bounded server-derived value. EVERY label (project, engine,
    mission, automation) passes the HQ text boundary with ``secrets`` — the snapshot the caller
    took under the registration fence; ``None`` (unavailable) drops them all. The ONE cleaner for
    both an enqueued and a reconciled member."""
    cls = facts.get("class")
    allowed = FACT_KEYS.get(cls) if isinstance(cls, str) else None
    if allowed is None:
        raise ValueError("unknown dispatch class")
    out: dict = {"class": cls}
    for k in LABEL_KEYS:
        if k in allowed:
            label = _guarded_label(facts.get(k), secrets)
            if label is not None:
                out[k] = label
    if "outcome" in allowed and facts.get("outcome") in OUTCOMES:
        out["outcome"] = facts["outcome"]
    age = facts.get("age_min")
    out["age_min"] = min(AGE_MAX, max(0, int(age))) if _num(age) else 0  # what a read accepts
    return out


def _clean_display(display: dict) -> dict:
    link = display.get("link")
    return {
        "class": str(display.get("class") or "")[:40],
        "title": str(display.get("title") or "")[:TITLE_MAX],
        "reason": str(display.get("reason") or "")[:REASON_MAX],
        "link": link if _valid_link(link) else "",
        "ts": float(display["ts"]) if _num(display.get("ts")) else 0.0,
    }


def _secrets_snapshot() -> list[str] | None:
    from . import template_secrets

    try:
        return template_secrets.redaction_values()
    except template_secrets.RedactionUnavailable:
        return None


# ---- the enqueue: membership with the announcement ---------------------------------------------


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _proc_start(pid: int) -> str | None:
    """A process's identity beyond its pid: this boot and its start time in clock ticks (field 22
    of ``/proc/<pid>/stat``, read after the command name's closing parenthesis), stable for its
    whole life, so a reused pid can never pass for it. ``None`` when it is gone or unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    if len(fields) < 20:
        return None
    return f"{_boot_id()}:{fields[19]}"


def _owner_alive(r: dict) -> bool:
    """Whether a reservation's owner is the SAME live process (pid and start time)."""
    return _proc_start(int(r["pid"])) == r["start"]


@contextlib.contextmanager
def _try_lock(path: Path, wait_s: float):
    """The store's flock (the same sidecar ``json_write_lock`` takes), waited for at most
    ``wait_s``; yields whether it was taken."""
    import fcntl

    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---- the push-first fence ----------------------------------------------------------------------
#
# THE IN-FLIGHT SET (Hermes 7625, finding 1) is the authoritative fence for a live push in THIS
# process: the tags whose producer is between "about to push" and "handed off". The writer loop runs
# in the same process as the producers (BattleLab is one process), so reconcile skips every tag in
# it, whatever its age, and adding to it touches no lock and no store: it cannot fail or wait. The
# persisted reservation (`pending`) is only the record across a restart; a multi-process deployment
# is outside the contract (as for typed secrets).

# A COUNT per tag, not a set (Hermes 7681, finding 1): two producers may fence the same tag at once
# (the same episode key announced twice), and the first one to finish must not lift the other's.
_inflight: dict[str, int] = {}
# Tags whose enqueue failed while held: their reservation is released once the LAST hold ends, so
# a reservation still queued can no longer be written, and no other producer's fence is lifted.
_release_owed: set[str] = set()
_inflight_lock = threading.Lock()


def _tags(rows) -> list[str]:
    return [
        r["dispatch"]
        for r in rows or ()
        if isinstance(r, dict) and isinstance(r.get("dispatch"), str)
    ]


def begin_flight(rows) -> list[str]:
    """Mark ``rows``' tags as being pushed by this process. Returns them, for :func:`end_flight`:
    each returned tag must be ended exactly once."""
    tags = _tags(rows)
    with _inflight_lock:
        for t in tags:
            _inflight[t] = _inflight.get(t, 0) + 1
    return tags


def end_flight(tags) -> None:
    """One producer's push and hand-off are over (or failed): its hold on each tag ends. A tag
    leaves the in-flight set when the last producer holding it ends; a reservation owed a release
    (a failed enqueue) is released then, after it has left."""
    owed: list[str] = []
    with _inflight_lock:
        for t in tags or ():
            n = _inflight.get(t, 0) - 1
            if n > 0:
                _inflight[t] = n
                continue
            _inflight.pop(t, None)
            if t in _release_owed:
                _release_owed.discard(t)
                owed.append(t)
    for t in owed:
        release(t)


def _release_when_landed(tag: str) -> None:
    """Release ``tag``'s reservation now if no producer holds it, else when the last hold ends."""
    with _inflight_lock:
        held = _inflight.get(tag, 0) > 0
        if held:
            _release_owed.add(tag)
    if not held:
        release(tag)


def in_flight(tag: str) -> bool:
    with _inflight_lock:
        return _inflight.get(tag, 0) > 0


@contextlib.contextmanager
def flight(rows):
    """``begin_flight`` … ``end_flight`` around a push AND its hand-off, on every path."""
    tags = begin_flight(rows)
    try:
        yield tags
    finally:
        end_flight(tags)


def _prune_pending(doc: dict, now: float) -> None:
    for tag in [t for t, r in doc["pending"].items() if now - float(r["at"]) > RECONCILE_S]:
        del doc["pending"][tag]


# The persisted reservation is written by ONE background worker, never by the producer (Hermes 7627,
# finding 2): reading, validating and fsync-replacing the store can be slow, and nothing may sit
# between a producer and its push. The producer queues its tags and pushes at once.
_reserve_q: queue.Queue = queue.Queue()
_reserve_worker: threading.Thread | None = None
_reserve_worker_lock = threading.Lock()


def reserve(rows, *, now: float | None = None) -> None:
    """Queue the persisted hand-off RESERVATION for ``rows``' tags and return AT ONCE: the producer
    only adds its tags to the in-flight set (`begin_flight`, the fence in this process) and pushes.
    The worker writes the reservation whenever it can, possibly after the push has started. Never
    raises and never waits."""
    try:
        tags = _tags(rows)
        if not tags:
            return
        _reserve_q.put_nowait((tags, time.time() if now is None else now))
        _ensure_reserve_worker()
    except Exception:  # noqa: BLE001 — never the row's or the push's problem
        log.warning(
            "hq dispatches: a hand-off reservation was not queued; the in-flight set fences"
        )


def _ensure_reserve_worker() -> None:
    global _reserve_worker
    worker = _reserve_worker
    if worker is not None and worker.is_alive():
        return
    with _reserve_worker_lock:
        if _reserve_worker is not None and _reserve_worker.is_alive():
            return
        _reserve_worker = threading.Thread(
            target=_reserve_loop, name="hq-dispatch-reserve", daemon=True
        )
        _reserve_worker.start()


def _reserve_loop() -> None:
    while True:
        tags, at = _reserve_q.get()
        try:
            _write_reservation(tags, at)
        except Exception:  # noqa: BLE001 — best-effort: the in-flight set still fences
            log.warning("hq dispatches: a hand-off reservation failed", exc_info=True)
        finally:
            _reserve_q.task_done()


def _drain_reservations(timeout: float = 10.0) -> bool:
    """Wait until every queued reservation has been handled (tests). True when idle."""
    deadline = time.monotonic() + timeout
    while _reserve_q.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)
    return True


def _write_reservation(tags: list[str], now: float, path: Path | None = None) -> bool:
    """The worker's write: ``pending[tag] = {pid, start, at}`` for the tags STILL being pushed, in
    ONE write with ONE non-blocking try of the store's lock (busy → skipped). A tag that has left
    the in-flight set (its hand-off ran, or failed and was released) or that already joined a
    bundle is skipped, so a reservation that lands after its hand-off can never recreate a pending
    entry: the hand-off joins under this same lock, and leaves the set only after it."""
    live = [t for t in tags if in_flight(t)]
    if not live:
        return False
    start = _proc_start(os.getpid())
    if start is None:
        return False
    p = path or _path()
    with _try_lock(p, RESERVE_WAIT_S) as taken:
        if not taken:
            log.info("hq dispatches: no persisted hand-off reservation (the store is busy)")
            return False
        doc = _read_strict(p)
        fresh = [t for t in live if t not in doc["member_of"] and in_flight(t)]
        if not fresh:
            return False
        for t in fresh:
            doc["pending"][t] = {"pid": os.getpid(), "start": start, "at": now}
        _prune_pending(doc, now)
        _write(doc, p)
        return True


def release(tag: object, *, path: Path | None = None) -> None:
    """Drop ``tag``'s reservation (a failed enqueue gives the event back to reconciliation). It runs
    after the push, outside any producer lock, so it may wait for the store. Best-effort and never
    raises: a live owner that can neither enqueue nor release leaves the event with its fixed text
    and no line — the documented fallback."""
    try:
        if not isinstance(tag, str):
            return
        p = path or _path()
        with json_write_lock(p):
            doc = _read_strict(p)
            if doc["pending"].pop(tag, None) is not None:
                _write(doc, p)
    except Exception:  # noqa: BLE001
        log.debug("hq dispatches: a reservation could not be released", exc_info=True)


def enqueue(
    tag: str,
    facts: dict,
    display: dict,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> str | None:
    """Join ``tag`` to a bundle with both of its snapshots, in ONE locked write. Returns the bundle
    id (an existing one when the tag already joined), or ``None`` for an invalid tag.

    Called by a producer AFTER its row write and push, wrapped on its own (it raises on an
    unreadable store or a failed write; the caller logs and swallows). Blocking."""
    from . import notifications, template_secrets

    if not notifications.valid_dispatch_tag(tag):
        log.warning("hq dispatches: an invalid tag was not enqueued")
        return None
    given = now
    _point("enqueue")
    p = path or _path()
    with template_secrets.registration_fence():
        secrets = _secrets_snapshot()
        cleaned = _clean_facts(facts, secrets)
        shown = _clean_display(display)
        with json_write_lock(p):
            # The join time is read UNDER the lock: read before it, a producer that waited while
            # another opened a bundle would see that bundle as opened in its future and start a
            # second one dated before it.
            now = time.time() if given is None else given
            doc = _read_strict(p)
            # The hand-off is done: its reservation goes in this same write.
            released = doc["pending"].pop(tag, None) is not None
            existing = doc["member_of"].get(tag)
            if isinstance(existing, str) and existing in doc["bundles"]:
                # Reconciled first (the reservation could not be written, or it went stale): the
                # producer's own enqueue-only facts UPGRADE the member while its bundle is not yet
                # claimed. A claimed bundle's input was already sent or is being sent: as is.
                upgraded = False
                if existing not in doc["claims"]:
                    have = doc["facts"].get(tag) or {}
                    extra = {k: cleaned[k] for k in ENQUEUE_ONLY if k in cleaned}
                    if any(have.get(k) != v for k, v in extra.items()):
                        doc["facts"][tag] = {**have, **extra}
                        upgraded = True
                if released or upgraded:
                    _write(doc, p)
                return existing
            bid = _join(doc, tag, cleaned, shown, now)
            _prune(doc, now)
            _prune_pending(doc, now)
            _write(doc, p)
    _point("joined")
    return bid


def enqueue_row(row: dict, extra: dict | None = None, *, now: float | None = None) -> str | None:
    """:func:`enqueue` for a row a producer just wrote: its ``dispatch`` tag, the reduced facts its
    structured fields give plus the producer's own ``extra`` (a name, an outcome), and its display
    snapshot. A row without a tag enqueues nothing."""
    tag = row.get("dispatch")
    if not isinstance(tag, str):
        return None
    facts = {**reduced_facts(row, now), **(extra or {})}
    return enqueue(tag, facts, display_of(row), now=now)


def announce(row: dict | None, extra: dict | None = None) -> None:
    """A producer's whole hand-off, after its push handling: enqueue (logged and swallowed) and
    wake the writer. Never raises."""
    if not row or not isinstance(row.get("dispatch"), str):
        return
    try:
        enqueue_row(row, extra)
    except Exception:  # noqa: BLE001 — the row and push stand; the reconcile pass covers this
        log.warning("hq dispatches: an enqueue failed; the next pass reconciles it", exc_info=True)
        # Its reservation would otherwise hide the event from reconciliation while we live. It is
        # released once the tag has LEFT the in-flight set (when its last producer's hold ends,
        # Hermes 7681), so a reservation still queued can no longer be written.
        _release_when_landed(row["dispatch"])
    poke()
    # The notify routes (#1841) read the same rows: wake them too. A failed poke never raises.
    with contextlib.suppress(Exception):
        from . import notify_routes

        notify_routes.poke()


# ---- the writer ---------------------------------------------------------------------------------


def _housekeep(now: float, path: Path | None = None) -> list[str]:
    """Reconcile, seal, give up and prune in ONE locked write. Returns the bundles to claim, oldest
    first. Raises StoreUnreadable (either store) — the pass is skipped.

    A reconciled row joins by its OWN event time (``row.ts``), never the pass's: after a writer
    outage, events hours apart stay in separate bundles, and a recovered bundle whose window has
    already passed seals on this same pass (Hermes 7619, finding 2). Its facts go through the same
    cleaner as an enqueue, with a secrets snapshot taken under the registration fence."""
    from . import notifications, template_secrets

    p = path or _path()
    # STRICT, and read before this store's lock (never under it): an unreadable notification store
    # skips the pass rather than guessing which rows exist.
    rows, _a, _e = notifications._load_doc_strict(notifications._notifications_path())
    with template_secrets.registration_fence():
        secrets = _secrets_snapshot()
        with json_write_lock(p):
            doc = _read_strict(p)
            changed = False
            floor = max(float(doc["reconcile_floor"]), now - RECONCILE_S)
            for row in sorted(rows, key=lambda r: float(r["ts"]) if _num(r.get("ts")) else 0):
                tag = row.get("dispatch")
                if not notifications.valid_dispatch_tag(tag) or tag in doc["member_of"]:
                    continue
                ts = row.get("ts")
                if not _num(ts) or float(ts) <= floor or float(ts) > now - RECONCILE_GRACE_S:
                    continue
                # A push this process is still running: its own hand-off takes the event, however
                # long the push takes (the authoritative fence for a live push here).
                if in_flight(tag):
                    continue
                held = doc["pending"].get(tag)
                if held is not None:
                    # Its owner process lives (same pid and start): never stale. A dead owner (a
                    # crash mid-push, a restart) is: reconciled now, its reservation dropped.
                    if _owner_alive(held):
                        continue
                    del doc["pending"][tag]
                facts = _clean_facts(reduced_facts(row, now), secrets)
                _join(doc, tag, facts, display_of(row), float(ts))
                changed = True
            before_pending = len(doc["pending"])
            _prune_pending(doc, now)
            changed = changed or len(doc["pending"]) != before_pending
            for rec in doc["bundles"].values():
                if not rec["sealed"] and (
                    now >= float(rec["seals_at"]) or len(rec["members"]) >= BUNDLE_MAX
                ):
                    rec["sealed"] = True
                    changed = True
            for c in doc["claims"].values():
                if c["state"] == "attempt" and now - float(c["at"]) >= GIVE_UP_S:
                    c.update({"state": "gave_up", "code": "gave_up", "at": now})
                    changed = True
            before = len(doc["bundles"])
            _prune(doc, now)
            changed = changed or len(doc["bundles"]) != before
            if changed:
                _write(doc, p)
            bundles = doc["bundles"]
            due = sorted(
                (b for b, rec in bundles.items() if rec["sealed"] and b not in doc["claims"]),
                key=lambda b: (float(bundles[b]["opened_at"]), b),
            )
    _point("sealed")
    return due


def _claim(bid: str, now: float, path: Path | None = None) -> list[dict] | None:
    """Take ``bid``'s claim — create-only, under the lock, BEFORE the model call. Returns the
    members' facts, or ``None`` when another worker has it (or it is gone or not sealed)."""
    p = path or _path()
    with json_write_lock(p):
        doc = _read_strict(p)
        rec = doc["bundles"].get(bid)
        if rec is None or not rec["sealed"] or bid in doc["claims"]:
            return None
        doc["claims"][bid] = {"state": "attempt", "at": now, "code": ""}
        _write(doc, p)
        return [dict(doc["facts"].get(t) or {}) for t in rec["members"]]


def _settle(
    bid: str, claimed_at: float, outcome: dict, now: float, path: Path | None = None
) -> bool:
    """Settle OUR claim: a line → ``written``; anything else → ``failed`` with its code. A claim
    that is no longer ours (given up meanwhile) is left as it is and nothing is written."""
    from . import ask_store, template_secrets

    p = path or _path()
    with template_secrets.registration_fence():
        secrets = _secrets_snapshot()
        with json_write_lock(p):
            doc = _read_strict(p)
            c = doc["claims"].get(bid)
            if c is None or c["state"] != "attempt" or float(c["at"]) != float(claimed_at):
                return False
            text = outcome.get("text")
            if isinstance(text, str) and text:
                if secrets is None:
                    c.update({"state": "failed", "code": "redaction_unavailable", "at": now})
                else:
                    guarded = ask_store.guard_text(text, LINE_MAX, secrets=secrets)
                    doc["lines"][bid] = {
                        "text": guarded,
                        "contact": str(outcome.get("contact") or "HQ"),
                        "at": now,
                    }
                    c.update({"state": "written", "code": "", "at": now})
            else:
                code = str(outcome.get("code") or "failed")
                c.update(
                    {"state": "failed", "code": code if _CODE_RE.fullmatch(code) else "failed"}
                )
                c["at"] = now
            _write(doc, p)
            return True


def _persona(contact: str) -> list[dict]:
    """The contact's persona as its own system message, BEFORE the guarded prompt — or nothing
    for ``off`` (plain HQ). An exact registry string, as on every HQ turn."""
    from . import hq_contacts, prompts

    if contact not in hq_contacts.IDS:
        return []
    return [{"role": "system", "content": prompts.effective(hq_contacts.prompt_id(contact))}]


def _speaker(now: float) -> str:
    from . import hq_contacts, prefs

    try:
        return hq_contacts.speaker(prefs.get_hq_contact(), now)
    except Exception:  # noqa: BLE001 — an unreadable pref speaks as plain HQ
        return hq_contacts.OFF


def _contact_name(contact: str) -> str:
    from . import hq_contacts

    return hq_contacts.get(contact).name if contact in hq_contacts.IDS else "HQ"


def _one_line(content: object) -> str | None:
    """The model's answer as ONE line: newlines and runs of space collapsed; ``None`` if empty."""
    if not isinstance(content, str):
        return None
    text = " ".join(content.split())
    return text or None


async def _write_line(members: list[dict], now: float) -> dict:
    """ONE bounded model call for a claimed bundle. Returns ``{"text", "contact"}`` or
    ``{"code"}``. Never raises."""
    from . import hq_usage, prompts, review

    try:
        cfg = review._require_config()  # HQ is set up: an endpoint, and its egress acknowledged
    except review.NotConfiguredError:
        return {"code": "unconfigured"}
    except Exception:  # noqa: BLE001
        return {"code": "unconfigured"}
    contact = _speaker(now)
    body = {
        "model": cfg["model"],
        "messages": [
            # The persona first, the guarded prompt LAST: its guard is the final instruction.
            *_persona(contact),
            {"role": "system", "content": prompts.effective("hq_notify")},
            {"role": "user", "content": json.dumps(members, ensure_ascii=False)},
        ],
        "temperature": 0,
        "stream": False,
        "max_tokens": MAX_TOKENS,
    }
    try:
        cap = min(float(CALL_CAP_S), float(review.request_timeout(cfg)))
        with hq_usage.hq_call():  # counted in HQ's usage, like every HQ turn
            # ONE request per claim, never a degrade retry (Hermes 7620, finding 1): a refusal
            # is this bundle's `failed`, not a second call.
            r = await asyncio.wait_for(
                review.post_chat_response(cfg, body, retry_without_thinking=False), timeout=cap
            )
    except TimeoutError:
        return {"code": "timeout"}
    except Exception:  # noqa: BLE001 — a transport failure is a failed line, nothing more
        return {"code": "error"}
    if r.status_code != 200:
        return {"code": f"http_{int(r.status_code)}"}
    _point("answered")
    # The SHAPE is checked before anything is read from it (Hermes 7619, finding 3): a 200 whose
    # body is not `{"choices": [{"message": {...}}]}` is this bundle's `failed: malformed`, never
    # an exception that would end the pass for the bundles after it.
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"code": "malformed"}
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    msg = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(msg, dict):
        return {"code": "malformed"}
    if msg.get("refusal"):
        return {"code": "refused"}
    if choice.get("finish_reason") == "length":
        return {"code": "cut_off"}
    content = msg.get("content")
    if content is not None and not isinstance(content, str):
        return {"code": "malformed"}
    text = _one_line(content)
    if text is None:
        return {"code": "empty"}
    if len(text) > LINE_MAX:
        return {"code": "too_long"}
    return {"text": text, "contact": _contact_name(contact)}


_flight = threading.Lock()


async def pass_once(now: float | None = None) -> dict:
    """One writer pass: reconcile, seal, give up, then claim and write each sealed, unclaimed
    bundle one at a time. Single-flight in this process (across processes, the claim is)."""
    if not _flight.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        t = time.time() if now is None else now
        from . import notifications

        try:
            due = await asyncio.to_thread(_housekeep, t)
        except (StoreUnreadable, notifications.StoreUnreadable):
            # Never rewritten, never guessed: the next pass tries again.
            log.warning("hq dispatches: a store is unreadable; the pass is skipped")
            return {"skipped": "store unreadable"}
        out = {"claimed": 0, "written": 0, "failed": 0}
        for bid in due:
            claimed_at = time.time() if now is None else now
            members = await asyncio.to_thread(_claim, bid, claimed_at)
            if members is None:
                continue
            out["claimed"] += 1
            _point("claimed")
            result = await _write_line(members, claimed_at)
            settled_at = time.time() if now is None else now
            await asyncio.to_thread(_settle, bid, claimed_at, result, settled_at)
            out["written" if result.get("text") else "failed"] += 1
        return out
    finally:
        _flight.release()


def loop_enabled() -> bool:
    """Env kill-switch. ``AGENT_SESSIONS_HQ_DISPATCH_LOOP=0`` stops the writer."""
    return (os.environ.get("AGENT_SESSIONS_HQ_DISPATCH_LOOP", "1") or "1") != "0"


_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def poke() -> None:
    """Run the next pass now. Thread-safe; a no-op when no writer is running."""
    wake, loop = _wake, _loop
    if wake is not None and loop is not None:
        with contextlib.suppress(RuntimeError):  # the loop has closed
            loop.call_soon_threadsafe(wake.set)


async def run() -> None:
    """The background writer. Exits at once under the kill-switch."""
    global _wake, _loop
    if not loop_enabled():
        return
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    while True:
        try:
            await pass_once()
        except Exception:  # noqa: BLE001 — never take the app down over a dispatch pass
            log.warning("hq dispatches: a pass failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=INTERVAL_S)
        _wake.clear()


# ---- the views ----------------------------------------------------------------------------------


def _short(name: str) -> str:
    """How the bell and the view sign a line: "Gen. Okafor" for "General Amara Okafor"."""
    parts = name.split()
    if len(parts) >= 2 and parts[0] == "General":
        return f"Gen. {parts[-1]}"
    return name or "HQ"


def _written(doc: dict) -> list[str]:
    """Only written lines are dispatches: a bundle whose claim is ``written`` and has a line."""
    return [
        b
        for b, c in doc["claims"].items()
        if c.get("state") == "written" and b in doc["lines"] and b in doc["bundles"]
    ]


def _key(doc: dict, bid: str) -> tuple[float, str]:
    return (float(doc["lines"][bid]["at"]), bid)


def _guard(value: object, cap: int, secrets: list[str] | None) -> str:
    from . import ask_store

    if not isinstance(value, str) or not value:
        return ""
    if secrets is None:
        return ask_store.WITHHELD
    try:
        return ask_store.guard_text(value, cap, secrets=secrets)
    except Exception:  # noqa: BLE001 — a field that cannot be checked is withheld
        return ask_store.WITHHELD


def _new_count(doc: dict) -> int:
    seen = (float(doc["seen"][0]), str(doc["seen"][1]))
    return sum(1 for b in _written(doc) if _key(doc, b) > seen)


def _entry(doc: dict, bid: str, secrets: list[str] | None, routes_doc: dict | None = None) -> dict:
    rec = doc["bundles"][bid]
    line = doc["lines"][bid]
    members = []
    for tag in rec["members"]:
        d = doc["display"].get(tag) or {}
        link = d.get("link")
        members.append(
            {
                "class": str(d.get("class") or class_of(tag) or ""),
                "title": _guard(d.get("title"), TITLE_MAX, secrets),
                "reason": _guard(d.get("reason"), REASON_MAX, secrets),
                "link": link if _valid_link(link) else "",
                "ts": float(d["ts"]) if _num(d.get("ts")) else 0.0,
            }
        )
    contact = str(line.get("contact") or "HQ")
    out = {
        "id": bid,
        "at": float(line["at"]),
        "opened_at": float(rec["opened_at"]),
        "line": _guard(line.get("text"), LINE_MAX, secrets),
        "contact": contact,
        "by": _short(contact),
        "members": members,
    }
    if routes_doc is not None:
        # Where the same events went (#1841): joined by member tag, omitted when unreadable.
        from . import notify_routes

        out["routes"] = notify_routes.dispatch_routes(routes_doc, list(rec["members"]))
    return out


def _routes_doc() -> dict | None:
    from . import notify_routes

    return notify_routes.view_doc()


def list_view(limit: int = LIST_MAX, path: Path | None = None) -> dict:
    """The newest written dispatches, newest first, with the unread count. Raises
    StoreUnreadable. Nothing is read from the notification rows."""
    doc = _read_strict(path)
    secrets = _secrets_snapshot()
    order = sorted(_written(doc), key=lambda b: _key(doc, b), reverse=True)
    routes_doc = _routes_doc()
    return {
        "dispatches": [_entry(doc, b, secrets, routes_doc) for b in order[:limit]],
        "new": _new_count(doc),
    }


def one_view(bid: str, path: Path | None = None) -> dict | None:
    """One written dispatch, or ``None`` (unknown, pruned, malformed or not written)."""
    if not isinstance(bid, str) or not BID_RE.fullmatch(bid):
        return None
    doc = _read_strict(path)
    if bid not in _written(doc):
        return None
    return {
        "dispatch": _entry(doc, bid, _secrets_snapshot(), _routes_doc()),
        "new": _new_count(doc),
    }


def mark_seen(through: object, path: Path | None = None) -> dict:
    """Move the unread cursor to ``through``'s ``(line at, bid)`` — CLAMPED: an id that is not a
    written bundle is ignored, and the cursor only ever moves forward. Raises StoreUnreadable."""
    p = path or _path()
    with json_write_lock(p):
        doc = _read_strict(p)
        moved = False
        if isinstance(through, str) and through in _written(doc):
            key = _key(doc, through)
            if key > (float(doc["seen"][0]), str(doc["seen"][1])):
                doc["seen"] = [key[0], key[1]]
                _write(doc, p)
                moved = True
        return {"moved": moved, "new": _new_count(doc)}


def join_listing(out: dict, path: Path | None = None) -> dict:
    """The bell's listing with HQ's line joined into each row as ``dispatch: {id, line, by,
    bundled}`` — only for a written bundle. A row's stored tag never reaches the client, and an
    unreadable dispatch store shows the rows without lines, never an error."""
    try:
        doc: dict | None = _read_strict(path)
    except Exception:  # noqa: BLE001 — fail soft: rows without lines
        doc = None
    written = set(_written(doc)) if doc is not None else set()
    secrets = _secrets_snapshot() if written else None

    def one(row: object) -> object:
        if not isinstance(row, dict) or "dispatch" not in row:
            return row
        r = {k: v for k, v in row.items() if k != "dispatch"}
        tag = row.get("dispatch")
        bid = doc["member_of"].get(tag) if doc is not None and isinstance(tag, str) else None
        if isinstance(bid, str) and bid in written:
            line = doc["lines"][bid]
            contact = str(line.get("contact") or "HQ")
            r["dispatch"] = {
                "id": bid,
                "line": _guard(line.get("text"), LINE_MAX, secrets),
                "by": _short(contact),
                "bundled": len(doc["bundles"][bid]["members"]),
            }
        return r

    res = dict(out)
    for k in ("notifications", "settled"):
        if isinstance(res.get(k), list):
            res[k] = [one(r) for r in res[k]]
    return res
