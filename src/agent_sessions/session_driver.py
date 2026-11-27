"""Who drives an API session: HQ, the Commander, or a mission (#2397, epic #2070 A7).

HQ can work WITH an API session it started or adopted: send it the next step, read the reply,
send the next one, close it. The Commander takes over by typing in it, and hands it back from the
session's header; a session a mission holds stays the mission's. This module is the one record of
that, and the one check every HQ drive request is admitted by.

**The record.** A small ``0600`` JSON store (``session-driver.json``):
``{key: {"who": "hq" | "commander", "cid": <conversation>, "generation": n, "since": ts,
"title": <the session's title as HQ named it>}}``.

* A session a mission holds reads ``who: "mission"`` whatever the store says: DERIVED from
  ``missions.all_active_memberships()``, never stored. HQ never pre-empts a mission.
* A session HQ's start card launched is recorded ``who: "hq"`` at generation 1, with the
  conversation that started it (`record_start`). A session with no record is the Commander's.
* ``cid`` is the ONE HQ conversation that drives it (or last drove it, after a take-over: that is
  where Hand back returns it). ``generation`` only grows: every change of driver bumps it, and an
  HQ request is bound to the generation it was staged under.

**Every write is an authority withdrawal**, committed inside `session_input`'s write fence
(`session_input.session_transaction`), which replaces the session's native authority tokens
(`native_authority.bump_session`) before the write. The lock order, here and nowhere else:

    session_input._lock  →  authfence  →  this store's flock

An HQ drive turn is admitted by `guard`'s ``admit`` inside the SAME fence
(`session_input.admit_native`), which re-reads this record, the mission membership and the
session's newest operator turn there; the worker re-checks the tokens it was issued at its claim.
So a take-over and an HQ send are ordered one way or the other: the send was admitted before the
take-over (and runs), or it is refused at admission, or the worker refuses its replaced tokens.
Never both. No flock is held across an await: every function here is blocking and short, and
callers run it off the event loop.

Nothing here names an engine: an API session is the manifest's (`mission_structured.is_api_key`).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock, read_json_doc

log = logging.getLogger(__name__)

HQ, COMMANDER, MISSION = "hq", "commander", "mission"
#: What a stored record may say. ``mission`` is derived, never stored.
STORED = frozenset({HQ, COMMANDER})
#: The verbs an HQ drive request names (the catalog's `session.*` drive entries).
VERBS = frozenset({"send", "interrupt", "withdraw", "adopt"})
#: Where ids HQ's drive requests use come from: derived from the proposal id, never minted, so an
#: act and a tap of one card submit ONE turn.
NAMESPACE = uuid.UUID("8b6e1d3a-27c4-5f19-a0d2-4c7e9b51f3a6")
TITLE_MAX = 120

#: What a refused drive request says (the route answers 409 with it; the catalog reads stale).
TAKEN_OVER = "the Commander took this session over"
MISSION_HELD = "a mission holds this session"
OTHER_CONVERSATION = "another HQ conversation drives this session"
NOT_DRIVEN = "HQ does not drive this session"
MOVED = "who drives this session changed"
NEWER_TURN = "the session has a newer turn than the one HQ read"
#: The closed reason a refused drive request carries (the catalog reads it as stale).
REFUSED_REASON = "driver"


class DriverError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_SESSION_DRIVER",
            str(Path.home() / ".config" / "agent-sessions" / "session-driver.json"),
        )
    )


# ---- ids ----------------------------------------------------------------------------------------


def _derived(pid: str, part: str) -> str:
    if not isinstance(pid, str) or not pid:
        raise ValueError("a drive request needs its proposal id")
    return str(uuid.uuid5(NAMESPACE, f"ask-drive:{pid}:{part}"))


def send_operation_id(pid: str) -> str:
    """The one operation id (= turn id) an HQ drive send of proposal ``pid`` uses."""
    return _derived(pid, "send")


def interrupt_operation_id(pid: str) -> str:
    return _derived(pid, "interrupt")


def withdraw_operation_id(pid: str) -> str:
    return _derived(pid, "withdraw")


def sent_by_hq(turn: dict) -> bool:
    """Was this snapshot turn sent by HQ: a drive send (its id derived from the proposal id it
    carries as ``reservation_id``), or the first message of a session HQ's card started? Only
    server code mints either id, so a turn from anyone else never matches. Pure."""
    from . import mission_structured

    if not isinstance(turn, dict) or turn.get("kind") == "agent":
        return False
    rid = (turn.get("context") or {}).get("reservation_id")
    tid = turn.get("turn_id")
    if not isinstance(rid, str) or not rid or not isinstance(tid, str):
        return False
    try:
        return tid in (send_operation_id(rid), mission_structured.hq_ids(rid)[1])
    except ValueError:
        return False


# ---- the store ----------------------------------------------------------------------------------


def _coerce(rec: object) -> dict | None:
    if not isinstance(rec, dict) or rec.get("who") not in STORED:
        return None
    gen = rec.get("generation")
    if type(gen) is not int or gen < 0:
        return None
    cid = rec.get("cid")
    since = rec.get("since")
    title = rec.get("title")
    return {
        "who": rec["who"],
        "cid": cid if isinstance(cid, str) and cid else None,
        "generation": gen,
        "since": float(since) if isinstance(since, int | float) and not isinstance(since, bool)
        else 0.0,
        "title": title[:TITLE_MAX] if isinstance(title, str) else "",
    }  # fmt: skip


def records() -> dict[str, dict]:
    """Every record, in ONE read (lock-free: writes replace the file atomically). Blocking."""
    raw = read_json_doc(path()).get("sessions")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, rec in raw.items():
        r = _coerce(rec)
        if isinstance(key, str) and r is not None:
            out[key] = r
    return out


def record(key: str) -> dict | None:
    return records().get(key)


def _write(key: str, change) -> dict | None:
    """``change(current record or None) -> new record or None (no change)``, committed inside
    the write fence: `session_input.session_transaction` withdraws the session's native authority
    tokens BEFORE the store write. Raises `session_input.AuthorityFenceBusy` on a busy fence (the
    write did not happen). Blocking: run it off the loop."""
    from . import session_input

    with session_input.session_transaction(key), json_write_lock(path()):
        doc = read_json_doc(path())
        sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
        current = _coerce(sessions.get(key))
        new = change(current)
        if new is None:
            return None
        sessions = {k: v for k, v in sessions.items() if _coerce(v) is not None}
        sessions[key] = new
        atomic_write_json(path(), {"sessions": sessions}, mode=0o600)
        return dict(new)


def _title(title: object) -> str:
    return str(title or "")[:TITLE_MAX]


def record_start(key: str, cid: str, title: str = "") -> dict | None:
    """A session HQ's start card launches is HQ's from the start (generation 1). Called before
    the create, so the record is ahead of the session, never behind it. A record that already
    exists (a replayed launch) is left as it is."""

    def change(cur):
        if cur is not None:
            return None
        return {"who": HQ, "cid": cid, "generation": 1, "since": time.time(),
                "title": _title(title)}  # fmt: skip

    return _write(key, change)


def take_over(key: str) -> dict | None:
    """The Commander typed in ``key``: if HQ drives it, it is theirs now (the generation moves,
    the conversation is kept as where Hand back returns it). Returns the new record when it
    changed, else None. A cheap lock-free read first, so the Commander's ordinary typing into a
    session HQ does not drive takes no fence at all."""
    cur = record(key)
    if cur is None or cur["who"] != HQ:
        return None

    def change(cur):
        if cur is None or cur["who"] != HQ:
            return None
        return {**cur, "who": COMMANDER, "generation": cur["generation"] + 1,
                "since": time.time()}  # fmt: skip

    return _write(key, change)


def hand_back(key: str, generation: int) -> dict:
    """The Commander's Hand back to HQ: compare-and-set on ``generation``. Raises DriverError
    (409 stale, 409 mission-held, 422 HQ never drove it)."""
    from . import missions

    def change(cur):
        if _held(key, missions):
            raise DriverError(409, MISSION_HELD)
        now = cur["generation"] if cur is not None else 0
        if now != generation:
            raise DriverError(409, "who drives this session changed; reload and choose again")
        if cur is None or not cur["cid"]:
            raise DriverError(422, "HQ never drove this session: ask HQ to take it over")
        if cur["who"] == HQ:
            raise DriverError(409, "HQ already drives this session")
        return {**cur, "who": HQ, "generation": now + 1, "since": time.time()}

    return _write(key, change) or {}


def adopt(key: str, *, cid: str, generation: int, title: str, admit=None) -> dict:
    """HQ takes over driving ``key`` for conversation ``cid`` (``session.adopt``), a CAS on
    ``generation`` inside the fence. ``admit()`` (the act's grant at this effect boundary) runs
    inside the fence too: "" lets it commit. Raises DriverError."""
    from . import missions

    def change(cur):
        if _held(key, missions):
            raise DriverError(409, MISSION_HELD)
        now = cur["generation"] if cur is not None else 0
        if now != generation:
            raise DriverError(409, MOVED)
        if cur is not None and cur["who"] == HQ and cur["cid"] == cid:
            raise DriverError(409, "HQ already drives this session from this conversation")
        if admit is not None:
            why = admit()
            if why:
                raise GrantRefused(why)
        return {"who": HQ, "cid": cid, "generation": now + 1, "since": time.time(),
                "title": _title(title) or (cur or {}).get("title") or ""}  # fmt: skip

    return _write(key, change) or {}


class GrantRefused(DriverError):
    """The act's grant refused an adopt at its effect boundary: nothing changed."""

    def __init__(self, why: str):
        super().__init__(409, why)


def mission_takes(key: str) -> bool:
    """A mission adopts ``key``. Called INSIDE the adoption's own fence
    (`session_input.sessions_transaction`, which already withdrew the session's native tokens):
    only this store's flock is taken here, never `_lock` again. A session HQ drove becomes the
    Commander's (and the mission's while it holds it), at a new generation, so nothing HQ staged
    before can run after the mission lets go. Returns whether a record changed. Blocking."""
    with json_write_lock(path()):
        doc = read_json_doc(path())
        sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
        cur = _coerce(sessions.get(key))
        if cur is None or cur["who"] != HQ:
            return False
        sessions[key] = {**cur, "who": COMMANDER, "generation": cur["generation"] + 1,
                         "since": time.time()}  # fmt: skip
        atomic_write_json(path(), {"sessions": sessions}, mode=0o600)
        return True


# ---- what a session's driver is, as everyone reads it ------------------------------------------


def _held(key: str, missions) -> str | None:
    held = missions.all_active_memberships().get(key)
    return held if isinstance(held, str) and held else None


def view_of(key: str, rec: dict | None, held: str | None) -> dict:
    """The driver as the header, HQ's tools and the session list read it. Pure."""
    gen = rec["generation"] if rec is not None else 0
    if held:
        return {"who": MISSION, "generation": gen, "mission_id": held, "cid": None}
    if rec is None:
        return {"who": COMMANDER, "generation": 0, "cid": None, "since": None}
    return {"who": rec["who"], "generation": gen, "cid": rec["cid"], "since": rec["since"]}


def view(key: str) -> dict:
    """One session's driver, read now: one store read and one membership read. Blocking."""
    from . import missions

    try:
        held = _held(key, missions)
    except Exception:  # noqa: BLE001 — unreadable membership: never reported as HQ's
        return {"who": "unknown", "generation": None, "cid": None}
    return view_of(key, record(key), held)


def views(keys) -> dict[str, dict]:
    """Many sessions' drivers in ONE store read and ONE membership read (`list_sessions`), never a
    read per row and never a rewrite. Blocking."""
    from . import missions

    keys = list(keys)
    recs = records()
    try:
        held = missions.all_active_memberships()
    except Exception:  # noqa: BLE001
        return {k: {"who": "unknown", "generation": None, "cid": None} for k in keys}
    return {k: view_of(k, recs.get(k), held.get(k)) for k in keys}


# ---- the session's own facts, read synchronously (inside the fence) -----------------------------


def newest_operator_turn(key: str) -> str | None:
    """The newest operator turn id in API session ``key``'s journal (a withdrawn message is not
    part of the conversation): the SAME read at staging and inside the admission, so the two
    agree by construction. Raises when the journal can't be read. Blocking."""
    from . import engines, native_journal, native_runtime

    prov, native = engines.parse_key(key)
    journal = native_runtime._journal(prov, native)
    removed = native_journal.withdrawn(journal)
    last = None
    for op in journal.operations.values():
        if op.request.get("action") == "submit" and op.operation_id not in removed:
            last = op.operation_id
    return last


def bypass_now(key: str) -> bool:
    """Whether API session ``key`` skips permission prompts now (its record, or an enabled
    change). Raises when the record can't be read. Blocking."""
    from . import engines, native_runtime

    prov, native = engines.parse_key(key)
    _prov, rec = native_runtime._session(prov.engine_id, native, retiring_ok=True)
    return bool(native_runtime._effective_bypass(rec))


# ---- the admission ------------------------------------------------------------------------------


def check(key: str, binding: dict) -> tuple[bool, str]:
    """Whether HQ's drive request ``binding`` (``{pid, cid, generation, last_turn, verb}``) may
    reach ``key`` NOW: HQ drives it, from that conversation, at that generation; no mission holds
    it; and (send, interrupt) its newest operator turn is still the one HQ staged against. Fails
    closed: anything unreadable refuses. Blocking; it takes no lock (inside the admission it runs
    under `session_input._lock` and the fence, and must not take `_lock` itself)."""
    from . import missions

    try:
        if _held(key, missions):
            return False, MISSION_HELD
        rec = record(key)
        if rec is None:
            return False, NOT_DRIVEN
        if rec["who"] != HQ:
            return False, TAKEN_OVER
        if rec["cid"] != binding.get("cid"):
            return False, OTHER_CONVERSATION
        if rec["generation"] != binding.get("generation"):
            return False, MOVED
        bound = binding.get("verb") in ("send", "interrupt")
        if bound and newest_operator_turn(key) != binding.get("last_turn"):
            return False, NEWER_TURN
    except Exception as exc:  # noqa: BLE001 — unverifiable authority admits nothing
        return False, f"who drives this session could not be read ({type(exc).__name__})"
    return True, ""


def binding_of(raw: object) -> dict | None:
    """A drive binding as the in-process scope carries it (`asgi_call.DRIVE`), or None. Shape
    only: server code built it."""
    if not isinstance(raw, dict) or raw.get("verb") not in VERBS:
        return None
    if not isinstance(raw.get("pid"), str) or not raw["pid"]:
        return None
    if not isinstance(raw.get("cid"), str) or type(raw.get("generation")) is not int:
        return None
    return dict(raw)


def guard(key: str, binding: dict, *, act_admit=None, attended: bool = False):
    """The `ExecutionGuard` an HQ drive turn or interrupt is admitted by: `check` and the act's
    grant (``act_admit``, an HQ act's; absent on a tap) run inside `session_input.admit_native`,
    where withdrawals commit; the worker re-checks the tokens it returns at its claim.
    ``attended``: the Commander tapped the card (their own decision may go into a session that
    skips permission prompts, as their typed message may); an act is never attended, so the
    native runtime refuses it in such a session at its own boundary too. Returns ``(guard,
    refused)``: ``refused["grant"]`` names a refusal by the act's grant (the card returns to the
    Commander), else the refusal was the driver's."""
    from . import mission_structured, session_input
    from .structured_types import ExecutionGuard

    refused = {"grant": "", "why": ""}

    def fenced() -> tuple[bool, str]:
        ok, why = check(key, binding)
        if ok and act_admit is not None:
            try:
                why = act_admit() or ""
            except Exception as exc:  # noqa: BLE001 — fail closed
                why = f"HQ's authority could not be checked ({type(exc).__name__})"
            if why:
                refused["grant"] = why
                ok = False
        refused["why"] = "" if ok else why
        return ok, why

    g = ExecutionGuard(
        f"hq-drive:{binding['pid']}",
        mission_structured._in_process_only,
        admit=lambda: session_input.admit_native(key, fenced),
        attended=attended is True,
    )
    return g, refused
