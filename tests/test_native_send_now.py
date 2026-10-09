"""Shared immediate delivery: real workers/codecs, only vendor/service manager substituted."""

import asyncio
import contextlib

import pytest

import test_native_clients as client_tests
import test_native_images as image_tests
import test_native_journal as journal_tests
import test_native_runtime as rt
from agent_sessions import native_ipc, native_journal, native_runtime, native_state, native_worker
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import storage

host, project = rt.host, rt.project
conversation = journal_tests.conversation
uploads = image_tests.uploads
app_client = client_tests.app_client
ENGINES, frames, ident, settle = rt.ENGINES, rt.frames, rt.ident, rt.settle


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def queued_session(source, project, text="urgent guidance"):
    key = (await runtime.create_session(ENGINES[source], str(project), operation_id=ident()))[
        "session_key"
    ]
    active, waiting, urgent = ident(), ident(), ident()
    await runtime.submit_turn(key, operation_id=active, text="HANG")
    # Wait for the native turn id, independent of the submit handoff receipt.
    for _ in range(200):
        events = (await runtime.events(key, after=0))["events"]
        if any(e["kind"] == "turn_started" for e in events):
            break
        await asyncio.sleep(0.02)
    await runtime.submit_turn(key, operation_id=waiting, text="earlier queued")
    await runtime.submit_turn(key, operation_id=urgent, text=text)
    return key, active, waiting, urgent


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_accepted_send_now_ipc_loss_is_uncertain_and_replays_once(
    host, project, monkeypatch, source
):
    key, active, waiting, urgent = await queued_session(source, project)
    operation = ident()
    decode = native_ipc.decode_response
    lost = False

    def lose_response(frame, *, request):
        nonlocal lost
        response = decode(frame, request=request)
        if request["action"] == "send_now" and not lost:
            lost = True
            assert response["result"]["handoff"] == "sent"
            raise ConnectionResetError("accepted worker response lost")
        return response

    monkeypatch.setattr(native_ipc, "decode_response", lose_response)
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    assert error.value.status == 503  # A 409 would tell the browser to discard its recovery ID.
    assert "may still be applied" in error.value.detail
    launches = len(host.launches)
    receipt = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert receipt["handoff"] == "sent"
    if source == "codex":
        await settle(key, urgent, state=("delivered",))
        await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    await settle(key, waiting)
    assert len(input_frames(project)) == 3
    assert len(host.launches) == launches


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_send_now_connection_refusal_before_write_is_definite(
    host, project, monkeypatch, source
):
    key, active, _, urgent = await queued_session(source, project)
    operation = ident()

    async def refuse(*args, **kwargs):
        raise ConnectionRefusedError("socket did not accept a connection")

    monkeypatch.setattr(asyncio, "open_unix_connection", refuse)
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    assert error.value.status == 409
    journal = native_runtime._journal(registry.get(ENGINES[source]), key.partition(":")[2])
    assert operation not in journal.operations
    assert len(input_frames(project)) == 1


def input_frames(project):
    return [
        f["frame"]
        for f in frames(project)
        if f["frame"].get("method")
        in {
            "turn/start",
            "turn/steer",
            "session/prompt",
        }
        or f["frame"].get("type") == "user"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_send_now_reuses_queued_message_and_preserves_remaining_order(host, project, source):
    key, active, waiting, urgent = await queued_session(source, project)
    mode = "steer" if source == "codex" else "interrupt"
    assert (await runtime.snapshot(key))["native"]["send_now"] == mode
    operation = ident()
    result = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert result["handoff"] == "sent"
    if source == "codex":
        snap, turn = await settle(key, urgent, state=("delivered",))
        assert turn["delivery"] == "steered"
        assert snap["active_turn"] == active
        assert next(t for t in snap["turns"] if t["turn_id"] == waiting)["state"] == "queued"
        writes = input_frames(project)
        assert [f["method"] for f in writes] == ["turn/start", "turn/steer"]
        assert writes[-1]["params"]["clientUserMessageId"] == urgent
        await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    snap, _ = await settle(key, waiting)
    assert len(snap["turns"]) == 3
    assert snap["turns"][0]["state"] == "interrupted"
    replay = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert replay == result
    with pytest.raises(runtime.StructuredError, match="different request"):
        await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=waiting)
    writes = input_frames(project)
    assert len(writes) == 3
    assert "urgent guidance" in str(writes[1]) and "earlier queued" in str(writes[2])


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_stale_target_and_old_worker_cannot_deliver_input(host, project, source):
    key, active, _, urgent = await queued_session(source, project)
    with pytest.raises(runtime.StructuredError, match="no longer queued"):
        await runtime.send_now(key, operation_id=ident(), turn_id=ident(), queued_turn_id=urgent)
    with pytest.raises(runtime.StructuredError, match="no longer queued"):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=ident())
    worker = (await runtime.snapshot(key))["native"]["worker"]
    native_state.update_lifecycle(worker, send_now=None)
    assert "send_now" not in (await runtime.snapshot(key))["native"]
    with pytest.raises(runtime.StructuredError, match="newly started"):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    assert len(input_frames(project)) == 1


@pytest.mark.anyio
async def test_steering_refusal_does_not_replace_active_turn_or_resend_input(host, project):
    key, active, _, urgent = await queued_session("codex", project, "STEER_REFUSE")
    operation = ident()
    await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    snap, turn = await settle(key, urgent)
    assert turn["state"] == "failed" and "steering refused" in turn["reason"]
    assert snap["active_turn"] == active
    await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    assert len(input_frames(project)) == 2


@pytest.mark.anyio
async def test_steering_ack_can_arrive_after_original_completion(host, project):
    key, active, waiting, urgent = await queued_session("codex", project, "STEER_FINISH_FIRST")
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    _, turn = await settle(key, urgent, state=("delivered",))
    assert turn["delivery"] == "steered"
    await settle(key, waiting)


@pytest.mark.anyio
async def test_unknown_steering_is_not_replayed_by_successor(host, project):
    key, active, _, urgent = await queued_session("codex", project, "STEER_UNKNOWN")
    operation = ident()
    await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    _, turn = await settle(key, urgent, state=("delivering",))
    assert turn["delivery"] == "steering"
    host.kill((await runtime.snapshot(key))["native"]["worker"])
    _, turn = await settle(key, urgent, state=("uncertain",))
    await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    fresh = ident()
    await runtime.submit_turn(key, operation_id=fresh, text="new deliberate turn")
    await settle(key, fresh)
    assert len(input_frames(project)) == 3
    journal = native_journal.read(
        native_runtime._journal_root(registry.get(ENGINES["codex"])), key.partition(":")[2]
    )
    assert journal.operations[urgent].handoff == "sent"


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_concurrent_tabs_share_one_control_receipt(host, project, source):
    key, active, _, urgent = await queued_session(source, project)
    operation = ident()
    a, b = await asyncio.gather(
        *(
            runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
            for _ in range(2)
        )
    )
    assert a == b
    await settle(key, urgent, state=("delivered", "completed"))
    writes = [f["frame"] for f in frames(project)]
    controls = [
        f
        for f in writes
        if f.get("method") in {"turn/steer", "session/cancel"}
        or f.get("request", {}).get("subtype") == "interrupt"
    ]
    assert len(controls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["claude", "opencode"])
async def test_cancel_ack_does_not_dispatch_until_native_completion(host, project, source):
    key, active, _, urgent = await queued_session(source, project)
    (project / "hold-cancel-completion").touch()
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    for _ in range(5):
        snap = await runtime.snapshot(key)
        assert snap["active_turn"] == active
        turn = next(t for t in snap["turns"] if t["turn_id"] == urgent)
        assert turn["state"] == "queued" and turn["delivery"] == "interrupting"
        assert len(input_frames(project)) == 1
        await asyncio.sleep(0.04)
    with pytest.raises(runtime.StructuredError, match="already waiting"):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    (project / "release-cancel-completion").touch()
    await settle(key, urgent)


@pytest.mark.anyio
async def test_native_cancel_refusal_is_visible_and_explicit_retry_can_succeed(host, project):
    key, active, _, urgent = await queued_session("claude", project)
    marker = project / "refuse-interrupt"
    marker.touch()
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    for _ in range(200):
        snap = await runtime.snapshot(key)
        turn = next(t for t in snap["turns"] if t["turn_id"] == urgent)
        if turn.get("delivery") == "interrupt_failed":
            break
        await asyncio.sleep(0.025)
    assert turn["delivery"] == "interrupt_failed"
    assert turn["state"] == "queued" and "refused" in turn["delivery_reason"]
    marker.unlink()  # isolated test fixture, not user data
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    await settle(key, urgent)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_busy_admission_keeps_original_queue_and_control_retryable(host, project, source):
    key, active, _, urgent = await queued_session(source, project)
    operation = ident()
    with storage.locked("launch", wait=0):
        with pytest.raises(runtime.StructuredError, match="another launch"):
            await runtime.send_now(
                key, operation_id=operation, turn_id=active, queued_turn_id=urgent
            )
        assert len(input_frames(project)) == 1
        assert [t["state"] for t in (await runtime.snapshot(key))["turns"]] == [
            "running",
            "queued",
            "queued",
        ]
    await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
    await settle(key, urgent, state=("delivered", "completed"))


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_stopped_session_cannot_deliver_queued_input_or_launch_a_successor(
    host, project, source
):
    key, active, _, urgent = await queued_session(source, project)
    assert (await runtime.stop(key))["containment"] == "gone"
    with pytest.raises(runtime.StructuredError):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    assert len(host.launches) == 1
    assert len(input_frames(project)) == 1
    _, turn = await settle(key, urgent)
    assert "not sent" in turn["reason"]


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["claude", "opencode"])
async def test_stop_during_cancellation_prevents_selected_dispatch(host, project, source):
    key, active, _, urgent = await queued_session(source, project)
    (project / "hold-cancel-completion").touch()
    operation = ident()
    receipt = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    assert (await runtime.stop(key))["containment"] == "gone"
    assert (
        await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
        == receipt
    )
    assert len(input_frames(project)) == 1
    assert len(host.launches) == 1
    _, turn = await settle(key, urgent)
    assert "not sent" in turn["reason"]


def test_codex_steering_response_is_bound_to_expected_native_turn():
    from agent_sessions.native_protocol import ProtocolError
    from test_native_protocol import OPERATION, TURN, codex_running

    codec = codex_running()
    queued = ident()
    frame = codec.steer("guidance", queued, [("image/png", "aGVsbG8=")])
    assert frame["params"]["expectedTurnId"] == TURN
    assert frame["params"]["input"][0]["url"] == "data:image/png;base64,aGVsbG8="
    assert codec.operation_id == OPERATION  # output/approvals keep the original correlation
    with pytest.raises(ProtocolError, match="does not match"):
        codec.feed({"id": frame["id"], "result": {"turnId": "another-turn"}})


def prepared_worker(conversation, monkeypatch):
    from test_native_protocol import OPERATION, codex_running

    root, sid, binding = conversation
    worker = native_worker.Worker(
        {
            "worker_id": binding.worker_id,
            "session_key": binding.session_key,
            "adapter": binding.adapter,
            "journal_root": str(root),
            "capability": native_ipc.Capability.create()._value,
            "connection_id": binding.connection_id,
        },
        root,
    )
    worker.codec = codex_running()
    worker.ready.set()
    monkeypatch.setattr(worker, "gate_open", lambda: True)
    monkeypatch.setattr(worker, "queued_admission", contextlib.nullcontext)
    native_journal.claim(root, journal_tests.request(binding, operation_id=OPERATION))
    queued = journal_tests.request(binding, revision=native_journal.read(root, sid).revision)
    native_journal.claim(root, queued, queued=True)
    worker.queued.append(queued["params"])
    control = journal_tests.request(
        binding,
        action="send_now",
        turn_id=OPERATION,
        queued_turn_id=queued["params"]["operation_id"],
        mode="steer",
        revision=native_journal.read(root, sid).revision,
    )
    return worker, queued, control


@pytest.mark.anyio
async def test_send_now_before_native_turn_ack_keeps_original_input_queued(
    conversation, monkeypatch
):
    worker, queued, control = prepared_worker(conversation, monkeypatch)
    worker.codec.native_turn_id = None
    result = await worker.effect(control)
    assert result["error"]["code"] == "busy"
    assert worker.queued == [queued["params"]]
    journal = native_journal.read(*conversation[:2])
    assert control["params"]["operation_id"] not in journal.operations
    assert journal.operations[queued["params"]["operation_id"]].handoff == "queued"


@pytest.mark.anyio
async def test_click_racing_completion_during_claim_keeps_original_queue(conversation, monkeypatch):
    worker, queued, control = prepared_worker(conversation, monkeypatch)
    original_claim = native_journal.claim

    def finish_during_claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        worker.codec.operation_id = None
        return result

    monkeypatch.setattr(native_journal, "claim", finish_during_claim)

    async def forbidden(frame):
        pytest.fail("the stale active turn must never receive a native write")

    monkeypatch.setattr(worker, "write", forbidden)
    result = await worker.effect(control)
    assert result["result"]["handoff"] == "not_sent"
    assert worker.queued == [queued["params"]]
    journal = native_journal.read(*conversation[:2])
    assert journal.operations[queued["params"]["operation_id"]].handoff == "queued"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["before_write", "write_failure", "after_write"])
async def test_partial_delivery_never_becomes_replayable(conversation, monkeypatch, failure):
    worker, queued, control = prepared_worker(conversation, monkeypatch)
    writes = []
    original_record = native_journal.record_handoff
    queued_id, control_id = queued["params"]["operation_id"], control["params"]["operation_id"]

    def record(root, binding, operation_id, handoff):
        result = original_record(root, binding, operation_id, handoff)
        if (failure == "before_write" and operation_id == queued_id and handoff == "uncertain") or (
            failure == "after_write" and operation_id == control_id and handoff == "sent"
        ):
            raise native_journal.JournalError("unavailable", "simulated failed receipt")
        return result

    monkeypatch.setattr(native_journal, "record_handoff", record)

    async def write(frame):
        writes.append(frame)
        if failure == "write_failure":
            raise TimeoutError("partial stdin handoff")

    monkeypatch.setattr(worker, "write", write)
    await worker.effect(control)
    assert worker.terminated
    assert len(writes) == (0 if failure == "before_write" else 1)
    journal = native_journal.read(*conversation[:2])
    assert journal.operations[queued_id].handoff in {"uncertain", "sent"}
    # Simulate the web process recovering the exact control after worker death.
    immutable = native_ipc.immutable_request(control)
    assert native_runtime._replay(journal, control_id, immutable) is not None
    assert len(writes) == (0 if failure == "before_write" else 1)


@pytest.mark.anyio
async def test_steering_rechecks_queued_image_bytes(host, project, uploads):
    key, active, _, _ = await queued_session("codex", project)
    name = uploads("20261008-010000-guidance.png", image_tests.PNG)
    queued = ident()
    await runtime.submit_turn(key, operation_id=queued, text="inspect image", attachments=[name])
    (uploads.dir / name).write_bytes(image_tests.PNG + b"changed")
    with pytest.raises(runtime.StructuredError, match="not applied"):
        await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=queued)
    _, turn = await settle(key, queued)
    assert turn["state"] == "failed" and "not sent" in turn["reason"]
    assert len(input_frames(project)) == 1


def test_send_now_route_binds_only_ids_and_requires_csrf(app_client, monkeypatch):
    body = {"operation_id": ident(), "turn_id": ident(), "queued_turn_id": ident()}
    key = f"codex-api:{ident()}"
    calls = []

    async def apply(session_key, **params):
        calls.append((session_key, params))
        return {"handoff": "sent"}

    monkeypatch.setattr(runtime, "send_now", apply)
    url = f"/api/structured/sessions/{key}/send-now"
    csrf = app_client.headers.pop("X-CSRF-Token")
    assert app_client.post(url, json=body).status_code == 403
    app_client.headers["X-CSRF-Token"] = csrf
    assert app_client.post(url, json={**body, "mode": "steer"}).status_code == 422
    assert (
        app_client.post(url, json=body, headers={"Origin": "https://elsewhere.invalid"}).status_code
        == 403
    )
    assert not calls
    assert app_client.post(url, json=body).status_code == 202
    assert calls == [(key, body)]
    app_client.cookies.clear()
    assert app_client.post(url, json=body).status_code in {401, 403}
    assert len(calls) == 1


@pytest.mark.anyio
async def test_late_cancel_refusal_cannot_unlock_newer_selection(conversation, monkeypatch):
    from agent_sessions.native_protocol import NativeEvent

    worker, queued, old_control = prepared_worker(conversation, monkeypatch)
    # The old response finished naturally; a new selection is already cancelling a later turn.
    newer = ident()
    worker.priority_turn = newer
    worker.priority_requests["old-cancel"] = (
        queued["params"]["operation_id"],
        "old-native-turn",
        old_control["params"]["operation_id"],
        old_control["params"]["turn_id"],
    )
    events = []
    monkeypatch.setattr(worker, "record", events.extend)
    await worker.handle_events(
        [NativeEvent("error", {"request_id": "old-cancel", "message": "already finished"})]
    )
    assert worker.priority_turn == newer
    assert events[-1].kind == "delivery_failed"
    assert events[-1].data["control_id"] == old_control["params"]["operation_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("completion", ["before", "after"])
async def test_cancel_refusal_preserves_fifo_without_send_now_retry(host, project, completion):
    key, active, waiting, urgent = await queued_session("claude", project)
    (project / "refuse-interrupt").touch()
    (project / f"finish-{completion}-refusal").touch()
    operation = ident()
    receipt = await runtime.send_now(
        key, operation_id=operation, turn_id=active, queued_turn_id=urgent
    )
    if completion == "before":
        await settle(key, active)
        # Terminal evidence alone must not dispatch while the correlated cancel reply is pending.
        await asyncio.sleep(0.1)
        assert len(input_frames(project)) == 1
        snapshot = await runtime.snapshot(key)
        assert all(t["state"] == "queued" for t in snapshot["turns"] if t["turn_id"] != active)
        (project / "release-queued-turn").touch()
    await settle(key, urgent)
    await settle(key, waiting)
    writes = input_frames(project)
    assert [f["message"]["content"] for f in writes] == [
        "HANG",
        "earlier queued",
        "urgent guidance",
    ]
    assert (
        await runtime.send_now(key, operation_id=operation, turn_id=active, queued_turn_id=urgent)
        == receipt
    )
    assert len(input_frames(project)) == 3
