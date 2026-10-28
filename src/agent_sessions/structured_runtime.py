"""Shared structured-client admission and operations (#1275).

This facade is transport-neutral. Reviewed implementations, selected by manifest shape, own
execution; console launch/seed capabilities never count as structured support. The first adapter
delegates to the existing chat runtime, including its durable store, tools and approval fences.
Native adapters delegate through the same admission and observation boundary.
Snapshots preserve adapter-recorded reply times; absent evidence remains unknown.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from types import MappingProxyType

from . import (
    chat_config,
    chat_runtime,
    chat_store,
    engines,
    native_attachments,
    native_protocol,
    native_runtime,
)
from .engine_errors import EngineError
from .plugins import api_source, kinds
from .structured_types import ExecutionGuard, normalize_context

SNAPSHOT_TURNS = 50
SNAPSHOT_TEXT = 20_000
SNAPSHOT_TEXT_BUDGET = 200_000


class StructuredError(Exception):
    """A safe refusal at the common client boundary."""

    def __init__(self, status: int, detail: str, reason: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason  # closed (#1644): `native_ipc.SEND_NOW_REFUSALS`


@dataclass(frozen=True)
class ClientDescriptor:
    engine_id: str
    runtime: str
    adapter: str | None
    generation: int
    operations: tuple[str, ...]
    ready: bool
    reason: str | None
    authentication: str
    # Deliberately false until the complete mission observation/stop/authority cutover lands.
    mission_ready: bool = False
    # The client's protocol takes pictures in a turn (`kinds.API_IMAGE_INPUT`, #1332 Phase 3).
    images: bool = False
    files: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class _ChatAdapter:
    authentication = "configured_endpoint"

    @staticmethod
    def operations(prov) -> tuple[str, ...]:
        m = prov.manifest
        return tuple(
            name
            for name, enabled in (
                ("create", m.can("new")),
                ("snapshot", True),
                ("submit", m.can("new") or m.can("resume")),
                ("decide", True),
            )
            if enabled
        )

    @staticmethod
    def ready(prov) -> tuple[bool, str | None]:
        if not chat_config.is_configured(prov.engine_id):
            return False, "configure this agent's endpoint in Settings → Agents"
        return True, None

    async def create(
        self, *args, bypass=False, effort=None, wait_ready=True, specialist=None, **kwargs
    ):
        # A chat conversation launches no worker, so it has no readiness to wait for (#1505).
        if specialist is not None:
            raise StructuredError(422, "this agent can't be started from a specialist")
        if bypass:
            raise StructuredError(422, "this agent has no permission prompts to skip")
        if effort is not None:
            raise StructuredError(422, "this agent takes no reasoning effort")
        return await _call(chat_runtime.new_session, *args, **kwargs)

    async def snapshot(self, *args, **kwargs):
        return await _call(chat_runtime.get_session, *args, **kwargs)

    async def submit(self, *args, **kwargs):
        return await _call(chat_runtime.send, *args, **kwargs)

    async def decide(self, *args, **kwargs):
        return await _call(chat_runtime.decide, *args, **kwargs)


class _NativeAdapter:
    """Contained native protocol workers (#1278). Vendor login stays the vendor's."""

    authentication = "vendor_native"

    @staticmethod
    def operations(prov) -> tuple[str, ...]:
        operations = (
            "create",
            "start",
            "snapshot",
            "submit",
            "decide",
            "events",
            "history",
            "interrupt",
            "send_now",
            "withdraw",
            "stop",
            "probe",
        )
        return operations + (
            ("enable_bypass",) if prov.manifest.api.kind in kinds.API_BYPASS_KINDS else ()
        )

    @staticmethod
    def ready(prov) -> tuple[bool, str | None]:
        return native_runtime.readiness(prov)

    async def create(
        self,
        engine,
        cwd,
        *,
        session_id,
        model=None,
        effort=None,
        bypass=False,
        execution_admission=None,
        wait_ready=True,
        specialist=None,
    ):
        return await _native(
            native_runtime.create,
            engine,
            cwd,
            session_id=session_id,
            model=model,
            effort=effort,
            bypass=bypass,
            execution_admission=execution_admission,
            wait_ready=wait_ready,
            specialist=specialist,
        )

    async def snapshot(self, engine, native):
        return await _native(native_runtime.snapshot, engine, native)

    async def submit(self, engine, native, tid, text, **kwargs):
        return await _native(native_runtime.submit, engine, native, tid, text, **kwargs)

    async def decide(self, engine, native, turn_id, request_id, decision, user, **kwargs):
        return await _native(
            native_runtime.decide, engine, native, turn_id, request_id, decision, user, **kwargs
        )

    async def events(self, engine, native, after, limit):
        return await _native(native_runtime.events, engine, native, after, limit)

    async def events_stamp(self, engine, native):
        return await _native(native_runtime.journal_stamp, engine, native)

    async def history(self, engine, native, before, limit):
        return await _native(native_runtime.history, engine, native, before, limit)

    async def start(self, engine, native, *, wait_ready=True):
        return await _native(native_runtime.start, engine, native, wait_ready=wait_ready)

    async def interrupt(self, engine, native, **kwargs):
        return await _native(native_runtime.interrupt, engine, native, **kwargs)

    async def send_now(self, engine, native, **kwargs):
        return await _native(native_runtime.send_now, engine, native, **kwargs)

    async def withdraw(self, engine, native, **kwargs):
        return await _native(native_runtime.withdraw, engine, native, **kwargs)

    async def stop(self, engine, native):
        return await _native(native_runtime.stop, engine, native)

    async def enable_bypass(self, engine, native, **kwargs):
        return await _native(native_runtime.enable_bypass, engine, native, **kwargs)

    async def probe(self, engine, native):
        return await _native(native_runtime.probe, engine, native)


async def _native(fn, *args, **kwargs):
    from . import native_journal, native_ownership, native_state
    from .plugins import storage

    try:
        return await fn(*args, **kwargs)
    except native_runtime.NativeError as exc:
        raise StructuredError(exc.status, exc.detail, exc.reason) from None
    except native_journal.JournalError as exc:
        raise StructuredError(
            503, f"the conversation journal is unavailable: {exc.detail}"
        ) from None
    except (
        native_state.StateError,
        native_ownership.OwnershipError,
        storage.StateError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        # Lifecycle locks, private state, the ownership ledger or systemd were unavailable:
        # a retryable refusal, never a 500 (and never a guess about what happened).
        raise StructuredError(503, f"the native runtime is unavailable: {exc}") from None


# A manifest selects reviewed behavior; it cannot register code or invent a supported operation.
_ADAPTERS = MappingProxyType(
    {
        ("chat", "openai-chat"): _ChatAdapter(),
        ("api", "codex-app-server"): _NativeAdapter(),
        ("api", "claude-stream-json"): _NativeAdapter(),
        ("api", "opencode-acp"): _NativeAdapter(),
        ("api", "copilot-acp"): _NativeAdapter(),
    }
)


def _kind(manifest) -> str | None:
    if manifest.runtime == "chat" and manifest.endpoint is not None:
        return manifest.endpoint.kind
    if manifest.runtime == "api" and manifest.api is not None:
        return manifest.api.kind
    return None


def describe(engine_id: str, *, check_ready: bool = True) -> ClientDescriptor:
    """Current capability/readiness data, never an authorization grant or a network probe.

    ``check_ready=False`` answers only which operations are implemented: readiness can run
    local probes (a vendor CLI's ``--version``), which every facade call must not repeat.
    """
    with engines.registry.snapshot_scope(fresh=True) as roster:
        prov = roster.by_id.get(engine_id)
        if prov is None:
            return ClientDescriptor(
                engine_id,
                "unknown",
                None,
                roster.generation,
                (),
                False,
                "unknown, disabled or removed agent",
                "unknown",
            )
        m = prov.manifest
        kind = _kind(m)
        adapter = _ADAPTERS.get((m.runtime, kind))
        reason = None
        if m.runtime == "api":
            try:
                api_source.resolve(prov, roster=roster)
            except api_source.SourceError as exc:
                reason = str(exc)
        if adapter is None:
            reason = reason or (
                "console clients do not support structured execution"
                if m.runtime == "pty"
                else "this native API adapter is not implemented in this build"
            )
            return ClientDescriptor(
                engine_id, m.runtime, kind, roster.generation, (), False, reason, "unknown"
            )
        if not engines.registry.admits(prov):
            ready, reason = False, "the agent changed or was removed"
        elif not check_ready:
            ready, reason = False, "readiness not checked"
        else:
            ready, reason = adapter.ready(prov)
        return ClientDescriptor(
            engine_id,
            m.runtime,
            kind,
            roster.generation,
            adapter.operations(prov),
            ready,
            reason,
            adapter.authentication,
            images=m.runtime == "api" and kinds.API_IMAGE_INPUT.get(kind or "", False),
            files=m.runtime == "api",
        )


def unavailable_reason(prov) -> str | None:
    """Why this already-resolved native API provider cannot start a session, or None (#1311).

    The roster's start answer for runtime ``api`` — the same adapter readiness ``describe``
    reports, without taking a second roster snapshot. Not an authorization: create re-checks."""
    m = prov.manifest
    adapter = _ADAPTERS.get((m.runtime, _kind(m)))
    if m.runtime != "api" or adapter is None:
        return "this native API adapter is not implemented in this build"
    ready, reason = adapter.ready(prov)
    return None if ready else (reason or "the client is not ready")


def require(engine_id: str, operation: str) -> ClientDescriptor:
    """Require implemented support; the adapter checks live readiness after replay lookup."""
    descriptor = describe(engine_id, check_ready=False)
    if operation not in descriptor.operations:
        reason = None if descriptor.reason == "readiness not checked" else descriptor.reason
        raise StructuredError(409, reason or f"{operation} is not supported")
    return descriptor


def _adapter(engine_id: str, operation: str):
    descriptor = require(engine_id, operation)
    return _ADAPTERS[(descriptor.runtime, descriptor.adapter)]


def _key(session_key: str) -> tuple[str, str]:
    try:
        prov, native = engines.parse_key(session_key)
    except EngineError:
        raise StructuredError(404, "no such structured session") from None
    require(prov.engine_id, "snapshot")
    return prov.engine_id, native


def _operation_id(value: object) -> str:
    if not chat_store.valid_turn_id(value):
        raise StructuredError(422, "operation_id must be a UUID")
    return value


async def _call(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except chat_runtime.ChatError as exc:
        raise StructuredError(exc.status, exc.detail) from None


def _outcome(turn: dict) -> str:
    status = turn.get("status")
    if status == "done":
        return "completed"
    if status == "pending":
        return "running"
    if status == "queued":
        return "queued"
    if status in {"delivering", "delivered"}:
        return status
    if status == "awaiting_approval":
        return "awaiting_approval"
    if status == "failed":
        code = turn.get("code")
        return code if code in ("interrupted", "uncertain") else "failed"
    return "unavailable"


def _reply(turn: dict, text_budget: list[int] | None) -> tuple[str | None, bool, int, dict]:
    """The part of a turn's kept reply this snapshot sends (#1643), and how it relates to what was
    left out. Returns (text, head_cut, start, fields).

    * It all fits: all of it.
    * More than one message (any start after the first character) and over the cap (the
      per-reply 20,000 or what the shared budget left): the NEWEST WHOLE messages that fit — a
      long autonomous turn shows its latest progress, not its first. If even the newest message
      alone does not fit, that message's tail.
    * One message over the cap: its head, as always (`head_cut`, shown with "…").

    ``fields``: `reply_breaks` (message starts in the sent text; absent when the projection
    recorded none, so a client folds nothing), `first_message_partial` (the sent text begins
    mid-message: never presented as a whole one) and `reply_omitted` ({messages, chars} left out
    before it — by the kept window or by this slice — or None)."""
    value = turn.get("reply")
    if not isinstance(value, str):
        return None, False, 0, {}
    cap = SNAPSHOT_TEXT if text_budget is None else min(SNAPSHOT_TEXT, max(0, text_budget[0]))
    known = isinstance(turn.get("reply_breaks"), list)
    breaks = [
        b for b in turn.get("reply_breaks") or [] if isinstance(b, int) and 0 <= b <= len(value)
    ]
    if len(value) <= cap:
        start, text, head_cut = 0, value, False
    elif cap <= 0:
        # The shared budget is spent: none of it is sent, however many messages it holds — all of
        # it counts as not loaded (Hermes on #1651), never a reply that silently vanished.
        start, text, head_cut = len(value), "", False
    elif any(b > 0 for b in breaks):
        # More than one message: some message starts after the text does (one retained start is
        # enough — the kept window may have cut into the message before it).
        fits = [b for b in breaks if len(value) - b <= cap]
        start = fits[0] if fits else len(value) - cap
        text, head_cut = value[start:], False
    else:
        start, text, head_cut = 0, value[:cap], True
    if text_budget is not None:
        text_budget[0] -= len(text)
    if not known:
        return text, head_cut, start, {}
    # The sent text always starts a part at 0; whether that is a message's own start is the flag.
    shown = ([0] if text else []) + [b - start for b in breaks if start < b < start + len(text)]
    partial = bool(text) and start not in breaks
    dropped = int(turn.get("reply_messages_cut") or 0) + sum(1 for b in breaks if b < start)
    omitted = {
        # Messages missing ENTIRELY: one whose tail is shown is not counted as not loaded.
        "messages": max(0, dropped - (1 if partial else 0)),
        "chars": int(turn.get("reply_chars_cut") or 0) + start,
    }
    return (
        text,
        head_cut,
        start,
        {
            "reply_breaks": shown,
            "first_message_partial": partial,
            "reply_omitted": omitted if omitted["messages"] or omitted["chars"] else None,
        },
    )


def _turn(turn: dict, text_budget: list[int] | None = None) -> dict:
    def bounded(value):
        if not isinstance(value, str):
            return None, False
        cap = SNAPSHOT_TEXT if text_budget is None else min(SNAPSHOT_TEXT, text_budget[0])
        text = value[:cap]
        if text_budget is not None:
            text_budget[0] -= len(text)
        return text, len(value) > len(text)

    if turn.get("kind") == "agent":
        return _agent_turn(turn, text_budget)
    text, text_cut = bounded(turn.get("text"))
    reply, reply_cut, start, shape = _reply(turn, text_budget)
    tools, tools_cut = _tools(turn, reply, start)
    return {
        "kind": "operator",
        "turn_id": turn["turn_id"],
        "operation_id": turn["turn_id"],
        "state": _outcome(turn),
        "text": text,
        "ts": turn.get("ts"),
        "reply": reply,
        "reply_ts": turn.get("reply_ts"),
        "text_truncated": text_cut,
        # Images use read-back thumbnails; files expose only an inert display name (#1396).
        "attachments": [native_attachments.public(a) for a in turn.get("attachments") or ()],
        "reply_truncated": reply_cut or bool(turn.get("truncated")),
        **shape,
        "reason": turn.get("reason"),
        # A queued message proved never sent, and why (#1538): a closed code the pane turns into
        # fixed text and offers Send again on. Absent on every other turn.
        **({"not_sent": dict(turn["not_sent"])} if turn.get("not_sent") else {}),
        **({"delivery": turn["delivery"]} if turn.get("delivery") else {}),
        **({"delivery_turn_id": turn["delivery_turn_id"]} if turn.get("delivery_turn_id") else {}),
        **({"delivery_reason": turn["delivery_reason"]} if turn.get("delivery_reason") else {}),
        "context": dict(turn.get("context") or {}),
        "requires_authority": bool(turn.get("execution_binding")),
        "tools": tools,
        "tools_truncated": tools_cut,
        # Live turns only, which never spend the shared text budget (a spent one sends "").
        **(_stream(turn, reply_cut) if text_budget is None else {}),
        "usage": turn.get("usage"),
        **(
            {"questions": turn["questions"], "question_id": turn["question_id"]}
            if turn.get("questions")
            else {}
        ),
        **_oom(turn),
    }


def _oom(turn: dict) -> dict:
    """OOM notices (#1732): processes killed during this turn (`oom_kills`), and session-level
    ones between this turn and the next (`oom_kills_after`). Counts only; absent when none."""
    return {
        key: turn[key]
        for key in ("oom_kills", "oom_kills_after")
        if isinstance(turn.get(key), int) and turn[key] > 0
    }


def _stream(turn: dict, head_cut: bool) -> dict:
    """``stream`` (#1727): this running turn's reply is its streamed text so far, sent whole or as
    its newest part, so the pane may append the turn's next partial text events after the
    snapshot's cursor to it. ``item`` is the message that text continues (null when the client
    names none). Absent otherwise: a settled reply, a reply cut at its END, any other state."""
    stream = turn.get("stream")
    if not isinstance(stream, dict) or _outcome(turn) != "running" or head_cut:
        return {}
    item = stream.get("item")
    return {"stream": {"item": item if isinstance(item, str) else None}}


def _tools(turn: dict, reply: str | None, start: int = 0) -> tuple[list[dict], bool]:
    """The newest TOOLS_SHOWN calls (the ones the projection classified), placed in the sent text.
    A call made before the sent part began (#1643: the newest-whole-messages slice) belongs to
    history this view does not hold: left out, never clamped onto the first shown message. A
    call placed past the part shown sits at its end. Returns (calls, whether any were left out)."""
    calls = [
        {**t, "at": min(t["at"] - start, len(reply or ""))} if isinstance(t.get("at"), int) else t
        for t in list(turn.get("tools") or [])
        if not (start and isinstance(t.get("at"), int) and t["at"] < start)
    ]
    shown = native_runtime.TOOLS_SHOWN
    return calls[-shown:], len(calls) > shown or len(calls) < len(turn.get("tools") or [])


def _agent_turn(turn: dict, text_budget: list[int] | None) -> dict:
    """A turn the agent ran for itself (#1438): its own key set, never an operator submission's.
    No `operation_id`, `text`, `attachments`, `delivery`, `context` or `requires_authority` —
    absent, not null — so nothing can mistake it for a message someone sent."""
    reply, reply_cut, start, shape = _reply(turn, text_budget)
    tools, tools_cut = _tools(turn, reply, start)
    return {
        "kind": "agent",
        "turn_id": turn["turn_id"],
        "state": _outcome(turn),
        "ts": turn.get("ts"),
        "reply": reply,
        "reply_ts": turn.get("reply_ts"),
        "reply_truncated": reply_cut,
        **shape,
        **({"reason": turn["reason"]} if turn.get("reason") else {}),
        "tools": tools,
        "tools_truncated": tools_cut,
        **(
            {"questions": turn["questions"], "question_id": turn["question_id"]}
            if turn.get("questions")
            else {}
        ),
        **_oom(turn),
    }


def _snapshot(session_key: str, raw: dict) -> dict:
    all_turns = raw["turns"]
    budget = [SNAPSHOT_TEXT_BUDGET]
    # The newest turns, plus any older operator turn that is still live (queued or running behind
    # the agent's own turns, Hermes on #1439): history is cut, live work never.
    live = {"queued", "running", "awaiting_approval", "delivering"}
    window = all_turns[-SNAPSHOT_TURNS:]
    older = all_turns[:-SNAPSHOT_TURNS] if len(all_turns) > SNAPSHOT_TURNS else []
    kept = [t for t in older if t.get("kind") != "agent" and _outcome(t) in live]
    shown = [*kept, *window]
    # Live operator input is never charged to the shared budget: each waiting or running message
    # gets its own per-field cap (SNAPSHOT_TEXT), so what is pending always reads in full however
    # many such messages there are or how much history follows (Hermes on #1439). The journal
    # bounds that set (MAX_QUEUED + the one running). History spends the shared budget, newest
    # first. Return chronological order.
    first = [i for i, t in enumerate(shown) if t.get("kind") != "agent" and _outcome(t) in live]
    projected = {i: _turn(shown[i]) for i in first}
    for i in reversed(range(len(shown))):
        if i not in projected:
            projected[i] = _turn(shown[i], budget)
    turns = [projected[i] for i in range(len(shown))]
    requests = list(raw.get("pending_requests") or [])
    for turn in all_turns if "pending_requests" not in raw else ():
        if turn["status"] != "awaiting_approval":
            continue
        for proposal in turn.get("proposals", []):
            if proposal.get("status") != "awaiting_approval":
                continue
            requests.append(
                {
                    "request_id": proposal["id"],
                    "turn_id": turn["turn_id"],
                    "kind": "file_edit",
                    "path": proposal.get("path"),
                    "choices": ["approve", "reject"] if proposal.get("can_approve") else ["reject"],
                    "operator_only": True,
                    "context": dict(turn.get("context") or {}),
                }
            )
    state = "idle"
    # The session's state is the operator's conversation; the agent's own turns (#1438) report
    # through `native.background_active`, as before they were shown.
    operator = [t for t in all_turns if t.get("kind") != "agent"]
    active = next((t for t in operator if t["turn_id"] == raw.get("in_flight")), None)
    if active is not None:
        state = _outcome(active)
    elif operator and _outcome(operator[-1]) != "completed":
        state = _outcome(operator[-1])
    return {
        "session_key": session_key,
        "revision": raw["revision"],
        "event_cursor": raw["revision"],
        "cwd": raw["cwd"],
        "state": state,
        "active_turn": raw.get("in_flight"),
        "model_requested": raw.get("model"),
        # A requested level (#1407), like the model: configuration, never an observation.
        "effort_requested": raw.get("effort"),
        # Effective mode includes an explicit existing-session change (#1388).
        "bypass": bool(raw.get("bypass")),
        "can_enable_bypass": bool(raw.get("can_enable_bypass")),
        "permission_change": dict(raw.get("permission_change") or {}),
        # Two-phase skip start (#1339): waiting for `start`, or expired without ever running.
        "pending_start": bool(raw.get("pending_start")),
        **(
            {"start_expires_at": raw["start_expires_at"]}
            if raw.get("start_expires_at") and raw.get("pending_start")
            else {}
        ),
        **({"start_incomplete": True} if raw.get("start_incomplete") else {}),
        **({"start_expired": True} if raw.get("start_expired") else {}),
        # Discarded before it ever started (#1647): it takes no message, so the composer is off.
        **({"start_discarded": True} if raw.get("start_discarded") else {}),
        # Starting vs failed (#1505): a launched worker that is not ready yet, or a start that
        # failed, with its bounded reason. Neither offers or implies anything on its own.
        **({"start_in_flight": True} if raw.get("start_in_flight") else {}),
        **({"start_failed": True} if raw.get("start_failed") else {}),
        **(
            {"start_failed_reason": raw["start_failed_reason"]}
            if raw.get("start_failed") and raw.get("start_failed_reason")
            else {}
        ),
        # The generation-scoped launch view (#1505): {gen, state, reason, since} or absent.
        **({"launch": dict(raw["launch"])} if raw.get("launch") else {}),
        # Configuration is a request, never proof of the model the endpoint actually executed.
        "model_effective": raw.get("model_effective"),
        # What the client reports it runs (#1511) — the model and level an "agent default" session
        # actually got. A report, named apart from the request and the per-turn observation.
        "model_configured": raw.get("model_configured"),
        "effort_configured": raw.get("effort_configured"),
        "turns": turns,
        # Session-level OOM notices before the first turn (#1732): only while that turn is shown.
        **(
            {"oom_kills_start": raw["oom_kills_start"]}
            if raw.get("oom_kills_start") and len(turns) == len(all_turns)
            else {}
        ),
        "omitted_turns": max(0, raw.get("omitted_turns", 0)) + max(0, len(all_turns) - len(turns)),
        # Where scrolling up continues (#1504): the oldest turn of the window, None at the first.
        "history_before": raw.get("history_before"),
        "pending_requests": requests,
        **({"native": dict(raw["native"])} if "native" in raw else {}),
    }


async def snapshot(session_key: str) -> dict:
    """The bounded view. ``read_only`` is None while the client can take new work, else why not:
    a retiring native client (its source disabled or removed) still shows its history (#1311).

    The client lookups take a fresh roster snapshot (state files, the roster lock): off the event
    loop (#1505), like every other blocking read on the create/start path."""
    engine, native, adapter, read_only = await asyncio.to_thread(
        _observation_target, session_key, "snapshot"
    )
    raw = await adapter.snapshot(engine, native)
    images = (await asyncio.to_thread(partial(describe, engine, check_ready=False))).images
    return {
        **_snapshot(f"{engine}:{native}", raw),
        "read_only": read_only,
        # Whether the composer offers pictures (#1332 Phase 3); submit re-checks it.
        "images": images,
        "files": bool(raw.get("native", {}).get("files")),
    }


async def create_session(
    engine: str,
    cwd: str,
    *,
    operation_id: str,
    model: str | None = None,
    effort: str | None = None,
    bypass: bool = False,
    execution_admission: ExecutionGuard | None = None,
    wait_ready: bool = True,
    specialist: tuple[str, int] | None = None,
) -> dict:
    """``wait_ready`` (#1505): True — every internal caller — returns once the session is ready,
    as it always has. Only the HTTP route passes False: it returns as soon as the session is
    recorded and its worker launched, and the snapshot's ``launch`` view reports readiness."""
    adapter = await asyncio.to_thread(_adapter, engine, "create")  # a fresh roster read
    # `effort` (#1407) is sent only when chosen, so a create without one is the call it always was.
    extra = {"effort": effort} if effort is not None else {}
    if specialist is not None:  # #1693 S4b: sent only when chosen, like `effort`
        extra["specialist"] = specialist
    sid = await adapter.create(
        engine,
        cwd,
        session_id=_operation_id(operation_id),
        model=model,
        **extra,
        bypass=bypass,
        execution_admission=execution_admission,
        wait_ready=wait_ready,
    )
    return await snapshot(f"{engine}:{sid}")


async def submit_turn(
    session_key: str,
    *,
    operation_id: str,
    text: str,
    expected_revision: int | None = None,
    context: dict | None = None,
    attachments: list[str] | None = None,
    execution_admission: ExecutionGuard | None = None,
    reply_to: str | None = None,
) -> dict:
    """``attachments``: upload names (#1332 Phase 3), admitted here before anything durable.
    ``reply_to``: the async question this message answers (#1463), native clients only."""
    engine, native = _key(session_key)
    adapter = _adapter(engine, "submit")
    try:
        context = normalize_context(context)
    except ValueError as exc:
        raise StructuredError(422, str(exc)) from None
    tid = _operation_id(operation_id)
    uploaded = {}
    if attachments:
        if not isinstance(adapter, _NativeAdapter):
            raise StructuredError(422, "this client takes no attachments")
        try:
            uploaded["attachments"] = await asyncio.to_thread(
                native_attachments.admit,
                attachments,
                images=describe(engine, check_ready=False).images,
            )
        except native_attachments.AttachmentError as exc:
            raise StructuredError(422, str(exc)) from None
    if reply_to is not None:
        if not isinstance(adapter, _NativeAdapter):
            raise StructuredError(422, "this client asks no questions to answer")
        if not isinstance(reply_to, str):
            raise StructuredError(422, "reply_to must be a question id")
        uploaded["reply_to"] = reply_to
    await adapter.submit(
        engine,
        native,
        tid,
        text,
        expected_revision=expected_revision,
        context=context,
        **uploaded,
        execution_admission=execution_admission,
        idempotent=True,
    )
    raw = await adapter.snapshot(engine, native)
    turn = next(t for t in raw["turns"] if t["turn_id"] == tid and t.get("kind") != "agent")
    return {"session_key": f"{engine}:{native}", "revision": raw["revision"], **_turn(turn)}


async def decide(
    session_key: str,
    *,
    turn_id: str,
    request_id: str,
    decision_id: str,
    decision: str,
    user: str,
    expected_revision: int | None = None,
    grant: str | None = None,
    answers: list | None = None,
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    engine, native = _key(session_key)
    adapter = _adapter(engine, "decide")
    if answers is not None and not isinstance(adapter, _NativeAdapter):
        raise StructuredError(422, "this client's requests take no answer")
    result = await adapter.decide(
        engine,
        native,
        turn_id,
        request_id,
        decision,
        user,
        decision_id=_operation_id(decision_id),
        expected_revision=expected_revision,
        # A standing grant (#1339) only ever travels with an `always` decision.
        **({"grant": grant} if grant is not None else {}),
        # An agent question's answer (#1463) only ever travels with an `answer` decision.
        **({"answers": answers} if answers is not None else {}),
        execution_admission=execution_admission,
    )
    return {"session_key": f"{engine}:{native}", "decision_id": decision_id, **result}


async def events(session_key: str, *, after: int, limit: int = 100) -> dict:
    """Durable journal observations after a cursor; the cursor counts records, not time."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise StructuredError(422, "after must be a cursor and limit 1-100")
    engine, native, adapter, _ = _observation_target(session_key, "events")
    page = await adapter.events(engine, native, after, limit)
    return {"session_key": f"{engine}:{native}", **page}


#: The longest an operator's events read waits for the journal to move (#1727). Home Free's app
#: proxy buffers each response through a default HTTP client (≈5 s read timeout), so a quiet wait
#: must answer well inside that. A larger request is capped here, never trusted.
EVENTS_WAIT_MAX = 3.0
#: How often a waiting read re-checks the journal's size and times (a `stat`, no IPC).
EVENTS_WAIT_TICK = 0.1
#: Live waiters, per session and in all. Over either cap a read answers at once, exactly as a read
#: without `wait` does; the client simply keeps its ordinary poll.
EVENTS_WAITERS_PER_SESSION = 4
EVENTS_WAITERS_TOTAL = 64
_EVENTS_WAITERS: dict[str, int] = {}
# One page read at a time per session among waiters: those woken by the same append then fold it
# once, and the rest reuse that fold through the journal's unchanged-file shortcut.
_EVENTS_READS: dict[str, asyncio.Lock] = {}


def events_waiters(session_key: str | None = None) -> int:
    """Live waiting events reads: of one session, or in all."""
    if session_key is None:
        return sum(_EVENTS_WAITERS.values())
    return _EVENTS_WAITERS.get(session_key, 0)


def _moved(page: dict, after: int) -> bool:
    # An empty page can still advance its cursor (private audit records are skipped).
    return bool(page["events"]) or page["next_cursor"] != after


async def wait_events(
    session_key: str,
    *,
    after: int,
    limit: int = 100,
    wait: float = 0,
    disconnected: Callable[[], bool] | None = None,
) -> dict:
    """``events``, held up to ``wait`` seconds (capped at ``EVENTS_WAIT_MAX``) while nothing lies
    past ``after`` (#1727). Any journal append past it wakes the read, which answers with that
    page; the same validation, cursor and reset (``409``) answers as ``events``. A read over the
    waiter caps, or one whose client went away, answers with the page it holds."""
    if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
        raise StructuredError(422, "wait must be a number of seconds")
    wait = min(float(wait), EVENTS_WAIT_MAX)
    if wait <= 0:
        return await events(session_key, after=after, limit=limit)
    # The wait counts from the request, not from after the stamp and the first read (review of
    # #1747): a slow first read (a cold fold) or a queued one must not stretch a held read past
    # its cap and into Home Free's ≈5 s margin. At most one read can finish past it.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    engine, native, adapter, _ = _observation_target(session_key, "events")
    key = f"{engine}:{native}"
    stamp_of = getattr(adapter, "events_stamp", None)
    if (
        stamp_of is None
        or _EVENTS_WAITERS.get(key, 0) >= EVENTS_WAITERS_PER_SESSION
        or events_waiters() >= EVENTS_WAITERS_TOTAL
    ):
        return await events(session_key, after=after, limit=limit)
    _EVENTS_WAITERS[key] = _EVENTS_WAITERS.get(key, 0) + 1
    reads = _EVENTS_READS.setdefault(key, asyncio.Lock())

    async def read() -> dict:
        async with reads:
            return await events(session_key, after=after, limit=limit)

    try:
        stamp = await stamp_of(engine, native)
        # Stamped BEFORE the first read: an append between the two is a change, never missed.
        seen = stamp()
        page = await read()
        while not _moved(page, after):
            left = deadline - loop.time()
            if left <= 0:
                break
            await asyncio.sleep(min(EVENTS_WAIT_TICK, left))
            if disconnected is not None and disconnected():
                break
            now = stamp()
            if now != seen:
                seen = now
                page = await read()
        return page
    finally:
        # Cancelled (the client went away mid-wait) or answered: the slot is free either way.
        _EVENTS_WAITERS[key] -= 1
        if not _EVENTS_WAITERS[key]:
            del _EVENTS_WAITERS[key]
            _EVENTS_READS.pop(key, None)


# One history page render per session at a time (#1504): a page projects the whole journal.
_HISTORY_IN_FLIGHT: set[str] = set()


async def history(session_key: str, *, before: object, limit: object = SNAPSHOT_TURNS) -> dict:
    """The turns just older than ``before`` (#1504), chronological and shaped as the snapshot's
    turns are, under one text budget spent newest first. ``before`` is where the next older page
    starts, None at the conversation's first turn."""
    if not chat_store.valid_turn_id(before):
        raise StructuredError(422, "before must be a turn id")
    if type(limit) is not int or not 1 <= limit <= SNAPSHOT_TURNS:
        raise StructuredError(422, f"limit must be 1-{SNAPSHOT_TURNS}")
    engine, native, adapter, _ = _observation_target(session_key, "history")
    key = f"{engine}:{native}"
    if key in _HISTORY_IN_FLIGHT:
        raise StructuredError(429, "a history page for this session is already being read")
    _HISTORY_IN_FLIGHT.add(key)
    try:
        raw = await adapter.history(engine, native, before, limit)
    finally:
        _HISTORY_IN_FLIGHT.discard(key)
    budget = [SNAPSHOT_TEXT_BUDGET]
    turns = [_turn(t, budget) for t in reversed(raw["turns"])][::-1]
    return {
        "session_key": key,
        "turns": turns,
        "before": raw["history_before"],
        "has_more": raw["history_before"] is not None,
    }


async def start_session(session_key: str, *, wait_ready: bool = True) -> dict:
    """Launch a skip-permissions creation the operator has SEEN succeed (#1339): its create
    reserved and launched nothing. Idempotent: a repeat after a launch only observes.
    ``wait_ready`` as for :func:`create_session` (#1505)."""
    engine, native = await asyncio.to_thread(_key, session_key)  # fresh roster reads: off-loop
    adapter = await asyncio.to_thread(_adapter, engine, "start")
    await adapter.start(engine, native, wait_ready=wait_ready)
    return await snapshot(f"{engine}:{native}")


async def interrupt(session_key: str, *, operation_id: str, turn_id: str) -> dict:
    """Request interruption of the exact active turn. Acknowledgement is not completion."""
    engine, native = _key(session_key)
    result = await _adapter(engine, "interrupt").interrupt(
        engine, native, operation_id=_operation_id(operation_id), turn_id=_operation_id(turn_id)
    )
    return {"session_key": f"{engine}:{native}", **result}


async def enable_bypass(session_key: str, *, operation_id: str, user: str) -> dict:
    """Enable prompts bypass only after containing the previous worker; resume on next Send."""
    engine, native = _key(session_key)
    await _adapter(engine, "enable_bypass").enable_bypass(
        engine, native, operation_id=_operation_id(operation_id), user=user
    )
    return await snapshot(f"{engine}:{native}")


def _stop_all(value) -> dict | None:
    """Stop all and send's confirmation (#1644): the live worker and the exact background task
    ids the operator saw. Shape only; whether it still matches is the worker's to say."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"worker", "task_ids"}
        or not isinstance(value["task_ids"], list)
        or not value["task_ids"]
        or len(value["task_ids"]) > native_protocol.MAX_PENDING
        or not all(isinstance(t, str) and 0 < len(t) <= 256 for t in value["task_ids"])
    ):
        raise StructuredError(422, "invalid Stop all and send confirmation")
    return {"worker": _operation_id(value["worker"]), "task_ids": list(value["task_ids"])}


async def send_now(
    session_key: str,
    *,
    operation_id: str,
    turn_id: str,
    queued_turn_id: str,
    stop_all: dict | None = None,
) -> dict:
    """Deliver queued guidance sooner using the live adapter's declared native behavior."""
    engine, native = _key(session_key)
    confirm = _stop_all(stop_all)
    result = await _adapter(engine, "send_now").send_now(
        engine,
        native,
        operation_id=_operation_id(operation_id),
        turn_id=_operation_id(turn_id),
        queued_turn_id=_operation_id(queued_turn_id),
        **({"stop_all": confirm} if confirm is not None else {}),
    )
    return {"session_key": f"{engine}:{native}", **result}


async def withdraw(session_key: str, *, operation_id: str, queued_turn_id: str) -> dict:
    """Remove one still-queued message before it is sent (#1447)."""
    engine, native = _key(session_key)
    result = await _adapter(engine, "withdraw").withdraw(
        engine,
        native,
        operation_id=_operation_id(operation_id),
        queued_turn_id=_operation_id(queued_turn_id),
    )
    return {"session_key": f"{engine}:{native}", **result}


def _containment_target(session_key: str):
    """Stop/probe reach an ALREADY-ADMITTED native worker even when its provider is retiring:
    retirement must not strand a running process (Hermes on #1278). New work still requires
    live admission through `_key`/`_adapter`."""
    try:
        engine, native = _key(session_key)
        return engine, native, _adapter(engine, "stop")
    except StructuredError:
        engine, sep, native = session_key.partition(":")
        prov = engines.get_any(engine) if sep else None
        if prov is None or _ADAPTERS.get((prov.manifest.runtime, _kind(prov.manifest))) is None:
            raise
        adapter = _ADAPTERS[(prov.manifest.runtime, _kind(prov.manifest))]
        if not isinstance(adapter, _NativeAdapter):
            raise
        return engine, native, adapter


def _observation_target(session_key: str, operation: str):
    """Reads of an existing conversation: a live client as usual, else a retiring NATIVE client
    through the same fallback as stop/probe, with the reason it takes no new work. Reading grants
    nothing — submit and decide still require live admission."""
    try:
        engine, native = _key(session_key)
        return engine, native, _adapter(engine, operation), None
    except StructuredError:
        engine, native, adapter = _containment_target(session_key)
        problem = engines.registry.current().problems.get(engine)
        return engine, native, adapter, problem or engines.REMOVED_REASON


def model_facts(session_key: str) -> dict | None:
    """A native API session's requested, client-reported and observed model and level (#1511), or
    None when the key is not a native API session or nothing could be read. Fail-soft like
    `transcript.effective_model`: the one-session lookup must answer without it, never 500.
    Blocking."""
    try:
        engine, native, adapter, _ = _observation_target(session_key, "snapshot")
        if not isinstance(adapter, _NativeAdapter):
            return None
        return native_runtime.model_facts(engine, native)
    except Exception:
        return None


async def stop(session_key: str) -> dict:
    """Close the session's worker generation and report proved containment."""
    engine, native, adapter = _containment_target(session_key)
    result = await adapter.stop(engine, native)
    return {"session_key": f"{engine}:{native}", **result}


async def probe(session_key: str) -> dict:
    engine, native, adapter = _containment_target(session_key)
    result = await adapter.probe(engine, native)
    return {"session_key": f"{engine}:{native}", **result}
