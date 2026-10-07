"""Bounded, transport-free codecs for the reviewed native protocols (#1278).

The worker owns admission, durable identities, timeouts and process containment. A
frame returned here is only a proposal to write: the caller must admit and journal
it first. There are no retries. EOF is never completion. Protocol observations are
allowlisted; original permission input stays private until a one-operation reply.

Wire shapes are based on Codex app-server 0.159.3's generated v2 schemas and the
Claude Agent SDK's stream-JSON control protocol. Claude CLI 2.1.248 or later is
required for client_composed (verbatim input); the complete reviewed argv requires
2.1.287 or later, matching the CLI help used here. Explicit session_id and human
origin are supported SDK input fields.
Native sandbox/settings govern tools within an admitted turn; this codec does
not install per-tool authority hooks. No SDK implementation is vendored.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

CODEX_MIN_VERSION = (0, 159, 3)
CLAUDE_MIN_VERSION = (2, 1, 287)
MAX_FRAME_BYTES = 1_048_576
MAX_TEXT = 20_000
MAX_INPUT_TEXT = 200_000
MAX_PENDING = 128
MAX_RESULTS = 1024
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z", re.ASCII)


class ProtocolError(ValueError):
    """Malformed, oversized or unsupported protocol operation."""


@dataclass(frozen=True)
class NativeEvent:
    kind: str
    data: dict[str, Any]


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProtocolError("invalid native identity")
    return value


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ProtocolError("expected a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ProtocolError("expected a canonical UUID") from exc
    if canonical != value:
        raise ProtocolError("expected a canonical UUID")
    return canonical


def _rpc_id(value: Any) -> str | int:
    if type(value) is int and -(2**53) < value < 2**53:
        return value
    return _identity(value)


def _approval_key(value: Any) -> str:
    value = _rpc_id(value)
    return f"{'i' if type(value) is int else 's'}:{value}"


def _text(value: Any, *, limit: int = MAX_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(ord(char) < 32 for char in value)
        or ".." in PurePosixPath(value).parts
    ):
        raise ProtocolError("expected an absolute validated path")
    return value


def _model(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or not _MODEL.fullmatch(value)):
        raise ProtocolError("invalid model identity")
    return value


def validate_text(text: Any) -> str:
    """Validate before a durable submit claim; reserve room for protocol metadata."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_TEXT:
        raise ProtocolError("turn text is empty or exceeds the size limit")
    try:
        size = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    except UnicodeError as exc:
        raise ProtocolError("turn text is not valid UTF-8") from exc
    if size > MAX_FRAME_BYTES - 4096:
        raise ProtocolError("turn text exceeds the native frame size limit")
    return text


def _observed_model(value: Any) -> str | None:
    # Truncating an identity would invent a different model, not bound evidence.
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


def _copy_frame(value: Any) -> dict[str, Any]:
    """A bounded deep copy also prevents callers mutating a pending approval."""
    if not isinstance(value, dict):
        raise ProtocolError("expected a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > MAX_FRAME_BYTES:
            raise ProtocolError("native frame exceeds the size limit")
        result = json.loads(encoded)
        stack = [(result, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 32:
                raise ProtocolError("native frame exceeds the nesting limit")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return result
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ProtocolError("invalid native JSON frame") from exc


def encode(frame: dict[str, Any]) -> bytes:
    """Encode one admitted complete JSONL frame (never shell input)."""
    return (
        json.dumps(_copy_frame(frame), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def decode(line: bytes) -> dict[str, Any]:
    """Decode one complete frame; the worker bounds buffering before this call."""
    if not isinstance(line, bytes) or len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise ProtocolError("native frame is incomplete or oversized")
    if b"\n" in line[:-1] or b"\r" in line:
        raise ProtocolError("native frame must contain exactly one JSONL record")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("native frame has duplicate fields")
            result[key] = value
        return result

    try:
        return _copy_frame(json.loads(line.decode("utf-8", "strict"), object_pairs_hook=pairs))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid native JSON frame") from exc


def _digest(frame: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(frame, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _event(kind: str, **data: Any) -> NativeEvent:
    return NativeEvent(kind, data)


def _summary(value: Any) -> str:
    if not isinstance(value, dict | list):
        return _text(value)
    return _text(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _approval_bound(frame: dict[str, Any]) -> None:
    """Refuse a callback that cannot be presented completely within the preview bound.

    A digest identifies the exact callback but is not an operator review of its contents.
    The future approval UI must project the full consequential payload before offering a
    decision. Until that projection exists, these events remain backend observations only.
    """
    if len(json.dumps(frame, ensure_ascii=True, separators=(",", ":"))) > MAX_TEXT:
        raise ProtocolError("permission request exceeds the full-payload presentation bound")


class _Codec:
    def __init__(self) -> None:
        self._serial = 0
        self._requests: dict[str, tuple[str, str | None]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._consumed: set[str] = set()
        self._expected_sessions: dict[str, str] = {}
        self.native_id: str | None = None
        self.operation_id: str | None = None
        self.native_turn_id: str | None = None
        self._closed = False

    def _next(self, action: str, operation_id: str | None = None) -> str:
        if self._closed or len(self._requests) >= MAX_PENDING:
            raise ProtocolError("native connection is closed or request capacity exceeded")
        self._serial += 1
        request_id = f"battlelab-{self._serial}"
        self._requests[request_id] = (action, operation_id)
        return request_id

    def _submit(self, text: str, operation_id: str) -> None:
        if self._closed or self.operation_id is not None:
            raise ProtocolError("native turn is already active or connection closed")
        validate_text(text)
        self.operation_id = _uuid(operation_id)
        self._consumed.clear()
        self.native_turn_id = None

    def _remember(self, request_id: Any, payload: dict[str, Any]) -> str:
        key = _approval_key(request_id)
        if key in self._consumed:
            raise ProtocolError("native permission request was already consumed")
        if key in self._approvals:
            if self._approvals[key] != payload:
                raise ProtocolError("native permission request identity changed")
        elif len(self._approvals) + len(self._consumed) >= MAX_PENDING:
            raise ProtocolError("native permission capacity exceeded")
        self._approvals[key] = payload
        return key

    def _decision(self, request_id: str, decision: str) -> dict[str, Any]:
        if not isinstance(decision, str) or decision not in {"approve", "reject", "cancel"}:
            raise ProtocolError("only one-operation permission decisions are supported")
        item = self._approvals.get(request_id)
        if (
            item is None
            or self._closed
            or item["operation_id"] != self.operation_id
            or item["native_turn_id"] != self.native_turn_id
        ):
            raise ProtocolError("permission request is no longer pending for this turn")
        # Consumption is deliberate: failed/uncertain writes are never retried by a codec.
        self._consumed.add(request_id)
        return self._approvals.pop(request_id)

    def eof(self) -> NativeEvent:
        self._closed = True
        self._approvals.clear()
        self._requests.clear()
        self._expected_sessions.clear()
        return _event(
            "disconnected",
            state="uncertain",
            native_id=self.native_id,
            native_turn_id=self.native_turn_id,
            operation_id=self.operation_id,
        )


class CodexCodec(_Codec):
    def __init__(self) -> None:
        super().__init__()
        self._early_frames: list[dict[str, Any]] = []
        self._early_size = 0

    @staticmethod
    def argv(binary: str) -> list[str]:
        return [_path(binary), "app-server", "--listen", "stdio://"]

    def _request(self, action: str, params: dict[str, Any], op: str | None = None) -> dict:
        return {"id": self._next(action, op), "method": action, "params": params}

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {"clientInfo": {"name": "battlelab_api", "title": "BattleLab", "version": "1"}},
        )

    @staticmethod
    def initialized() -> dict:
        return {"method": "initialized"}

    def create(self, cwd: str, model: str | None = None) -> dict:
        if self.native_id is not None:
            raise ProtocolError("native thread is already bound")
        params = self.create_params(cwd, model)
        return self._request("thread/start", params)

    def read(self, thread_id: str) -> dict:
        frame = self._request(
            "thread/read", {"threadId": _identity(thread_id), "includeTurns": True}
        )
        self._expected_sessions[frame["id"]] = thread_id
        return frame

    def resume(self, thread_id: str, cwd: str, model: str | None = None) -> dict:
        params = self.create_params(cwd, model)
        params["threadId"] = _identity(thread_id)
        # Never hydrate the full history into one frame: a long thread exceeds the frame bound
        # and could then never be resumed. BattleLab's own journal already holds what it saw.
        params["excludeTurns"] = True
        frame = self._request("thread/resume", params)
        self._expected_sessions[frame["id"]] = thread_id
        return frame

    @staticmethod
    def create_params(cwd: str, model: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": _path(cwd),
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "read-only",
        }
        if _model(model) is not None:
            params["model"] = model
        return params

    def submit(self, text: str, operation_id: str) -> dict:
        if self.native_id is None:
            raise ProtocolError("native thread is not bound")
        self._submit(text, operation_id)
        return self._request(
            "turn/start",
            {
                "threadId": self.native_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": self.operation_id,
            },
            self.operation_id,
        )

    def interrupt(self) -> dict:
        if self.native_id is None or self.native_turn_id is None or self.operation_id is None:
            raise ProtocolError("no correlated native turn to interrupt")
        return self._request(
            "turn/interrupt", {"threadId": self.native_id, "turnId": self.native_turn_id}
        )

    def decide(self, request_id: str, decision: str) -> dict:
        pending = self._decision(request_id, decision)
        return {
            "id": pending["id"],
            "result": {
                "decision": {"approve": "accept", "reject": "decline", "cancel": "cancel"}[decision]
            },
        }

    def _turn(self, turn: dict, *, completed: bool = False) -> list[NativeEvent]:
        turn_id = _identity(turn.get("id"))
        if self.operation_id is None or (
            self.native_turn_id is not None and self.native_turn_id != turn_id
        ):
            return [
                _event(
                    "background", native_turn_id=turn_id, state=_text(turn.get("status"), limit=64)
                )
            ]
        already_started = self.native_turn_id == turn_id
        self.native_turn_id = turn_id
        data = {"native_turn_id": turn_id, "operation_id": self.operation_id}
        if not completed:
            return [] if already_started else [_event("turn_started", **data)]
        status = turn.get("status")
        if not isinstance(status, str) or status not in {"completed", "failed", "interrupted"}:
            raise ProtocolError("completed notification has no terminal turn status")
        error = turn.get("error")
        self.operation_id = None
        self.native_turn_id = None
        self._approvals.clear()
        return [
            _event(
                "turn_completed",
                **data,
                state=status,
                error=_text(error.get("message")) if isinstance(error, dict) else "",
            )
        ]

    def _reply(self, frame: dict) -> list[NativeEvent]:
        request_id = _rpc_id(frame.get("id"))
        pending = self._requests.pop(request_id, None) if isinstance(request_id, str) else None
        if pending is None:
            return []
        action, op = pending
        expected_session = self._expected_sessions.pop(request_id, None)
        if "error" in frame:
            error = frame["error"]
            if action == "turn/start" and op is not None and op == self.operation_id:
                # The agent refused the turn itself: it never started, so the connection is
                # free for the next one (leaving it "active" wedged every later submit).
                self.operation_id = None
                self.native_turn_id = None
                self._early_frames, self._early_size = [], 0
            return [
                _event(
                    "error",
                    request_id=request_id,
                    action=action,
                    operation_id=op,
                    message=_text(error.get("message"))
                    if isinstance(error, dict)
                    else "Native request failed",
                )
            ]
        result = frame.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("native response has no result object")
        if action == "initialize":
            return [_event("initialized", request_id=request_id)]
        if action in {"thread/start", "thread/read", "thread/resume"}:
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise ProtocolError("native response has no thread")
            native_id = _identity(thread.get("id"))
            if expected_session is not None and native_id != expected_session:
                raise ProtocolError("native response does not match the requested thread")
            if self.native_id is not None and self.native_id != native_id:
                raise ProtocolError("native thread identity changed")
            self.native_id = native_id
            turns = thread.get("turns", [])
            if not isinstance(turns, list):
                raise ProtocolError("native thread turns must be an array")
            observed = []
            for turn in turns[-50:]:
                if not isinstance(turn, dict):
                    raise ProtocolError("invalid native thread turn")
                observed.append(
                    {
                        "native_turn_id": _identity(turn.get("id")),
                        "state": _text(turn.get("status"), limit=64),
                    }
                )
            return [
                _event(
                    "session",
                    request_id=request_id,
                    action=action,
                    native_id=native_id,
                    model_configured=_observed_model(result.get("model") or thread.get("model")),
                    model_effective=None,
                    turns=observed,
                    omitted_turns=max(0, len(turns) - 50),
                )
            ]
        if action == "turn/start":
            turn = result.get("turn")
            if not isinstance(turn, dict):
                raise ProtocolError("native response has no turn")
            if self.operation_id != op:
                return [_event("response", request_id=request_id, action=action)]
            output = self._turn(turn)
            for event in output:
                event.data.update(request_id=request_id, action=action)
            early, self._early_frames = self._early_frames, []
            self._early_size = 0
            for observed_frame in early:
                output.extend(self.feed(observed_frame))
            return output
        return [_event("response", request_id=request_id, action=action)]

    def _approval(self, frame: dict, params: dict) -> list[NativeEvent]:
        request_id = _rpc_id(frame.get("id"))
        method = frame.get("method")
        allowed = method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }
        correlated = (
            self.operation_id is not None
            and params.get("threadId") == self.native_id
            and params.get("turnId") == self.native_turn_id
        )
        # grantRoot explicitly asks for a lasting filesystem grant, not one patch.
        if (
            not allowed
            or not correlated
            or (method == "item/fileChange/requestApproval" and params.get("grantRoot"))
        ):
            return [
                _event(
                    "send",
                    frame={
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": "Unsupported or uncorrelated permission request",
                        },
                    },
                )
            ]
        item_id = _identity(params.get("itemId"))
        _approval_bound(frame)
        key = self._remember(
            request_id,
            {
                "id": request_id,
                "operation_id": self.operation_id,
                "native_turn_id": self.native_turn_id,
                "params": params,
                "frame": frame,
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=_digest(frame),
                native_turn_id=self.native_turn_id,
                operation_id=self.operation_id,
                item_id=item_id,
                tool="command"
                if method == "item/commandExecution/requestApproval"
                else "file_change",
                # The COMPLETE request (network context, proposed amendments, cwd …), not a
                # command excerpt: what the operator sees is everything a decision approves.
                # `_approval_bound` already refused any frame too large to present whole.
                summary=_summary(params),
                choices=["approve", "reject", "cancel"],
            )
        ]

    def feed(self, message: dict[str, Any]) -> list[NativeEvent]:
        if self._closed:
            raise ProtocolError("native connection is closed")
        frame = _copy_frame(message)
        method = frame.get("method")
        if method is not None and not isinstance(method, str):
            raise ProtocolError("native method must be a string")
        if method is None:
            return self._reply(frame)
        params = frame.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("native notification params must be an object")
        if (
            self.operation_id is not None
            and self.native_turn_id is None
            and params.get("threadId") == self.native_id
            and method
            in {
                "turn/started",
                "turn/completed",
                "item/started",
                "item/completed",
                "item/agentMessage/delta",
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "error",
            }
        ):
            size = len(encode(frame))
            if len(self._early_frames) >= MAX_PENDING or self._early_size + size > MAX_FRAME_BYTES:
                raise ProtocolError("uncorrelated native event buffer exceeded")
            self._early_frames.append(frame)
            self._early_size += size
            return []
        if "id" in frame:
            return self._approval(frame, params)
        if params.get("threadId") != self.native_id or self.native_id is None:
            return []
        if method == "serverRequest/resolved":
            key = _approval_key(params.get("requestId"))
            if self._approvals.pop(key, None) is not None:
                self._consumed.add(key)
            return [_event("approval_cancelled", request_id=key)]
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise ProtocolError("native turn notification has no turn")
            return self._turn(turn, completed=method == "turn/completed")
        if params.get("turnId") != self.native_turn_id or self.operation_id is None:
            return []
        common = {"native_turn_id": self.native_turn_id, "operation_id": self.operation_id}
        if method == "error":
            error = params.get("error")
            return [
                _event(
                    "error",
                    **common,
                    message=_text(error.get("message"))
                    if isinstance(error, dict)
                    else "Native turn error",
                    native_will_retry=params.get("willRetry") is True,
                )
            ]
        if method == "item/agentMessage/delta":
            return [
                _event(
                    "text",
                    **common,
                    item_id=_identity(params.get("itemId")),
                    text=_text(params.get("delta")),
                    partial=True,
                    truncated=isinstance(params.get("delta"), str)
                    and len(params["delta"]) > MAX_TEXT,
                )
            ]
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                raise ProtocolError("native item notification has no item")
            item_id, kind = _identity(item.get("id")), item.get("type")
            if not isinstance(kind, str):
                raise ProtocolError("native item kind must be a string")
            if kind == "agentMessage":
                return [
                    _event(
                        "text",
                        **common,
                        item_id=item_id,
                        text=_text(item.get("text")),
                        partial=method == "item/started",
                        truncated=isinstance(item.get("text"), str)
                        and len(item["text"]) > MAX_TEXT,
                    )
                ]
            if kind in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
                "dynamicToolCall",
                "webSearch",
                "collabAgentToolCall",
            }:
                return [
                    _event(
                        "tool",
                        **common,
                        item_id=item_id,
                        tool=kind,
                        state=_text(item.get("status"), limit=64),
                        summary=_text(item.get("command") or item.get("tool") or item.get("query")),
                        output=_text(item.get("aggregatedOutput")),
                        completed=method == "item/completed",
                    )
                ]
        return []


class ClaudeCodec(_Codec):
    """One serial native connection, with result replay detection across its submitted turns.

    Claude does not expose a submitted-operation id on results: native_turn_id below is our
    submitted user-message UUID. An unseen human result is correlated by the native stream's
    serial ordering, not by an invented echo-order guarantee. Result UUIDs are retained without
    eviction until connection close; a full ledger refuses further submissions. A missing UUID
    cannot support this guarantee even though the SDK's compatibility type permits its absence.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.native_id = _uuid(session_id)
        self._background: set[str] = set()
        self._session_state: str | None = None
        self._results: dict[str, str] = {}

    @staticmethod
    def argv(
        binary: str, session_id: str, *, resume: bool = False, model: str | None = None
    ) -> list[str]:
        native_id = _uuid(session_id)
        argv = [
            _path(binary),
            "--print",
            "--permission-prompts",
            "host",
            "--replay-user-messages",
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--permission-prompt-tool",
            "stdio",
            "--permission-mode",
            "default",
            "--include-partial-messages",
            f"--{'resume' if resume else 'session-id'}={native_id}",
        ]
        if _model(model) is not None:
            argv.extend(["--model", model])
        return argv

    def _request(self, action: str, **params: Any) -> dict:
        return {
            "type": "control_request",
            "request_id": self._next(action),
            "request": {"subtype": action, **params},
        }

    def initialize(self) -> dict:
        return self._request("initialize", hooks=None)

    def submit(self, text: str, operation_id: str) -> dict:
        if len(self._results) >= MAX_RESULTS:
            raise ProtocolError(
                "native result replay capacity exceeded; a new connection is required"
            )
        self._submit(text, operation_id)
        self.native_turn_id = self.operation_id
        return {
            "type": "user",
            "uuid": self.operation_id,
            "session_id": self.native_id,
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "origin": {"kind": "human"},
            "client_composed": True,
        }

    def interrupt(self) -> dict:
        if self.operation_id is None:
            raise ProtocolError("no correlated native turn to interrupt")
        return self._request("interrupt")

    @staticmethod
    def _response(request_id: str, response: dict) -> dict:
        return {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": response},
        }

    def decide(self, request_id: str, decision: str) -> dict:
        pending = self._decision(request_id, decision)
        data = (
            {"behavior": "allow", "updatedInput": pending["input"]}
            if decision == "approve"
            else {
                "behavior": "deny",
                "message": "Declined by BattleLab operator",
                "interrupt": decision == "cancel",
            }
        )
        return self._response(pending["id"], data)

    def _control(self, frame: dict) -> list[NativeEvent]:
        request_id = _identity(frame.get("request_id"))
        request = frame.get("request")
        if not isinstance(request, dict):
            raise ProtocolError("native control request must be an object")
        subtype = request.get("subtype")
        # A callback from a background/sub-agent (`agent_id`) cannot be proved to belong to the
        # operator's turn, so it is refused rather than attributed to it (Hermes on #1278).
        if (
            subtype != "can_use_tool"
            or self.operation_id is None
            or request.get("agent_id") is not None
        ):
            return [
                _event(
                    "send",
                    frame={
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": request_id,
                            "error": "Unsupported or uncorrelated control request",
                        },
                    },
                )
            ]
        original_input = request.get("input")
        if not isinstance(original_input, dict):
            raise ProtocolError("native permission input must be an object")
        tool_name = _identity(request.get("tool_name"))
        item_id = _identity(request.get("tool_use_id"))
        _approval_bound(frame)
        key = self._remember(
            request_id,
            {
                "id": request_id,
                "operation_id": self.operation_id,
                "native_turn_id": self.native_turn_id,
                "input": original_input,
                "tool": tool_name,
                "item_id": item_id,
                "frame": frame,
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=_digest(frame),
                native_turn_id=self.native_turn_id,
                operation_id=self.operation_id,
                item_id=item_id,
                tool=tool_name,
                summary=_summary(original_input),
                choices=["approve", "reject", "cancel"],
            )
        ]

    def feed(self, message: dict[str, Any]) -> list[NativeEvent]:
        if self._closed:
            raise ProtocolError("native connection is closed")
        frame = _copy_frame(message)
        kind = frame.get("type")
        if frame.get("session_id") is not None and frame.get("session_id") != self.native_id:
            raise ProtocolError("native session identity changed")
        if kind == "control_request":
            return self._control(frame)
        if kind == "control_cancel_request":
            key = _approval_key(frame.get("request_id"))
            if self._approvals.pop(key, None) is not None:
                self._consumed.add(key)
            return [_event("approval_cancelled", request_id=key)]
        if kind == "control_response":
            response = frame.get("response")
            if not isinstance(response, dict):
                raise ProtocolError("native control response must be an object")
            request_id = _identity(response.get("request_id"))
            pending = self._requests.pop(request_id, None)
            if pending is None:
                return []
            action, _ = pending
            if response.get("subtype") != "success":
                return [
                    _event(
                        "error",
                        request_id=request_id,
                        action=action,
                        message=_text(response.get("error")),
                    )
                ]
            return [
                _event(
                    "initialized" if action == "initialize" else "response",
                    request_id=request_id,
                    action=action,
                )
            ]
        subtype = frame.get("subtype")
        if not isinstance(kind, str) or (subtype is not None and not isinstance(subtype, str)):
            raise ProtocolError("native message kind must be a string")
        if kind == "system" and subtype == "init":
            return [
                _event(
                    "session",
                    native_id=self.native_id,
                    model_configured=_observed_model(frame.get("model")),
                    model_effective=None,
                )
            ]
        if kind == "system" and subtype in {
            "task_started",
            "task_progress",
            "task_notification",
            "task_updated",
        }:
            task_id = _identity(frame.get("task_id"))
            patch = frame.get("patch")
            status = (
                patch.get("status")
                if subtype == "task_updated" and isinstance(patch, dict)
                else frame.get("status")
            )
            state = _text(status, limit=64)
            if subtype in {"task_notification", "task_updated"} and state in {
                "completed",
                "failed",
                "stopped",
                "killed",
            }:
                self._background.discard(task_id)
            else:
                if len(self._background) >= MAX_PENDING and task_id not in self._background:
                    raise ProtocolError("native background task capacity exceeded")
                self._background.add(task_id)
            return [
                _event(
                    "background",
                    task_id=task_id,
                    state=state or "running",
                    active=bool(self._background),
                )
            ]
        if kind == "system" and subtype == "session_state_changed":
            state = frame.get("state")
            self._session_state = (
                state
                if isinstance(state, str) and state in {"running", "idle", "requires_action"}
                else "unknown"
            )
            return [
                _event(
                    "background",
                    state=self._session_state,
                    active=bool(self._background) or self._session_state != "idle",
                    native_state=self._session_state,
                )
            ]
        if kind == "system" and subtype == "status":
            return [
                _event(
                    "background",
                    state=_text(frame.get("status"), limit=64),
                    active=bool(self._background),
                )
            ]
        origin = frame.get("origin")
        if origin is not None and (
            not isinstance(origin, dict) or not isinstance(origin.get("kind"), str)
        ):
            raise ProtocolError("native message origin is malformed")
        if kind == "result":
            result_id = _uuid(frame.get("uuid"))
            if type(frame.get("is_error")) is not bool:
                raise ProtocolError("native result has no explicit error status")
            digest = _digest(frame)
            previous = self._results.get(result_id)
            if previous is not None:
                if previous != digest:
                    raise ProtocolError("native result identity changed")
                return []
            if len(self._results) >= MAX_RESULTS:
                raise ProtocolError("native result replay capacity exceeded")
            # Retain idle and background results too: observing them before submission is
            # not permission to attribute a later replay to the next operator turn.
            self._results[result_id] = digest
        background = frame.get("parent_tool_use_id") is not None or (
            isinstance(origin, dict) and origin.get("kind") != "human"
        )
        if background:
            return [_event("background", state="activity", active=True)]
        if self.operation_id is None:
            return []
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        if kind == "result":
            if frame["is_error"] or subtype != "success":
                state = "failed"
            elif frame.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
                state = "interrupted"
            elif frame.get("terminal_reason") not in (None, "completed"):
                state = "failed"
            else:
                state = "completed"
            self.operation_id = None
            self.native_turn_id = None
            self._approvals.clear()
            return [
                _event(
                    "turn_completed",
                    **common,
                    state=state,
                    text=_text(frame.get("result")),
                    background_active=bool(self._background)
                    or self._session_state in {"running", "requires_action", "unknown"},
                    native_state=self._session_state,
                    error=_summary(frame.get("errors")),
                )
            ]
        if kind == "assistant":
            msg = frame.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                raise ProtocolError("native assistant message has no content")
            item = {"item_id": _identity(msg["id"])} if "id" in msg else {}
            output = []
            model = _observed_model(msg.get("model"))
            if model is not None:
                output.append(_event("model", **common, model_effective=model))
            for block in msg["content"][:128]:
                if not isinstance(block, dict):
                    raise ProtocolError("invalid native content block")
                if block.get("type") == "text":
                    output.append(
                        _event(
                            "text",
                            **common,
                            **item,
                            text=_text(block.get("text")),
                            partial=False,
                            truncated=isinstance(block.get("text"), str)
                            and len(block["text"]) > MAX_TEXT,
                        )
                    )
                elif block.get("type") == "tool_use":
                    output.append(
                        _event(
                            "tool",
                            **common,
                            item_id=_identity(block.get("id")),
                            tool=_identity(block.get("name")),
                            state="running",
                            summary=_summary(block.get("input")),
                        )
                    )
            return output
        if kind == "user" and isinstance(frame.get("message"), dict):
            content = frame["message"].get("content")
            if isinstance(content, list):
                return [
                    _event(
                        "tool",
                        **common,
                        item_id=_identity(block.get("tool_use_id")),
                        state="failed" if block.get("is_error") else "completed",
                        output=_summary(block.get("content")),
                    )
                    for block in content[:128]
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
        if kind == "stream_event":
            event = frame.get("event")
            if isinstance(event, dict) and event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    return [
                        _event(
                            "text",
                            **common,
                            text=_text(delta.get("text")),
                            partial=True,
                            truncated=isinstance(delta.get("text"), str)
                            and len(delta["text"]) > MAX_TEXT,
                        )
                    ]
        return []
