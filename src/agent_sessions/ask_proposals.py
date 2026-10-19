"""Ask's session proposals: a session the operator starts with one tap (#1443 Phase 3a).

``propose_session`` (``ask_orchestrator``) stages one: the project and agent the model picked BY
INDEX, resolved here to the project's id and folder and the engine's id, plus the brief, normalized
the way delivery normalizes it. The entry lives in ``proposals/<cid>.json`` (``ask_store``'s
sidecar, through the same store boundary) and the conversation gets a card. A card is what is
SHOWN; the entry is what a tap launches, so the client can never supply what starts.

**The tap** is ``POST /api/ask/proposals/{id}/decide`` with ``start`` or ``dismiss``:

* **Idempotent and durable.** Only a ``pending`` entry can be decided; any other state is returned
  as it is. ``deciding`` is written (fsynced) BEFORE anything is spawned and the session key is
  recorded before the spawn (``headless_dispatch``'s ``on_key``), so a crash leaves a record that
  names what may have started. The conversation's decide lock is held for the whole launch; a
  ``deciding`` entry whose lock is free is one nobody is still deciding, and it becomes
  ``interrupted`` — never replayed.
* **Attended, and the bypass is the operator's.** The card's starting value is
  ``agent_defaults.bypass`` (the new-session form's), never ``unattended_bypass_granted``. The tap
  carries the value the operator saw; an absent value is ``False``. The default is compared again
  INSIDE the launch fence, where a change to it commits either before (the card asks again) or
  after the spawn (``session_input.launch_fence``).
* **Re-checked before and inside the fence:** session assistance on, the agent still startable,
  the project still the one shown with the same folder, the folder still inside the roots and
  outside the exclusions. Any of those failing is ``stale`` — "out of date", nothing started.
* **Honest outcomes.** ``started`` means the engine's own store knows the session AND it was
  briefed (``Dispatch.ok``). A launch that may be running but was not proved is ``unconfirmed``
  — never reported as started, never retried. ``failed`` says nothing is running.

The launch is ``headless_dispatch.dispatch``, the one launcher every unattended start uses. The
decision runs as a server task so a launch's evidence wait (up to minutes) does not hold a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time

from . import ask_store

log = logging.getLogger(__name__)

PENDING, DECIDING = "pending", "deciding"
STARTED, UNCONFIRMED, FAILED = "started", "unconfirmed", "failed"
STALE, DISMISSED, INTERRUPTED = "stale", "dismissed", "interrupted"

#: How long shutdown waits for launches in flight. Past it a launch is cancelled; its entry stays
#: ``deciding`` and reads ``interrupted`` on the next look.
DECIDE_SHUTDOWN_WAIT_S = 30.0

INTERRUPTED_REASON = (
    "the server stopped while this was starting — it may be running; check your sessions. "
    "It is not retried."
)
DEFAULT_CHANGED = "your default for permission bypass changed since this was proposed"
PROJECT_BUSY = "the project was being changed just then, so nothing was started — try again"
#: How long the spawn may wait for the project store's flock — tried, never waited on, because it
#: is ordered OUTSIDE the launch fence this is held under (`projects.try_locked_index`).
PROJECT_LOCK_WAIT_S = 2.0
_PID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Launches in flight in this process: ``{proposal id: task}``.
_TASKS: dict[str, asyncio.Task] = {}


class ProposalError(Exception):
    """A refusal the route turns into an HTTP status, with the entry's view when there is one."""

    def __init__(self, status: int, detail: str, view: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.view = view


def valid_pid(value: object) -> bool:
    return isinstance(value, str) and _PID_RE.fullmatch(value) is not None


# ---- what may be started -----------------------------------------------------------------------


def scope_refusal() -> str:
    """Why session assistance does not allow an Ask launch now, or "". Read-only, and safe inside
    the launch fence (``prefs.get_automation_policy``)."""
    from . import prefs

    cfg = prefs.get_automation_policy("session")
    if not cfg.get("enabled") or cfg.get("autonomy") not in ("suggest", "yolo"):
        return "session assistance is off or observe-only"
    return ""


def bypass_default() -> bool:
    """The new-session form's starting value for permission bypass (``agent_defaults.bypass``) —
    never the unattended grant: an Ask launch is attended, decided on the card."""
    from . import prefs

    return bool(prefs.get_agent_defaults()["bypass"])


def _real(cwd: str) -> str:
    return os.path.realpath(cwd)


def _folder_refusal(row: dict) -> str:
    """The folder still exists, resolves to the SAME place it did when the card was shown
    (``real``, stored at proposal time), and is inside the boundary."""
    from . import automation_runner

    real = _real(row["cwd"])
    if real != row["real"]:
        return "the project folder moved since this was proposed"
    ok, why = automation_runner.folder_ok(real)
    return "" if ok else why


def _project_refusal(row: dict, index: dict | None = None) -> str:
    """The project still exists, is not archived, and still has the folder the card showed.
    ``index`` is a store read under its own flock (the spawn's hold), else it is read fresh."""
    from . import mission_plan

    project, why = mission_plan.resolve_chosen_project(row["project_id"], index=index)
    if project is None:
        return why
    if project["cwd"] != row["cwd"]:
        return "the project's folder changed since this was proposed"
    return ""


def _brief_refusal(brief: str) -> str:
    """The brief, against the secrets known NOW, under the registration fence: a secret registered
    after the card was shown is caught here — the brief is never launched altered, and never
    launched carrying it. Blocking."""
    from . import template_secrets

    with template_secrets.registration_fence():
        try:
            same = ask_store.guard_text(brief, ask_store.BRIEF_MAX) == brief
        except ask_store.StoreRefused:
            return "secrets could not be checked just now, so nothing was started"
    return "" if same else ask_store.BRIEF_NOW_SECRET


def precheck(row: dict) -> str:
    """Everything a start depends on, before ``deciding`` is written. Returns the stale reason, or
    "". Blocking."""
    from . import automation_runner

    why = scope_refusal()
    if why:
        return why
    ok, why = automation_runner.engine_state(row["engine"])
    if not ok:
        return why
    return _project_refusal(row) or _folder_refusal(row) or _brief_refusal(row["brief"])


# ---- the sidecar -------------------------------------------------------------------------------


def _find(rows: list[dict], pid: str) -> dict | None:
    return next((r for r in rows if r.get("id") == pid), None)


def add(cid: str, entry: dict) -> dict:
    """Store a NEW pending entry. Raises ask_store.Withheld / StoreRefused. Blocking."""
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        written = ask_store.write_proposals(cid, [*rows, entry], fresh=entry["id"])
    return written[-1]


def _update(cid: str, pid: str, **changes) -> dict | None:
    """Re-read under the sidecar lock, apply ``changes`` to one entry, write. Blocking."""
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        row = _find(rows, pid)
        if row is None:
            return None
        row.update(changes)
        written = ask_store.write_proposals(cid, rows)
    return _find(written, pid)


def _row(cid: str, pid: str) -> dict | None:
    return _find(ask_store.read_proposals(cid), pid)


def locate(pid: str) -> str | None:
    """The conversation holding proposal ``pid``. Blocking."""
    with contextlib.suppress(FileNotFoundError):
        for name in sorted(os.listdir(ask_store.root() / "proposals")):
            cid = name[: -len(".json")] if name.endswith(".json") else ""
            if not ask_store.valid_cid(cid):
                continue
            try:
                if _row(cid, pid) is not None:
                    return cid
            except ask_store.SidecarUnreadable:
                continue
    return None


def recover(cid: str) -> None:
    """A ``deciding`` entry whose decide lock is free was abandoned by a stopped server: it is
    ``interrupted`` — reported, never replayed. Blocking."""
    try:
        rows = ask_store.read_proposals(cid)
    except (ask_store.SidecarUnreadable, ask_store.ConversationNotFound):
        return
    if not any(r.get("state") == DECIDING for r in rows):
        return
    fd = ask_store.try_decide_lock(cid)
    if fd is None:
        return  # a launch is running
    try:
        with ask_store.proposals_lock(cid):
            rows = ask_store.read_proposals(cid)
            changed = False
            for r in rows:
                if r.get("state") == DECIDING:
                    r.update(state=INTERRUPTED, reason=INTERRUPTED_REASON, settled_at=time.time())
                    changed = True
            if changed:
                ask_store.write_proposals(cid, rows)
    finally:
        os.close(fd)


def view(row: dict) -> dict:
    """What the card may show and offer — nothing the decide route would refuse."""
    state = row.get("state")
    return {
        "state": state,
        "engine": row.get("engine") or "",
        "bypass_default": bool(row.get("bypass_default")),
        "bypass": row.get("bypass"),
        "session_key": row.get("session_key") if state in (STARTED, UNCONFIRMED) else "",
        "reason": row.get("reason") or "",
        "notice": row.get("notice") or "",
        "can_start": state == PENDING,
        "can_dismiss": state == PENDING,
    }


def views(cid: str) -> tuple[str, dict[str, dict]]:
    """``("ok", {id: view})``, or ``("unknown", {})`` when the sidecar cannot be read. Settles
    abandoned launches first. Blocking."""
    recover(cid)
    try:
        rows = ask_store.read_proposals(cid)
    except (ask_store.SidecarUnreadable, ask_store.ConversationNotFound):
        return "unknown", {}
    return "ok", {r["id"]: view(r) for r in rows if isinstance(r.get("id"), str)}


# ---- the decision ------------------------------------------------------------------------------


#: How long a tap waits for the conversation's decide lock before answering 409: a launch that
#: just returned its card to `pending` writes that state a moment before it lets the lock go.
DECIDE_LOCK_WAIT_S = 2.0


async def _decide_lock(cid: str) -> int | None:
    deadline = time.monotonic() + DECIDE_LOCK_WAIT_S
    while True:
        fd = await asyncio.to_thread(ask_store.try_decide_lock, cid)
        if fd is not None or time.monotonic() >= deadline:
            return fd
        row_states = await asyncio.to_thread(_states, cid)
        if DECIDING in row_states:
            return None  # a launch is genuinely running: answer at once
        await asyncio.sleep(0.05)


def _states(cid: str) -> set[str]:
    try:
        return {str(r.get("state")) for r in ask_store.read_proposals(cid)}
    except ask_store.SidecarUnreadable:
        return set()


async def decide(pid: str, decision: str, *, bypass: bool, registry) -> tuple[int, dict]:
    """Decide one proposal. Returns ``(HTTP status, view)``; raises ProposalError."""
    if decision not in ("start", "dismiss"):
        raise ProposalError(422, "decision must be start or dismiss")
    cid = await asyncio.to_thread(locate, pid)
    if cid is None:
        raise ProposalError(404, "no such proposal")
    fd = await _decide_lock(cid)
    if fd is None:
        row = await asyncio.to_thread(_row, cid, pid)
        if row is None:
            raise ProposalError(404, "no such proposal")
        if row.get("state") != PENDING:
            return 200, view(row)  # the same tap again, while it runs or after
        raise ProposalError(
            409, "another session from this conversation is still starting — try again shortly"
        )
    handed = False
    try:
        row = await asyncio.to_thread(_row, cid, pid)
        if row is None:
            raise ProposalError(404, "no such proposal")
        if row.get("state") != PENDING:
            return 200, view(row)
        if decision == "dismiss":
            row = await asyncio.to_thread(
                _update, cid, pid, state=DISMISSED, notice="", settled_at=time.time()
            )
            return 200, view(row or {})
        why = await asyncio.to_thread(precheck, row)
        if why:
            row = await asyncio.to_thread(
                _update, cid, pid, state=STALE, reason=why, notice="", settled_at=time.time()
            )
            return 200, view(row or {})
        current = await asyncio.to_thread(bypass_default)
        if current != row["bypass_default"]:
            # Re-confirm: the card shows the new starting value and the operator taps again.
            row = await asyncio.to_thread(
                _update, cid, pid, bypass_default=current, notice="default_changed"
            )
            raise ProposalError(409, DEFAULT_CHANGED, view(row or {}))
        # WHAT THE OPERATOR APPROVED is what launches: the brief and folder as read before the
        # `deciding` write, never a copy that write re-admitted (and may have scrubbed).
        approved = {k: row[k] for k in ("id", "engine", "cwd", "real", "brief", "project_id")}
        approved["bypass_default"] = row["bypass_default"]
        # DURABLE BEFORE ANYTHING IS SPAWNED: from here on a crash leaves `deciding`, which the
        # next read settles as `interrupted`.
        row = await asyncio.to_thread(
            _update,
            cid,
            pid,
            state=DECIDING,
            bypass=bool(bypass),
            notice="",
            decided_at=time.time(),
        )
        if row is None:
            raise ProposalError(404, "no such proposal")
        task = asyncio.create_task(_launch(cid, dict(approved, bypass=bool(bypass)), fd, registry))
        _TASKS[pid] = task
        handed = True
        return 202, view(row)
    finally:
        if not handed:
            os.close(fd)


async def _launch(cid: str, row: dict, fd: int, registry) -> None:
    """Run the launch and write its outcome — whatever happened. Owns ``fd`` (the decide lock)."""
    from . import headless_dispatch

    pid = row["id"]
    recorded = {"key": ""}
    fenced = {"why": ""}

    def on_key(key: str) -> None:
        # BEFORE the spawn: a record naming a session that never came to be is honest, one
        # missing a session that did is not. A refusal here (raised) stops the launch.
        if _update(cid, pid, session_key=key) is None:
            raise RuntimeError("the proposal is gone")
        recorded["key"] = key

    held: dict = {"active": False, "index": None}

    @contextlib.contextmanager
    def spawn_fence():
        # HELD ACROSS `authorize` AND THE SPAWN, inside the launch fence: what the check reads
        # stays true until the process exists. The project store's flock is OUTERMOST, so it is
        # TRIED here, never waited on (`projects.try_locked_index`); the registration fence keeps
        # a secret from becoming known between the brief's check and the spawn.
        from . import projects, template_secrets

        with (
            projects.try_locked_index(PROJECT_LOCK_WAIT_S) as index,
            template_secrets.registration_fence(),
        ):
            held.update(active=True, index=index)
            try:
                yield
            finally:
                held.update(active=False, index=None)

    def authorize(_epoch) -> str | None:
        # INSIDE the launch fence. A change to session assistance or to the bypass default
        # commits under the same fence, so it lands before this read (and refuses) or after the
        # spawn. Under `spawn_fence` (the spawn itself) the project is read from the index held
        # under its flock and the brief under the registration fence, both kept until the
        # process exists. Outside it (an engine's preflight probe) they are read fresh.
        why = scope_refusal()
        if not why and bypass_default() != row["bypass_default"]:
            why = DEFAULT_CHANGED
        if not why and held["active"] and held["index"] is None:
            why = PROJECT_BUSY
        why = (
            why
            or _project_refusal(row, held["index"] if held["active"] else None)
            or _folder_refusal(row)
            or _brief_refusal(row["brief"])
        )
        fenced["why"] = why
        return why or None

    outcome: dict
    try:
        try:
            out = await headless_dispatch.dispatch(
                engine=row["engine"],
                cwd=row["real"],
                brief=row["brief"],
                registry=registry,
                bypass=bool(row["bypass"]),
                on_key=on_key,
                authorize=authorize,
                spawn_fence=spawn_fence,
                # The delivery checks the brief again as it is typed (`handoff.claim_seed_guarded`).
                guard_secrets=True,
            )
        except headless_dispatch.DispatchError as e:
            outcome = {"state": FAILED, "reason": f"{e} — nothing was started"}
        else:
            outcome = _outcome(out, fenced["why"])
    except asyncio.CancelledError:
        # Shutdown: the entry stays `deciding` and reads `interrupted` once this lock is gone.
        os.close(fd)
        _TASKS.pop(pid, None)
        raise
    except Exception as e:  # noqa: BLE001 — a decision must always settle
        log.warning("ask launch failed (%s)", type(e).__name__)
        outcome = (
            {
                "state": UNCONFIRMED,
                "reason": "the launch stopped unexpectedly — it may be running; check your "
                "sessions. It is not retried.",
            }
            if recorded["key"]
            else {"state": FAILED, "reason": "the launch failed before anything was started"}
        )
    try:
        if outcome.pop("retry", False):
            # Nothing was spawned and nothing about the card changed: it can be tapped again.
            await asyncio.to_thread(
                _update, cid, pid, state=PENDING, bypass=None, session_key="", notice="busy"
            )
        elif outcome.pop("reconfirm", False):
            current = await asyncio.to_thread(bypass_default)
            await asyncio.to_thread(
                _update,
                cid,
                pid,
                state=PENDING,
                bypass=None,
                session_key="",
                bypass_default=current,
                notice="default_changed",
            )
        else:
            await asyncio.to_thread(_update, cid, pid, settled_at=time.time(), **outcome)
    except Exception as e:  # noqa: BLE001 — the entry stays `deciding` → `interrupted`
        log.warning("ask launch outcome not recorded (%s)", type(e).__name__)
    finally:
        os.close(fd)
        _TASKS.pop(pid, None)


def _outcome(out, fenced_why: str) -> dict:
    """The entry's settled state from the dispatch's own facts — never a summary of them."""
    if not out.ok and out.teardown in ("leaked", "spared", "error"):
        # A teardown that did not PROVE the boundary empty: something may be running, whatever
        # `launched` says (it stays false until the socket answers, after the spawn).
        return {
            "state": UNCONFIRMED,
            "reason": f"{out.reason or 'it was not confirmed'} — it may be running; check your "
            "sessions. It is not retried.",
            "session_key": out.adopt_key,
        }
    if not out.launched:
        if fenced_why and out.reason == fenced_why:
            if fenced_why == DEFAULT_CHANGED:
                return {"reconfirm": True}
            if fenced_why == PROJECT_BUSY:
                return {"retry": True}
            return {"state": STALE, "reason": fenced_why, "session_key": ""}
        return {
            "state": FAILED,
            "reason": f"{out.reason or 'the launch did not happen'} — nothing was started",
            "session_key": "",
        }
    if out.ok:
        return {"state": STARTED, "reason": "", "session_key": out.adopt_key}
    if out.teardown == "stopped":
        return {
            "state": FAILED,
            "reason": f"{out.reason or 'it did not start'} — it was stopped; nothing is running",
            "session_key": "",
        }
    return {
        "state": UNCONFIRMED,
        "reason": f"{out.reason or 'it was not confirmed'} — it may be running; check your "
        "sessions. It is not retried.",
        "session_key": out.adopt_key,
    }


def running(pid: str) -> bool:
    return pid in _TASKS


async def drain(timeout: float = DECIDE_SHUTDOWN_WAIT_S) -> None:
    """Let launches in flight settle before the app goes away; cancel what is still running."""
    pending = set(_TASKS.values())
    if not pending:
        return
    log.info("waiting for %d Ask launch(es) to settle", len(pending))
    _done, late = await asyncio.wait(pending, timeout=timeout)
    for t in late:
        t.cancel()
    if late:
        await asyncio.wait(late, timeout=5)
