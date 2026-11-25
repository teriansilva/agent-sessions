"""Durable structured requests share the existing chat loop, fences and per-file consent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import uuid

import httpx
import pytest

import test_chat_agent as api
import test_chat_edits as edits
import test_chat_tools as reads
import test_plugins
from agent_sessions import (
    chat_config,
    chat_edits,
    chat_runtime,
    chat_store,
    chat_tools,
    fileedit,
    template_secrets,
)
from agent_sessions.plugins import admission
from agent_sessions.plugins.manifest import parse
from agent_sessions.structured_types import ExecutionGuard

endpoint = api.endpoint
anyio_backend = api.anyio_backend
project = reads.project
lease_signal = edits.lease_signal
client = api.client


class Authority:
    def __init__(self, binding="mission-grant-1"):
        self.allowed = True
        self.held = False
        self.entries = 0
        self.releases = 0
        self.guard = ExecutionGuard(binding, self.acquire)

    @contextlib.asynccontextmanager
    async def acquire(self):
        if not self.allowed:
            raise admission.Refused("original mission authority was withdrawn")
        assert not self.held
        self.held = True
        self.entries += 1

        def release():
            if self.held:
                self.releases += 1
                self.held = False

        try:
            yield release
        finally:
            release()


def ident():
    return str(uuid.uuid4())


async def settle(sid):
    task = chat_runtime.running_task(api.ENGINE, sid)
    if task is not None:
        await task
    return await chat_runtime.get_session(api.ENGINE, sid)


@pytest.mark.anyio
async def test_creation_replay_binds_request_and_preserves_interactive_creation(endpoint, tmp_path):
    api.configure()
    sid = ident()
    assert await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid) == sid
    first = await chat_runtime.get_session(api.ENGINE, sid)
    assert first["request"] == {"cwd": str(tmp_path), "model": None, "execution_binding": None}
    assert first["model"] == "test-model"
    assert first["revision"] == 1
    assert await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid) == sid
    assert await chat_runtime.get_session(api.ENGINE, sid) == first
    for changed in ({"cwd": str(tmp_path / "other")}, {"model": "default"}):
        with pytest.raises(chat_runtime.ChatError) as error:
            await chat_runtime.new_session(
                api.ENGINE,
                changed.get("cwd", str(tmp_path)),
                session_id=sid,
                model=changed.get("model"),
            )
        assert error.value.status == 409
    a, b = await asyncio.gather(
        chat_runtime.new_session(api.ENGINE, str(tmp_path)),
        chat_runtime.new_session(api.ENGINE, str(tmp_path)),
    )
    assert a != b != sid
    assert not endpoint.requests


@pytest.mark.anyio
async def test_creation_model_replay_never_silently_switches_model(endpoint, tmp_path):
    api.configure()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(tmp_path), session_id=ident(), model="default"
    )
    chat_config.set_config(api.ENGINE, {"model": "changed-model"})
    assert (
        await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid, model="default")
        == sid
    )
    with pytest.raises(chat_runtime.ChatError, match="configured model changed"):
        await chat_runtime.send(api.ENGINE, sid, ident(), "work")
    assert not endpoint.requests


@pytest.mark.anyio
async def test_creation_replay_retries_failed_publication_sync(endpoint, tmp_path, monkeypatch):
    api.configure()
    sid = ident()
    sync = chat_store.sync_directory
    calls = 0

    def fail_once(root):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("publication fsync failed")
        return sync(root)

    monkeypatch.setattr(chat_store, "sync_directory", fail_once)
    with pytest.raises(OSError, match="publication fsync"):
        await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid)
    assert await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid) == sid
    assert calls == 2
    assert chat_store.session_ids(tmp_path / "chat-store") == [sid]


@pytest.mark.anyio
async def test_declared_capabilities_gate_creation_and_continuation(
    endpoint, tmp_path, monkeypatch
):
    api.configure()
    prov = chat_runtime._chat_provider(api.ENGINE)
    doc = test_plugins.doc(api.ENGINE)
    doc["capabilities"]["resume"] = False
    monkeypatch.setattr(prov, "manifest", parse(doc))
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    tid = ident()
    await chat_runtime.send(api.ENGINE, sid, tid, "first")
    await settle(sid)
    with pytest.raises(chat_runtime.ChatError, match="another conversation turn"):
        await chat_runtime.send(api.ENGINE, sid, ident(), "second")
    doc["capabilities"]["new"] = False
    monkeypatch.setattr(prov, "manifest", parse(doc))
    with pytest.raises(chat_runtime.ChatError, match="creating conversations"):
        await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    assert (await chat_runtime.send(api.ENGINE, sid, tid, "first", idempotent=True))["turn"][
        "status"
    ] == "done"
    assert chat_store.session_ids(tmp_path / "chat-store") == [sid]
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_same_creation_from_two_processes_has_one_durable_identity(endpoint, tmp_path):
    api.configure()
    sid = ident()
    code = """
import asyncio, json, sys
from agent_sessions import chat_runtime
async def run():
    try:
        sid = await chat_runtime.new_session(sys.argv[1], sys.argv[2], session_id=sys.argv[3])
        print(json.dumps({'session_id': sid}))
    except chat_runtime.ChatError as exc:
        print(json.dumps({'status': exc.status}))
asyncio.run(run())
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, api.ENGINE, str(tmp_path), sid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = []
    for child in children:
        stdout, stderr = await asyncio.to_thread(child.communicate, timeout=60)
        assert child.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert {"session_id": sid} in results
    assert all(r in ({"session_id": sid}, {"status": 409}) for r in results)
    assert await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=sid) == sid
    assert chat_store.session_ids(tmp_path / "chat-store") == [sid]


def test_revision_counts_complete_appends_and_ignores_a_json_shaped_torn_tail(tmp_path):
    sid, tid = ident(), ident()
    chat_store.create(tmp_path, sid, cwd=str(tmp_path))
    chat_store.append(tmp_path, sid, {"type": "user", "turn_id": tid, "text": "hello"})
    path = tmp_path / f"{sid}.jsonl"
    with path.open("ab") as fh:
        fh.write(json.dumps({"type": "status", "turn_id": tid, "status": "done"}).encode())
    assert chat_store.read(tmp_path, sid).revision == 2
    assert chat_store.read(tmp_path, sid).turn(tid).status == "pending"
    chat_store.append(tmp_path, sid, {"type": "status", "turn_id": tid, "status": "failed"})
    assert chat_store.read(tmp_path, sid).revision == 4
    assert chat_store.read(tmp_path, sid).turn(tid).status == "failed"


@pytest.mark.anyio
async def test_pending_duplicate_is_idempotent_before_revision_but_changed_context_refuses(
    endpoint,
    tmp_path,
):
    api.configure()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    tid = ident()
    endpoint.gate = asyncio.Event()
    context = {"mission_id": "msn_123", "flow_revision": "v1", "step_id": "build", "episode": 2}
    try:
        await chat_runtime.send(api.ENGINE, sid, tid, "hello", expected_revision=1, context=context)
        repeated = await chat_runtime.send(
            api.ENGINE,
            sid,
            tid,
            "hello",
            expected_revision=1,
            context=dict(context),
            idempotent=True,
        )
        assert repeated["turn"]["status"] == "pending"
        for changed in ("different", "context"):
            with pytest.raises(chat_runtime.ChatError) as error:
                await chat_runtime.send(
                    api.ENGINE,
                    sid,
                    tid,
                    "hello" if changed == "context" else changed,
                    context={**context, "episode": 3} if changed == "context" else context,
                )
            assert error.value.status == 409
    finally:
        endpoint.gate.set()
        await settle(sid)
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_peer_replay_waits_for_original_append_durability(endpoint, tmp_path, monkeypatch):
    api.configure()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    root, tid = tmp_path / "chat-store", ident()
    owner = chat_edits.turn_fence(root, sid)
    entered, release, synced = threading.Event(), threading.Event(), threading.Event()
    replay_read = threading.Event()
    writer_ident = []
    original_sync, original_read = os.fsync, chat_store.read

    def pause_sync(fd):
        if threading.get_ident() in writer_ident:
            entered.set()
            assert release.wait(30)
            original_sync(fd)
            synced.set()
        else:
            original_sync(fd)

    def observed_read(*args, **kwargs):
        if kwargs.get("durable"):
            replay_read.set()
        value = original_read(*args, **kwargs)
        if kwargs.get("durable"):
            assert synced.is_set()
        return value

    def append():
        writer_ident.append(threading.get_ident())
        chat_store.append(
            root,
            sid,
            {"type": "user", "turn_id": tid, "text": "hello"},
            {"type": "status", "turn_id": tid, "status": "pending"},
        )

    monkeypatch.setattr(chat_store.os, "fsync", pause_sync)
    monkeypatch.setattr(chat_store, "read", observed_read)
    writer = asyncio.create_task(asyncio.to_thread(append))
    replay = None
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        replay = asyncio.create_task(
            chat_runtime.send(api.ENGINE, sid, tid, "hello", idempotent=True)
        )
        assert await asyncio.to_thread(replay_read.wait, 30)
        assert not replay.done()
    finally:
        release.set()
        await writer
        if replay is not None:
            result = await replay
        owner.release()
    assert result["turn"]["status"] == "pending"
    assert not endpoint.requests


@pytest.mark.anyio
async def test_stale_revision_refuses_new_turn_and_unknown_context_never_persists(
    endpoint, tmp_path
):
    api.configure()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    for kwargs, status in (
        ({"expected_revision": 0}, 409),
        ({"expected_revision": True}, 422),
        ({"context": {"bypass": True}}, 422),
    ):
        with pytest.raises(chat_runtime.ChatError) as error:
            await chat_runtime.send(api.ENGINE, sid, ident(), "hello", **kwargs)
        assert error.value.status == status
    assert (await settle(sid))["turns"] == []
    assert not endpoint.requests


@pytest.mark.anyio
async def test_guard_is_rechecked_after_payload_build_and_before_network(
    endpoint, tmp_path, monkeypatch
):
    api.configure()
    authority = Authority()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(tmp_path), session_id=ident(), execution_admission=authority.guard
    )
    redact = template_secrets.redact_messages

    def withdraw(messages):
        authority.allowed = False
        return redact(messages)

    monkeypatch.setattr(template_secrets, "redact_messages", withdraw)
    await chat_runtime.send(api.ENGINE, sid, ident(), "hello", execution_admission=authority.guard)
    result = await settle(sid)
    assert not endpoint.requests
    assert result["turns"][0]["status"] == "failed"
    assert not authority.held


@pytest.mark.anyio
async def test_guard_released_after_body_and_rechecked_for_transport_fallback(endpoint, tmp_path):
    api.configure()
    authority = Authority()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(tmp_path), session_id=ident(), execution_admission=authority.guard
    )

    def refuse(_request):
        assert not authority.held
        authority.allowed = False
        return httpx.Response(400, json={"error": "thinking is unsupported"})

    endpoint.replies = [refuse, api.ok("must never arrive")]
    await chat_runtime.send(api.ENGINE, sid, ident(), "hello", execution_admission=authority.guard)
    view = await settle(sid)
    assert view["turns"][0]["status"] == "failed"
    assert len(endpoint.requests) == 1
    # One for the guarded creation, one for the first body handoff; the fallback never entered.
    assert authority.entries == authority.releases == 3


@pytest.mark.anyio
@pytest.mark.parametrize("effect", ["read", "stage"])
async def test_revocation_during_response_prevents_next_tool_effect(
    endpoint, project, monkeypatch, effect
):
    reads.configure("write")
    authority = Authority()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(project), session_id=ident(), execution_admission=authority.guard
    )
    target = chat_tools if effect == "read" else chat_edits
    function = "run" if effect == "read" else "stage"

    def forbidden(*args, **kwargs):
        pytest.fail("tool effect ran after authority was withdrawn")

    monkeypatch.setattr(target, function, forbidden)
    response = reads.calls(
        ("read_file", {"path": "README.md"})
        if effect == "read"
        else ("propose_edit", {"path": "README.md", "base_sha256": "a" * 64, "content": "changed"})
    )

    def withdraw(_request):
        authority.allowed = False
        return response

    endpoint.replies = [withdraw]
    await chat_runtime.send(api.ENGINE, sid, ident(), "hello", execution_admission=authority.guard)
    view = await settle(sid)
    assert view["turns"][0]["status"] == "failed"
    assert view["turns"][0]["reason"] == "original mission authority was withdrawn"
    assert view["turns"][0]["proposals"] == []
    assert len(endpoint.requests) == 1
    assert not authority.held


@pytest.mark.anyio
async def test_cancelled_tool_retains_authority_until_actual_reader_finishes(
    endpoint,
    project,
    monkeypatch,
):
    reads.configure("read")
    authority = Authority()
    started, release = threading.Event(), threading.Event()
    run = chat_tools.run

    def blocked(*args):
        started.set()
        assert release.wait(30)
        assert authority.held
        return run(*args)

    monkeypatch.setattr(chat_tools, "run", blocked)
    task = asyncio.create_task(
        chat_runtime._run_tool(
            str(project),
            "read_file",
            json.dumps({"path": "README.md"}),
            provider=chat_runtime._chat_provider(api.ENGINE),
            execution_admission=authority.guard,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 30)
        task.cancel()
        await asyncio.sleep(0)
        assert authority.held and not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not authority.held


@pytest.mark.anyio
async def test_guarded_turn_cannot_be_retried_or_replayed_through_ordinary_chat(endpoint, tmp_path):
    api.configure()
    authority = Authority()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(tmp_path), session_id=ident(), execution_admission=authority.guard
    )
    tid = ident()
    endpoint.replies = [httpx.Response(500)]
    await chat_runtime.send(api.ENGINE, sid, tid, "hello", execution_admission=authority.guard)
    await settle(sid)
    for call in (
        chat_runtime.retry(api.ENGINE, sid, tid),
        chat_runtime.send(api.ENGINE, sid, tid, "hello"),
        chat_runtime.retry(api.ENGINE, sid, tid, execution_admission=Authority("changed").guard),
    ):
        with pytest.raises(chat_runtime.ChatError, match="original execution authority"):
            await call
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_restarted_pending_turn_has_explicit_interruption_code(endpoint, tmp_path):
    api.configure()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    tid = ident()
    chat_store.append(
        tmp_path / "chat-store",
        sid,
        {"type": "user", "turn_id": tid, "text": "unknown"},
        {"type": "status", "turn_id": tid, "status": "pending"},
    )
    turn = (await settle(sid))["turns"][0]
    assert turn["status"] == "failed" and turn["code"] == "interrupted"
    assert not endpoint.requests


@pytest.mark.anyio
async def test_request_timeout_is_explicitly_uncertain(endpoint, tmp_path):
    api.configure()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    endpoint.replies = [httpx.ReadTimeout("the response was lost")]
    await chat_runtime.send(api.ENGINE, sid, ident(), "hello")
    turn = (await settle(sid))["turns"][0]
    assert turn["status"] == "failed" and turn["code"] == "uncertain"
    assert len(endpoint.requests) == 1


@pytest.mark.anyio
async def test_decision_replay_precedes_revision_and_changed_request_conflicts(endpoint, project):
    sid, tid, turn = await edits.propose(endpoint, project)
    proposal_id = turn["proposals"][0]["id"]
    revision = (await chat_runtime.get_session(api.ENGINE, sid))["revision"]
    did = ident()
    args = (api.ENGINE, sid, tid, proposal_id, "approve", "operator")
    first = await chat_runtime.decide(*args, decision_id=did, expected_revision=revision)
    await settle(sid)
    assert await chat_runtime.decide(*args, decision_id=did, expected_revision=revision) == first
    with pytest.raises(chat_runtime.ChatError, match="different request"):
        await chat_runtime.decide(
            api.ENGINE,
            sid,
            tid,
            proposal_id,
            "reject",
            "operator",
            decision_id=did,
        )
    assert (project / "README.md").read_text() == "clearer wording\n"


@pytest.mark.anyio
async def test_stale_decision_and_interrupted_claim_do_not_save(endpoint, project):
    sid, tid, turn = await edits.propose(endpoint, project)
    pid, did = turn["proposals"][0]["id"], ident()
    with pytest.raises(chat_runtime.ChatError, match="conversation changed"):
        await chat_runtime.decide(
            api.ENGINE,
            sid,
            tid,
            pid,
            "approve",
            "operator",
            decision_id=did,
            expected_revision=0,
        )
    root = chat_runtime._root(chat_runtime._chat_provider(api.ENGINE))
    chat_store.append(
        root, sid, chat_runtime._decision_request(did, tid, pid, "approve", "operator", None)
    )
    with pytest.raises(chat_runtime.ChatError, match="not replayed"):
        await chat_runtime.decide(api.ENGINE, sid, tid, pid, "approve", "operator", decision_id=did)
    with pytest.raises(chat_runtime.ChatError, match="already has a decision"):
        await chat_runtime.decide(api.ENGINE, sid, tid, pid, "approve", "operator")
    recovered = await settle(sid)
    assert recovered["turns"][0]["code"] == "interrupted"
    assert recovered["turns"][0]["proposals"][0]["status"] == "interrupted"
    replay = await chat_runtime.decide(
        api.ENGINE, sid, tid, pid, "approve", "operator", decision_id=did, expected_revision=0
    )
    assert replay["proposal"]["status"] == "interrupted"
    assert (project / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
async def test_guarded_proposal_requires_original_authority_for_save_and_resume(
    endpoint,
    project,
    monkeypatch,
):
    reads.configure("write")
    authority = Authority()
    send = chat_runtime.send

    async def guarded_send(*args, **kwargs):
        return await send(*args, **kwargs, execution_admission=authority.guard)

    monkeypatch.setattr(chat_runtime, "send", guarded_send)
    create = chat_runtime.new_session

    async def guarded_create(*args, **kwargs):  # guarded work lives in a guarded conversation
        return await create(*args, **kwargs, execution_admission=authority.guard)

    monkeypatch.setattr(chat_runtime, "new_session", guarded_create)
    sid, tid, turn = await edits.propose(endpoint, project)
    args = (api.ENGINE, sid, tid, turn["proposals"][0]["id"], "approve", "operator")
    for guard in (None, Authority("replacement-grant").guard):
        with pytest.raises(chat_runtime.ChatError, match="original execution authority"):
            await chat_runtime.decide(*args, decision_id=ident(), execution_admission=guard)
    assert (project / "README.md").read_text() == "hello\n"
    save = fileedit.save

    def checked_save(*args, **kwargs):
        assert authority.held
        return save(*args, **kwargs)

    monkeypatch.setattr(fileedit, "save", checked_save)
    did = ident()
    result = await chat_runtime.decide(*args, decision_id=did, execution_admission=authority.guard)
    view = await settle(sid)
    assert result["proposal"]["status"] == "approved"
    assert view["turns"][0]["status"] == "done"
    assert view["turns"][0]["execution_binding"] == authority.guard.binding
    assert (project / "README.md").read_text() == "clearer wording\n"
    assert not authority.held and authority.entries == authority.releases


@pytest.mark.anyio
async def test_a_busy_launch_lock_is_a_retryable_tool_refusal_not_a_failed_turn(endpoint, project):
    """Review of #1275: a launch holding the lock used to raise `Refused` out of the tool and
    fail the whole turn. The model now gets a bounded, retryable refusal for that one call."""
    from agent_sessions.plugins import storage

    api.configure()
    held, release = threading.Event(), threading.Event()

    def hold():
        with storage.locked(admission.LOCK):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert await asyncio.to_thread(held.wait, 30)
        result = await asyncio.wait_for(
            chat_runtime._run_tool(
                str(project),
                "read_file",
                json.dumps({"path": "README.md"}),
                provider=chat_runtime._chat_provider(api.ENGINE),
            ),
            10,
        )
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 30)
    assert result.summary["outcome"] == "refused"
    assert "try again" in result.content


@pytest.mark.anyio
async def test_a_provider_revoked_while_the_read_is_queued_never_reads(
    endpoint, project, monkeypatch
):
    """Hermes on #1275: admission is rechecked in the pool worker, right before the read."""
    from agent_sessions.engines import registry
    from agent_sessions.routes import files as file_routes

    api.configure()
    provider = chat_runtime._chat_provider(api.ENGINE)
    real_run_bounded = file_routes.run_bounded
    reads = []

    async def revoke_while_queued(key, fn, *args):
        monkeypatch.setattr(registry, "admits", lambda prov: False)  # removed while queued
        return await real_run_bounded(key, fn, *args)

    monkeypatch.setattr(file_routes, "run_bounded", revoke_while_queued)
    monkeypatch.setattr(chat_tools, "run", lambda *a: reads.append(a))
    result = await chat_runtime._run_tool(
        str(project), "read_file", json.dumps({"path": "README.md"}), provider=provider
    )
    assert result.summary["outcome"] == "refused" and not reads


@pytest.mark.anyio
async def test_a_refused_plain_create_leaves_no_proposal_directory(endpoint, tmp_path):
    api.configure()
    root = chat_runtime._root(chat_runtime._chat_provider(api.ENGINE))
    with pytest.raises(chat_runtime.ChatError) as error:
        await chat_runtime.new_session(api.ENGINE, str(tmp_path / "missing"))
    assert error.value.status == 422
    assert not (root / "proposals").exists() or not any((root / "proposals").iterdir())


@pytest.mark.anyio
async def test_a_guarded_conversation_refuses_a_fresh_unguarded_turn(endpoint, tmp_path):
    """Hermes on #1275: the binding was checked only for an EXISTING turn id, so a new turn
    without the original authority could continue a guarded conversation."""
    api.configure()
    authority = Authority()
    sid = await chat_runtime.new_session(
        api.ENGINE, str(tmp_path), session_id=ident(), execution_admission=authority.guard
    )
    with pytest.raises(chat_runtime.ChatError, match="original execution authority") as error:
        await chat_runtime.send(api.ENGINE, sid, ident(), "continue without authority")
    assert error.value.status == 409
    other = Authority("another-grant")
    with pytest.raises(chat_runtime.ChatError, match="original execution authority"):
        await chat_runtime.send(
            api.ENGINE, sid, ident(), "another grant", execution_admission=other.guard
        )
    assert not endpoint.requests
    await chat_runtime.send(api.ENGINE, sid, ident(), "hello", execution_admission=authority.guard)
    await settle(sid)
    assert len(endpoint.requests) == 1


def test_the_ordinary_chat_route_cannot_continue_a_guarded_conversation(client, endpoint, tmp_path):
    api.configure()
    authority = Authority()

    async def create():
        return await chat_runtime.new_session(
            api.ENGINE, str(tmp_path), session_id=ident(), execution_admission=authority.guard
        )

    sid = client.portal.call(create)
    r = client.post(
        f"/api/chat/{api.ENGINE}:{sid}/messages", json={"turn_id": ident(), "text": "hijack"}
    )
    assert r.status_code == 409 and "original execution authority" in r.json()["detail"]
    assert not endpoint.requests


@pytest.mark.anyio
async def test_a_queued_read_holds_no_caller_authority_and_revocation_refuses_it(
    endpoint, project, monkeypatch
):
    """Hermes on #1275: caller authority was acquired BEFORE the bounded queue, so a queued
    read held the authorization fence (blocking revocation) and could read after it."""
    from agent_sessions.routes import files as file_routes

    api.configure()
    authority = Authority()
    real_run_bounded = file_routes.run_bounded
    reads, observed = [], []

    async def queued(key, fn, *args):
        observed.append(authority.held)  # nothing held while the call waits for a slot
        authority.allowed = False  # revoked while queued
        return await real_run_bounded(key, fn, *args)

    monkeypatch.setattr(file_routes, "run_bounded", queued)
    monkeypatch.setattr(chat_tools, "run", lambda *a: reads.append(a))
    result = await chat_runtime._run_tool(
        str(project),
        "read_file",
        json.dumps({"path": "README.md"}),
        provider=chat_runtime._chat_provider(api.ENGINE),
        execution_admission=authority.guard,
    )
    assert observed == [False]
    assert result.summary["outcome"] == "refused" and not reads
    assert not authority.held


@pytest.mark.anyio
async def test_guarded_work_cannot_enter_an_ordinary_conversation(endpoint, tmp_path):
    """Hermes on #1275: an ordinary conversation took a guarded turn, and a later unguarded
    turn then resent that guarded exchange as history after its authority was revoked."""
    api.configure()
    authority = Authority()
    sid = await chat_runtime.new_session(api.ENGINE, str(tmp_path), session_id=ident())
    with pytest.raises(chat_runtime.ChatError, match="original execution authority") as error:
        await chat_runtime.send(
            api.ENGINE, sid, ident(), "guarded work", execution_admission=authority.guard
        )
    assert error.value.status == 409 and not endpoint.requests and not authority.entries
    await chat_runtime.send(api.ENGINE, sid, ident(), "ordinary")
    await settle(sid)
    assert len(endpoint.requests) == 1
    assert b"guarded work" not in endpoint.requests[0].content
