"""HQ's step lists (#2251): a multi-part request sorted into ordered steps, one card at a time.

When a request needs more than one card, HQ first lays out a STEP LIST (`propose_steps`) and then
stages only the CURRENT step's card. The Commander confirms it; the step is done; HQ continues
once (`ask_orchestrator.kick_steps`) and stages the next step's card from fresh reads, so a step
that depends on an earlier one is staged only once that one's outcome exists.

**Structure is stored, state is derived.** The sidecar (`ask_store.read_steps`) holds only the
lists' structure: immutable step ids (``sid``), titles, order, ``needs``, which cards belong to
which step, ``skipped`` and ``ended``. A step's state is computed when it is read, from its cards'
states in the proposal sidecar — the one source of truth for cards. Nothing here is a second copy
of an outcome, so a crash between a card settling and anything else loses nothing: the next read
derives the same state.

**Exactly once.** What a settlement owes (an advance to the next step, a hold to ask about, or the
finish) is derived too (:func:`owed`), as a key. The turn that takes it up records that key on its
own user record, in the same append as the turn (`ask_orchestrator.begin`, under the turn lock), so
a key already in the records is never begun again — after a duplicate settlement, a restart, or a
crash at any point in between.

Display numbers ("Step 2") are derived from the current order and are never stored or used as a
reference: every reference — ``needs``, card membership, a card's ``for_step``, a revision — is a
``sid``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets as _secrets
import time

from . import ask_store

#: A card state that completes its step: an operation done, a session started (it got its brief;
#: its WORK is not tracked here), a message sent.
SUCCESS = frozenset({"done", "started", "sent"})
#: Card states still in flight: nothing to derive from them yet.
IN_FLIGHT = frozenset({"pending", "deciding", "creating"})
#: Card states that can still be withdrawn (nothing of them ran).
WITHDRAWABLE = frozenset({"pending"})
#: Card states a withdrawal must wait for: they are running.
BUSY = frozenset({"deciding", "creating"})

ACTIVE, HELD, FINISHED, ENDED = "active", "held", "finished", "ended"
NOW, NEXT, DONE, STEP_HELD, SKIPPED, STEP_ENDED = "now", "next", "done", "held", "skipped", "ended"

#: The longest a replacement chain is followed (each replacement names the card it replaced).
CHAIN_MAX = 50


class StepsRefused(Exception):
    """A step-list change that can't be made; the text tells HQ what to do instead."""

    def __init__(self, text: str, code: str = "steps", lst: dict | None = None):
        super().__init__(text)
        self.text = text
        self.code = code
        self.lst = lst


def new_list_id() -> str:
    return _secrets.token_hex(8)


def new_sid(taken: set[str]) -> str:
    while True:
        sid = "s" + _secrets.token_hex(4)
        if sid not in taken:
            return sid


def _digest(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ---- derivation ---------------------------------------------------------------------------------


def _live(ids: list[str], rows: dict[str, dict]) -> list[str]:
    """The cards a step holds NOW: each id followed along `replaced_by` to the card that replaced
    it (#2120), so a replacement takes its slot and is never mistaken for a dismissal."""
    out: list[str] = []
    for pid in ids:
        cur, hops = pid, 0
        while hops < CHAIN_MAX:
            nxt = (rows.get(cur) or {}).get("replaced_by")
            if not nxt or (rows.get(cur) or {}).get("state") != "dismissed":
                break
            cur, hops = nxt, hops + 1
        if cur not in out:
            out.append(cur)
    return out


def _state_of(pid: str, rows: dict[str, dict]) -> str:
    row = rows.get(pid)
    return str(row.get("state")) if row is not None else "gone"


def _said(row: dict | None) -> str:
    if not row:
        return ""
    return str(row.get("outcome") or row.get("reason") or "")[: ask_store.DETAIL_MAX]


def derive(lst: dict, rows: dict[str, dict]) -> dict:
    """One list's view: every step's derived state, the current step, the list's status. ``rows``
    is the proposal sidecar by id. Pure."""
    steps_out: list[dict] = []
    skipped: set[str] = set()
    current: str | None = None
    status = ENDED if lst.get("ended") else FINISHED
    for n, st in enumerate(lst["steps"], start=1):
        cards = _live(st.get("cards") or [], rows)
        states = [_state_of(c, rows) for c in cards]
        view = {
            "sid": st["sid"],
            "n": n,
            "title": st["title"],
            "detail": st.get("detail") or "",
            "needs": list(st.get("needs") or []),
            "kind": st.get("kind") or "",
            "cards": cards,
            "state": NEXT,
            "reason": "",
            "outcome": "",
            "settled_at": None,
        }
        settled = [
            (rows.get(c) or {}).get("settled_at")
            for c in cards
            if isinstance((rows.get(c) or {}).get("settled_at"), int | float)
        ]
        if settled:
            view["settled_at"] = max(settled)
        if st.get("skipped"):
            view["state"] = SKIPPED
            done = sum(1 for s in states if s in SUCCESS)
            if done:
                view["outcome"] = f"{done} of {len(cards)} had already run"
            skipped.add(st["sid"])
        elif cards and all(s in SUCCESS for s in states):
            view["state"] = DONE
            if len(cards) == 1:
                view["outcome"] = _said(rows.get(cards[0]))
            else:
                view["outcome"] = f"all {len(cards)} done"
            view["started"] = all(s == "started" for s in states)
        elif lst.get("ended"):
            view["state"] = STEP_ENDED
        elif current is not None:
            view["state"] = NEXT
        else:
            current = st["sid"]
            missing = [s for s in view["needs"] if s in skipped]
            if missing:
                view["state"] = STEP_HELD
                view["reason"] = "needs_skipped"
                view["needs_skipped"] = missing
                status = HELD
            elif not cards or any(s in IN_FLIGHT or s == "unknown" for s in states):
                view["state"] = NOW
                status = ACTIVE
            else:
                view["state"] = STEP_HELD
                view["reason"] = "card"
                bad = [c for c in cards if _state_of(c, rows) not in SUCCESS]
                good = len(cards) - len(bad)
                if len(cards) > 1:
                    view["outcome"] = f"{good} of {len(cards)} done; {len(bad)} did not"
                else:
                    view["outcome"] = _said(rows.get(bad[0])) or _state_of(bad[0], rows)
                status = HELD
        view["card_states"] = {c: _state_of(c, rows) for c in cards}
        steps_out.append(view)
    return {
        "id": lst["id"],
        "revision": lst["revision"],
        "title": lst.get("title") or "",
        "status": status,
        "current": current,
        "drawn_at_turn": lst.get("drawn_at_turn"),
        "created_turn": lst.get("created_turn") or lst.get("drawn_at_turn"),
        "steps": steps_out,
    }


def owed(view: dict) -> tuple[str, str] | None:
    """What this list's settlements owe right now, as ``(key, kind)``: ``next`` — a step is done
    and the current one has no card yet; ``hold`` — the current step is held; ``done`` — every
    step settled. ``None`` when nothing is owed (a card is in flight, the list ended, or the list
    was just laid out and its first card is the turn's own business). Pure."""
    if view["status"] == ENDED:
        return None
    steps = view["steps"]
    cur = next((s for s in steps if s["sid"] == view["current"]), None)
    if cur is not None and cur["state"] == STEP_HELD:
        basis = [cur["reason"], *cur.get("needs_skipped", [])]
        basis += [f"{c}={cur['card_states'].get(c)}" for c in cur["cards"]]
        return f"hold:{view['id']}:{cur['sid']}:{_digest(*basis)}", "hold"
    done_before = [s for s in steps if s["state"] == DONE]
    if cur is not None:
        if cur["cards"] or cur["state"] != NOW:
            return None
        before = [s for s in steps[: cur["n"] - 1] if s["state"] == DONE]
        if not before:
            return None
        prev = before[-1]
        return f"next:{view['id']}:{prev['sid']}:{_digest(*prev['cards'])}", "next"
    if view["status"] == FINISHED and done_before:
        last = done_before[-1]
        return f"done:{view['id']}:{last['sid']}:{_digest(*last['cards'])}", "done"
    return None


def begun(records: list[dict]) -> set[str]:
    """Every step-list key a turn of this conversation already took up."""
    keys: set[str] = set()
    for r in records:
        if r.get("type") == "user" and isinstance(r.get("steps_key"), str):
            keys.add(r["steps_key"])
        elif r.get("type") == "status" and isinstance(r.get("steps_seen"), list):
            keys.update(k for k in r["steps_seen"] if isinstance(k, str))
    return keys


def _rows(cid: str) -> dict[str, dict]:
    return {r["id"]: r for r in ask_store.read_proposals(cid) if isinstance(r.get("id"), str)}


def read(cid: str) -> tuple[list[dict], list[dict]]:
    """``(stored lists, derived views)``, oldest first. Raises SidecarUnreadable. Blocking."""
    lists = ask_store.read_steps(cid)
    if not lists:
        return [], []
    rows = _rows(cid)
    return lists, [derive(x, rows) for x in lists]


def latest_view(cid: str) -> dict | None:
    """The newest list's view, or None. Raises SidecarUnreadable. Blocking."""
    _lists, views = read(cid)
    return views[-1] if views else None


def open_view(cid: str) -> dict | None:
    """The list a card may still belong to: the newest, while it is active or held."""
    view = latest_view(cid)
    return view if view is not None and view["status"] in (ACTIVE, HELD) else None


def owed_now(cid: str, records: list[dict]) -> tuple[str, str, dict] | None:
    """The newest list's owed key not yet taken up by a turn: ``(key, kind, view)``. Blocking."""
    view = latest_view(cid)
    if view is None:
        return None
    got = owed(view)
    if got is None or got[0] in begun(records):
        return None
    return got[0], got[1], view


def card_list(cid: str, pid: str) -> tuple[dict, dict] | None:
    """``(list view, step view)`` holding card ``pid`` (or a card it replaced). Blocking."""
    _lists, views = read(cid)
    for v in views:
        for s in v["steps"]:
            if pid in s["cards"]:
                return v, s
    return None


def public(view: dict | None, records: list[dict] | None = None) -> dict | None:
    """The list as the client draws it, with ``owed``: HQ is about to continue, keep reading."""
    if view is None:
        return None
    keys = ("id", "revision", "title", "status", "current", "drawn_at_turn", "created_turn")
    out = {k: view.get(k) for k in keys}
    out["steps"] = [
        {
            k: s.get(k)
            for k in (
                "sid",
                "n",
                "title",
                "detail",
                "needs",
                "state",
                "reason",
                "outcome",
                "cards",
                "settled_at",
                "started",
            )
        }  # fmt: skip
        for s in view["steps"]
    ]
    got = owed(view)
    out["owed"] = got is not None and (records is None or got[0] not in begun(records))
    return out


# ---- writes (each under the proposal sidecar's lock) ---------------------------------------------


def _check_needs(steps: list[dict]) -> None:
    seen: set[str] = set()
    for st in steps:
        for n in st.get("needs") or []:
            if n not in seen:
                raise StepsRefused(
                    f"step '{st['title']}' needs a step that does not come before it; a step may "
                    "only need steps before it (no forward dependency, no cycle)",
                    "needs",
                )
        seen.add(st["sid"])


def _kind_ok(kind: object) -> bool:
    return isinstance(kind, str) and ask_store.STEP_KIND_RE.match(kind) is not None


#: An operation's group → the kind key its staging tool gives (`ask_orchestrator._card_kind`).
_OP_KINDS = {
    "project": "propose_project_change",
    "projects": "propose_project_change",
    "session": "propose_session_change",
    "sessions": "propose_session_change",
    "mission": "propose_mission",
    "team": "team",
    "secret": "propose_secret",
    "mcp": "propose_mcp",
    "memory": "propose_hq_memory_change",
}


def kind_of_row(row: dict) -> str:
    """A stored card's kind key, as its staging tool gave it."""
    if row.get("kind") == "session":
        return "propose_session"
    if row.get("kind") == "message":
        return "propose_message"
    group = str(row.get("op") or "").split(".")[0]
    if group in ("playbook", "skill", "automation", "specialist"):
        return f"library:{group}"
    return _OP_KINDS.get(group, "propose_" + group if group.isidentifier() else "")


def loose_cards(lists: list[dict], rows: dict[str, dict]) -> list[str]:
    """Waiting cards that belong to no step list: what a new list must adopt or see withdrawn."""
    members: set[str] = set()
    for lst in lists:
        for st in lst["steps"]:
            members.update(st.get("cards") or [])
            members.update(_live(st.get("cards") or [], rows))
    return [pid for pid, r in rows.items() if r.get("state") == "pending" and pid not in members]


def create(
    cid: str, turn_id: str, title: str, steps_in: list[dict], adopt: list[str] | None = None
) -> dict:
    """A new list from HQ's ``steps`` (``{"title", "detail"?, "needs"?: [index…]}``, needs naming
    EARLIER entries by their 0-based position). Refused while the newest list is still open.

    Every card still WAITING outside a step list is reconciled first (Hermes on #2251): ``adopt``
    names those that are the first step's card (one kind) and they join that step; any other
    refuses the list, named, so HQ withdraws it — a new list never leaves a live card outside the
    wizard. Returns its view. Blocking."""
    if not isinstance(steps_in, list) or not 2 <= len(steps_in) <= ask_store.STEPS_PER_LIST_MAX:
        raise StepsRefused(
            f"a step list has 2 to {ask_store.STEPS_PER_LIST_MAX} steps; one card needs no list",
            "bad_arguments",
        )
    with ask_store.proposals_lock(cid):
        lists = ask_store.read_steps(cid)
        rows = _rows(cid)
        if lists:
            last = derive(lists[-1], rows)
            if last["status"] in (ACTIVE, HELD):
                raise StepsRefused(
                    "a step list is still open in this conversation — revise it (revise_steps), "
                    "or end it first",
                    "open_list",
                    last,
                )
        loose = loose_cards(lists, rows)
        adopt = list(dict.fromkeys(adopt or []))
        stray = [a for a in adopt if a not in loose]
        if stray:
            raise StepsRefused(
                "adopt names a card that is not waiting outside a step: "
                + ", ".join(map(str, stray)),
                "adopt",
            )
        left = [p for p in loose if p not in adopt]
        if left:
            raise StepsRefused(
                f"{len(left)} card(s) still wait outside any step: {', '.join(left)}. A step list "
                "leaves no card live outside it: pass the first step's card in adopt, and "
                "withdraw the others (withdraw_card), then lay out the steps",
                "loose_cards",
            )
        kinds = {kind_of_row(rows[a]) for a in adopt}
        if len(kinds) > 1:
            raise StepsRefused(
                "adopted cards must be one kind (the first step's); withdraw the others", "adopt"
            )
        taken: set[str] = set()
        steps: list[dict] = []
        for i, raw in enumerate(steps_in):
            if not isinstance(raw, dict):
                raise StepsRefused("each step is an object with a title", "bad_arguments")
            t = raw.get("title")
            if not isinstance(t, str) or not t.strip():
                raise StepsRefused("each step needs a title", "bad_arguments")
            needs_idx = raw.get("needs") or []
            if not isinstance(needs_idx, list) or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x < i for x in needs_idx
            ):
                raise StepsRefused(
                    "needs lists the 0-based positions of EARLIER steps only (no forward "
                    "dependency, no cycle)",
                    "needs",
                )
            sid = new_sid(taken)
            taken.add(sid)
            d = raw.get("detail")
            steps.append(
                {
                    "sid": sid,
                    "title": t.strip(),
                    "detail": d.strip() if isinstance(d, str) else "",
                    "needs": [steps[x]["sid"] for x in needs_idx],
                    "cards": [],
                }
            )
        if adopt:
            steps[0]["cards"] = list(adopt)
            kind = next(iter(kinds))
            if _kind_ok(kind):
                steps[0]["kind"] = kind
        lst = {
            "id": new_list_id(),
            "revision": 1,
            "title": (title or "").strip() or steps[0]["title"],
            "ended": False,
            "steps": steps,
            "created_turn": turn_id,
            "drawn_at_turn": turn_id,
            "ts": time.time(),
        }
        written = ask_store.write_steps(cid, [*lists, lst][-ask_store.STEP_LISTS_MAX :])
    return derive(written[-1], rows)


def attach(cid: str, list_id: str, sid: str, pid: str, kind: str) -> int | None:
    """Card ``pid`` (kind key ``kind``) now belongs to step ``sid``. Settled cards of that step
    that did NOT succeed are retired from it (a corrected card replaces a failed one; the record
    stays in ``retired``). The list's new revision, or None when it moved on meanwhile. Blocking."""
    with ask_store.proposals_lock(cid):
        lists = ask_store.read_steps(cid)
        if not lists or lists[-1]["id"] != list_id or lists[-1].get("ended"):
            return None
        rows = _rows(cid)
        lst = lists[-1]
        st = next((s for s in lst["steps"] if s["sid"] == sid), None)
        if st is None:
            return None
        keep, retired = [], list(st.get("retired") or [])
        for c in st.get("cards") or []:
            live = _live([c], rows)[0]
            state = _state_of(live, rows)
            if state in SUCCESS or state in IN_FLIGHT:
                keep.append(c)
            else:
                retired.append(c)
        st["cards"] = [*keep, pid]
        st["retired"] = retired
        if _kind_ok(kind):
            st["kind"] = kind
        lst["drawn_at_turn"] = rows.get(pid, {}).get("turn_id") or lst.get("drawn_at_turn")
        # Membership is structure: a plan read before it is stale (Hermes on #2251), so End steps
        # or a revision can never write back a list without this card.
        lst["revision"] = int(lst["revision"]) + 1
        ask_store.write_steps(cid, lists)
    return int(lst["revision"])


def relocate(cid: str, pid: str, turn_id: str) -> bool:
    """A card that replaced a step's card (#2120) moves its open list under the replacing turn
    (#2251 G6): the list is drawn once, under the newest turn that changed it. Blocking."""
    with ask_store.proposals_lock(cid):
        lists = ask_store.read_steps(cid)
        if not lists:
            return False
        rows = _rows(cid)
        lst = lists[-1]
        if derive(lst, rows)["status"] not in (ACTIVE, HELD):
            return False
        if not any(pid in _live(st.get("cards") or [], rows) for st in lst["steps"]):
            return False
        if lst.get("drawn_at_turn") == turn_id:
            return True
        lst["drawn_at_turn"] = turn_id
        ask_store.write_steps(cid, lists)
    return True


#: How long a step-list transition waits for the fence before it is refused as busy.
FENCE_WAIT_S = 10.0


@contextlib.asynccontextmanager
async def fence(cid: str):
    """The step-list fence (`ask_store.try_steps_lock`), held across one whole transition:
    check → stage → attach, or plan → withdraw → write. Raises StepsRefused("busy") when another
    transition holds it past `FENCE_WAIT_S`."""
    deadline = time.monotonic() + FENCE_WAIT_S
    while True:
        fd = await asyncio.to_thread(ask_store.try_steps_lock, cid)
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            raise StepsRefused(
                "the steps are being changed right now; nothing was done — try again", "busy"
            )
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)


def _pending_cards(view: dict, sids: set[str]) -> tuple[list[str], list[str]]:
    """The cards of steps ``sids``: ``(withdrawable, busy)``."""
    pend, busy = [], []
    for s in view["steps"]:
        if s["sid"] not in sids:
            continue
        for c, state in s["card_states"].items():
            if state in WITHDRAWABLE:
                pend.append(c)
            elif state in BUSY:
                busy.append(c)
    return pend, busy


#: The `revise_steps` operations.
OPS = ("edit", "add", "move", "skip", "end")


def plan_revision(cid: str, list_id: str, revision: int, ops: list[dict]) -> tuple[dict, list[str]]:
    """Validate ``ops`` against the newest list at ``revision`` and return ``(the list as it
    would be, the pending cards to withdraw first)``. Raises StepsRefused — with the current list
    when the revision is stale — and refuses outright when a card that would have to be withdrawn
    is running (busy): nothing that already began is ever cancelled. Blocking, read-only."""
    lists, views = read(cid)
    if not lists:
        raise StepsRefused("there is no step list in this conversation", "no_list")
    lst, view = lists[-1], views[-1]
    if lst["id"] != list_id:
        raise StepsRefused("that is not this conversation's newest step list", "stale", view)
    if lst["revision"] != revision:
        raise StepsRefused(
            "the step list changed since you read it — here it is now; revise that", "stale", view
        )
    if view["status"] not in (ACTIVE, HELD):
        raise StepsRefused("this step list is already " + view["status"], "closed", view)
    if not isinstance(ops, list) or not ops or len(ops) > 20:
        raise StepsRefused("ops is a list of 1 to 20 changes", "bad_arguments")
    new = {**lst, "steps": [dict(s) for s in lst["steps"]]}
    by_view = {s["sid"]: s for s in view["steps"]}

    def find(sid: object) -> dict:
        st = next((s for s in new["steps"] if s["sid"] == sid), None)
        if st is None:
            raise StepsRefused(f"no step has the id {sid!r}", "unknown_step")
        return st

    def settled_step(sid: str) -> bool:
        v = by_view.get(sid)
        return v is not None and (
            v["state"] in (DONE, SKIPPED)
            or any(state in SUCCESS for state in v["card_states"].values())
        )

    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise StepsRefused(f"each change is an object with op in {', '.join(OPS)}")
        kind = op["op"]
        if kind == "end":
            new["ended"] = True
            new["ended_at"] = time.time()
            continue
        if kind == "add":
            t = op.get("title")
            if not isinstance(t, str) or not t.strip():
                raise StepsRefused("an added step needs a title", "bad_arguments")
            # `after`: the sid it follows; "" = first; absent = last.
            after = op.get("after")
            if after is None:
                pos = len(new["steps"])
            elif after == "":
                pos = 0
            else:
                pos = new["steps"].index(find(after)) + 1
            needs = op.get("needs") or []
            if not isinstance(needs, list):
                raise StepsRefused("needs is a list of step ids", "bad_arguments")
            sid = new_sid({s["sid"] for s in new["steps"]})
            d = op.get("detail")
            new["steps"].insert(
                pos,
                {
                    "sid": sid,
                    "title": t.strip(),
                    "detail": d.strip() if isinstance(d, str) else "",
                    "needs": [str(x) for x in needs],
                    "cards": [],
                },
            )
            continue
        st = find(op.get("sid"))
        if kind == "edit":
            if settled_step(st["sid"]):
                raise StepsRefused(f"step '{st['title']}' has already run; it is not edited")
            for k, cap in (
                ("title", ask_store.STEP_TITLE_MAX),
                ("detail", ask_store.STEP_DETAIL_MAX),
            ):
                v = op.get(k)
                if v is not None:
                    if not isinstance(v, str) or (k == "title" and not v.strip()):
                        raise StepsRefused(f"{k} must be text", "bad_arguments")
                    st[k] = v.strip()[:cap]
            if op.get("needs") is not None:
                if not isinstance(op["needs"], list):
                    raise StepsRefused("needs is a list of step ids", "bad_arguments")
                st["needs"] = [str(x) for x in op["needs"]]
        elif kind == "skip":
            if st.get("skipped") or by_view.get(st["sid"], {}).get("state") == DONE:
                raise StepsRefused(f"step '{st['title']}' is already settled; nothing to skip")
            st["skipped"] = True
            st["skipped_at"] = time.time()
        elif kind == "move":
            if settled_step(st["sid"]):
                raise StepsRefused(f"step '{st['title']}' has already run; it does not move")
            after = op.get("after")
            new["steps"].remove(st)
            pos = 0 if after in (None, "") else new["steps"].index(find(after)) + 1
            new["steps"].insert(pos, st)
    _check_needs(new["steps"])
    pend = _displace(cid, new, view)
    return new, pend


def _displace(cid: str, new: dict, view: dict) -> list[str]:
    """What the RESULTING order takes off screen (Hermes on #2251): every step holding a card in
    flight that is not the current step any more — moved or skipped, a step added or moved ahead
    of it, the list ended. Its pending cards are to be withdrawn and leave the step (retired; it
    gets a card again when its turn comes round), so no card is ever live but hidden. A RUNNING
    card refuses the whole change: what already began is never cancelled. Mutates ``new``."""
    states = {v["sid"]: v for v in view["steps"]}
    current = None
    if not new.get("ended"):
        current = next(
            (
                st["sid"]
                for st in new["steps"]
                if not st.get("skipped") and states.get(st["sid"], {}).get("state") != DONE
            ),
            None,
        )
    displaced = {
        st["sid"]
        for st in new["steps"]
        if st["sid"] != current
        and any(x in IN_FLIGHT for x in states.get(st["sid"], {}).get("card_states", {}).values())
    }
    pend, busy = _pending_cards(view, displaced)
    if busy:
        raise StepsRefused(
            "a card of a step this would move off screen is running right now; nothing was "
            "changed. Wait until it settles — what already began is never cancelled or undone",
            "busy",
            view,
        )
    if displaced:
        rows = _rows(cid)
        for st in new["steps"]:
            if st["sid"] not in displaced:
                continue
            keep, retired = [], list(st.get("retired") or [])
            for c in st.get("cards") or []:
                live = _live([c], rows)[0]
                (retired if _state_of(live, rows) in IN_FLIGHT else keep).append(c)
            st["cards"], st["retired"] = keep, retired
    return pend


def apply_revision(cid: str, new: dict, base_revision: int, turn_id: str | None = None) -> dict:
    """Write the planned list if the stored one is still at ``base_revision`` (the CAS); the new
    view. Raises StepsRefused with the current list otherwise. Blocking."""
    with ask_store.proposals_lock(cid):
        lists = ask_store.read_steps(cid)
        rows = _rows(cid)
        if not lists or lists[-1]["id"] != new["id"] or lists[-1]["revision"] != base_revision:
            cur = derive(lists[-1], rows) if lists else None
            raise StepsRefused(
                "the step list changed meanwhile — here it is now; revise that", "stale", cur
            )
        new = {**new, "revision": base_revision + 1}
        if turn_id:
            new["drawn_at_turn"] = turn_id
        written = ask_store.write_steps(cid, [*lists[:-1], new])
    return derive(written[-1], rows)


def end_plan(cid: str, list_id: str) -> tuple[dict, list[str]]:
    """What End steps does to the newest list: ``(the ended list, the pending cards to dismiss
    first)``. Refuses when a card of the list is running. Blocking, read-only."""
    lists, views = read(cid)
    if not lists or lists[-1]["id"] != list_id:
        raise StepsRefused("that is not this conversation's newest step list", "stale")
    return plan_revision(cid, list_id, lists[-1]["revision"], [{"op": "end"}])
