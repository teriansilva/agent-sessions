"""The turn loop for `chat`-runtime agents (#853 P9a, #1209). BattleLab IS the agent here.

**One transition path.** Send and Retry both go through :func:`_begin`, under one per-session
asyncio lock: it decides whether a turn may start, persists the transition to ``pending`` BEFORE
anything is sent, and spawns the request. So a duplicate ``turn_id`` never appends a second copy,
a reused ``turn_id`` with different text is refused rather than replacing the original, and two
simultaneous retries start one operation.

**The request is a server-owned task, not the HTTP request.** It is an ``asyncio`` task held in
:data:`_TASKS` for as long as it runs, so a client that disconnects or reloads does not cancel it;
the client recovers the result by reading the session. A process-shared fence spans each turn
and decision, including continuation. Only a reader that acquires that fence can declare an
orphaned ``pending`` turn ``failed: interrupted``; another worker's task stays authoritative.

**One transport.** The model call is ``await review.post_chat_response(cfg, body)`` — i.e.
``review._post_chat``, with this agent's own
configuration snapshot — the same function every other model call uses, so the registered-prompt
check and template-secret redaction apply, and no new outbound-HTTP site exists. Store I/O is
blocking and runs in ``asyncio.to_thread``; the awaited request never blocks the event loop.

**Tools are off unless the operator turned them on (#1222, #1230).** With ``tools =
"read"`` a turn becomes a bounded loop of rounds: the model may call ``list_files`` /
``read_file`` (:mod:`agent_sessions.chat_tools`), each call's result is sent back through the same
transport, and the final round is forced to answer (``tool_choice: "none"``). The permission is
re-read from the LIVE prefs before every round and every call, so turning it off mid-turn stops
the next call. A reply is still text, stored and rendered as text; the only thing a tool call
executes directly is a bounded, confined read. With `write`, a sole `propose_edit` call pauses
the turn in `awaiting_approval`. Its private checkpoint survives restart and holds single-flight
until a separate operator decision saves through fileedit or rejects; live policy is rechecked.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import time
from pathlib import Path

from . import chat_config, chat_edits, chat_store, chat_tools, prompts, review
from .plugins import admission

#: Budget factor after the endpoint rejects a conversation as too long (see `_context_rejected`).
BUDGET_CUT = 0.75
_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
)

#: Tool loop bounds (#1222): rounds of tool calls per turn, and calls per round. The round after
#: the last is always sent with `tool_choice: "none"`, so the model must answer.
MAX_TOOL_ROUNDS = 8
MAX_CALLS_PER_ROUND = 16
_ARGS_MAX = 4096
_TOOLS_REFUSED_MARKERS = ("tool", "function")

#: session key → the running request. Holding the task here is what keeps it alive past the HTTP
#: request that started it (and what `_reconcile` asks to tell "pending" from "interrupted").
_TASKS: dict[str, asyncio.Task] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


class ChatError(Exception):
    """A refusal the route turns into an HTTP status. ``detail`` is operator-safe (never a key)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _key(engine_id: str, session_id: str) -> str:
    return f"{engine_id}:{session_id}"


def _lock(key: str) -> asyncio.Lock:
    lk = _LOCKS.get(key)
    if lk is None:
        lk = _LOCKS[key] = asyncio.Lock()
    return lk


async def _fence(root: Path, sid: str) -> chat_edits.TurnFence | None:
    task = asyncio.create_task(asyncio.to_thread(chat_edits.turn_fence, root, sid))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancellation must not orphan a descriptor the worker has acquired but not returned.
        def release(done):
            with contextlib.suppress(Exception):
                lease = done.result()
                if lease is not None:
                    lease.release()

        task.add_done_callback(release)
        raise


async def _write_io(fn, *args, **kwargs):
    """A cancelled request retains its fence until the mutating worker actually finishes."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        with contextlib.suppress(Exception):
            task.result()
        raise


def _start(key: str, coroutine, fence: chat_edits.TurnFence) -> None:
    task = asyncio.create_task(coroutine)
    _TASKS[key] = task
    fence.handed_off = True
    # A callback runs even if the task is cancelled BEFORE its first coroutine instruction.
    task.add_done_callback(lambda _done: fence.release())


def _root(prov) -> Path:
    root = prov.store_root()
    if root is None:
        raise ChatError(500, "this agent has no conversation store")
    return root


def _chat_provider(engine_id: str):
    from . import engines

    prov = engines.get(engine_id)
    if prov is None or engines.is_retiring(prov):
        raise ChatError(404, "unknown or removed agent")
    if prov.manifest.runtime != "chat":
        raise ChatError(409, f"{engine_id} is not a chat agent")
    return prov


def _admit_provider(prov) -> None:
    from .engines import registry

    if not registry.admits(prov):
        raise ChatError(409, "this agent changed or was removed; reload before starting work")


async def _admitted_write(prov, fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(admission.commit, prov, fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        with contextlib.suppress(Exception):
            task.result()
        raise
    except admission.Refused as exc:
        raise ChatError(409, str(exc)) from None


def _status(turn_id: str, status: str, reason: str | None = None) -> dict:
    rec = {"type": "status", "turn_id": turn_id, "status": status, "ts": time.time()}
    if reason:
        rec["reason"] = reason
    return rec


def _view(log: chat_store.ChatLog) -> dict:
    return {
        "session_id": log.session_id,
        "cwd": log.cwd,
        "created_at": log.created_at,
        "turns": [t.as_dict() for t in log.turns],
        "in_flight": next(
            (t.turn_id for t in log.turns if t.status in ("pending", "awaiting_approval")), None
        ),
    }


async def _read(root: Path, session_id: str) -> chat_store.ChatLog:
    log = await asyncio.to_thread(chat_store.read, root, session_id)
    if log is None:
        raise ChatError(404, "no such conversation")
    return log


async def _reconcile(
    key: str, root: Path, log: chat_store.ChatLog, *, fenced: bool = False
) -> chat_store.ChatLog:
    """Only the cross-process fence can prove a decision or model turn has no live owner."""
    task = _TASKS.get(key)
    if task is not None and not task.done():
        return log
    fence = None if fenced else await _fence(root, log.session_id)
    if not fenced and fence is None:
        return log  # another worker owns the save, pending transition or continuation
    try:
        return await _write_io(_reconcile_owned, root, log.session_id)
    finally:
        if fence is not None:
            fence.release()


def _reconcile_owned(root: Path, session_id: str) -> chat_store.ChatLog:
    # The earlier HTTP read is only a hint. Re-read BOTH turn and proposal states after winning
    # the shared fence; a worker may have finished a decision while this reader was acquiring it.
    log = chat_store.read(root, session_id)
    if log is None:
        raise ChatError(404, "no such conversation")
    stale = [t for t in log.turns if t.status == "pending"]
    for turn in log.turns:
        if turn.status == "awaiting_approval":
            proposals = chat_edits.views(root, log.session_id, turn.turn_id)
            if not any(p["status"] == "awaiting_approval" for p in proposals):
                stale.append(turn)
    if not stale:
        return log
    recs = []
    for turn in stale:
        proposals = chat_edits.views(root, log.session_id, turn.turn_id)
        for proposal in proposals:
            audit = chat_edits.interrupt(root, log.session_id, proposal["id"])
            recs.append({"type": "proposal", "turn_id": turn.turn_id, **audit})
        reason = (
            "interrupted — inspect the file before retrying this turn"
            if proposals
            else "interrupted — BattleLab restarted while waiting for the reply"
        )
        recs.append(_status(turn.turn_id, "failed", reason))
    chat_store.append(root, log.session_id, *recs)
    return chat_store.read(root, log.session_id)


# --- public operations ---------------------------------------------------------------------------


async def new_session(engine_id: str, cwd: str) -> str:
    prov = _chat_provider(engine_id)
    if not chat_config.is_configured(engine_id):
        raise ChatError(409, "this agent has no endpoint yet — configure it in Settings → Agents")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\x00" in cwd:
        raise ChatError(422, "cwd must be an absolute path")
    if not await asyncio.to_thread(os.path.isdir, cwd):
        raise ChatError(422, "cwd is not a directory")
    import uuid

    sid = str(uuid.uuid4())
    _admit_provider(prov)
    await _admitted_write(prov, chat_store.create, _root(prov), sid, cwd=cwd)
    return sid


async def get_session(engine_id: str, session_id: str) -> dict:
    prov = _chat_provider(engine_id)
    key, root = _key(engine_id, session_id), _root(prov)
    async with _lock(key):
        log = await _reconcile(key, root, await _read(root, session_id))
    view = _view(log)
    for turn in view["turns"]:
        turn["proposals"] = await asyncio.to_thread(
            chat_edits.views, root, session_id, turn["turn_id"]
        )
    return view


async def send(engine_id: str, session_id: str, turn_id: object, text: object) -> dict:
    if not chat_store.valid_turn_id(turn_id):
        raise ChatError(422, "turn_id must be a UUID")
    if not isinstance(text, str) or not text.strip():
        raise ChatError(422, "text must be a non-empty string")
    if len(text) > chat_store.TEXT_MAX:
        raise ChatError(413, "message is too long")
    return await _begin(engine_id, session_id, turn_id, text)


async def retry(engine_id: str, session_id: str, turn_id: object) -> dict:
    if not chat_store.valid_turn_id(turn_id):
        raise ChatError(422, "turn_id must be a UUID")
    return await _begin(engine_id, session_id, turn_id, None)


async def decide(
    engine_id: str,
    session_id: str,
    turn_id: str,
    proposal_id: str,
    decision: object,
    user: str,
) -> dict:
    """The operator's decision, serialized with reads/sends/retries for this conversation."""
    if decision not in ("approve", "reject"):
        raise ChatError(422, "decision must be approve or reject")
    prov = _chat_provider(engine_id)
    key, root = _key(engine_id, session_id), _root(prov)
    async with _lock(key):
        fence = await _fence(root, session_id)
        if fence is None:
            proposals = await asyncio.to_thread(chat_edits.views, root, session_id, turn_id)
            proposal = next((p for p in proposals if p["id"] == proposal_id), None)
            if proposal and proposal["status"] not in ("awaiting_approval", "deciding"):
                return {"proposal": proposal}
            raise ChatError(409, "the agent or another decision is still working")
        try:
            return await _decide_owned(
                engine_id, session_id, turn_id, proposal_id, decision, user, key, root, fence, prov
            )
        finally:
            if not fence.handed_off:
                fence.release()


async def _decide_owned(
    engine_id, session_id, turn_id, proposal_id, decision, user, key, root, fence, prov
):
    from .fsbrowse import FsError

    log = await _read(root, session_id)
    turn = log.turn(turn_id)
    if turn is None:
        raise ChatError(404, "no such turn")
    # Terminal repeats are admitted by the proposal's own durable decision state. A pending
    # proposal belonging to an orphaned/failed turn is never actionable.
    views = await asyncio.to_thread(chat_edits.views, root, session_id, turn_id)
    proposal = next((p for p in views if p["id"] == proposal_id), None)
    if proposal is None:
        raise ChatError(404, "no such proposal")
    if proposal["status"] not in ("awaiting_approval", "deciding"):
        return {"proposal": proposal}
    running = _TASKS.get(key)
    if running is not None and not running.done():
        raise ChatError(409, "the agent is still working")
    if proposal["status"] == "awaiting_approval" and turn.status != "awaiting_approval":
        raise ChatError(409, "this turn is no longer awaiting approval")
    if decision == "approve" and proposal["status"] == "awaiting_approval":
        _admit_provider(prov)
    try:
        rec, checkpoint = await _write_io(
            chat_edits.decide,
            root,
            session_id,
            turn_id,
            proposal_id,
            engine_id,
            decision,
            user,
            provider=prov,
        )
    except FsError as e:
        raise ChatError(e.status, str(e)) from None
    audit = {"type": "proposal", "turn_id": turn_id, **chat_edits.summary(rec)}
    if checkpoint is not None:
        status = {**_status(turn_id, "pending"), "resume": True}
        await _write_io(chat_store.append, root, session_id, audit, status)
        _start(key, _run(engine_id, root, session_id, turn_id, checkpoint), fence)
    elif turn.status == "awaiting_approval" and rec["status"] != "awaiting_approval":
        await _write_io(
            chat_store.append,
            root,
            session_id,
            audit,
            _status(
                turn_id,
                "failed",
                "the decision was recorded but the reply was "
                "interrupted; inspect the file before retrying",
            ),
        )
    return {"proposal": chat_edits.summary(rec)}


async def _begin(engine_id: str, session_id: str, turn_id: str, text: str | None) -> dict:
    """THE transition into ``pending`` — send (``text``) and retry (``text is None``) alike."""
    prov = _chat_provider(engine_id)
    key, root = _key(engine_id, session_id), _root(prov)
    async with _lock(key):
        fence = await _fence(root, session_id)
        if fence is None:
            log = await _read(root, session_id)
            existing = log.turn(turn_id)
            if (
                text is not None
                and existing is not None
                and existing.text == text
                and existing.status != "pending"
            ):
                return {"turn": existing.as_dict()}
            raise ChatError(409, "in_flight — another worker owns this conversation")
        try:
            return await _begin_owned(engine_id, session_id, turn_id, text, key, root, fence, prov)
        finally:
            if not fence.handed_off:
                fence.release()


async def _begin_owned(engine_id, session_id, turn_id, text, key, root, fence, prov):
    log = await _reconcile(key, root, await _read(root, session_id), fenced=True)
    existing = log.turn(turn_id)
    if text is not None and existing is not None:
        if existing.text != text:
            raise ChatError(409, "turn id already used for a different message")
        if existing.status == "pending":
            raise ChatError(409, "in_flight")
        return {"turn": existing.as_dict()}  # a repeat of a settled send: its result, once
    if text is None:
        if existing is None:
            raise ChatError(404, "no such turn")
        if existing.status != "failed":
            raise ChatError(409, "in_flight" if existing.status == "pending" else "already done")
    if any(t.status == "awaiting_approval" for t in log.turns):
        raise ChatError(409, "a file change is awaiting your decision")
    running = _TASKS.get(key)
    if running is not None and not running.done():
        raise ChatError(409, "another message is still waiting for its reply")
    cfg = chat_config.snapshot(engine_id)
    if cfg is None:
        raise ChatError(409, "this agent has no endpoint — configure it in Settings → Agents")
    body_text = text if text is not None else existing.text  # type: ignore[union-attr]
    budget = _budget(cfg, log)
    if _tokens(body_text) > budget:
        raise ChatError(413, "too long for this endpoint's context window")
    recs = [_status(turn_id, "pending")]
    if text is not None:
        recs.insert(0, {"type": "user", "turn_id": turn_id, "text": text, "ts": time.time()})
    _admit_provider(prov)
    await _admitted_write(prov, chat_store.append, root, session_id, *recs)
    _start(key, _run(engine_id, root, session_id, turn_id), fence)
    return {"turn": {"turn_id": turn_id, "status": "pending"}}


# --- the request ---------------------------------------------------------------------------------


def _tokens(text: str) -> int:
    return -(-len(text) // chat_config.CHARS_PER_TOKEN)


def _budget(cfg: dict, log: chat_store.ChatLog) -> int:
    # Re-read at every send: the prompt may have changed. With tools on, the declarations are
    # part of every request, so they come out of the budget too.
    system = prompts.effective(chat_config.prompt_id(cfg))
    return int(chat_config.budget_tokens(cfg, system_prompt=system) * log.budget_factor)


def history(log: chat_store.ChatLog, turn_id: str, budget: int) -> tuple[list[dict], int]:
    """The messages to send for ``turn_id``: every settled exchange before it, then it — trimmed
    oldest-first, whole exchanges only, to the ESTIMATED ``budget``. Returns (messages, dropped).
    Only ``user``/``assistant`` roles are ever produced (no replayed history can carry a system
    role — the `pulse_chat.bound_history` rule)."""
    current = log.turn(turn_id)
    assert current is not None
    pairs: list[tuple[str, str]] = []
    for t in log.turns:
        if t.turn_id == turn_id:
            break
        if t.status == "done" and t.reply is not None:
            pairs.append((t.text, t.reply))
    used = _tokens(current.text)
    kept: list[tuple[str, str]] = []
    for u, a in reversed(pairs):
        cost = _tokens(u) + _tokens(a)
        if used + cost > budget:
            break
        kept.append((u, a))
        used += cost
    kept.reverse()
    msgs: list[dict] = []
    for u, a in kept:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": current.text})
    return msgs, len(pairs) - len(kept)


def _context_rejected(r) -> bool:
    if r.status_code not in (400, 413, 422):
        return False
    text = r.text.lower()[:4000]
    return any(m in text for m in _CONTEXT_MARKERS)


async def _run(
    engine_id: str, root: Path, session_id: str, turn_id: str, resume: dict | None = None
) -> None:
    """Make the request, then SETTLE it — write its outcome and give up the task slot — inside the
    session lock. `_start` holds the process-shared fence until this task completes, including
    cancellation and pending disk writes. Within this process reconciliation holds the same
    asyncio lock, so it either sees
    this task still registered (and leaves the pending turn alone) or sees the settled records.
    Settling outside the lock let a read that had just seen `pending` find no task and mark a
    turn whose reply had already been stored as interrupted (Hermes on #1216)."""
    key = _key(engine_id, session_id)
    try:
        records = await _run_once(engine_id, root, session_id, turn_id, resume)
    except Exception as e:  # noqa: BLE001 — a turn must always settle, whatever went wrong
        records = [_status(turn_id, "failed", f"internal error ({type(e).__name__})")]
    async with _lock(key):
        try:
            if records:
                await _write_io(chat_store.append, root, session_id, *records)
        finally:
            if _TASKS.get(key) is asyncio.current_task():
                _TASKS.pop(key, None)


async def _run_once(
    engine_id: str, root: Path, session_id: str, turn_id: str, resume: dict | None = None
) -> list[dict]:
    """The request itself. Returns the records that settle the turn; writes nothing."""

    def fail(reason: str, *extra: dict) -> list[dict]:
        return [*extra, _status(turn_id, "failed", reason)]

    try:
        prov = _chat_provider(engine_id)
    except ChatError as exc:
        return fail(str(exc))
    log = await asyncio.to_thread(chat_store.read, root, session_id)
    cfg = chat_config.snapshot(engine_id)  # ONE snapshot: this URL and this key, together
    if log is None or log.turn(turn_id) is None:
        return []
    if cfg is None:
        return fail("this agent has no endpoint — configure it in Settings → Agents")
    budget = _budget(cfg, log)
    if _tokens(log.turn(turn_id).text) > budget:  # type: ignore[union-attr]
        return fail("too long for this endpoint's context window")
    msgs, dropped = history(log, turn_id, budget)
    # Literal system messages, so the registry ratchet reads each call site's prompt id.
    if cfg["tools"] == "write":
        system = {"role": "system", "content": prompts.effective("chat_agent_edits")}
    elif cfg["tools"] == "read":
        system = {"role": "system", "content": prompts.effective("chat_agent_tools")}
    else:
        system = {"role": "system", "content": prompts.effective("chat_agent")}
    # `msgs` are built by `history`, whose roles are the literals "user"/"assistant" only; the
    # tool loop below adds only "assistant" (rebuilt field by field) and "tool" messages.
    convo: list[dict] = [system, *msgs]
    remaining = budget - sum(_tokens(m["content"]) for m in msgs)
    usage: dict[str, int] = {}
    ran_tools = False
    #: convo index of each tool message → the verified path its content came from.
    targets: dict[int, str | None] = {}
    endpoint_binding = cfg["binding"]
    reads: dict[str, str] = {}
    first_round = 0
    if resume is not None:
        if resume["binding"] != endpoint_binding:
            return fail("the endpoint configuration changed while awaiting approval")
        convo = [system, *resume["messages"]]
        remaining, usage, dropped = resume["remaining"], resume["usage"], resume["dropped"]
        first_round = resume["round"]
    for round_no in range(first_round, MAX_TOOL_ROUNDS + 1):
        try:
            _admit_provider(prov)
        except ChatError as exc:
            return fail(str(exc))
        # The permission is asked of the LIVE prefs every round, never the turn's snapshot.
        offer = cfg["tools"] in ("read", "write") and chat_config.tools_enabled(engine_id)
        if ran_tools:
            # Results already read are re-admitted before they go out: tools revoked, the folder
            # no longer admitted, or the target newly excluded — nothing read under the old policy
            # leaves under the new one. The call/result pairing stays valid.
            await _withdraw_results(convo, targets, log.cwd, revoked=not offer)
        body: dict = {
            "model": cfg["model"],
            "messages": convo,
            "max_tokens": cfg["max_output_tokens"],
            "stream": False,
        }
        last = round_no == MAX_TOOL_ROUNDS or remaining <= 0
        if offer:
            mode = cfg["tools"]
            if mode == "write" and not chat_config.edits_enabled(engine_id):
                mode = "read"
            body["tools"] = chat_config.tool_specs({"tools": mode})
            if last:
                body["tool_choice"] = "none"
        try:
            r = await review.post_chat_response(
                cfg, body, admission=lambda prov=prov: admission.request(prov)
            )
        except admission.Refused as exc:
            return fail(str(exc))
        except review.TransportTimeout as e:
            return fail(
                f"{e} — it may already have processed (and billed) this request; Retry sends it "
                "again"
            )
        except review.ReviewError as e:
            return fail(str(e))
        if r.status_code != 200:
            if _context_rejected(r):
                return fail(
                    "the endpoint rejected the conversation as too long — send a shorter "
                    "message; the next send includes less history",
                    {"type": "budget", "factor": BUDGET_CUT * log.budget_factor, "ts": time.time()},
                )
            if offer and _tools_rejected(r):
                return fail(
                    "this endpoint does not accept tools — turn Tools off for this agent in "
                    "Settings → Agents, or use a model that supports tool calls"
                )
            what = {401: "the key was rejected", 403: "access was refused", 429: "rate limited"}
            detail = what.get(r.status_code, "")
            suffix = f" — {detail}" if detail else ""
            return fail(f"the endpoint refused the request (HTTP {r.status_code}{suffix})")
        try:
            payload = r.json()
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return fail("the endpoint returned an unexpected response shape")
        _add_usage(usage, payload.get("usage") if isinstance(payload, dict) else None)
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if offer and not last and isinstance(calls, list) and calls:
            clean = _sanitize_calls(calls)
            if (
                clean is not None
                and len(clean) == 1
                and clean[0]["function"]["name"] == "propose_edit"
                and cfg["tools"] == "write"
            ):
                call = clean[0]
                checkpoint_msgs = copy.deepcopy(convo[1:])
                for msg in checkpoint_msgs:
                    if msg["role"] == "tool":
                        msg["content"] = json.dumps(
                            {
                                "notice": "earlier file contents were not "
                                "retained across approval; read again if needed"
                            }
                        )
                checkpoint_msgs.append({"role": "assistant", "content": None, "tool_calls": clean})
                cost = _tokens(json.dumps(clean))
                if cost > remaining:
                    return fail("the proposed edit does not fit this endpoint's context window")
                try:
                    from .fsbrowse import FsError

                    args = json.loads(call["function"]["arguments"])
                    rec = await _write_io(
                        chat_edits.stage,
                        root,
                        session_id,
                        turn_id,
                        engine_id,
                        log.cwd,
                        args,
                        call_id=call["id"],
                        reads=reads,
                        endpoint_binding=endpoint_binding,
                        checkpoint={
                            "messages": checkpoint_msgs,
                            "remaining": remaining - cost,
                            "usage": usage,
                            "dropped": dropped,
                            "round": round_no + 1,
                        },
                    )
                except (FsError, ValueError) as e:
                    reason = str(e) if isinstance(e, FsError) else "arguments are not valid JSON"
                    convo.extend(
                        [
                            {"role": "assistant", "content": None, "tool_calls": clean},
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps({"error": reason}),
                            },
                        ]
                    )
                    remaining -= cost + _tokens(reason)
                    await _tool_record(
                        root,
                        session_id,
                        turn_id,
                        call["id"],
                        {
                            "name": "propose_edit",
                            "path": _shown_path(call["function"]["arguments"]),
                            "outcome": "refused",
                            "reason": reason,
                        },
                    )
                    continue
                return [
                    {"type": "proposal", "turn_id": turn_id, **chat_edits.summary(rec)},
                    _status(turn_id, "awaiting_approval"),
                ]
            spent = await _tool_round(
                engine_id,
                root,
                session_id,
                turn_id,
                log.cwd,
                convo,
                content,
                calls,
                remaining,
                targets,
                reads,
            )
            if spent is None:
                return fail("the endpoint returned an unexpected response shape")
            remaining -= spent
            ran_tools = True
            continue
        if not isinstance(content, str) or (calls and not content.strip()):
            if calls:
                # Told to answer (`tool_choice: "none"`, or tools withdrawn) and it called anyway:
                # fail closed — no further round, no execution.
                return fail("the endpoint kept calling tools after it was told to answer")
            return fail("the endpoint returned an unexpected response shape")
        # The store's reply cap is a POLICY applied here, at ingestion, and recorded — never a
        # silent cut on read (Hermes on #1216). It sits far above any configurable output size.
        stored_cut = len(content) > chat_store.REPLY_MAX
        reply = {
            "type": "assistant",
            "turn_id": turn_id,
            "text": content[: chat_store.REPLY_MAX],
            "ts": time.time(),
            # An OUTPUT limit, not a context rejection: the reply is kept, and marked as cut off.
            "truncated": choice.get("finish_reason") == "length" or stored_cut,
            "dropped": dropped,
        }
        if usage:
            reply["usage"] = usage
        return [reply, _status(turn_id, "done")]
    return fail("the endpoint kept calling tools after it was told to answer")  # pragma: no cover


_WITHDRAWN = json.dumps({"error": "withdrawn: this result may no longer be shared"})


async def _withdraw_results(
    convo: list[dict], targets: dict[int, str | None], cwd: str, *, revoked: bool
) -> None:
    """Replace, in place, every tool result that may no longer be sent with a withdrawal notice:
    all of them when tools were revoked, otherwise each one the live boundary no longer admits."""
    for i, target in targets.items():
        if convo[i].get("content") == _WITHDRAWN:
            continue
        if revoked or not await asyncio.to_thread(chat_tools.still_admitted, cwd, target):
            convo[i] = {**convo[i], "content": _WITHDRAWN}


def _tools_rejected(r) -> bool:
    """A 400/422 whose body names tools: the endpoint or model does not support tool calls."""
    if r.status_code not in (400, 422):
        return False
    text = r.text.lower()[:4000]
    return any(m in text for m in _TOOLS_REFUSED_MARKERS)


def _add_usage(total: dict[str, int], usage: object) -> None:
    """Sum one round's usage into the turn's (recorded ONCE, with the reply)."""
    if not isinstance(usage, dict):
        return
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        v = usage.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            total[k] = total.get(k, 0) + v


def _sanitize_calls(calls: list) -> list[dict] | None:
    """The model's tool calls rebuilt field by field — nothing it sent is echoed back unread.
    None when the shape is not a list of function calls."""
    out: list[dict] = []
    seen: set[str] = set()
    for i, c in enumerate(calls):
        if not isinstance(c, dict):
            return None
        fn = c.get("function")
        if not isinstance(fn, dict):
            return None
        name = fn.get("name")
        args = fn.get("arguments", "{}")
        if not isinstance(name, str):
            return None
        if not isinstance(args, str):
            args = json.dumps(args) if isinstance(args, dict) else "{}"
        cid = c.get("id")
        cid = chat_tools.printable(cid[:128]) if isinstance(cid, str) and cid else f"call_{i}"
        name = chat_tools.printable(name)
        while cid in seen:
            cid = f"{cid}_{i}"
        seen.add(cid)
        out.append(
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": name[:64],
                    "arguments": chat_tools.printable(
                        args[: (8 * chat_edits.MAX_BYTES if name == "propose_edit" else _ARGS_MAX)]
                    ),
                },
            }
        )
    return out


async def _tool_round(
    engine_id: str,
    root: Path,
    session_id: str,
    turn_id: str,
    cwd: str,
    convo: list[dict],
    content: object,
    raw_calls: list,
    remaining: int,
    targets: dict[int, str | None],
    reads: dict[str, str] | None = None,
) -> int | None:
    """Run one round of tool calls and extend ``convo`` with the call and its results. Returns
    the tokens the round added (an ESTIMATE, like every budget here), or None on a bad shape."""
    calls = _sanitize_calls(raw_calls)
    if calls is None:
        return None
    convo.append(
        {
            "role": "assistant",
            "content": chat_tools.printable(content) if isinstance(content, str) else None,
            "tool_calls": calls,
        }
    )
    spent = _tokens(json.dumps(calls)) + (_tokens(content) if isinstance(content, str) else 0)
    for n, call in enumerate(calls):
        name, args, cid = call["function"]["name"], call["function"]["arguments"], call["id"]
        if n >= MAX_CALLS_PER_ROUND:
            result = chat_tools.ToolResult(
                json.dumps({"error": f"refused: at most {MAX_CALLS_PER_ROUND} calls per round"}),
                {
                    "name": name,
                    "path": "",
                    "outcome": "refused",
                    "reason": f"more than {MAX_CALLS_PER_ROUND} calls in one round",
                },
            )
        elif not chat_config.tools_enabled(engine_id):
            result = chat_tools.ToolResult(
                json.dumps({"error": "refused: the operator turned tools off"}),
                {"name": name, "path": "", "outcome": "refused", "reason": "tools were turned off"},
            )
        else:
            await _tool_record(
                root,
                session_id,
                turn_id,
                cid,
                {"name": name, "path": _shown_path(args), "outcome": "running"},
            )
            result = await _run_tool(cwd, name, args)
        cost = _tokens(result.content)
        if spent + cost > remaining:
            result = chat_tools.ToolResult(
                json.dumps({"error": "refused: the context budget is used up — answer now"}),
                {
                    **result.summary,
                    "outcome": "refused",
                    "reason": "the result did not fit the context window",
                },
            )
            cost = _tokens(result.content)
        spent += cost
        await _tool_record(root, session_id, turn_id, cid, result.summary)
        if reads is not None and name == "read_file" and result.target:
            payload = json.loads(result.content)
            digest = payload.get("base_sha256")
            if isinstance(digest, str):
                reads[result.target] = digest
        targets[len(convo)] = result.target
        convo.append({"role": "tool", "tool_call_id": cid, "content": result.content})
    return spent


def _shown_path(args: str) -> str:
    try:
        p = json.loads(args).get("path", ".")
    except (ValueError, AttributeError):
        return ""
    return chat_tools.printable(p[:200]) if isinstance(p, str) else ""


async def _tool_record(root: Path, session_id: str, turn_id: str, cid: str, summary: dict) -> None:
    rec = {"type": "tool", "turn_id": turn_id, "call_id": cid, **summary, "ts": time.time()}
    await _write_io(chat_store.append, root, session_id, rec)


async def _run_tool(cwd: str, name: str, args: str) -> chat_tools.ToolResult:
    """One call on the FILE PANEL's bounded pool — one admission budget for every file read."""
    from . import files
    from .routes.files import run_bounded

    try:
        return await run_bounded(cwd, chat_tools.run, cwd, name, args)
    except files.FilesBusy:
        return chat_tools.ToolResult(
            json.dumps({"error": "refused: the file reader is busy — try again"}),
            {
                "name": name,
                "path": _shown_path(args),
                "outcome": "refused",
                "reason": "the file reader was busy",
            },
        )


def running_task(engine_id: str, session_id: str) -> asyncio.Task | None:
    """The request in flight for a session (tests await it; routes never need to)."""
    return _TASKS.get(_key(engine_id, session_id))
