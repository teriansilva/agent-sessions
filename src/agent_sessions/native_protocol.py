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
#: The Claude result ledger's bound, per connection (#1725). Non-evicting. Measured with
#: tracemalloc over `feed` on CPython 3.11: 224 B per entry, 3.5 MiB at this size against the 4 MiB
#: budget (the next dict resize is past it). `test_native_safety_table` pins the bytes.
MAX_RESULTS = 16_384
#: Room left above the high-water mark: at `MAX_RESULTS - RESULTS_RESERVE` a connection takes no new
#: turn and retires between turns (`rollover`). What is already queued and the agent's own turns
#: still fit, so the mark is reached well before the ledger stops recording.
RESULTS_RESERVE = 1024
#: Callbacks remembered as answered, for refusing a repeat of one. A long guarded turn answers more
#: than `MAX_PENDING`, which used to end the generation. The oldest is forgotten past this bound:
#: an ancient id asked again is only a new request for the operator, never an authorization.
MAX_CONSUMED = 4096
#: How much of each end of a frame too large to read is kept, to tell what was lost (#1725).
LOST_FRAME_ENDS = 4096
FRAME_SKIPPED = "A frame too large to read (over 1 MiB) was skipped"
LOST_RESULT = (
    "BattleLab couldn't read a frame this turn may have depended on (over 1 MiB), so its outcome "
    "isn't known"
)
LOST_UNREADABLE = (
    "A frame too large to read may have been a request, and it couldn't be read well enough to "
    "answer, so the agent's connection was ended; this turn's outcome isn't known"
)
LOST_REQUEST = (
    "A request too large to read (over 1 MiB) was refused; this turn went on without an answer "
    "from you, so its outcome isn't known"
)
# Display observations (#1461), shared with the closed IPC schema. Native task payloads and
# output-file paths never cross that boundary; a cut text value visibly ends with an ellipsis.
TASK_METADATA_LIMITS = {
    "description": 1000,
    "task_type": 128,
    "last_tool_name": 128,
    "summary": 2000,
}
#: `ended` (#1724): the client's full task list no longer names the task, with no terminal frame
#: of its own seen (Claude's `background_tasks_changed`; a Codex terminal no process can be
#: running). It ended; how is not known.
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "stopped", "killed", "ended"})
#: A task whose latest report was too large to read (#1725): whether it runs isn't known, and the
#: journal says so. Its requests are refused until a whole list names it.
TASK_UNCERTAIN = "uncertain"
#: What the operator can do about a FAILED turn, when its client says so in a structured field
#: (never read from the failure's words), shared with the closed IPC schema. ``signin``: the
#: agent's vendor login is missing or expired; signing in again is the remedy. Absent: no remedy
#: is known. Adapter-neutral; Claude's stream-json is the first adapter that reports one.
TURN_REMEDIES = frozenset({"signin"})
# The agent's plan and the tools it works with (#1726), shared with the closed IPC schema. Display
# text only: a plan entry's words and state, a proposed plan document, and the NAMES (with a
# status) of MCP servers, skills and commands. Never a command line, URL, header, environment or
# path from the client's configuration. A cut text visibly ends with an ellipsis.
PLAN_ENTRIES = 100
PLAN_TEXT = 500
PLAN_EXPLANATION = 2000
PLAN_DOCUMENT = 64 * 1024
PLAN_STATES = ("pending", "in_progress", "completed")
ENV_NAME = 128
ENV_MCP = 64
ENV_SKILLS = 256
ENV_COMMANDS = 256
#: Measured states are the client's own report; `configured` is a name from the client's
#: resolved configuration that nothing measured (opencode's probe), so it never reads as healthy.
MCP_STATES = ("connected", "failed", "pending", "needs_auth", "disabled", "configured")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z", re.ASCII)
#: A reasoning level (#1407): the grammar `native_models` lists and a create admits.
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)


class ProtocolError(ValueError):
    """Malformed, oversized or unsupported protocol operation."""


class _Consumed(dict):
    """Answered callback keys, in the order they were answered; bounded (`MAX_CONSUMED`)."""

    def add(self, key: str) -> None:
        self[key] = None
        if len(self) > MAX_CONSUMED:
            del self[next(iter(self))]


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


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


#: A plan entry's state as each client spells it (#1726). Anything else reads as pending: a step
#: is never shown done (or under way) on a word BattleLab does not know.
_PLAN_STATE = {
    "pending": "pending",
    "in_progress": "in_progress",
    "inProgress": "in_progress",
    "completed": "completed",
    "done": "completed",
}


def _plan_entries(items: Any, text_key: str) -> list[dict[str, str]] | None:
    """A client's checklist as bounded ``{text, status}`` rows, or None when it is not a list.
    A row without words is skipped; past `PLAN_ENTRIES` the list is cut."""
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if len(out) >= PLAN_ENTRIES:
            break
        if not isinstance(item, dict):
            continue
        text = item.get(text_key)
        if not isinstance(text, str) or not text.strip():
            continue
        status = item.get("status")
        known = _PLAN_STATE.get(status, "pending") if isinstance(status, str) else "pending"
        out.append({"text": _cut(text.strip(), PLAN_TEXT), "status": known})
    return out


def _plan_document(value: Any) -> str | None:
    return _cut(value, PLAN_DOCUMENT) if isinstance(value, str) and value.strip() else None


def _names(values: Any, limit: int) -> list[str]:
    """Distinct, bounded display names from a client's list (strings, or objects with `name`)."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else ():
        if len(out) >= limit:
            break
        name = value.get("name") if isinstance(value, dict) else value
        if not isinstance(name, str) or not name.strip():
            continue
        name = _cut(name.strip(), ENV_NAME)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


class _Environment:
    """What the client reports it works with (#1726): MCP servers with a status, skills and
    commands, by NAME only. One `environment` event carries the whole current state, and only
    when it changed: a client that repeats its report every turn (claude's `system/init`) costs
    one journal record, not one per turn. A list the client never reported stays absent."""

    def __init__(self, *, skill_use: bool) -> None:
        self.state: dict[str, Any] = {"skill_use": skill_use}
        self._sent: dict[str, Any] | None = None

    def update(self, **fields: Any) -> list[NativeEvent]:
        for key, value in fields.items():
            if value is not None:
                self.state[key] = value
        if self.state == self._sent or not ({"mcp", "skills", "commands"} & self.state.keys()):
            return []
        self._sent = json.loads(json.dumps(self.state))
        return [_event("environment", **json.loads(json.dumps(self.state)))]


def _mcp_rows(rows: list[tuple[str, str]]) -> list[dict[str, str]]:
    out, seen = [], set()
    for name, status in rows:
        name = _cut(name.strip(), ENV_NAME) if isinstance(name, str) else ""
        if not name or name in seen or len(out) >= ENV_MCP:
            continue
        seen.add(name)
        out.append({"name": name, "status": status if status in MCP_STATES else "pending"})
    return out


REQUEST_REFUSED = "A request too large to present was refused; the agent went on without it"
REQUEST_ENDED_TASK = "A request from a background task that had already ended was refused"
REQUEST_UNCERTAIN_TASK = (
    "A request from a background task whose latest report could not be read was refused"
)


def _oversized(frame: dict[str, Any]) -> bool:
    """A callback that cannot be presented completely within the preview bound (#1725: refused,
    with a visible row, and the connection lives).

    A digest identifies the exact callback but is not an operator review of its contents, so
    what can't be shown whole is never pending.
    """
    return len(json.dumps(frame, ensure_ascii=True, separators=(",", ":"))) > MAX_TEXT


# --- reading a frame too large to read, from its two ends (#1725) ------------------------------
#
# A frame over the read bound is dropped, but a request in it still waits for its answer. Its id can
# be on either side of its payload, so both ends are kept, and each is read the only safe way: as
# JSON, from the frame's own first or last byte, one top-level field at a time, stepping over what
# a string or a nested value holds. So a tool's output can never name a request. Whatever can't be
# read that way stays unknown, and a request whose id stays unknown ends the connection: no guesses.

_BODY = object()  # a nested value, or a string the kept end cuts off: there, and not read
_BAD = object()  # a value that is no valid JSON token
_INTEGER = re.compile(rb"-?(?:0|[1-9][0-9]*)\Z")
_JSON_SPACE = b" \t\r\n"
_SCALAR_END = _JSON_SPACE + b',:{}[]"'


def _scalar(token: bytes) -> Any:
    if _INTEGER.match(token):
        return int(token)
    return {b"true": True, b"false": False, b"null": None}.get(token, _BAD)


def _string(token: bytes) -> Any:
    try:
        return json.loads(token.decode("utf-8", "strict"))
    except ValueError:
        return _BAD


def _string_end(data: bytes, at: int) -> int | None:
    """Past the string that opens at ``at``, or None if the kept end cuts it off."""
    at += 1
    while at < len(data):
        if data[at] == 0x5C:  # a backslash takes the next byte with it
            at += 2
        elif data[at] == 0x22:
            return at + 1
        else:
            at += 1
    return None


def _nested_end(data: bytes, at: int) -> int | None:
    """Past the object or array that opens at ``at``, or None if the kept end cuts it off."""
    depth = 0
    while at < len(data):
        byte = data[at]
        if byte == 0x22:
            end = _string_end(data, at)
            if end is None:
                return None
            at = end
            continue
        if byte in b"{[":
            depth += 1
        elif byte in b"}]":
            depth -= 1
            if depth == 0:
                return at + 1
        at += 1
    return None


def _skip_space(data: bytes, at: int) -> int:
    while at < len(data) and data[at] in _JSON_SPACE:
        at += 1
    return at


def _fields_from_start(head: bytes) -> list[tuple[str, Any]]:
    """The frame's top-level fields, from its first byte, until the kept end runs out."""
    out: list[tuple[str, Any]] = []
    at = _skip_space(head, 0)
    if at >= len(head) or head[at] != 0x7B:
        return out
    at = _skip_space(head, at + 1)
    while at < len(head) and head[at] == 0x22:
        end = _string_end(head, at)
        if end is None:
            return out
        key = _string(head[at:end])
        at = _skip_space(head, end)
        if not isinstance(key, str) or at >= len(head) or head[at] != 0x3A:
            return out
        at = _skip_space(head, at + 1)
        if at >= len(head):
            return out
        if head[at] == 0x22:
            end = _string_end(head, at)
            if end is None:
                out.append((key, _BODY))
                return out
            value = _string(head[at:end])
        elif head[at] in b"{[":
            end = _nested_end(head, at)
            if end is None:
                out.append((key, _BODY))
                return out
            value = _BODY
        else:
            end = at
            while end < len(head) and head[end] not in _SCALAR_END:
                end += 1
            if end >= len(head):
                return out  # cut off: not this token, never a shorter one
            value = _scalar(head[at:end])
        out.append((key, value))
        at = _skip_space(head, end)
        if at >= len(head) or head[at] != 0x2C:
            return out
        at = _skip_space(head, at + 1)
    return out


def _skip_space_back(data: bytes, at: int) -> int:
    while at >= 0 and data[at] in _JSON_SPACE:
        at -= 1
    return at


def _string_start(data: bytes, at: int) -> int | None:
    """Where the string that closes at ``at`` opens, or None if the kept end cuts it off. No
    unescaped quote can be inside a JSON string, so the nearest is its opening one. Backslashes
    running back to the kept end's edge could be half of a pair: unknown."""
    at -= 1
    while at >= 0:
        if data[at] == 0x22:
            run = at - 1
            while run >= 0 and data[run] == 0x5C:
                run -= 1
            if run < 0:
                return None
            if (at - run - 1) % 2 == 0:
                return at
        at -= 1
    return None


def _nested_start(data: bytes, at: int) -> int | None:
    """Where the object or array closing at ``at`` opens, or None if the kept end cuts it off."""
    depth = 0
    while at >= 0:
        byte = data[at]
        if byte == 0x22:
            start = _string_start(data, at)
            if start is None:
                return None
            at = start - 1
            continue
        if byte in b"}]":
            depth += 1
        elif byte in b"{[":
            depth -= 1
            if depth == 0:
                return at
        at -= 1
    return None


def _fields_from_end(tail: bytes) -> list[tuple[str, Any]]:
    """The frame's top-level fields, from its last byte back, until the kept end runs out."""
    out: list[tuple[str, Any]] = []
    at = _skip_space_back(tail, len(tail) - 1)
    if at < 0 or tail[at] != 0x7D:
        return out
    at = _skip_space_back(tail, at - 1)
    while at >= 0:
        byte = tail[at]
        if byte == 0x22:
            start = _string_start(tail, at)
            if start is None:
                return out
            value = _string(tail[start : at + 1])
        elif byte in b"}]":
            start = _nested_start(tail, at)
            if start is None:
                return out
            value = _BODY
        else:
            start = at
            while start >= 0 and tail[start] not in _SCALAR_END:
                start -= 1
            if start < 0:
                return out  # cut off: not this token, never a shorter one
            start += 1
            value = _scalar(tail[start : at + 1])
        at = _skip_space_back(tail, start - 1)
        if at < 0 or tail[at] != 0x3A:
            return out
        at = _skip_space_back(tail, at - 1)
        if at < 0 or tail[at] != 0x22:
            return out
        start = _string_start(tail, at)
        if start is None:
            return out
        key = _string(tail[start : at + 1])
        if not isinstance(key, str):
            return out
        out.append((key, value))
        at = _skip_space_back(tail, start - 1)
        if at < 0 or tail[at] != 0x2C:
            return out
        at = _skip_space_back(tail, at - 1)
    return out


def _frame_fields(head: bytes, tail: bytes) -> dict[str, Any] | None:
    """The top-level fields both ends of a lost frame establish. None: the ends disagree (a field
    twice, which a read frame is refused for): nothing about this frame can be trusted."""
    fields: dict[str, Any] = {}
    for key, value in [*_fields_from_start(head), *_fields_from_end(tail)]:
        if key in fields and not (fields[key] is value or fields[key] == value):
            return None
        fields[key] = value
    return fields


def _lost_rpc(head: bytes, tail: bytes) -> tuple[str | None, str | int | None, bool, bool]:
    """A lost JSON-RPC frame: (method, id, readable, replies). An id is its whole validated token,
    or the frame isn't ``readable``: never another id. ``replies``: it carries a result or error."""
    fields = _frame_fields(head, tail)
    if fields is None:
        return None, None, False, False
    method, identity = fields.get("method"), None
    readable = method is None or isinstance(method, str)
    if "id" in fields:
        try:
            identity = _rpc_id(fields["id"]) if fields["id"] not in (_BAD, _BODY) else None
        except ProtocolError:
            identity = None
        readable = readable and identity is not None
    return (
        method if isinstance(method, str) else None,
        identity,
        readable,
        "result" in fields or "error" in fields,
    )


def _origin_public(origin: dict) -> dict:
    """What the operator is shown of a request's origin (#1725)."""
    return {"task_id": origin["task_id"], "description": origin.get("description") or ""}


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
        self._consumed = _Consumed()
        # Background-origin callbacks answered (#1725): they outlive an operator turn, so a new
        # turn's reset of `_consumed` never forgets them.
        self._origin_consumed = _Consumed()
        self._expected_sessions: dict[str, str] = {}
        self.native_id: str | None = None
        self.operation_id: str | None = None
        self.native_turn_id: str | None = None
        self._closed = False
        # This worker generation (#1725), part of what a background-origin request is bound to.
        self.generation: str | None = None
        # More background work than is tracked (#1725): the set is not fully enumerated, so it
        # stays busy until the client reports its whole list again.
        self.overflow = False
        # A turn whose success BattleLab cannot claim: a request too large to read was refused
        # while it ran, so the operator never saw it (#1725).
        self.lost_turn: str | None = None
        self._rows = 0
        # Requests refused with no turn to carry their row (#1725): the pane says how many.
        self.requests_refused = 0
        # A frame too large to read that may be a request and can't be answered (#1725): it could
        # leave the agent waiting for ever, so the worker ends the connection.
        self.unread_frame = False
        self._last_plan: dict | None = None  # the newest checklist this connection journaled

    def _plan_once(self, **data: Any) -> list[NativeEvent]:
        """A checklist, journaled only when it differs from the last one this connection sent
        (review on #1811: one call's repeated reports are one plan)."""
        if data == self._last_plan:
            return []
        self._last_plan = json.loads(json.dumps(data))
        return [_event("plan", **data)]

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
        if key in self._consumed or key in self._origin_consumed:
            raise ProtocolError("native permission request was already consumed")
        if key in self._approvals and self._approvals[key] != payload:
            raise ProtocolError("native permission request identity changed")
        self._approvals[key] = payload
        return key

    def _no_room(self, request_id: Any) -> bool:
        """More requests open at once than an operator could be shown (#1725): the callback is
        refused, the agent goes on without the tool, and the connection lives."""
        return (
            _approval_key(request_id) not in self._approvals and len(self._approvals) >= MAX_PENDING
        )

    def _row(self, summary: str) -> list[NativeEvent]:
        """A visible activity row on the turn in flight (#1725). With no turn to carry it, it is
        a diagnostic, and the worker's `frames_skipped` count says so in the pane."""
        self._rows += 1
        owner: dict[str, str]
        if self.operation_id is not None and self.native_turn_id is not None:
            owner = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        elif self.operation_id is None and self.background_turn is not None:
            owner = {"agent_turn": self.background_turn}
        else:
            return [_event("error", message=summary)]
        return [
            _event(
                "tool",
                **owner,
                item_id=f"battlelab-skipped-{self._rows}",
                tool="battlelab",
                state="skipped",
                summary=summary,
                completed=True,
            )
        ]

    def _uncertain(self, operation_id: str, native_turn_id: str | None, why: str) -> list:
        """The turn ended and BattleLab cannot say how (#1725): the two records every reader
        already takes for an uncertain end. Never a claimed success, and no new journal shape."""
        common = {"operation_id": operation_id, "native_turn_id": native_turn_id or operation_id}
        if self.lost_turn == operation_id:
            self.lost_turn = None
        return [
            _event("error", **common, message=why),
            _event(
                "turn_completed",
                **common,
                state="failed",
                error=why,
                background_active=bool(getattr(self, "_background", None)),
            ),
        ]

    def settle_lost(self, events: list[NativeEvent]) -> list[NativeEvent]:
        """A turn that had a request too large to read refused while it ran ends uncertain,
        whatever the agent says of it (#1725): the operator was never asked. An Interrupt stays
        an interrupt."""
        if self.lost_turn is None:
            return events
        out: list[NativeEvent] = []
        for event in events:
            if event.kind == "turn_completed" and event.data.get("operation_id") == self.lost_turn:
                self.lost_turn = None
                if event.data.get("state") != "interrupted":
                    common = {
                        "operation_id": event.data["operation_id"],
                        "native_turn_id": event.data["native_turn_id"],
                    }
                    out.append(_event("error", **common, message=LOST_REQUEST))
                    event = _event(
                        "turn_completed", **{**event.data, "state": "failed", "error": LOST_REQUEST}
                    )
            out.append(event)
        return out

    def _clear_turn_approvals(self) -> None:
        """A turn ended: its requests went with it. A background-origin request did not belong
        to it (#1725) and stays pending until its own task ends."""
        self._approvals = {
            key: item for key, item in self._approvals.items() if item.get("origin") is not None
        }

    def turn_approvals(self) -> bool:
        """Is the operator's own turn waiting on a request? A background-origin one holds no
        operator message back (#1725)."""
        return any(item.get("origin") is None for item in self._approvals.values())

    def origin_live(self, origin: dict) -> bool:
        """Does the background task a request was presented for still run, as that same task?"""
        return False

    def _refusal(self, item: dict) -> dict:
        """The answer to one held request that can no longer be decided: a refusal, never allow."""
        raise NotImplementedError

    def _retire_origins(self, stale) -> list[NativeEvent]:
        """A background task ended, or its id now names another task (#1725): the requests it
        made can no longer be decided. Each is refused and retracted, once."""
        out: list[NativeEvent] = []
        for key, item in list(self._approvals.items()):
            origin = item.get("origin")
            if origin is not None and stale(origin):
                del self._approvals[key]
                self._origin_consumed.add(key)
                out.append(_event("send", frame=self._refusal(item)))
                out.append(_event("approval_cancelled", request_id=key))
        return out

    def _invalidate_origins(self) -> list[NativeEvent]:
        """A frame that ended a task or a request was lost, and which one can't be said (#1725). So
        no background request is asserted still live: each is refused and retracted, and the set
        reads unknown (`overflow`) until the client reports its whole list again."""
        self.overflow = True
        return self._retire_origins(lambda origin: True)

    def _withdrawn(self, key: str) -> list[NativeEvent]:
        """The agent took a request back. A background one stays answered across operator turns."""
        item = self._approvals.pop(key, None)
        if item is not None:
            spent = self._origin_consumed if item.get("origin") is not None else self._consumed
            spent.add(key)
        return [_event("approval_cancelled", request_id=key)]

    def _after_turn(self) -> None:
        """What a client keeps per turn, dropped with it."""

    def _ended_uncertain(self, why: str) -> list[NativeEvent]:
        """End the turn in flight without claiming how it went (#1725)."""
        operation, turn = self.operation_id, self.native_turn_id
        self.operation_id = None
        self.native_turn_id = None
        self._clear_turn_approvals()
        self._after_turn()
        return self._uncertain(operation, turn, why)

    def frame_lost(self, head: bytes = b"", tail: bytes = b"") -> list[NativeEvent]:
        """A frame over the read bound was dropped (#1725): always a visible row. ``head`` and
        ``tail`` are its two ends, enough to tell what was lost. Only frames that can't matter to
        a turn are known by name; anything else may be what the turn was waiting for."""
        return self._row(FRAME_SKIPPED)

    def _unreadable(self, row: list[NativeEvent]) -> list[NativeEvent]:
        """It may be a request, and nothing can answer it: the worker ends the connection, the
        turn in flight reads uncertain, and the operator is told why. Fail closed, never silent."""
        self.unread_frame = True
        if self.operation_id is None:
            return row
        return [*row, *self._ended_uncertain(LOST_UNREADABLE)]

    #: The adapter kind ``native_grants`` derives standing grants for (#1339).
    ADAPTER = ""

    def _always(self, request_id: str, grant: Any, cwd: str | None) -> Any:
        """The exact native grant for an ``always`` decision, re-derived from the request THIS
        connection holds (#1339): never trusted from the caller, refused unless the request itself
        proposed it. A background-origin request is one-shot only (#1725)."""
        from . import native_grants

        item = self._approvals.get(request_id)
        if (
            item is None
            or item.get("origin") is not None
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
        origin = item.get("origin") if item is not None else None
        if (
            item is None
            or self._closed
            or (
                origin is None
                and (
                    item["operation_id"] != self.operation_id
                    or item["native_turn_id"] != self.native_turn_id
                )
            )
        ):
            raise ProtocolError("permission request is no longer pending for this turn")
        if origin is not None:
            # One-shot, and only while the task it was presented for runs (#1725).
            if decision not in {"approve", "reject"}:
                raise ProtocolError("a background task's request is approved once or declined")
            if not self.origin_live(origin):
                raise ProtocolError("that background task is no longer running")
        # Consumption is deliberate: failed/uncertain writes are never retried by a codec.
        (self._origin_consumed if origin is not None else self._consumed).add(request_id)
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


#: Read-only discovery requests (#1726) and Codex's MCP connection states for them. An unknown
#: state is `pending`: never shown healthy on a word BattleLab does not know.
_CODEX_DISCOVERY = frozenset({"mcpServerStatus/list", "skills/list"})
_CODEX_MCP_STATE = {
    "connected": "connected",
    "starting": "pending",
    "notStarted": "pending",
    "authenticationRequired": "needs_auth",
    "failed": "failed",
    "cancelled": "failed",
    "disabled": "disabled",
}

#: The approval kind of an MCP tool call Codex asks about (#1506), and the elicitation action each
#: offered decision answers with (once only — see `CodexCodec.decide`).
MCP_TOOL = "mcp_tool"
_MCP_ACTIONS = {"approve": "accept", "reject": "decline"}


#: What Codex sends with no id, so nothing waits on an answer: `ServerNotification.json` of
#: `codex app-server generate-json-schema` (0.164.0), by exact name. Never inferred from a prefix:
#: `item/tool/call` is a request. A method not on this list whose id can't be read may be one, so it
#: ends the connection (#1725); a later Codex's new notification costs that only when over 1 MiB.
CODEX_NOTIFICATIONS = frozenset(
    {
        "account/gatewayOAuth/changed",
        "account/login/completed",
        "account/rateLimits/updated",
        "account/updated",
        "app/list/updated",
        "autoApprovalReview/strictReviewRequired",
        "command/exec/outputDelta",
        "configWarning",
        "deprecationNotice",
        "error",
        "externalAgentConfig/import/completed",
        "externalAgentConfig/import/progress",
        "fs/changed",
        "fuzzyFileSearch/sessionCompleted",
        "fuzzyFileSearch/sessionUpdated",
        "guardianWarning",
        "hook/completed",
        "hook/started",
        "item/agentMessage/delta",
        "item/autoApprovalReview/completed",
        "item/autoApprovalReview/started",
        "item/commandExecution/outputDelta",
        "item/commandExecution/terminalInteraction",
        "item/completed",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/mcpToolCall/progress",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "mcpServer/event/stream/notification",
        "mcpServer/oauthLogin/completed",
        "mcpServer/startupStatus/updated",
        "model/rerouted",
        "model/safetyBuffering/updated",
        "model/verification",
        "modelProvider/authRecoveryCompleted",
        "modelProvider/authRecoveryStarted",
        "process/exited",
        "process/outputDelta",
        "project/changed",
        "remoteControl/status/changed",
        "serverRequest/resolved",
        "skills/changed",
        "thread/archived",
        "thread/attachment/updated",
        "thread/closed",
        "thread/compacted",
        "thread/deleted",
        "thread/environment/connected",
        "thread/environment/disconnected",
        "thread/goal/cleared",
        "thread/goal/updated",
        "thread/name/updated",
        "thread/prediction/updated",
        "thread/project/updated",
        "thread/queue/changed",
        "thread/readState/changed",
        "thread/realtime/closed",
        "thread/realtime/error",
        "thread/realtime/item/completed",
        "thread/realtime/item/started",
        "thread/realtime/item/transcript/delta",
        "thread/realtime/itemAdded",
        "thread/realtime/outputAudio/delta",
        "thread/realtime/sdp",
        "thread/realtime/started",
        "thread/realtime/transcript/delta",
        "thread/realtime/transcript/done",
        "thread/reverted",
        "thread/settings/updated",
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "thread/unarchived",
        "turn/completed",
        "turn/diff/updated",
        "turn/moderationMetadata",
        "turn/plan/updated",
        "turn/started",
        "warning",
        "windows/worldWritableWarning",
        "windowsSandbox/setupCompleted",
    }
)


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
        # The tools (#1726): MCP servers from the CLI's own startup reports and `mcpServerStatus/
        # list`, and its skills from `skills/list`, both asked once the thread is bound. Codex
        # reports no use of a skill (BattleLab sends text input only), so none is ever counted.
        self._env = _Environment(skill_use=False)
        self._mcp: dict[str, str] = {}
        self._mcp_pages = 0
        self._cwd: str | None = None
        # One `skills/list` in flight; a change meanwhile asks again once it answers (review on
        # #1811: an unthrottled ask per `skills/changed` could fill the request table).
        self._skills_inflight = False
        self._skills_dirty = False
        # Background terminals (#1724, measured on 0.164.0: `fixtures/codex_background_terminal`).
        # A `commandExecution` item with a `processId` still in progress when its turn completes
        # keeps running; its `item/completed` comes later under that ended turn's id. Item id →
        # (turn id, command) while in progress, then → command once it outlived its turn.
        self._commands: dict[str, tuple[str | None, str]] = {}
        self.terminals: dict[str, str] = {}

    @staticmethod
    def argv(binary: str) -> list[str]:
        return [_path(binary), "app-server", "--listen", "stdio://"]

    def _track_command(self, method: str, params: dict) -> list[NativeEvent]:
        """Follow every command item of this thread, any turn: one that outlives its turn is an
        engine-reported background task until its own `item/completed` (#1724). Bounded: past
        `MAX_PENDING` a command is simply not tracked (the process floor still sees it)."""
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "commandExecution":
            return []
        item_id = _identity(item.get("id"))
        if method == "item/started":
            if item.get("status") == "inProgress" and item.get("processId") is not None:
                if len(self._commands) >= MAX_PENDING:
                    self.overflow = True  # not tracked: the set is no longer complete (#1725)
                    return []
                turn = params.get("turnId")
                command = _text(item.get("command"), limit=TASK_METADATA_LIMITS["description"])
                self._commands[item_id] = (turn if isinstance(turn, str) else None, command)
            return []
        self._commands.pop(item_id, None)
        if item_id not in self.terminals:
            return []
        del self.terminals[item_id]
        status = item.get("status")
        state = {"completed": "completed", "failed": "failed", "declined": "stopped"}.get(
            status if isinstance(status, str) else "", "ended"
        )
        return [
            _event(
                "background",
                task_id=item_id,
                state=state,
                active=bool(self.terminals),
                task_state=state,
            )
        ]

    def _outlived(self, turn_id: str) -> list[NativeEvent]:
        """The command items still running when ``turn_id`` completed: background terminals."""
        out = []
        for item_id, (turn, command) in list(self._commands.items()):
            if turn != turn_id:
                continue
            del self._commands[item_id]
            if len(self.terminals) >= MAX_PENDING:
                self.overflow = True  # not tracked: the set is no longer complete (#1725)
                continue
            self.terminals[item_id] = command
            out.append(
                _event(
                    "background",
                    task_id=item_id,
                    state="running",
                    active=True,
                    task_state="running",
                    task_type="background_terminal",
                    **({"description": command} if command else {}),
                )
            )
        return out

    def retire_terminals(self, item_ids) -> list[NativeEvent]:
        """No process of the agent's runs any more (the process floor read its group), so none
        of these terminals, all reported before that scan began, can: each ended unobserved
        (#1724). A ghost never pins a worker."""
        ended = sorted(set(item_ids) & self.terminals.keys())
        for item_id in ended:
            del self.terminals[item_id]
        return [
            _event(
                "background",
                task_id=item_id,
                state="ended",
                active=bool(self.terminals),
                task_state="ended",
            )
            for item_id in ended
        ]

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
        self._cwd = params["cwd"]
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
        self._cwd = params["cwd"]
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

    def _after_turn(self) -> None:
        self._patches.clear()
        self._mcp_calls.clear()
        self._early_frames, self._early_size = [], 0

    def _early_fits(self, size: int) -> bool:
        return len(self._early_frames) < MAX_PENDING and self._early_size + size <= MAX_FRAME_BYTES

    def _evict_early_pieces(self, size: int) -> None:
        """Streamed pieces go, oldest first, only as far as ``size`` needs."""
        kept, count = [], len(self._early_frames)
        for frame in self._early_frames:
            full = count >= MAX_PENDING or self._early_size + size > MAX_FRAME_BYTES
            if full and frame.get("method") == "item/agentMessage/delta":
                self._early_size -= len(encode(frame))
                count -= 1
                continue
            kept.append(frame)
        self._early_frames = kept

    def origin_live(self, origin: dict) -> bool:
        """A request of a turn the agent started itself (#1725): only while that turn runs."""
        return (
            origin.get("kind") == "agent_turn"
            and origin.get("generation") == self.generation
            and origin.get("task_id") == self.background_turn
            and origin.get("agent_id") == self._unsolicited
        )

    def _refusal(self, item: dict) -> dict:
        return {"id": item["id"], "result": {"decision": "decline"}}

    def frame_lost(self, head: bytes = b"", tail: bytes = b"") -> list[NativeEvent]:
        method, request_id, readable, replies = _lost_rpc(head, tail)
        row = self._row(FRAME_SKIPPED)
        if not readable:
            return self._unreadable(row)
        if method is not None and request_id is not None:
            # The agent asked something BattleLab can't read: refused, never answered blind. The
            # turn goes on, and its end isn't claimed as a success (`settle_lost`).
            if self.operation_id is not None:
                self.lost_turn = self.operation_id
            return [*row, *self._refuse(request_id)]
        if method == "turn/completed":
            out = [*row]
            if self.operation_id is not None:
                out += self._ended_uncertain(LOST_RESULT)
            if self._unsolicited is not None:
                # The agent's own turn may be the one that ended (it is, if it is the only one),
                # how isn't known, and nothing of it is trusted past this.
                out += self._turn({"id": self._unsolicited, "status": "failed"}, completed=True)
            return out
        if method == "serverRequest/resolved":
            # Its request id is inside its params, out of reach: which request it ended can't be
            # said, so none of the agent's own requests is asserted still open (#1725).
            return [*row, *self._invalidate_origins()]
        if method is not None:
            # Named a notification: nothing waits on it. Anything else may be a request whose id
            # couldn't be read.
            return row if method in CODEX_NOTIFICATIONS else self._unreadable(row)
        if request_id is not None:
            pending = self._requests.get(request_id) if isinstance(request_id, str) else None
            if pending is not None:
                if pending[0] in _CODEX_DISCOVERY:
                    # Best effort (#1726): as an error reply, so what waited for it goes on.
                    return [*row, *self._reply({"id": request_id, "error": {"message": ""}})]
                self._requests.pop(request_id, None)
                if pending[0] == "turn/start" and pending[1] == self.operation_id:
                    return [*row, *self._ended_uncertain(LOST_RESULT)]
                return row
            # Not a reply to anything asked, and it says no more: it may be a request.
            return row if replies else self._unreadable(row)
        return self._unreadable(row)

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
                # Its requests can't be decided any more (#1725), whatever Codex says of them.
                retired = self._retire_origins(lambda origin: origin.get("kind") == "agent_turn")
                return [
                    _event(
                        "background",
                        native_turn_id=turn_id,
                        state=state,
                        active=False,
                        background_turn=None,
                    ),
                    *retired,
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
        self._clear_turn_approvals()
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
        if action in _CODEX_DISCOVERY:
            # Best effort (#1726): a client that cannot answer leaves that list unreported.
            out = self._discovered(action, frame.get("result")) if "error" not in frame else []
            if action == "skills/list":
                self._skills_inflight = False
                if self._skills_dirty:
                    self._skills_dirty = False
                    out.extend(self._skills_request())
            return out
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
                ),
                *(self._discover() if action in {"thread/start", "thread/resume"} else ()),
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

    # --- the plan and the tools (#1726) ---------------------------------------------------------

    def _discover(self) -> list[NativeEvent]:
        """Ask the bound thread for its MCP servers (one per page: a page carries that server's
        tool inventory, which must stay far below the frame bound) and its skills. Read-only."""
        self._mcp_pages = 0
        return [*self._mcp_page(None), *self._skills_request()]

    def _ask(self, action: str, params: dict[str, Any]) -> list[NativeEvent]:
        """A discovery request, or nothing: discovery never fails the connection."""
        try:
            return [_event("send", frame=self._request(action, params))]
        except ProtocolError:
            return []

    def _mcp_page(self, cursor: str | None) -> list[NativeEvent]:
        params: dict[str, Any] = {
            "threadId": self.native_id,
            "detail": "toolsAndAuthOnly",
            "limit": 1,
        }
        if cursor is not None:
            params["cursor"] = cursor
        self._mcp_pages += 1
        return self._ask("mcpServerStatus/list", params)

    def _skills_request(self) -> list[NativeEvent]:
        if self._skills_inflight:
            self._skills_dirty = True
            return []
        out = self._ask("skills/list", {"cwds": [self._cwd]} if self._cwd else {})
        self._skills_inflight = bool(out)
        return out

    def _mcp_report(self, name: Any, status: str) -> list[NativeEvent]:
        if not isinstance(name, str) or not name.strip():
            return []
        name = _cut(name.strip(), ENV_NAME)
        if name not in self._mcp and len(self._mcp) >= ENV_MCP:
            return []
        self._mcp[name] = status
        return self._env.update(mcp=_mcp_rows(list(self._mcp.items())))

    def _discovered(self, action: str, result: Any) -> list[NativeEvent]:
        if not isinstance(result, dict):
            return []
        if action == "skills/list":
            names = []
            for entry in result.get("data") or []:
                if isinstance(entry, dict) and isinstance(entry.get("skills"), list):
                    names.extend(
                        skill
                        for skill in entry["skills"]
                        if isinstance(skill, dict) and skill.get("enabled") is not False
                    )
            return self._env.update(skills=[{"name": n} for n in _names(names, ENV_SKILLS)])
        out = []
        for server in result.get("data") or []:
            if isinstance(server, dict):
                status = _CODEX_MCP_STATE.get(server.get("runtimeStatus"), "pending")
                out.extend(self._mcp_report(server.get("name"), status))
        # A thread with no servers says so: an empty list, not "not reported".
        out.extend(self._env.update(mcp=_mcp_rows(list(self._mcp.items()))))
        cursor = result.get("nextCursor")
        if isinstance(cursor, str) and cursor and self._mcp_pages < ENV_MCP:
            out.extend(self._mcp_page(cursor))
        return out

    def _startup_status(self, params: dict) -> list[NativeEvent]:
        status = params.get("status")
        if status == "failed" and params.get("failureReason") == "reauthenticationRequired":
            state = "needs_auth"
        else:
            state = {"starting": "pending", "ready": "connected"}.get(status, "failed")
        return self._mcp_report(params.get("name"), state)

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
        if self._no_room(request_id):
            return [*self._row(REQUEST_REFUSED), *self._refuse(request_id)]
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
        # A command asked for inside a turn the agent started itself (#1725): the protocol names
        # that turn, so it reaches the operator as that turn's own. File changes and MCP calls of
        # such a turn stay refused: nothing presents their patch or call outside a running turn.
        origin = (
            self._turn_origin(request_id, params)
            if method == "item/commandExecution/requestApproval" and not correlated
            else None
        )
        # grantRoot explicitly asks for a lasting filesystem grant, not one patch.
        if (
            not allowed
            or not (correlated or origin is not None)
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
        if _oversized(frame) or self._no_room(request_id):
            return [*self._row(REQUEST_REFUSED), *self._refuse(request_id)]
        presented: dict[str, Any] = dict(params)
        if origin is not None:
            presented["background_task"] = _origin_public(origin)
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
        if origin is not None and len(_summary(presented)) >= MAX_TEXT:
            # With the origin the operator must see, it can't be shown whole (#1725): never a cut
            # summary presented as complete.
            return [*self._row(REQUEST_REFUSED), *self._refuse(request_id)]
        if complete and len(_summary(presented)) >= MAX_TEXT:
            presented, complete = dict(params), False  # cannot be shown whole
        digest = _digest(
            {"frame": frame, "presented": presented, **({"origin": origin} if origin else {})}
        )
        key = self._remember(
            request_id,
            {
                "id": request_id,
                "operation_id": None if origin else self.operation_id,
                "native_turn_id": None if origin else self.native_turn_id,
                "params": params,
                "frame": frame,
                "digest": digest,
                "complete": complete,
                "presented": presented,
                "kind": "command"
                if method == "item/commandExecution/requestApproval"
                else "file_change",
                **({"origin": origin} if origin else {}),
            },
        )
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=digest,
                native_turn_id=None if origin else self.native_turn_id,
                operation_id=None if origin else self.operation_id,
                item_id=item_id,
                tool="command"
                if method == "item/commandExecution/requestApproval"
                else "file_change",
                # The COMPLETE request (network context, proposed amendments, cwd, and for a
                # file change its proposed patch), not an excerpt: what the operator sees is
                # everything a decision approves, and the digest binds exactly that.
                summary=_summary(presented),
                complete=complete,
                choices=(["approve", "reject"] if complete else ["reject"])
                if origin
                else ["approve", "reject", "cancel"],
                **({"background_task": origin} if origin else {}),
            )
        ]

    def _turn_origin(self, request_id: Any, params: dict) -> dict | None:
        """The origin of a command request inside a turn the agent started itself, if that is
        what this names: its thread, and the very turn this connection saw start. A repeat of a
        request keeps the origin it was presented with."""
        if (
            self._unsolicited is None
            or self.background_turn is None
            or params.get("threadId") != self.native_id
            or params.get("turnId") != self._unsolicited
        ):
            return None
        held = self._approvals.get(_approval_key(request_id))
        if held is not None and held.get("origin") is not None:
            return held["origin"]
        return {
            "kind": "agent_turn",
            "id": str(uuid.uuid4()),
            "task_id": self.background_turn,
            "agent_id": self._unsolicited,
            "generation": self.generation,
            "epoch": 0,
            "description": "A turn the agent started for itself",
        }

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
            return  # not retained (#1725): like an unreadable call, its elicitation is refused
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
            # Not retained (#1725). It is only ever shown beside a decline, and its approval then
            # says so by showing no patch.
            return []
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
            # A streamed piece of text is the one thing this can lose (#1725): its message's
            # `item/completed` carries it whole. So a piece never takes room from anything else: it
            # is dropped when there is none, and pieces already held give way for what matters. If
            # what matters still doesn't fit, it may be a request or how the turn ended, and losing
            # that could cost an effect: the connection ends.
            droppable = method == "item/agentMessage/delta"
            if not self._early_fits(size):
                if droppable:
                    return []
                self._evict_early_pieces(size)
                if not self._early_fits(size):
                    raise ProtocolError("uncorrelated native event buffer exceeded")
            self._early_frames.append(frame)
            self._early_size += size
            return []
        if "id" in frame:
            return self._approval(frame, params)
        if self.native_id is not None and method == "mcpServer/startupStatus/updated":
            # A thread-less report is the CLI's own startup of a server this thread uses.
            if params.get("threadId") in (None, self.native_id):
                return self._startup_status(params)
            return []
        if self.native_id is not None and method == "skills/changed":
            return self._skills_request()
        if params.get("threadId") != self.native_id or self.native_id is None:
            return []
        if method == "turn/plan/updated":
            # The plan belongs to the conversation, whichever turn (or own turn) updated it.
            entries = _plan_entries(params.get("plan"), "step")
            if entries is None:
                return []
            explanation = params.get("explanation")
            return self._plan_once(
                entries=entries,
                **(
                    {"explanation": _cut(explanation, PLAN_EXPLANATION)}
                    if isinstance(explanation, str) and explanation.strip()
                    else {}
                ),
            )
        if method == "item/completed" and isinstance(params.get("item"), dict):
            item = params["item"]
            if item.get("type") == "plan":
                # The proposed plan document, from the settled item: its streamed deltas are
                # explicitly not promised to add up to it.
                document = _plan_document(item.get("text"))
                return [] if document is None else [_event("plan", document=document)]
        if method == "serverRequest/resolved":
            return self._withdrawn(_approval_key(params.get("requestId")))
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise ProtocolError("native turn notification has no turn")
            out = self._turn(turn, completed=method == "turn/completed")
            if method == "turn/completed":
                out += self._outlived(_identity(turn.get("id")))
            return out
        tracked = (
            self._track_command(method, params)
            if method in {"item/started", "item/completed"}
            else []
        )
        if params.get("turnId") != self.native_turn_id or self.operation_id is None:
            return tracked
        return tracked + self._item(method, params)

    def _item(self, method: str, params: dict) -> list[NativeEvent]:
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
                # `call` (#1726): `item/started` starts the call; the MCP server it names is the
                # CLI's own (`""` when it named none: counted unattributed, never guessed).
                extra: dict[str, Any] = {}
                if method == "item/started":
                    extra["call"] = True
                    if kind == "mcpToolCall":
                        server = item.get("server")
                        extra["mcp_server"] = (
                            _cut(server.strip(), ENV_NAME)
                            if isinstance(server, str) and server.strip()
                            else ""
                        )
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
                        **extra,
                    )
                ]
        return []


# How the CLI's lifecycle ends an input: answered, or interrupted (#1427).
_LIFECYCLE_ENDED = frozenset({"completed", "cancelled"})
# What may come between an input's `completed` and the result naming it without ending the wait
# (#1644): bookkeeping that is no turn's output. Anything else ends the input without its result.
_BETWEEN_END_AND_RESULT = frozenset(
    {
        "task_started",
        "task_progress",
        "task_notification",
        "task_updated",
        # The full task list (#1724): a task ending with the turn sends it before the result.
        "background_tasks_changed",
        "thinking_tokens",
    }
)


#: Claude's task-list tools (#1726): the ones that change it, and the one that reads it whole.
_TASK_EDITS = frozenset({"TaskCreate", "TaskUpdate"})
_TASK_TOOLS = _TASK_EDITS | {"TaskList"}

#: Claude's MCP server states in `system/init` (#1726). An unknown word is `pending`, never healthy.
_CLAUDE_MCP_STATE = {
    "connected": "connected",
    "failed": "failed",
    "pending": "pending",
    "needs-auth": "needs_auth",
    "disabled": "disabled",
}


def _between_end_and_result(frame: dict) -> bool:
    kind = frame.get("type")
    if kind in {"rate_limit_event", "control_response"}:
        return True
    return kind == "system" and frame.get("subtype") in _BETWEEN_END_AND_RESULT


_DROPPED = frozenset({"cancelled", "discarded"})  # an input the CLI never ran
#: How a task report says the task is over.
_CLAUDE_TASK_ENDED = frozenset({"completed", "failed", "stopped", "killed"})
#: The reports about one task, by their exact `subtype`.
_CLAUDE_TASK_REPORTS = frozenset(
    {"task_started", "task_progress", "task_notification", "task_updated"}
)
#: Frames nothing waits on an answer to and a turn's outcome does not hang on, by their exact `type`
#: (Claude Code 2.1.314's own schema): output (its result repeats the reply), bookkeeping the full
#: task list reconciles, replies to controls and a withdrawn request. Any other type too large to
#: read may be a request nothing can answer, so it ends the connection (#1725).
_CLAUDE_OBSERVED = frozenset(
    {
        "assistant",
        "user",
        "stream_event",
        "system",
        "rate_limit_event",
        "command_lifecycle",
        "control_response",
        "tool_progress",
        "tool_use_summary",
        "control_cancel_request",
        "auth_status",
        "prompt_suggestion",
        "keep_alive",
    }
)


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
        # What each tracked task is (#1725): its description, and an epoch a request made by it
        # is bound to. An id that ends and starts again is another task.
        self._task_facts: dict[str, dict[str, Any]] = {}
        self._epochs = 0
        # Task ids that ended this generation. The journal's fold keeps such an id ended for good,
        # so a request from one could never be shown: it is refused, never held.
        self._ended_tasks: set[str] = set()
        # Tasks a lost frame left in an unknown state (#1725), and ids it named that no task had.
        # None is trusted until a whole list names it or (an id no task has) its own start.
        self._quarantined: set[str] = set()
        self._quarantine_spilled = False  # past its bound nothing is trusted, until a whole list
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
        # The plan and the tools (#1726). `system/init` names MCP servers, skills and slash
        # commands; a `Skill` call is a REPORTED use. The task list is built from the Task tools'
        # own structured results (`tool_use_result`), and shown only while this connection knows
        # the WHOLE list: from task #1 of a fresh list, or from a `TaskList` answer.
        self._env = _Environment(skill_use=True)
        self._mcp_names: list[str] = []
        self._plan_tasks: dict[str, dict[str, str]] = {}
        self._tasks_whole = False
        self._stale_sent = False  # a `stale` marker went out since the list was last whole
        self._task_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        # The operator turn's own output said its vendor login failed (`error:
        # authentication_failed` on an assistant frame): a failed end offers Sign in.
        self._signin = False

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

    def at_capacity(self) -> bool:
        """The ledger reached its high-water mark (#1725): this connection takes no new turn and
        retires between turns (`rollover`). Below the hard bound, so nothing already admitted
        is refused and nothing running is cut."""
        return len(self._results) >= MAX_RESULTS - RESULTS_RESERVE

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
        self._signin = False
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
        return (bool(self._background) or self.overflow) and not self.send_now_ready()

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
        self._signin = False
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
                self._clear_turn_approvals()
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

    def _ended_uncertain(self, why: str) -> list[NativeEvent]:
        operation, turn = self.operation_id, self.native_turn_id
        self._distrust()
        self._cycle = None
        self.operation_id = None
        self.native_turn_id = None
        self._clear_turn_approvals()
        self._promote_ahead()
        return self._uncertain(operation, turn, why)

    def _remedy(self, state: str) -> dict[str, str]:
        """The ending operator turn's remedy (`TURN_REMEDIES`), consumed: only a failed turn
        carries one, and the next turn starts without it."""
        signin, self._signin = self._signin, False
        return {"remedy": "signin"} if signin and state == "failed" else {}

    def _ended_without_result(self, state: str) -> list[NativeEvent]:
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        interrupted = state == "cancelled"
        remedy = self._remedy("interrupted" if interrupted else "failed")
        self._distrust()
        self._cycle = None
        self.operation_id = None
        self.native_turn_id = None
        self._clear_turn_approvals()
        self._promote_ahead()
        return [
            _event(
                "turn_completed",
                **common,
                state="interrupted" if interrupted else "failed",
                text="",
                background_active=bool(self._background),
                native_state=self._session_state,
                error="" if interrupted else "the agent ended this turn without its result",
                **remedy,
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
        agent = request.get("agent_id")
        ended = subtype == "can_use_tool" and self._ended_task(agent)
        if ended or (subtype == "can_use_tool" and self._quarantined_task(agent)):
            # Held, it would wait for ever behind a card nothing can show, with the session busy
            # and a full ledger held (#1725). Fail closed, visibly, and let the agent go on.
            row = self._row(REQUEST_ENDED_TASK if ended else REQUEST_UNCERTAIN_TASK)
            if row[0].kind != "tool":
                self.requests_refused += 1
            return [*row, _event("send", frame=self._control_refusal(request_id))]
        origin = self._task_origin(request_id, agent) if subtype == "can_use_tool" else None
        # A callback from a background/sub-agent (`agent_id`) never belongs to the operator's turn
        # (Hermes on #1278). One whose agent is a task this generation reports as BACKGROUNDED
        # reaches the operator as that task's own (#1725). Any other is refused, as is one with no
        # turn to ask for: a foreground sub-agent's is not "a background task, not your current
        # turn", and it is strictly safer to keep refusing it than to present it as one.
        if (
            subtype != "can_use_tool"
            or (agent is None and self.operation_id is None)
            or (agent is not None and origin is None)
        ):
            return [_event("send", frame=self._control_refusal(request_id))]
        original_input = request.get("input")
        if not isinstance(original_input, dict):
            raise ProtocolError("native permission input must be an object")
        tool_name = _identity(request.get("tool_name"))
        item_id = _identity(request.get("tool_use_id"))
        # Everything the prompt carries (blocked_path, decision_reason, title, suggestions,
        # input …): the operator sees what the decision answers, not only the tool input.
        presented = {key: value for key, value in request.items() if key != "subtype"}
        if origin is not None:
            presented["background_task"] = _origin_public(origin)
        # The origin counts: it is part of what must be shown whole, never a cut summary.
        if (
            _oversized(frame)
            or self._no_room(request_id)
            or (origin is not None and len(_summary(presented)) >= MAX_TEXT)
        ):
            return [
                *self._row(REQUEST_REFUSED),
                _event("send", frame=self._control_refusal(request_id)),
            ]
        digest = _digest(frame) if origin is None else _digest({"frame": frame, "origin": origin})
        key = self._remember(
            request_id,
            {
                "id": request_id,
                "operation_id": None if origin else self.operation_id,
                "native_turn_id": None if origin else self.native_turn_id,
                "input": original_input,
                "tool": tool_name,
                "item_id": item_id,
                "frame": frame,
                "digest": digest,
                "complete": True,
                "presented": presented,
                "kind": tool_name,
                **({"origin": origin} if origin else {}),
            },
        )
        asks = tool_name == native_questions.ASK_TOOL
        return [
            _event(
                "approval",
                request_id=key,
                payload_digest=digest,
                native_turn_id=None if origin else self.native_turn_id,
                operation_id=None if origin else self.operation_id,
                item_id=item_id,
                tool=tool_name,
                summary=_summary(presented),
                complete=True,
                # An agent question (#1463) is answered, skipped or stopped — never approved. One
                # BattleLab can't answer exactly (malformed, repeated questions or labels) can
                # still be skipped. A background task's request is one-shot, and a question it
                # asks can only be skipped: its answer has no turn to go to (#1725).
                choices=(["reject"] if asks else ["approve", "reject"])
                if origin
                else (
                    ["answer", "reject", "cancel"]
                    if native_questions.claude_questions(original_input) is not None
                    else ["reject", "cancel"]
                )
                if asks
                else ["approve", "reject", "cancel"],
                **({"background_task": origin} if origin else {}),
            )
        ]

    @staticmethod
    def _control_refusal(request_id: str) -> dict:
        return {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": "Unsupported or uncorrelated control request",
            },
        }

    def _refusal(self, item: dict) -> dict:
        return self._response(
            item["id"],
            {"behavior": "deny", "message": "That background task ended", "interrupt": False},
        )

    def _task_origin(self, request_id: str, agent: Any) -> dict | None:
        """A sub-agent's `agent_id` is the id of its task (`task_started.task_id`, the CLI's
        `local_agent` task id; read from Claude Code 2.1.314): a request names a task of this
        generation exactly, or it maps to nothing. A repeat keeps the origin it was shown with."""
        task = self._task_facts.get(agent) if isinstance(agent, str) else None
        if task is None or task.get("backgrounded") is not True:
            return None  # only what is positively known as backgrounded
        held = self._approvals.get(_approval_key(request_id))
        if held is not None and held.get("origin") is not None:
            return held["origin"]
        return {
            "kind": "task",
            "id": str(uuid.uuid4()),
            "task_id": agent,
            "agent_id": agent,
            "generation": self.generation,
            "epoch": task["epoch"],
            **({"description": task["description"]} if task.get("description") else {}),
        }

    def origin_live(self, origin: dict) -> bool:
        task = self._task_facts.get(origin.get("task_id"))
        return (
            task is not None
            and not self._quarantined_task(origin.get("task_id"))
            and task.get("backgrounded") is True
            and origin.get("kind") == "task"
            and origin.get("generation") == self.generation
            and origin.get("epoch") == task["epoch"]
        )

    def _track_task(
        self, task_id: str, metadata: dict[str, str], backgrounded: Any = None
    ) -> list[NativeEvent]:
        """``backgrounded``: what the client said of it, if it did (`is_backgrounded`). A task it
        says is in the foreground blocks a tool call of the operator's own turn."""
        if task_id not in self._background:
            self._epochs += 1
            self._task_facts[task_id] = {"epoch": self._epochs}
        self._background.add(task_id)
        task = self._task_facts[task_id]
        if metadata.get("description"):
            task["description"] = metadata["description"]
        if isinstance(backgrounded, bool):
            task["backgrounded"] = backgrounded
            if not backgrounded:  # no longer what its open requests were presented as
                return self._retire_origins(lambda origin: origin.get("task_id") == task_id)
        return []

    def _ended_task(self, agent: Any) -> bool:
        """Did this id end in this generation? Past its bound nothing more is remembered, so
        nothing more is trusted: every agent id is refused, never one wrongly held."""
        if not isinstance(agent, str):
            return False
        return agent in self._ended_tasks or len(self._ended_tasks) >= MAX_CONSUMED

    def _quarantined_task(self, agent: Any) -> bool:
        return isinstance(agent, str) and (agent in self._quarantined or self._quarantine_spilled)

    def _quarantine(self, task_id: str) -> list[NativeEvent]:
        """A lost frame may have ended this task or changed what it is (#1725). It keeps the worker
        busy and stays in the inventory, since it may run; what it asked is refused, and so is
        whatever it asks next. The journal says uncertain, never running."""
        self.overflow = True
        if len(self._quarantined) < MAX_PENDING or task_id in self._quarantined:
            self._quarantined.add(task_id)
        else:
            self._quarantine_spilled = True
        retired = self._retire_origins(lambda origin: origin.get("task_id") == task_id)
        if task_id not in self._background:
            return retired
        said = {"state": TASK_UNCERTAIN, "task_state": TASK_UNCERTAIN}
        return [_event("background", task_id=task_id, active=True, **said), *retired]

    def _invalidate_origins(self) -> list[NativeEvent]:
        """Which task a lost frame was about can't be said: none known now is vouched for."""
        known = sorted(self._background | self._task_facts.keys())
        quarantined = [event for task_id in known for event in self._quarantine(task_id)]
        return [*quarantined, *super()._invalidate_origins()]

    def _forget_task(self, task_id: str) -> list[NativeEvent]:
        self._quarantined.discard(task_id)
        self._background.discard(task_id)
        self._task_facts.pop(task_id, None)
        if len(self._ended_tasks) < MAX_CONSUMED:
            self._ended_tasks.add(task_id)
        return self._retire_origins(lambda origin: origin.get("task_id") == task_id)

    def _lost_report(self, kind: str, fields: dict[str, Any]) -> list[NativeEvent]:
        """What a lost frame ended (#1725). No request may be decidable for an origin whose end or
        cancel was lost, so what its ends prove is applied, and anything else about a task
        quarantines it (every task known, with no identity). An operator turn's own request stays
        through a cancel it can't place: it holds that turn, and the CLI ignores an answer to one
        it withdrew."""
        if kind == "control_cancel_request":
            try:
                return self._withdrawn(_approval_key(fields.get("request_id")))
            except ProtocolError:
                return self._invalidate_origins()
        if kind != "system":
            return []
        subtype = fields.get("subtype")
        if not isinstance(subtype, str) or subtype == "background_tasks_changed":
            return self._invalidate_origins()
        if subtype not in _CLAUDE_TASK_REPORTS:
            return []
        try:
            task_id = _identity(fields.get("task_id"))
        except ProtocolError:
            return self._invalidate_origins()
        # A `task_updated` says its state inside a patch, which is never in reach.
        status = fields.get("status") if subtype == "task_notification" else None
        if isinstance(status, str) and status in _CLAUDE_TASK_ENDED:
            retired = self._forget_task(task_id)
            active = bool(self._background)
            ended = {"task_id": task_id, "state": status, "task_state": status}
            return [_event("background", active=active, **ended), *retired]
        # Any other report, `task_progress` too, can carry what the frame's middle hides.
        return self._quarantine(task_id)

    def frame_lost(self, head: bytes = b"", tail: bytes = b"") -> list[NativeEvent]:
        fields = _frame_fields(head, tail)
        kind = fields.get("type") if fields is not None else None
        row = self._row(FRAME_SKIPPED)
        # With no type named, it may be a request nothing can answer.
        if not isinstance(kind, str):
            return self._unreadable(row)
        if kind == "control_request":
            try:
                asked = _identity(fields.get("request_id"))
            except ProtocolError:
                return self._unreadable(row)
            # The agent asked something BattleLab can't read: refused, never answered blind. The
            # turn goes on, and its end isn't claimed as a success (`settle_lost`).
            refused = [_event("send", frame=self._control_refusal(asked))]
            if self.operation_id is not None:
                self.lost_turn = self.operation_id
            if self._cycle == "ended":
                return [*row, *refused, *self._ended_uncertain(LOST_RESULT)]
            return [*row, *refused]
        row += self._lost_report(kind, fields)
        if self._cycle == "ended":
            # It may be the result this input waits for (#1644): it ends now, never running for
            # ever, and never as a success nothing proved.
            return [*row, *self._ended_uncertain(LOST_RESULT)]
        if kind in _CLAUDE_OBSERVED:
            return row
        if kind != "result":
            return self._unreadable(row)
        if self._owned():
            return [*row, *self._ended_uncertain(LOST_RESULT)]
        return [*row, *self._agent_turn(False, "failed")]

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
            return self._withdrawn(_approval_key(frame.get("request_id")))
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
                ),
                *self._init_environment(frame),
            ]
        if kind == "system" and subtype in _CLAUDE_TASK_REPORTS:
            task_id = _identity(frame.get("task_id"))
            patch = frame.get("patch")
            status = (
                patch.get("status")
                if subtype == "task_updated" and isinstance(patch, dict)
                else frame.get("status")
            )
            state = _text(status, limit=64)
            # A task_updated patch is sparse: without status it says nothing about a paused
            # task's state. Keep the existing state/active semantics for dispatch, and carry the
            # explicit display state separately. Never copy an arbitrary patch or output path.
            source = patch if subtype == "task_updated" and isinstance(patch, dict) else frame
            metadata = {}
            for field, limit in TASK_METADATA_LIMITS.items():
                value = source.get(field)
                if isinstance(value, str) and value:
                    metadata[field] = value if len(value) <= limit else value[: limit - 1] + "…"
            retired: list[NativeEvent] = []
            if subtype in {"task_notification", "task_updated"} and state in _CLAUDE_TASK_ENDED:
                retired = self._forget_task(task_id)
            elif task_id not in self._background and len(self._background) >= MAX_PENDING:
                # More tasks than are tracked (#1725): this one is not, and the set is no longer
                # complete. Busy and said so, until the client reports its whole list again.
                self.overflow = True
                return [_event("background", state="activity", active=True)]
            else:
                if subtype == "task_started" and task_id in self._background:
                    # The same id started again is another task: what the old one asked is over.
                    retired = self._forget_task(task_id)
                if subtype == "task_started":
                    self._quarantined.discard(task_id)  # its own epoch, from here on
                retired += self._track_task(task_id, metadata, source.get("is_backgrounded"))
            reported = state or ("running" if subtype == "task_started" else "")
            return [
                _event(
                    "background",
                    task_id=task_id,
                    state=state or "running",
                    active=bool(self._background),
                    # One report isn't what settles a quarantined task: a whole list is.
                    task_state=TASK_UNCERTAIN if task_id in self._quarantined else reported,
                    **metadata,
                ),
                *retired,
            ]
        if kind == "system" and subtype == "background_tasks_changed":
            return self._tasks_changed(frame)
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
        unrecorded = False
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
            # Retain idle and background results too: observing them before submission is
            # not permission to attribute a later replay to the next operator turn. Past the bound
            # nothing more is retained (#1725): the high-water mark stopped new turns long before
            # this, so a replay of what goes unrecorded has no later turn to complete.
            unrecorded = len(self._results) >= MAX_RESULTS
            if not unrecorded:
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
            and unrecorded
            and not background
            and self.operation_id is not None
            and frame.get("user_message_uuid") != self.operation_id
        ):
            # Unrecorded, so a repeat of it would not be caught (#1725): only the result naming
            # this input can complete it now. The input still ends, by that or its lifecycle.
            self._distrust()
            return [_event("background", state="activity", active=True)]
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
            stale = (
                self._subagent_tasks(frame, kind)
                if frame.get("parent_tool_use_id") is not None
                else []
            )
            return [*(ended or [_event("background", state="activity", active=True)]), *stale]
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

    def _tasks_changed(self, frame: dict) -> list[NativeEvent]:
        """The CLI's FULL list of running background tasks (#1724): the set is reconciled to it,
        never just accumulated. A task the list no longer names ended (`ended`: its own terminal
        frame was missed); one it names that was never started is added. An unchanged list
        records nothing. Bounded like the task frames themselves."""
        tasks = frame.get("tasks")
        if not isinstance(tasks, list):
            raise ProtocolError("native background task list is malformed")
        listed: dict[str, dict[str, str]] = {}
        for task in tasks:
            if not isinstance(task, dict):
                raise ProtocolError("native background task list is malformed")
            metadata = {}
            for name in ("description", "task_type"):
                value = task.get(name)
                if isinstance(value, str) and value:
                    limit = TASK_METADATA_LIMITS[name]
                    metadata[name] = value if len(value) <= limit else value[: limit - 1] + "…"
            listed[_identity(task.get("task_id"))] = metadata
        # The list names background tasks only (#1725): one the client said runs in the foreground
        # was never on it, and leaving it is no end. Its own terminal frame is, unless a lost frame
        # may have been that: then, like any quarantined task, leaving it is the end.
        foreground = {
            task_id
            for task_id, task in self._task_facts.items()
            if task.get("backgrounded") is False
            and task_id not in listed
            and task_id not in self._quarantined
        }
        # A whole list settles whether the set is complete (#1725). One over the bound keeps what
        # is already tracked first, and says the rest isn't.
        room = MAX_PENDING - len(foreground)
        self.overflow = len(listed) > room
        if self.overflow:
            ordered = sorted(listed, key=lambda task_id: (task_id not in self._background, task_id))
            listed = {task_id: listed[task_id] for task_id in ordered[:room]}
        ended = sorted(self._background - listed.keys() - foreground)
        # A whole list settles every quarantined task (#1725): named, it runs, and it may not be
        # the task its open requests were for, which is why they stay refused (a new epoch); not
        # named, it is over.
        lifted = self._quarantined & listed.keys()
        started = sorted((listed.keys() - self._background) | lifted)
        retired = [event for task_id in ended for event in self._forget_task(task_id)]
        for task_id in sorted(self._quarantined - listed.keys()):
            retired += self._forget_task(task_id)
        self._quarantined.clear()
        self._quarantine_spilled = False
        for task_id in listed:
            # Being on it is the client saying so: a foreground task moved to the background too.
            self._track_task(task_id, listed[task_id], True)
        for task_id in lifted:
            self._epochs += 1
            self._task_facts[task_id]["epoch"] = self._epochs
        active = bool(self._background)
        return (
            retired
            + [
                _event(
                    "background", task_id=task_id, state="ended", active=active, task_state="ended"
                )
                for task_id in ended
            ]
            + [
                _event(
                    "background",
                    task_id=task_id,
                    state="running",
                    active=active,
                    task_state="running",
                    **listed[task_id],
                )
                for task_id in started
            ]
        )

    # --- the plan and the tools (#1726) ---------------------------------------------------------

    def _init_environment(self, frame: dict) -> list[NativeEvent]:
        """`system/init`'s MCP servers (name + the CLI's own status), skills and slash commands.
        Names only: a server's `source`, the tool list and every path stay behind."""
        servers = frame.get("mcp_servers")
        rows = []
        for server in servers if isinstance(servers, list) else ():
            if isinstance(server, dict) and isinstance(server.get("name"), str):
                status = server.get("status")
                rows.append((server["name"], _CLAUDE_MCP_STATE.get(status, "pending")))
        mcp = _mcp_rows(rows) if isinstance(servers, list) else None
        if mcp is not None:
            self._mcp_names = [row["name"] for row in mcp]
        skills = frame.get("skills")
        commands = frame.get("slash_commands")
        return self._env.update(
            mcp=mcp,
            skills=[{"name": n} for n in _names(skills, ENV_SKILLS)]
            if isinstance(skills, list)
            else None,
            commands=[{"name": n} for n in _names(commands, ENV_COMMANDS)]
            if isinstance(commands, list)
            else None,
        )

    def _mcp_server(self, tool: str) -> str | None:
        """The server an `mcp__<server>__<tool>` call went to: the longest server `system/init`
        named whose normalized name prefixes the tool. An MCP-shaped name no known server
        matches is `""` — counted as unattributed, never guessed onto a server. None: no MCP."""
        if not tool.startswith("mcp__"):
            return None
        best = ""
        for name in self._mcp_names:
            normalized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
            if tool.startswith(f"mcp__{normalized}__") and len(name) > len(best):
                best = name
        return best

    def _plan_call(self, item_id: str, name: str, tool_input: Any) -> list[NativeEvent]:
        if not isinstance(tool_input, dict):
            return []
        if name == "ExitPlanMode":
            document = _plan_document(tool_input.get("plan"))
            return [] if document is None else [_event("plan", document=document)]
        if name in {"TodoWrite", *_TASK_TOOLS}:
            # A checklist counts only once its call came back clean (review on #1811: 2.1.312
            # refuses TodoWrite, and a refused list must never replace the real one).
            out = []
            if len(self._task_calls) >= MAX_PENDING:
                evicted, _ = self._task_calls.pop(next(iter(self._task_calls)))
                if evicted in _TASK_TOOLS:
                    out = self._stale()  # its result can no longer be read: list unknown
            self._task_calls[item_id] = (name, tool_input)
            return out
        return []

    def _stale(self) -> list[NativeEvent]:
        """The task list can no longer be reconciled (a result lost, refused or unreadable, a
        task never seen, a sub-agent's change): not whole until a `TaskList` answer, and the
        shown list is marked possibly out of date — once."""
        self._tasks_whole = False
        if self._stale_sent:
            return []
        self._stale_sent = True
        self._last_plan = None
        return [_event("plan", stale=True)]

    def _subagent_tasks(self, frame: dict, kind: str) -> list[NativeEvent]:
        """A sub-agent's Task call changes the shared list where this connection cannot follow."""
        msg = frame.get("message")
        if (
            kind != "assistant"
            or not isinstance(msg, dict)
            or not isinstance(msg.get("content"), list)
        ):
            return []
        if any(
            isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _TASK_EDITS
            for b in msg["content"][:128]
        ):
            return self._stale()
        return []

    def _plan_result(self, item_id: Any, result: Any, ok: bool) -> list[NativeEvent]:
        call = self._task_calls.pop(item_id, None) if isinstance(item_id, str) else None
        if call is None:
            return []
        name, tool_input = call
        if name == "TodoWrite":
            entries = _plan_entries(tool_input.get("todos"), "content") if ok else None
            return [] if entries is None else self._plan_once(entries=entries)
        if not ok or not isinstance(result, dict):
            return self._stale()  # refused, or a result this frame does not let us read
        tasks = self._plan_tasks
        if name == "TaskList":
            listed = result.get("tasks")
            if not isinstance(listed, list):
                return []
            tasks.clear()
            for task in listed[:PLAN_ENTRIES]:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    tasks[task["id"]] = {
                        "text": task.get("subject") if isinstance(task.get("subject"), str) else "",
                        "status": task.get("status") if isinstance(task.get("status"), str) else "",
                    }
            self._tasks_whole = True
        elif name == "TaskCreate":
            task = result.get("task")
            if not isinstance(task, dict) or not isinstance(task.get("id"), str):
                return self._stale()
            if not tasks and task["id"] == "1":
                self._tasks_whole = True  # a fresh list: this connection sees all of it
            subject = task.get("subject", tool_input.get("subject"))
            if len(tasks) < PLAN_ENTRIES:
                tasks[task["id"]] = {
                    "text": subject if isinstance(subject, str) else "",
                    "status": "pending",
                }
        else:  # TaskUpdate
            task_id = tool_input.get("taskId")
            if result.get("success") is not True or not isinstance(task_id, str):
                return self._stale()
            if task_id not in tasks:
                return self._stale()  # a task this connection never saw: list unknown
            if tool_input.get("status") == "deleted":
                tasks.pop(task_id)
            else:
                if isinstance(tool_input.get("status"), str):
                    tasks[task_id]["status"] = tool_input["status"]
                if isinstance(tool_input.get("subject"), str):
                    tasks[task_id]["text"] = tool_input["subject"]
        if not self._tasks_whole:
            return []
        self._stale_sent = False
        entries = _plan_entries(list(tasks.values()), "text")
        return self._plan_once(entries=entries or [])

    def _result(self, frame: dict, *, proven: bool = False) -> list[NativeEvent]:
        """The operator turn's own result. Normally its `completed` follows and proves the fence
        (`_closing`); ``proven``: that `completed` already came, cleanly (#1644)."""
        common = {"operation_id": self.operation_id, "native_turn_id": self.native_turn_id}
        state = _result_state(frame)
        remedy = self._remedy(state)
        clean = self._cycle == "started" and not self._tainted
        self._closing = self.operation_id if clean else None
        if proven:
            self.lifecycle_proven = True
        self._cycle = None
        self.operation_id = None
        self.native_turn_id = None
        self._clear_turn_approvals()
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
                **remedy,
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
            if not own and frame.get("error") == "authentication_failed":
                # The CLI's own marker on the synthetic message it writes when its vendor login is
                # missing or expired (captured: `"error": "authentication_failed"`, model
                # `<synthetic>`). Keyed on the field, never on the message's words.
                self._signin = True
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
                    item_id, name = _identity(block.get("id")), _identity(block.get("name"))
                    tool_input = block.get("input")
                    # `call` (#1726): this event STARTS the call, so a derived count reads each
                    # call once however many observations it gets.
                    extra: dict[str, Any] = {"call": True}
                    server = self._mcp_server(name)
                    if server is not None:
                        extra["mcp_server"] = server
                    if (
                        name == "Skill"
                        and isinstance(tool_input, dict)
                        and isinstance(tool_input.get("skill"), str)
                        and tool_input["skill"].strip()
                    ):
                        extra["skill"] = _cut(tool_input["skill"].strip(), ENV_NAME)
                    output.append(
                        _event(
                            "tool",
                            **common,
                            item_id=item_id,
                            tool=name,
                            state="running",
                            summary=_summary(tool_input),
                            **extra,
                        )
                    )
                    output.extend(self._plan_call(item_id, name, tool_input))
            return output
        if kind == "user" and isinstance(frame.get("message"), dict):
            content = frame["message"].get("content")
            if isinstance(content, list):
                results = [
                    block
                    for block in content[:128]
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
                out = [
                    _event(
                        "tool",
                        **common,
                        item_id=_identity(block.get("tool_use_id")),
                        state="failed" if block.get("is_error") else "completed",
                        output=_summary(block.get("content")),
                    )
                    for block in results
                ]
                for block in results:
                    # The structured result belongs to the frame's ONE tool result, if it worked.
                    ok = not block.get("is_error")
                    structured = frame.get("tool_use_result") if len(results) == 1 and ok else None
                    out.extend(self._plan_result(block.get("tool_use_id"), structured, ok))
                return out
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


def _acp_frame_lost(codec, head: bytes, tail: bytes) -> list[NativeEvent]:
    """A frame too large to read on an ACP connection (#1725), told by its two ends. A request is
    refused, never answered blind; a turn's own reply lost ends that turn uncertain; a request
    that can't be answered ends the connection."""
    method, request_id, readable, replies = _lost_rpc(head, tail)
    row = codec._row(FRAME_SKIPPED)
    if not readable:
        return codec._unreadable(row)
    if method is not None and request_id is not None:
        asked = method == "session/request_permission"
        if asked and codec.operation_id is not None:
            codec.lost_turn = codec.operation_id
        answer = (
            {"result": {"outcome": {"outcome": "cancelled"}}}
            if asked
            else {"error": {"code": -32601, "message": "Unsupported client method"}}
        )
        return [
            *row,
            _event("send", frame={"jsonrpc": "2.0", "id": request_id, **answer}),
        ]
    if method is not None:
        # The one thing ACP sends without an id. Anything else may be a request whose id couldn't
        # be read.
        return row if method == "session/update" else codec._unreadable(row)
    if request_id is not None:
        pending = codec._requests.get(request_id) if isinstance(request_id, str) else None
        if pending is not None:
            codec._requests.pop(request_id, None)
            if pending[0] == "session/prompt" and pending[1] == codec.operation_id:
                return [*row, *codec._ended_uncertain(LOST_RESULT)]
            return row
        # Not a reply to anything asked, and it says no more: it may be a request.
        return row if replies else codec._unreadable(row)
    return codec._unreadable(row)


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


def _acp_inert(codec: _Codec, env: _Environment, update: dict) -> list[NativeEvent] | None:
    """The two ACP updates read OUTSIDE an operator turn too (#1726): the agent's commands and
    its plan. Both are inert display state of the conversation, never a turn's output, so the
    no-turn filter (a replay or idle chatter is never attributed) is relaxed for exactly these.
    A `plan` replayed while `session/load` is pending is history BattleLab already journaled.
    None: not one of them."""
    kind = update.get("sessionUpdate")
    if kind == "available_commands_update":
        commands = update.get("availableCommands")
        if not isinstance(commands, list):
            return []
        return env.update(commands=[{"name": n} for n in _names(commands, ENV_COMMANDS)])
    if kind == "plan":
        if any(action == "session/load" for action, _ in codec._requests.values()):
            return []
        entries = _plan_entries(update.get("entries"), "content")
        return [] if entries is None else codec._plan_once(entries=entries)
    return None


def _acp_server(title: Any, names: dict[str, str]) -> str | None:
    """The configured MCP server an opencode tool call's name (`<server>_<tool>`) starts with,
    the longest match by its FULL name, reported under its shown (cut) name; None when none
    does (then it is not known to be an MCP call at all)."""
    if not isinstance(title, str):
        return None
    best: tuple[str, str] | None = None
    for full, shown in names.items():
        normalized = re.sub(r"[^A-Za-z0-9_-]", "_", full)
        if title.startswith(f"{normalized}_") and (best is None or len(full) > len(best[0])):
            best = (full, shown)
    return None if best is None else best[1]


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
        # The tools (#1726). opencode reports its commands (skills among them) over ACP; its MCP
        # servers are only NAMES from the spawn-time configuration probe and the servers this
        # generation was given, so their status is `configured` — never measured. A `skill`
        # call is a reported use; the plan is its `todowrite` input (1.18.35 sends no `plan`).
        self._env = _Environment(skill_use=True)
        self._configured: list[tuple[str, str]] | None = None  # None: no probe, nothing given
        self._server_names: dict[str, str] = {}  # full name (a call's prefix) -> shown name
        # Per call: the tool's NAME (the title it started with; later updates retitle it) and a
        # `todowrite` checklist, journaled when that call completes (review on #1811).
        self._call_names: dict[str, str] = {}
        self._todos: dict[str, list[dict[str, str]]] = {}

    def configured_mcp(self, servers: list[tuple[str, str]]) -> None:
        """The probe's MCP servers, ``(name, "configured" | "disabled")``, reported with the
        session once it is bound."""
        self._configured = []
        for name, state in servers:
            if state in {"configured", "disabled"}:
                self._configure(name, state)

    def _configure(self, name: Any, state: str) -> None:
        """One configured server, its name stripped and cut ONCE (review on #1811): the listed
        name, the name a call is counted under and the journal's bound always agree."""
        if not isinstance(name, str) or not name.strip():
            return
        full = name.strip()
        name = _cut(full, ENV_NAME)
        configured = self._configured if self._configured is not None else []
        if name not in {n for n, _ in configured} and len(configured) < ENV_MCP:
            configured.append((name, state))
            self._server_names[full] = name
        self._configured = configured

    def _add_given(self, mcp_servers: list[dict] | None) -> None:
        for server in mcp_servers or ():
            self._configure(server.get("name") if isinstance(server, dict) else None, "configured")

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
        self._add_given(mcp_servers)
        return self._request(
            "session/new", {"cwd": _path(cwd), "mcpServers": list(mcp_servers or [])}
        )

    def load(self, session_id: str, cwd: str, mcp_servers: list[dict] | None = None) -> dict:
        session_id = opencode_session_id(session_id)
        if self.native_id not in (None, session_id):
            raise ProtocolError("native session identity changed")
        self._add_given(mcp_servers)
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

    def _after_turn(self) -> None:
        self._tool_calls.clear()

    def frame_lost(self, head: bytes = b"", tail: bytes = b"") -> list[NativeEvent]:
        return _acp_frame_lost(self, head, tail)

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
                ),
                *self._env.update(
                    mcp=None if self._configured is None else _mcp_rows(self._configured)
                ),
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
        if self._no_room(frame["id"]):
            # More open at once than can be shown (#1725): refused, and the connection lives.
            return [
                *self._row(REQUEST_REFUSED),
                _event(
                    "send",
                    frame={
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    },
                ),
            ]
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
        inert = _acp_inert(self, self._env, update)
        if inert is not None:
            return inert
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
            extra, plan = self._call_facts(kind, update, previous)
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
            return [*cancellations, _event("tool", **common, **data, **extra), *plan]
        return []

    def _call_facts(
        self, kind: str, update: dict, previous: dict
    ) -> tuple[dict[str, Any], list[NativeEvent]]:
        """A call's start (with the tool's name, so counts read `todowrite`, not ACP's `other`),
        the configured MCP server it names, a skill it loads, and the checklist a successful
        `todowrite` carries (#1726, review on #1811)."""
        item_id = _safe_id(update.get("toolCallId"), "tool") or ""
        title = update.get("title")
        if kind == "tool_call" and isinstance(title, str) and title.strip():
            if item_id not in self._call_names and len(self._call_names) >= MAX_PENDING:
                self._call_names.pop(next(iter(self._call_names)))
            self._call_names[item_id] = _cut(title.strip(), ENV_NAME)
        name = self._call_names.get(item_id)
        raw = update.get("rawInput")
        extra: dict[str, Any] = {}
        if kind == "tool_call":
            extra["call"] = True
            if name is not None:
                extra["tool_name"] = name
            # Matched on the uncut title: a long server name's prefix outlives the name's cut.
            server = _acp_server(
                title.strip() if isinstance(title, str) else None, self._server_names
            )
            if server is not None:
                extra["mcp_server"] = server
        plan: list[NativeEvent] = []
        if isinstance(raw, dict):
            if name == "skill" and isinstance(raw.get("name"), str) and raw["name"].strip():
                before = previous.get("rawInput") if previous else None
                if not (isinstance(before, dict) and before.get("name") == raw["name"]):
                    extra["skill"] = _cut(raw["name"].strip(), ENV_NAME)
            if name == "todowrite":
                entries = _plan_entries(raw.get("todos"), "content")
                if entries is not None:
                    if item_id not in self._todos and len(self._todos) >= MAX_PENDING:
                        self._todos.pop(next(iter(self._todos)))
                    self._todos[item_id] = entries
        status = update.get("status")
        if status in {"completed", "failed"}:
            entries = self._todos.pop(item_id, None)
            self._call_names.pop(item_id, None)
            if status == "completed" and entries is not None:
                plan = self._plan_once(entries=entries)
        return extra, plan

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
        # The tools and the plan (#1726): copilot's commands and its ACP `plan` updates. It names
        # no MCP server status and reports no skill use over ACP.
        self._env = _Environment(skill_use=False)

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

    def _after_turn(self) -> None:
        self._tool_calls.clear()

    def frame_lost(self, head: bytes = b"", tail: bytes = b"") -> list[NativeEvent]:
        return _acp_frame_lost(self, head, tail)

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
        if self._no_room(frame["id"]):
            # More open at once than can be shown (#1725): refused, and the connection lives.
            return [
                *self._row(REQUEST_REFUSED),
                _event(
                    "send",
                    frame={
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"outcome": {"outcome": "cancelled"}},
                    },
                ),
            ]
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
        inert = _acp_inert(self, self._env, update)
        if inert is not None:
            return inert
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
            if kind == "tool_call":
                data["call"] = True  # the call starts here (#1726)
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
