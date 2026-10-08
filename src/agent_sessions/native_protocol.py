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
#: What BattleLab may WRITE to the agent in one frame: a turn's text plus up to four inlined
#: 5 MiB pictures as base64 (``native_images``). Frames read FROM the agent keep the 1 MiB cap.
MAX_WRITE_FRAME_BYTES = MAX_FRAME_BYTES + 4 * (-(-5 * 1024 * 1024 // 3) * 4 + 256)
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


def validate_text(text: Any, *, allow_empty: bool = False) -> str:
    """Validate before a durable submit claim; reserve room for protocol metadata.

    ``allow_empty``: a turn its pictures carry (#1332 Phase 3) may have no words.
    """
    if (
        not isinstance(text, str)
        or (not text.strip() and not allow_empty)
        or len(text) > MAX_INPUT_TEXT
    ):
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


def _copy_frame(value: Any, limit: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    """A bounded deep copy also prevents callers mutating a pending approval."""
    if not isinstance(value, dict):
        raise ProtocolError("expected a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > limit:
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


def encode(frame: dict[str, Any], limit: int = MAX_FRAME_BYTES) -> bytes:
    """Encode one admitted complete JSONL frame (never shell input)."""
    return (
        json.dumps(_copy_frame(frame, limit), ensure_ascii=False, separators=(",", ":")) + "\n"
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

    def _submit(self, text: str, operation_id: str, images=()) -> None:
        if self._closed or self.operation_id is not None:
            raise ProtocolError("native turn is already active or connection closed")
        validate_text(text, allow_empty=bool(images))
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
        # The latest proposed changes per file-change item of the active turn. A file-change
        # approval request names only its item; the patch arrives separately and is what an
        # approval would actually authorize.
        self._patches: dict[str, list[dict[str, Any]]] = {}

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

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """``images``: ``(mime, base64)`` pairs, sent inline as data URLs (#1332 Phase 3)."""
        if self.native_id is None:
            raise ProtocolError("native thread is not bound")
        self._submit(text, operation_id, images)
        items: list[dict] = [
            {"type": "image", "url": f"data:{mime};base64,{data}"} for mime, data in images
        ]
        if text.strip():
            items.append({"type": "text", "text": text})
        return self._request(
            "turn/start",
            {
                "threadId": self.native_id,
                "input": items,
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
        self._patches.clear()
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
        presented: dict[str, Any] = dict(params)
        complete = True
        if method == "item/fileChange/requestApproval":
            # The proposed patch is shown so the operator sees what is being declined, but a
            # file change is NEVER approvable here: nothing binds the patch Codex applies to the
            # one presented (an accept cannot be unsent, and a later revision could differ).
            # Approve waits for a version-bound guarantee of that binding (Hermes on #1278).
            complete = False
            changes = self._patches.get(item_id)
            if changes is not None:
                presented["changes"] = changes
        if complete and len(_summary(presented)) >= MAX_TEXT:
            presented, complete = dict(params), False  # cannot be shown whole
        digest = _digest({"frame": frame, "presented": presented})
        key = self._remember(
            request_id,
            {
                "id": request_id,
                "operation_id": self.operation_id,
                "native_turn_id": self.native_turn_id,
                "params": params,
                "frame": frame,
                "digest": digest,
                "complete": complete,
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=digest,
                native_turn_id=self.native_turn_id,
                operation_id=self.operation_id,
                item_id=item_id,
                tool="command"
                if method == "item/commandExecution/requestApproval"
                else "file_change",
                # The COMPLETE request (network context, proposed amendments, cwd, and for a
                # file change its proposed patch), not an excerpt: what the operator sees is
                # everything a decision approves, and the digest binds exactly that.
                summary=_summary(presented),
                complete=complete,
                choices=["approve", "reject", "cancel"],
            )
        ]

    def _patch(self, params: dict) -> list[NativeEvent]:
        item_id = _identity(params.get("itemId"))
        changes = params.get("changes")
        if not isinstance(changes, list) or any(
            not isinstance(c, dict)
            or not isinstance(c.get("path"), str)
            or not isinstance(c.get("diff"), str)
            for c in changes
        ):
            raise ProtocolError("native file change patch is malformed")
        if item_id not in self._patches and len(self._patches) >= MAX_PENDING:
            raise ProtocolError("native file change capacity exceeded")
        latest = _copy_frame({"changes": changes})["changes"]
        unchanged = self._patches.get(item_id) == latest
        self._patches[item_id] = latest
        if unchanged:
            return []  # a repeated, identical proposal leaves the presented request standing
        # A patch that CHANGES after it was presented invalidates the pending approval: what
        # was shown is no longer what would be applied. Decline it rather than leave it open.
        stale = [
            key
            for key, approval in self._approvals.items()
            if approval["params"].get("itemId") == item_id and "digest" in approval
        ]
        out = []
        for key in stale:
            approval = self._approvals.pop(key)
            self._consumed.add(key)
            out.append(
                _event("send", frame={"id": approval["id"], "result": {"decision": "decline"}})
            )
            out.append(_event("approval_cancelled", request_id=key))
        return out

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
                "item/fileChange/patchUpdated",
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
        if method == "item/fileChange/patchUpdated":
            return self._patch(params)
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
                prior = []
                if kind == "fileChange" and isinstance(item.get("changes"), list):
                    prior = self._patch({"itemId": item_id, "changes": item["changes"]})
                return prior + [
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

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """``images``: ``(mime, base64)`` pairs, sent as base64 image blocks (#1332 Phase 3)."""
        if len(self._results) >= MAX_RESULTS:
            raise ProtocolError(
                "native result replay capacity exceeded; a new connection is required"
            )
        self._submit(text, operation_id, images)
        self.native_turn_id = self.operation_id
        content: str | list[dict] = text
        if images:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                for mime, data in images
            ]
            if text.strip():
                content.append({"type": "text", "text": text})
        return {
            "type": "user",
            "uuid": self.operation_id,
            "session_id": self.native_id,
            "message": {"role": "user", "content": content},
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
        # Everything the prompt carries (blocked_path, decision_reason, title, suggestions,
        # input …): the operator sees what the decision answers, not only the tool input.
        presented = {key: value for key, value in request.items() if key != "subtype"}
        digest = _digest(frame)
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
                "digest": digest,
                "complete": True,
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=digest,
                native_turn_id=self.native_turn_id,
                operation_id=self.operation_id,
                item_id=item_id,
                tool=tool_name,
                summary=_summary(presented),
                complete=True,
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


# --- opencode: the Agent Client Protocol over stdio (#1312) -------------------------------------
#
# Checked against opencode 1.18.35 (`opencode acp`, ACP protocol version 1; Phase 1 spike
# fixtures on #1312). JSON-RPC 2.0, one object per line. Our requests carry string ids; opencode's
# requests to us (`session/request_permission`, `fs/*`) carry its own integer ids.
OPENCODE_MIN_VERSION = (1, 18, 35)
ACP_PROTOCOL_VERSION = 1
#: The only primary agent an API client runs: BattleLab's own ask-everything agent, minted per
#: worker generation and defined in the child's `OPENCODE_CONFIG_CONTENT`
#: (`engines.opencode.api_config_content`, decision on #1312).
_OPENCODE_AGENT = re.compile(r"battlelab-api-[0-9a-f]{16}\Z", re.ASCII)
_OPENCODE_SESSION = re.compile(r"ses_[A-Za-z0-9]{1,250}\Z", re.ASCII)
_ACP_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
#: The one-shot permission kinds an operator decision can select. `allow_always` (a lasting
#: grant) and any kind this build does not know are never offered and never answerable.
_ONCE = {"approve": "allow_once", "reject": "reject_once"}


def opencode_session_id(value: Any) -> str:
    """An opencode session id (`ses_…`, never a UUID): where `_uuid` guards Claude's."""
    if not isinstance(value, str) or not _OPENCODE_SESSION.fullmatch(value):
        raise ProtocolError("expected an opencode session id")
    return value


def _safe_id(value: Any, prefix: str) -> str | None:
    """A native item id as a journal identity; an id outside the bounded alphabet is replaced by
    a stable digest instead of ending the connection (tool call ids are provider-chosen)."""
    if value is None:
        return None
    if isinstance(value, str) and _ID.fullmatch(value):
        return value
    if not isinstance(value, str | int):
        raise ProtocolError("invalid native item identity")
    return f"{prefix}-{hashlib.sha256(str(value).encode()).hexdigest()[:32]}"


def _acp_config(options: Any) -> dict[str, str]:
    """`configOptions` → {option id: current value}; malformed options are a protocol error."""
    if not isinstance(options, list):
        raise ProtocolError("native configOptions must be an array")
    out: dict[str, str] = {}
    for option in options[:64]:
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            raise ProtocolError("invalid native config option")
        if isinstance(option.get("currentValue"), str):
            out[option["id"]] = option["currentValue"]
    return out


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _acp_edit_context(tool_call: dict) -> bool:
    raw = tool_call.get("rawInput")
    if isinstance(raw, dict) and _nonempty(raw.get("filepath")) and _nonempty(raw.get("diff")):
        return True
    content = tool_call.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict)
        and block.get("type") == "diff"
        and _nonempty(block.get("path"))
        and isinstance(block.get("oldText"), str)
        and isinstance(block.get("newText"), str)
        for block in content
    )


#: Tool kinds whose request can be approved, each with what must be presented for it. Any other
#: kind (`think`, `switch_mode`, a kind a later opencode adds, none at all) is decline-only.
_ACP_APPROVABLE = {
    "execute": (
        lambda call: isinstance(call.get("rawInput"), dict)
        and _nonempty(call["rawInput"].get("command")),
        "the request does not say which command would run",
    ),
    "edit": (_acp_edit_context, "the request does not carry the file and its diff"),
    **{
        kind: (
            lambda call: isinstance(call.get("rawInput"), dict) and bool(call["rawInput"]),
            "the request does not say what the tool would do",
        )
        for kind in ("read", "search", "fetch", "delete", "move", "other")
    },
}


def _acp_unpresentable(tool_call: dict) -> str | None:
    """Why a permission request cannot be approved as presented, or None when it can."""
    rule = _ACP_APPROVABLE.get(tool_call.get("kind"))
    if rule is None:
        return "this kind of tool call cannot be approved from BattleLab"
    check, reason = rule
    return None if check(tool_call) else reason


class OpencodeAcpCodec(_Codec):
    """One `opencode acp` connection: one session, one prompt turn at a time.

    ACP has no server-side turn id: like Claude, the submitted operation id is the turn. The
    session runs only in the mode pinned to BattleLab's own agent (``agent``); a mode change
    observed after the pin ends the connection. Permission requests become exact one-shot
    approvals (allow_once / reject_once); a request is live only on the connection that asked it,
    and `session/load` replays history without ever re-asking one.
    """

    def __init__(self, session_id: str | None = None, *, agent: str) -> None:
        super().__init__()
        if not isinstance(agent, str) or not _OPENCODE_AGENT.fullmatch(agent):
            raise ProtocolError("expected BattleLab's own opencode agent")
        self.agent = agent
        self.native_id = None if session_id is None else opencode_session_id(session_id)
        self.version: tuple[int, int, int] | None = None
        self._pinned = False  # the mode pin was confirmed; later mode changes are violations
        self._config_requests: dict[str, tuple[str, str]] = {}

    @staticmethod
    def argv(binary: str) -> list[str]:
        # The listener `opencode acp` opens is pinned to loopback ON THE COMMAND LINE (Hermes on
        # #1336): without flags 1.18.35 takes hostname/port/mdns from the operator's global
        # `server.*` config, and `server.mdns: true` (or `hostname: 0.0.0.0`) binds every
        # interface — measured. Flags win over that config; `--mdns=false` and `--no-mdns` are
        # both honoured. It stays password-gated (OPENCODE_SERVER_PASSWORD in the environment).
        return [_path(binary), "acp", "--hostname", "127.0.0.1", "--port", "0", "--mdns=false"]

    @staticmethod
    def config_probe_argv(binary: str) -> list[str]:
        """The resolved configuration of a session's directory (no listener, no session)."""
        return [_path(binary), "debug", "config"]

    def _request(self, method: str, params: dict[str, Any], op: str | None = None) -> dict:
        return {"jsonrpc": "2.0", "id": self._next(method, op), "method": method, "params": params}

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                # BattleLab serves no files and no terminals to the agent.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )

    def create(self, cwd: str) -> dict:
        if self.native_id is not None:
            raise ProtocolError("native session is already bound")
        return self._request("session/new", {"cwd": _path(cwd), "mcpServers": []})

    def load(self, session_id: str, cwd: str) -> dict:
        session_id = opencode_session_id(session_id)
        if self.native_id not in (None, session_id):
            raise ProtocolError("native session identity changed")
        self.native_id = session_id
        frame = self._request(
            "session/load", {"sessionId": session_id, "cwd": _path(cwd), "mcpServers": []}
        )
        self._expected_sessions[frame["id"]] = session_id
        return frame

    def _set(self, option: str, value: str) -> dict:
        if self.native_id is None:
            raise ProtocolError("native session is not bound")
        frame = self._request(
            "session/set_config_option",
            {"sessionId": self.native_id, "configId": option, "value": value},
        )
        self._config_requests[frame["id"]] = (option, value)
        return frame

    def pin_mode(self) -> dict:
        return self._set("mode", self.agent)

    def set_model(self, model: str) -> dict:
        if _model(model) is None:
            raise ProtocolError("no model to select")
        return self._set("model", model)

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """Text only: this kind declares no image input (`kinds.API_IMAGE_INPUT`), so the runtime
        refuses pictures before a claim. ACP's `{"type":"image"}` prompt blocks, gated by the
        agent's `promptCapabilities.image`, are the follow-up (#1332)."""
        if self.native_id is None or not self._pinned:
            raise ProtocolError("native session is not bound in its pinned mode")
        if images:
            raise ProtocolError("this client takes no images")
        self._submit(text, operation_id)
        self.native_turn_id = self.operation_id
        return self._request(
            "session/prompt",
            {"sessionId": self.native_id, "prompt": [{"type": "text", "text": text}]},
            self.operation_id,
        )

    def interrupt(self) -> dict:
        if self.native_id is None or self.operation_id is None:
            raise ProtocolError("no correlated native turn to interrupt")
        return {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": self.native_id},
        }

    def cancel_pending(self) -> list[NativeEvent]:
        """After `session/cancel`, ACP requires every pending permission request to be answered
        `cancelled`. Each is consumed: nothing can approve it afterwards."""
        out = []
        for key in list(self._approvals):
            pending = self._approvals.pop(key)
            self._consumed.add(key)
            out.append(
                _event(
                    "send",
                    frame={
                        "jsonrpc": "2.0",
                        "id": pending["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    },
                )
            )
            out.append(_event("approval_cancelled", request_id=key))
        return out

    def decide(self, request_id: str, decision: str) -> dict:
        if decision not in _ONCE:
            # One-shot allow or reject only: no "always", no policy-amending variant.
            raise ProtocolError("only a one-shot allow or reject is supported")
        item = self._approvals.get(request_id)
        if item is not None and decision == "approve" and item.get("complete") is not True:
            raise ProtocolError("this request could not be presented completely")
        pending = self._decision(request_id, decision)
        option = pending["options"].get(_ONCE[decision])
        if option is None and decision == "approve":
            raise ProtocolError("the agent offered no one-shot approval")  # pragma: no cover
        outcome = (
            {"outcome": "selected", "optionId": option}
            if option is not None
            else {"outcome": "cancelled"}  # no reject_once offered: a refusal all the same
        )
        return {"jsonrpc": "2.0", "id": pending["id"], "result": {"outcome": outcome}}

    # --- observations -----------------------------------------------------------------------

    def _mode_check(self, config: dict[str, str]) -> None:
        mode = config.get("mode")
        if self._pinned and mode is not None and mode != self.agent:
            raise ProtocolError("the agent left its pinned mode")

    def _turn_done(self, state: str, error: str = "") -> list[NativeEvent]:
        data = {"native_turn_id": self.native_turn_id, "operation_id": self.operation_id}
        self.operation_id = None
        self.native_turn_id = None
        self._approvals.clear()
        return [_event("turn_completed", **data, state=state, error=error)]

    def _reply(self, frame: dict) -> list[NativeEvent]:
        request_id = frame.get("id")
        pending = self._requests.pop(request_id, None) if isinstance(request_id, str) else None
        if pending is None:
            return []
        action, op = pending
        expected_session = self._expected_sessions.pop(request_id, None)
        config_request = self._config_requests.pop(request_id, None)
        prompt = action == "session/prompt" and op is not None and op == self.operation_id
        if "error" in frame:
            error = frame["error"]
            message = (
                _text(error.get("message")) if isinstance(error, dict) else "Native request failed"
            )
            if prompt:
                # A prompt can fail after tools ran: the turn ended, it did not "never start".
                return self._turn_done("failed", message or "the agent failed this turn")
            return [
                _event(
                    "error", request_id=request_id, action=action, operation_id=op, message=message
                )
            ]
        result = frame.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("native response has no result object")
        if action == "initialize":
            info = result.get("agentInfo")
            found = (
                _ACP_VERSION.match(info.get("version", ""))
                if isinstance(info, dict) and isinstance(info.get("version"), str)
                else None
            )
            version = tuple(int(x) for x in found.groups()) if found else None
            capabilities = result.get("agentCapabilities")
            refusal = None
            if result.get("protocolVersion") != ACP_PROTOCOL_VERSION:
                refusal = "the agent speaks another ACP protocol version"
            elif version is None or version < OPENCODE_MIN_VERSION:
                refusal = (
                    f"opencode {'.'.join(map(str, OPENCODE_MIN_VERSION))} or later is required"
                )
            elif not isinstance(capabilities, dict) or capabilities.get("loadSession") is not True:
                refusal = "the agent cannot load an existing session"
            if refusal is not None:
                return [_event("error", request_id=request_id, action=action, message=refusal)]
            self.version = version
            return [_event("initialized", request_id=request_id, action=action)]
        if action in {"session/new", "session/load"}:
            native_id = (
                opencode_session_id(result.get("sessionId"))
                if action == "session/new"
                else expected_session
            )
            if native_id is None or (self.native_id is not None and self.native_id != native_id):
                raise ProtocolError("native session identity changed")
            self.native_id = native_id
            config = _acp_config(result.get("configOptions", []))
            return [
                _event(
                    "session",
                    request_id=request_id,
                    action=action,
                    native_id=native_id,
                    model_configured=_observed_model(config.get("model")),
                    model_effective=None,
                )
            ]
        if action == "session/set_config_option" and config_request is not None:
            option, value = config_request
            config = _acp_config(result.get("configOptions"))
            if config.get(option) != value:
                return [
                    _event(
                        "error",
                        request_id=request_id,
                        action=action,
                        message=f"the agent did not select the requested {option}",
                    )
                ]
            if option == "mode":
                self._pinned = True
            self._mode_check(config)
            return [
                _event(
                    "session",
                    request_id=request_id,
                    action=action,
                    native_id=self.native_id,
                    model_configured=_observed_model(config.get("model")),
                    model_effective=None,
                )
            ]
        if prompt:
            reason = result.get("stopReason")
            if reason == "end_turn":
                return self._turn_done("completed")
            if reason == "cancelled":
                return self._turn_done("interrupted")
            return self._turn_done("failed", f"the agent stopped: {_text(reason, limit=64)}")
        return [_event("response", request_id=request_id, action=action)]

    def _refuse(self, frame: dict) -> list[NativeEvent]:
        return [
            _event(
                "send",
                frame={
                    "jsonrpc": "2.0",
                    "id": _rpc_id(frame.get("id")),
                    "error": {"code": -32601, "message": "Unsupported client method"},
                },
            )
        ]

    def _approval(self, frame: dict, params: dict) -> list[NativeEvent]:
        _rpc_id(frame.get("id"))
        if (
            self.operation_id is None
            or self.native_id is None
            or params.get("sessionId") != self.native_id
        ):
            # Not this connection's live turn (an idle or replayed callback): refuse the tool.
            return [
                _event(
                    "send",
                    frame={
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    },
                )
            ]
        tool_call = params.get("toolCall")
        options = params.get("options")
        if not isinstance(tool_call, dict) or not isinstance(options, list):
            raise ProtocolError("native permission request is malformed")
        offered: dict[str, list[str]] = {}
        for option in options[:16]:
            if (
                isinstance(option, dict)
                and option.get("kind") in _ONCE.values()
                and isinstance(option.get("optionId"), str)
                and 0 < len(option["optionId"]) <= 256
            ):
                offered.setdefault(option["kind"], []).append(option["optionId"])
        # Exactly one of each one-shot kind, or that choice is not answerable at all.
        once = {kind: ids[0] for kind, ids in offered.items() if len(ids) == 1}
        item_id = _safe_id(tool_call.get("toolCallId"), "tool")
        if item_id is None:
            raise ProtocolError("native permission request names no tool call")
        # Everything the decision answers: the tool call as asked (title, kind, locations, the
        # complete rawInput — a command, or a file path and its whole diff — and its content,
        # including the diff's old and new text), plus the one-shot options a decision can pick.
        presented: dict[str, Any] = {
            "toolCall": tool_call,
            "options": [
                {"optionId": option_id, "kind": kind} for kind, option_id in sorted(once.items())
            ],
        }
        # Approve needs the consequential action itself in what is presented (Hermes on #1336):
        # an allow_once option alone would approve a blind request.
        reason = _acp_unpresentable(tool_call)
        if reason is None and "allow_once" not in once:
            reason = "the agent offered no single one-shot approval"
        if reason is not None:
            presented["declineOnly"] = reason
        complete = reason is None
        if len(_summary(presented)) >= MAX_TEXT:
            # Too large to present whole: shown in outline, and it can only be refused.
            presented = {
                "toolCall": {
                    k: tool_call[k]
                    for k in ("toolCallId", "title", "kind", "locations")
                    if k in tool_call
                },
                "omitted": "the request is too large to present in full; it can only be declined",
            }
            if len(_summary(presented)) >= MAX_TEXT:
                presented = {"omitted": "the request is too large to present"}
            complete = False
        digest = _digest({"frame": frame, "presented": presented})
        key = self._remember(
            frame["id"],
            {
                "id": frame["id"],
                "operation_id": self.operation_id,
                "native_turn_id": self.native_turn_id,
                "item_id": item_id,
                "options": once,
                "frame": frame,
                "digest": digest,
                "complete": complete,
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=digest,
                native_turn_id=self.native_turn_id,
                operation_id=self.operation_id,
                item_id=item_id,
                tool=_safe_id(tool_call.get("kind"), "kind") or "tool",
                summary=_summary(presented),
                complete=complete,
                choices=["approve", "reject"] if complete else ["reject"],
            )
        ]

    def _update(self, update: dict) -> list[NativeEvent]:
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update":
            self._mode_check({"mode": update.get("currentModeId")})
            return []
        if kind == "config_option_update":
            self._mode_check(_acp_config(update.get("configOptions", [])))
            return []
        if self.operation_id is None:
            # Replayed history (`session/load`) and idle chatter: BattleLab's own journal already
            # holds what it observed; a replay is never attributed to a turn.
            return []
        common = {"native_turn_id": self.native_turn_id, "operation_id": self.operation_id}
        if kind == "agent_message_chunk":
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text":
                return []
            item = _safe_id(update.get("messageId"), "msg")
            return [
                _event(
                    "text",
                    **common,
                    **({"item_id": item} if item is not None else {}),
                    text=_text(content.get("text")),
                    partial=True,
                    truncated=isinstance(content.get("text"), str)
                    and len(content["text"]) > MAX_TEXT,
                )
            ]
        if kind in {"tool_call", "tool_call_update"}:
            item_id = _safe_id(update.get("toolCallId"), "tool")
            if item_id is None:
                raise ProtocolError("native tool update names no tool call")
            status = _text(update.get("status"), limit=64)
            data: dict[str, Any] = {"item_id": item_id, "state": status}
            tool = _safe_id(update.get("kind"), "kind")
            if tool is not None:
                data["tool"] = tool
            if isinstance(update.get("title"), str):
                data["summary"] = _text(update["title"])
            if isinstance(update.get("content"), list):
                texts = [
                    block["content"].get("text")
                    for block in update["content"][:32]
                    if isinstance(block, dict)
                    and block.get("type") == "content"
                    and isinstance(block.get("content"), dict)
                    and isinstance(block["content"].get("text"), str)
                ]
                if texts:
                    data["output"] = _text("\n".join(texts))
            data["completed"] = status in {"completed", "failed"}
            return [_event("tool", **common, **data)]
        return []

    def feed(self, message: dict[str, Any]) -> list[NativeEvent]:
        if self._closed:
            raise ProtocolError("native connection is closed")
        frame = _copy_frame(message)
        if frame.get("jsonrpc") != "2.0":
            raise ProtocolError("native frame is not JSON-RPC 2.0")
        method = frame.get("method")
        if method is not None and not isinstance(method, str):
            raise ProtocolError("native method must be a string")
        if method is None:
            return self._reply(frame)
        params = frame.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError("native notification params must be an object")
        if "id" in frame:
            if method == "session/request_permission":
                return self._approval(frame, params)
            return self._refuse(frame)  # fs/*, terminal/* and anything else: never served
        if method != "session/update" or self.native_id is None:
            return []
        if params.get("sessionId") != self.native_id:
            return []
        update = params.get("update")
        if not isinstance(update, dict):
            raise ProtocolError("native session update must be an object")
        return self._update(update)
