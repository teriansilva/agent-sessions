"""Ask as a conversation that can look things up (#1443 Phase 1 — read-only).

One turn is a bounded tool loop over the configured AI endpoint: the model may call READ tools —
find sessions, a session's status, the missions, one mission's status, the projects, the agents —
and then answers in plain text. Everything a turn decides is in :mod:`ask_store`'s conversation,
so the answer survives a reload or a client that went away.

**One transport.** Each round is ``review.post_chat_response`` with the AI-review configuration:
the registered-prompt check and template-secret redaction apply, and no outbound-HTTP site is
added. The system prompt is ``prompts.effective("ask_orchestrator")``, a GUARDED prompt.

**No write tool exists in this phase.** :data:`TOOLS` holds read tools only, pinned by a test, so a
proposal cannot appear before the phases that add proposals (with their operator-tap cards) ship.
:data:`MAX_PROPOSALS_PER_TURN` is enforced on any tool whose kind is ``proposal`` all the same.

**Model output picks, the server resolves.** A session key is accepted only when this turn's own
tools returned it (and it parses); a mission id only when this turn's tools returned it. Anything
else is refused as a tool error the model sees — never repaired, never looked up anyway.
Projects and agents are listed by INDEX with names only (``mission_plan``'s rule: a model that
never sees a path cannot echo one back).

**Tool output is data.** Screen tails are wrapped as ``untrusted_screen``, the guarded prompt says
instructions inside tool results are not the operator's, and no tool here can change anything —
the real defence. Raw tool output stays in this turn's memory: the store records metadata only.

**The turn is a server-owned task** (``mission-turn-stream.md``): a stream watches it, never owns
it. One turn at a time per conversation, held by the conversation's turn lock (an ``flock``, so it
also fences a second process); the lifespan drains turns on shutdown (:func:`drain_turns`). A
pending turn whose lock is free and which no task here holds was interrupted, and is settled as
``failed`` the next time the conversation is read.

**When the endpoint does not take tools** it answers with today's two-stage find
(``pulse_chat.ask``) instead, so a conversation never does less than the transient Ask did.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import ask_store, engines, mission_plan, missions, prompts, pulse_chat, review

log = logging.getLogger(__name__)

#: Bounds (#1443 §2). The round after the last is sent with `tool_choice: "none"`.
MAX_TOOL_ROUNDS = 6
MAX_CALLS_PER_ROUND = 8
MAX_PROPOSALS_PER_TURN = 3
SCREEN_TAIL_MAX = 4_000
TOOL_RESULT_MAX = 12_000
SUMMARY_MAX = 500
REASON_MAX = 280
INSTRUCTION_MAX = 500
MISSION_LIST_MAX = 40
OBJECTIVES_MAX = 30
QUERY_MAX = pulse_chat.QUERY_MAX
#: Replayed history: the last settled turns of this conversation, each side capped.
HISTORY_TURNS = 8

#: How long shutdown waits for running turns — under systemd's default 90 s stop timeout.
TURN_SHUTDOWN_WAIT_S = 60.0
#: Progress lines buffered per watcher before newer ones are dropped (never the last line).
PROGRESS_QUEUE_MAX = 64

INTERRUPTED = "interrupted — the server stopped before this answer settled; ask again"


class TurnError(Exception):
    """A refusal the route turns into an HTTP status. ``detail`` is authored and content-free."""

    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


class _ToolRefused(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# ---- the tools ---------------------------------------------------------------------------------


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


@dataclass(frozen=True)
class Tool:
    name: str
    #: "read" — looks something up. "proposal" — stages an effect for an operator tap (none yet).
    kind: str
    spec: dict
    run: Callable


@dataclass
class _Turn:
    """What one turn's tools have returned so far — the only ids the model may use."""

    cid: str
    turn_id: str
    question: str
    working_keys: set[str]
    progress: Callable[[dict], None]
    keys: set[str] = field(default_factory=set)
    mission_ids: set[str] = field(default_factory=set)
    matches: dict[str, dict] = field(default_factory=dict)
    mission_matches: dict[str, dict] = field(default_factory=dict)
    proposals: int = 0
    _catalog: list[dict] | None = None

    async def catalog(self) -> list[dict]:
        """The in-scope catalog (roots + exclusions), read once per turn."""
        if self._catalog is None:
            from .routes.pulse import in_scope_cards

            cards = await asyncio.to_thread(
                pulse_chat.build_catalog, working_keys=self.working_keys
            )
            self._catalog = await asyncio.to_thread(in_scope_cards, cards)
        return self._catalog


def _str_arg(args: dict, name: str, cap: int, *, required: bool = True) -> str:
    v = args.get(name)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or not v.strip():
        raise _ToolRefused(f"{name} must be a non-empty string", "bad_arguments")
    v = v.strip()
    if len(v) > cap:
        raise _ToolRefused(f"{name} is longer than {cap} characters", "bad_arguments")
    return v


def _known_key(turn: _Turn, args: dict) -> str:
    key = args.get("key")
    if not isinstance(key, str) or key not in turn.keys:
        raise _ToolRefused(
            "refused: use a session key one of your tools returned in this turn", "unknown_key"
        )
    try:
        engines.parse_key(key)
    except Exception:  # noqa: BLE001 — a key that no longer parses is not a session
        raise _ToolRefused("refused: not a session key", "unknown_key") from None
    return key


def _known_mission(turn: _Turn, args: dict) -> str:
    mid = args.get("mission_id")
    if not isinstance(mid, str) or mid not in turn.mission_ids:
        raise _ToolRefused(
            "refused: use a mission id one of your tools returned in this turn", "unknown_mission"
        )
    if not missions.MISSION_ID_RE.match(mid):
        raise _ToolRefused("refused: not a mission id", "unknown_mission")
    return mid


def _project_name(card: dict) -> str:
    return str((card.get("project") or {}).get("name") or "")[: pulse_chat.PROJECT_MAX]


async def _find_sessions(turn: _Turn, args: dict) -> tuple[dict, dict]:
    query = _str_arg(args, "query", QUERY_MAX)

    def on_event(ev: dict) -> None:
        if ev.get("type") == "progress":
            turn.progress(
                {
                    k: v
                    for k, v in ev.items()
                    if k in ("type", "step", "sessions", "missions", "candidates")
                }
            )

    try:
        result = await pulse_chat.ask(query, working_keys=turn.working_keys, on_event=on_event)
    except review.NotConfiguredError:
        raise
    except review.ReviewError:
        raise _ToolRefused(
            "the search failed — the endpoint did not answer it", "search_failed"
        ) from None
    sessions, mission_rows = [], []
    for m in result.get("matches") or []:
        key = str(m.get("id") or "")
        turn.keys.add(key)
        turn.matches.setdefault(
            key, {"id": key, "title": str(m.get("title") or ""), "why": str(m.get("why") or "")}
        )
        sessions.append(
            {
                "key": key,
                "title": str(m.get("title") or "")[: pulse_chat.TITLE_MAX],
                "project": _project_name(m),
                "state": str(m.get("state") or ""),
                "why": str(m.get("why") or ""),
            }
        )
    for m in result.get("mission_matches") or []:
        mid = str(m.get("id") or "")
        turn.mission_ids.add(mid)
        turn.mission_matches.setdefault(
            mid,
            {
                "id": mid,
                "title": str(m.get("title") or ""),
                "state": str(m.get("state") or ""),
                "why": str(m.get("why") or ""),
            },
        )
        mission_rows.append(
            {
                "mission_id": mid,
                "title": str(m.get("title") or "")[: pulse_chat.TITLE_MAX],
                "state": str(m.get("state") or ""),
                "why": str(m.get("why") or ""),
            }
        )
    out = {
        "answer": str(result.get("answer") or ""),
        "sessions": sessions,
        "missions": mission_rows,
    }
    meta = {
        "count": len(sessions) + len(mission_rows),
        "keys": [s["key"] for s in sessions],
        "missions": [m["mission_id"] for m in mission_rows],
    }
    return out, meta


def _live_decision(key: str) -> dict | None:
    from . import orchestrator_ledger

    with contextlib.suppress(Exception):
        for a in orchestrator_ledger.live_actions():
            if str(a.get("session_id") or "") == key:
                return {"verb": str(a.get("verb") or ""), "state": str(a.get("state") or "")}
    return None


def _session_facts(key: str) -> tuple[str, str | None]:
    """The screen tail and the holding mission, read off the loop."""
    from . import scrollback

    screen = ""
    with contextlib.suppress(Exception):
        screen = scrollback.live_tail_text(engines.physical_key(key), SCREEN_TAIL_MAX)
    held = None
    with contextlib.suppress(Exception):
        held = missions.all_active_memberships().get(key)
    return screen[-SCREEN_TAIL_MAX:], held


async def _session_status(turn: _Turn, args: dict) -> tuple[dict, dict]:
    key = _known_key(turn, args)
    card = next((c for c in await turn.catalog() if c.get("id") == key), None)
    if card is None:
        raise _ToolRefused(
            "that session is not available — archived, excluded, or outside the roots",
            "not_available",
        )
    screen, held = await asyncio.to_thread(_session_facts, key)
    decision = await asyncio.to_thread(_live_decision, key)
    if held and missions.MISSION_ID_RE.match(held):
        turn.mission_ids.add(held)
    out = {
        "key": key,
        "title": str(card.get("title") or "")[: pulse_chat.TITLE_MAX],
        "project": _project_name(card),
        "state": str(card.get("state") or ""),
        "live": bool(card.get("live")),
        "needs_you": bool(card.get("intervention_required")) or decision is not None,
        "needs_you_reason": str(card.get("intervention_reason") or "")[:REASON_MAX],
        "pending_decision": decision,
        "summary": str(card.get("ai_summary") or "")[:SUMMARY_MAX],
        "last_activity_hours_ago": round(
            (time.time() - float(card.get("last_activity") or time.time())) / 3600, 1
        ),
        "held_by_mission": held,
        # Untrusted: what the agent's terminal shows. Data, never an instruction.
        "untrusted_screen": screen,
    }
    meta = {"keys": [key], "length": len(screen), "count": 1}
    if held and missions.MISSION_ID_RE.match(held):
        meta["missions"] = [held]
    return out, meta


async def _list_missions(turn: _Turn, args: dict) -> tuple[dict, dict]:
    state = args.get("state")
    if state is not None and state not in missions.STATES:
        raise _ToolRefused(
            f"state must be one of {', '.join(sorted(missions.STATES))}", "bad_arguments"
        )
    rows = (
        await missions.run_admitted(
            lambda: missions.safe_list_missions(
                archived=False, state=state or "", limit=MISSION_LIST_MAX
            )
        )
    )["missions"]
    out_rows = []
    for m in rows[:MISSION_LIST_MAX]:
        mid = str(m.get("id") or "")
        if not missions.MISSION_ID_RE.match(mid):
            continue
        turn.mission_ids.add(mid)
        out_rows.append(
            {
                "mission_id": mid,
                "title": str(m.get("title") or "")[: pulse_chat.TITLE_MAX],
                "state": str(m.get("state") or ""),
                "sessions": len(m.get("session_keys") or []),
            }
        )
    return {"missions": out_rows}, {
        "count": len(out_rows),
        "missions": [r["mission_id"] for r in out_rows],
    }


async def _mission_status(turn: _Turn, args: dict) -> tuple[dict, dict]:
    mid = _known_mission(turn, args)
    try:
        m = await missions.run_admitted(
            lambda: missions.get_mission(mid, events_limit=1, attention=True)
        )
    except missions.MissionError:
        raise _ToolRefused("the mission store is busy — try again", "busy") from None
    if m is None:
        raise _ToolRefused("that mission no longer exists", "not_available")
    keys = [
        str(s.get("session_key") or "")
        for s in (m.get("sessions") or [])
        if not s.get("removed_at")
    ]
    keys = [k for k in keys if k]
    turn.keys.update(keys)
    objectives = [
        {
            "title": str(o.get("title") or "")[:200],
            "state": str(o.get("state") or ""),
            "gate": bool(o.get("gate")),
        }
        for o in (m.get("objectives") or [])[:OBJECTIVES_MAX]
    ]
    out = {
        "mission_id": mid,
        "title": str(m.get("title") or "")[: pulse_chat.TITLE_MAX],
        "state": str(m.get("state") or ""),
        "instruction": str(m.get("instruction") or "")[:INSTRUCTION_MAX],
        "needs_you": bool(m.get("needs_you")),
        "objectives": objectives,
        "objectives_met": sum(1 for o in objectives if o["state"] == "met"),
        "session_keys": keys[:20],
    }
    return out, {"count": len(objectives), "keys": keys[:20], "missions": [mid]}


async def _list_projects(turn: _Turn, args: dict) -> tuple[dict, dict]:
    options = await asyncio.to_thread(mission_plan.project_options)
    rows = [{"index": i, "name": p["name"]} for i, p in enumerate(options)]
    return {"projects": rows}, {"count": len(rows)}


def _agent_rows() -> list[dict]:
    from .engines import registry

    rows = []
    for prov in registry.present_providers():
        if not registry.is_agent(prov):
            continue
        manifest = registry.manifest_of(prov)
        name = getattr(getattr(manifest, "display", None), "name", "") or prov.engine_id
        rows.append({"index": len(rows), "id": prov.engine_id, "name": str(name)})
    return rows


async def _list_agents(turn: _Turn, args: dict) -> tuple[dict, dict]:
    rows = await asyncio.to_thread(_agent_rows)
    return {"agents": rows}, {"count": len(rows)}


_KEY_PROP = {"key": {"type": "string", "description": "a session key a tool returned"}}

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "find_sessions",
            "read",
            _spec(
                "find_sessions",
                "Search the operator's sessions and missions by topic. Returns matching session "
                "keys and mission ids with why each matched. The only way to find 'the X session'.",
                {"query": {"type": "string", "description": "what to look for, in words"}},
                ["query"],
            ),
            _find_sessions,
        ),
        Tool(
            "session_status",
            "read",
            _spec(
                "session_status",
                "One session's live state: whether it is working or idle, whether it needs the "
                "operator and why, a pending decision, its summary, and its terminal screen.",
                _KEY_PROP,
                ["key"],
            ),
            _session_status,
        ),
        Tool(
            "list_missions",
            "read",
            _spec(
                "list_missions",
                "The operator's current missions, newest first, optionally only one state.",
                {"state": {"type": "string", "enum": sorted(missions.STATES)}},
                [],
            ),
            _list_missions,
        ),
        Tool(
            "mission_status",
            "read",
            _spec(
                "mission_status",
                "One mission: its state, objectives and which are met, whether it needs the "
                "operator, and the session keys it holds.",
                {"mission_id": {"type": "string", "description": "a mission id a tool returned"}},
                ["mission_id"],
            ),
            _mission_status,
        ),
        Tool(
            "list_projects",
            "read",
            _spec("list_projects", "The operator's projects, by index and name.", {}, []),
            _list_projects,
        ),
        Tool(
            "list_agents",
            "read",
            _spec("list_agents", "The coding agents available here, by index and name.", {}, []),
            _list_agents,
        ),
    )
}


def tool_specs() -> list[dict]:
    return [t.spec for t in TOOLS.values()]


async def _call(turn: _Turn, name: str, raw_args: str) -> tuple[str, dict, dict]:
    """Run one tool call. Returns ``(content for the model, args to store, metadata to store)``."""
    tool = TOOLS.get(name)
    if tool is None:
        refusal = json.dumps({"error": f"refused: no tool named {name!r}"})
        return refusal, {}, {"error": "unknown_tool"}
    try:
        args = json.loads(raw_args or "{}")
    except ValueError:
        args = None
    if not isinstance(args, dict):
        refusal = json.dumps({"error": "arguments are not a JSON object"})
        return refusal, {}, {"error": "bad_arguments"}
    declared = tool.spec["function"]["parameters"]["properties"]
    # Declared STRING arguments only; the store guards each one. Nothing else the model sent is
    # kept, whatever its type.
    stored_args = {k: v for k, v in args.items() if k in declared and isinstance(v, str)}
    if tool.kind == "proposal":
        if turn.proposals >= MAX_PROPOSALS_PER_TURN:
            return (
                json.dumps(
                    {"error": f"refused: at most {MAX_PROPOSALS_PER_TURN} proposals per turn"}
                ),
                stored_args,
                {"error": "too_many_proposals"},
            )
        turn.proposals += 1
    turn.progress({"type": "progress", "step": "tool", "tool": name})
    try:
        result, meta = await tool.run(turn, args)
    except _ToolRefused as e:
        return json.dumps({"error": str(e)}), stored_args, {"error": e.code}
    content = json.dumps(result, ensure_ascii=False)
    if len(content) > TOOL_RESULT_MAX:
        return (
            json.dumps({"error": "refused: the result was too large — narrow the question"}),
            stored_args,
            {"error": "too_large"},
        )
    return content, stored_args, meta


# ---- the turn ----------------------------------------------------------------------------------


def _history(records: list[dict], turn_id: str) -> list[dict]:
    """The settled exchanges before ``turn_id``, as user/assistant messages (stored text — already
    through the store boundary)."""
    questions: dict[str, str] = {}
    answers: dict[str, str] = {}
    order: list[str] = []
    for r in records:
        tid = r.get("turn_id")
        if tid == turn_id:
            break
        if r.get("type") == "user" and isinstance(r.get("text"), str):
            questions[tid] = r["text"]
            order.append(tid)
        elif r.get("type") == "assistant" and isinstance(r.get("text"), str):
            answers[tid] = r["text"]
    out: list[dict] = []
    for tid in order[-HISTORY_TURNS:]:
        if tid in answers:
            out += [
                {"role": "user", "content": questions[tid]},
                {"role": "assistant", "content": answers[tid]},
            ]
    return out


def _tools_rejected(r) -> bool:
    from .chat_runtime import _tools_rejected as rejected

    return rejected(r)


def _sanitize(calls: list) -> list[dict] | None:
    from .chat_runtime import _sanitize_calls

    return _sanitize_calls(calls)


async def _fallback_find(turn: _Turn, history: list[dict]) -> list[dict]:
    """The endpoint refused tools: answer as the transient Ask does (two-stage find)."""
    result = await pulse_chat.ask(
        turn.question,
        history,
        working_keys=turn.working_keys,
        on_event=lambda ev: turn.progress(ev) if ev.get("type") == "progress" else None,
    )
    for m in result.get("matches") or []:
        key = str(m.get("id") or "")
        turn.matches.setdefault(
            key, {"id": key, "title": str(m.get("title") or ""), "why": str(m.get("why") or "")}
        )
    for m in result.get("mission_matches") or []:
        mid = str(m.get("id") or "")
        turn.mission_matches.setdefault(
            mid,
            {
                "id": mid,
                "title": str(m.get("title") or ""),
                "state": str(m.get("state") or ""),
                "why": str(m.get("why") or ""),
            },
        )
    return [_assistant(turn, str(result.get("answer") or "")), _status(turn, "done")]


def _assistant(turn: _Turn, text: str) -> dict:
    return {
        "type": "assistant",
        "turn_id": turn.turn_id,
        "text": text,
        "matches": list(turn.matches.values())[: ask_store.MATCHES_MAX],
        "mission_matches": list(turn.mission_matches.values())[: ask_store.MATCHES_MAX],
    }


def _status(turn: _Turn, status: str, detail: str = "", code: str | None = None) -> dict:
    rec: dict = {"type": "status", "turn_id": turn.turn_id, "status": status}
    if detail:
        rec["detail"] = detail
    if code:
        rec["code"] = code
    return rec


async def _append(cid: str, *records: dict) -> None:
    await asyncio.to_thread(ask_store.append, cid, *records)


async def run_turn(turn: _Turn) -> list[dict]:
    """The turn itself. Writes tool records as it goes; RETURNS the records that settle it."""

    def fail(detail: str, code: str | None = None) -> list[dict]:
        return [_status(turn, "failed", detail, code)]

    try:
        cfg = review._require_config()
    except review.NotConfiguredError:
        return fail("the AI endpoint is not configured", "unconfigured")
    records = await asyncio.to_thread(ask_store.read, turn.cid)
    history = _history(records, turn.turn_id)
    convo: list[dict] = [
        {"role": "system", "content": prompts.effective("ask_orchestrator")},
        # Stored user/assistant text only; the pinned sanitizer gates the role at the sink.
        *pulse_chat.bound_history(history),
        {"role": "user", "content": turn.question},
    ]
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        last = round_no == MAX_TOOL_ROUNDS
        turn.progress({"type": "progress", "step": "thinking", "round": round_no})
        body: dict = {
            "model": cfg["model"],
            "messages": convo,
            "temperature": 0,
            "stream": False,
            "tools": tool_specs(),
        }
        if last:
            body["tool_choice"] = "none"
        try:
            r = await review.post_chat_response(cfg, body)
        except review.TransportTimeout as e:
            return fail(str(e), "timeout")
        except review.ReviewError as e:
            return fail(str(e))
        if r.status_code != 200:
            if round_no == 0 and _tools_rejected(r):
                try:
                    return await _fallback_find(turn, history)
                except review.ReviewError as e:
                    return fail(str(e))
            what = {401: "the key was rejected", 403: "access was refused", 429: "rate limited"}
            suffix = f" — {what[r.status_code]}" if r.status_code in what else ""
            return fail(f"the endpoint refused the request (HTTP {r.status_code}{suffix})")
        try:
            payload = r.json()
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return fail("the endpoint returned an unexpected response shape")
        if choice.get("finish_reason") == "length":
            # A completion cut off at the token limit is not an answer — and a cut tool call's
            # arguments are not what the model meant either. Never stored as complete.
            return fail("the endpoint cut the answer off at its length limit", "truncated")
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not last and isinstance(calls, list) and calls:
            # Bounded BEFORE anything is replayed or recorded: calls past the cap are dropped from
            # the turn entirely, so neither the conversation nor the store grows with them.
            clean = _sanitize(calls[:MAX_CALLS_PER_ROUND])
            if clean is None:
                return fail("the endpoint returned an unexpected response shape")
            convo.append(
                {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else None,
                    "tool_calls": clean,
                }
            )
            for n, call in enumerate(clean):
                name, args, cid = (
                    call["function"]["name"],
                    call["function"]["arguments"],
                    call["id"],
                )
                result, stored, meta = await _call(turn, name, args)
                convo.append({"role": "tool", "tool_call_id": cid, "content": result})
                await _append(
                    turn.cid,
                    {
                        "type": "tool",
                        "turn_id": turn.turn_id,
                        "call_id": cid if ask_store._CALL_ID_RE.match(cid) else f"call_{n}",
                        "name": name if name in TOOLS else "unknown",
                        "args": stored,
                        "meta": {**meta, "rounds": round_no + 1},
                    },
                )
            continue
        if not isinstance(content, str) or not content.strip():
            if calls:
                return fail("the endpoint kept calling tools after it was told to answer")
            return fail("the endpoint returned an empty answer")
        return [_assistant(turn, content.strip()), _status(turn, "done")]
    return fail("the endpoint kept calling tools after it was told to answer")  # pragma: no cover


# ---- turn lifecycle: one at a time per conversation, owned by the server -----------------------


@dataclass
class _Running:
    turn_id: str
    task: asyncio.Task
    listeners: set[asyncio.Queue] = field(default_factory=set)


_RUNNING: dict[str, _Running] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(cid: str) -> asyncio.Lock:
    lk = _LOCKS.get(cid)
    if lk is None:
        lk = _LOCKS[cid] = asyncio.Lock()
    return lk


def _turn_statuses(records: list[dict]) -> dict[str, dict]:
    last: dict[str, dict] = {}
    for r in records:
        if r.get("type") == "status" and isinstance(r.get("turn_id"), str):
            last[r["turn_id"]] = r
    return last


def _reconcile_owned(cid: str) -> list[dict]:
    """Settle pending turns no task holds — under the turn lock, so a turn another process runs is
    never declared interrupted. Blocking."""
    records = ask_store.read(cid)
    if not any(st.get("status") == "pending" for st in _turn_statuses(records).values()):
        return records
    with ask_store.turn_lock(cid) as free:
        if not free:
            return records
        return _settle_stale_locked(cid)


async def read_conversation(cid: str) -> list[dict]:
    """The conversation's records, with interrupted turns settled. Raises ConversationNotFound."""
    async with _lock(cid):
        if cid in _RUNNING:
            return await asyncio.to_thread(ask_store.read, cid)
        return await asyncio.to_thread(_reconcile_owned, cid)


def turn_views(records: list[dict]) -> list[dict]:
    """The conversation as the client renders it: one entry per question, in order."""
    turns: dict[str, dict] = {}
    order: list[str] = []
    for r in records:
        tid = r.get("turn_id")
        kind = r.get("type")
        if not isinstance(tid, str):
            continue
        if kind == "user":
            if tid in turns:
                continue
            turns[tid] = {
                "turn_id": tid,
                "question": r.get("text") or "",
                "asked_at": r.get("ts"),
                "status": "pending",
                "answer": None,
                "detail": None,
                "code": None,
                "matches": [],
                "mission_matches": [],
                "tools": [],
            }
            order.append(tid)
            continue
        t = turns.get(tid)
        if t is None:
            continue
        if kind == "assistant":
            t["answer"] = r.get("text") or ""
            t["matches"] = r.get("matches") or []
            t["mission_matches"] = r.get("mission_matches") or []
        elif kind == "status":
            t["status"] = r.get("status")
            t["detail"] = r.get("detail")
            t["code"] = r.get("code")
        elif kind == "tool":
            meta = r.get("meta") or {}
            t["tools"].append(
                {"name": r.get("name"), "outcome": "refused" if meta.get("error") else "ok"}
            )
    return [turns[t] for t in order]


def _view_of(records: list[dict], turn_id: str) -> dict | None:
    return next((t for t in turn_views(records) if t["turn_id"] == turn_id), None)


def _broadcast(cid: str, ev: dict) -> None:
    running = _RUNNING.get(cid)
    if running is None:
        return
    for q in list(running.listeners):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(ev)


async def _settle(turn: _Turn, lock_fd: int) -> dict:
    """Run the turn, write its outcome, give up the slot and the lock — whatever happened."""
    try:
        try:
            records = await run_turn(turn)
        except review.NotConfiguredError:
            records = [_status(turn, "failed", "the AI endpoint is not configured", "unconfigured")]
        except ask_store.StoreRefused as e:
            records = [_status(turn, "failed", str(e))]
        except Exception as e:  # noqa: BLE001 — a turn must always settle
            log.warning("ask turn failed (%s)", type(e).__name__)
            records = [_status(turn, "failed", f"internal error ({type(e).__name__})")]
        try:
            await _append(turn.cid, *records)
        except ask_store.StoreRefused as e:
            await _append(turn.cid, _status(turn, "failed", str(e)))
        stored = await asyncio.to_thread(ask_store.read, turn.cid)
        return _view_of(stored, turn.turn_id) or {}
    finally:
        async with _lock(turn.cid):
            running = _RUNNING.get(turn.cid)
            if running is not None and running.task is asyncio.current_task():
                _RUNNING.pop(turn.cid, None)
            os.close(lock_fd)


def _stored_digest(records: list[dict], turn_id: str) -> str | None:
    for r in records:
        if r.get("type") == "user" and r.get("turn_id") == turn_id:
            return r.get("qd") if isinstance(r.get("qd"), str) else ""
    return None


def _repeat(records: list[dict], turn_id: str, qd: str) -> dict | None:
    """The settled-or-running view a repeat of ``turn_id`` names, or None for a new id. A repeat
    must carry the SAME question — compared by keyed digest of the original text, never by the
    stored (redacted, possibly withheld) display text. Raises TurnError(409) otherwise."""
    stored = _stored_digest(records, turn_id)
    if stored is None:
        return None
    if stored != qd:
        raise TurnError(409, "that turn id was already used for a different question")
    return _view_of(records, turn_id)


def _settle_stale_locked(cid: str) -> list[dict]:
    """Read the conversation with the turn lock HELD by the caller: any pending turn is then one
    no live task owns, and is settled as interrupted. Blocking."""
    records = ask_store.read(cid)
    stale = [t for t, st in _turn_statuses(records).items() if st.get("status") == "pending"]
    if stale:
        ask_store.append(
            cid,
            *(
                {
                    "type": "status",
                    "turn_id": t,
                    "status": "failed",
                    "detail": INTERRUPTED,
                    "code": "interrupted",
                }
                for t in stale
            ),
        )
        records = ask_store.read(cid)
    return records


async def begin(
    cid: str, turn_id: str, text: str, *, working_keys: set[str]
) -> tuple[asyncio.Task | None, dict | None]:
    """Start a turn, or find the one this ``turn_id`` already names.

    Returns ``(task, None)`` for a running turn (new, or the same id still running), or
    ``(None, view)`` for a turn that already settled: a repeat never runs the model again. A
    watcher subscribes to progress itself (:func:`listen`), only once it is actually streaming.
    Raises :class:`TurnError`.

    Every decision that admits a NEW turn — the id is unused, nothing is pending, the conversation
    has room — is taken again after the cross-process turn lock is held, on a fresh read: the
    first look is only a fast path, and another process may have run and settled this very id in
    between."""
    qd = await asyncio.to_thread(ask_store.question_digest, text)
    async with _lock(cid):
        records = await asyncio.to_thread(
            ask_store.read if cid in _RUNNING else _reconcile_owned, cid
        )
        existing = _repeat(records, turn_id, qd)
        if existing is not None:
            running = _RUNNING.get(cid)
            if running is not None and running.turn_id == turn_id:
                return running.task, None
            if existing["status"] == "pending":
                raise TurnError(409, "that turn is still running in another process")
            return None, existing
        if cid in _RUNNING or any(t["status"] == "pending" for t in turn_views(records)):
            raise TurnError(409, "a question is already running in this conversation")
        try:
            review._require_config()
        except review.NotConfiguredError:
            raise TurnError(409, "AI endpoint is not configured", configured=False) from None
        lock_fd = await asyncio.to_thread(ask_store.try_turn_lock, cid)
        if lock_fd is None:
            raise TurnError(409, "a question is already running in this conversation")
        try:
            records = await asyncio.to_thread(_settle_stale_locked, cid)
            existing = _repeat(records, turn_id, qd)
            if existing is not None:
                os.close(lock_fd)
                return None, existing
            if len(turn_views(records)) >= ask_store.MAX_TURNS:
                raise TurnError(409, "this conversation is full — start a new one")
            await _append(
                cid,
                {"type": "user", "turn_id": turn_id, "text": text, "qd": qd},
                {"type": "status", "turn_id": turn_id, "status": "pending"},
            )
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(lock_fd)
            raise
        turn = _Turn(
            cid=cid,
            turn_id=turn_id,
            question=text,
            working_keys=working_keys,
            progress=lambda ev: _broadcast(cid, ev),
        )
        task = asyncio.get_running_loop().create_task(_settle(turn, lock_fd), name=f"ask:{cid}")
        _RUNNING[cid] = _Running(turn_id, task)

        def _done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.warning("ask turn settle failed (%s)", type(t.exception()).__name__)

        task.add_done_callback(_done)
        return task, None


def listen(cid: str, task: asyncio.Task) -> asyncio.Queue:
    """A fresh progress queue for the watcher of ``task``. Registered only while that task is the
    conversation's running turn; pair every call with :func:`unlisten`."""
    q: asyncio.Queue = asyncio.Queue(PROGRESS_QUEUE_MAX)
    running = _RUNNING.get(cid)
    if running is not None and running.task is task:
        running.listeners.add(q)
    return q


def unlisten(cid: str, queue: asyncio.Queue | None) -> None:
    """A watcher that has gone stops receiving progress at once — not when the turn settles."""
    running = _RUNNING.get(cid)
    if running is not None and queue is not None:
        running.listeners.discard(queue)


def running(cid: str) -> bool:
    return cid in _RUNNING


async def drain_turns(timeout: float = TURN_SHUTDOWN_WAIT_S) -> None:
    """Let running turns settle before the app goes away: a turn whose reader left belongs to no
    request, so the server's wait for requests in flight does not cover it."""
    pending = {r.task for r in _RUNNING.values()}
    if pending:
        log.info("waiting for %d Ask turn(s) to settle", len(pending))
        await asyncio.wait(pending, timeout=timeout)
