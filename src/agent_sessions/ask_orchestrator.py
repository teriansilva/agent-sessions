"""Ask as a conversation that can look things up, ask back and propose (#1443 Phases 1–3a).

One turn is a bounded tool loop over the configured AI endpoint: the model may call READ tools —
find sessions, a session's status, the missions, one mission's status, the projects, the agents —
and then answers in plain text. Everything a turn decides is in :mod:`ask_store`'s conversation,
so the answer survives a reload or a client that went away.

**One transport.** Each round is ``review.post_chat_response`` with the AI-review configuration:
the registered-prompt check and template-secret redaction apply, and no outbound-HTTP site is
added. The system prompt is ``prompts.effective("ask_orchestrator")``, a GUARDED prompt. A turn
with an HQ contact (#1525) sends that contact's registered persona as its own system message
FIRST, so the guarded prompt, and its guard, stay the last instruction the model reads.

**Nothing here sends or starts.** :data:`TOOLS` is pinned by name AND kind: read tools,
``ask_operator`` (ends the turn on a stored question), ``propose_nudge`` (Phase 2: stages a ledger
action that starts ``proposed`` whatever the tier; only the operator's tap on its card delivers it,
through the existing approve route) and ``propose_session`` (Phase 3a: stages a session to start in
``ask_proposals``; only the operator's tap launches it). :data:`MAX_PROPOSALS_PER_TURN` bounds
proposals per turn.

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
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from . import (
    ask_operations,
    ask_settings,
    ask_store,
    engines,
    hq_contacts,
    hq_usage,
    mission_plan,
    missions,
    prompts,
    pulse_chat,
    review,
    template_secrets,
)

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
    #: Who answers (#1525): a known HQ contact id, or ``off`` for plain HQ (no persona).
    contact: str = hq_contacts.OFF
    #: Sessions VISIBLY working right now (`actuator.busy_keys`), snapshotted when the turn began.
    busy_keys: set[str] = field(default_factory=set)
    #: The question this turn answers (`ask_operator`, #1443 Phase 2): ``{"question", ...}``.
    answering: dict | None = None
    #: The question this turn ends on, once `ask_operator` has been called.
    asked: dict | None = None
    #: The project and agent lists THIS turn's tools returned (#1443 Phase 3a): what an index in
    #: `propose_session` means. None until the tool ran in this turn.
    projects: list[dict] | None = None
    agents: list[dict] | None = None
    #: Who asked (#1559): their request identity (`asgi_call.Operator`) and the app, so the facts an
    #: operation card shows are read as THEY would read them. Memory only, never stored.
    operator: object | None = None
    app: object | None = None
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


#: Projects listed beyond the startable ones (archived, or with no folder yet), at most.
OTHER_PROJECTS_MAX = 50


def _project_rows() -> list[dict]:
    """Startable projects first (``mission_plan.project_options``, the order `propose_session`
    has always used), then the rest — archived or folderless — so they can be acted on (#1559).
    Names and flags only reach the model; ids and folders stay in the turn."""
    from . import projects

    options = mission_plan.project_options()
    rows = [dict(p, archived=False, startable=True) for p in options]
    seen = {p["id"] for p in options}
    with contextlib.suppress(Exception):
        rest = sorted(
            (e for e in projects.load().values() if e.id not in seen),
            key=lambda e: (e.archived, e.name.lower(), e.id),
        )
        for e in rest[:OTHER_PROJECTS_MAX]:
            rows.append(
                {"id": e.id, "name": e.name, "cwd": "", "archived": e.archived, "startable": False}
            )
    return rows


async def _list_projects(turn: _Turn, args: dict) -> tuple[dict, dict]:
    options = await asyncio.to_thread(_project_rows)
    turn.projects = options
    rows = [
        {
            "index": i,
            "name": p["name"],
            "archived": p["archived"],
            "can_start_session": p["startable"],
        }
        for i, p in enumerate(options)
    ]
    return {"projects": rows}, {"count": len(rows)}


#: Why an agent can't be started, as HQ may say it (#1554): a CLOSED table of vetted, authored
#: reasons. Anything else the start gate says becomes the generic line — never echoed.
WHY_NOT_GENERIC = "cannot be started right now"


def _why_not(why: str) -> str:
    from . import engines, handoff

    if not why:
        return ""
    table = {
        engines.REMOVED_REASON: "it has been removed, so it can't start new sessions",
        handoff._REASON_NOT_INSTALLED: "it is not installed",
        handoff._REASON_NO_SEED: "it can't take a first message yet",
        handoff._REASON_NOT_AGENT: "it is not an agent",
    }
    if why in table:
        return table[why]
    # `registry.unattended_start_state`'s one reason names the engine; matched by its fixed tail.
    if "does not reveal its session id until after its first turn" in why:
        return (
            "it reveals its session only after its first turn, so it can't be started "
            "unattended; start it from a terminal"
        )
    return WHY_NOT_GENERIC


def _agent_rows() -> list[dict]:
    from . import ask_settings, automation_runner, engines
    from .engines import registry

    labels = ask_settings.agent_labels()
    rows = []
    for prov in registry.present_providers():
        if not registry.is_agent(prov):
            continue
        name = labels.get(prov.engine_id) or f"agent {len(rows) + 1}"
        can_start, why = automation_runner.engine_state(prov.engine_id)
        rows.append(
            {
                "index": len(rows),
                "id": prov.engine_id,
                "name": str(name),
                "can_start": can_start,
                "why_not": "" if can_start else _why_not(why),
                "retiring": bool(engines.is_retiring(prov)),
            }
        )
    return rows


async def _list_agents(turn: _Turn, args: dict) -> tuple[dict, dict]:
    rows = await asyncio.to_thread(_agent_rows)
    turn.agents = rows
    shown = [
        {
            "index": r["index"],
            "name": r["name"],
            "can_start": r["can_start"],
            **({"why_not": r["why_not"]} if r.get("why_not") else {}),
            **({"retiring": True} if r.get("retiring") else {}),
        }
        for r in rows
    ]
    return {"agents": shown}, {"count": len(rows)}


# ---- usage, the fleet and the settings view (#1554) --------------------------------------------


def _hours_ago(ts: object) -> float | None:
    """Hours since ``ts`` (epoch seconds), computed here so the model does no epoch arithmetic."""
    if not isinstance(ts, int | float) or isinstance(ts, bool) or not ts:
        return None
    return round(max(0.0, time.time() - float(ts)) / 3600, 1)


USAGE_NOTES = {
    "plan": "A quota the agent itself reported: used_pct per window, resetting at resets_in_hours.",
    "tokens": "A token count the agent reported over window_days. It means something only against "
    "limit_tokens, the operator's own limit; with no limit there is no percentage.",
    "manual": "The operator's own counter, typed in Settings → Agents. Not measured by the agent.",
    "none": "Not measured: this agent reports nothing and no counter is set. That is NOT 0 %.",
}


def _hours_until(ts: object, now: float) -> float | None:
    if not isinstance(ts, int | float) or isinstance(ts, bool) or not ts:
        return None
    return round(max(0.0, float(ts) - now) / 3600, 1)


def _usage_rows() -> list[dict]:
    """`agent_usage.snapshot` (the cached read the Agents panel serves — it never probes), each row
    BUILT from named fields: no vendor free text reaches the model unless it passes a shape gate."""
    from . import agent_usage, ask_settings, prefs

    budgets = prefs.get_agent_budgets()
    names = ask_settings.agent_labels()
    now = time.time()
    out = []
    for row in agent_usage.snapshot(budgets=budgets):
        source = row.get("source")
        if source not in USAGE_NOTES:
            continue
        r: dict = {
            "agent": names.get(row.get("engine"), "an agent"),
            "source": source,
            "note": USAGE_NOTES[source],
            "stale": bool(row.get("stale")),
            "probe_failed": bool(row.get("error")),
            "checked_hours_ago": _hours_ago(row.get("checked_at") or row.get("at") or None),
            "alert_threshold_pct": budgets.get("threshold_pct"),
        }
        # Vendor text is untrusted and no shape test proves a string is not a key: the plan name
        # is never sent, and a window keeps its label only from `window_label`'s closed set.
        if source == "plan":
            r["windows"] = [
                {
                    "label": ask_settings.window_label(w.get("label")) or f"window {i + 1}",
                    "used_pct": w.get("used_pct"),
                    "resets_in_hours": _hours_until(w.get("resets_at"), now),
                }
                for i, w in enumerate(row.get("windows") or [])
                if isinstance(w, dict)
            ]
        if source == "tokens":
            r["billable_tokens"] = agent_usage.billable(row.get("tokens"))
            r["window_days"] = row.get("window_days")
        if source in ("tokens", "manual"):
            r["limit_tokens"] = row.get("limit_tokens") or None
        if source == "manual":
            r["manual_used"] = row.get("manual_used")
        # `none` carries NO percentage at all: not measured is not 0 %.
        if source != "none" and isinstance(row.get("used_pct"), int | float):
            r["used_pct"] = row["used_pct"]
        acc = row.get("access")
        access = acc.get("state") if isinstance(acc, dict) else None
        r["access"] = access if access in ("ok", "denied") else "unknown"
        fc = row.get("forecast")
        if isinstance(fc, dict) and fc.get("state") in ("ok", "exhausts", "out", "learning"):
            r["forecast"] = fc["state"]
        out.append(r)
    return out


async def _agent_usage(turn: _Turn, args: dict) -> tuple[dict, dict]:
    rows = await asyncio.to_thread(_usage_rows)
    return {"agents": rows}, {"count": len(rows)}


def _fleet_missions() -> dict | None:
    """Missions per state, from each state's own `total` (computed over the full filtered set
    before `limit`) — never summed from a capped list. Any read that fails, or answers with a
    `store_error` (the degraded `total: 0`), makes the whole answer unknown: never zeros."""
    out: dict[str, int] = {}
    for state in sorted(missions.STATES):
        r = missions.safe_list_missions(archived=False, state=state, limit=1)
        if r.get("store_error") is not None or not isinstance(r.get("total"), int):
            return None
        out[state] = r["total"]
    return out


async def _fleet_status(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """Counts only. Sessions = exactly what `find_sessions` searches: the STRICT card read, then the
    terminal's roots + exclusions boundary. States are Pulse's own, each session counted once."""
    from . import ask_settings, pulse
    from .routes.pulse import in_scope_cards

    complete = True
    sessions: dict | None
    try:
        cards = await asyncio.to_thread(
            pulse.build_cards, window_days=None, working_keys=turn.working_keys, strict=True
        )
        cards = await asyncio.to_thread(in_scope_cards, cards)
        names = await asyncio.to_thread(ask_settings.agent_labels)
        sessions = {}
        for c in cards:
            agent = names.get(str(c.get("engine") or ""), "other agents")
            state = str(c.get("state") or "")
            if state not in ("needs_you", "in_flight", "recently_active", "idle"):
                state = "idle"
            sessions.setdefault(agent, {}).setdefault(state, 0)
            sessions[agent][state] += 1
    except pulse.CardsIncomplete:
        # No fail-soft fallback: its counts could lose archive exclusions and ADD sessions.
        sessions, complete = None, False
    try:
        mission_counts = await missions.run_admitted(_fleet_missions)
    except Exception:  # noqa: BLE001 — an unreadable mission store is unknown, never zero
        mission_counts = None
    if mission_counts is None:
        complete = False
    return (
        {
            "complete": complete,
            "sessions": sessions,
            "missions": mission_counts,
            "note": "Sessions are those HQ can search (inside your project roots, outside the "
            "excluded folders). needs_you = waiting for the operator, even if running; "
            "in_flight = its agent process is running, not necessarily mid-turn; "
            "recently_active = activity in the last days; idle = the rest. Null means it "
            "could not be read just now, never zero.",
        },
        {"count": sum(sum(v.values()) for v in (sessions or {}).values())},
    )


async def _read_settings(turn: _Turn, args: dict) -> tuple[dict, dict]:
    from . import ask_settings

    try:
        out = await asyncio.to_thread(ask_settings.read_settings, args.get("section"))
    except ask_settings.UnknownSection as e:
        raise _ToolRefused(f"refused: {e}", "bad_arguments") from None
    return out, {"count": 1}


# ---- asking back and nudging (#1443 Phase 2) ---------------------------------------------------


async def _ask_operator(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """End the turn on a bounded question. The options are the reply texts the server will send
    on the operator's behalf, so they are fixed here and stored; the client picks one by index."""
    if turn.asked is not None:
        raise _ToolRefused("refused: this turn already asks one question", "already_asked")
    question = _str_arg(args, "question", ask_store.QUESTION_MAX)
    raw = args.get("options") or []
    if not isinstance(raw, list) or len(raw) > ask_store.OPTIONS_MAX:
        raise _ToolRefused(
            f"options must be a list of at most {ask_store.OPTIONS_MAX} strings", "bad_arguments"
        )
    options: list[str] = []
    for o in raw:
        if not isinstance(o, str) or not o.strip() or len(o.strip()) > ask_store.OPTION_MAX:
            raise _ToolRefused(
                f"each option must be a non-empty string of at most {ask_store.OPTION_MAX} "
                "characters",
                "bad_arguments",
            )
        if o.strip() not in options:
            options.append(o.strip())
    allow_text = args.get("allow_text") is True
    if not options and not allow_text:
        raise _ToolRefused("give options, allow a free-text answer, or both", "bad_arguments")
    turn.asked = {
        "question_id": str(uuid.uuid4()),
        "question": question,
        "options": options,
        "allow_text": allow_text,
    }
    return {"asked": True, "note": "Your turn ends here; the operator answers next."}, {
        "count": len(options)
    }


def _nudge_refusal(turn: _Turn, key: str, card: dict | None) -> tuple[str, str, dict] | None:
    """Every gate a nudge must pass, read off the loop. Returns ``(message, code, {})`` for the
    first that fails, or None with the facts stored on ``card`` for the record. Blocking."""
    from . import automation, metadata, orchestrator, orchestrator_ledger, prefs, session_input

    cfg = prefs.get_automation_policy("session")
    if not cfg.get("enabled") or cfg.get("autonomy") not in ("suggest", "yolo"):
        return (
            "session assistance is off or observe-only, so Ask cannot propose a message — the "
            "operator can turn it on (Suggest) under Settings → Automation",
            "scope_disabled",
            {},
        )
    if card is None:
        return (
            "that session is not available — archived, excluded, or outside the roots",
            "not_available",
            {},
        )
    if card.get("engine") not in engines.orchestrator_input_engines():
        return ("that agent does not take input from Ask", "not_actuable", {})
    held = None
    with contextlib.suppress(Exception):
        held = missions.all_active_memberships().get(key)
    if held:
        # Phase 2 stops at the mission line (#1472 is reshaping how missions drive sessions).
        return (
            "a mission holds that session — the operator can message it from the mission's "
            "own console",
            "mission_held",
            {},
        )
    try:
        grant = automation.capture_candidates([key], None).get(key)
    except Exception:  # noqa: BLE001 — unreadable ownership authorizes nothing
        grant = None
    if grant is None:
        return ("Ask cannot confirm who owns that session right now", "not_available", {})
    meta = metadata.load().get(key) or metadata.load().get(grant["physical_key"])
    if meta is not None and meta.orchestrator_excluded:
        return (
            "that session is excluded from automation — the operator opted it out",
            "excluded",
            {},
        )
    for a in orchestrator_ledger.live_actions():
        if a.get("session_id") == key:
            return (
                "that session already has a decision waiting — see Needs you",
                "pending",
                {},
            )
    phys = grant["physical_key"]
    if key in turn.busy_keys or phys in turn.busy_keys:
        return ("that session is working right now — ask again when it stops", "working", {})
    if not session_input.is_live(phys):
        return ("that session is not running, so nothing can be typed into it", "not_live", {})
    screen = orchestrator.observed_screen(phys)
    if screen.get("permission") is not None:
        return (
            "that session is showing a permission dialog — only the operator answers those, "
            "from Needs you or the terminal",
            "permission_dialog",
            {},
        )
    card["_nudge"] = {
        "cfg": cfg,
        "grant": grant,
        "precondition": orchestrator.precondition_for(phys),
    }
    return None


async def _propose_nudge(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """Stage a message for one session no mission holds. It is a ledger `answer` action that
    starts `proposed` WHATEVER the tier — only the operator's tap on its card delivers it, through
    the existing approve route and `actuator.deliver`."""
    from . import handoff, orchestrator_ledger

    key = _known_key(turn, args)
    raw = _str_arg(args, "text", ask_store.NUDGE_MAX)
    # THE SAME NORMALIZATION DELIVERY APPLIES (`actuator.render` → `handoff.sanitize_seed`: control
    # bytes stripped, trimmed), applied HERE — so the text the card shows as "verbatim", the text
    # stored, and the text typed are one string, never a display of bytes that will not be sent.
    try:
        text = handoff.sanitize_seed(raw)
    except handoff.HandoffError:
        raise _ToolRefused(
            "refused: nothing typeable is left in that text", "bad_arguments"
        ) from None
    # A credential must never become a stored effect: text the store boundary would change is
    # refused before anything is written anywhere.
    if await asyncio.to_thread(ask_store.guard_text, text, ask_store.NUDGE_MAX) != text:
        raise _ToolRefused(
            "refused: that text contains a secret or looks like a credential — Ask will not "
            "send it; the operator can type it themselves",
            "credential",
        )
    card = next((dict(c) for c in await turn.catalog() if c.get("id") == key), None)
    refusal = await asyncio.to_thread(_nudge_refusal, turn, key, card)
    if refusal is not None:
        raise _ToolRefused(f"refused: {refusal[0]}", refusal[1])
    assert card is not None
    facts = card["_nudge"]
    now = time.time()
    project = card.get("project") or {}
    rec = {
        "id": uuid.uuid4().hex,
        # ALWAYS a proposal: Ask never gets yolo (#1443, out of scope). The tier is recorded, not
        # obeyed — the operator's tap is the only way this is delivered.
        "state": "proposed",
        "ts": now,
        "expires_at": now + int(facts["cfg"]["proposal_ttl_minutes"]) * 60,
        "tier": facts["cfg"]["autonomy"],
        "source": "ask",
        # Which conversation proposed it (#1517): once DELIVERED, that conversation follows the
        # session. Set here, server-side, from the turn; never from a browser.
        "ask_conversation": turn.cid,
        "session_id": key,
        "engine": card.get("engine", ""),
        "title": str(card.get("title") or "")[: pulse_chat.TITLE_MAX],
        "project": str(project.get("name") or "")[: pulse_chat.PROJECT_MAX],
        "project_id": project.get("id") or "",
        "verb": "answer",
        "answer": text,
        "confidence": 1.0,
        "rationale": "Proposed from Ask",
        "evidence": "none",
        "authority": facts["grant"],
        "precondition": facts["precondition"],
    }
    # CARD FIRST, ACTION SECOND. The two stores cannot commit together, so the order decides what
    # a failure between them leaves behind: a card whose action never existed reads "no longer
    # tracked" and offers nothing, while an action whose card never existed would sit approvable
    # in Needs you with no card where it was promised. Only the first is acceptable.
    try:
        await _append(
            turn.cid,
            {
                "type": "proposal",
                "turn_id": turn.turn_id,
                "action_id": rec["id"],
                "kind": "nudge",
                "key": key,
                "title": rec["title"],
                "text": text,
            },
        )
    except Exception:
        raise _ToolRefused(
            "refused: the proposal could not be recorded; nothing was proposed or sent",
            "not_recorded",
        ) from None
    refused_secret = {"v": False}

    def clean_at_write(r: dict) -> bool:
        # THE SECRET CHECK AT THE WRITE: inside the ledger's own lock, against the secrets known
        # NOW — one registered while the eligibility checks ran is caught here, not missed.
        try:
            ok = ask_store.guard_text(r["answer"], ask_store.NUDGE_MAX) == r["answer"]
        except Exception:  # noqa: BLE001 — redaction that cannot run authorizes nothing
            ok = False
        refused_secret["v"] = not ok
        return ok

    def fenced_append() -> list[dict]:
        # The registration fence held across the guard AND the write (`template_secrets`): a
        # secret registered meanwhile lands before the guard runs or after the record exists.
        with template_secrets.registration_fence():
            kept, _dropped = orchestrator_ledger.append_batch_for_free_sessions(
                [rec], record_guard=clean_at_write
            )
        return kept

    try:
        kept = await asyncio.to_thread(fenced_append)
    except Exception:
        # AMBIGUOUS: the record may have been written before the failure (a directory fsync after
        # the file's own). Whether this was proposed is decided by reading the ledger for THIS id,
        # never inferred from the exception — a live action must never be reported as refused.
        try:
            landed = await asyncio.to_thread(orchestrator_ledger.get, rec["id"])
        except Exception:  # noqa: BLE001 — an unreadable ledger answers nothing either way
            landed = None
        if landed is None:
            raise _ToolRefused(
                "refused: the proposal could not be confirmed as recorded; check Needs you "
                "before asking again",
                "uncertain",
            ) from None
        kept = [landed]
    if not kept:
        if refused_secret["v"]:
            raise _ToolRefused(
                "refused: that text contains a secret or looks like a credential — Ask will not "
                "send it; the operator can type it themselves",
                "credential",
            )
        raise _ToolRefused(
            "refused: that session already has a decision waiting — see Needs you", "pending"
        )
    return (
        {
            "proposed": True,
            "note": "The operator now sees a card with this exact text and decides. Nothing has "
            "been sent; never say it was.",
        },
        {"keys": [key], "count": 1},
    )


def _index_arg(args: dict, name: str, rows: list[dict] | None, listed_by: str) -> dict:
    if rows is None:
        raise _ToolRefused(f"refused: call {listed_by} in this turn first", "not_listed")
    v = args.get(name)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(rows):
        raise _ToolRefused(
            f"refused: {name} must be an index {listed_by} returned in this turn", "bad_index"
        )
    return rows[v]


async def _propose_session(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """Stage a session to start: a project and an agent BY INDEX into the lists this turn's own
    tools returned, and a brief. Nothing starts — the operator's tap on its card does, with the
    permission-bypass setting the card shows (`ask_proposals`)."""
    from . import ask_proposals, automation_runner, handoff

    project = _index_arg(args, "project_index", turn.projects, "list_projects")
    if not project.get("startable", True):
        raise _ToolRefused(
            "refused: that project is archived or has no folder, so a session can't start there",
            "not_startable",
        )
    agent = _index_arg(args, "engine_index", turn.agents, "list_agents")
    raw = _str_arg(args, "brief", ask_store.BRIEF_MAX)
    # The normalization delivery applies (control bytes stripped, trimmed), applied HERE: the
    # brief the card shows is the brief typed.
    try:
        brief = handoff.sanitize_seed(raw)
    except handoff.HandoffError:
        raise _ToolRefused(
            "refused: nothing typeable is left in that brief", "bad_arguments"
        ) from None
    if await asyncio.to_thread(ask_store.guard_text, brief, ask_store.BRIEF_MAX) != brief:
        raise _ToolRefused(
            "refused: that brief contains a secret or looks like a credential — Ask will not "
            "start a session with it",
            "credential",
        )
    why = await asyncio.to_thread(ask_proposals.scope_refusal)
    if why:
        raise _ToolRefused(
            f"refused: {why}, so Ask cannot propose a session — the operator can turn it on "
            "(Suggest) under Settings → Automation",
            "scope_disabled",
        )
    ok, why = await asyncio.to_thread(automation_runner.engine_state, agent["id"])
    if not ok:
        raise _ToolRefused(f"refused: {agent['name']} cannot be started from Ask — {why}", "agent")
    ok, why = await asyncio.to_thread(automation_runner.folder_ok, project["cwd"])
    if not ok:
        raise _ToolRefused(f"refused: {why}", "folder")
    pid = uuid.uuid4().hex
    entry = {
        "id": pid,
        "turn_id": turn.turn_id,
        "kind": "session",
        "state": ask_proposals.PENDING,
        "ts": time.time(),
        "engine": agent["id"],
        "agent": agent["name"],
        "project_id": project["id"],
        "project": project["name"],
        "cwd": project["cwd"],
        # Where it resolves NOW, as the card is shown: what a start is compared against.
        "real": os.path.realpath(project["cwd"]),
        "brief": brief,
        "bypass_default": await asyncio.to_thread(ask_proposals.bypass_default),
        "bypass": None,
    }
    # CARD FIRST, ENTRY SECOND: a card whose entry never landed reads "no longer tracked" and
    # offers nothing; an entry is only startable from its card.
    try:
        await _append(
            turn.cid,
            {
                "type": "proposal",
                "turn_id": turn.turn_id,
                "action_id": pid,
                "kind": "session",
                "title": project["name"],
                "agent": agent["name"],
                "text": brief,
            },
        )
    except Exception:
        raise _ToolRefused(
            "refused: the proposal could not be recorded; nothing will start", "not_recorded"
        ) from None
    try:
        await asyncio.to_thread(ask_proposals.add, turn.cid, entry)
    except ask_store.Withheld:
        raise _ToolRefused(
            "refused: that brief contains a secret or looks like a credential — Ask will not "
            "start a session with it",
            "credential",
        ) from None
    except Exception:
        raise _ToolRefused(
            "refused: the proposal could not be recorded; nothing will start", "not_recorded"
        ) from None
    return (
        {
            "proposed": True,
            "note": "The operator now sees a card with this project, agent and brief, chooses "
            "the permission setting and starts it or not. Nothing has started; never say it has.",
        },
        {"count": 1},
    )


# ---- HQ operations (#1558 P1, #1559) -------------------------------------------------------------


def _operator(turn: _Turn):
    if turn.operator is None or turn.app is None:
        raise _ToolRefused(
            "refused: this conversation can't stage changes here — ask from the HQ sidebar",
            "no_operator",
        )
    return turn.operator, turn.app


async def _stage(turn: _Turn, op, target: str, title: str, fields: dict) -> tuple[dict, dict]:
    from . import ask_operations

    operator, app = _operator(turn)
    try:
        staged = await ask_operations.stage(
            cid=turn.cid,
            turn_id=turn.turn_id,
            op=op,
            target=target,
            title=title,
            fields=fields,
            app=app,
            operator=operator,
            append_card=_append,
        )
    except ask_operations.Refused as e:
        raise _ToolRefused(str(e), e.code) from None
    return (
        {
            "proposed": True,
            "action": op.label,
            "target": staged["target"],
            "what_happens": staged["summary"],
            "note": "The operator now sees a card and confirms it or not. Nothing has happened "
            "yet; never say it has. The card shows the outcome.",
        },
        {"count": 1, **({"keys": [target]} if op.target == "session" else {})},
    )


#: Rows `list_sessions` returns at most (the sidebar's own list, a page at a time).
LIST_SESSIONS_MAX = 30
RECAP_MAX = 1_500


async def _list_sessions(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """The sidebar's own list (`GET /api/sessions`, as the operator: same scope, same filters).
    Names and states only — never a folder path. Every key returned may be acted on this turn."""
    import urllib.parse

    from . import asgi_call

    operator, app = _operator(turn)
    params: dict[str, str] = {"limit": str(LIST_SESSIONS_MAX)}
    if args.get("query") is not None:
        params["q"] = _str_arg(args, "query", QUERY_MAX)
    if args.get("project_index") is not None:
        params["project"] = _index_arg(args, "project_index", turn.projects, "list_projects")["id"]
    if args.get("agent_index") is not None:
        params["engine"] = _index_arg(args, "agent_index", turn.agents, "list_agents")["id"]
    mission = args.get("mission_id")
    if mission is not None:
        # A mission id a tool returned THIS turn (the #1443 rule), or "none": never model text.
        if mission != "none" and not (isinstance(mission, str) and mission in turn.mission_ids):
            raise _ToolRefused(
                "refused: mission_id must be one a tool returned in this turn, or 'none'",
                "bad_index",
            )
        params["mission"] = mission
    if args.get("archived") is True:
        params["archived"] = "true"
    if args.get("working") is True:
        params["running"] = "working"
    offset = args.get("offset")
    if offset is not None:
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise _ToolRefused("offset must be a whole number from 0", "bad_arguments")
        params["offset"] = str(offset)
    r = await asgi_call.call(
        app, operator, "GET", "/api/sessions", None, query=urllib.parse.urlencode(params)
    )
    if r.status != 200 or not isinstance(r.body, dict):
        raise _ToolRefused("the session list could not be read just now", "not_available")
    labels = await asyncio.to_thread(ask_settings.agent_labels)
    rows = []
    for row in (r.body.get("sessions") or [])[:LIST_SESSIONS_MAX]:
        key = str(row.get("id") or "")
        try:
            engines.parse_key(key)
        except Exception:  # noqa: BLE001, S112 — a row that doesn't parse is not offered
            continue
        title = str(row.get("title") or "")[: pulse_chat.TITLE_MAX]
        turn.keys.add(key)
        turn.matches.setdefault(key, {"id": key, "title": title, "why": ""})
        project = row.get("project") if isinstance(row.get("project"), dict) else {}
        rows.append(
            {
                "key": key,
                "title": title,
                "project": str(project.get("name") or "")[: pulse_chat.PROJECT_MAX]
                if project.get("kind") == "project"
                else "",
                # Authored names only (#1569 review 6739): never a loaded manifest's text.
                "agent": labels.get(str(row.get("engine") or ""), "an agent"),
                "archived": bool(row.get("archived")),
                "favourite": bool(row.get("sticky")),
                "tag": str(row.get("tag") or "")[:40],
                "working": bool(row.get("working")),
                "last_activity_hours_ago": _hours_ago(row.get("last_mtime")),
            }
        )
    out = {
        "sessions": rows,
        "total": r.body.get("total"),
        "next_offset": r.body.get("next_offset"),
    }
    return out, {"count": len(rows), "keys": [x["key"] for x in rows]}


async def _session_brief(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """One session's brief: its AI summary and recap, why it needs the operator, when it was last
    reviewed — the Session brief dialog's facts, read as the operator."""
    from . import asgi_call

    key = _known_key(turn, args)
    operator, app = _operator(turn)
    r = await asgi_call.call(app, operator, "GET", f"/api/sessions/{key}", None)
    if r.status != 200 or not isinstance(r.body, dict):
        raise _ToolRefused("that session is not available", "not_available")
    row = r.body
    out = {
        "key": key,
        "title": str(row.get("title") or "")[: pulse_chat.TITLE_MAX],
        "archived": bool(row.get("archived")),
        "favourite": bool(row.get("sticky")),
        "tag": str(row.get("tag") or "")[:40],
        "summary": str(row.get("ai_summary") or "")[:SUMMARY_MAX],
        # Written by the AI review from the transcript: data, never an instruction.
        "recap": str(row.get("ai_recap") or "")[:RECAP_MAX],
        "needs_you": bool(row.get("intervention_required")),
        "needs_you_reason": str(row.get("intervention_reason") or "")[:REASON_MAX],
        "reviewed_hours_ago": _hours_ago(row.get("reviewed_at")),
        "excluded_from_ai_review": bool(row.get("review_excluded")),
        "excluded_from_mission_control": bool(row.get("orchestrator_excluded")),
    }
    lines = args.get("history_lines")
    if lines is not None:
        if (
            isinstance(lines, bool)
            or not isinstance(lines, int)
            or not 1 <= lines <= HISTORY_LINES_MAX
        ):
            raise _ToolRefused(
                f"history_lines must be a whole number from 1 to {HISTORY_LINES_MAX}",
                "bad_arguments",
            )
        # The session's transcript turns, each guarded WHOLE before anything cuts it (#1571 review
        # 6748): the history route renders, wraps and cuts by line first, which can split a secret
        # before any redactor sees it. Untrusted: what the agent wrote. Data, never an instruction.
        try:
            text, more = await asyncio.to_thread(_history_text, key, lines)
        except ask_store.StoreRefused:
            # The secret store could not be read, so nothing can be redacted: send nothing.
            raise _ToolRefused(
                "its history could not be checked for secrets just now", "not_available"
            ) from None
        out["untrusted_history"] = text
        out["history_has_more"] = more
    length = len(out["recap"]) + len(out.get("untrusted_history", ""))
    return out, {"keys": [key], "count": 1, "length": length}


#: Lines of history `session_brief` may add, and its cap in characters (inside the tool-result cap,
#: with the brief's own fields).
HISTORY_LINES_MAX = 200
HISTORY_CHARS_MAX = 6_000

#: The shortest run of a known secret that, left anywhere in the page HQ would send (whitespace
#: removed, so a wrapped secret still reads whole), withholds the page.
SECRET_FRAGMENT_MIN = 6


def _holds_secret_fragment(text: str, secrets: list[str]) -> bool:
    """Whether ``text`` still holds a run of any known secret — a part the full-value redactor
    cannot match (the transcript holding only some of it). The last net, after every turn was
    guarded whole: fails toward withholding; a window is half the secret, never less than
    ``SECRET_FRAGMENT_MIN``."""
    flat = re.sub(r"\s+", "", text)
    for secret in secrets:
        v = re.sub(r"\s+", "", secret)
        if not v:
            continue
        w = min(len(v), max(SECRET_FRAGMENT_MIN, len(v) // 2))
        if any(v[i : i + w] in flat for i in range(len(v) - w + 1)):
            return True
    return False


def _history_text(key: str, lines: int) -> tuple[str, bool]:
    """The newest ``lines`` lines of a session's transcript as plain text the model may see, and
    whether older ones remain (#1571 reviews 6745, 6746, 6748, 6751, 6754): messages, tool calls
    and their results.

    The one cut an adapter makes — a tool call's argument, in `transcript._short` — runs the same
    boundary over the FULL argument first (`transcript.PRE_CUT_GUARD`, set for this read only).

    Every turn's COMPLETE text goes through the store's boundary first (`ask_store.guard_text`:
    known secrets redacted; a turn that still looks like a credential withheld whole). Only then
    are lines taken, newest first, and the characters capped — so a cut can split a `[secret]`
    marker, never a secret. A last check withholds the page if a run of a known secret survives
    anyway (one the transcript itself holds in part). Raises `StoreRefused` when redaction is
    unavailable."""
    from . import history, scrollback, template_secrets, transcript

    prov, native = engines.parse_key(key)
    try:
        secrets = template_secrets.redaction_values()
    except template_secrets.RedactionUnavailable as e:
        raise ask_store.StoreRefused(f"{e} — nothing was sent") from None
    token = transcript.PRE_CUT_GUARD.set(
        lambda v: ask_store.guard_text(v, max(len(v), 1), secrets=secrets)
    )
    try:
        turns = history.load_turns(prov.engine_id, native) or []
    finally:
        transcript.PRE_CUT_GUARD.reset(token)
    taken: list[str] = []
    more = False
    for t in reversed(turns):
        guarded = ask_store.guard_text(t.text, max(len(t.text), 1), secrets=secrets)
        # Escapes off AFTER the guard: its redactor already matches a secret an escape splits.
        raw = guarded.encode("utf-8", "replace")
        guarded = scrollback._stripped_tail_text(raw, len(raw) + 1)
        who = "you" if t.role == "user" and t.kind != "context" else t.role
        rows = [f"[{who}] {row}" if n == 0 else row for n, row in enumerate(guarded.split("\n"))]
        room = lines - len(taken)
        if len(rows) > room:
            taken[:0] = rows[-room:] if room > 0 else []
            more = True
            break
        taken[:0] = rows
    else:
        more = bool(getattr(turns, "truncated", False))
    text = "\n".join(taken)[-HISTORY_CHARS_MAX:]
    if _holds_secret_fragment(text, secrets):
        return ask_store.WITHHELD, more
    return text, more


async def _list_folders(turn: _Turn, args: dict) -> tuple[dict, dict]:
    """Discovered launch folders by NAME, and which project owns each. Never a path: a folder is
    chosen by the operator on the card."""
    import os

    from . import asgi_call, projects

    operator, app = _operator(turn)
    r = await asgi_call.call(app, operator, "GET", "/api/folders", None)
    if r.status != 200 or not isinstance(r.body, dict):
        raise _ToolRefused("the folder list could not be read just now", "not_available")
    index = await asyncio.to_thread(projects.load)
    rows = []
    for f in (r.body.get("folders") or [])[:OTHER_PROJECTS_MAX]:
        cwd = str(f.get("cwd") or "")
        owner = projects.owning_project(cwd, index) if cwd else None
        rows.append(
            {
                "name": os.path.basename(cwd.rstrip("/")) or cwd[:1],
                "project": owner.name if owner else "",
            }
        )
    return {"folders": rows}, {"count": len(rows)}


async def _propose_session_change(turn: _Turn, args: dict) -> tuple[dict, dict]:
    from . import ask_operations

    action = args.get("action")
    op = ask_operations.by_action("session", action) if isinstance(action, str) else None
    if op is None:
        raise _ToolRefused(
            f"action must be one of {', '.join(ask_operations.actions('session'))}",
            "bad_arguments",
        )
    key, title = "", ""
    if op.target == "session":
        key = _known_key(turn, args)
        title = str((turn.matches.get(key) or {}).get("title") or "")
    try:
        fields = ask_operations.gate_fields(op, args)
    except ask_operations.Refused as e:
        raise _ToolRefused(f"refused: {e}", e.code) from None
    if op.action == "move":
        raw = args.get("project_index")
        if raw is None:
            fields.update(project_id="", project_name="")
        else:
            row = _index_arg(args, "project_index", turn.projects, "list_projects")
            fields.update(project_id=row["id"], project_name=row["name"])
    return await _stage(turn, op, key, title, fields)


async def _propose_project_change(turn: _Turn, args: dict) -> tuple[dict, dict]:
    from . import ask_operations

    action = args.get("action")
    op = ask_operations.by_action("project", action) if isinstance(action, str) else None
    if op is None:
        raise _ToolRefused(
            f"action must be one of {', '.join(ask_operations.actions('project'))}",
            "bad_arguments",
        )
    pid, title = "", ""
    if op.target == "project":
        row = _index_arg(args, "project_index", turn.projects, "list_projects")
        pid, title = row["id"], row["name"]
    try:
        fields = ask_operations.gate_fields(op, args)
    except ask_operations.Refused as e:
        raise _ToolRefused(f"refused: {e}", e.code) from None
    return await _stage(turn, op, pid, title, fields)


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
            "ask_operator",
            "ends_turn",
            _spec(
                "ask_operator",
                "Ask the operator ONE question when a detail you need is missing or ambiguous, "
                "instead of guessing. Offer up to four short options (each is the exact reply "
                "the operator can pick) and/or allow a free-text answer. Ends your turn.",
                {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": ask_store.OPTIONS_MAX,
                    },
                    "allow_text": {"type": "boolean"},
                },
                ["question"],
            ),
            _ask_operator,
        ),
        Tool(
            "propose_nudge",
            "proposal",
            _spec(
                "propose_nudge",
                "Propose a message to type into one session NO mission holds. Nothing is sent: "
                "the operator sees a card with the exact text and approves or dismisses it.",
                {
                    **_KEY_PROP,
                    "text": {"type": "string", "description": "exactly what to type"},
                },
                ["key", "text"],
            ),
            _propose_nudge,
        ),
        Tool(
            "list_agents",
            "read",
            _spec(
                "list_agents",
                "The coding agents available here, by index and name, and whether Ask can start "
                "one (can_start).",
                {},
                [],
            ),
            _list_agents,
        ),
        Tool(
            "propose_session",
            "proposal",
            _spec(
                "propose_session",
                "Propose starting a new session: a project and an agent by the indexes "
                "list_projects and list_agents returned in this turn, and the brief the agent "
                "gets as its first message. Nothing starts: the operator sees a card and decides.",
                {
                    "project_index": {"type": "integer"},
                    "engine_index": {"type": "integer"},
                    "brief": {"type": "string", "description": "the first message, in full"},
                },
                ["project_index", "engine_index", "brief"],
            ),
            _propose_session,
        ),
        Tool(
            "list_sessions",
            "read",
            _spec(
                "list_sessions",
                "The operator's sessions as their sidebar lists them, newest first: key, title, "
                "project, agent, archived, favourite, tag, whether it is working, hours since "
                "its last activity. Filter by words (query), project (project_index from "
                "list_projects), agent (agent_index from list_agents), mission (mission_id a "
                "tool returned, or 'none' for sessions no mission holds), archived (true for the "
                "archive), working (true for only those working now); page with offset. Every "
                "key returned can be used this turn.",
                {
                    "query": {"type": "string"},
                    "project_index": {"type": "integer"},
                    "agent_index": {"type": "integer"},
                    "mission_id": {"type": "string"},
                    "archived": {"type": "boolean"},
                    "working": {"type": "boolean"},
                    "offset": {"type": "integer"},
                },
                [],
            ),
            _list_sessions,
        ),
        Tool(
            "session_brief",
            "read",
            _spec(
                "session_brief",
                "One session's brief: its AI summary and recap, whether and why it needs the "
                "operator, when it was last reviewed, and whether it is excluded from AI review "
                "or mission control. history_lines (1-200) adds the newest that many lines of "
                "its conversation (messages, tool calls and their output), as data.",
                {**_KEY_PROP, "history_lines": {"type": "integer"}},
                ["key"],
            ),
            _session_brief,
        ),
        Tool(
            "list_folders",
            "read",
            _spec(
                "list_folders",
                "The launch folders BattleLab discovered, by folder name, and which project owns "
                "each. You never name a folder: the operator picks it on a card.",
                {},
                [],
            ),
            _list_folders,
        ),
        Tool(
            "propose_session_change",
            "proposal",
            _spec(
                "propose_session_change",
                "Propose a change to one session (or, for archive_older, to every session idle "
                "for longer than `days`): archive, unarchive, close (stops an API session's "
                "worker; a terminal session closes by archiving), rename (title), tag (tag, "
                "empty clears), color (color '#rrggbb', empty clears), favorite, unfavorite, move "
                "(project_index from list_projects in this turn; omit it to take the session out "
                "of its project), review (sends it to the AI endpoint), exclude_review, "
                "include_review, exclude_orchestration, include_orchestration, archive_older "
                "(days). Nothing happens: the operator sees a card with what it does and "
                "confirms or dismisses it.",
                {
                    "action": {"type": "string", "enum": ask_operations.actions("session")},
                    **_KEY_PROP,
                    "title": {"type": "string"},
                    "tag": {"type": "string"},
                    "color": {"type": "string"},
                    "project_index": {"type": "integer"},
                    "days": {"type": "integer"},
                },
                ["action"],
            ),
            _propose_session_change,
        ),
        Tool(
            "propose_project_change",
            "proposal",
            _spec(
                "propose_project_change",
                "Propose a change to one project, by the index list_projects returned in this "
                "turn: rename (name), color (color '#rrggbb', empty clears), archive (and its "
                "sessions), unarchive, delete (sessions are kept), make_default (new sessions "
                "start there), add_folder, remove_folder, default_folder. Or, with no index: "
                "create (name) or auto_sort (the AI endpoint sorts unassigned sessions into "
                "projects). Folders are never yours to name: the operator picks the folder on "
                "the card. Nothing happens: the operator sees a card and confirms or dismisses "
                "it.",
                {
                    "action": {"type": "string", "enum": ask_operations.actions("project")},
                    "project_index": {"type": "integer"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                },
                ["action"],
            ),
            _propose_project_change,
        ),
        Tool(
            "agent_usage",
            "read",
            _spec(
                "agent_usage",
                "Each agent's usage as the agent itself last reported it (it never asks the "
                "agents anew). source says what a figure is: plan = a real quota, used_pct per "
                "window and resets_in_hours; tokens = a count that means something only against "
                "limit_tokens; manual = the operator's own counter; none = NOT measured, which is "
                "not 0 %. Always say how long ago it was checked (checked_hours_ago). If stale or "
                "probe_failed, say so and point the operator to Settings → Agents → Ask the "
                "agents to refresh.",
                {},
                [],
            ),
            _agent_usage,
        ),
        Tool(
            "fleet_status",
            "read",
            _spec(
                "fleet_status",
                "Counts only: the operator's sessions per agent and state (needs_you, in_flight, "
                "recently_active, idle) and missions per state. complete=false or a null part "
                "means it could not be read just now — say that, never report zeros. To name "
                "sessions, use find_sessions.",
                {},
                [],
            ),
            _fleet_status,
        ),
        Tool(
            "read_settings",
            "read",
            _spec(
                "read_settings",
                "One section of the operator's settings, read-only: usage (alert threshold, "
                "token limits), agents (defaults, which agents start new sessions), automation "
                "(session assistance and mission orchestration), ai_review (on/off, whether an "
                "endpoint is configured), pulse (background scan, session review), appearance. "
                "You cannot change settings: say where in Settings the operator changes them.",
                {"section": {"type": "string", "enum": list(ask_settings.SECTIONS)}},
                ["section"],
            ),
            _read_settings,
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


def _user_content(turn: _Turn) -> str:
    if turn.answering is None:
        return turn.question
    return f"Answering your question \"{turn.answering['question']}\": {turn.question}"


def _asked_records(turn: _Turn) -> list[dict]:
    """The records a turn that ended on a question settles with: the question card, and the
    question as the assistant's text so the next turn's history replays it."""
    q = turn.asked or {}
    return [
        {"type": "question", "turn_id": turn.turn_id, **q},
        _assistant(turn, q.get("question", "")),
        _status(turn, "done"),
    ]


def _persona(turn: _Turn) -> list[dict]:
    """The contact's persona as its own system message, or nothing for ``off`` (#1525). An exact
    registry string: the outbound check holds as it is, and it goes BEFORE the guarded prompt."""
    if turn.contact not in hq_contacts.IDS:
        return []
    return [{"role": "system", "content": prompts.effective(hq_contacts.prompt_id(turn.contact))}]


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
        # The persona first, the guarded prompt LAST of the system messages: its guard is then
        # the final instruction the model reads, whatever the voice says (`prompt-registry.md`).
        *_persona(turn),
        {"role": "system", "content": prompts.effective("ask_orchestrator")},
        # Stored user/assistant text only; the pinned sanitizer gates the role at the sink.
        *pulse_chat.bound_history(history),
        {"role": "user", "content": _user_content(turn)},
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
            with hq_usage.hq_call():  # the footer's HQ token count (#1531)
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
                if turn.asked is not None:
                    # A question ends the turn, and the calls after it in the same reply are
                    # never run: nothing is staged while Ask waits for the operator's detail.
                    break
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
            if turn.asked is not None:
                # `ask_operator` ends the turn: the operator holds the next move.
                return _asked_records(turn)
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
                # #1443 Phase 2: the question this turn ends on, the nudges it proposed, and the
                # question this turn ANSWERS.
                "ask": None,
                "proposals": [],
                "answers": r.get("answers"),
                # #1525: who answered this turn — its own, kept when the operator switches.
                # A turn from before HQ contacts (or with a contact since removed) is plain HQ.
                "contact": hq_contacts.resolve(r.get("contact")),
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
        elif kind == "question":
            t["ask"] = {
                "question_id": r.get("question_id"),
                "question": r.get("question") or "",
                "options": r.get("options") or [],
                "allow_text": bool(r.get("allow_text")),
                "answered": False,
            }
        elif kind == "proposal":
            t["proposals"].append(
                {
                    "action_id": r.get("action_id"),
                    "kind": r.get("kind"),
                    "key": r.get("key"),
                    "title": r.get("title") or "",
                    "agent": r.get("agent") or "",
                    "text": r.get("text") or "",
                    # An HQ operation's card (#1559): the server's own wording.
                    **(
                        {
                            "op": r.get("op") or "",
                            "risk": r.get("risk") or "",
                            "label": r.get("label") or "",
                            "verb": r.get("verb") or "",
                            "needs": r.get("needs") or [],
                        }
                        if r.get("kind") == "operation"
                        else {}
                    ),
                }
            )
    out = [turns[t] for t in order]
    # Only a SETTLED answer answers: a failed or still-running one leaves the question open, so
    # the operator can answer again after a timeout or an interruption.
    answered = {t["answers"] for t in out if t.get("answers") and t.get("status") == "done"}
    for t in out:
        if t["ask"] is not None:
            t["ask"]["answered"] = t["ask"]["question_id"] in answered
    return out


#: Proposal kinds whose live state is the proposal sidecar's (`ask_proposals.views`).
SIDECAR_KINDS = frozenset({"session", "operation"})


def with_live(views: list[dict], cid: str) -> list[dict]:
    """Each proposal card's LIVE state, read from where it lives — the ledger for a nudge, the
    proposal sidecar for a session (`ask_proposals`) — never a copy kept here, so the card can
    only ever say what the deciding path says (`project_for_operator`'s contract: no control the
    backend would refuse). An unreadable store is `unknown`, not `gone`. Blocking."""
    from . import ask_proposals
    from . import orchestrator_ledger as ledger

    cards = [p for t in views for p in t.get("proposals") or []]
    # Session starts and HQ operations (#1559) both live in the proposal sidecar.
    sessions = [p for p in cards if p.get("kind") in SIDECAR_KINDS]
    if sessions:
        status, entries = ask_proposals.views(cid)
        for p in sessions:
            got = entries.get(p["action_id"])
            if got is not None:
                p.update(got)
            else:
                # Unreadable → unknown (it may yet be read); readable but absent → the entry
                # never landed or left with retention: nothing to decide.
                p.update(
                    state="unknown" if status != "ok" else "gone",
                    can_start=False,
                    can_confirm=False,
                    can_dismiss=False,
                    session_key="",
                    reason="",
                    notice="",
                    summary="",
                )
    if not any(p.get("kind") not in SIDECAR_KINDS for p in cards):
        return views
    status, latest = ledger.latest_by_id_checked()
    for t in views:
        for p in t.get("proposals") or []:
            if p.get("kind") in SIDECAR_KINDS:
                continue
            rec = latest.get(p["action_id"]) if status == "ok" else None
            p.update(
                ledger.project_for_operator(rec.get("state") if rec else None, known=status == "ok")
            )
            detail = (rec or {}).get("detail") or (rec or {}).get("reason") or ""
            p["detail"] = str(detail)[: ask_store.DETAIL_MAX]
    return views


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
        view = _view_of(stored, turn.turn_id) or {}
        return (await asyncio.to_thread(with_live, [view], turn.cid))[0] if view else view
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


def _resolve_answer(
    records: list[dict], text: str, answers: str | None, option: int | None
) -> tuple[str, dict | None]:
    """``(text, answering)`` for a message. An answer to an `ask_operator` question names it; an
    OPTION is resolved to the reply text the server stored with the question — the client picks
    an index and can never make an option say something else."""
    if answers is None:
        if option is not None:
            raise TurnError(422, "an option needs the question it answers")
        return text, None
    q = next(
        (r for r in records if r.get("type") == "question" and r.get("question_id") == answers),
        None,
    )
    if q is None:
        raise TurnError(422, "that question is not in this conversation")
    if option is not None:
        options = q.get("options") or []
        if not 0 <= option < len(options):
            raise TurnError(422, "that option is not one the question offered")
        text = options[option]
    elif not q.get("allow_text"):
        raise TurnError(422, "pick one of the question's options")
    if not text:
        raise TurnError(422, "the answer is empty")
    return text, {"question_id": answers, "question": q.get("question") or ""}


async def begin(
    cid: str,
    turn_id: str,
    text: str,
    *,
    working_keys: set[str],
    busy_keys: set[str] | None = None,
    answers: str | None = None,
    option: int | None = None,
    contact: str = hq_contacts.OFF,
    operator=None,
    app=None,
) -> tuple[asyncio.Task | None, dict | None]:
    """Start a turn, or find the one this ``turn_id`` already names.

    Returns ``(task, None)`` for a running turn (new, or the same id still running), or
    ``(None, view)`` for a turn that already settled: a repeat never runs the model again. A
    watcher subscribes to progress itself (:func:`listen`), only once it is actually streaming.
    Raises :class:`TurnError`.

    Every decision that admits a NEW turn — the id is unused, nothing is pending, the conversation
    has room — is taken again after the cross-process turn lock is held, on a fresh read: the
    first look is only a fast path, and another process may have run and settled this very id in
    between.

    ``contact`` is who answers (#1525): resolved here to a known id or ``off`` and stored on the
    turn's own record."""
    contact = hq_contacts.resolve(contact)
    answering = None
    if answers is not None or option is not None:
        # A question record never changes once written, so resolving outside the lock is exact.
        text, answering = _resolve_answer(
            await asyncio.to_thread(ask_store.read, cid), text, answers, option
        )
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
            if answering is not None and any(
                t.get("answers") == answering["question_id"] and t.get("status") == "done"
                for t in turn_views(records)
            ):
                # One SETTLED answer per question, decided under the turn lock: a stale tab cannot
                # answer it again. A failed answer does not count, so a retry after it is admitted.
                raise TurnError(409, "that question was already answered")
            if len(turn_views(records)) >= ask_store.MAX_TURNS:
                raise TurnError(409, "this conversation is full — start a new one")
            user: dict = {
                "type": "user",
                "turn_id": turn_id,
                "text": text,
                "qd": qd,
                "contact": contact,
            }
            if answering is not None:
                user["answers"] = answering["question_id"]
            await _append(cid, user, {"type": "status", "turn_id": turn_id, "status": "pending"})
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(lock_fd)
            raise
        turn = _Turn(
            cid=cid,
            turn_id=turn_id,
            question=text,
            working_keys=working_keys,
            busy_keys=set(busy_keys or ()),
            answering=answering,
            contact=contact,
            progress=lambda ev: _broadcast(cid, ev),
            operator=operator,
            app=app,
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
