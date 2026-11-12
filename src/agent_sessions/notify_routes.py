"""Notify routes (#1841, #1746 P3b): a short copy of each bell event to the Commander's own
Telegram chat, Matrix DM or mail address, edited to "✓ resolved" when the event is handled.

A route is a messenger connector instance with ``enabled.notify`` and a ``notify`` block
(`connectors.store`). It is a SINK of the bell's own events: it reads only bell rows that carry a
``dispatch`` tag (#1837), never announces anything the bell didn't, never sends one event twice,
and never delays or replaces the bell or the push. Full contract:
``docs/invariants/notify-routes.md``.

**The ledger** is its own file (``AGENT_SESSIONS_NOTIFY_ROUTES_PATH``, 0600, `atomicjson` replace
under its own flock), read strictly: a document that exists but can't be read raises
:class:`StoreUnreadable`, no route sends, the views say so, and it is never rewritten. Shape::

    {"version": 1,
     "bundles":    {"<rb>": {"route", "opened_at", "seals_at", "sealed", "members": [<tag>, …]}},
     "member_of":  {"<route>|<tag>": "<rb>"},     # create-only: a tag joins ONE bundle per route
     "snap":       {"<route>|<tag>": {"class", "label", "link", "ts"}},
     "deliveries": {"<rb>": {"state", "claimed_at", "owner", "armed", "settled_at", "code",
                             "message_id", "generation", "destination", "connector", "counted",
                             "members", "sent", "resolved", "tried", "edits", "attempts",
                             "next_try"}},
     "evicted":    [{"rb", "route", "delivered_at", "evicted_at", "members": [{label, link}]}]}

**At most once per (route, bundle).** The delivery is a create-only claim written under the lock
before the provider call, with its owner (pid + process start). A second pass, in this process or
another, finds it and does nothing. A claim whose owner died after it ARMED the send (the write
just before the provider call) is ``unconfirmed`` and never re-sent; one that died before arming
sent nothing (``failed: interrupted``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets as _secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import hq_dispatch
from .atomicjson import atomic_write_json, json_write_lock

log = logging.getLogger("agent_sessions.notify_routes")

VERSION = 1
INTERVAL_S = 10
#: A bell row joins a route only while it is at most this old (and newer than the route's
#: ``since``). It is also what makes pruning safe: a pruned bundle's rows can never join again.
JOIN_MAX_AGE_S = 15 * 60
#: A held send (``held_unknown``) is decided by a later pass, or ``expired`` this long after its
#: newest member.
DELAY_CAP_S = 15 * 60
BUNDLE_MAX = 10
#: How long HQ's line is waited for when a route includes it (`hq_dispatch.GIVE_UP_S`).
LINE_WAIT_S = hq_dispatch.GIVE_UP_S
RATE_WINDOW_S = 3600
KEEP_S = 30 * 86400
KEEP_MAX = 5000
TOMBSTONES_MAX = 200
LIST_MAX = 50
#: A failed edit is retried on the reconcile pass after these delays, then given up.
EDIT_BACKOFF_S = (60, 300, 1800, 7200, 43200)
EDIT_ATTEMPTS_MAX = 5

RB_RE = re.compile(r"r_[0-9a-f]{16}")
_CODE_RE = re.compile(r"[a-z0-9_]{1,40}")

CLASS_NEEDS_YOU = hq_dispatch.CLASS_NEEDS_YOU
CLASS_ESCALATION = hq_dispatch.CLASS_ESCALATION
CLASS_AUTOMATION = hq_dispatch.CLASS_AUTOMATION
#: The classes a member can RESOLVE in. An automation failure never does: its copy is never edited.
RESOLVABLE = frozenset({CLASS_NEEDS_YOU, CLASS_ESCALATION})
#: Each class's fixed label: never a row's title, reason or any session text.
CLASS_LABEL = {
    CLASS_ESCALATION: "Mission escalation",
    CLASS_AUTOMATION: "Automation failed",
}
NEEDS_YOU_FALLBACK = "A session needs you"

DELIVERING = "delivering"
HELD = "held_unknown"
DELIVERED = "delivered"
RETRACT_FAILED = "retract_failed"
#: Not final: the reconcile pass may still act on them.
OPEN_STATES = frozenset({DELIVERING, HELD, DELIVERED, RETRACT_FAILED})
FINAL_STATES = frozenset(
    {
        "retracted", "skipped_closed", "skipped_quiet", "skipped_rate", "skipped_class",
        "expired", "failed", "unconfirmed", "retract_gave_up", "retract_evicted",
        "delivered_final", "retract_unsupported", "retract_lost", "retract_window_passed",
    }
)  # fmt: skip
STATES = OPEN_STATES | FINAL_STATES
#: The states in which the copy went out (its ``sent`` members reached the provider).
SENT_STATES = frozenset(
    {
        DELIVERED, RETRACT_FAILED, "retracted", "retract_gave_up", "retract_evicted",
        "delivered_final", "retract_unsupported", "retract_lost", "retract_window_passed",
    }
)  # fmt: skip


class StoreUnreadable(Exception):
    """The route ledger exists and can't be read or parsed. It is never rewritten."""


# ---- the ledger ---------------------------------------------------------------------------------


def _path() -> Path:
    override = os.environ.get("AGENT_SESSIONS_NOTIFY_ROUTES_PATH")
    if override:
        return Path(override)
    return hq_dispatch._path().with_name("notify-routes.json")


def _empty() -> dict:
    return {
        "version": VERSION,
        "bundles": {},
        "member_of": {},
        "snap": {},
        "deliveries": {},
        "evicted": [],
    }


_num = hq_dispatch._num


def _strs(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(t, str) for t in v)


def _valid_delivery(d: object) -> bool:
    if not isinstance(d, dict):
        return False
    owner = d.get("owner")
    return (
        d.get("state") in STATES
        and _num(d.get("claimed_at"))
        and (owner is None or (
            isinstance(owner, dict)
            and isinstance(owner.get("pid"), int)
            and not isinstance(owner.get("pid"), bool)
            and isinstance(owner.get("start"), str)
        ))
        and isinstance(d.get("armed"), bool)
        and (d.get("settled_at") is None or _num(d.get("settled_at")))
        and isinstance(d.get("code"), str)
        and (d["code"] == "" or _CODE_RE.fullmatch(d["code"]) is not None)
        and isinstance(d.get("message_id"), str)
        and len(d["message_id"]) <= 255
        and isinstance(d.get("generation"), int)
        and isinstance(d.get("destination"), str)
        and isinstance(d.get("connector"), str)
        and isinstance(d.get("counted"), bool)
        and _strs(d.get("members"))
        and _strs(d.get("sent"))
        and _strs(d.get("resolved"))
        and _strs(d.get("tried"))
        and isinstance(d.get("edits"), int)
        and isinstance(d.get("attempts"), int)
        and _num(d.get("next_try"))
    )  # fmt: skip


def _valid_snap(s: object) -> bool:
    return (
        isinstance(s, dict)
        and set(s) == {"class", "label", "link", "ts"}
        and s["class"] in hq_dispatch.FACT_KEYS
        and isinstance(s["label"], str)
        and len(s["label"]) <= 80
        and isinstance(s["link"], str)
        and (s["link"] == "" or hq_dispatch._valid_link(s["link"]))
        and _num(s["ts"])
    )


def _valid_tombstone(t: object) -> bool:
    return (
        isinstance(t, dict)
        and set(t) == {"rb", "route", "delivered_at", "evicted_at", "members"}
        and isinstance(t["rb"], str)
        and isinstance(t["route"], str)
        and _num(t["delivered_at"])
        and _num(t["evicted_at"])
        and isinstance(t["members"], list)
        and all(
            isinstance(m, dict)
            and set(m) == {"label", "link"}
            and isinstance(m["label"], str)
            and isinstance(m["link"], str)
            for m in t["members"]
        )
    )


def _shape(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("version") != VERSION or set(raw) != set(_empty()):
        raise StoreUnreadable("not a version-1 route ledger")
    doc = {k: raw[k] for k in _empty()}
    for k in ("bundles", "member_of", "snap", "deliveries"):
        if not isinstance(doc[k], dict):
            raise StoreUnreadable(f"{k} is not a mapping")
    if not isinstance(doc["evicted"], list) or not all(map(_valid_tombstone, doc["evicted"])):
        raise StoreUnreadable("an eviction record is malformed")
    for rb, b in doc["bundles"].items():
        if not (
            RB_RE.fullmatch(str(rb))
            and isinstance(b, dict)
            and set(b) == {"route", "opened_at", "seals_at", "sealed", "members"}
            and isinstance(b["route"], str)
            and _num(b["opened_at"])
            and _num(b["seals_at"])
            and isinstance(b["sealed"], bool)
            and _strs(b["members"])
            and len(b["members"]) <= BUNDLE_MAX
        ):
            raise StoreUnreadable("a bundle is malformed")
    if not all(isinstance(v, str) for v in doc["member_of"].values()):
        raise StoreUnreadable("a membership is malformed")
    if not all(map(_valid_snap, doc["snap"].values())):
        raise StoreUnreadable("a member's snapshot is malformed")
    for rb, d in doc["deliveries"].items():
        if rb not in doc["bundles"] or not _valid_delivery(d):
            raise StoreUnreadable("a delivery is malformed")
    # The cross-references every reader relies on (Hermes 7985): each member has its membership
    # and its snapshot, and a delivery names only its bundle's members. This module writes them in
    # one locked write; a hand-edited file that breaks one is unreadable, never a KeyError.
    for rb, b in doc["bundles"].items():
        for t in b["members"]:
            key = f"{b['route']}|{t}"
            if doc["member_of"].get(key) != rb or key not in doc["snap"]:
                raise StoreUnreadable("a bundle member has no membership or snapshot")
        d = doc["deliveries"].get(rb)
        if d is not None and not set(d["members"] + d["sent"] + d["resolved"] + d["tried"]) <= set(
            b["members"]
        ):
            raise StoreUnreadable("a delivery names an event outside its bundle")
    return doc


def read_strict(path: Path | None = None) -> dict:
    """The ledger as stored. Missing or empty is an empty ledger; anything else that can't be
    read, parsed or shaped raises :class:`StoreUnreadable`."""
    import json

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
    except (ValueError, RecursionError) as e:
        raise StoreUnreadable("not JSON") from e
    try:
        return _shape(raw)
    except StoreUnreadable:
        raise
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise StoreUnreadable("a value is malformed") from e


def _write(doc: dict, path: Path | None = None) -> None:
    atomic_write_json(path or _path(), doc, mode=0o600)


@contextlib.contextmanager
def _locked(path: Path | None = None):
    p = path or _path()
    with json_write_lock(p):
        yield p, read_strict(p)


# Test seams: the acceptance tests kill a real worker at a named point, and hold a send.
_POINT = None
_HOOK_BEFORE_SEND = None


def _point(name: str) -> None:
    hook = _POINT
    if hook is not None:
        hook(name)


# ---- who owns a claim ---------------------------------------------------------------------------


def _me() -> dict:
    pid = os.getpid()
    return {"pid": pid, "start": hq_dispatch._proc_start(pid) or ""}


def _mine(d: dict) -> bool:
    return d.get("owner") == _me()


def _alive(owner: dict | None) -> bool:
    return owner is not None and hq_dispatch._owner_alive(owner)


# ---- what a member is ---------------------------------------------------------------------------


def _label(row: dict, cls: str) -> str:
    """The fixed kind label, from the row's structured fields. A needs-you row's reason is
    already one of `notifications.KIND_REASON`'s fixed strings; anything else gets the fallback."""
    from . import notifications

    if cls == CLASS_NEEDS_YOU:
        reason = row.get("reason")
        return reason if reason in notifications.KIND_REASON.values() else NEEDS_YOU_FALLBACK
    return CLASS_LABEL[cls]


def _snapshot(row: dict, cls: str) -> dict:
    from . import notifications

    link = notifications._link(row)
    return {
        "class": cls,
        "label": _label(row, cls),
        "link": link if hq_dispatch._valid_link(link) else "",
        "ts": float(row["ts"]),
    }


class Resolver:
    """Each member's state from ONE strict snapshot per pass: ``open``, ``resolved`` or
    ``unknown``. An unreadable source is ``unknown``, never ``open`` and never ``resolved``."""

    def __init__(self, episodes: dict | None):
        from . import notifications

        self.episodes = episodes
        self.closed: set[str] = set()
        if episodes is not None:
            self.closed = {
                str(ep.get("tag"))
                for k, ep in episodes.items()
                if k.startswith(notifications.CLOSED) or k.startswith(notifications.CLOSING)
            }
        self._esc: dict[str, str] = {}

    def __call__(self, tag: str) -> str:
        cls = hq_dispatch.class_of(tag)
        if cls == CLASS_AUTOMATION:
            return "open"  # an automation failure has no settlement
        if cls == CLASS_NEEDS_YOU:
            if self.episodes is None:
                return "unknown"
            if tag in self.closed:
                return "resolved"
            sid = tag[len("needs-you:") :].rsplit(":", 1)[0]
            ep = self.episodes.get(sid)
            # No longer the session's open episode: closed, or evicted from the closed ledger by
            # `CLOSED_MAX` (it can't be open again: an episode tag is never reused).
            return "open" if isinstance(ep, dict) and ep.get("tag") == tag else "resolved"
        if cls == CLASS_ESCALATION:
            if tag not in self._esc:
                self._esc[tag] = _escalation_state(tag)
            return self._esc[tag]
        return "unknown"


def _escalation_state(tag: str) -> str:
    from . import missions

    try:
        _p, mission_id, digest, episode = tag.split(":")
        return (
            "resolved" if missions.escalation_settled(mission_id, digest, int(episode)) else "open"
        )
    except Exception:  # noqa: BLE001 — a store that can't be read decides nothing
        return "unknown"


def _bell() -> tuple[list[dict], dict] | None:
    """The bell's rows and episodes, strictly; None when the notification store is unreadable."""
    from . import notifications

    try:
        rows, _a, episodes = notifications._load_doc_strict(notifications._notifications_path())
    except notifications.StoreUnreadable:
        return None
    return rows, episodes


# ---- the message ------------------------------------------------------------------------------


def compose(
    members: list[tuple[str, dict]],
    resolved: set[str],
    lines: list[tuple[str, str, str]],
    origin: str,
) -> str:
    """The plain text of a copy: the fixed kind label and one link per member, then HQ's lines
    (``(signed by, line, bundle id)``) when the route includes them. A resolved member's line is
    prefixed ``✓``; when every member is resolved the header reads ``✓ resolved``. An automation
    failure is never marked, so a copy holding one keeps its header."""
    n = len(members)
    done = n > 0 and all(
        snap["class"] in RESOLVABLE and tag in resolved for tag, snap in members
    )  # fmt: skip
    head = "BattleLab · ✓ resolved" if done else f"BattleLab · {n} event{'s' if n != 1 else ''}"
    out = [head]
    for tag, snap in members:
        mark = "✓ " if snap["class"] in RESOLVABLE and tag in resolved else ""
        link = f" — {origin}{snap['link']}" if snap["link"] else ""
        out.append(f"• {mark}{snap['label']}{link}")
    for by, line, bid in lines:
        out.append(f"HQ ({by}): {line} — {origin}/hq/d/{bid}")
    return "\n".join(out)


def _clean(text: str) -> bool:
    """Whether ``text`` passes the HQ text boundary unchanged (no registered secret, nothing
    credential-shaped). Unavailable redaction is not clean."""
    from . import ask_store

    try:
        return ask_store.guard_text(text, len(text)) == text
    except Exception:  # noqa: BLE001 — a text that can't be checked is not sent
        return False


# ---- quiet hours and the cap --------------------------------------------------------------------


def in_quiet(quiet: dict | None, now: float) -> bool:
    """Whether ``now`` falls in the route's quiet window, in its own time zone. The window may
    cross midnight (``22:00``–``07:00``); its end is exclusive."""
    if not quiet:
        return False
    from zoneinfo import ZoneInfo

    local = datetime.fromtimestamp(now, ZoneInfo(quiet["tz"]))
    minute = local.hour * 60 + local.minute
    a = int(quiet["from"][:2]) * 60 + int(quiet["from"][3:])
    b = int(quiet["to"][:2]) * 60 + int(quiet["to"][3:])
    return a <= minute < b if a < b else (minute >= a or minute < b)


def _sent_this_hour(doc: dict, route: str, now: float) -> int:
    return sum(
        1
        for rb, d in doc["deliveries"].items()
        if d["counted"]
        and doc["bundles"][rb]["route"] == route
        and now - float(d["claimed_at"]) < RATE_WINDOW_S
    )


# ---- housekeeping: join, seal, claim, recover, prune --------------------------------------------


def _new_rb(doc: dict) -> str:
    rb = "r_" + _secrets.token_hex(8)
    while rb in doc["bundles"]:
        rb = "r_" + _secrets.token_hex(8)
    return rb


def _join(doc: dict, route: str, window_s: int, tag: str, snap: dict, now: float) -> str:
    """Join ``tag`` to the route's open bundle, or open one, in the caller's locked write.
    Create-only: the membership, the bundle's list and the snapshot land together."""
    key = f"{route}|{tag}"
    bundles = doc["bundles"]
    open_ = [
        b
        for b, rec in bundles.items()
        if rec["route"] == route
        and not rec["sealed"]
        and 0 <= now - float(rec["opened_at"]) < window_s
        and len(rec["members"]) < BUNDLE_MAX
    ]
    if open_:
        rb = max(open_, key=lambda b: (float(bundles[b]["opened_at"]), b))
    else:
        rb = _new_rb(doc)
        bundles[rb] = {
            "route": route,
            "opened_at": now,
            "seals_at": now + window_s,
            "sealed": False,
            "members": [],
        }
    bundles[rb]["members"].append(tag)
    doc["member_of"][key] = rb
    doc["snap"][key] = snap
    return rb


def _delivery(state: str, now: float, **kw) -> dict:
    d = {
        "state": state, "claimed_at": now, "owner": None, "armed": False, "settled_at": None,
        "code": "", "message_id": "", "generation": 0, "destination": "", "connector": "",
        "counted": False, "members": [], "sent": [], "resolved": [], "tried": [], "edits": 0,
        "attempts": 0, "next_try": 0,
    }  # fmt: skip
    d.update(kw)
    if state in FINAL_STATES and d["settled_at"] is None:
        d["settled_at"] = now
    return d


def _connector_label(inst: dict) -> str:
    from .connectors import manifest

    m = manifest.bundled().get(inst["manifest_id"])
    return m.label if m is not None else ""


def _claim(doc: dict, rb: str, routes: dict[str, dict], now: float) -> None:
    """Decide a sealed bundle with no delivery, create-only: skipped by the route's class
    toggles, quiet hours or hourly cap, or claimed ``delivering`` by this process."""
    b = doc["bundles"][rb]
    inst = routes.get(b["route"])
    if inst is None:
        doc["deliveries"][rb] = _delivery("failed", now, code="notify_off", members=b["members"])
        return
    n = inst["notify"]
    base = {
        "generation": inst["generation"],
        "destination": n["destination"],
        "connector": _connector_label(inst),
    }
    members = [t for t in b["members"] if doc["snap"][f"{b['route']}|{t}"]["class"] in n["classes"]]
    if not members:
        doc["deliveries"][rb] = _delivery("skipped_class", now, members=b["members"], **base)
    elif in_quiet(n["quiet"], now):
        doc["deliveries"][rb] = _delivery("skipped_quiet", now, members=members, **base)
    elif _sent_this_hour(doc, b["route"], now) >= n["cap_per_hour"]:
        doc["deliveries"][rb] = _delivery("skipped_rate", now, members=members, **base)
    else:
        doc["deliveries"][rb] = _delivery(
            DELIVERING, now, owner=_me(), counted=True, members=members, **base
        )


def _owes_edit(doc: dict, rb: str) -> bool:
    """A copy that went out with a resolvable member still unresolved: its edit is owed."""
    d = doc["deliveries"].get(rb)
    if d is None or d["state"] not in (DELIVERED, RETRACT_FAILED):
        return False
    route = doc["bundles"][rb]["route"]
    return any(
        doc["snap"][f"{route}|{t}"]["class"] in RESOLVABLE and t not in d["resolved"]
        for t in d["sent"]
    )


def _newest_ts(doc: dict, rb: str) -> float:
    route = doc["bundles"][rb]["route"]
    return max(float(doc["snap"][f"{route}|{t}"]["ts"]) for t in doc["bundles"][rb]["members"])


def _drop(doc: dict, rb: str) -> None:
    b = doc["bundles"].pop(rb)
    doc["deliveries"].pop(rb, None)
    for t in b["members"]:
        key = f"{b['route']}|{t}"
        doc["member_of"].pop(key, None)
        doc["snap"].pop(key, None)


def _tombstone(doc: dict, rb: str, now: float) -> None:
    b, d = doc["bundles"][rb], doc["deliveries"][rb]
    doc["evicted"].append(
        {
            "rb": rb,
            "route": b["route"],
            "delivered_at": float(d["settled_at"] or d["claimed_at"]),
            "evicted_at": now,
            "members": [
                {
                    "label": doc["snap"][f"{b['route']}|{t}"]["label"],
                    "link": doc["snap"][f"{b['route']}|{t}"]["link"],
                }
                for t in d["sent"]
            ],
        }
    )
    del doc["evicted"][:-TOMBSTONES_MAX]


def _prune(doc: dict, now: float) -> None:
    """Age out, then bound the ledger (`KEEP_MAX`). A bundle is only ever pruned once none of its
    rows can join again (each is past `JOIN_MAX_AGE_S`) and it is final, so pruning can never let
    an event be announced twice. Rows that owe no edit go first, oldest first; one that owes an
    edit is given up only when every kept row owes one, and then it settles ``retract_evicted``
    and leaves a tombstone first, never silently."""
    for d in doc["deliveries"].values():
        if d["state"] in (DELIVERED, RETRACT_FAILED):
            if now - float(d["settled_at"] or d["claimed_at"]) > KEEP_S:
                d.update(state="retract_window_passed", settled_at=now)
    doc["evicted"] = [t for t in doc["evicted"] if now - float(t["evicted_at"]) <= KEEP_S][
        -TOMBSTONES_MAX:
    ]

    def prunable(rb: str) -> bool:
        d = doc["deliveries"].get(rb)
        return (
            d is not None
            and (d["state"] in FINAL_STATES or _owes_edit(doc, rb))
            and now - _newest_ts(doc, rb) > JOIN_MAX_AGE_S
        )

    for rb in [
        rb
        for rb, d in doc["deliveries"].items()
        if d["state"] in FINAL_STATES
        and now - float(d["settled_at"] or d["claimed_at"]) > KEEP_S
        and prunable(rb)
    ]:
        _drop(doc, rb)
    over = len(doc["bundles"]) - KEEP_MAX
    if over <= 0:
        return
    order = sorted(
        (rb for rb in doc["bundles"] if prunable(rb)),
        key=lambda rb: (_owes_edit(doc, rb), float(doc["bundles"][rb]["opened_at"]), rb),
    )
    for rb in order[:over]:
        if _owes_edit(doc, rb):
            _tombstone(doc, rb, now)
            doc["deliveries"][rb].update(state="retract_evicted", settled_at=now)
        _drop(doc, rb)


def _housekeep(
    now: float, routes: dict[str, dict], bell: tuple[list[dict], dict] | None
) -> list[str]:
    """Join, seal, claim, recover and prune in ONE locked write. Returns the bundles THIS process
    now owns as ``delivering``. Raises StoreUnreadable (the pass is skipped)."""
    from . import notifications

    with _locked() as (p, doc):
        before = repr(doc)
        # 1. JOIN: only bell rows carrying a valid tag, of a class the route carries, newer than
        #    its `since` and at most `JOIN_MAX_AGE_S` old, not yet a member, and not in this
        #    process's push in-flight set (the device push goes first). No bell, no joins.
        if bell is not None:
            rows, _eps = bell
            for route, inst in routes.items():
                n = inst["notify"]
                for row in rows:
                    tag = row.get("dispatch")
                    if not notifications.valid_dispatch_tag(tag):
                        continue
                    cls = hq_dispatch.class_of(tag)
                    ts = row.get("ts")
                    if cls not in n["classes"] or not _num(ts):
                        continue
                    if not (float(ts) > float(n["since"]) and now - float(ts) <= JOIN_MAX_AGE_S):
                        continue
                    if f"{route}|{tag}" in doc["member_of"] or hq_dispatch.in_flight(tag):
                        continue
                    _join(doc, route, n["window_s"], tag, _snapshot(row, cls), now)
        # 2. SEAL: at the window's end, or at once when full (a window of 0 seals at once).
        for b in doc["bundles"].values():
            if not b["sealed"] and (now >= float(b["seals_at"]) or len(b["members"]) >= BUNDLE_MAX):
                b["sealed"] = True
        # 3. CLAIM each sealed bundle with no delivery (create-only).
        for rb, b in doc["bundles"].items():
            if b["sealed"] and rb not in doc["deliveries"]:
                _claim(doc, rb, routes, now)
        # 4. RECOVER: a dead owner's claim; a held send decided again, or expired.
        for rb, d in doc["deliveries"].items():
            if d["state"] == DELIVERING and not _mine(d) and not _alive(d["owner"]):
                if d["armed"]:
                    d.update(state="unconfirmed", code="interrupted", settled_at=now)
                else:
                    d.update(state="failed", code="interrupted", settled_at=now)
            elif d["state"] == HELD:
                if now - _newest_ts(doc, rb) > DELAY_CAP_S:
                    d.update(state="expired", settled_at=now)
                elif doc["bundles"][rb]["route"] not in routes:
                    d.update(state="failed", code="notify_off", settled_at=now)
                else:
                    d.update(state=DELIVERING, owner=_me(), armed=False)
        _prune(doc, now)
        if repr(doc) != before:
            _write(doc, p)
        return [
            rb
            for rb, d in doc["deliveries"].items()
            if d["state"] == DELIVERING and _mine(d) and not d["armed"]
        ]


def _settle(rb: str, *, armed_only: bool = False, **fields) -> bool:
    """Settle THIS process's ``delivering`` claim. False (nothing written) when it is no longer
    ours or no longer delivering."""
    with _locked() as (p, doc):
        d = doc["deliveries"].get(rb)
        if d is None or d["state"] != DELIVERING or not _mine(d):
            return False
        if armed_only and d["armed"]:
            return False
        if fields.get("state") in FINAL_STATES or fields.get("state") == DELIVERED:
            fields.setdefault("settled_at", time.time())
        d.update(fields)
        _write(doc, p)
        return True


# ---- the send -----------------------------------------------------------------------------------


def _bound(inst: dict, mode: str, destination: str):
    from . import connector_http
    from .connectors import manifest

    m = manifest.bundled()[inst["manifest_id"]]
    return connector_http.Bound(
        instance_id=inst["id"],
        generation=inst["generation"],
        origin=inst["origin"],
        auth_scheme=m.auth_scheme,
        label=m.label,
        adapter=m.adapter,
        for_read=False,
        for_notify=mode,
        destination=destination,
    )


async def _identity(bound, inst: dict, room: str) -> str:
    """A Matrix destination checked against its pin: ``same``, ``changed`` (another member, a
    third one, the pinned one gone, or the room now encrypted) or ``unknown`` (it couldn't be
    read — never "same")."""
    from . import connector_http
    from .connectors.adapters import matrix

    try:
        facts = await matrix.room_facts(bound, room)
    except connector_http.ConnectorError:
        return "unknown"
    why = facts.get("unavailable") or ""
    if why.startswith("encrypted"):
        return "changed"
    ids = facts.get("member_ids")
    if why or ids is None:
        return "unknown"
    pinned = sorted([inst["tested"]["login"], inst["notify"]["identity"]["member"]])
    return "same" if ids == pinned else "changed"


def matrix_pin(facts: dict, bot: str) -> tuple[str | None, str]:
    """At enable: ``(the one other member to pin, or None; why not, in words)``."""
    why = facts.get("unavailable") or ""
    ids = facts.get("member_ids")
    if why.startswith("encrypted"):
        return None, "That room is encrypted, which isn't supported yet."
    if why or ids is None:
        return None, "We couldn't check who is in that room. Try again in a moment."
    others = [u for u in ids if u != bot]
    if bot not in ids or len(others) != 1:
        count = facts.get("members") or len(ids)
        return None, (
            f"This room has {count} people in it. Notifications go only to a room of just you "
            "and the bot."
        )
    return others[0], ""


def _final_check(inst_id: str, generation: int, destination: str, text: str, mode: str):
    """The check the transport runs at the outbound boundary (twice, as for a tapped message,
    `ask_messages._final_check`): the route still on to the same destination at the same
    generation (or, for an edit, the copy's own generation and destination), and no secret in
    the text now. A refusal is proven unsent."""
    from . import connector_http, template_secrets
    from .connectors import store

    def refuse(code: str) -> None:
        raise connector_http.ConnectorError(code, "the route changed; nothing was sent")

    def check(fenced: bool) -> None:
        fence = template_secrets.registration_fence() if fenced else contextlib.nullcontext()
        with fence:
            try:
                doc = store.read()
            except store.StoreUnreadable:
                refuse("store_unreadable")
            inst = next((i for i in doc["instances"] if i["id"] == inst_id), None)
            if inst is None:
                refuse("connector_removed")
            if mode == "send":
                if not store.notify_on(inst) or inst["generation"] != generation:
                    refuse("notify_off")
                if inst["notify"]["destination"] != destination:
                    refuse("destination_changed")
            else:
                why = store.notify_edit_ok(inst, generation, destination)
                if why is not None:
                    refuse(why)
            if not _clean(text):
                refuse("secret")

    return check


def _hq_lines(
    members: list[str], sealed_at: float, now: float
) -> list[tuple[str, str, str]] | None:
    """HQ's written lines for these members, ``None`` while one is still being written (within
    `LINE_WAIT_S` of sealing). After that, what is written goes and the rest doesn't."""
    try:
        doc = hq_dispatch._read_strict()
    except hq_dispatch.StoreUnreadable:
        doc = None
    out: list[tuple[str, str, str]] = []
    waiting = False
    for tag in members:
        bid = doc["member_of"].get(tag) if doc is not None else None
        claim = doc["claims"].get(bid) if doc is not None and isinstance(bid, str) else None
        state = claim.get("state") if isinstance(claim, dict) else None
        if state not in ("written", "failed", "gave_up"):
            waiting = True
            continue
        if state == "written" and bid in doc["lines"] and bid not in {b for _x, _y, b in out}:
            line = doc["lines"][bid]
            text = " ".join(str(line.get("text") or "").split())[: hq_dispatch.LINE_MAX]
            if text:
                out.append((hq_dispatch._short(str(line.get("contact") or "HQ")), text, bid))
    if waiting and now - sealed_at < LINE_WAIT_S:
        return None
    return out


async def _deliver(rb: str, now: float) -> str:
    """Send one claim this process owns, or leave it for the next pass (waiting for HQ's line).
    Returns what happened, for the pass summary."""
    from . import connector_http, connector_smtp
    from .connectors import store

    doc = await asyncio.to_thread(read_strict)
    d, b = doc["deliveries"].get(rb), doc["bundles"].get(rb)
    if d is None or d["state"] != DELIVERING or not _mine(d) or d["armed"]:
        return "none"
    try:
        conn = await asyncio.to_thread(store.read)
    except store.StoreUnreadable:
        return "none"  # decided on a later pass
    inst = next((i for i in conn["instances"] if i["id"] == b["route"]), None)
    if inst is None or not store.notify_on(inst) or inst["generation"] != d["generation"]:
        await asyncio.to_thread(_settle, rb, state="failed", code="notify_off")
        return "failed"
    n = inst["notify"]
    lines: list[tuple[str, str, str]] = []
    if n["include_line"]:
        got = await asyncio.to_thread(_hq_lines, d["members"], float(b["seals_at"]), now)
        if got is None:
            return "waiting"
        lines = got
    # THE STRICT RE-READ, immediately before the send: unknown is never open.
    bell = await asyncio.to_thread(_bell)
    resolve = Resolver(bell[1] if bell is not None else None)
    states = {t: await asyncio.to_thread(resolve, t) for t in d["members"]}
    if "unknown" in states.values():
        await asyncio.to_thread(_settle, rb, state=HELD, owner=None)
        return "held"
    live = [t for t in d["members"] if states[t] == "open"]
    if not live:
        await asyncio.to_thread(_settle, rb, state="skipped_closed")
        return "skipped"
    origin = store.app_origin()
    if not origin:
        await asyncio.to_thread(_settle, rb, state="failed", code="needs_origin")
        return "failed"
    members = [(t, doc["snap"][f"{b['route']}|{t}"]) for t in live]
    text = compose(members, set(), lines, origin)
    if not await asyncio.to_thread(_clean, text):
        await asyncio.to_thread(_settle, rb, state="failed", code="secret")
        return "failed"
    dest = d["destination"]
    bound = _bound(inst, "send", dest)
    if bound.adapter == "matrix-cs":
        who = await _identity(bound, inst, dest)
        if who == "unknown":
            await asyncio.to_thread(_settle, rb, state=HELD, owner=None)
            return "held"
        if who == "changed":
            await asyncio.to_thread(_settle, rb, state="failed", code="destination_changed")
            return "failed"
    # ARMED: from here a kill can't be told apart from one after the provider's write.
    if not await asyncio.to_thread(_settle, rb, armed_only=True, armed=True):
        return "none"
    _point("armed")
    if _HOOK_BEFORE_SEND is not None:
        await _HOOK_BEFORE_SEND()
    check = _final_check(inst["id"], d["generation"], dest, text, "send")
    try:
        if bound.adapter == "smtp":
            idem = hashlib.sha256(rb.encode()).hexdigest()[:32]
            out = await connector_smtp._send(
                bound, dest, text, idem, inst.get("sender") or "", final_check=check
            )
        else:

            async def recheck() -> bool:
                return await _identity(bound, inst, dest) == "same"

            out = await connector_http._send(
                bound,
                dest,
                text,
                idem_key=rb,
                final_check=check,
                recheck=recheck if bound.adapter == "matrix-cs" else None,
            )
    except Exception:  # noqa: BLE001 — every armed send ends settled, never re-sent
        log.warning("notify routes: a send ended unexpectedly", exc_info=True)
        out = {"outcome": "unconfirmed", "code": "error", "message_id": ""}
    _point("sent")
    if out["outcome"] == "sent":
        resolvable = any(doc["snap"][f"{b['route']}|{t}"]["class"] in RESOLVABLE for t in live)
        state = DELIVERED
        if not resolvable:
            state = "delivered_final"
        elif bound.adapter == "smtp":
            state = "retract_unsupported"  # mail can't be taken back
        await asyncio.to_thread(
            _settle, rb, state=state, sent=live, message_id=str(out.get("message_id") or "")[:255]
        )
        return "delivered"
    code = str(out.get("code") or "")[:40]
    code = code if _CODE_RE.fullmatch(code) else "error"
    state = "failed" if out["outcome"] == "failed" else "unconfirmed"
    await asyncio.to_thread(_settle, rb, state=state, code=code)
    return state


# ---- reconcile: mark delivered copies -----------------------------------------------------------


async def _reconcile(now: float, conn: dict, bell: tuple[list[dict], dict] | None) -> int:
    """Walk the LEDGER's open copies (not the enabled routes, so a row outlives its route):
    settle ``retract_lost`` when the connector changed or went, else edit each copy once for a
    change in what is resolved, retrying a failed edit on its backoff. An unreadable notification
    store skips the whole reconcile. Returns the edits attempted."""
    from . import connector_http
    from .connectors import store

    if bell is None:
        return 0
    doc = await asyncio.to_thread(read_strict)
    instances = {i["id"]: i for i in conn["instances"]}
    resolve = Resolver(bell[1])
    tried = 0
    for rb, d in list(doc["deliveries"].items()):
        if d["state"] not in (DELIVERED, RETRACT_FAILED):
            continue
        route = doc["bundles"][rb]["route"]
        inst = instances.get(route)
        lost = (
            "connector_removed"
            if inst is None
            else store.notify_edit_ok(inst, d["generation"], d["destination"])
        )
        if lost is not None:
            await asyncio.to_thread(_settle_copy, rb, d, state="retract_lost", code=lost)
            continue
        states = {t: resolve(t) for t in d["sent"]}
        if "unknown" in states.values():
            continue
        resolved = sorted(t for t in d["sent"] if states[t] == "resolved")
        if d["state"] == RETRACT_FAILED:
            # Retried on its backoff whether or not anything changed again (a failure on the
            # FINAL member's edit has no later change to wait for), and at once on a new change.
            due = now >= float(d["next_try"]) or set(resolved) != set(d["tried"])
        else:
            due = bool(set(resolved) - set(d["resolved"]))
        if not due:
            continue
        members = [(t, doc["snap"][f"{route}|{t}"]) for t in d["sent"]]
        origin = store.app_origin()
        text = compose(members, set(resolved), [], origin) if origin else ""
        bound = _bound(inst, "edit", d["destination"])
        out: dict
        if not text or bound.adapter not in connector_http.EDITS:
            out = {"outcome": "failed", "code": "unsupported"}
        else:
            tried += 1
            check = _final_check(inst["id"], d["generation"], d["destination"], text, "edit")
            key = f"{rb}-e{d['edits'] + 1}-{len(resolved)}"
            try:
                out = await connector_http._edit(
                    bound, d["destination"], d["message_id"], text, key, final_check=check
                )
            except Exception:  # noqa: BLE001 — a failed edit is retried on its backoff
                out = {"outcome": "unconfirmed", "code": "error"}
        if out["outcome"] == "sent":
            owed = any(
                doc["snap"][f"{route}|{t}"]["class"] in RESOLVABLE and t not in resolved
                for t in d["sent"]
            )
            await asyncio.to_thread(
                _settle_copy,
                rb,
                d,
                state=DELIVERED if owed else "retracted",
                resolved=resolved,
                tried=[],
                edits=d["edits"] + 1,
                attempts=0,
                next_try=0,
                code="",
            )
        else:
            attempts = d["attempts"] + 1
            code = str(out.get("code") or "error")[:40]
            code = code if _CODE_RE.fullmatch(code) else "error"
            if attempts >= EDIT_ATTEMPTS_MAX:
                await asyncio.to_thread(
                    _settle_copy, rb, d, state="retract_gave_up", attempts=attempts, code=code,
                    tried=resolved,
                )  # fmt: skip
            else:
                await asyncio.to_thread(
                    _settle_copy,
                    rb,
                    d,
                    state=RETRACT_FAILED,
                    attempts=attempts,
                    tried=resolved,
                    code=code,
                    next_try=now + EDIT_BACKOFF_S[attempts - 1],
                )
    return tried


def _settle_copy(rb: str, seen: dict, **fields) -> bool:
    """Record an edit's outcome, only if the copy is still as this pass read it."""
    with _locked() as (p, doc):
        d = doc["deliveries"].get(rb)
        if d is None or d["state"] != seen["state"] or d["edits"] != seen["edits"]:
            return False
        if d["attempts"] != seen["attempts"]:
            return False
        if fields.get("state") in FINAL_STATES:
            fields["settled_at"] = time.time()
        d.update(fields)
        _write(doc, p)
        return True


# ---- the worker ---------------------------------------------------------------------------------

_flight = threading.Lock()


def _has_open(doc: dict) -> bool:
    return any(d["state"] in OPEN_STATES for d in doc["deliveries"].values()) or any(
        not b["sealed"] or rb not in doc["deliveries"] for rb, b in doc["bundles"].items()
    )


async def pass_once(now: float | None = None) -> dict:
    """One pass: join, seal, claim, recover, prune; send each claim this process owns; then
    reconcile. Single-flight in this process (across processes, the claim is). An idle pass
    (no route, no open row) reads the ledger and the connector store and does nothing else."""
    from .connectors import store

    if not _flight.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        t = time.time() if now is None else now
        try:
            ledger = await asyncio.to_thread(read_strict)
            conn = await asyncio.to_thread(store.read)
        except (StoreUnreadable, store.StoreUnreadable):
            log.warning("notify routes: a store is unreadable; the pass is skipped")
            return {"skipped": "store unreadable"}
        routes = {i["id"]: i for i in conn["instances"] if store.notify_on(i)}
        if not routes and not _has_open(ledger):
            return {"idle": True}
        bell = await asyncio.to_thread(_bell)
        try:
            due = await asyncio.to_thread(_housekeep, t, routes, bell)
        except StoreUnreadable:
            return {"skipped": "store unreadable"}
        _point("housekept")
        out: dict = {"claimed": len(due)}
        for rb in due:
            what = await _deliver(rb, time.time() if now is None else now)
            out[what] = out.get(what, 0) + 1
        out["edits"] = await _reconcile(t, conn, bell)
        return out
    finally:
        _flight.release()


def loop_enabled() -> bool:
    """Env kill-switch. ``AGENT_SESSIONS_NOTIFY_ROUTES_LOOP=0`` stops the worker."""
    return (os.environ.get("AGENT_SESSIONS_NOTIFY_ROUTES_LOOP", "1") or "1") != "0"


_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def poke() -> None:
    """Run the next pass now. Thread-safe; never raises; a no-op when no worker runs."""
    wake, loop = _wake, _loop
    if wake is not None and loop is not None:
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(wake.set)


async def run() -> None:
    """The background worker, a pass every `INTERVAL_S` or when poked. Exits at once under the
    kill switch."""
    global _wake, _loop
    if not loop_enabled():
        return
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    while True:
        try:
            await pass_once()
        except Exception:  # noqa: BLE001 — never take the app down over a route pass
            log.warning("notify routes: a pass failed", exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=INTERVAL_S)
        _wake.clear()


# ---- the views ----------------------------------------------------------------------------------


def deliveries(route: str, limit: int = LIST_MAX, path: Path | None = None) -> list[dict]:
    """The route's newest deliveries, tombstones of evicted ones merged in, newest first: state,
    times, code and each member's fixed label and link. Never the message text. Raises
    StoreUnreadable."""
    doc = read_strict(path)
    out: list[dict] = []
    for rb, d in doc["deliveries"].items():
        b = doc["bundles"][rb]
        if b["route"] != route:
            continue
        members = []
        for t in d["members"] or b["members"]:
            s = doc["snap"][f"{route}|{t}"]
            members.append(
                {
                    "label": s["label"],
                    "link": s["link"],
                    "sent": t in d["sent"],
                    "resolved": t in d["resolved"],
                }
            )
        out.append(
            {
                "id": rb,
                "state": d["state"],
                "at": float(d["claimed_at"]),
                "settled_at": d["settled_at"],
                "code": d["code"],
                "members": members,
            }
        )
    for tomb in doc["evicted"]:
        if tomb["route"] == route:
            out.append(
                {
                    "id": tomb["rb"],
                    "state": "retract_evicted",
                    "at": float(tomb["delivered_at"]),
                    "settled_at": float(tomb["evicted_at"]),
                    "code": "",
                    "members": [{**m, "sent": True, "resolved": False} for m in tomb["members"]],
                }
            )
    out.sort(key=lambda e: (e["at"], e["id"]), reverse=True)
    return out[:limit]


def view_doc() -> dict | None:
    """The ledger for a Dispatches read, or None when it is unreadable (the routes are omitted)."""
    try:
        return read_strict()
    except StoreUnreadable:
        return None


def dispatch_routes(doc: dict, tags: list[str]) -> list[dict]:
    """Per route, how a dispatch's events went out, joined by MEMBER TAG (never by ``sent``): a
    route that skipped them shows its skip, and only a tag in a delivery's ``sent`` reads as sent.
    A member left out of a copy that went out reads ``skipped_closed``."""
    per: dict[str, dict] = {}
    for key, rb in doc["member_of"].items():
        route, _, tag = key.partition("|")
        if tag not in tags or rb not in doc["bundles"]:
            continue
        d = doc["deliveries"].get(rb)
        if d is None:
            state, sent, resolved, label = "pending", False, False, ""
        elif tag in d["sent"]:
            state, sent, label = d["state"], True, d["connector"]
            resolved = tag in d["resolved"] or d["state"] == "retracted"
        else:
            state = "skipped_closed" if d["state"] in SENT_STATES else d["state"]
            sent, resolved, label = False, False, d["connector"]
        have = per.get(route)
        if have is None or (sent and not have["sent"]):
            per[route] = {
                "instance": route,
                "connector": label or (have or {}).get("connector") or "",
                "state": state,
                "sent": sent,
                "resolved": resolved,
            }
    return [per[k] for k in sorted(per)]
