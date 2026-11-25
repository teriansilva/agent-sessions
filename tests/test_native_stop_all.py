"""Stop all and send (#1644): the operator's explicit choice when only a Stop can cut a turn.

Send now on Claude falls back to a plain interrupt, a Stop, which also ends every background
task. That is refused (`would_stop_background`). The operator may confirm ending exactly the
tasks it was shown: the confirmation binds the worker generation and the task identities, beside
the target turn and the queued head the admission already checks, and every part is checked again
after the journal claim. Anything that changed refuses `inventory_changed`; nothing is stopped.
"""

from __future__ import annotations

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_ipc, native_journal
from agent_sessions import structured_runtime as runtime
from test_native_clients import app_client  # noqa: F401
from test_native_send_now import background_worker, conversation, host, ident, project  # noqa: F401

TASKS = ["bg-vitest", "bg-monitor"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _worker(conversation, monkeypatch, *, tasks=TASKS):  # noqa: F811
    """A Claude worker running a turn the agent started itself, background tasks in flight, no
    native Send now: its only way to cut the turn is a Stop."""
    worker, queued, control = background_worker(conversation, monkeypatch, "claude")
    worker.codec._background = set(tasks)
    assert worker.codec.send_now_would_stop_background()
    writes = []

    async def write(frame):
        writes.append(frame)

    monkeypatch.setattr(worker, "write", write)
    return worker, queued, control, writes


def _stop_all(worker, control, *, tasks=TASKS, worker_id=None):
    root_params = dict(control["params"])
    root_params.update(
        mode="stop_all",
        confirm_worker_id=worker_id or worker.worker_id,
        confirm_tasks=native_ipc.tasks_digest(tasks),
    )
    return {**control, "params": root_params}


@pytest.mark.anyio
async def test_send_now_that_would_stop_background_work_says_so_in_a_closed_reason(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, queued, control, writes = _worker(conversation, monkeypatch)
    result = await worker.effect(control)
    assert result["error"]["reason"] == "would_stop_background"
    assert result["error"]["code"] == "unsupported"
    assert writes == [] and worker.queued == [queued["params"]]
    # The reason survives the private IPC boundary.
    assert native_ipc.validate_response(result)["error"]["reason"] == "would_stop_background"


@pytest.mark.anyio
async def test_stop_all_with_the_exact_confirmation_stops_and_sends(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, queued, control, writes = _worker(conversation, monkeypatch)
    result = await worker.effect(_stop_all(worker, control))
    assert result["result"]["handoff"] == "sent"
    (frame,) = writes
    assert frame["type"] == "control_request" and frame["request"] == {"subtype": "interrupt"}
    assert worker.queued == [queued["params"]]  # promoted when the turn ends, never consumed
    root, sid, _ = conversation
    op = native_journal.read(root, sid).operations[control["params"]["operation_id"]]
    assert op.request["params"]["mode"] == "stop_all"  # its own journaled mode


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    ["task-started", "task-ended", "other-generation", "other-turn", "other-head"],
)
async def test_any_change_before_the_claim_refuses_and_stops_nothing(
    conversation,  # noqa: F811
    monkeypatch,
    change,
):
    worker, queued, control, writes = _worker(conversation, monkeypatch)
    request = _stop_all(worker, control)
    if change == "task-started":
        worker.codec._background.add("bg-new")
    elif change == "task-ended":
        worker.codec._background.discard("bg-vitest")
    elif change == "other-generation":
        request = _stop_all(worker, control, worker_id=ident())
    elif change == "other-turn":
        request["params"]["turn_id"] = ident()
    else:
        root, sid, binding = conversation
        ahead = journal_tests.request(binding, revision=native_journal.read(root, sid).revision)
        native_journal.claim(root, ahead, queued=True)
        worker.queued.insert(0, ahead["params"])
    result = await worker.effect(request)
    # Every changed binding asks again (Hermes on #1650): the turn and the head too.
    assert result["error"]["reason"] == "inventory_changed"
    assert writes == []
    assert queued["params"] in worker.queued


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["task-started", "task-ended", "turn-ended"])
async def test_a_change_during_the_claim_records_not_sent_and_stops_nothing(
    conversation,  # noqa: F811
    monkeypatch,
    change,
):
    """The other side of the claim: the fsync is an await, and frames are read meanwhile."""
    worker, queued, control, writes = _worker(conversation, monkeypatch)
    original = native_journal.claim

    def change_during_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "task-started":
            worker.codec._background.add("bg-new")
        elif change == "task-ended":
            worker.codec._background.discard("bg-monitor")
        else:
            worker.codec.unowned_turn = False
            worker.codec.background_turn = None
        return result

    monkeypatch.setattr(native_journal, "claim", change_during_claim)
    result = await worker.effect(_stop_all(worker, control))
    assert result["result"]["handoff"] == "not_sent"
    assert writes == [] and worker.queued == [queued["params"]]


@pytest.mark.anyio
async def test_a_failed_stop_write_is_uncertain_and_never_retried(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, queued, control, _ = _worker(conversation, monkeypatch)
    attempts = []

    async def broken(frame):
        attempts.append(frame)
        raise OSError("stdin closed")

    monkeypatch.setattr(worker, "write", broken)
    result = await worker.effect(_stop_all(worker, control))
    assert result["result"]["handoff"] != "sent"
    assert len(attempts) == 1  # once, never again by itself
    assert worker.queued == [queued["params"]] and worker.priority_turn is None


@pytest.mark.anyio
async def test_stop_all_takes_the_clis_own_send_now_when_it_is_there(
    conversation,  # noqa: F811
    monkeypatch,
):
    """The confirmation permits a Stop; it never prefers one. With the CLI's own Send now (its
    fence proved), the tasks survive."""
    worker, queued, control, writes = _worker(conversation, monkeypatch)
    worker.codec.native_send_now = True
    assert worker.codec.send_now_ready()

    async def current(_queued):
        return True

    monkeypatch.setattr(worker, "_answer_current", current)
    result = await worker.effect(_stop_all(worker, control))
    assert result["result"]["handoff"] == "sent"
    assert writes[0]["type"] == "user"
    assert writes[-1]["request"].get("send_now") is True
    assert worker.codec._background == set(TASKS)


def test_the_confirmation_goes_with_stop_all_and_only_with_it():
    base = {
        "operation_id": ident(),
        "expected_revision": 1,
        "turn_id": ident(),
        "queued_turn_id": ident(),
    }
    confirm = {"confirm_worker_id": ident(), "confirm_tasks": native_ipc.tasks_digest(TASKS)}
    native_ipc._params("send_now", {**base, "mode": "stop_all", **confirm})
    native_ipc._params("send_now", {**base, "mode": "interrupt"})
    for bad in (
        {**base, "mode": "stop_all"},
        {**base, "mode": "interrupt", **confirm},
        {**base, "mode": "stop_all", **confirm, "confirm_tasks": "labels"},
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc._params("send_now", bad)


def test_the_digest_is_task_identity_not_order_or_labels():
    assert native_ipc.tasks_digest(["a", "b"]) == native_ipc.tasks_digest(["b", "a", "a"])
    assert native_ipc.tasks_digest(["a"]) != native_ipc.tasks_digest(["a", "b"])


def test_an_unknown_refusal_reason_is_an_invalid_frame(conversation, monkeypatch):  # noqa: F811
    worker, _, control, _ = _worker(conversation, monkeypatch)
    response = worker.error(control, "stale", "x", "because")
    with pytest.raises(native_ipc.IPCError):
        native_ipc.validate_response(response)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value",
    [
        {"worker": "w", "task_ids": ["a"]},
        {"task_ids": ["a"]},
        {"worker": None, "task_ids": []},
        {"worker": None, "task_ids": ["x" * 300]},
        {"worker": None, "task_ids": "a"},
        {"worker": None, "task_ids": ["a"], "labels": ["vitest"]},
    ],
)
async def test_a_malformed_confirmation_is_refused_at_the_boundary(value):
    value = {**value, **({"worker": ident()} if value.get("worker", 0) is None else {})}
    with pytest.raises(runtime.StructuredError) as error:
        await runtime.send_now(
            f"claude-api:{ident()}",
            operation_id=ident(),
            turn_id=ident(),
            queued_turn_id=ident(),
            stop_all=value,
        )
    assert error.value.status == 422


def test_the_route_passes_the_confirmation_and_answers_a_closed_reason(
    app_client,  # noqa: F811
    monkeypatch,
):
    key = f"claude-api:{ident()}"
    body = {"operation_id": ident(), "turn_id": ident(), "queued_turn_id": ident()}
    confirm = {"worker": ident(), "task_ids": TASKS}
    calls = []

    async def refuse(session_key, **params):
        calls.append(params)
        raise runtime.StructuredError(409, "changed", "inventory_changed")

    monkeypatch.setattr(runtime, "send_now", refuse)
    url = f"/api/structured/sessions/{key}/send-now"
    r = app_client.post(url, json={**body, "stop_all": confirm})
    assert r.status_code == 409
    assert r.json() == {"detail": "changed", "reason": "inventory_changed"}
    assert calls == [{**body, "stop_all": confirm}]
    csrf = app_client.headers.pop("X-CSRF-Token")
    assert app_client.post(url, json={**body, "stop_all": confirm}).status_code == 403
    app_client.headers["X-CSRF-Token"] = csrf
    assert len(calls) == 1


# --- P3: what Send now would do, for the composer (a hint, never authority) --------------------


@pytest.mark.anyio
async def test_the_worker_reports_send_now_state_once_per_change(
    conversation,  # noqa: F811
    monkeypatch,
):
    worker, _, _, _ = _worker(conversation, monkeypatch)
    reports = []
    monkeypatch.setattr(worker, "report", lambda **fields: reports.append(fields))
    await worker.handle_events([])
    await worker.handle_events([])
    worker.codec._background.clear()
    await worker.handle_events([])
    assert reports == [
        {"send_now_state": "would_stop_background"},
        {"send_now_state": "ready"},
    ]


@pytest.mark.anyio
async def test_the_snapshot_says_unknown_without_a_live_worker_report(host, project):  # noqa: F811
    from test_native_send_now import queued_session

    key, *_ = await queued_session("claude", project, queue=False)
    # The fake CLI reports no background task: Send now would stop nothing.
    assert (await runtime.snapshot(key))["native"]["send_now_state"] == "ready"
    await runtime.stop(key)
    assert (await runtime.snapshot(key))["native"]["send_now_state"] == "unknown"
