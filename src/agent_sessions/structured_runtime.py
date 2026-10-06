"""Shared structured-client admission and operations (#1275).

This facade is transport-neutral. Reviewed implementations, selected by manifest shape, own
execution; console launch/seed capabilities never count as structured support. The first adapter
delegates to the existing chat runtime, including its durable store, tools and approval fences.
Native manifests can be validated before their adapters ship, but cannot execute in this build.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType

from . import chat_config, chat_runtime, chat_store, engines
from .engine_errors import EngineError
from .plugins import api_source
from .structured_types import ExecutionGuard, normalize_context

SNAPSHOT_TURNS = 50
SNAPSHOT_TEXT = 20_000
SNAPSHOT_TEXT_BUDGET = 200_000


class StructuredError(Exception):
    """A safe refusal at the common client boundary."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


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

    async def create(self, *args, **kwargs):
        return await _call(chat_runtime.new_session, *args, **kwargs)

    async def snapshot(self, *args, **kwargs):
        return await _call(chat_runtime.get_session, *args, **kwargs)

    async def submit(self, *args, **kwargs):
        return await _call(chat_runtime.send, *args, **kwargs)

    async def decide(self, *args, **kwargs):
        return await _call(chat_runtime.decide, *args, **kwargs)


# A manifest selects reviewed behavior; it cannot register code or invent a supported operation.
_ADAPTERS = MappingProxyType({("chat", "openai-chat"): _ChatAdapter()})


def _kind(manifest) -> str | None:
    if manifest.runtime == "chat" and manifest.endpoint is not None:
        return manifest.endpoint.kind
    if manifest.runtime == "api" and manifest.api is not None:
        return manifest.api.kind
    return None


def describe(engine_id: str) -> ClientDescriptor:
    """Current capability/readiness data, never an authorization grant or a network probe."""
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
        )


def require(engine_id: str, operation: str) -> ClientDescriptor:
    """Require implemented support; the adapter checks live readiness after replay lookup."""
    descriptor = describe(engine_id)
    if operation not in descriptor.operations:
        raise StructuredError(409, descriptor.reason or f"{operation} is not supported")
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
    if status == "awaiting_approval":
        return "awaiting_approval"
    if status == "failed":
        code = turn.get("code")
        return code if code in ("interrupted", "uncertain") else "failed"
    return "unavailable"


def _turn(turn: dict, text_budget: list[int] | None = None) -> dict:
    def bounded(value):
        if not isinstance(value, str):
            return None, False
        cap = SNAPSHOT_TEXT if text_budget is None else min(SNAPSHOT_TEXT, text_budget[0])
        text = value[:cap]
        if text_budget is not None:
            text_budget[0] -= len(text)
        return text, len(value) > len(text)

    text, text_cut = bounded(turn.get("text"))
    reply, reply_cut = bounded(turn.get("reply"))
    return {
        "turn_id": turn["turn_id"],
        "operation_id": turn["turn_id"],
        "state": _outcome(turn),
        "text": text,
        "reply": reply,
        "text_truncated": text_cut,
        "reply_truncated": reply_cut or bool(turn.get("truncated")),
        "reason": turn.get("reason"),
        "context": dict(turn.get("context") or {}),
        "requires_authority": bool(turn.get("execution_binding")),
        "tools": list(turn.get("tools") or [])[-64:],
        "tools_truncated": len(turn.get("tools") or []) > 64,
        "usage": turn.get("usage"),
    }


def _snapshot(session_key: str, raw: dict) -> dict:
    all_turns = raw["turns"]
    budget = [SNAPSHOT_TEXT_BUDGET]
    # Spend the display budget on newest turns first; return chronological order.
    turns = [_turn(t, budget) for t in reversed(all_turns[-SNAPSHOT_TURNS:])]
    turns.reverse()
    requests = []
    for turn in all_turns:
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
    active = next((t for t in all_turns if t["turn_id"] == raw.get("in_flight")), None)
    if active is not None:
        state = _outcome(active)
    elif all_turns and _outcome(all_turns[-1]) != "completed":
        state = _outcome(all_turns[-1])
    return {
        "session_key": session_key,
        "revision": raw["revision"],
        "event_cursor": raw["revision"],
        "cwd": raw["cwd"],
        "state": state,
        "active_turn": raw.get("in_flight"),
        "model_requested": raw.get("model"),
        # Configuration is a request, never proof of the model the endpoint actually executed.
        "model_effective": raw.get("model_effective"),
        "turns": turns,
        "omitted_turns": max(0, len(all_turns) - len(turns)),
        "pending_requests": requests,
    }


async def snapshot(session_key: str) -> dict:
    engine, native = _key(session_key)
    raw = await _adapter(engine, "snapshot").snapshot(engine, native)
    return _snapshot(f"{engine}:{native}", raw)


async def create_session(
    engine: str,
    cwd: str,
    *,
    operation_id: str,
    model: str | None = None,
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    adapter = _adapter(engine, "create")
    sid = await adapter.create(
        engine,
        cwd,
        session_id=_operation_id(operation_id),
        model=model,
        execution_admission=execution_admission,
    )
    return await snapshot(f"{engine}:{sid}")


async def submit_turn(
    session_key: str,
    *,
    operation_id: str,
    text: str,
    expected_revision: int | None = None,
    context: dict | None = None,
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    engine, native = _key(session_key)
    adapter = _adapter(engine, "submit")
    try:
        context = normalize_context(context)
    except ValueError as exc:
        raise StructuredError(422, str(exc)) from None
    tid = _operation_id(operation_id)
    await adapter.submit(
        engine,
        native,
        tid,
        text,
        expected_revision=expected_revision,
        context=context,
        execution_admission=execution_admission,
        idempotent=True,
    )
    raw = await adapter.snapshot(engine, native)
    turn = next(t for t in raw["turns"] if t["turn_id"] == tid)
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
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    engine, native = _key(session_key)
    adapter = _adapter(engine, "decide")
    result = await adapter.decide(
        engine,
        native,
        turn_id,
        request_id,
        decision,
        user,
        decision_id=_operation_id(decision_id),
        expected_revision=expected_revision,
        execution_admission=execution_admission,
    )
    return {"session_key": f"{engine}:{native}", "decision_id": decision_id, **result}
