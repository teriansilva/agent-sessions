"""Remove a queued message before it is sent (#1447): real workers/codecs, vendor substituted."""

import pytest

import test_native_clients as client_tests
import test_native_journal as journal_tests
import test_native_runtime as rt
import test_native_send_now as send_now_tests
from agent_sessions import native_journal, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

host, project = rt.host, rt.project
app_client = client_tests.app_client
conversation = journal_tests.conversation
ENGINES, ident, settle = rt.ENGINES, rt.ident, rt.settle
queued_session, input_frames = send_now_tests.queued_session, send_now_tests.input_frames


@pytest.fixture
def anyio_backend():
    return "asyncio"


def journal(source, key):
    return native_runtime._journal(registry.get(ENGINES[source]), key.partition(":")[2])


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_withdraw_removes_one_queued_message_and_keeps_the_rest_in_order(
    host, project, source
):
    key, active, waiting, urgent = await queued_session(source, project)
    assert (await runtime.snapshot(key))["native"]["withdraw"] is True
    operation = ident()
    receipt = await runtime.withdraw(key, operation_id=operation, queued_turn_id=waiting)
    assert receipt["handoff"] == "sent"
    snap = await runtime.snapshot(key)
    assert [t["turn_id"] for t in snap["turns"]] == [active, urgent]
    assert next(t for t in snap["turns"] if t["turn_id"] == urgent)["state"] == "queued"
    # The journal keeps both records: the message settled unsent, the control applied.
    ops = journal(source, key).operations
    assert ops[waiting].handoff == "not_sent" and ops[operation].handoff == "sent"
    # Exact replay answers from the journal, even though the message is gone from the queue.
    assert await runtime.withdraw(key, operation_id=operation, queued_turn_id=waiting) == receipt
    with pytest.raises(runtime.StructuredError, match="different request"):
        await runtime.withdraw(key, operation_id=operation, queued_turn_id=urgent)
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    await settle(key, urgent)
    writes = input_frames(project)
    assert len(writes) == 2
    assert "urgent guidance" in str(writes[1])
    assert "earlier queued" not in str(writes)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_withdraw_refuses_a_message_that_is_not_queued_and_claims_nothing(
    host, project, source
):
    key, active, waiting, urgent = await queued_session(source, project)
    for target in (ident(), active):
        operation = ident()
        with pytest.raises(runtime.StructuredError, match="no longer queued") as error:
            await runtime.withdraw(key, operation_id=operation, queued_turn_id=target)
        assert error.value.status == 409
        assert operation not in journal(source, key).operations
    await runtime.interrupt(key, operation_id=ident(), turn_id=active)
    await settle(key, urgent)
    # Already sent: the agent has it, so it can't be taken back.
    with pytest.raises(runtime.StructuredError, match="no longer queued"):
        await runtime.withdraw(key, operation_id=ident(), queued_turn_id=waiting)
    assert len(input_frames(project)) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("source", ENGINES)
async def test_an_older_worker_offers_no_withdraw_and_is_refused_without_relaunch(
    host, project, source
):
    key, _, waiting, _ = await queued_session(source, project)
    worker = (await runtime.snapshot(key))["native"]["worker"]
    native_state.update_lifecycle(worker, withdraw=None)
    assert "withdraw" not in (await runtime.snapshot(key))["native"]
    launches = len(host.launches)
    operation = ident()
    with pytest.raises(runtime.StructuredError, match="newly started") as error:
        await runtime.withdraw(key, operation_id=operation, queued_turn_id=waiting)
    assert error.value.status == 409
    assert operation not in journal(source, key).operations
    assert len(host.launches) == launches
    assert (
        next(t for t in (await runtime.snapshot(key))["turns"] if t["turn_id"] == waiting)["state"]
        == "queued"
    )


@pytest.mark.anyio
async def test_a_message_being_steered_can_no_longer_be_removed(host, project):
    key, active, _, urgent = await queued_session("codex", project, "STEER_UNKNOWN")
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    await settle(key, urgent, state=("delivering",))
    with pytest.raises(runtime.StructuredError, match="no longer queued"):
        await runtime.withdraw(key, operation_id=ident(), queued_turn_id=urgent)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["claude", "opencode"])
async def test_withdraw_after_an_interrupting_send_now_is_never_both_removed_and_sent(
    host, project, source
):
    key, active, waiting, urgent = await queued_session(source, project)
    await runtime.send_now(key, operation_id=ident(), turn_id=active, queued_turn_id=urgent)
    try:
        await runtime.withdraw(key, operation_id=ident(), queued_turn_id=urgent)
        removed = True
    except runtime.StructuredError as exc:
        assert "no longer queued" in exc.detail
        removed = False
    # The reply stops either way (the interrupt already went out); the rest still runs.
    await settle(key, waiting)
    sent = "urgent guidance" in str(input_frames(project))
    assert sent is not removed
    turns = {t["turn_id"] for t in (await runtime.snapshot(key))["turns"]}
    assert (urgent in turns) is not removed


def test_withdraw_route_binds_only_ids_and_requires_csrf(app_client, monkeypatch):
    body = {"operation_id": ident(), "queued_turn_id": ident()}
    key = f"claude-api:{ident()}"
    calls = []

    async def apply(session_key, **params):
        calls.append((session_key, params))
        return {"handoff": "sent"}

    monkeypatch.setattr(runtime, "withdraw", apply)
    url = f"/api/structured/sessions/{key}/withdraw"
    csrf = app_client.headers.pop("X-CSRF-Token")
    assert app_client.post(url, json=body).status_code == 403
    app_client.headers["X-CSRF-Token"] = csrf
    assert app_client.post(url, json={**body, "turn_id": ident()}).status_code == 422
    assert (
        app_client.post(url, json=body, headers={"Origin": "https://elsewhere.invalid"}).status_code
        == 403
    )
    assert not calls
    assert app_client.post(url, json=body).status_code == 202
    assert calls == [(key, body)]


def _projected(root, sid, binding):
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )
    return {t["turn_id"]: t for t in raw["turns"]}


@pytest.mark.parametrize("proved", ["nothing", "control only", "message only", "both"])
def test_a_removed_message_is_hidden_only_on_durable_proof_of_both_halves(conversation, proved):
    root, sid, binding = conversation
    message = journal_tests.request(binding)
    queued_id = message["params"]["operation_id"]
    native_journal.claim(root, message, queued=True)
    control = journal_tests.request(
        binding,
        action="withdraw",
        revision=native_journal.read(root, sid).revision,
        queued_turn_id=queued_id,
    )
    native_journal.claim(root, control)
    if proved in {"message only", "both"}:
        native_journal.record_handoff(root, binding, queued_id, "not_sent")
    if proved in {"control only", "both"}:
        native_journal.record_handoff(root, binding, control["params"]["operation_id"], "sent")
    turns = _projected(root, sid, binding)
    # A lost or interrupted removal never erases the message: it stays as the journal proves it.
    assert (queued_id in turns) is (proved != "both")


def _claim(root, sid, binding, *, queued=False, **changes):
    claim = journal_tests.request(
        binding, revision=native_journal.read(root, sid).revision, **changes
    )
    native_journal.claim(root, claim, queued=queued)
    return claim["params"]["operation_id"]


def _withdraw(root, sid, binding, queued_id):
    control = _claim(root, sid, binding, action="withdraw", queued_turn_id=queued_id)
    native_journal.record_handoff(root, binding, queued_id, "not_sent")
    native_journal.record_handoff(root, binding, control, "sent")


def test_a_removed_message_takes_no_slot_in_the_turn_window(conversation):
    root, sid, binding = conversation
    window = runtime.SNAPSHOT_TURNS
    sent = [_claim(root, sid, binding) for _ in range(window - 1)]
    for operation in sent:
        native_journal.record_handoff(root, binding, operation, "sent")
    kept, gone = _claim(root, sid, binding, queued=True), _claim(root, sid, binding, queued=True)
    _withdraw(root, sid, binding, gone)
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=window,
    )
    shown = [t["turn_id"] for t in raw["turns"]]
    # The window is full of surviving messages, oldest included, and nothing is "omitted".
    assert shown == [*sent, kept]
    assert raw["omitted_turns"] == 0
    # One more message pushes exactly one real turn out of the window.
    _claim(root, sid, binding, queued=True)
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=window,
    )
    assert len(raw["turns"]) == window and raw["omitted_turns"] == 1
    assert gone not in {t["turn_id"] for t in raw["turns"]}


def test_a_removed_message_is_not_in_the_transcript(conversation):
    from agent_sessions import transcript

    root, sid, binding = conversation
    first = _claim(root, sid, binding, text="kept message")
    native_journal.record_handoff(root, binding, first, "sent")
    gone = _claim(root, sid, binding, queued=True, text="removed message")
    _withdraw(root, sid, binding, gone)
    texts = [t.text for t in transcript._native_turns(root, sid)]
    assert texts == ["kept message"]
