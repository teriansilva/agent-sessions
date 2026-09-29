"""The turn loop for `chat`-runtime agents (#853 P9a, #1209). BattleLab IS the agent here.

**One transition path.** Send and Retry both go through :func:`_begin`, under one per-session
asyncio lock: it decides whether a turn may start, persists the transition to ``pending`` BEFORE
anything is sent, and spawns the request. So a duplicate ``turn_id`` never appends a second copy,
a reused ``turn_id`` with different text is refused rather than replacing the original, and two
simultaneous retries start one operation.

**The request is a server-owned task, not the HTTP request.** It is an ``asyncio`` task held in
:data:`_TASKS` for as long as it runs, so a client that disconnects or reloads does not cancel it;
the client recovers the result by reading the session. A turn found ``pending`` with no task in
this process (an app restart mid-request) is settled as ``failed: interrupted`` on the next read.

**One transport.** The model call is ``await review.post_chat_response(cfg, body)`` — i.e.
``review._post_chat``, with this agent's own
configuration snapshot — the same function every other model call uses, so the registered-prompt
check and template-secret redaction apply, and no new outbound-HTTP site exists. Store I/O is
blocking and runs in ``asyncio.to_thread``; the awaited request never blocks the event loop.

**Tools are read-only, and off unless the operator turned them on (#1222).** With ``tools =
"read"`` a turn becomes a bounded loop of rounds: the model may call ``list_files`` /
``read_file`` (:mod:`agent_sessions.chat_tools`), each call's result is sent back through the same
transport, and the final round is forced to answer (``tool_choice: "none"``). The permission is
re-read from the LIVE prefs before every round and every call, so turning it off mid-turn stops
the next call. A reply is still text, stored and rendered as text; the only thing a tool call
executes is a bounded, confined read.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from . import chat_config, chat_store, chat_tools, prompts, review

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
        "in_flight": next((t.turn_id for t in log.turns if t.status == "pending"), None),
    }


async def _read(root: Path, session_id: str) -> chat_store.ChatLog:
    log = await asyncio.to_thread(chat_store.read, root, session_id)
    if log is None:
        raise ChatError(404, "no such conversation")
    return log


async def _reconcile(key: str, root: Path, log: chat_store.ChatLog) -> chat_store.ChatLog:
    """A turn left ``pending`` with no task running for it was cut off (an app restart): settle it
    as failed so it can be retried, instead of reading as in flight forever."""
    task = _TASKS.get(key)
    if task is not None and not task.done():
        return log
    stale = [t for t in log.turns if t.status == "pending"]
    if not stale:
        return log
    recs = [
        _status(
            t.turn_id, "failed", "interrupted — BattleLab restarted while waiting for the reply"
        )
        for t in stale
    ]
    await asyncio.to_thread(chat_store.append, root, log.session_id, *recs)
    return await _read(root, log.session_id)


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
    await asyncio.to_thread(chat_store.create, _root(prov), sid, cwd=cwd)
    return sid


async def get_session(engine_id: str, session_id: str) -> dict:
    prov = _chat_provider(engine_id)
    key, root = _key(engine_id, session_id), _root(prov)
    async with _lock(key):
        log = await _reconcile(key, root, await _read(root, session_id))
    return _view(log)


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


async def _begin(engine_id: str, session_id: str, turn_id: str, text: str | None) -> dict:
    """THE transition into ``pending`` — send (``text``) and retry (``text is None``) alike."""
    prov = _chat_provider(engine_id)
    key, root = _key(engine_id, session_id), _root(prov)
    async with _lock(key):
        log = await _reconcile(key, root, await _read(root, session_id))
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
                raise ChatError(
                    409, "in_flight" if existing.status == "pending" else "already done"
                )
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
        await asyncio.to_thread(chat_store.append, root, session_id, *recs)
        _TASKS[key] = asyncio.create_task(_run(engine_id, root, session_id, turn_id))
    return {"turn": {"turn_id": turn_id, "status": "pending"}}


# --- the request ---------------------------------------------------------------------------------


def _tokens(text: str) -> int:
    return -(-len(text) // chat_config.CHARS_PER_TOKEN)


def _budget(cfg: dict, log: chat_store.ChatLog) -> int:
    # Re-read at every send: the prompt may have changed. With tools on, the declarations are
    # part of every request, so they come out of the budget too.
    system = prompts.effective(chat_config.prompt_id(cfg))
    if cfg.get("tools") == "read":
        system += json.dumps(chat_tools.SPECS)
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


async def _run(engine_id: str, root: Path, session_id: str, turn_id: str) -> None:
    """Make the request, then SETTLE it — write its outcome and give up the task slot — inside the
    session lock. That is what makes `_reconcile` safe: it holds the same lock, so it either sees
    this task still registered (and leaves the pending turn alone) or sees the settled records.
    Settling outside the lock let a read that had just seen `pending` find no task and mark a
    turn whose reply had already been stored as interrupted (Hermes on #1216)."""
    key = _key(engine_id, session_id)
    try:
        records = await _run_once(engine_id, root, session_id, turn_id)
    except Exception as e:  # noqa: BLE001 — a turn must always settle, whatever went wrong
        records = [_status(turn_id, "failed", f"internal error ({type(e).__name__})")]
    async with _lock(key):
        try:
            if records:
                await asyncio.to_thread(chat_store.append, root, session_id, *records)
        finally:
            if _TASKS.get(key) is asyncio.current_task():
                _TASKS.pop(key, None)


async def _run_once(engine_id: str, root: Path, session_id: str, turn_id: str) -> list[dict]:
    """The request itself. Returns the records that settle the turn; writes nothing."""

    def fail(reason: str, *extra: dict) -> list[dict]:
        return [*extra, _status(turn_id, "failed", reason)]

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
    # Two literal system messages, so the registry ratchet reads each call site's prompt id.
    if cfg["tools"] == "read":
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
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        # The permission is asked of the LIVE prefs every round, never the turn's snapshot.
        offer = cfg["tools"] == "read" and chat_config.tools_enabled(engine_id)
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
            body["tools"] = chat_tools.SPECS
            if last:
                body["tool_choice"] = "none"
        try:
            r = await review.post_chat_response(cfg, body)
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
                "function": {"name": name[:64], "arguments": args[:_ARGS_MAX]},
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
) -> int | None:
    """Run one round of tool calls and extend ``convo`` with the call and its results. Returns
    the tokens the round added (an ESTIMATE, like every budget here), or None on a bad shape."""
    calls = _sanitize_calls(raw_calls)
    if calls is None:
        return None
    convo.append(
        {
            "role": "assistant",
            "content": content if isinstance(content, str) else None,
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
    await asyncio.to_thread(chat_store.append, root, session_id, rec)


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
