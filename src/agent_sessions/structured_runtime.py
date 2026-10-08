"""Shared structured-client admission and operations (#1275).

This facade is transport-neutral. Reviewed implementations, selected by manifest shape, own
execution; console launch/seed capabilities never count as structured support. The first adapter
delegates to the existing chat runtime, including its durable store, tools and approval fences.
Native manifests can be validated before their adapters ship, but cannot execute in this build.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import asdict, dataclass
from types import MappingProxyType

from . import chat_config, chat_runtime, chat_store, engines, native_images, native_runtime
from .engine_errors import EngineError
from .plugins import api_source, kinds
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
    # The client's protocol takes pictures in a turn (`kinds.API_IMAGE_INPUT`, #1332 Phase 3).
    images: bool = False

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

    async def create(self, *args, bypass=False, **kwargs):
        if bypass:
            raise StructuredError(422, "this agent has no permission prompts to skip")
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
        return (
            "create",
            "start",
            "snapshot",
            "submit",
            "decide",
            "events",
            "interrupt",
            "stop",
            "probe",
        )

    @staticmethod
    def ready(prov) -> tuple[bool, str | None]:
        return native_runtime.readiness(prov)

    async def create(
        self, engine, cwd, *, session_id, model=None, bypass=False, execution_admission=None
    ):
        return await _native(
            native_runtime.create,
            engine,
            cwd,
            session_id=session_id,
            model=model,
            bypass=bypass,
            execution_admission=execution_admission,
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

    async def start(self, engine, native):
        return await _native(native_runtime.start, engine, native)

    async def interrupt(self, engine, native, **kwargs):
        return await _native(native_runtime.interrupt, engine, native, **kwargs)

    async def stop(self, engine, native):
        return await _native(native_runtime.stop, engine, native)

    async def probe(self, engine, native):
        return await _native(native_runtime.probe, engine, native)


async def _native(fn, *args, **kwargs):
    from . import native_journal, native_ownership, native_state
    from .plugins import storage

    try:
        return await fn(*args, **kwargs)
    except native_runtime.NativeError as exc:
        raise StructuredError(exc.status, exc.detail) from None
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
        # Names only: the browser shows each through the upload read-back route (#1332).
        "attachments": [
            {"stored": a["stored"], "mime": a["mime"]} for a in turn.get("attachments") or ()
        ],
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
        # Skip-permissions is fixed at create (#1339): the head shows it, nothing can change it.
        "bypass": bool(raw.get("bypass")),
        # Two-phase skip start (#1339): waiting for `start`, or expired without ever running.
        "pending_start": bool(raw.get("pending_start")),
        **(
            {"start_expires_at": raw["start_expires_at"]}
            if raw.get("start_expires_at") and raw.get("pending_start")
            else {}
        ),
        **({"start_incomplete": True} if raw.get("start_incomplete") else {}),
        **({"start_expired": True} if raw.get("start_expired") else {}),
        # Configuration is a request, never proof of the model the endpoint actually executed.
        "model_effective": raw.get("model_effective"),
        "turns": turns,
        "omitted_turns": max(0, len(all_turns) - len(turns)),
        "pending_requests": requests,
        **({"native": dict(raw["native"])} if "native" in raw else {}),
    }


async def snapshot(session_key: str) -> dict:
    """The bounded view. ``read_only`` is None while the client can take new work, else why not:
    a retiring native client (its source disabled or removed) still shows its history (#1311)."""
    engine, native, adapter, read_only = _observation_target(session_key, "snapshot")
    raw = await adapter.snapshot(engine, native)
    return {
        **_snapshot(f"{engine}:{native}", raw),
        "read_only": read_only,
        # Whether the composer offers pictures (#1332 Phase 3); submit re-checks it.
        "images": describe(engine, check_ready=False).images,
    }


async def create_session(
    engine: str,
    cwd: str,
    *,
    operation_id: str,
    model: str | None = None,
    bypass: bool = False,
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    adapter = _adapter(engine, "create")
    sid = await adapter.create(
        engine,
        cwd,
        session_id=_operation_id(operation_id),
        model=model,
        bypass=bypass,
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
    attachments: list[str] | None = None,
    execution_admission: ExecutionGuard | None = None,
) -> dict:
    """``attachments``: upload names (#1332 Phase 3), admitted here before anything durable."""
    engine, native = _key(session_key)
    adapter = _adapter(engine, "submit")
    try:
        context = normalize_context(context)
    except ValueError as exc:
        raise StructuredError(422, str(exc)) from None
    tid = _operation_id(operation_id)
    images = {}
    if attachments:
        if not isinstance(adapter, _NativeAdapter):
            raise StructuredError(422, "this client takes no images")
        try:
            images["attachments"] = await asyncio.to_thread(native_images.admit, attachments)
        except native_images.ImageError as exc:
            raise StructuredError(422, str(exc)) from None
    await adapter.submit(
        engine,
        native,
        tid,
        text,
        expected_revision=expected_revision,
        context=context,
        **images,
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


async def events(session_key: str, *, after: int, limit: int = 100) -> dict:
    """Durable journal observations after a cursor; the cursor counts records, not time."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise StructuredError(422, "after must be a cursor and limit 1-100")
    engine, native, adapter, _ = _observation_target(session_key, "events")
    page = await adapter.events(engine, native, after, limit)
    return {"session_key": f"{engine}:{native}", **page}


async def start_session(session_key: str) -> dict:
    """Launch a skip-permissions creation the operator has SEEN succeed (#1339): its create
    reserved and launched nothing. Idempotent: a repeat after a launch only observes."""
    engine, native = _key(session_key)
    await _adapter(engine, "start").start(engine, native)
    return await snapshot(f"{engine}:{native}")


async def interrupt(session_key: str, *, operation_id: str, turn_id: str) -> dict:
    """Request interruption of the exact active turn. Acknowledgement is not completion."""
    engine, native = _key(session_key)
    result = await _adapter(engine, "interrupt").interrupt(
        engine, native, operation_id=_operation_id(operation_id), turn_id=_operation_id(turn_id)
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


async def stop(session_key: str) -> dict:
    """Close the session's worker generation and report proved containment."""
    engine, native, adapter = _containment_target(session_key)
    result = await adapter.stop(engine, native)
    return {"session_key": f"{engine}:{native}", **result}


async def probe(session_key: str) -> dict:
    engine, native, adapter = _containment_target(session_key)
    result = await adapter.probe(engine, native)
    return {"session_key": f"{engine}:{native}", **result}
