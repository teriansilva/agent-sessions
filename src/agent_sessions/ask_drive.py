"""HQ drives API sessions: the catalog's drive entries (#2397, epic #2070 A7).

``session.send`` (the next turn), ``session.interrupt`` (stop the running turn),
``session.withdraw`` (take back a queued turn HQ sent) and ``session.adopt`` (HQ drives this
session from this conversation). Each is an ordinary catalog operation (`ask_operations`): a card
under *Ask*, at once under *Act*, D4 after untrusted text, the claim, the second admission right
before the route, settlement as the route's facts. What they add:

* **Facts** (`facts`): the session's title, archive state and runtime, who drives it relative to
  THIS conversation (``here`` / ``other`` / ``commander`` / ``mission`` / ``none``), the driver's
  ``generation``, its permission mode (``bypass``) and its newest operator turn (``last_turn``),
  plus the running turn (interrupt) or whether HQ's turn is still queued (withdraw). Bound at
  staging, re-read at the claim (the class) and before the route (`ask_operations.changed`): a
  take-over, a hand-back, an adoption, a mission, a newer turn or a permission change since
  staging is ``stale``, nothing sent.
* **A binding** (`Operation.drive`): ``{pid, cid, generation, last_turn, verb, title}`` rides into
  the route's in-process scope, never its body, and the route admits the request by it INSIDE the
  write fence (`session_driver.guard`), where a take-over commits.
* **Ids from the card**: the turn, the interrupt and the withdraw use ids derived from the
  proposal id (`session_driver.*_operation_id`), so an act and a tap of one card send one turn.
* **The class** (`hq_authority.effective_class`): *starts*, or *external* for a send or adopt into
  a session that skips permission prompts (a card until A1c-external).

Only API sessions (`mission_structured.is_api_key`, the manifest's answer; no engine is named
here), and never a session a mission holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable

from . import session_driver

#: The longest text a drive send carries: the card shows it whole inside its summary sentence, so
#: a goal that needs more is several steps.
SEND_MAX = 320
#: The facts an operation may store (`ask_store.OP_FIELDS_MAX`): the drive facts stay within it.
DRIVE_OPS = frozenset({"session.send", "session.interrupt", "session.withdraw", "session.adopt"})


def _message(v: object) -> str:
    from . import handoff
    from .ask_operations import Refused

    if not isinstance(v, str):
        raise Refused("message must be a string")
    try:
        # The normalization delivery applies (control bytes out, trimmed), applied HERE: the text
        # the card shows verbatim, the text stored and the text sent are one string.
        text = handoff.sanitize_seed(v)
    except handoff.HandoffError:
        raise Refused("message has nothing sendable in it") from None
    if not text:
        raise Refused("message must not be empty")
    if len(text) > SEND_MAX:
        raise Refused(f"message is longer than {SEND_MAX} characters: send one clear step")
    return text


def _turn_id(v: object) -> str:
    from .ask_operations import Refused

    try:
        if isinstance(v, str) and str(uuid.UUID(v)) == v:
            return v
    except ValueError:
        pass
    raise Refused("turn_id must be a turn id session_turns returned")


GATES = {"message": _message, "turn_id": _turn_id}


def moved(now: object) -> str:
    """Why a drive card is out of date, by who drives the session now."""
    return {
        "mission": "a mission holds it now",
        "commander": "the Commander took it over",
        "other": "another HQ conversation drives it now",
        "here": "HQ drives it from here now",
    }.get(str(now), "who drives it changed")


def _relation(view: dict, cid: str) -> str:
    who = view.get("who")
    if who == session_driver.MISSION:
        return "mission"
    if who == session_driver.HQ:
        return "here" if view.get("cid") == cid else "other"
    if who == session_driver.COMMANDER and view.get("generation"):
        return "commander"
    return "none"


def _drive_facts(key: str, cid: str, op_id: str) -> dict:
    """The driver, permission mode and (send, interrupt) the newest turn, read now: one store
    read and one membership read. Blocking."""
    from . import missions

    rec = session_driver.record(key)
    held = missions.all_active_memberships().get(key)
    out = {
        "driver": _relation(session_driver.view_of(key, rec, held), cid),
        "generation": int((rec or {}).get("generation") or 0),
        "bypass": session_driver.bypass_now(key),
    }
    if op_id in ("session.send", "session.interrupt"):
        # A newer turn makes a send or a stop stale; a withdraw or an adopt does not care.
        out["last_turn"] = session_driver.newest_operator_turn(key)
    return out


async def facts(op, target: str, fields: dict, app, operator, *, cid: str = "") -> dict | None:
    """A drive card's facts (≤ `ask_store.OP_FIELDS_MAX` of them), or None when the session is
    gone. A terminal session's facts say so (``runtime``); `refusal` refuses it at staging."""
    from . import ask_operations
    from . import structured_runtime as runtime

    base = await ask_operations._session_facts(app, operator, target)
    if base is None:
        return None
    out = {k: base[k] for k in ("title", "archived", "runtime")}
    if base["runtime"] != "api":
        return out
    try:
        out.update(await asyncio.to_thread(_drive_facts, target, cid, op.id))
    except Exception:  # noqa: BLE001 — a session that can't be read is not driven
        return None
    if op.id == "session.interrupt":
        try:
            snap = await runtime.snapshot(target)
        except runtime.StructuredError:
            return None
        active = snap.get("active_turn")
        out["active"] = str(active) if active else None
    elif op.id == "session.withdraw":
        try:
            snap = await runtime.snapshot(target)
        except runtime.StructuredError:
            return None
        turn = next(
            (t for t in snap.get("turns") or [] if t.get("turn_id") == fields.get("turn_id")),
            None,
        )
        out["queued"] = bool(turn) and turn.get("state") == "queued"
    return out


def refusal(op_id: str, cid: str, facts: dict, fields: dict, *, own_turn: bool = False) -> tuple:
    """Why a drive card is not staged at all, ``(message, code)``, or ``("", "")``. Server facts
    only. ``own_turn``: (withdraw) the turn named is one of THIS conversation's drive sends."""
    if facts.get("runtime") != "api":
        return "HQ drives API sessions only; a terminal session gets send_nudge", "not_api"
    if facts.get("archived"):
        return "that session is archived", "not_available"
    driver = facts.get("driver")
    if driver == "mission":
        return (
            "a mission holds that session; the Commander messages it from the mission's console",
            "mission_held",
        )
    if op_id == "session.adopt":
        if driver == "here":
            return "HQ already drives that session from this conversation", "already_driven"
        return "", ""
    if driver == "commander":
        return (
            "the Commander took that session over by typing in it: stop steering it and tell "
            "them; adopt it only when they ask you to take it back",
            "taken_over",
        )
    if driver == "other":
        return (
            "another HQ conversation drives that session; adopt it from here only when the "
            "Commander asks",
            "other_conversation",
        )
    if driver != "here":
        return (
            "HQ does not drive that session; adopt it first, only when the Commander asks",
            "not_driven",
        )
    if op_id == "session.interrupt" and not facts.get("active"):
        return "nothing is running in that session, so there is nothing to stop", "idle"
    if op_id == "session.withdraw":
        if not own_turn:
            return "HQ withdraws only a message it sent from this conversation", "not_yours"
        if not facts.get("queued"):
            return "that message is no longer queued", "not_queued"
    return "", ""


# ---- routes, outcomes ---------------------------------------------------------------------------


def _binding(verb: str):
    def build(key: str, f: dict, x: dict) -> dict:
        return {
            "pid": f["_pid"],
            "cid": f["_cid"],
            "generation": int(x.get("generation") or 0),
            "last_turn": x.get("last_turn"),
            "verb": verb,
            "title": str(x.get("title") or "")[: session_driver.TITLE_MAX],
        }

    return build


def _send_route(key: str, f: dict, x: dict) -> tuple[str, str, dict | None]:
    body = {"operation_id": session_driver.send_operation_id(f["_pid"]), "text": f["message"]}
    return "POST", f"/api/structured/sessions/{key}/turns", body


def _interrupt_route(key: str, f: dict, x: dict) -> tuple[str, str, dict | None]:
    body = {
        "operation_id": session_driver.interrupt_operation_id(f["_pid"]),
        "turn_id": str(x.get("active") or ""),
    }
    return "POST", f"/api/structured/sessions/{key}/interrupt", body


def _withdraw_route(key: str, f: dict, x: dict) -> tuple[str, str, dict | None]:
    body = {
        "operation_id": session_driver.withdraw_operation_id(f["_pid"]),
        "queued_turn_id": f["turn_id"],
    }
    return "POST", f"/api/structured/sessions/{key}/withdraw", body


def _adopt_route(key: str, f: dict, x: dict) -> tuple[str, str, dict | None]:
    return "POST", f"/api/sessions/{key}/driver/adopt", {"generation": int(x["generation"])}


def _driver_refused(status: int, body: object) -> str:
    """The route's closed refusal (the driver moved): its words, or ""."""
    if status == 409 and isinstance(body, dict) and body.get("reason") == "driver":
        return str(body.get("detail") or "not sent: who drives this session changed")
    return ""


def _send_outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED, STALE, UNCONFIRMED

    why = _driver_refused(status, body)
    if why:
        return STALE, f"{why}; nothing was sent"
    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    code = body.get("handoff")
    if code == "sent":
        return DONE, "Sent into the session."
    if code == "queued":
        return DONE, "Queued behind the running turn."
    if code == "not_sent":
        return FAILED, "The agent did not take it; nothing was sent."
    return UNCONFIRMED, "It may or may not have reached the agent. It is never sent twice."


def _interrupt_outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED, STALE

    why = _driver_refused(status, body)
    if why:
        return STALE, f"{why.replace('not sent', 'not stopped')}; nothing was stopped"
    if not 200 <= status < 300:
        return FAILED, ""
    return DONE, "Asked the running turn to stop. Acknowledged, not yet confirmed stopped."


def _withdraw_outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED, STALE

    why = _driver_refused(status, body)
    if why:
        return STALE, f"{why.replace('not sent: ', '')}; nothing was withdrawn"
    if not 200 <= status < 300:
        return FAILED, ""
    return DONE, "Withdrawn before it was sent."


def _adopt_outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED, STALE

    why = _driver_refused(status, body)
    if why:
        return STALE, f"{why.replace('not sent: ', '')}; HQ did not take it over"
    if not 200 <= status < 300:
        return FAILED, ""
    return DONE, "HQ drives this session from this conversation now. Typing in it takes it back."


def drive_ops():
    from .ask_operations import Operation

    s = "session"
    return (
        Operation(
            "session.send",
            s,
            "send",
            "Send to session",
            "Send",
            "external",
            s,
            ("message",),
            _send_route,
            lambda f, x: f"Sends this as the session's next message: “{f['message']}”",
            _send_outcome,
            lambda st, d, x: f"Not sent: {d or 'refused'}. Nothing was sent.",
            drive=_binding("send"),
        ),
        Operation(
            "session.interrupt",
            s,
            "interrupt",
            "Interrupt",
            "Interrupt",
            "external",
            s,
            (),
            _interrupt_route,
            lambda f, x: "Stops the turn the session is running now. Its conversation is kept.",
            _interrupt_outcome,
            lambda st, d, x: f"Not stopped: {d or 'refused'}.",
            drive=_binding("interrupt"),
        ),
        Operation(
            "session.withdraw",
            s,
            "withdraw",
            "Withdraw message",
            "Withdraw",
            "reversible",
            s,
            ("turn_id",),
            _withdraw_route,
            lambda f, x: "Takes back the message HQ queued in it, before it is sent.",
            _withdraw_outcome,
            lambda st, d, x: f"Not withdrawn: {d or 'refused'}.",
            drive=_binding("withdraw"),
        ),
        Operation(
            "session.adopt",
            s,
            "adopt",
            "Drive from HQ",
            "Let HQ drive",
            "reversible",
            s,
            (),
            _adopt_route,
            lambda f, x: "HQ drives this session from this conversation: it sends the next steps "
            "and reads the replies. Typing in it takes it back.",
            _adopt_outcome,
            lambda st, d, x: f"Not taken over: {d or 'refused'}.",
            drive=_binding("adopt"),
        ),
    )


# ---- waiting on a session (`wait_session`) ------------------------------------------------------

#: How often the session's journal is looked at (a file stat), how soon after a change it is read
#: again at most, and how often it is read anyway (a turn whose worker ended settles in the
#: snapshot, not in the journal). Measured before merge: 50 waits cost the server what ONE
#: watcher per session costs, whatever the number of waiters.
WATCH_TICK_S = 0.5
WATCH_THROTTLE_S = 2.0
WATCH_RESNAP_S = 15.0


class _Watch:
    """ONE watcher per session, shared by every `wait_session` on it: it stats the journal and
    takes a snapshot when it moved (at most every `WATCH_THROTTLE_S`, and every `WATCH_RESNAP_S`
    regardless), and wakes the waiters. It holds no lock while waiting and ends with its last
    waiter."""

    def __init__(self, key: str):
        self.key = key
        self.waiters = 0
        self.version = 0
        self.snap: dict | None = None
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        #: Events of multi-session waits (#2398 `wait`) set on every change, beside `changed`.
        self.listeners: set[asyncio.Event] = set()
        #: The one snapshot a joining `wait` reads before the watcher's first tick (#2398): shared,
        #: so 50 calls joining at once read the session once, not 50 times.
        self._first: asyncio.Future | None = None

    async def current(self) -> dict | None:
        """The session as the watcher last read it, or ONE shared read when it has none yet.
        None when it can't be read."""
        if self.snap is not None:
            return self.snap
        if self._first is None:
            from . import structured_runtime as runtime

            async def read() -> dict | None:
                try:
                    return await runtime.snapshot(self.key)
                except Exception:  # noqa: BLE001 — not there (yet) or unreadable
                    return None

            self._first = asyncio.ensure_future(read())
        snap = await asyncio.shield(self._first)
        if self.snap is None and snap is not None:
            self.snap = snap
        if snap is None:
            self._first = None  # absent now: the next joiner reads again
        return snap

    async def run(self) -> None:
        from . import structured_runtime as runtime

        loop = asyncio.get_running_loop()
        stamp: Callable[[], tuple | None] | None = None
        with contextlib.suppress(Exception):
            stamp = await runtime.journal_stamp(self.key)
        seen = stamp() if stamp is not None else None
        # The first tick reads the session once: a turn that settled between a waiter's own read
        # and this stamp is never left to the periodic read.
        last = loop.time() - WATCH_RESNAP_S
        while self.waiters > 0:
            await asyncio.sleep(WATCH_TICK_S)
            now = stamp() if stamp is not None else None
            since = loop.time() - last
            if not ((now != seen and since >= WATCH_THROTTLE_S) or since >= WATCH_RESNAP_S):
                continue
            seen = now
            try:
                self.snap = await runtime.snapshot(self.key)
            except Exception:  # noqa: BLE001 — unreadable: the waiters read "unavailable"
                self.snap = None
            last = loop.time()
            self.version += 1
            self.changed.set()
            self.changed = asyncio.Event()
            for ev in list(self.listeners):
                ev.set()


_WATCHES: dict[str, _Watch] = {}


@contextlib.asynccontextmanager
async def watch(key: str):
    """Join the session's one watcher for the duration of a wait."""
    w = _WATCHES.get(key)
    if w is None:
        w = _WATCHES[key] = _Watch(key)
    w.waiters += 1
    if w.task is None or w.task.done():
        w.task = asyncio.ensure_future(w.run())
    try:
        yield w
    finally:
        w.waiters -= 1
        if w.waiters <= 0:
            _WATCHES.pop(key, None)
            if w.task is not None:
                w.task.cancel()
