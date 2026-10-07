"""Private, transport-free worker wire contract (#1278), protocol 1.

The verified socket/peer and capability handshake precede requests; decoding is not admission.
Worker UUIDs identify never-reused contained services. Connection UUIDs identify the NATIVE
protocol connection, and survive browser/IPC reconnects. Request UUIDs correlate one exchange;
operation UUIDs identify immutable journal claims across exchanges and worker reconnects.

An effect request must be durably claimed before any native write. A receipt describes that
claim and stdin handoff, never agent acceptance or completion. Unknown handoff stays occupied.
Callbacks, containment evidence and original authority must be checked by the worker at effect.
No execution guard can be serialized by this contract. Correlation is never permission.

Handshake bytes are private: pass PrivateFrame.data only to the verified socket, never a log,
repr, event or conversation journal. Responses allow normalized observations, never raw native
frames. These helpers open no socket and grant no executable/runtime capability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass

from .native_protocol import ProtocolError, validate_text
from .structured_types import normalize_context

PROTOCOL = 1
ADAPTER_VERSION = 1
MAX_FRAME_BYTES = 1_048_576
MAX_TEXT = 200_000
MAX_EVENT_TEXT = 20_000
MAX_EVENTS = 100
MAX_INTEGER = 2**53 - 1
ADAPTERS = frozenset({"codex-app-server", "claude-stream-json"})
ACTIONS = frozenset({"submit", "decide", "interrupt", "snapshot", "events", "stop", "probe"})
EFFECT_ACTIONS = frozenset({"submit", "decide", "interrupt", "stop"})
_ENGINE = re.compile(r"[a-z][a-z0-9-]{1,23}\Z", re.ASCII)
_NATIVE = re.compile(r"[A-Za-z0-9_.:-]{1,258}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HEADER = frozenset(
    {"protocol", "type", "session_key", "worker_id", "connection_id", "adapter", "adapter_version"}
)


class IPCError(ValueError):
    """Safe refusal: messages never include untrusted frame or capability values."""


def _object(value, required, optional=()):
    if (
        type(value) is not dict
        or not set(required) <= value.keys()
        or value.keys() - (set(required) | set(optional))
    ):
        raise IPCError("invalid private IPC fields")
    return value


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise IPCError("invalid private IPC UUID") from None
    return value


def _integer(value, *, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise IPCError("invalid private IPC integer")
    return value


def _text(value, limit=MAX_EVENT_TEXT):
    if type(value) is not str or len(value) > limit:
        raise IPCError("invalid private IPC text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise IPCError("invalid private IPC text") from None
    return value


def _native(value, limit=256):
    if type(value) is not str or len(value) > limit or not _NATIVE.fullmatch(value):
        raise IPCError("invalid private IPC native identity")
    return value


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise IPCError("invalid private IPC digest")
    return value


def _choice(value, choices):
    if type(value) is not str or value not in choices:
        raise IPCError("unsupported private IPC value")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise IPCError("invalid private IPC boolean")
    return value


def _session_key(value):
    if type(value) is not str:
        raise IPCError("invalid private IPC session key")
    engine, sep, native = value.partition(":")
    if not sep or not _ENGINE.fullmatch(engine):
        raise IPCError("invalid private IPC session key")
    _uuid(native)
    return value  # Shape only; the admitted registry/ownership record decides membership.


def validate_session_key(value) -> str:
    """Validate qualified API UUID shape without consulting or granting roster membership."""
    return _session_key(value)


@dataclass(frozen=True)
class Binding:
    session_key: str
    worker_id: str
    connection_id: str
    adapter: str
    adapter_version: int = ADAPTER_VERSION

    def __post_init__(self):
        _session_key(self.session_key)
        _uuid(self.worker_id)
        _uuid(self.connection_id)
        _choice(self.adapter, ADAPTERS)
        _integer(self.adapter_version, minimum=ADAPTER_VERSION, maximum=ADAPTER_VERSION)

    def envelope(self, kind):
        return {
            "protocol": PROTOCOL,
            "type": kind,
            "session_key": self.session_key,
            "worker_id": self.worker_id,
            "connection_id": self.connection_id,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
        }


@dataclass(frozen=True, repr=False, eq=False)
class Capability:
    _value: str

    def __post_init__(self):
        _digest(self._value)

    @classmethod
    def create(cls):
        return cls(secrets.token_hex(32))

    def matches(self, value):
        return type(value) is str and hmac.compare_digest(self._value, _digest(value))

    def __repr__(self):
        return "<private worker capability>"


@dataclass(frozen=True, repr=False)
class PrivateFrame:
    data: bytes

    def __repr__(self):
        return "<private worker handshake>"


def _binding(value, kind, expected=None):
    _integer(value.get("protocol"), minimum=PROTOCOL, maximum=PROTOCOL)
    _choice(value.get("type"), {kind})
    actual = Binding(
        *(
            value.get(k)
            for k in ("session_key", "worker_id", "connection_id", "adapter", "adapter_version")
        )
    )
    if expected is not None and actual != expected:
        raise IPCError("private IPC generation or adapter mismatch")
    return actual


def _encode(value):
    try:
        line = (
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise IPCError("invalid private IPC frame") from None
    if len(line) > MAX_FRAME_BYTES:
        raise IPCError("private IPC frame exceeds its bound")
    return line


def _decode(line):
    if type(line) is not bytes or len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise IPCError("incomplete or oversized private IPC frame")
    if b"\n" in line[:-1] or b"\r" in line:
        raise IPCError("private IPC requires exactly one JSONL record")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IPCError("duplicate private IPC field")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise IPCError("invalid private IPC number")

    try:
        value = json.loads(
            line.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=invalid_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise IPCError("invalid private IPC frame") from None
    if type(value) is not dict:
        raise IPCError("private IPC requires an object")
    return value


def encode_handshake(binding: Binding, capability: Capability) -> PrivateFrame:
    if not isinstance(capability, Capability):
        raise IPCError("private worker capability required")
    return PrivateFrame(_encode({**binding.envelope("hello"), "capability": capability._value}))


def decode_handshake(line: bytes, *, expected: Binding, capability: Capability) -> Binding:
    value = _decode(line)
    _object(value, _HEADER | {"capability"})
    actual = _binding(value, "hello", expected)
    if not isinstance(capability, Capability) or not capability.matches(value["capability"]):
        raise IPCError("private worker authentication refused")
    return actual  # Never return or retain the presented capability in an observation.


def encode_welcome(binding: Binding) -> bytes:
    """The worker's reply to an authenticated hello: its generation, never the capability."""
    return _encode(binding.envelope("welcome"))


def decode_welcome(line: bytes, *, expected: Binding) -> Binding:
    value = _decode(line)
    _object(value, _HEADER)
    return _binding(value, "welcome", expected)


def _params(action, value, *, immutable=False):
    keys = {
        "submit": {"operation_id", "expected_revision", "text", "context"},
        "decide": {
            "operation_id",
            "expected_revision",
            "turn_id",
            "request_id",
            "item_id",
            "payload_digest",
            "decision",
            "approval_worker_id",
            "approval_connection_id",
            "actor",
        },
        "interrupt": {"operation_id", "expected_revision", "turn_id"},
        "stop": {"operation_id", "expected_revision", "target_worker_id"},
        "probe": {"target_worker_id"},
        "events": {"after", "limit"},
        "snapshot": set(),
    }
    # Stored records written before #1278's actor binding have no `actor`; they must stay
    # readable (an unreadable journal strands the session). New requests always require it.
    required = (
        keys[action] - {"operation_id", "expected_revision", "actor"} if immutable else keys[action]
    )
    _object(value, required, keys[action] & {"actor"} if immutable else ())
    out = {}
    for key, item in value.items():
        if key.endswith("worker_id") or key in {
            "operation_id",
            "turn_id",
            "approval_connection_id",
        }:
            out[key] = _uuid(item)
        elif key in {"expected_revision", "after"}:
            out[key] = _integer(item)
        elif key == "limit":
            out[key] = _integer(item, minimum=1, maximum=MAX_EVENTS)
        elif key == "text":
            try:
                out[key] = validate_text(_text(item, MAX_TEXT))
            except ProtocolError:
                raise IPCError("invalid private IPC submit text") from None
        elif key == "context":
            try:
                out[key] = normalize_context(item)
            except ValueError:
                raise IPCError("invalid private IPC correlation") from None
        elif key == "payload_digest":
            out[key] = _digest(item)
        elif key == "decision":
            out[key] = _choice(item, {"approve", "reject", "cancel"})
        elif key == "actor":
            # The authenticated operator, bound into the decision's durable identity (#1278).
            out[key] = _text(item, 128)
            if not out[key]:
                raise IPCError("a decision needs its operator")
        else:
            out[key] = _native(item, 258 if key == "request_id" else 256)
    return out


def validate_request(value, *, expected: Binding | None = None):
    _object(value, _HEADER | {"request_id", "action", "params"})
    binding = _binding(value, "request", expected)
    action = _choice(value["action"], ACTIONS)
    out = {
        **binding.envelope("request"),
        "request_id": _uuid(value["request_id"]),
        "action": action,
        "params": _params(action, value["params"]),
    }
    _encode(out)
    return out


def encode_request(binding: Binding, request_id: str, action: str, params: dict) -> bytes:
    return _encode(
        validate_request(
            {
                **binding.envelope("request"),
                "request_id": request_id,
                "action": action,
                "params": params,
            }
        )
    )


def decode_request(line: bytes, *, expected: Binding) -> dict:
    return validate_request(_decode(line), expected=expected)


def immutable_request(request: dict) -> dict:
    """Journal binding; replay precedes revision checks and survives web/native reconnects.

    Transport request UUID and current connection do not change a submission's identity. Exact
    decisions preserve the ORIGINAL approval's worker/connection, request/item and payload hash.
    Stop preserves its exact contained-service target. This value contains no capability.
    """
    value = validate_request(request)
    if value["action"] not in EFFECT_ACTIONS:
        raise IPCError("read-only requests have no durable effect identity")
    return {
        "action": value["action"],
        "params": {
            key: item
            for key, item in value["params"].items()
            if key not in {"operation_id", "expected_revision"}
        },
    }


def normalize_immutable_request(value: dict) -> dict:
    """Validate stored operation identity without inventing transport or execution authority."""
    _object(value, {"action", "params"})
    action = _choice(value["action"], EFFECT_ACTIONS)
    out = {"action": action, "params": _params(action, value["params"], immutable=True)}
    _encode(out)
    return out


def request_digest(request: dict) -> str:
    body = json.dumps(
        immutable_request(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(body.encode()).hexdigest()


_COMMON = {"operation_id", "native_turn_id"}
_RPC = {"request_id", "action"}
_EVENT_FIELDS = {
    "initialized": ({"request_id"}, {"action"}),
    "session": (
        {"native_id", "model_configured", "model_effective"},
        _RPC | {"turns", "omitted_turns"},
    ),
    "turn_started": (_COMMON, _RPC),
    "text": (_COMMON | {"text", "partial", "truncated"}, {"item_id"}),
    "tool": (_COMMON | {"item_id"}, {"tool", "state", "summary", "output", "completed"}),
    "approval": (
        _COMMON
        | {
            "request_id",
            "item_id",
            "tool",
            "summary",
            "choices",
            "payload_digest",
            "worker_id",
            "connection_id",
        },
        # Whether `summary` presents EVERYTHING the request authorizes; approve needs it.
        {"complete"},
    ),
    "approval_cancelled": ({"request_id"}, set()),
    "model": (_COMMON | {"model_effective"}, set()),
    "turn_completed": (
        _COMMON | {"state", "error"},
        _RPC | {"text", "background_active", "native_state"},
    ),
    "background": ({"state"}, {"native_turn_id", "task_id", "active", "native_state"}),
    "response": (_RPC, set()),
    "error": ({"message"}, _COMMON | _RPC | {"native_will_retry"}),
    "disconnected": ({"state", "native_id", "native_turn_id", "operation_id"}, set()),
}


def _event(value):
    _object(value, {"cursor", "kind", "data"})
    kind = _choice(value["kind"], _EVENT_FIELDS)
    required, optional = _EVENT_FIELDS[kind]
    data = _object(value["data"], required, optional)
    out = {}
    for key, item in data.items():
        if item is None and key in {
            "operation_id",
            "native_id",
            "native_turn_id",
            "model_configured",
            "model_effective",
            "native_state",
        }:
            out[key] = None
        elif key in {"operation_id", "worker_id", "connection_id"}:
            out[key] = _uuid(item)
        elif key in {"native_id", "native_turn_id", "item_id", "request_id", "task_id", "tool"}:
            out[key] = _native(item, 258 if key == "request_id" else 256)
        elif key in {
            "partial",
            "truncated",
            "completed",
            "background_active",
            "active",
            "native_will_retry",
            "complete",
        }:
            out[key] = _boolean(item)
        elif key == "payload_digest":
            out[key] = _digest(item)
        elif key == "omitted_turns":
            out[key] = _integer(item)
        elif key == "choices":
            if type(item) is not list or not 1 <= len(item) <= 3:
                raise IPCError("invalid private IPC approval choices")
            out[key] = [_choice(choice, {"approve", "reject", "cancel"}) for choice in item]
            if len(set(out[key])) != len(out[key]):
                raise IPCError("duplicate private IPC approval choices")
        elif key == "turns":
            if type(item) is not list or len(item) > 50:
                raise IPCError("private IPC observed turns exceed their bound")
            out[key] = []
            for turn in item:
                _object(turn, {"native_turn_id", "state"})
                out[key].append(
                    {
                        "native_turn_id": _native(turn["native_turn_id"]),
                        "state": _text(turn["state"], 64),
                    }
                )
        else:
            out[key] = _text(
                item,
                256
                if key.startswith("model_")
                else 128
                if key == "action"
                else 64
                if key in {"state", "native_state"}
                else MAX_EVENT_TEXT,
            )
    if kind not in {"error", "disconnected"} and "operation_id" in out:
        _uuid(out["operation_id"])
        _native(out["native_turn_id"])
    if kind == "turn_completed":
        _choice(out["state"], {"completed", "failed", "interrupted"})
    if kind == "disconnected":
        _choice(out["state"], {"uncertain"})
    if kind == "model" and out["model_effective"] is None:
        raise IPCError("effective model observation requires a model")
    return {"cursor": _integer(value["cursor"], minimum=1), "kind": kind, "data": out}


def normalize_event(value: dict) -> dict:
    """Validate a normalized journal observation, excluding private native response frames.

    The worker stamps approval events with worker_id/connection_id before this boundary. The
    journal supplies a durable cursor separately; native clocks and native IDs are not cursors.
    """
    _object(value, {"kind", "data"})
    event = _event({"cursor": 1, **value})
    return {"kind": event["kind"], "data": event["data"]}


def _result(action, value):
    if action in EFFECT_ACTIONS:
        _object(
            value,
            {"operation_id", "recorded_revision", "handoff"},
            {"containment"} if action == "stop" else (),
        )
        out = {
            "operation_id": _uuid(value["operation_id"]),
            "recorded_revision": _integer(value["recorded_revision"], minimum=1),
            "handoff": _choice(value["handoff"], {"not_sent", "sent", "uncertain"}),
        }
        if "containment" in value:
            out["containment"] = _choice(value["containment"], {"live", "gone", "unknown"})
        return out
    if action == "probe":
        _object(value, {"containment"})
        return {"containment": _choice(value["containment"], {"live", "gone", "unknown"})}
    common = {"revision", "next_cursor", "events"}
    extra = (
        {"state", "native_id", "model_requested", "model_effective"}
        if action == "snapshot"
        else set()
    )
    _object(value, common | extra)
    out = {key: _integer(value[key]) for key in ("revision", "next_cursor")}
    if out["next_cursor"] > out["revision"]:
        raise IPCError("invalid private IPC cursor range")
    if type(value["events"]) is not list or len(value["events"]) > MAX_EVENTS:
        raise IPCError("private IPC events exceed their bound")
    out["events"] = [_event(event) for event in value["events"]]
    cursor = 0
    for event in out["events"]:
        if not cursor < event["cursor"] <= out["next_cursor"]:
            raise IPCError("private IPC event cursors must increase")
        cursor = event["cursor"]
    if extra:
        out["state"] = _choice(
            value["state"],
            {
                "idle",
                "running",
                "awaiting_approval",
                "completed",
                "failed",
                "interrupted",
                "uncertain",
                "unavailable",
            },
        )
        out["native_id"] = None if value["native_id"] is None else _native(value["native_id"])
        for key in ("model_requested", "model_effective"):
            out[key] = None if value[key] is None else _text(value[key], 256)
    return out


def validate_response(value, *, expected: Binding | None = None):
    _object(value, _HEADER | {"request_id", "action"}, {"result", "error"})
    binding = _binding(value, "response", expected)
    action = _choice(value["action"], ACTIONS)
    out = {
        **binding.envelope("response"),
        "request_id": _uuid(value["request_id"]),
        "action": action,
    }
    if ("result" in value) == ("error" in value):
        raise IPCError("private IPC response requires exactly one outcome")
    if "error" in value:
        error = _object(value["error"], {"code", "message"})
        out["error"] = {
            "code": _choice(
                error["code"],
                {
                    "invalid",
                    "unsupported",
                    "conflict",
                    "stale",
                    "busy",
                    "unavailable",
                    "unauthorized",
                },
            ),
            "message": _text(error["message"], 2000),
        }
    else:
        out["result"] = _result(action, value["result"])
    return out


def encode_response(value: dict) -> bytes:
    return _encode(validate_response(value))


def decode_response(line: bytes, *, request: dict) -> dict:
    requested = validate_request(request)
    value = validate_response(_decode(line), expected=_binding(requested, "request"))
    if (value["request_id"], value["action"]) != (requested["request_id"], requested["action"]):
        raise IPCError("private IPC response does not match its request")
    if "result" in value:
        result = value["result"]
        if (
            value["action"] in EFFECT_ACTIONS
            and result["operation_id"] != requested["params"]["operation_id"]
        ):
            raise IPCError("private IPC receipt does not match its operation")
        if value["action"] == "events" and (
            result["next_cursor"] < requested["params"]["after"]
            or any(event["cursor"] <= requested["params"]["after"] for event in result["events"])
            or len(result["events"]) > requested["params"]["limit"]
        ):
            raise IPCError("private IPC events do not match their requested range")
    return value
