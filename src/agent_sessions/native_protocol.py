"""Bounded, transport-free codecs for the reviewed native protocols (#1278).

The worker owns admission, durable identities, timeouts and process containment. A
frame returned here is only a proposal to write: the caller must admit and journal
it first. There are no retries. EOF is never completion. Protocol observations are
allowlisted; original permission input stays private until a one-operation reply.

Wire shapes are based on Codex app-server 0.159.3's generated v2 schemas and the
Claude Agent SDK's stream-JSON control protocol. Claude CLI 2.1.248 or later is
required for client_composed (verbatim input); the complete reviewed argv requires
2.1.287 or later, matching the CLI help used here. Explicit session_id and human
origin are supported SDK input fields. Background task observations retain only bounded display
metadata; their state does not add turn ownership or interrupt authority.
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

from . import native_questions

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
# Display observations (#1461), shared with the closed IPC schema. Native task payloads and
# output-file paths never cross that boundary; a cut text value visibly ends with an ellipsis.
TASK_METADATA_LIMITS = {
    "description": 1000,
    "task_type": 128,
    "last_tool_name": 128,
    "summary": 2000,
}
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "stopped", "killed"})
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z", re.ASCII)
#: A reasoning level (#1407): the grammar `native_models` lists and a create admits.
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)


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


def _effort(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or not _EFFORT.fullmatch(value)):
        raise ProtocolError("invalid effort level")
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


def _observed_effort(value: Any) -> str | None:
    # The level a client REPORTS it runs at (#1511); anything not shaped like a level is no report.
    return value if isinstance(value, str) and _EFFORT.fullmatch(value) else None


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
    # The worker owns selection/queueing; codecs only map native effects (#1389).
    send_now_mode = "interrupt"
    # Send now against a turn the agent runs for itself (#1427): always an interrupt, since a
    # steered reply would land in a turn no operator message owns. None: no such turn exists.
    send_now_background: str | None = None
    # `submit_now` cuts a turn the agent runs for itself, so a message Send now selected need not
    # wait behind one (#1475). False: it is written like any other message.
    submit_now_cuts_own_turn = False

    def __init__(self) -> None:
        # A turn the agent runs that no operator command owns, and its Send now target: minted
        # when it starts, cleared when it ends (#1427).
        self.unowned_turn = False
        self.background_turn: str | None = None
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

    def submit_now(self, text: str, operation_id: str, images=()) -> dict:
        """The message a Send now selected, written once the turn it cut has ended (#1475). An
        agent with no queue of its own to jump sends it like any other."""
        return self.submit(text, operation_id, images)

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

    #: The adapter kind ``native_grants`` derives standing grants for (#1339).
    ADAPTER = ""

    def _always(self, request_id: str, grant: Any, cwd: str | None) -> Any:
        """The exact native grant for an ``always`` decision, re-derived from the request THIS
        connection holds (#1339): never trusted from the caller, refused unless the request itself
        proposed it."""
        from . import native_grants

        item = self._approvals.get(request_id)
        if (
            item is None
            or item.get("complete") is not True
            or item.get("kind") == native_questions.ASK_TOOL  # a question grants nothing (#1463)
            or not native_grants.valid_grant_id(grant)
        ):
            raise ProtocolError("that standing grant is not offered for this request")
        derived = native_grants.derive(self.ADAPTER, item.get("kind"), item.get("presented"), cwd)
        match = next((c for c in derived["always"] if c["id"] == grant), None)
        if match is None:
            raise ProtocolError("that standing grant is not offered for this request")
        return match["grant"]

    def check_answer(self, request_id: str, answers: Any) -> dict[str, str]:
        """An agent question's answer (#1463), checked against the request this connection holds.
        Only a client whose protocol takes one overrides this."""
        raise ProtocolError("this agent's requests take no answer")

    def _decision(self, request_id: str, decision: str, *, answer: bool = False) -> dict[str, Any]:
        if not isinstance(decision, str) or decision not in {
            "approve",
            "reject",
            "cancel",
            "always",
            *({"answer"} if answer else ()),
        }:
            raise ProtocolError("unsupported permission decision")
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


#: The approval kind of an MCP tool call Codex asks about (#1506), and the elicitation action each
#: offered decision answers with (once only — see `CodexCodec.decide`).
MCP_TOOL = "mcp_tool"
_MCP_ACTIONS = {"approve": "accept", "reject": "decline"}


class CodexCodec(_Codec):
    send_now_mode = "steer"
    send_now_background = "interrupt"
    ADAPTER = "codex-app-server"

    def __init__(self, effort: str | None = None) -> None:
        super().__init__()
        # The session's requested level (#1407). Codex takes it per TURN (`TurnStartParams.effort`,
        # v2 schema), so it rides on every `turn/start` this connection sends.
        self.effort = _effort(effort)
        self._steers: dict[str, str] = {}
        self._unsolicited: str | None = None  # the native id of a turn the agent started itself
        self._early_frames: list[dict[str, Any]] = []
        self._early_size = 0
        # The latest proposed changes per file-change item of the active turn. A file-change
        # approval request names only its item; the patch arrives separately and is what an
        # approval would actually authorize.
        self._patches: dict[str, list[dict[str, Any]]] = {}
        # The MCP tool calls of the active turn still in progress (#1506): item id → server, tool
        # and arguments. An `mcpServer/elicitation/request` names only the server and the
        # arguments; it is presented (and approvable) only when exactly one of these matches.
        self._mcp_calls: dict[str, dict[str, Any]] = {}

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

    def create(
        self,
        cwd: str,
        model: str | None = None,
        *,
        bypass: bool = False,
        session: str | None = None,
        shell_env: dict[str, str] | None = None,
        mcp_config: dict | None = None,
    ) -> dict:
        if self.native_id is not None:
            raise ProtocolError("native thread is already bound")
        params = self.create_params(
            cwd, model, bypass=bypass, session=session, shell_env=shell_env, mcp_config=mcp_config
        )
        return self._request("thread/start", params)

    def read(self, thread_id: str) -> dict:
        frame = self._request(
            "thread/read", {"threadId": _identity(thread_id), "includeTurns": True}
        )
        self._expected_sessions[frame["id"]] = thread_id
        return frame

    def resume(
        self,
        thread_id: str,
        cwd: str,
        model: str | None = None,
        *,
        bypass: bool = False,
        session: str | None = None,
        shell_env: dict[str, str] | None = None,
        mcp_config: dict | None = None,
    ) -> dict:
        params = self.create_params(
            cwd, model, bypass=bypass, session=session, shell_env=shell_env, mcp_config=mcp_config
        )
        params["threadId"] = _identity(thread_id)
        # Never hydrate the full history into one frame: a long thread exceeds the frame bound
        # and could then never be resumed. BattleLab's own journal already holds what it saw.
        params["excludeTurns"] = True
        frame = self._request("thread/resume", params)
        self._expected_sessions[frame["id"]] = thread_id
        return frame

    @staticmethod
    def create_params(
        cwd: str,
        model: str | None,
        *,
        bypass: bool = False,
        session: str | None = None,
        shell_env: dict[str, str] | None = None,
        mcp_config: dict | None = None,
    ) -> dict[str, Any]:
        # Skip-permissions (#1339) is the structured twin of the console's
        # `--dangerously-bypass-approvals-and-sandbox`: Codex never asks and runs unsandboxed.
        params: dict[str, Any] = {
            "cwd": _path(cwd),
            "approvalPolicy": "never" if bypass is True else "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "danger-full-access" if bypass is True else "read-only",
        }
        if _model(model) is not None:
            params["model"] = model
        if session is not None:
            # #1406: an operator `shell_environment_policy` (`inherit = "core"/"none"`, `exclude`)
            # would strip the child's BATTLELAB_SESSION from the agent's tool shell. The dotted
            # key merges into the operator's `set` table and outranks their config (codex 0.162,
            # start and resume). An `include_only` allowlist still wins; it is not rewritten.
            params["config"] = {"shell_environment_policy.set.BATTLELAB_SESSION": session}
        if shell_env:
            # The shared project memory paths (#1441) ride the same `set` table, for the same
            # reason: an operator policy must not strip them from the agent's tool shell.
            params.setdefault("config", {}).update(
                {f"shell_environment_policy.set.{name}": value for name, value in shell_env.items()}
            )
        if mcp_config:
            # The memory tool (#1506): this thread's own `mcp_servers.<name>` entry
            # (`agent_tools.codex_thread_config`), merged by name over the operator's servers.
            # Independent of the `shell_environment_policy` keys (proven alone on 0.162.0).
            params.setdefault("config", {}).update(mcp_config)
        return params

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """``images``: ``(mime, base64)`` pairs, sent inline as data URLs (#1332 Phase 3)."""
        if self.native_id is None:
            raise ProtocolError("native thread is not bound")
        self._submit(text, operation_id, images)
        params: dict[str, Any] = {
            "threadId": self.native_id,
            "input": self._input(text, images),
            "clientUserMessageId": self.operation_id,
        }
        if self.effort is not None:
            params["effort"] = self.effort
        return self._request("turn/start", params, self.operation_id)

    @staticmethod
    def _input(text: str, images) -> list[dict]:
        items: list[dict] = [
            {"type": "image", "url": f"data:{mime};base64,{data}"} for mime, data in images
        ]
        if text.strip():
            items.append({"type": "text", "text": text})
        return items

    def steer(self, text: str, operation_id: str, images=()) -> dict:
        """Append input to the exact native turn, keeping its output correlation unchanged."""
        if self.native_id is None or self.native_turn_id is None or self.operation_id is None:
            raise ProtocolError("no correlated native turn to steer")
        validate_text(text, allow_empty=bool(images))
        frame = self._request(
            "turn/steer",
            {
                "threadId": self.native_id,
                "expectedTurnId": self.native_turn_id,
                "input": self._input(text, images),
                "clientUserMessageId": _uuid(operation_id),
            },
            operation_id,
        )
        self._steers[frame["id"]] = self.native_turn_id
        return frame

    def interrupt(self) -> dict:
        if self.native_id is None or self.native_turn_id is None or self.operation_id is None:
            raise ProtocolError("no correlated native turn to interrupt")
        return self._request(
            "turn/interrupt", {"threadId": self.native_id, "turnId": self.native_turn_id}
        )

    def interrupt_background(self, target: str) -> dict:
        """Stop the turn the agent started itself, by ITS native id; never a steer (#1427)."""
        if (
            self.native_id is None
            or self.operation_id is not None
            or self._unsolicited is None
            or target is None
            or target != self.background_turn
        ):
            raise ProtocolError("no turn of the agent's own to interrupt")
        return self._request(
            "turn/interrupt", {"threadId": self.native_id, "turnId": self._unsolicited}
        )

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        grant: str | None = None,
        cwd: str | None = None,
        answers: Any = None,
    ) -> dict:
        if answers is not None:
            raise ProtocolError("this agent's requests take no answer")
        held = self._approvals.get(request_id)
        if held is not None and held.get("kind") == MCP_TOOL:
            # An MCP tool approval (#1506) is answered as the elicitation it is, ONCE: Codex
            # offers `persist` scopes, but its schema defines no answer that selects one, and
            # "always" would be written into the operator's own Codex config. Decline and
            # approve only; anything else is refused before the request is consumed.
            if decision not in _MCP_ACTIONS:
                raise ProtocolError("an MCP tool request is allowed once or denied")
            pending = self._decision(request_id, decision)
            return {
                "id": pending["id"],
                "result": {"action": _MCP_ACTIONS[decision], "content": {}},
            }
        # `always` (#1339): the exact `acceptWithExecpolicyAmendment` object / `acceptForSession`
        # the request itself listed, re-derived before anything is consumed.
        native = self._always(request_id, grant, cwd) if decision == "always" else None
        pending = self._decision(request_id, decision)
        return {
            "id": pending["id"],
            "result": {
                "decision": native
                if decision == "always"
                else {"approve": "accept", "reject": "decline", "cancel": "cancel"}[decision]
            },
        }

    def _turn(self, turn: dict, *, completed: bool = False) -> list[NativeEvent]:
        turn_id = _identity(turn.get("id"))
        if self.operation_id is None or (
            self.native_turn_id is not None and self.native_turn_id != turn_id
        ):
            state = _text(turn.get("status"), limit=64)
            if self.operation_id is None and not completed and self._unsolicited is None:
                # A turn the agent started itself (#1427): operator messages wait behind it, and
                # it is the Send now target until it ends.
                self._unsolicited = turn_id
                self.unowned_turn = True
                self.background_turn = str(uuid.uuid4())
                return [
                    _event(
                        "background",
                        native_turn_id=turn_id,
                        state=state,
                        active=True,
                        background_turn=self.background_turn,
                    )
                ]
            if completed and turn_id == self._unsolicited:
                self._unsolicited = None
                self.unowned_turn = False
                self.background_turn = None
                return [
                    _event(
                        "background",
                        native_turn_id=turn_id,
                        state=state,
                        active=False,
                        background_turn=None,
                    )
                ]
            return [_event("background", native_turn_id=turn_id, state=state)]
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
        self._mcp_calls.clear()
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
        steered_turn = self._steers.pop(request_id, None)
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
        if action == "turn/steer":
            if steered_turn is None or result.get("turnId") != steered_turn:
                raise ProtocolError("native steering response does not match its turn")
            return [_event("input_accepted", operation_id=op, native_turn_id=steered_turn)]
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
                    effort_configured=_observed_effort(result.get("reasoningEffort")),
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

    def _refuse(self, request_id: Any) -> list[NativeEvent]:
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

    def _mcp_approval(self, frame: dict, params: dict) -> list[NativeEvent]:
        """`mcpServer/elicitation/request` as an MCP tool approval (#1506), the shape codex-cli
        0.162.0 sends (recorded in `tests/fixtures/codex_mcp_elicitation.json`): a `form`
        elicitation whose `_meta.codex_approval_kind` is `mcp_tool_call`, with an EMPTY
        requested schema (it asks for a decision, never for data) and the call's arguments in
        `_meta.tool_params`. It is bound to the one in-progress `mcpToolCall` item of this turn
        with the same server and arguments, which names the tool. Anything else — another
        elicitation mode or kind, a form asking for input, an uncorrelated turn or call — is
        refused exactly as an unknown request is: Codex reads that as a rejection."""
        request_id = _rpc_id(frame.get("id"))
        meta = params.get("_meta")
        server = params.get("serverName")
        if (
            self.operation_id is None
            or params.get("threadId") != self.native_id
            or params.get("turnId") != self.native_turn_id
            or params.get("mode") != "form"
            or not isinstance(meta, dict)
            or meta.get("codex_approval_kind") != "mcp_tool_call"
            or params.get("requestedSchema") != {"type": "object", "properties": {}}
            or not isinstance(server, str)
            or not _ID.fullmatch(server)
            or not isinstance(meta.get("tool_params"), dict)
        ):
            return self._refuse(request_id)
        arguments = meta["tool_params"]
        calls = [
            (item_id, call)
            for item_id, call in self._mcp_calls.items()
            if call["server"] == server and call["arguments"] == arguments
        ]
        if len(calls) != 1:
            return self._refuse(request_id)
        item_id, call = calls[0]
        # What the operator decides on: server, tool and the complete arguments. Shown in full
        # or not approvable — an over-long request stays declineable (decline needs no review).
        presented: dict[str, Any] = {
            "server": server,
            "tool": call["tool"],
            "input": arguments,
            "message": _text(params.get("message"), limit=500),
        }
        complete = len(_summary(presented)) < MAX_TEXT
        if not complete:
            presented = {"server": server, "tool": call["tool"]}
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
                "presented": presented,
                "kind": MCP_TOOL,
                "item_id": item_id,
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
                tool=MCP_TOOL,
                summary=_summary(presented),
                complete=complete,
                choices=list(_MCP_ACTIONS),
            )
        ]

    def _approval(self, frame: dict, params: dict) -> list[NativeEvent]:
        request_id = _rpc_id(frame.get("id"))
        method = frame.get("method")
        if method == "mcpServer/elicitation/request":
            return self._mcp_approval(frame, params)
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
                "presented": presented,
                "kind": "command"
                if method == "item/commandExecution/requestApproval"
                else "file_change",
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

    def _track_mcp_call(self, item_id: str, item: dict, *, done: bool) -> None:
        """Remember an in-progress MCP tool call of this turn (bounded), forget a finished one."""
        if done:
            self._mcp_calls.pop(item_id, None)
            return
        server, tool = item.get("server"), item.get("tool")
        if (
            not isinstance(server, str)
            or not isinstance(tool, str)
            or not _ID.fullmatch(server)
            or not _ID.fullmatch(tool)
            or not isinstance(item.get("arguments"), dict)
        ):
            return  # never presentable: its elicitation is refused
        if item_id not in self._mcp_calls and len(self._mcp_calls) >= MAX_PENDING:
            raise ProtocolError("native MCP call capacity exceeded")
        self._mcp_calls[item_id] = {
            "server": server,
            "tool": tool,
            "arguments": _copy_frame({"a": item["arguments"]})["a"],
        }

    def _forget_patch(self, item_id: str) -> None:
        """A completed file change has nothing left to present or invalidate: forget its patch.

        Only in-progress changes count toward MAX_PENDING. Kept until turn end, every edit of a long
        turn filled it, and the 129th killed the worker mid-turn ("native file change capacity
        exceeded", codex-api 9471d32e: two turns of exactly 128 edits). A patch an approval still
        presents is kept: that approval is settled or cancelled first, as before."""
        if any(a["params"].get("itemId") == item_id for a in self._approvals.values()):
            return
        self._patches.pop(item_id, None)

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
                "mcpServer/elicitation/request",
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
                # An async question (#1463) carries its options as data. Codex is not waiting
                # for them: the operator's next message is the answer.
                questions = (
                    native_questions.async_questions(item.get("questions"))
                    if method == "item/completed" and item.get("delivery") == "async"
                    else None
                )
                return [
                    _event(
                        "text",
                        **common,
                        item_id=item_id,
                        text=_text(item.get("text")),
                        partial=method == "item/started",
                        truncated=isinstance(item.get("text"), str)
                        and len(item["text"]) > MAX_TEXT,
                        **({"questions": questions} if questions else {}),
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
                if kind == "fileChange" and method == "item/completed":
                    self._forget_patch(item_id)
                if kind == "mcpToolCall":
                    self._track_mcp_call(item_id, item, done=method == "item/completed")
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


# How the CLI's lifecycle ends an input: answered, or interrupted (#1427).
_LIFECYCLE_ENDED = frozenset({"completed", "cancelled"})
# What may come between an input's `completed` and the result naming it without ending the wait
# (#1644): bookkeeping that is no turn's output. Anything else ends the input without its result.
_BETWEEN_END_AND_RESULT = frozenset(
    {"task_started", "task_progress", "task_notification", "task_updated", "thinking_tokens"}
)


def _between_end_and_result(frame: dict) -> bool:
    kind = frame.get("type")
    if kind in {"rate_limit_event", "control_response"}:
        return True
    return kind == "system" and frame.get("subtype") in _BETWEEN_END_AND_RESULT


_DROPPED = frozenset({"cancelled", "discarded"})  # an input the CLI never ran


def _result_state(frame: dict) -> str:
    """How a result ended its turn. An interrupt is reported as an error result
    (`error_during_execution`) whose terminal reason is the abort: it is interrupted, and its
    diagnostic no failure."""
    if frame.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
        return "interrupted"
    if frame["is_error"] or frame.get("subtype") != "success":
        return "failed"
    if frame.get("terminal_reason") not in (None, "completed"):
        return "failed"
    return "completed"


class ClaudeCodec(_Codec):
    """One serial native connection, with result replay detection across its submitted turns.

    Claude does not expose a submitted-operation id on results: native_turn_id below is our
    submitted user-message UUID. An unseen human result is correlated by the native stream's
    serial ordering, not by an invented echo-order guarantee. Result UUIDs are retained without
    eviction until connection close; a full ledger refuses further submissions. A missing UUID
    cannot support this guarantee even though the SDK's compatibility type permits its absence.
    """

    ADAPTER = "claude-stream-json"
    send_now_background = "interrupt"
    submit_now_cuts_own_turn = True

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.native_id = _uuid(session_id)
        self._background: set[str] = set()
        self._session_state: str | None = None
        self._results: dict[str, str] = {}
        # The command lifecycle (#1427). The CLI frames every input it is given with
        # `command_lifecycle` queued → started → completed carrying the uuid we submitted, and a
        # turn it starts itself (a task notification) with none. Output belongs to an operator
        # turn only inside its own started window. Unknown per generation until one complete,
        # correlated cycle is seen; a frame outside the cycle makes it unknown again.
        self.lifecycle_proven = False
        self._cycle: str | None = None  # the operator command: written | queued | started
        self._closing: str | None = None  # a command whose result came, awaiting `completed`
        self._tainted = False  # this command's cycle saw an anomaly: it proves nothing
        # The CLI's own Send now (#1526): `system/init` advertises `interrupt_send_now_v1`. A plain
        # interrupt is a Stop, which also ends every background agent; a `send_now` one cuts only
        # the running turn for a message already written to the CLI.
        self.native_send_now = False
        # That message, written while the operator turn it cuts still runs: it becomes the
        # operation when that turn ends. Its lifecycle state: written | queued.
        self._ahead: str | None = None
        self._ahead_cycle: str | None = None

    @staticmethod
    def argv(
        binary: str,
        session_id: str,
        *,
        resume: bool = False,
        model: str | None = None,
        bypass: bool = False,
        effort: str | None = None,
        mcp_config: str | None = None,
        add_dirs: tuple[str, ...] = (),
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
            # Skip-permissions (#1339): fixed per session, so every generation gets the same mode.
            "bypassPermissions" if bypass is True else "default",
            "--include-partial-messages",
            f"--{'resume' if resume else 'session-id'}={native_id}",
        ]
        if _model(model) is not None:
            argv.extend(["--model", model])
        # Every generation of the session gets the level it was created with (#1407).
        if _effort(effort) is not None:
            argv.extend(["--effort", effort])
        # The secrets tool's copy directory (#1786, `agent_tools.claude_add_dirs`): exactly
        # ``("--add-dir", <dir>)`` or nothing.
        if add_dirs:
            if len(add_dirs) != 2 or add_dirs[0] != "--add-dir":
                raise ValueError("add_dirs is ('--add-dir', <absolute dir>)")
            argv.extend(["--add-dir", _path(add_dirs[1])])
        if mcp_config is not None:
            # The memory tool (#1506, `agent_tools.claude_mcp_config`). LAST: the flag takes
            # several values. Not `--strict-mcp-config`: the operator's own servers still load.
            argv.extend(["--mcp-config", mcp_config])
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
        self._cycle = "written"
        self._tainted = False
        return self._user_frame(text, self.operation_id, images)

    def _user_frame(self, text: str, operation_id: str, images=()) -> dict:
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
            "uuid": operation_id,
            "session_id": self.native_id,
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "origin": {"kind": "human"},
            "client_composed": True,
        }

    def submit_now(self, text: str, operation_id: str, images=()) -> dict:
        """Ahead of the CLI's own queue (#1475). The interrupt that preceded this leaves a gap in
        which the CLI starts whatever it queued for itself, a task notification first. Its queue
        takes `now` before `next` and cuts a turn it has just started for it (captured: before
        any output; the notification stays in the history the message is answered with)."""
        return {**self.submit(text, operation_id, images), "priority": "now"}

    def send_now_ready(self) -> bool:
        """May Send now go the CLI's own way (#1526): the message written first, then a
        `send_now` interrupt that ends no background agent? Only with the capability, a lifecycle
        fence that tells the message's frames from the turn it cuts, and nothing already written
        ahead. The fence is proved, or the operator turn running now has started cleanly inside
        its own lifecycle: the first turn of a generation, whose end proves it (captured: the
        cut turn's result, then its `cancelled`, then the message's `started`)."""
        fenced = self.lifecycle_proven or (
            self.operation_id is not None and self._cycle == "started" and not self._tainted
        )
        return self.native_send_now and fenced and self._ahead is None and not self._closed

    def send_now_would_stop_background(self) -> bool:
        """Would Send now's fallback, a plain interrupt (a Stop), end background work (#1526)?"""
        return bool(self._background) and not self.send_now_ready()

    def submit_ahead(self, text: str, operation_id: str, images=()) -> dict:
        """Send now's message (#1526), written while the operator turn it cuts still runs. That
        turn stays the operation until its own result or lifecycle end; the message takes over
        from there and owns output only from its own `started`. With no operator turn running
        (the agent's own turn is the one cut) it is simply the next operation."""
        if self.operation_id is None:
            return self.submit(text, operation_id, images)
        if not self.send_now_ready():
            raise ProtocolError("this connection cannot write a message ahead of its turn")
        if len(self._results) >= MAX_RESULTS:
            raise ProtocolError(
                "native result replay capacity exceeded; a new connection is required"
            )
        validate_text(text, allow_empty=bool(images))
        ahead = _uuid(operation_id)
        if ahead == self.operation_id:
            raise ProtocolError("a message cannot be written ahead of its own turn")
        self._ahead, self._ahead_cycle = ahead, "written"
        return self._user_frame(text, ahead, images)

    def send_now_interrupt(self, message_uuid: str) -> dict:
        """The CLI's Send now for a message it already holds (#1526): not a Stop. It cuts the
        running turn (or moves what it waits on to the background) and ends no background task.
        A message the CLI already runs, or no longer counts as waiting, makes it do nothing."""
        if message_uuid is None or message_uuid not in (self._ahead, self.operation_id):
            raise ProtocolError("no written message to send now")
        return self._request("interrupt", send_now=True, message_uuid=message_uuid)

    def _promote_ahead(self) -> None:
        """The cut turn has ended: the message written ahead of it is the operation now."""
        if self._ahead is None:
            return
        self.operation_id = self.native_turn_id = self._ahead
        self._cycle = self._ahead_cycle
        self._tainted = False
        self._consumed.clear()
        self._ahead = self._ahead_cycle = None

    def interrupt(self) -> dict:
        if self.operation_id is None:
            raise ProtocolError("no correlated native turn to interrupt")
        return self._request("interrupt")

    def interrupt_background(self, target: str) -> dict:
        """Stop the turn the agent runs for itself; ``target`` is the one the operator saw."""
        if self.operation_id is not None or target is None or target != self.background_turn:
            raise ProtocolError("no turn of the agent's own to interrupt")
        return self._request("interrupt")

    def _agent_turn(self, running: bool, outcome: str = "completed") -> list[NativeEvent]:
        """A turn the agent runs for itself starts or ends: mint or clear its Send now target.
        An end carries how it ended (#1438): its own result says, else it simply completed."""
        if running == self.unowned_turn:
            return []
        self.unowned_turn = running
        self.background_turn = str(uuid.uuid4()) if running else None
        return [
            _event(
                "background",
                state="running" if running else outcome,
                active=running or bool(self._background),
                background_turn=self.background_turn,
            )
        ]

    def _lifecycle(self, frame: dict) -> list[NativeEvent]:
        command, state = frame.get("command_uuid"), frame.get("state")
        if command is not None and command == self._ahead:
            # Send now's message (#1526), written while the turn it cuts still runs: the CLI
            # queues it behind that turn and starts it once that turn has ended.
            if state == "queued" and self._ahead_cycle == "written":
                self._ahead_cycle = "queued"
                return []
            if state == "started" and self._ahead_cycle == "queued":
                # The cut turn ended without a result or lifecycle end naming it: close it as
                # interrupted (which distrusts the fence) and let the message run.
                ended = self._ended_without_result("cancelled")
                self._cycle = "started"
                return [*ended, *self._agent_turn(False)]
            if state in _DROPPED:
                # The CLI dropped it before it ran: it was sent, and it never will run.
                ahead = self._ahead
                self._ahead = self._ahead_cycle = None
                return self._dropped(ahead)
        if command is not None and command == self.operation_id:
            expected = {"queued": "written", "started": "queued"}.get(state)
            if expected is not None and self._cycle == expected:
                self._cycle = state
                # The CLI runs one turn at a time: this one starts only after any turn the agent
                # was running for itself has ended.
                return self._agent_turn(False) if state == "started" else []
            if state == "completed" and self._cycle == "started":
                # A message written while the agent runs a turn of its own (a task notification)
                # is taken INTO that turn: its `completed` comes BEFORE the result naming it, and
                # that result carries the notification's origin (captured, #1644). Wait for it:
                # the next frame that is not that result ends the input as below.
                self._cycle = "ended"
                return []
            if state in _LIFECYCLE_ENDED and self._cycle == "started":
                # The CLI ended this input without the result that names it: complete it
                # visibly, never leave it running for ever, and trust the fence no longer.
                return self._ended_without_result(state)
            if state in _DROPPED and self._cycle in {"written", "queued"}:
                # Dropped before it ran (Hermes on #1528): e.g. Send now's message, promoted when
                # the turn it cut ended, then cancelled. It never runs: settle it, never wait.
                operation = self.operation_id
                self._cycle = None
                self.operation_id = None
                self.native_turn_id = None
                self._approvals.clear()
                return self._dropped(operation)
        elif command is not None and command == self._closing and state in _LIFECYCLE_ENDED:
            # `cancelled` is how the CLI ends an interrupted input (captured, #1427).
            self._closing = None
            self.lifecycle_proven = True
            return []
        # Out of order, unknown or unexpected: this generation can no longer fence its output.
        self._distrust()
        if command is not None and command == self.operation_id and state == "started":
            # Even out of order, the operator's input starting means the agent's own turn is
            # over (one turn at a time): never leave it running beside a message that runs
            # (#1475 review), which the pane would read as the message still waiting.
            return self._agent_turn(False)
        return []

    def _distrust(self) -> None:
        """Every anomaly comes here (Hermes on #1429): the fence is no longer proved, the cycle in
        flight can never prove it, and a cycle waiting for its `completed` proves nothing. Only a
        LATER cycle that saw nothing unexpected earns the proof back."""
        self.lifecycle_proven = False
        self._tainted = True
        self._closing = None

    def _ended_without_result(self, state: str) -> list[NativeEvent]:
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        self._distrust()
        self._cycle = None
        self.operation_id = None
        self.native_turn_id = None
        self._approvals.clear()
        self._promote_ahead()
        interrupted = state == "cancelled"
        return [
            _event(
                "turn_completed",
                **common,
                state="interrupted" if interrupted else "failed",
                text="",
                background_active=bool(self._background),
                native_state=self._session_state,
                error="" if interrupted else "the agent ended this turn without its result",
            )
        ]

    def _dropped(self, operation: str | None) -> list[NativeEvent]:
        """An input the CLI dropped before it started completes as failed."""
        self._distrust()
        return [
            _event(
                "turn_completed",
                operation_id=operation,
                native_turn_id=operation,
                state="failed",
                text="",
                background_active=bool(self._background),
                native_state=self._session_state,
                error="the agent dropped this message before it ran",
            )
        ]

    def _owned(self) -> bool:
        """Is the frame being read the operator turn's own output?"""
        if self.operation_id is None:
            return False
        return self._cycle == "started" or not self.lifecycle_proven

    @staticmethod
    def _response(request_id: str, response: dict) -> dict:
        return {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id, "response": response},
        }

    def check_answer(self, request_id: str, answers: Any) -> dict[str, str]:
        item = self._approvals.get(request_id)
        if item is None or item.get("kind") != native_questions.ASK_TOOL:
            raise ProtocolError("only an agent question takes an answer")
        try:
            return native_questions.claude_answers(item["input"], answers)
        except native_questions.QuestionError as exc:
            raise ProtocolError(str(exc)) from None

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        grant: str | None = None,
        cwd: str | None = None,
        answers: Any = None,
    ) -> dict:
        item = self._approvals.get(request_id)
        asks = item is not None and item.get("kind") == native_questions.ASK_TOOL
        if (decision == "answer") != (answers is not None):
            raise ProtocolError("an answer goes with an answer decision, and only with one")
        # A question is never approved as if it were a permission (#1463): approving it sends
        # Claude no answers ("The user did not answer the questions"), and grants nothing.
        if asks and decision in {"approve", "always"}:
            raise ProtocolError("an agent question is answered, skipped or stopped, not approved")
        if decision == "answer":
            # Checked against the request held here before anything is consumed.
            reply = self.check_answer(request_id, answers)
            pending = self._decision(request_id, decision, answer=True)
            return self._response(
                pending["id"],
                {"behavior": "allow", "updatedInput": {**pending["input"], "answers": reply}},
            )
        # `always` (#1339): allow with the ORIGINAL input plus exactly the one proposed
        # suggestion the prompt itself carried, re-derived before anything is consumed.
        suggestion = self._always(request_id, grant, cwd) if decision == "always" else None
        pending = self._decision(request_id, decision)
        data = (
            {"behavior": "allow", "updatedInput": pending["input"]}
            if decision == "approve"
            else {
                "behavior": "allow",
                "updatedInput": pending["input"],
                "updatedPermissions": [suggestion],
            }
            if decision == "always"
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
                "presented": presented,
                "kind": tool_name,
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
                # An agent question (#1463) is answered, skipped or stopped — never approved. One
                # BattleLab can't answer exactly (malformed, repeated questions or labels) can
                # still be skipped.
                choices=(
                    ["answer", "reject", "cancel"]
                    if native_questions.claude_questions(original_input) is not None
                    else ["reject", "cancel"]
                )
                if tool_name == native_questions.ASK_TOOL
                else ["approve", "reject", "cancel"],
            )
        ]

    def frame_lost(self) -> list[NativeEvent]:
        """A frame too large to read was dropped (the worker's bound): if it was the result an
        ended input waits for, that input ends now rather than running for ever (#1644)."""
        if self._cycle != "ended":
            return []
        return self._ended_without_result("completed")

    def _awaited_result(self, frame: dict) -> bool:
        return (
            self._cycle == "ended"
            and frame.get("type") == "result"
            and frame.get("parent_tool_use_id") is None
            and frame.get("user_message_uuid") == self.operation_id
        )

    def feed(self, message: dict[str, Any]) -> list[NativeEvent]:
        if self._closed:
            raise ProtocolError("native connection is closed")
        frame = _copy_frame(message)
        kind = frame.get("type")
        if frame.get("session_id") is not None and frame.get("session_id") != self.native_id:
            raise ProtocolError("native session identity changed")
        if (
            self._cycle == "ended"
            and not self._awaited_result(frame)
            and not _between_end_and_result(frame)
        ):
            # Not the result the ended input waits for (#1644): end it as one without its
            # result, then read this frame as usual.
            return [*self._ended_without_result("completed"), *self.feed(message)]
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
        if kind == "command_lifecycle":
            return self._lifecycle(frame)
        if kind == "system" and subtype == "init":
            capabilities = frame.get("capabilities")
            if isinstance(capabilities, list):
                self.native_send_now = "interrupt_send_now_v1" in capabilities
            return [
                _event(
                    "session",
                    native_id=self.native_id,
                    model_configured=_observed_model(frame.get("model")),
                    # `system/init` names no reasoning level (#1511): no report, never a guess.
                    effort_configured=None,
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
            # A task_updated patch is sparse: without status it says nothing about a paused
            # task's state. Keep the existing state/active semantics for dispatch, and carry the
            # explicit display state separately. Never copy an arbitrary patch or output path.
            source = patch if subtype == "task_updated" and isinstance(patch, dict) else frame
            metadata = {}
            for field, limit in TASK_METADATA_LIMITS.items():
                value = source.get(field)
                if isinstance(value, str) and value:
                    metadata[field] = value if len(value) <= limit else value[: limit - 1] + "…"
            return [
                _event(
                    "background",
                    task_id=task_id,
                    state=state or "running",
                    active=bool(self._background),
                    task_state=state or ("running" if subtype == "task_started" else ""),
                    **metadata,
                )
            ]
        if kind == "system" and subtype == "session_state_changed":
            state = frame.get("state")
            self._session_state = (
                state
                if isinstance(state, str) and state in {"running", "idle", "requires_action"}
                else "unknown"
            )
            out = [
                _event(
                    "background",
                    state=self._session_state,
                    active=bool(self._background) or self._session_state != "idle",
                    native_state=self._session_state,
                )
            ]
            # The session's own state machine marks a turn the agent runs for itself from its
            # START, before any output (Hermes on #1429): the Send now target exists as soon as
            # the queue is held, and ends with the state. A running state the operator's input
            # owns is that input's turn, never the agent's.
            if self._session_state in {"running", "requires_action"} and not self._owned():
                out += self._agent_turn(True)
            elif self._session_state == "idle":
                out += self._agent_turn(False)
            return out
        if kind == "system" and subtype == "status":
            out = [
                _event(
                    "background",
                    state=_text(frame.get("status"), limit=64),
                    active=bool(self._background),
                )
            ]
            # The current CLI announces every model request with `status: requesting`, 2 ms after
            # a turn starts and seconds before its first output (captured, #1427 / Hermes on
            # #1429). Outside every operator window that is a turn the agent runs for itself:
            # its Send now target exists from here, and its own result ends it.
            if frame.get("status") == "requesting" and not self._owned():
                out += self._agent_turn(True)
            return out
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
        if self._awaited_result(frame):
            # The result naming an input whose lifecycle already completed (#1644): it ends that
            # input whatever origin the turn it ran in had. A clean, correlated cycle proves the
            # fence exactly as the usual order (result, then `completed`) does.
            return self._result(frame, proven=not self._tainted)
        background = frame.get("parent_tool_use_id") is not None or (
            isinstance(origin, dict) and origin.get("kind") != "human"
        )
        if kind == "result" and frame.get("user_message_uuid") not in (None, self.operation_id):
            background = True  # another input's result can never complete this turn
            self._distrust()  # nor can a stream that carried one prove the fence
        if (
            kind == "result"
            and not background
            and self.operation_id is not None
            and self._cycle == "started"
            and frame.get("user_message_uuid") != self.operation_id
        ):
            # Inside a lifecycle window only the result NAMING this input completes it (Hermes on
            # #1429): an unbound one is nobody's reply, and the fence is no longer trusted. The
            # input still ends, by its own result or its lifecycle `completed` / `cancelled`.
            self._distrust()
            return [_event("background", state="activity", active=True)]
        if background:
            ended = []
            if kind == "result" and frame.get("parent_tool_use_id") is None:
                # the agent's own turn has ended
                ended = self._agent_turn(False, _result_state(frame))
            elif not self._owned():
                ended = self._own_output(frame, kind)
            return ended or [_event("background", state="activity", active=True)]
        if not self._owned():
            # Output no operator turn owns: a turn the agent runs for itself. Before the
            # operator's `started`, this is the turn that was already running when it wrote.
            if kind == "result":
                return self._agent_turn(False, _result_state(frame))
            out = self._agent_turn(True) if kind in {"assistant", "stream_event", "user"} else []
            out += self._own_output(frame, kind)
            if self.operation_id is None:
                return out
            return [*out, _event("background", state="activity", active=True)]
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        if kind == "result":
            return self._result(frame)
        return self._output(frame, kind, common)

    def _result(self, frame: dict, *, proven: bool = False) -> list[NativeEvent]:
        """The operator turn's own result. Normally its `completed` follows and proves the fence
        (`_closing`); ``proven``: that `completed` already came, cleanly (#1644)."""
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        state = _result_state(frame)
        clean = self._cycle == "started" and not self._tainted
        self._closing = self.operation_id if clean else None
        if proven:
            self.lifecycle_proven = True
        self._cycle = None
        self.operation_id = None
        self.native_turn_id = None
        self._approvals.clear()
        self._promote_ahead()
        return [
            _event(
                "turn_completed",
                **common,
                state=state,
                text=_text(frame.get("result")),
                background_active=bool(self._background)
                or self._session_state in {"running", "requires_action", "unknown"},
                native_state=self._session_state,
                error="" if state == "interrupted" else _summary(frame.get("errors")),
            )
        ]

    def _own_output(self, frame: dict, kind: str) -> list[NativeEvent]:
        """What the agent does in a turn of its own (#1438), named by that turn's target. A
        subagent's frames stay its parent tool's business; a frame that cannot be read is
        skipped, never a reason to drop the connection over a turn nobody submitted."""
        if self.background_turn is None or frame.get("parent_tool_use_id") is not None:
            return []
        try:
            return self._output(frame, kind, {"agent_turn": self.background_turn}, own=True)
        except ProtocolError:
            return []

    def _output(
        self, frame: dict, kind: str, common: dict, *, own: bool = False
    ) -> list[NativeEvent]:
        """A turn's text and tool calls. An own turn records whole messages only: its streamed
        deltas would cost a journal write each, for a block that is collapsed by default."""
        if kind == "assistant":
            msg = frame.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                raise ProtocolError("native assistant message has no content")
            item = {"item_id": _identity(msg["id"])} if "id" in msg else {}
            output = []
            model = _observed_model(msg.get("model"))
            if model is not None and not own:
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
        if kind == "stream_event" and not own:
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
#: Native choices retained verbatim; standing grants are separately bound and labelled.
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
            "OpenCode did not include this tool’s arguments, so BattleLab cannot show what "
            "would be approved. Deny the request or stop the turn.",
        )
        for kind in ("read", "search", "fetch", "delete", "move", "other")
    },
}


#: Kinds whose EMPTY permission request may show the same call's running input instead, each with
#: what that input must name. Measured on 1.18.35: MCP tools (`other`) and `read` ask with
#: `rawInput: {}`, the input only on the call's `in_progress` update; a read must name its file.
#: search and fetch carry their own pattern / url, and no tool asks as delete or move.
_ACP_BORROWABLE = {
    "other": bool,
    "read": lambda raw: _nonempty(raw.get("filePath")),
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
    observed after the pin ends the connection. Permission requests retain native once/always
    options; a request is live only on the connection that asked it,
    and `session/load` replays history without ever re-asking one.
    """

    ADAPTER = "opencode-acp"

    def __init__(self, session_id: str | None = None, *, agent: str) -> None:
        super().__init__()
        if not isinstance(agent, str) or not _OPENCODE_AGENT.fullmatch(agent):
            raise ProtocolError("expected BattleLab's own opencode agent")
        self.agent = agent
        self.native_id = None if session_id is None else opencode_session_id(session_id)
        self.version: tuple[int, int, int] | None = None
        self._pinned = False  # the mode pin was confirmed; later mode changes are violations
        self._config_requests: dict[str, tuple[str, str]] = {}
        # ACP permissions carry metadata, not necessarily the tool's input (MCP metadata is
        # empty in 1.18.35). Retain bounded, same-turn tool observations for that exact call.
        self._tool_calls: dict[str, dict] = {}

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

    def create(self, cwd: str, mcp_servers: list[dict] | None = None) -> dict:
        if self.native_id is not None:
            raise ProtocolError("native session is already bound")
        # `mcpServers`: the memory tool (#1506, `agent_tools.acp_servers`) when this generation
        # was given memory, else none — on `session/load` exactly as on `session/new`.
        return self._request(
            "session/new", {"cwd": _path(cwd), "mcpServers": list(mcp_servers or [])}
        )

    def load(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None) -> dict:
        session_id = opencode_session_id(session_id)
        if self.native_id not in (None, session_id):
            raise ProtocolError("native session identity changed")
        self.native_id = session_id
        frame = self._request(
            "session/load",
            {"sessionId": session_id, "cwd": _path(cwd), "mcpServers": list(mcp_servers or [])},
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

    def set_effort(self, effort: str) -> dict:
        """The model's reasoning level (#1407): ACP's `effort` option (category `thought_level`),
        offered only once a model with variants is selected — so this follows `set_model` — and,
        like it, confirmed from the echoed `currentValue`. A `model/variant` id is never used
        instead: 1.18.35 ignores it without an error (#1407 spike)."""
        if _effort(effort) is None:
            raise ProtocolError("no effort to select")
        return self._set("effort", effort)

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """Text only: this kind declares no image input (`kinds.API_IMAGE_INPUT`), so the runtime
        refuses pictures before a claim. ACP's `{"type":"image"}` prompt blocks, gated by the
        agent's `promptCapabilities.image`, are the follow-up (#1332)."""
        if self.native_id is None or not self._pinned:
            raise ProtocolError("native session is not bound in its pinned mode")
        if images:
            raise ProtocolError("this client takes no images")
        self._submit(text, operation_id)
        self._tool_calls.clear()
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

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        grant: str | None = None,
        cwd: str | None = None,
        answers: Any = None,
    ) -> dict:
        if answers is not None:
            raise ProtocolError("this agent's requests take no answer")
        if decision not in {*_ONCE, "always"}:
            raise ProtocolError("unsupported permission decision")
        selected = self._always(request_id, grant, cwd) if decision == "always" else None
        item = self._approvals.get(request_id)
        if item is not None and decision == "approve" and item.get("complete") is not True:
            raise ProtocolError("this request could not be presented completely")
        pending = self._decision(request_id, decision)
        option = selected if decision == "always" else pending["options"].get(_ONCE[decision])
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
        self._tool_calls.clear()
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
                    effort_configured=_observed_effort(config.get("effort")),
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
                    effort_configured=_observed_effort(config.get("effort")),
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
                and option.get("kind") in {*_ONCE.values(), "allow_always"}
                and isinstance(option.get("optionId"), str)
                and 0 < len(option["optionId"]) <= 256
            ):
                offered.setdefault(option["kind"], []).append(option["optionId"])
        # Exactly one of each native kind, or that choice is not answerable at all.
        once = {kind: ids[0] for kind, ids in offered.items() if len(ids) == 1}
        item_id = _safe_id(tool_call.get("toolCallId"), "tool")
        if item_id is None:
            raise ProtocolError("native permission request names no tool call")
        # Everything the decision answers: the tool call as asked (title, kind, locations, the
        # complete rawInput — a command, or a file path and its whole diff — and its content,
        # including the diff's old and new text), plus the native options a decision can pick.
        presented: dict[str, Any] = {
            "toolCall": tool_call,
            "options": [
                {"optionId": option_id, "kind": kind} for kind, option_id in sorted(once.items())
            ],
        }
        context = self._tool_calls.get(item_id)
        effective = tool_call
        # Only fill an empty `rawInput` (`_ACP_BORROWABLE`) from the same call, same kind and
        # title, already observed running in THIS turn, and only with input naming what the kind
        # acts on. Never use a command title, another call or replayed history.
        borrowable = _ACP_BORROWABLE.get(tool_call.get("kind"))
        if (
            borrowable is not None
            and tool_call.get("rawInput") == {}
            and context is not None
            and context.get("kind") == tool_call.get("kind")
            and context.get("title") == tool_call.get("title")
            and _nonempty(context.get("title"))
            and context.get("status") == "in_progress"
            and isinstance(context.get("rawInput"), dict)
            and bool(context["rawInput"])
            and borrowable(context["rawInput"])
        ):
            presented["toolContext"] = dict(context)
            effective = {**tool_call, "rawInput": context["rawInput"]}
        # Approve needs the consequential action itself in what is presented (Hermes on #1336):
        # an allow_once option alone would approve a blind request.
        reason = _acp_unpresentable(effective)
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
                "presented": presented,
                "kind": tool_call.get("kind"),
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
            previous = self._tool_calls.get(item_id, {})
            context = {**previous, **{k: v for k, v in update.items() if k != "sessionUpdate"}}
            cancellations = []
            for key, approval in list(self._approvals.items()):
                bound = approval.get("presented", {}).get("toolContext")
                if approval["item_id"] != item_id or not bound:
                    continue
                if any(context.get(k) != bound.get(k) for k in ("rawInput", "title", "kind")):
                    # The supplementary input changed while pending: that callback can no longer
                    # approve what was displayed. Consume it once and retract the card.
                    self._approvals.pop(key)
                    self._consumed.add(key)
                    cancellations.extend(
                        [
                            _event(
                                "send",
                                frame={
                                    "jsonrpc": "2.0",
                                    "id": approval["id"],
                                    "result": {"outcome": {"outcome": "cancelled"}},
                                },
                            ),
                            _event("approval_cancelled", request_id=key),
                        ]
                    )
            if len(_summary(context)) < MAX_TEXT and (
                item_id in self._tool_calls or len(self._tool_calls) < MAX_PENDING
            ):
                self._tool_calls[item_id] = context
            else:
                self._tool_calls.pop(item_id, None)
            data: dict[str, Any] = {"item_id": item_id, "state": status}
            tool = _safe_id(update.get("kind"), "kind")
            if tool is not None:
                data["tool"] = tool
            if isinstance(update.get("title"), str):
                data["summary"] = _text(update["title"])
            if context.get("kind") == "execute":
                raw = context.get("rawInput")
                # Original command input, never the display title. Oversize is unclassified.
                summary = _summary(raw) if isinstance(raw, dict) else "{}"
                data["summary"] = summary if len(summary) < MAX_TEXT else "{}"
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
            return [*cancellations, _event("tool", **common, **data)]
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


# --- GitHub Copilot CLI: the Agent Client Protocol over stdio (#1565) ---------------------------
#
# Checked against copilot 1.0.68/1.0.70 (`copilot --acp --no-auto-update`, ACP protocol version 1).
# The wire is opencode's; the differences are copilot's own: session ids are plain v4 UUIDs, the
# session is pinned to the protocol's agent mode URI with `allow_all` as the bypass switch, and
# this build enumerates no `model` config option — a model set is accepted and never echoed.
COPILOT_MIN_VERSION = (1, 0, 68)
#: The only session mode an API client runs (#1565). Anything else observed after the pin —
#: `#autopilot` included — ends the connection.
COPILOT_MODE = "https://agentclientprotocol.com/protocol/session-modes#agent"


#: Tool kinds whose copilot request can be approved one-shot, each with the consequential input
#: it must name (#1565). Everything else — `edit`, `delete`, `move`, a kind a later copilot adds,
#: none at all — is decline-only, as is any of these without its input.
_COPILOT_APPROVABLE = {
    "execute": (
        lambda call: isinstance(call.get("rawInput"), dict)
        and _nonempty(call["rawInput"].get("command")),
        "the request does not say which command would run",
    ),
    "read": (
        lambda call: isinstance(call.get("rawInput"), dict)
        and _nonempty(call["rawInput"].get("path")),
        "the request does not say which file would be read",
    ),
    "fetch": (
        lambda call: isinstance(call.get("rawInput"), dict)
        and _nonempty(call["rawInput"].get("url")),
        "the request does not say which URL would be fetched",
    ),
    "other": (
        lambda call: isinstance(call.get("rawInput"), dict) and bool(call["rawInput"]),
        "Copilot did not include this tool’s arguments, so BattleLab cannot show what "
        "would be approved. Deny the request or stop the turn.",
    ),
}


def _copilot_unpresentable(tool_call: dict) -> str | None:
    """Why a permission request cannot be approved as presented, or None when it can."""
    rule = _COPILOT_APPROVABLE.get(tool_call.get("kind"))
    if rule is None:
        return "this kind of tool call cannot be approved from BattleLab"
    check, reason = rule
    return None if check(tool_call) else reason


class CopilotAcpCodec(_Codec):
    """One `copilot --acp` connection: one session, one prompt turn at a time (#1565).

    ACP has no server-side turn id: like Claude, the submitted operation id is the turn. The
    session runs pinned to the protocol's agent mode (`COPILOT_MODE`) with `allow_all` as the
    skip-permissions switch, both echo-confirmed; a mode change observed after the pin ends the
    connection. Permission requests live only on the asking connection, and `session/load` replays
    history (arriving before its reply) without ever re-asking one. `allow_always` is never
    offered and never selectable: one-shot decisions only.
    """

    ADAPTER = "copilot-acp"

    def __init__(self, session_id: str | None = None) -> None:
        super().__init__()
        self.native_id = None if session_id is None else _uuid(session_id)
        self.version: tuple[int, int, int] | None = None
        self._pinned = False  # the mode pin was confirmed; later mode changes are violations
        # Per `session/set_config_option` request: (option, value, echo_required). `model` and
        # `reasoning_effort` are confirmed only when the replied configOptions carry that option
        # at all — 1.0.68 never enumerates `model`, and older/newer builds may lack the level.
        self._config_requests: dict[str, tuple[str, str, bool]] = {}
        # Bounded same-turn tool observations, merged across a call's updates (display only:
        # nothing fills a permission request's empty input from them).
        self._tool_calls: dict[str, dict] = {}

    @staticmethod
    def argv(binary: str, mcp_config: str | None = None) -> list[str]:
        # `--no-auto-update` on every launch (#1565): the CLI otherwise self-downloads into
        # ~/.cache/copilot and re-execs from there, outside the provenance-bound entrypoint. The
        # memory tool rides `--additional-mcp-config` (#1506): copilot's ACP session/new rejects
        # stdio `mcpServers` ("Only HTTP and SSE MCP servers are supported"), so none are passed.
        out = [_path(binary), "--acp", "--no-auto-update"]
        if mcp_config is not None:
            out += ["--additional-mcp-config", mcp_config]
        return out

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

    def create(self, cwd: str, mcp_servers: list[dict] | None = None) -> dict:
        if self.native_id is not None:
            raise ProtocolError("native session is already bound")
        if mcp_servers:
            # Refused rather than silently dropped: the memory tool reaches the child on argv.
            raise ProtocolError("copilot's ACP session/new carries no stdio MCP servers")
        return self._request("session/new", {"cwd": _path(cwd), "mcpServers": []})

    def load(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None) -> dict:
        session_id = _uuid(session_id)
        if self.native_id not in (None, session_id):
            raise ProtocolError("native session identity changed")
        if mcp_servers:
            raise ProtocolError("copilot's ACP session/load carries no stdio MCP servers")
        self.native_id = session_id
        frame = self._request(
            "session/load", {"sessionId": session_id, "cwd": _path(cwd), "mcpServers": []}
        )
        self._expected_sessions[frame["id"]] = session_id
        return frame

    def _set(self, option: str, value: str, *, confirm: bool = True) -> dict:
        if self.native_id is None:
            raise ProtocolError("native session is not bound")
        frame = self._request(
            "session/set_config_option",
            {"sessionId": self.native_id, "configId": option, "value": value},
        )
        self._config_requests[frame["id"]] = (option, value, confirm)
        return frame

    def pin_mode(self) -> dict:
        return self._set("mode", COPILOT_MODE)

    def set_allow_all(self, bypass: bool) -> dict:
        """Skip-permissions (#1339) on copilot's own switch, echo-confirmed like the mode pin."""
        return self._set("allow_all", "on" if bypass is True else "off")

    def set_model(self, model: str) -> dict:
        """The session's model. 1.0.68 enumerates no `model` config option and a set is accepted
        without ever being echoed: a non-error reply is the acceptance — best-effort, observed
        only, never claimed as confirmed. The worker does NOT call this yet (#1565 review): with
        no enumeration and no echo there is no trustworthy selection, the runtime refuses
        non-default models while discovery is unavailable, and the session runs the vendor
        default. This pins the protocol seam for a future CLI that lists and confirms."""
        if _model(model) is None:
            raise ProtocolError("no model to select")
        return self._set("model", model, confirm=False)

    def set_effort(self, effort: str) -> dict:
        """The model's reasoning level (#1407): copilot's `reasoning_effort` option, offered only
        once a model is set. Confirmed from the echo when the build offers the option; a reply
        without it (another build) is tolerated, never an error. The worker does NOT call this
        yet (#1565 review): it belongs to a model, and no model is selected while the CLI
        neither enumerates nor echoes one."""
        if _effort(effort) is None:
            raise ProtocolError("no effort to select")
        return self._set("reasoning_effort", effort, confirm=False)

    def submit(self, text: str, operation_id: str, images=()) -> dict:
        """Text only: this kind declares no image input (`kinds.API_IMAGE_INPUT`), so the runtime
        refuses pictures before a claim. ACP's `{"type":"image"}` prompt blocks, gated by the
        agent's `promptCapabilities.image`, are the follow-up (#1332)."""
        if self.native_id is None or not self._pinned:
            raise ProtocolError("native session is not bound in its pinned mode")
        if images:
            raise ProtocolError("this client takes no images")
        self._submit(text, operation_id)
        self._tool_calls.clear()
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

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        grant: str | None = None,
        cwd: str | None = None,
        answers: Any = None,
    ) -> dict:
        if answers is not None:
            raise ProtocolError("this agent's requests take no answer")
        if decision == "always":
            # `allow_always` is filtered out of every presented request (#1565): never grantable.
            raise ProtocolError("this client grants no standing permission")
        if decision not in {*_ONCE, "cancel"}:
            raise ProtocolError("unsupported permission decision")
        item = self._approvals.get(request_id)
        if item is not None and decision == "approve" and item.get("complete") is not True:
            raise ProtocolError("this request could not be presented completely")
        pending = self._decision(request_id, decision)
        option = pending["options"].get(_ONCE.get(decision)) if decision != "cancel" else None
        if option is None and decision == "approve":
            raise ProtocolError("the agent offered no one-shot approval")  # pragma: no cover
        outcome = (
            {"outcome": "selected", "optionId": option}
            if option is not None
            else {"outcome": "cancelled"}  # cancel, or a reject no reject_once was offered for
        )
        return {"jsonrpc": "2.0", "id": pending["id"], "result": {"outcome": outcome}}

    # --- observations -----------------------------------------------------------------------

    def _mode_check(self, config: dict[str, str]) -> None:
        mode = config.get("mode")
        if self._pinned and mode is not None and mode != COPILOT_MODE:
            raise ProtocolError("the agent left its pinned mode")

    def _turn_done(self, state: str, error: str = "") -> list[NativeEvent]:
        data = {"native_turn_id": self.native_turn_id, "operation_id": self.operation_id}
        self.operation_id = None
        self.native_turn_id = None
        self._approvals.clear()
        self._tool_calls.clear()
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
            elif version is None or version < COPILOT_MIN_VERSION:
                refusal = f"copilot {'.'.join(map(str, COPILOT_MIN_VERSION))} or later is required"
            elif not isinstance(capabilities, dict) or capabilities.get("loadSession") is not True:
                refusal = "the agent cannot load an existing session"
            if refusal is not None:
                return [_event("error", request_id=request_id, action=action, message=refusal)]
            self.version = version
            return [_event("initialized", request_id=request_id, action=action)]
        if action in {"session/new", "session/load"}:
            # `session/load`'s result is an empty object on 1.0.68: the id asked for is the id.
            native_id = (
                _uuid(result.get("sessionId")) if action == "session/new" else expected_session
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
                    effort_configured=_observed_effort(config.get("reasoning_effort")),
                    model_effective=None,
                )
            ]
        if action == "session/set_config_option" and config_request is not None:
            option, value, confirm = config_request
            config = _acp_config(result.get("configOptions"))
            echoed = config.get(option)
            if echoed != value and (confirm or echoed is not None):
                # A required pin's wrong or missing echo is an error; an option the build does
                # not enumerate (model on 1.0.68) echoes nothing and is accepted best-effort —
                # but an option that IS echoed must name the value asked for.
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
                    effort_configured=_observed_effort(config.get("reasoning_effort")),
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
            # `allow_always` is NEVER retained (#1565): not presented, not selectable. A request
            # offering only it has no one-shot approval below and is decline-only.
            if (
                isinstance(option, dict)
                and option.get("kind") in set(_ONCE.values())
                and isinstance(option.get("optionId"), str)
                and 0 < len(option["optionId"]) <= 256
            ):
                offered.setdefault(option["kind"], []).append(option["optionId"])
        # Exactly one of each native kind, or that choice is not answerable at all.
        once = {kind: ids[0] for kind, ids in offered.items() if len(ids) == 1}
        item_id = _safe_id(tool_call.get("toolCallId"), "tool")
        if item_id is None:
            raise ProtocolError("native permission request names no tool call")
        # Everything the decision answers: the tool call as asked (title, kind, locations and
        # the complete rawInput), plus the native once options a decision can pick.
        presented: dict[str, Any] = {
            "toolCall": tool_call,
            "options": [
                {"optionId": option_id, "kind": kind} for kind, option_id in sorted(once.items())
            ],
        }
        # Approve needs the consequential action itself in what is presented: an allow_once
        # option alone would approve a blind request. No borrowed context fills an empty one.
        reason = _copilot_unpresentable(tool_call)
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
                "presented": presented,
                "kind": tool_call.get("kind"),
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
            previous = self._tool_calls.get(item_id, {})
            context = {**previous, **{k: v for k, v in update.items() if k != "sessionUpdate"}}
            if len(_summary(context)) < MAX_TEXT and (
                item_id in self._tool_calls or len(self._tool_calls) < MAX_PENDING
            ):
                self._tool_calls[item_id] = context
            else:
                self._tool_calls.pop(item_id, None)
            data: dict[str, Any] = {"item_id": item_id, "state": status}
            tool = _safe_id(update.get("kind"), "kind")
            if tool is not None:
                data["tool"] = tool
            if isinstance(update.get("title"), str):
                data["summary"] = _text(update["title"])
            if context.get("kind") == "execute":
                raw = context.get("rawInput")
                # Original command input, never the display title. Oversize is unclassified.
                summary = _summary(raw) if isinstance(raw, dict) else "{}"
                data["summary"] = summary if len(summary) < MAX_TEXT else "{}"
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
