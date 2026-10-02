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
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .atomicjson import fsync_dir

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


#: How many announcement tombstones the store keeps, newest last. A tombstone only has to outlive
#: the ledger row that could drive a repair, and the ledger keeps `HISTORY_MAX` (500) terminal rows
#: — so this is ample, and it is what stops the set growing without bound on a long-lived install.
ANNOUNCED_MAX = 1000

#: How many reclamation candidates one insert may try to converge. Bounds the work a single
#: announcement does while holding this store's lock: without it, a store whose receipts are all
#: failing would attempt the whole list on every write.
RECLAIM_ATTEMPTS = 8


def _load(path: Path) -> tuple[list[dict], list[str]]:
    """``(rows, announced)`` from either document shape. See :func:`_load_doc` for the episodes."""
    rows, announced, _episodes = _load_doc(path)
    return rows, announced


class StoreUnreadable(Exception):
    """An EXISTING notifications document could not be read or parsed (#1086 Phase 4, Hermes
    5231). A mutation must not replace it: the rows, the announcement tombstones and the open
    needs-you episodes it holds would be destroyed by a write built from an invented empty
    predecessor. Reads for display stay fail-soft (:func:`_load_doc`)."""


def _load_doc_strict(path: Path) -> tuple[list[dict], list[str], dict[str, dict]]:
    """:func:`_load_doc` for a MUTATION: a missing file is empty (knowable), anything else that
    cannot be read or parsed raises :class:`StoreUnreadable` rather than answering empty."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [], [], {}
    except (OSError, UnicodeDecodeError) as e:
        raise StoreUnreadable(type(e).__name__) from e
    if not text.strip():
        return [], [], {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise StoreUnreadable("not JSON") from e
    if not isinstance(raw, list | dict):
        raise StoreUnreadable("neither a list nor a document")
    return _shape(raw)


def _shape(raw: object) -> tuple[list[dict], list[str], dict[str, dict]]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)], [], {}
    if isinstance(raw, dict):
        rows, seen, eps = raw.get("rows"), raw.get("announced"), raw.get("episodes")
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [],
            [a for a in seen if isinstance(a, str)] if isinstance(seen, list) else [],
            {
                k: v
                for k, v in (eps.items() if isinstance(eps, dict) else ())
                if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("tag"), str)
            },
        )
    return [], [], {}


def _load_doc(path: Path) -> tuple[list[dict], list[str], dict[str, dict]]:
    """``(rows, announced, episodes)`` from either document shape.

    The store was a bare JSON list and still is whenever there is nothing to remember. It becomes
    ``{"rows": [...], "announced": [...]}`` only once an announcement tombstone exists, so the
    push-subscriptions file — which shares these helpers — and every notifications file written
    before #983 P4 keep exactly the bytes they had.
    """
    if not path.exists():
        return [], [], {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return [], [], {}
    return _shape(raw)


def _read_strict(path: Path) -> list[dict]:
    """The rows a MUTATION starts from: :func:`_load_doc_strict`'s, so a transient read failure
    raises instead of starting the replacement from an empty list (Hermes 5239, finding 2)."""
    return _load_doc_strict(path)[0]


def _read(path: Path) -> list[dict]:
    return _load(path)[0]


def _remember(
    announced: list[str], action_id: str, record: Callable[[str], bool] | None = None
) -> list[str]:
    """`announced` plus `action_id`, reclaiming only identities whose receipt is DURABLE.

    **Converge, then reclaim.** Before FIFO drops an identity this tries to make that identity's
    receipt durable, and refuses to drop it if that fails, moving on to the next candidate. So the
    list never loses an identity the authority cannot vouch for, and pressure on the list actively
    repairs receipts instead of silently discarding the only record of an announcement.

    That is stronger than the plain FIFO it replaces, and the difference is reachable with a
    perfectly healthy store (#983 P4, Hermes 4908). The intervening announcements need not be new
    autonomous sends: the repair sweep visits every RETAINED delivery once per pass and the
    compaction pin holds unannounced ones beyond ordinary retention, so an earlier bell-store
    outage supplies a backlog big enough to roll an identity out after a single transient receipt
    failure — even though the writer recovered immediately afterwards.

    The incoming identity is appended BEFORE any reclamation runs and is never itself a candidate,
    which is what stops a full set of unreclaimable entries evicting the very row being written.

    **When the receipt store is durably unwritable the list GROWS past its cap**, by one short
    action id per autonomous send, and logs once per cap's worth. That is the deliberate trade:
    slow, bounded-by-traffic memory growth in a broken-store scenario is better than re-announcing
    work the operator has dismissed. Nothing is ever dropped to stay under the cap.
    """
    if action_id not in announced:
        announced = [*announced, action_id]
    if len(announced) <= ANNOUNCED_MAX or record is None:
        return announced
    over = len(announced) - ANNOUNCED_MAX
    drop: set[str] = set()
    attempts = 0
    for candidate in announced:  # oldest first; the incoming id is newest and so never reached
        if len(drop) >= over or attempts >= RECLAIM_ATTEMPTS:
            break
        if candidate == action_id:
            continue
        attempts += 1
        try:
            durable = record(candidate)
        except Exception:  # noqa: BLE001 — unverifiable is not durable
            durable = False
        if durable:
            drop.add(candidate)
    if not drop and len(announced) % ANNOUNCED_MAX == 0:
        log.warning(
            "notifications: %d announcement identities are held because their receipts are not "
            "durable; the bell's dedupe list is growing past its %d cap",
            len(announced),
            ANNOUNCED_MAX,
        )
    return [a for a in announced if a not in drop]


def _converge(action_id: str, after: Callable[[], None] | None) -> None:
    """Write the caller's durable receipt. Called with this store's lock HELD.

    Only ever AFTER the document carrying the row and its tombstone is on disk — the receipt is
    never written first, which is what stops a failure there losing the announcement outright. A
    tombstone hit runs this too, so a receipt whose first write failed is repaired on any later
    announcement of the same action and the compaction pin then releases.
    """
    if after is None:
        return
    try:
        after()
    except Exception:  # noqa: BLE001 — still owed; a later announcement retries it
        log.debug("notifications: the announcement receipt did not converge", exc_info=True)


def _write(
    path: Path,
    rows: list[dict],
    *,
    announced: list[str] | None = None,
    episodes: dict[str, dict] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: a shared ".tmp" lets one writer's os.replace unlink the file
    # another is still writing into. The lock above makes this belt-and-braces, but the
    # cost is nil and it keeps _write correct if it is ever called unlocked.
    # The predecessor is resolved BEFORE the temporary is opened (Hermes 5239, finding 3): the
    # strict read can refuse, and a refusal after `os.open` leaked the descriptor on every attempt.
    #
    # `episodes` (#1086 Phase 4) follows the same rule: `None` preserves what is on disk, so no
    # writer that edits rows can drop the open needs-you episodes — which would re-announce every
    # one of them on the next sync.
    if announced is None or episodes is None:
        # STRICT: preserving what is on disk from a document that could not be read would write
        # an invented empty predecessor over it (Hermes 5231) — so an unreadable one refuses.
        _, disk_announced, disk_episodes = _load_doc_strict(path)
        announced = disk_announced if announced is None else announced
        episodes = disk_episodes if episodes is None else episodes
    doc: object = rows
    if announced or episodes:
        full: dict[str, object] = {"rows": rows, "announced": announced}
        if episodes:
            full["episodes"] = episodes
        doc = full
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # A buffered writer, not a bare `os.write`: `os.write` is permitted to write FEWER bytes
    # than it was given and simply return the count. Unchecked, that installs a truncated
    # document — `_read` then fails to parse it and returns `[]`, so the bell silently empties
    # while every caller was told the write succeeded. `BufferedWriter.write` writes everything
    # or raises, which is the property this needs (#839 review).
    # THE TOMBSTONES RIDE IN THIS SAME DOCUMENT, so the row and the dedupe identity reach disk in
    # ONE `os.replace` (#983 P4 review). They were two commits in two stores before, which no lock
    # can make atomic: the row landed, the receipt failed or the process died, `dismiss` deleted the
    # row — the only evidence — and the next repair announced it again as new unread activity.
    #
    # `announced=None` means PRESERVE what is on disk, which is what every caller that is editing
    # rows wants; a bare list is written whenever there is nothing to remember, so the shared
    # subscriptions file and every pre-P4 store keep their exact shape.
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True).encode())
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.replace(tmp, path)
    # The rename itself has to reach disk, or a crash can leave the directory entry pointing at
    # neither document.
    fsync_dir(path.parent)


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
    auto_direction: bool = False,
    activity_at: float | None = None,
    recorded: Callable[[], bool] | None = None,
    after: Callable[[], None] | None = None,
    record: Callable[[str], bool] | None = None,
    path: Path | None = None,
) -> dict | None:
    """Record one notification, or return ``None`` when an equivalent one is already pending.

    Deliberately takes named, bounded fields rather than a free dict: it is the same enforcement
    trick as ``webpush.build_payload`` — a caller cannot slip screen text in without changing
    this signature."""
    p = path or _notifications_path()
    with _locked(p):
        rows, announced, _eps = _load_doc_strict(p)
        # THE DEDUPE IDENTITY IS THIS STORE'S OWN, AND IT IS WRITTEN WITH THE ROW (#983 P4 review).
        #
        # Read inside the lock, so a lagging announcer cannot re-announce what a peer has already
        # committed; and written in the SAME `_write` as the row, so a failure or a crash can never
        # leave the row present and the identity absent. That torn state was the defect: `dismiss`
        # is a DELETE and `_evict` drops at the cap, so losing the identity meant the next repair
        # announced a delivery the operator had already cleared, as new unread activity.
        #
        # TWO RECORDS, ONE AUTHORITY, AND THEY ANSWER DIFFERENT HALVES OF THE SAME QUESTION.
        #
        # `recorded` is the caller's DURABLE receipt: unbounded, and the authority. The tombstone
        # covers the one window it cannot — the bell row has landed and the receipt has not, which
        # no lock can make atomic across two stores. Outside that window an evicted tombstone
        # simply falls back to the receipt, which is what stops reclamation being load-bearing.
        #
        # Both are read INSIDE this lock, so a lagging announcer cannot act on a stale answer. That
        # is notifications→missions, the direction `_converge` already takes when it writes the
        # receipt from here, so it adds no lock-order edge. (The compaction pin reads the receipt
        # rather than this store precisely because ITS direction is the opposite one.)
        #
        # A TOMBSTONE HIT CONVERGES, it does not merely suppress: a receipt whose first write
        # failed is repaired here, and the compaction pin then releases.
        if auto_direction and action_id:
            if action_id in announced:
                _converge(action_id, after)
                return None
            try:
                if recorded is not None and recorded():
                    return None
            except Exception:  # noqa: BLE001 — an unreadable receipt is not a licence to re-announce
                log.debug("notifications: could not read the announcement receipt", exc_info=True)
                return None
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
        # AN AUTONOMOUSLY SENT AI-WRITTEN DIRECTION (#983 P4), deduped BY ACTION ID.
        #
        # Its own announcing class, and never `escalation`: an escalation means "you need to decide
        # something" and enrols the row in the decision badge, while this is the opposite report —
        # "this was already done on your behalf, with nobody reading it". It still announces,
        # because an unreviewed write into a permission-bypassed agent is the one thing this mode
        # owes the operator a live signal for.
        #
        # The identity is the ACTION, not the session or the situation: one send, one row. Nothing
        # model-authored is in the key (the title is server-built and the draft never reaches here),
        # so the #760 failure — a re-worded title defeating the equivalence test — cannot recur.
        # Scoped to this class on BOTH sides, exactly like the escalation rule below: the same
        # action already has a proposal row under `notify: all`, and that is a different event, so
        # it must neither suppress this nor be suppressed by it. This is what lets the delivery and
        # the sweep's repair both call `add` freely — whichever runs first announces, the other
        # finds the row and returns `None`.
        if auto_direction and action_id:
            for r in rows:
                if r.get("auto_direction") is True and r.get("action_id") == action_id:
                    # Already on the list, which is just as announced — so the tombstone is laid
                    # here too, or a sweep would keep retrying an announcement that exists.
                    _write(p, rows, announced=_remember(announced, action_id, record))
                    _converge(action_id, after)
                    return None
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
                    # Cleared in the SAME write that revives the row. A stale `settled_at` on a
                    # live alert would file an unresolved situation in the operator's decision
                    # history and start a retention clock on something nobody has settled.
                    r.pop("settled_at", None)
                    # …and the HIDE, or a cleared incident that comes back stays invisible for
                    # ever: the flag outlived the settlement it was clearing, so the row's next
                    # settlement was filtered out by a decision the operator made about the
                    # previous one.
                    r.pop("settled_hidden", None)
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
            # Durable provenance for the class above, for the same reason `escalation` carries it:
            # the equivalence test is class-to-class, so a row has to say what it was.
            "auto_direction": bool(auto_direction),
            "activity_at": activity_at if isinstance(activity_at, int | float) else None,
        }
        rows.append(rec)
        kept = _evict(rows)
        if auto_direction and action_id:
            # ONE WRITE, carrying the row and its tombstone, so a failure or a crash can never
            # leave the row present and the identity absent. The tombstone is invisible to the
            # bell — it is not in `rows`, so neither `dismiss` nor `_evict` can reach it — and it
            # only has to survive until `_converge` makes the durable receipt the record.
            _write(p, kept, announced=_remember(announced, action_id, record))
            _converge(action_id, after)
            return rec
        _write(p, kept)
        if after is not None:
            after()
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


#: How much settled history the bell projects back. Bounds the PROJECTION, not the store —
#: `_evict` still trims only on `NOTIFY_MAX` overflow, so a row that ages out of this window
#: survives as the #760 dedupe memo and simply stops being drawn (#852).
SETTLED_MAX = 10
SETTLED_WINDOW_S = 24 * 3600


def _action_states(rows: list[dict]) -> dict[str, str] | None:
    """``action_id -> ledger state`` for the rows that carry one. ``None`` if the ledger could
    not be read AT ALL — which is not the same as "no rows matched", and callers must not
    collapse the two: an unreadable ledger has to fail toward showing, an empty one does not.

    Read WITHOUT the notifications lock held — see :func:`listing`.
    """
    wanted = {str(r.get("action_id")) for r in rows if r.get("action_id")}
    if not wanted:
        return {}
    try:
        from . import orchestrator_ledger as ledger

        status, latest = ledger.latest_by_id_checked()
    except Exception:  # noqa: BLE001 — an unreadable ledger must not retire or hide anything
        log.debug("notifications: could not reconcile against the ledger", exc_info=True)
        return None
    if status != "ok":
        # A read that FAILED is not a ledger that is EMPTY. `latest_by_id` cannot tell them
        # apart — it turns an OSError into `{}` — and `{}` here means "every action absent",
        # which projects each live escalation as `historical`: no controls, no badge, no outcome
        # asserted. A transient permission or I/O error would silently disarm the bell.
        log.debug("notifications: ledger unreadable; leaving rows unreconciled")
        return None
    out: dict[str, str] = {}
    for aid in wanted:
        rec = latest.get(aid)
        if rec is not None and rec.get("state"):
            out[aid] = str(rec["state"])
    return out


def _row_projection(row: dict, states: dict[str, str] | None) -> dict:
    """The projection for one notification row — the ONE place a row's state is resolved.

    Two things make a row's state unestablishable, and they are the same answer: the ledger could
    not be read at all, or the row carries no ``action_id`` to look up. Neither is "absent" (which
    asserts the action is genuinely gone), so both resolve through ``known=False`` to ``unknown``.
    """
    from . import orchestrator_ledger as ledger

    aid = str(row.get("action_id") or "")
    known = states is not None and bool(aid)
    return ledger.project_for_operator(states.get(aid) if known else None, known=known)


class _EverySession:
    """Membership that contains every session key: each pending decision has a surface.

    Since #1086 Phase 3, a decision for a session a mission holds is settled in the mission
    console, and one for a session no mission holds is settled on the Ask page (NEEDS YOU) — so
    whichever it is, the operator can act on it. Only an UNREADABLE membership store leaves that
    unestablished, and that stays ``None`` below.
    """

    def __contains__(self, _key: object) -> bool:
        return True


EVERY_SESSION = _EverySession()


def mission_surfaces() -> set[str] | None:
    """The sessions an OPEN mission holds, or ``None`` if the membership cannot be read (#1057).

    Kept separate from :func:`decision_surfaces` for one caller: the orchestrator's announcement
    gate. Since #1086 Phase 4 the pass announces only these sessions; the rest are announced by
    the needs-you episode sync (`needs_you_notify`), which retracts what it raises.
    """
    try:
        from . import missions

        return set(missions.all_active_memberships())
    except Exception:  # noqa: BLE001 — unreadable is its own answer
        log.debug("notifications: could not read mission membership", exc_info=True)
        return None


def decision_surfaces() -> set[str] | _EverySession | None:
    """The sessions whose pending decision has somewhere to be ACTED on, or ``None`` if that
    cannot be established (#1057, widened by #1086 Phase 3).

    A session a mission holds is decided in the mission console; one no mission holds is decided
    on the Ask page's NEEDS YOU list, which the orchestrator's eligibility and that list both scope
    to the same roots + ``folder_exclusions`` boundary. So every session has a surface —
    :data:`EVERY_SESSION` — once the membership that decides WHICH surface can be read.

    ``None`` is not "no surfaces": an unreadable membership store is an unestablishable answer,
    and it resolves the way #852 rule 5 resolves every other one — counted as `uncertain`, never
    silently dropped and never overstated as actionable.

    Read WITHOUT the notifications lock held — see :func:`listing`.
    """
    return None if mission_surfaces() is None else EVERY_SESSION


def _counts_toward_badge(
    row: dict, states: dict[str, str] | None, surfaces: set[str] | _EverySession | None
) -> bool:
    """Does this unread row still want the operator, **and can the operator do anything?**
    (#852 rule 5.)

    The badge counts what can still be **acted on**. A decision already delivered, claimed or
    settled is history, and leaving it in the count trains the operator to ignore the number.

    **No exceptions, and the exceptions were the bug.** This used to fail toward counting whenever
    the answer was genuinely unknown — an unreadable ledger, or an escalation with no action id —
    on the reasoning that hiding a decision the operator never saw is worse. That reasoning is
    right about *visibility* and wrong about *the count*: those rows render `unknown`, which offers
    no control, so counting them produced a number the operator could not clear by acting. That is
    precisely the property rule 5 exists to remove, and it contradicted this module's own claim
    that ONE projection decides both "does this badge" and "does this render controls".

    So the projection decides, for every row. Rows whose state cannot be established are not lost:
    they stay fully visible in the listing and are counted separately as `uncertain`, which says
    what is true — *something is outstanding and we cannot currently tell you whether you can act
    on it* — instead of overstating it as actionable.

    Informational rows are excluded for a different reason: they are not uncertain, they are
    **known not to be decisions**. Nothing about them can be approved or rejected, so no operator
    action could ever clear them.

    **An actionable decision with no surface is not counted either (#1057).** The projection says
    whether the LEDGER would still accept a decision; it cannot say whether any screen offers one.
    Between #1049 and #1086 Phase 3 only the mission console did, so a decision for a session no
    mission held was one the operator could not clear by acting — rule 5's defect by a second
    cause. Since the Ask page's NEEDS YOU list acts on those, every decision has a surface again
    (:func:`decision_surfaces`); what remains uncountable is an UNREADABLE membership, which is
    `uncertain`. Nothing model-authored enters this.
    """
    if row.get("needs_you") is True:
        # An OPEN episode is, by construction, a session that needs you NOW: the row is retired the
        # moment the episode closes (#1086 Phase 4), and the Ask page is its surface.
        return not row.get("retired")
    if row.get("escalation") is not True:
        return False  # a log entry, never a decision
    from . import orchestrator_ledger as ledger

    if _row_projection(row, states)["projection"] != ledger.ACTIONABLE:
        return False
    return surfaces is not None and str(row.get("session_id") or "") in surfaces


def _is_uncertain(
    row: dict, states: dict[str, str] | None, surfaces: set[str] | _EverySession | None
) -> bool:
    """An escalation whose state could not be established — counted, but never as actionable.

    Either half can be unestablishable: the ledger state (`unknown`), or — for a decision the
    ledger would still accept — whether any surface can act on it (membership unreadable)."""
    if row.get("escalation") is not True:
        return False
    from . import orchestrator_ledger as ledger

    projection = _row_projection(row, states)["projection"]
    if projection == ledger.UNKNOWN:
        return True
    return projection == ledger.ACTIONABLE and surfaces is None


def _terminal_settlements(rows: list[dict]) -> dict[str, float | None]:
    """Which of these rows point at an action that has already settled, **and when**.

    Returns the ids WITH their decision times, from the SAME read that detected them. Fetching the
    timestamps in a second read is a race: compaction can remove the terminal record in between,
    and the fallback is repair time — which reorders the operator's decision history and extends
    the 24h window for a decision made long ago. The snapshot that proves the settlement is the
    snapshot that carries its time.

    Read WITHOUT the notifications lock held — see :func:`listing`.

    Fails toward SHOWING, in three separate ways, because hiding an escalation the operator never
    saw is the one outcome this module exists to prevent: a row with no ``action_id``, an id the
    ledger has never heard of, and a ledger that cannot be read at all are all treated as "still
    live". A single corrupt ledger record must not empty the bell.
    """
    wanted = {
        str(r.get("action_id"))
        for r in rows
        if r.get("escalation") is True and not r.get("retired") and r.get("action_id")
    }
    if not wanted:
        return {}
    try:
        from . import orchestrator_ledger as ledger

        latest = ledger.latest_by_id()
    except Exception:  # noqa: BLE001 — an unreadable ledger must not retire anything
        log.debug("notifications: could not reconcile against the ledger", exc_info=True)
        return {}
    out: dict[str, float | None] = {}
    for aid in wanted:
        rec = latest.get(aid)
        if rec is not None and rec.get("state") in ledger.TERMINAL_STATES:
            ts = rec.get("ts")
            out[aid] = float(ts) if isinstance(ts, int | float) else None
    return out


def retire_for_actions(
    action_ids: list[str] | set[str],
    path: Path | None = None,
    *,
    escalations_only: bool = True,
    decided_at: dict[str, float] | None = None,
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
        rows = _read_strict(p)
        n = 0
        for r in rows:
            if r.get("retired") or r.get("action_id") not in ids:
                continue
            if escalations_only and r.get("escalation") is not True:
                continue
            r["retired"] = True
            # `settled_at` is the row's OWN stamp — it must survive compaction, which can remove
            # the ledger row carrying the decision time while this row is still inside the 24h
            # window (#852 rule 2). But when the DECISION time is available it is the honest
            # value: retirement can be a self-heal that runs long after the fact, and stamping
            # the repair time reorders history by when we noticed rather than when it happened —
            # an action decided first can then look newer than one decided after it.
            aid = str(r.get("action_id") or "")
            when = (decided_at or {}).get(aid)
            r["settled_at"] = float(when) if isinstance(when, int | float) else time.time()
            n += 1
        if n:
            _write(p, rows)
        return n


def unretire_for_action(action_id: str, path: Path | None = None) -> int:
    """Revive the escalation rows of an action that became LIVE again (#1060, #1082 review).

    An operator's answer from the console closes the escalation before the keypress, and that
    settlement retires its bell row. When the answer then sends nothing the escalation reopens —
    and its row must too, or the bell stops pointing at a decision that is still open. The same
    three fields `add` clears when it revives a row, for the same reasons.
    """
    if not action_id:
        return 0
    p = path or _notifications_path()
    with _locked(p):
        rows = _read_strict(p)
        n = 0
        for r in rows:
            if r.get("action_id") != action_id or not r.get("retired"):
                continue
            if r.get("escalation") is not True:
                continue
            r["retired"] = False
            r.pop("settled_at", None)
            r.pop("settled_hidden", None)
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
    settled_now = _terminal_settlements(rows)
    if settled_now:
        # The decision times come from the SAME snapshot that detected the settlement. Fetching
        # them in a second read races compaction, and losing that race silently substitutes
        # repair time for decision time — the reordering these stamps exist to prevent.
        # A self-heal, and the listing is a READ: an unreadable store refuses the write, and the
        # listing still shows what it can rather than failing the bell.
        with contextlib.suppress(StoreUnreadable):
            retire_for_actions(
                set(settled_now),
                p,
                decided_at={k: v for k, v in settled_now.items() if v is not None},
            )
        rows = _read(p)
    states = _action_states(rows)
    surfaces = decision_surfaces()
    visible = sorted(
        (r for r in rows if not r.get("retired")), key=lambda r: -float(r.get("ts") or 0)
    )
    # The badge counts ACTIONABLE unread rows (#852 rule 5). Counting every unread row put
    # decisions that were already delivered, claimed or settled into the number, so the number
    # stopped meaning "things waiting on you" — and a badge the operator cannot clear by acting
    # is a badge they learn to ignore.
    unread = sum(
        1 for r in visible if not r.get("read") and _counts_toward_badge(r, states, surfaces)
    )
    # Rows the badge cannot count because their state is unestablishable. Reported separately so
    # the operator is told "something is outstanding and we cannot read it" rather than either
    # silence (which loses the decision) or an actionable count they cannot clear (rule 5).
    uncertain = sum(1 for r in visible if not r.get("read") and _is_uncertain(r, states, surfaces))

    # Every producer of a decision consumes the ONE projection (#852). A live bell row is a
    # producer as much as a Pulse card is, and returning it raw left the bell deriving its own
    # controls from a state field — the drift this contract exists to end, in the third place.
    hydrated = [
        {**r, **_row_projection(r, states)} if r.get("escalation") is True else r for r in visible
    ]
    out = {
        "notifications": hydrated,
        "unread": unread,
        "uncertain": uncertain,
        "settled": settled(rows, states),
    }
    # Every device tag still OWED a retraction (#1086 Phase 4): the app closes EXACTLY these when it
    # is opened — the retraction path that needs no push at all. Episode tags only, never reused;
    # an escalation's URL tag never. ABSENT when the store could not be read.
    with contextlib.suppress(StoreUnreadable):
        out["close_tags"] = owed_close_tags(p)
    return out


def _settled_window(rows: list[dict], *, now: float | None = None) -> list[dict]:
    """The rows currently INSIDE the settled projection — newest first, bounded 10 and 24h.

    One definition, because "what is in the window" is asked by two callers who must agree.
    :func:`clear_settled` used to re-state the predicate and omit the bounds, so with eleven
    recent rows the operator saw ten and cleared eleven: the unseen row was hidden for ever,
    though it would have become visible as newer entries aged out. Hiding what was never shown is
    history loss, not a dismissal.
    """
    cutoff = (time.time() if now is None else now) - SETTLED_WINDOW_S
    out = [
        r
        for r in rows
        if r.get("retired")
        and r.get("escalation") is True
        and isinstance(r.get("settled_at"), int | float)
        and float(r["settled_at"]) >= cutoff
        and not r.get("settled_hidden")
    ]
    out.sort(key=lambda r: -float(r.get("settled_at") or 0))
    return out[:SETTLED_MAX]


def settled(rows: list[dict] | None = None, states: dict[str, str] | None = None) -> list[dict]:
    """A bounded window of recently decided rows, projected back as history with NO controls.

    Before this, a decided row simply disappeared from the bell, so the operator could not tell
    "I dealt with that" from "that vanished" — and the row was still in the store the whole time,
    doing its #760 dedupe job invisibly. This draws it.

    **Bounds the projection, not the store.** `_evict` is untouched: a row that falls outside this
    window is still present, still suppressing a re-announce, and merely stops being rendered.
    Trimming the store instead would destroy the memo and restart the very re-announce loop #800
    fixed — the same bug through the front door.

    Ordered by the row's own `settled_at`, never by a ledger timestamp, because the ledger record
    can be compacted away while the row is still inside the window.
    """
    if rows is None:
        rows = _read(_notifications_path())
    if states is None:
        states = _action_states(rows)
    out = _settled_window(rows)
    projected = []
    for r in out:
        from . import orchestrator_ledger as ledger

        aid = str(r.get("action_id") or "")
        state = (states or {}).get(aid)
        known = states is not None
        # A compacted action degrades to `historical`: no controls, and NO outcome asserted.
        # Claiming one would invent a fact the store cannot support (#852 rule 4).
        #
        # Then controls are forced OFF, unconditionally — this window is history and the
        # docstring above promises exactly that. The ledger is consulted here only to LABEL the
        # outcome; a row's *finality* is durable evidence on the row itself (`retired` plus a
        # `settled_at` this window already filtered on), so it cannot depend on whether the
        # ledger happens to read. Passing `known=` straight through let an outage project a
        # decided row as `unknown` and hand it a Reject — a control on history, for an action the
        # operator already settled, which the backend then answers 404.
        proj = ledger.project_for_operator(state, known=known)
        projected.append({**r, **proj, "can_approve": False, "can_reject": False})
    return projected


def clear_settled(seen_ids: list[str] | set[str], path: Path | None = None) -> int:
    """Hide the settled rows the operator actually saw. Returns how many left the projection.

    **A hide, not a delete, and only over rows already settled.** Physically removing them
    destroys the #760 dedupe memo and restarts the re-announce loop for exactly the actions the
    operator has already dealt with — #800's bug through the front door. And it may only touch
    rows already in the settled projection: reaching a live row here would clear an alert nobody
    decided.

    **``seen_ids`` is required, and that is the fix for a race a lock cannot close.** Recomputing
    the window when the POST arrives is correct only if nothing settled since the GET that drew
    the button — and something can: the operator reads a window holding A, action B settles, the
    operator clicks Clear, and B is hidden having never been rendered. It is then gone for good,
    because "hidden" is exactly the state that keeps it out of every future projection. The two
    moments are different moments, and no amount of locking inside the second one can recover
    what the first one displayed. So the caller passes what it showed, and this hides the
    intersection of that with the window as it stands now.

    The intersection matters in both directions: an id the caller never saw is not hidden (the
    race above), and an id that has since aged out of the window is not hidden either (it was
    never the caller's to clear, and it is due to resurface as newer rows age out).
    """
    wanted = {str(i) for i in seen_ids}
    if not wanted:
        return 0
    p = path or _notifications_path()
    with _locked(p):
        rows = _read_strict(p)
        # The SAME membership definition the operator is looking at, computed inside this lock so
        # the two cannot drift. Identity, not equality — two rows can compare equal.
        visible = {id(r) for r in _settled_window(rows)}
        n = 0
        for r in rows:
            if id(r) in visible and str(r.get("id")) in wanted:
                r["settled_hidden"] = True
                n += 1
        if n:
            _write(p, rows)
        return n


def mark_read(ids: list[str] | None = None, path: Path | None = None) -> int:
    """Mark the given ids read, or all of them when ``ids`` is None. Returns the count."""
    p = path or _notifications_path()
    with _locked(p):
        rows = _read_strict(p)
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
        rows = _read_strict(p)
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
        rows = _read_strict(p)
        keep = [r for r in rows if r.get("action_id") != action_id]
        n = len(rows) - len(keep)
        if n:
            _write(p, keep)
        return n


# --- needs-you episodes (#1086 Phase 4) --------------------------------------------------

#: What a needs-you row says, keyed by the SERVER-derived kind (`needs_you.KINDS`). Never a
#: model-authored string: a row's text is decided here, so nothing a model writes can reach the
#: bell or a push through this path.
KIND_REASON = {
    "choice": "Asks you to choose",
    "approval": "Waiting for your approval",
    "question": "Asks you a question",
    "needs_inspection": "Stopped — take a look",
}


def episode_tag(session_id: str, episode: str) -> str:
    """The notification tag for one episode — what a close push closes, and ONLY that."""
    return f"needs-you:{session_id}:{episode}"


#: Reserved key prefix in the episodes map for a device notification still to be RETRACTED. Session
#: keys are ``engine:id`` and no engine is called ``closing``, so the two can never collide; keeping
#: them in the same map is what makes every writer preserve them (`_write(..., episodes=None)`).
CLOSING = "closing:"

#: How long a pending retraction is kept for delivery on a shown push before it is dropped. The
#: closed-episode ledger below keeps covering it for the app's own reconciliation afterwards.
CLOSING_KEEP_S = 7 * 86400

#: The CLOSED-EPISODE LEDGER (Hermes 5265, finding 6): every retracted device tag, kept for the
#: supported recovery horizon whether or not a push ever carried it. It is the server's
#: authoritative answer to "is this notification retracted?" — the app closes exactly these tags
#: when it reads the bell, and the service worker asks before it shows a needs-you notification.
#: Unlike an owed retraction it is never acknowledged away, only aged out.
CLOSED = "closed:"
CLOSED_KEEP_S = 30 * 86400
CLOSED_MAX = 5000


def owed_close_tags(path: Path | None = None) -> list[str]:
    """Every retracted device tag in the recovery horizon — what the app closes EXACTLY, and what
    the service worker checks before showing (Hermes 5239 finding 5, 5265 findings 2 and 6). Strict.

    Only needs-you EPISODE tags are ever listed. They are minted per episode and never reused, so
    retracting one can never close or suppress a later notification (Hermes 5275, finding 1). An
    escalation's per-session URL tag is reused by every later escalation, so it is never retracted
    from here at all: episodes defer to such a row rather than adopting it (see
    :func:`sync_needs_you`)."""
    _rows, _a, episodes = _load_doc_strict(path or _notifications_path())
    return sorted({str(ep["tag"]) for k, ep in episodes.items() if k.startswith(CLOSED)})


def has_open_episode(session_id: str, path: Path | None = None) -> bool | None:
    """Whether ``session_id`` has an open needs-you episode; ``None`` when the store is unreadable.
    The orchestrator's unreadable-membership fallback asks, so an episode that already covers the
    situation is not announced a second time (Hermes 5239, finding 4)."""
    try:
        _rows, _a, episodes = _load_doc_strict(path or _notifications_path())
    except StoreUnreadable:
        return None
    return session_id in episodes


def ack_closes(tags: list[str] | set[str], path: Path | None = None) -> int:
    """Forget pending retractions that have been DELIVERED. Returns how many were removed."""
    wanted = {str(t) for t in tags}
    if not wanted:
        return 0
    p = path or _notifications_path()
    with _locked(p):
        rows, _a, episodes = _load_doc_strict(p)
        drop = [k for k in episodes if k.startswith(CLOSING) and episodes[k]["tag"] in wanted]
        for k in drop:
            del episodes[k]
        if drop:
            _write(p, rows, episodes=episodes)
        return len(drop)


def sync_needs_you(
    current: dict[str, dict],
    *,
    keep: set[str] | frozenset[str] = frozenset(),
    push: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Reconcile the needs-you EPISODES with who needs the operator now. One notification per
    episode, retracted when the episode ends.

    ``current`` maps each session to announce onto its row facts (title, project, engine, kind,
    action_id). ``keep`` names further sessions that still need the operator but are not to be
    announced by this pass (beyond the list's row cap): their open episodes stay open. Everything
    else with an open episode has stopped needing the operator, so its episode closes.

    **An episode is ``(session key, episode start)``** and never includes model text (the
    notification-volume rule): a session re-worded by its next review is the same episode, and a
    session that stops needing you and comes back is a new one. Episodes are persisted in THIS
    store, under its lock, in the same document as the rows — so a restart finds the open episode
    and never re-announces the state it announced, and an operator's bell dismissal (a row delete)
    does not re-open it either. An unreadable store raises :class:`StoreUnreadable` and is never
    rewritten (Hermes 5231).

    **Closing retires the episode's rows** (they leave the bell and the badge) and, when it was
    pushed, records a PENDING RETRACTION for its device tag(s) in the same document. Retractions
    are delivered by the caller only alongside something VISIBLE and acknowledged with
    :func:`ack_closes` once sent, so a failed send or a restart retries rather than forgetting
    (Hermes 5231, findings 5 and 7).

    **An episode DEFERS to a live escalation that already announces its session** — the
    orchestrator's unreadable-membership fallback, a mission escalation, or one from before this
    change. The episode opens with no row and no push, so the situation is announced once, and it
    never touches that row: the escalation keeps its own lifecycle (its decision settles it) and its
    per-session URL tag, which later escalations reuse, is never retracted by an episode. Adopting
    it instead let a retraction close a newer mission escalation under the same tag, and let a
    session joining a mission retire the mission's own new escalation (Hermes 5275, findings 1-2).

    Returns ``opened`` (new rows to push), ``closed``, ``pending`` (tags awaiting retraction) and
    ``keep`` (the newest open, pushed episode's row — what a retraction may be delivered with).
    Blocking.
    """
    now = time.time() if now is None else now
    live = set(current) | set(keep)
    p = path or _notifications_path()
    with _locked(p):
        rows, _announced, episodes = _load_doc_strict(p)  # raises StoreUnreadable, never guesses
        changed = False

        def _pend(tag: str) -> None:
            if tag:
                episodes[f"{CLOSING}{tag}"] = {"tag": tag, "closing": True, "ts": now}
                episodes[f"{CLOSED}{tag}"] = {"tag": tag, "closed": True, "ts": now}

        def _is_open(k: str) -> bool:
            return not (k.startswith(CLOSING) or k.startswith(CLOSED))

        # Owed retractions expire from the PUSH path after CLOSING_KEEP_S; the closed ledger keeps
        # covering them for the app for CLOSED_KEEP_S, then the oldest beyond CLOSED_MAX go.
        for prefix, keep_s in ((CLOSING, CLOSING_KEEP_S), (CLOSED, CLOSED_KEEP_S)):
            for k in [k for k in episodes if k.startswith(prefix)]:
                ts = episodes[k].get("ts")
                if not isinstance(ts, int | float) or now - float(ts) > keep_s:
                    del episodes[k]
                    changed = True
        ledger = sorted(
            (k for k in episodes if k.startswith(CLOSED)),
            key=lambda k: float(episodes[k].get("ts") or 0),
        )
        for k in ledger[: max(0, len(ledger) - CLOSED_MAX)]:
            del episodes[k]
            changed = True

        def _escalated(sid: str) -> bool:
            return any(
                r.get("session_id") == sid
                and r.get("escalation") is True
                and r.get("needs_you") is not True
                and not r.get("retired")
                for r in rows
            )

        closed: list[dict] = []
        for sid in sorted(k for k in episodes if _is_open(k)):
            if sid in live:
                continue
            ep = episodes.pop(sid)
            changed = True
            tag = str(ep.get("tag") or "")
            for r in rows:
                if r.get("needs_you") is True and r.get("tag") == tag and not r.get("retired"):
                    r["retired"] = True
                    r["settled_at"] = now
            pushed = ep.get("pushed") is True
            if pushed:
                _pend(tag)
            closed.append(
                {
                    "session_id": sid,
                    "episode": str(ep.get("id") or ""),
                    "tag": tag,
                    "pushed": pushed,
                }
            )

        opened: list[dict] = []
        for sid in sorted(current):
            if sid in episodes:
                continue
            info = current[sid]
            ep_id = f"{int(now * 1000):x}"
            tag = episode_tag(sid, ep_id)
            ep: dict = {"id": ep_id, "start": now, "tag": tag, "pushed": bool(push)}
            if _escalated(sid):
                # Already announced by a live escalation: defer to it — no second row, no push,
                # nothing to retract when the episode ends. The escalation is left untouched.
                ep.update({"pushed": False, "deferred": True})
                episodes[sid] = ep
                changed = True
                continue
            episodes[sid] = ep
            rec = {
                "id": hashlib.sha256(f"{tag}".encode()).hexdigest()[:16],
                "ts": now,
                "read": False,
                "title": str(info.get("title") or "A session needs you")[:TITLE_MAX],
                "project": str(info.get("project") or "")[:BODY_MAX],
                "reason": KIND_REASON.get(str(info.get("kind") or ""))
                or KIND_REASON["needs_inspection"],
                "session_id": sid,
                "engine": str(info.get("engine") or ""),
                "action_id": str(info.get("action_id") or ""),
                "escalation": False,
                "auto_direction": False,
                "needs_you": True,
                "episode": ep_id,
                "tag": tag,
                "activity_at": None,
            }
            rows.append(rec)
            opened.append(rec)
            changed = True
        if changed:
            rows = _evict(rows)
            _write(p, rows, episodes=episodes)
        # Oldest first, so a bounded batch always drains the longest-owed retractions.
        owed = sorted(
            (ep for k, ep in episodes.items() if k.startswith(CLOSING)),
            key=lambda ep: (float(ep.get("ts") or 0), str(ep["tag"])),
        )
        return {
            "opened": opened,
            "closed": closed,
            "pending": [str(ep["tag"]) for ep in owed],
            "open": sorted(k for k in episodes if _is_open(k)),
        }


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
        rows = [r for r in _read_strict(p) if r.get("endpoint") != endpoint]
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
        rows = _read_strict(p)
        keep = [r for r in rows if r.get("id") != sub_id]
        if len(keep) == len(rows):
            return False
        _write(p, keep)
        return True


def drop_endpoint(endpoint: str, path: Path | None = None) -> None:
    """Prune a subscription the push service reported gone (404/410)."""
    p = path or _subs_path()
    with _locked(p):
        rows = _read_strict(p)
        keep = [r for r in rows if r.get("endpoint") != endpoint]
        if len(keep) != len(rows):
            _write(p, keep)


#: An automation's failure episode key (`automation_runner.notify`): the link opens its run history.
_AUTOMATION_ACTION_RE = re.compile(r"^automation:([a-z0-9]{1,64}):")
#: The SPA's run-history route for one automation (`web/src/lib/routes.ts` `automationPath`).
AUTOMATION_PATH = "/mission/automations/"


def _link(notification: dict, base_url: str = "") -> str:
    m = _AUTOMATION_ACTION_RE.match(str(notification.get("action_id") or ""))
    if m and not notification.get("session_id"):
        return f"{base_url}{AUTOMATION_PATH}{m.group(1)}"
    uuid = notification.get("session_id", "")
    engine = notification.get("engine", "")
    return f"{base_url}/s/{engine}/{uuid.split(':', 1)[-1]}" if uuid else f"{base_url}/mission"


def fanout(notification: dict, base_url: str = "", path: Path | None = None, close=()) -> dict:
    """Push one notification to every subscribed device. Blocking — call under to_thread.

    Best-effort by design: the bell entry already exists, so a dead push service degrades the
    experience rather than losing the message. A ``410 Gone`` prunes that subscription.
    """
    from . import webpush

    # STRICT: a subscription list that could not be read is not "nobody to tell" — a caller that
    # acknowledges retractions on a send with no failures would forget work it never did (5265, 1).
    rows = _read_strict(path or _subs_path())
    if not rows:
        return {"sent": 0, "pruned": 0, "failed": 0}
    url = _link(notification, base_url)
    # Title + project + link ONLY. This is the third-party boundary (#726).
    payload = webpush.build_payload(
        title=notification.get("title", "Mission control"),
        project=notification.get("project", ""),
        url=url,
        # A needs-you row carries its episode's tag, so a later retraction can close exactly it.
        tag=str(notification.get("tag") or ""),
        # Pending retractions ride on a SHOWN push — never a silent one (Hermes 5231).
        close=close,
    )
    return _send_all(payload, path, rows=rows)


def _send_all(payload: bytes, path: Path | None, *, rows: list[dict] | None = None) -> dict:
    from . import webpush

    if rows is None:
        rows = _read_strict(path or _subs_path())
    sent = pruned = failed = 0
    for row in rows:
        try:
            webpush.send(row, payload)
            sent += 1
        except webpush.SubscriptionGone:
            # A prune is housekeeping: a subscriptions file that cannot be read refuses the write
            # (it must not be rewritten from a guess) and the gone device is pruned next time.
            with contextlib.suppress(StoreUnreadable):
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
