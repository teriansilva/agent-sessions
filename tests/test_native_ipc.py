"""Synthetic private-worker wire tests: no socket, native CLI, store or permission grant."""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest

from agent_sessions import native_ipc as ipc


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def binding():
    return ipc.Binding(f"fixture-api:{uid()}", uid(), uid(), "codex-app-server")


def params(action):
    effect = {"operation_id": uid(), "expected_revision": 4}
    return {
        "submit": {
            **effect,
            "text": "Please inspect the fixture.",
            "context": {"mission_id": "m:1", "episode": 0},
        },
        "decide": {
            **effect,
            "turn_id": uid(),
            "request_id": "i:17",
            "item_id": "item:4",
            "payload_digest": "a" * 64,
            "decision": "approve",
            "approval_worker_id": uid(),
            "approval_connection_id": uid(),
            "actor": "operator",
        },
        "interrupt": {**effect, "turn_id": uid()},
        "send_now": {**effect, "turn_id": uid(), "queued_turn_id": uid(), "mode": "steer"},
        "stop": {**effect, "target_worker_id": uid()},
        "probe": {"target_worker_id": uid()},
        "snapshot": {},
        "events": {"after": 4, "limit": 20},
    }[action]


def request(binding, action="submit", body=None):
    return {
        **binding.envelope("request"),
        "request_id": uid(),
        "action": action,
        "params": params(action) if body is None else body,
    }


def wire(value):
    return (json.dumps(value) + "\n").encode()


def response(requested, result):
    return {
        **{k: v for k, v in requested.items() if k != "params"},
        "type": "response",
        "result": result,
    }


def event(cursor=5):
    return {
        "cursor": cursor,
        "kind": "text",
        "data": {
            "operation_id": uid(),
            "native_turn_id": "turn:1",
            "item_id": "message:1",
            "text": "Structured output",
            "partial": True,
            "truncated": False,
        },
    }


def page(events=None, **changes):
    return {
        "revision": 6,
        "next_cursor": 6,
        "events": [event()] if events is None else events,
        **changes,
    }


def test_private_handshake_is_bound_and_never_appears_in_representations(binding):
    capability = ipc.Capability("ab" * 32)
    encoded = ipc.encode_handshake(binding, capability)
    assert ipc.decode_handshake(encoded.data, expected=binding, capability=capability) == binding
    assert capability._value not in repr(capability)
    assert capability._value not in str(capability)
    assert capability._value not in repr(encoded)
    assert capability._value not in str(encoded)
    with pytest.raises(ipc.IPCError) as error:
        ipc.decode_handshake(encoded.data, expected=binding, capability=ipc.Capability("cd" * 32))
    assert capability._value not in str(error.value)
    assert capability._value not in repr(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "field", ["worker_id", "connection_id", "session_key", "adapter", "adapter_version", "protocol"]
)
def test_handshake_rejects_wrong_generation_or_version(binding, field):
    capability = ipc.Capability.create()
    doc = json.loads(ipc.encode_handshake(binding, capability).data)
    doc[field] = {
        "worker_id": uid(),
        "connection_id": uid(),
        "session_key": f"fixture-api:{uid()}",
        "adapter": "claude-stream-json",
        "adapter_version": 2,
        "protocol": True,
    }[field]
    with pytest.raises(ipc.IPCError):
        ipc.decode_handshake(wire(doc), expected=binding, capability=capability)


@pytest.mark.parametrize("action", sorted(ipc.ACTIONS))
def test_closed_request_roundtrip_detaches_input(binding, action):
    original = request(binding, action)
    encoded = ipc.encode_request(binding, original["request_id"], action, original["params"])
    decoded = ipc.decode_request(encoded, expected=binding)
    assert decoded == original
    original["params"].clear()
    assert decoded["params"] == json.loads(encoded)["params"]


@pytest.mark.parametrize(
    "field", ["execution_guard", "execution_binding", "authority", "capability", "native_frame"]
)
@pytest.mark.parametrize("location", ["envelope", "params", "context"])
def test_browser_shaped_authority_or_private_frame_claims_refuse(binding, field, location):
    value = request(binding)
    target = value if location == "envelope" else value["params"]
    if location == "context":
        target = target["context"]
    target[field] = {"allow": True}
    with pytest.raises(ipc.IPCError):
        ipc.decode_request(wire(value), expected=binding)


@pytest.mark.parametrize("value", [True, False, -1, 1.0, ipc.MAX_INTEGER + 1, "4"])
@pytest.mark.parametrize("action,field", [("submit", "expected_revision"), ("events", "after")])
def test_revisions_and_cursors_are_safe_nonnegative_integers(binding, value, action, field):
    doc = request(binding, action)
    doc["params"][field] = value
    with pytest.raises(ipc.IPCError):
        ipc.validate_request(doc)


@pytest.mark.parametrize("bad", [0, True, 101, -1, 2.0])
def test_event_count_is_strictly_bounded(binding, bad):
    doc = request(binding, "events")
    doc["params"]["limit"] = bad
    with pytest.raises(ipc.IPCError):
        ipc.validate_request(doc)


@pytest.mark.parametrize(
    "bad", ["short", "00000000-0000-4000-8000-00000000000A", "../escape", True]
)
def test_operation_uuid_is_canonical_and_never_coerced(binding, bad):
    doc = request(binding)
    doc["params"]["operation_id"] = bad
    with pytest.raises(ipc.IPCError):
        ipc.validate_request(doc)


@pytest.mark.parametrize("bad", ["", "  \n", "x" * (ipc.MAX_TEXT + 1), "\ud800", 1])
def test_submit_text_is_valid_bounded_utf8(binding, bad):
    doc = request(binding)
    doc["params"]["text"] = bad
    with pytest.raises(ipc.IPCError):
        ipc.validate_request(doc)


def test_maximum_unicode_input_is_utf8_not_ascii_expansion(binding):
    doc = request(binding)
    doc["params"]["text"] = "😀" * ipc.MAX_TEXT
    encoded = ipc.encode_request(binding, doc["request_id"], "submit", doc["params"])
    assert len(encoded) < ipc.MAX_FRAME_BYTES
    assert ipc.decode_request(encoded, expected=binding) == doc


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "two-records",
        "crlf",
        "invalid-utf8",
        "nan",
        "duplicate",
        "duplicate-nested",
        "oversized",
        "array",
    ],
)
def test_invalid_jsonl_never_becomes_an_operation(binding, mutation):
    valid = wire(request(binding))
    broken = {
        "partial": valid[:-1],
        "two-records": valid + valid,
        "crlf": valid[:-1] + b"\r\n",
        "invalid-utf8": b'{"text":"\xff"}\n',
        "nan": b'{"number":NaN}\n',
        "duplicate": b'{"type":"hello","type":"request"}\n',
        "duplicate-nested": b'{"params":{"operation_id":"one","operation_id":"two"}}\n',
        "oversized": b" " * ipc.MAX_FRAME_BYTES + b"\n",
        "array": b"[]\n",
    }[mutation]
    with pytest.raises(ipc.IPCError):
        ipc.decode_request(broken, expected=binding)


def test_submit_identity_survives_reconnect_but_changes_with_original_input(binding):
    first = request(binding)
    immutable = ipc.immutable_request(first)
    reconnected = dataclasses.replace(binding, worker_id=uid(), connection_id=uid())
    repeat = {
        **first,
        **reconnected.envelope("request"),
        "request_id": uid(),
        "params": {**first["params"], "expected_revision": 0},
    }
    assert ipc.immutable_request(repeat) == immutable
    assert ipc.request_digest(repeat) == ipc.request_digest(first)
    assert ipc.normalize_immutable_request(immutable) == immutable
    for key, value in (("text", "Changed"), ("context", {"episode": 2})):
        changed = {**repeat, "params": {**repeat["params"], key: value}}
        assert ipc.request_digest(changed) != ipc.request_digest(first)
    assert set(immutable) == {"action", "params"}
    assert not {"operation_id", "expected_revision"} & immutable["params"].keys()


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "s:17"),
        ("item_id", "another-item"),
        ("payload_digest", "b" * 64),
        ("decision", "reject"),
        ("approval_worker_id", None),
        ("approval_connection_id", None),
    ],
)
def test_decisions_bind_original_callback_identity(binding, field, value):
    original = request(binding, "decide")
    changed = {
        **original,
        "params": {**original["params"], field: uid() if value is None else value},
    }
    assert ipc.request_digest(changed) != ipc.request_digest(original)
    immutable = ipc.immutable_request(original)
    assert (
        immutable["params"]["approval_connection_id"]
        == original["params"]["approval_connection_id"]
    )


def test_stop_identity_preserves_exact_service_generation(binding):
    original = request(binding, "stop")
    changed = {**original, "params": {**original["params"], "target_worker_id": uid()}}
    assert ipc.request_digest(changed) != ipc.request_digest(original)


@pytest.mark.parametrize("action", sorted(ipc.EFFECT_ACTIONS))
def test_stored_request_validation_rejects_transport_or_authority(binding, action):
    doc = ipc.immutable_request(request(binding, action))
    assert ipc.normalize_immutable_request(doc) == doc
    for key in (
        "operation_id",
        "expected_revision",
        "execution_binding",
        "capability",
        "raw_frame",
    ):
        with pytest.raises(ipc.IPCError):
            ipc.normalize_immutable_request({**doc, "params": {**doc["params"], key: uid()}})


def test_receipt_reports_durable_claim_and_handoff_without_claiming_completion(binding):
    original = request(binding)
    result = {
        "operation_id": original["params"]["operation_id"],
        "recorded_revision": 5,
        "handoff": "sent",
    }
    encoded = ipc.encode_response(response(original, result))
    assert ipc.decode_response(encoded, request=original)["result"] == result
    for field, value in (
        ("handoff", "completed"),
        ("recorded_revision", 0),
        ("accepted", True),
        ("capability", "a" * 64),
        ("frame", {"method": "run"}),
    ):
        with pytest.raises(ipc.IPCError):
            ipc.encode_response(response(original, {**result, field: value}))
    wrong_operation = response(original, {**result, "operation_id": uid()})
    with pytest.raises(ipc.IPCError):
        ipc.decode_response(ipc.encode_response(wrong_operation), request=original)


def test_response_matches_request_uuid_action_and_current_generation(binding):
    original = request(binding, "events")
    doc = response(original, page())
    assert ipc.decode_response(ipc.encode_response(doc), request=original) == doc
    for field, value in (("request_id", uid()), ("worker_id", uid()), ("connection_id", uid())):
        with pytest.raises(ipc.IPCError):
            ipc.decode_response(wire({**doc, field: value}), request=original)


@pytest.mark.parametrize(
    "changes",
    [
        {"next_cursor": 7},
        {"next_cursor": True},
        {"next_cursor": -1},
        {"events": [event(5), event(5)]},
        {"events": [event(0)]},
        {"events": [event(7)]},
        {"authority": {"allow": True}},
    ],
)
def test_event_pages_are_bounded_and_monotonic(binding, changes):
    original = request(binding, "events")
    with pytest.raises(ipc.IPCError):
        ipc.encode_response(response(original, page(**changes)))


def test_events_cannot_exceed_the_requested_limit_or_move_before_after(binding):
    original = request(binding, "events", {"after": 4, "limit": 1})
    for result in (page([event(5), event(6)]), page([event(4)]), page([], next_cursor=3)):
        with pytest.raises(ipc.IPCError):
            ipc.decode_response(ipc.encode_response(response(original, result)), request=original)


def test_empty_page_can_advance_over_audit_rows(binding):
    original = request(binding, "events", {"after": 4, "limit": 1})
    result = page([])
    assert (
        ipc.decode_response(ipc.encode_response(response(original, result)), request=original)[
            "result"
        ]
        == result
    )


def test_submit_reserves_native_metadata_space_before_durable_claim(binding):
    doc = request(binding)
    # This fits the private IPC frame but would crowd out the native protocol's metadata.
    doc["params"]["text"] = "\x01" * ((ipc.MAX_FRAME_BYTES - 4096) // 6 + 1)
    assert len(wire(doc)) < ipc.MAX_FRAME_BYTES
    with pytest.raises(ipc.IPCError):
        ipc.validate_request(doc)


def test_normalized_observations_refuse_private_send_frames_and_hidden_claims():
    public = {k: v for k, v in event().items() if k != "cursor"}
    assert ipc.normalize_event(public) == public
    for secret in ("capability", "execution_guard", "raw_frame", "input", "updatedPermissions"):
        with pytest.raises(ipc.IPCError):
            ipc.normalize_event({**public, "data": {**public["data"], secret: {"allow": True}}})
    with pytest.raises(ipc.IPCError):
        ipc.normalize_event({"kind": "send", "data": {"frame": {"result": {"allow": True}}}})


def test_approval_observation_requires_worker_and_native_connection_stamps(binding):
    pending = {
        "kind": "approval",
        "data": {
            "operation_id": uid(),
            "native_turn_id": "turn:1",
            "request_id": "i:17",
            "item_id": "item:4",
            "tool": "command",
            "summary": "Inspect fixture",
            "choices": ["approve", "reject", "cancel"],
            "payload_digest": "a" * 64,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
        },
    }
    assert ipc.normalize_event(pending) == pending
    for field in ("worker_id", "connection_id", "payload_digest", "operation_id"):
        with pytest.raises(ipc.IPCError):
            ipc.normalize_event(
                {**pending, "data": {k: v for k, v in pending["data"].items() if k != field}}
            )
    with pytest.raises(ipc.IPCError):
        ipc.normalize_event(
            {**pending, "data": {**pending["data"], "choices": ["acceptForSession"]}}
        )


def test_snapshot_and_containment_are_explicit_observations(binding):
    snapshot = request(binding, "snapshot")
    result = page(
        state="uncertain", native_id="native:1", model_requested="selected", model_effective=None
    )
    assert (
        ipc.decode_response(ipc.encode_response(response(snapshot, result)), request=snapshot)[
            "result"
        ]
        == result
    )
    probe = request(binding, "probe", {"target_worker_id": binding.worker_id})
    for containment in ("live", "gone", "unknown"):
        result = {"containment": containment}
        assert (
            ipc.decode_response(ipc.encode_response(response(probe, result)), request=probe)[
                "result"
            ]
            == result
        )


def test_error_response_is_closed_and_mutually_exclusive(binding):
    requested = request(binding)
    doc = {
        **{k: v for k, v in requested.items() if k != "params"},
        "type": "response",
        "error": {"code": "unavailable", "message": "Worker state is uncertain"},
    }
    assert ipc.decode_response(ipc.encode_response(doc), request=requested) == doc
    with pytest.raises(ipc.IPCError):
        ipc.encode_response({**doc, "result": {}})
    with pytest.raises(ipc.IPCError):
        ipc.encode_response({**doc, "error": {**doc["error"], "frame": {"secret": "hidden"}}})
