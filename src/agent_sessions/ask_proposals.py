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

The launch is ``headless_dispatch.dispatch``, the one launcher every unattended start uses, for a
terminal agent. An API client (#2014: ``runtime: "api"``, pinned when the card was shown) starts
through ``mission_structured.hq_start`` instead: a structured create and the brief as its first
turn, both under an ATTENDED guard whose fenced check re-reads scope, project, folder and brief,
with ids derived from the proposal so a replayed tap never makes a second session. It never skips
permission prompts. The decision runs as a server task so a launch's evidence wait (up to minutes)
does not hold a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time

from . import ask_store, session_capacity

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
#: A tap asking an API card to skip permission prompts (#2014): never offered on one.
API_NO_BYPASS = (
    "an API session HQ starts always asks before it acts; it can't skip permission prompts"
)
#: What a card's kind is called where it is said (#2014): the read-back and HQ's card list.
KIND_WORDS = {"api": "API session", "pty": "terminal session"}
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


def is_api(engine: str) -> bool:
    """Whether ``engine`` is a native API client (asked of its manifest, never named)."""
    from . import engines, handoff

    prov = engines.get(engine)
    return prov is not None and handoff.is_api_target(prov)


def start_state(engine: str) -> tuple[bool, str]:
    """Whether a card may start ``engine`` right now (#2014). Blocking: it may probe readiness.

    An API client takes its brief as its first structured turn, so it answers the handoff-target
    question: its kind is ready for a first turn (`kinds.API_HANDOFF_READY`) and it can start here
    (`registry.can_start`, the New session form's answer). A terminal agent answers the console
    launcher's question (`automation_runner.engine_state`) as before. Missions and automations
    keep their own, narrower answers."""
    from . import automation_runner, engines, handoff

    prov = engines.get(engine)
    if prov is None or not handoff.is_api_target(prov):
        return automation_runner.engine_state(engine)
    if engines.is_retiring(prov):
        return False, engines.REMOVED_REASON
    try:
        present = handoff.target_present(prov)
        ok, why = handoff.handoff_target_state(prov, present=present)
    except Exception:  # noqa: BLE001 — a client that can't answer is not startable
        ok, why = False, ""
    return (True, "") if ok else (False, why or f"{engine} cannot be started right now")


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
    from . import mission_targets

    project, why = mission_targets.resolve_chosen_project(row["project_id"], index=index)
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


def _specialist_refusal(row: dict) -> str:
    """A card started from a specialist (#1910) starts the version it SHOWED, and only while that
    is still the specialist's current, unarchived version on the card's agent: an edit, an archive
    or a move to another agent since the card was shown makes it out of date. Blocking."""
    pinned = row.get("specialist")
    if not pinned:
        return ""
    from . import specialists_store

    try:
        spec = specialists_store.get(pinned["id"])
    except specialists_store.NotFound:
        return "that specialist no longer exists"
    except specialists_store.StoreError as e:
        return f"the specialist could not be read just now ({e})"
    if spec["archived"]:
        return "that specialist was archived since this was proposed"
    cur = spec["current"]
    if cur["version"] != pinned["version"] or cur["engine"] != row["engine"]:
        return "that specialist was edited since this was proposed — ask HQ again"
    # A skill it carries changed in the Library while the card waited: the launch's bundle build
    # would refuse it, so the card is out of date now rather than a failed start later. Read the
    # way the Specialists page reads it (`library_revisions`): a specialist with no skills has
    # nothing to prove (Hermes on #1912), and a library that can't be read just now flags nothing
    # rather than spending the card, because the bundle build stays the last word at launch.
    pins = cur.get("skills") or []
    if pins:
        from . import specialists

        try:
            library = specialists.library_revisions()
        except Exception:  # noqa: BLE001 — unreadable: flag nothing, as the page does
            library = None
        if library is not None:
            moved = [p["id"] for p in pins if library.get(p.get("id")) != p.get("revision")]
            if moved:
                return (
                    f"a skill that specialist carries ({', '.join(moved)}) changed in the "
                    "Library since this was proposed"
                )
    return ""


def specialist_launch_args(row: dict) -> dict:
    """The dispatch arguments a card started from a specialist adds: the pinned ``(id, version)``
    and that version's model (`None` for the agent's default). Empty for a plain session."""
    pinned = row.get("specialist")
    if not pinned:
        return {}
    from . import model_grades, specialists_store

    model = specialists_store.version(pinned["id"], pinned["version"])["model"]
    return {
        "specialist": (pinned["id"], pinned["version"]),
        "model": None if model == model_grades.DEFAULT_MODEL else model,
    }


def precheck(row: dict) -> str:
    """Everything a start depends on, before ``deciding`` is written. Returns the stale reason, or
    "". Blocking."""
    why = scope_refusal()
    if why:
        return why
    ok, why = start_state(row["engine"])
    if not ok:
        return why
    if (row.get("runtime") == "api") != is_api(row["engine"]):
        # The kind the card showed is the kind that starts, never a silent switch (#2014).
        return "this agent's kind of session changed since this was proposed"
    return (
        _project_refusal(row)
        or _folder_refusal(row)
        or _brief_refusal(row["brief"])
        or _specialist_refusal(row)
    )


# ---- the sidecar -------------------------------------------------------------------------------


def _find(rows: list[dict], pid: str) -> dict | None:
    return next((r for r in rows if r.get("id") == pid), None)


def add(cid: str, entry: dict) -> dict:
    """Store a NEW pending entry. Raises ask_store.Withheld / StoreRefused. Blocking."""
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        written = ask_store.write_proposals(cid, [*rows, entry], fresh=entry["id"])
    return written[-1]


def _resumes(row: dict, decision: object) -> bool:
    """A confirm of an ``unsettled`` operation its runner can resume (#1786's move)."""
    from . import ask_operations

    return (
        row.get("kind") == "operation" and decision == "confirm" and ask_operations._resumable(row)
    )


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
                    reason = INTERRUPTED_REASON
                    if r.get("kind") == "message":
                        from . import ask_messages

                        reason = ask_messages.interrupted_reason(r)
                    if r.get("kind") == "operation":
                        from . import ask_operations

                        reason = ask_operations.INTERRUPTED_REASON
                        if ask_operations.resumes_after_a_stop(r):
                            # A move that had noted what it committed (#1786) is finished from
                            # there, never left half done: confirm again resumes it.
                            r.update(
                                state=ask_operations.UNSETTLED,
                                outcome=ask_operations.RESUME_AFTER_STOP,
                                settled_at=time.time(),
                            )
                            changed = True
                            continue
                    r.update(state=INTERRUPTED, reason=reason, settled_at=time.time())
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
        # The kind of session a tap starts (#2014): pinned when the card was shown.
        "runtime": "api" if row.get("runtime") == "api" else "pty",
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
    by_id = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}
    out = {pid: _any_view(r) for pid, r in by_id.items()}
    for pid, r in by_id.items():
        old = by_id.get(r.get("replaces") or "")
        if old is not None:
            # What changed against the card it replaced (#2251 G5), read from the two stored
            # entries: the trace the old card leaves once it is no longer drawn.
            out[pid]["changes"] = changes(old, r)
    return "ok", out


#: The most changes a card lists, and the longest value it quotes.
CHANGES_MAX = 6
CHANGE_VALUE_MAX = 80


def _short(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= CHANGE_VALUE_MAX else text[: CHANGE_VALUE_MAX - 1] + "…"


def changes(old: dict, new: dict) -> list[dict]:
    """``[{field, from, to}]`` between a replaced card and its replacement: display fields only,
    as stored (each already through the store boundary), each value cut short. A long text
    (a brief, a message) is named, not quoted. Pure."""
    out: list[dict] = []

    def add(name: str, a: object, b: object, quote: bool = True) -> None:
        if a == b or len(out) >= CHANGES_MAX:
            return
        if quote:
            out.append({"field": name, "from": _short(a or ""), "to": _short(b or "")})
        else:
            out.append({"field": name, "from": "", "to": ""})

    kind = new.get("kind")
    if kind == "session":
        add("project", old.get("project"), new.get("project"))
        add("folder", old.get("cwd"), new.get("cwd"))
        add("agent", old.get("agent"), new.get("agent"))
        add("brief", old.get("brief"), new.get("brief"), quote=False)
    elif kind == "message":
        add("recipient", (old.get("recipient") or {}).get("label"),
            (new.get("recipient") or {}).get("label"))  # fmt: skip
        add("text", old.get("text"), new.get("text"), quote=False)
    else:
        add("action", old.get("op"), new.get("op"))
        add("target", old.get("title"), new.get("title"))
        a, b = old.get("fields") or {}, new.get("fields") or {}
        for k in sorted(set(a) | set(b)):
            va, vb = a.get(k), b.get(k)
            if isinstance(va, str | int | bool | None) and isinstance(vb, str | int | bool | None):
                add(k, "" if va is None else va, "" if vb is None else vb)
        if not out and old.get("binding") != new.get("binding"):
            # Only what the card binds changed (a Library draft's files, say): named, not quoted.
            add("content", "x", "y", quote=False)
    return out


def _any_view(row: dict) -> dict:
    from . import ask_messages, ask_operations

    if row.get("kind") == "operation":
        out = ask_operations.view(row)
    elif row.get("kind") == "message":
        out = ask_messages.view(row)
    else:
        out = view(row)
    for key in ("replaced_by", "replaces"):  # the card that replaced it, or it replaced (#2120)
        if row.get(key):
            out[key] = row[key]
    return out


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


async def decide(
    pid: str,
    decision: str,
    *,
    bypass: bool,
    registry,
    operator=None,
    app=None,
    inputs=None,
    bare: bool = True,
) -> tuple[int, dict]:
    """Decide one proposal. Returns ``(HTTP status, view)``; raises ProposalError.

    An HQ operation (#1559, ``kind: "operation"``) is handed to ``ask_operations`` with the decide
    lock, the tapping ``operator`` (``asgi_call.Operator``) and the ``app`` it runs through."""
    from . import ask_operations

    if decision not in ("start", "dismiss", "confirm", "send"):
        raise ProposalError(422, "decision must be start, confirm, send or dismiss")
    cid = await asyncio.to_thread(locate, pid)
    if cid is None:
        raise ProposalError(404, "no such proposal")
    fd = await _decide_lock(cid)
    if fd is None:
        row = await asyncio.to_thread(_row, cid, pid)
        if row is None:
            raise ProposalError(404, "no such proposal")
        if row.get("state") != PENDING and not _resumes(row, decision):
            # The same tap again, while it runs or after.
            return 200, _any_view(row)
        raise ProposalError(
            409, "another session from this conversation is still starting — try again shortly"
        )
    handed = False
    try:
        row = await asyncio.to_thread(_row, cid, pid)
        if row is None:
            raise ProposalError(404, "no such proposal")
        if row.get("kind") == "message":
            # A message (#1833) is decided by exactly `{"decision": "send" | "dismiss"}`: the
            # stored entry is what is sent, so nothing else a request carries is read.
            if not bare or decision not in ("send", "dismiss"):
                raise ProposalError(422, 'a message takes exactly {"decision": "send" | "dismiss"}')
            from . import ask_messages

            handed = True  # `decide_locked` owns the lock from here, whatever happens
            return await ask_messages.decide_locked(cid, row, fd, decision)
        if decision == "send":
            raise ProposalError(422, "only a message is sent")
        if row.get("kind") == "operation":
            if row.get("state") != PENDING and not _resumes(row, decision):
                return 200, ask_operations.view(row)
            if operator is None or app is None:
                raise ProposalError(503, "operations can't run in this process")
            handed = True  # `decide_locked` owns the lock from here, whatever happens
            return await ask_operations.decide_locked(
                cid, row, fd, decision, operator=operator, app=app, inputs=inputs
            )
        if row.get("state") != PENDING:
            return 200, view(row)
        if decision == "confirm":
            raise ProposalError(422, "a session proposal is started or dismissed")
        api = row.get("runtime") == "api"
        if api and bypass and decision == "start":
            raise ProposalError(422, API_NO_BYPASS)
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
        if not api and current != row["bypass_default"]:
            # Re-confirm: the card shows the new starting value and the operator taps again.
            row = await asyncio.to_thread(
                _update, cid, pid, bypass_default=current, notice="default_changed"
            )
            raise ProposalError(409, DEFAULT_CHANGED, view(row or {}))
        # WHAT THE OPERATOR APPROVED is what launches: the brief and folder as read before the
        # `deciding` write, never a copy that write re-admitted (and may have scrubbed).
        approved = {
            k: row[k] for k in ("id", "engine", "cwd", "real", "brief", "project_id", "project")
        }
        approved["bypass_default"] = row["bypass_default"]
        if api:
            approved["runtime"] = "api"
        if row.get("specialist"):
            # The specialist version the card showed (#1910): part of what was approved.
            approved["specialist"] = dict(row["specialist"])
        approved["decided_at"] = decided_at = time.time()
        # DURABLE BEFORE ANYTHING IS SPAWNED: from here on a crash leaves `deciding`, which the
        # next read settles as `interrupted`.
        row = await asyncio.to_thread(
            _update,
            cid,
            pid,
            state=DECIDING,
            bypass=bool(bypass),
            notice="",
            decided_at=decided_at,
        )
        if row is None:
            raise ProposalError(404, "no such proposal")
        launch = _launch_api if api else _launch
        task = asyncio.create_task(launch(cid, dict(approved, bypass=bool(bypass)), fd, registry))
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
            # A plain card takes no thread hop here: nothing may suspend between the tap and the
            # dispatch's `on_key` that a plain launch did not suspend at before (#1910).
            try:
                extra = (
                    await asyncio.to_thread(specialist_launch_args, row)
                    if row.get("specialist")
                    else {}
                )
            except Exception:  # noqa: BLE001 — the version was checked at precheck; gone since
                raise headless_dispatch.DispatchError(
                    "the specialist could not be read just now"
                ) from None
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
                # From a specialist (#1910): its version, its model, its skills. Re-validated by
                # the dispatch itself against the engine and model it resolves.
                **extra,
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
            notice = outcome.pop("notice", "busy")
            await asyncio.to_thread(
                _update, cid, pid, state=PENDING, bypass=None, session_key="", notice=notice
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
            if outcome.get("state") == STARTED and outcome.get("session_key"):
                # A session Ask started is FOLLOWED from its conversation (#1443 Phase 4).
                await asyncio.to_thread(_follow_started, cid, outcome["session_key"], row)
    except Exception as e:  # noqa: BLE001 — the entry stays `deciding` → `interrupted`
        log.warning("ask launch outcome not recorded (%s)", type(e).__name__)
    finally:
        os.close(fd)
        _TASKS.pop(pid, None)


async def _launch_api(cid: str, row: dict, fd: int, registry) -> None:
    """An API card's launch (#2014): `mission_structured.hq_start`, settled from the same facts as
    a terminal launch. Owns ``fd`` (the decide lock)."""
    from . import headless_dispatch, mission_structured

    pid = row["id"]
    recorded = {"key": ""}
    fenced = {"why": ""}

    def on_key(key: str) -> None:
        if _update(cid, pid, session_key=key) is None:
            raise RuntimeError("the proposal is gone")
        recorded["key"] = key

    def check() -> tuple[bool, str]:
        # INSIDE the write fence (`session_input.admit_native`), at the create AND at the first
        # turn: a withdrawal commits entirely before this read (refused) or after the worker's
        # tokens were issued (refused at its claim). The brief is read under the registration
        # fence (`_brief_refusal`), as at the tap.
        why = (
            scope_refusal()
            or _project_refusal(row)
            or _folder_refusal(row)
            or _brief_refusal(row["brief"])
        )
        fenced["why"] = why
        return (not why), why

    outcome: dict
    try:
        try:
            try:
                extra = (
                    await asyncio.to_thread(specialist_launch_args, row)
                    if row.get("specialist")
                    else {}
                )
            except Exception:  # noqa: BLE001 — the version was checked at precheck; gone since
                raise headless_dispatch.DispatchError(
                    "the specialist could not be read just now"
                ) from None
            out = await mission_structured.hq_start(
                proposal_id=pid,
                engine=row["engine"],
                cwd=row["real"],
                brief=row["brief"],
                on_key=on_key,
                check=check,
                **extra,
            )
        except headless_dispatch.DispatchError as e:
            outcome = {"state": FAILED, "reason": f"{e} — nothing was started"}
        else:
            if not out.launched and fenced["why"]:
                # Refused inside the fence before anything ran: the card is out of date.
                outcome = {"state": STALE, "reason": fenced["why"], "session_key": ""}
            else:
                outcome = _outcome(out, "")
    except asyncio.CancelledError:
        os.close(fd)
        _TASKS.pop(pid, None)
        raise
    except Exception as e:  # noqa: BLE001 — a decision must always settle
        log.warning("ask api launch failed (%s)", type(e).__name__)
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
        outcome.pop("retry", None)
        outcome.pop("notice", None)
        await asyncio.to_thread(_update, cid, pid, settled_at=time.time(), **outcome)
        if outcome.get("state") == STARTED and outcome.get("session_key"):
            await asyncio.to_thread(_follow_started, cid, outcome["session_key"], row)
    except Exception as e:  # noqa: BLE001 — the entry stays `deciding` → `interrupted`
        log.warning("ask api launch outcome not recorded (%s)", type(e).__name__)
    finally:
        os.close(fd)
        _TASKS.pop(pid, None)


def _follow_started(cid: str, key: str, row: dict) -> None:
    from . import ask_follow

    try:
        ask_follow.follow(
            cid,
            key,
            title=str(row.get("project") or ""),
            via="start",
            reason=row["id"],
            since=row.get("decided_at"),
        )
    except Exception as e:  # noqa: BLE001 — the follower's pass reconciles it later
        log.warning("ask follow not recorded yet (%s)", type(e).__name__)


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
        if out.refusal == session_capacity.SESSION_LIMIT:
            # The operator archives a session and taps again (#1819): the card isn't spent.
            return {"retry": True, "notice": "session_limit"}
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


# ---- replacing and withdrawing a card (#2120) ---------------------------------------------------

#: What a replacement or a withdrawal can report about the card it named.
REPLACED, BUSY, MISSING = "replaced", "busy", "missing"


def replace_refusal(cid: str, pid: object, kind: str) -> str:
    """Why ``pid`` can't be named as the card a NEW ``kind`` card replaces, or ``""``. Checked
    before anything is staged, so a wrong id stages nothing. Only a card of this conversation's
    sidecar, and of the same kind, can be replaced; its state is NOT checked here — a card
    decided meanwhile still lets the new card stage, and the replacement reports what won.
    Blocking."""
    if not valid_pid(pid):
        return "replaces must be the action_id of a card you proposed in this conversation"
    try:
        row = _row(cid, str(pid))
    except ask_store.SidecarUnreadable:
        return "the cards of this conversation could not be read just now; nothing was proposed"
    if row is None:
        return "replaces names no card of this conversation; nothing was proposed"
    if row.get("kind") != kind:
        return f"replaces names a {row.get('kind')} card, not a {kind} card; nothing was proposed"
    return ""


async def supersede(cid: str, old: str, new: str) -> str:
    """Settle card ``old`` as replaced by the just-staged card ``new`` (#2120): ``pending →
    dismissed`` with ``replaced_by``, a compare-and-set under the conversation's decide lock AND
    its sidecar lock, so a tap, a spoken confirm or another replacement deciding the same card
    can't interleave. Returns ``"replaced"``, ``"busy"`` (a decision of this conversation is
    running; the old card stays as it is), ``"missing"``, or the old card's own state when
    something else got there first — that state is never relabelled. Nothing is undone."""
    fd = await _decide_lock(cid)
    if fd is None:
        return BUSY
    try:
        return await asyncio.to_thread(_supersede_locked, cid, old, new)
    finally:
        os.close(fd)


def _supersede_locked(cid: str, old: str, new: str) -> str:
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        row, repl = _find(rows, old), _find(rows, new)
        if row is None or repl is None:
            return MISSING
        state = str(row.get("state"))
        if state != PENDING:
            return state
        row.update(state=DISMISSED, replaced_by=new, settled_at=time.time())
        repl["replaces"] = old  # the call reads it back as "Changed: …"
        if row.get("kind") == "session":
            row["notice"] = ""  # as a tap's dismiss leaves it
        ask_store.write_proposals(cid, rows)
    return REPLACED
