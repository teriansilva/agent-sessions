"""Per-proposal consent, crash recovery and the real lease-bound save (#1230)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import test_chat_agent as chat_api
import test_chat_tools as reads
from agent_sessions import chat_config, chat_edits, chat_runtime, chat_tools, fileedit, prefs

project = reads.project
endpoint = reads.endpoint
anyio_backend = reads.anyio_backend
client = chat_api.client


@pytest.mark.parametrize("status", ["awaiting_approval", "deciding"])
def test_first_checkpoint_syncs_directory_ancestry_before_acknowledgement(
    tmp_path, monkeypatch, status
):
    root = tmp_path / "first" / "chat-store"
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    synced = []
    real_sync = os.fsync

    def sync(fd):
        synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        real_sync(fd)

    monkeypatch.setattr(chat_edits.os, "fsync", sync)
    path = chat_edits._path(root, sid, pid)
    chat_edits._write(path, {"id": pid, "status": status})
    for parent in (tmp_path, root.parent, root, root / "proposals", path.parent):
        assert parent in synced
    # The newly reachable ancestry precedes the checkpoint's file/directory durability.
    assert synced.index(root.parent) < synced.index(root / "proposals") < len(synced) - 1
    assert chat_edits._read(path)["status"] == status


def test_failed_directory_sync_retries_existing_ancestry_before_a_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "chat-store"
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    real_sync = os.fsync
    failed, retried = False, False

    def sync(fd):
        nonlocal failed, retried
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == root:
            if not failed:
                failed = True
                raise OSError("injected proposal ancestry sync failure")
            retried = True
        real_sync(fd)

    monkeypatch.setattr(chat_edits.os, "fsync", sync)
    with pytest.raises(OSError, match="ancestry sync"):
        chat_edits._path(root, sid, pid)
    assert not list(root.rglob("*.json"))
    path = chat_edits._path(root, sid, pid)
    assert retried
    chat_edits._write(path, {"id": pid, "status": "awaiting_approval"})
    assert chat_edits._read(path)["status"] == "awaiting_approval"


@pytest.fixture(autouse=True)
def lease_signal(monkeypatch, tmp_path):
    # These cases test approval/recovery, not wall-clock expiry. Shared-runner fsync contention
    # exceeded the production 10 s ceiling in the route-consent case (#853 CI). Keep that real
    # refusal in production; test_file_edit pins expiry separately with a deterministic clock.
    monkeypatch.setattr(fileedit, "LEASE_BUDGET_S", 120.0)
    monkeypatch.setenv("AGENT_SESSIONS_EDIT_RECOVERY", str(tmp_path / "recovery"))
    old = signal.getsignal(signal.SIGIO)
    assert fileedit.install_lease_signal_handler()
    yield
    fileedit._HOOK = None
    signal.signal(signal.SIGIO, old)


async def propose(endpoint, project, *, partial=False, read_first=True):
    reads.configure("write")
    content = (project / "README.md").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    endpoint.replies = (
        [reads.calls(("read_file", {"path": "README.md", **({"max_lines": 1} if partial else {})}))]
        if read_first
        else []
    ) + [
        reads.calls(
            (
                "propose_edit",
                {
                    "path": "README.md",
                    "content": "clearer wording\n",
                    "base_sha256": digest,
                },
            )
        ),
        reads.answer("finished"),
    ]
    sid, tid = await reads.turn(project)
    return sid, tid, await reads.view(sid)


async def decide(sid, tid, view, action="approve"):
    result = await chat_runtime.decide(
        reads.ENGINE, sid, tid, view["proposals"][0]["id"], action, "operator"
    )
    task = chat_runtime.running_task(reads.ENGINE, sid)
    if task:
        await task
    return result


@pytest.mark.anyio
async def test_nothing_written_until_approval_then_real_save_and_audit(endpoint, project, tmp_path):
    sid, tid, view = await propose(endpoint, project)
    assert view["status"] == "awaiting_approval"
    assert (project / "README.md").read_text() == "hello\n"
    assert view["proposals"][0]["can_approve"] is True
    assert "+clearer wording" in view["proposals"][0]["diff"]
    assert "clearer wording" not in reads.store_text(tmp_path)
    result = await decide(sid, tid, view)
    assert result["proposal"]["status"] == "approved"
    assert result["proposal"]["decided_by"] == "operator"
    assert (project / "README.md").read_text() == "clearer wording\n"
    assert (await reads.view(sid))["status"] == "done"
    assert any(
        p.read_bytes() == b"hello\n" for p in (tmp_path / "recovery").rglob("*") if p.is_file()
    )
    sidecar = next((tmp_path / "chat-store" / "proposals" / sid).glob("*.json"))
    rec = json.loads(sidecar.read_text())
    assert not {"base", "content", "checkpoint"} & rec.keys()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    before = len(endpoint.bodies())
    await decide(sid, tid, view)
    assert len(endpoint.bodies()) == before


@pytest.mark.anyio
async def test_rejection_never_calls_the_save(endpoint, project, monkeypatch):
    sid, tid, view = await propose(endpoint, project)
    monkeypatch.setattr(fileedit, "save", lambda *a, **k: pytest.fail("rejection wrote a file"))
    assert (await decide(sid, tid, view, "reject"))["proposal"]["status"] == "rejected"
    assert (project / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["disable", "replace"])
@pytest.mark.parametrize("boundary", ["session-lock", "proof", "save", "check", "install"])
async def test_parked_approval_cannot_outlive_its_provider(
    endpoint, project, tmp_path, monkeypatch, change, boundary
):
    from agent_sessions import engines
    from agent_sessions.engines import registry
    from agent_sessions.plugins import manager, storage

    sid, tid, view = await propose(endpoint, project)
    original = engines.get(reads.ENGINE)
    plugins_dir = os.environ["AGENT_SESSIONS_PLUGINS_DIR"]
    entered, release = threading.Event(), threading.Event()
    lock = chat_runtime._lock(chat_runtime._key(reads.ENGINE, sid))
    if boundary == "session-lock":
        await lock.acquire()
        provider = chat_runtime._chat_provider

        def captured(engine):
            value = provider(engine)
            entered.set()
            return value

        monkeypatch.setattr(chat_runtime, "_chat_provider", captured)
    else:

        def park():
            entered.set()
            assert release.wait(30), "approval test did not release its worker"

        if boundary in ("save", "proof"):
            owner, name = (fileedit, "save") if boundary == "save" else (chat_edits, "_file")
            save = getattr(owner, name)
            calls = 0

            def paused(*args, **kwargs):
                nonlocal calls
                calls += 1
                # The first proof belongs to the proposal view; park the mutation worker's
                # own proof, after the decision fence's live-provider check has passed.
                if boundary == "save" or calls == 2:
                    park()
                return save(*args, **kwargs)

            monkeypatch.setattr(owner, name, paused)
        else:
            fileedit._HOOK = lambda step: park() if step == boundary else None

    # Keep the request's original roster, just as the HTTP middleware does. Revocation must
    # consult the live roster even when this context still resolves the captured provider.
    fenced = change == "disable" and boundary in ("save", "check", "install")
    with registry.snapshot_scope():
        task = asyncio.create_task(decide(sid, tid, view))
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            if change == "disable":
                if fenced:
                    with pytest.raises(storage.StateError, match="busy"):
                        await asyncio.to_thread(manager.deactivate, str(uuid.uuid4()), reads.ENGINE)
                else:
                    await asyncio.to_thread(manager.deactivate, str(uuid.uuid4()), reads.ENGINE)
            else:
                # A real roster publication with an identical manifest at a different root:
                # the same binding distinction used for managed generation replacement.
                monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "replacement"))
                await asyncio.to_thread(registry.reload)
                assert registry.live_provider(reads.ENGINE).manifest == original.manifest
            assert registry.admits(original) is fenced
        finally:
            release.set()
            if boundary == "session-lock":
                lock.release()
        try:
            try:
                result = await task
            except chat_runtime.ChatError as exc:
                assert exc.status == 409 and boundary == "session-lock"
            else:
                assert result["proposal"]["status"] == ("approved" if fenced else "refused")
            assert (project / "README.md").read_text() == (
                "clearer wording\n" if fenced else "hello\n"
            )
            if fenced:
                # Withdrawal may commit once the already-admitted save has settled.
                await asyncio.to_thread(manager.deactivate, str(uuid.uuid4()), reads.ENGINE)
                assert not registry.admits(original)
        finally:
            if change == "replace":
                monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", plugins_dir)
                registry.reload()


@pytest.mark.anyio
async def test_second_worker_never_interrupts_decision_save_or_resumed_turn(endpoint, project):
    """A real second process owns the decision across all three formerly unguarded windows."""
    sid, tid, view = await propose(endpoint, project)
    child_code = r"""
import asyncio, json, sys, time
from agent_sessions import chat_runtime, chat_store, fileedit
engine, sid, tid, pid = json.loads(sys.stdin.readline())
fileedit.install_lease_signal_handler()
save, append = fileedit.save, chat_store.append
def wait_at(label):
    print(label, flush=True)
    if sys.stdin.readline().strip() != "continue":
        raise RuntimeError("test control ended")
def paused_save(*args, **kwargs):
    wait_at("deciding")
    return save(*args, **kwargs)
def paused_append(*args, **kwargs):
    if any(isinstance(r, dict) and r.get("resume") for r in args):
        wait_at("approved-before-pending")
    return append(*args, **kwargs)
async def resumed(*args, **kwargs):
    await asyncio.to_thread(wait_at, "resumed-pending")
    return [{"type":"assistant", "turn_id":tid, "text":"continued once", "ts":time.time()},
            chat_runtime._status(tid, "done")]
fileedit.save, chat_store.append, chat_runtime._run_once = paused_save, paused_append, resumed
async def main():
    await chat_runtime.decide(engine, sid, tid, pid, "approve", "operator")
    task = chat_runtime.running_task(engine, sid)
    if task:
        await task
asyncio.run(main())
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        proc.stdin.write(json.dumps([reads.ENGINE, sid, tid, view["proposals"][0]["id"]]) + "\n")
        proc.stdin.flush()
        for boundary in ("deciding", "approved-before-pending", "resumed-pending"):
            observed = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 10)
            assert observed.strip() == boundary
            current = await asyncio.wait_for(reads.view(sid), 1)
            assert current["status"] in ("awaiting_approval", "pending"), current
            with pytest.raises(chat_runtime.ChatError):
                await chat_runtime.send(reads.ENGINE, sid, str(uuid.uuid4()), "another turn")
            proc.stdin.write("continue\n")
            proc.stdin.flush()
        assert await asyncio.to_thread(proc.wait, timeout=10) == 0, proc.stderr.read()
        final = await reads.view(sid)
        assert final["status"] == "done" and final["reply"] == "continued once"
        assert final["proposals"][0]["status"] == "approved"
        assert (project / "README.md").read_text() == "clearer wording\n"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


@pytest.mark.anyio
async def test_cancelled_decision_keeps_cross_process_fence_until_save_worker_exits(
    endpoint, project, tmp_path, monkeypatch
):
    sid, tid, view = await propose(endpoint, project)
    entered, finish = threading.Event(), threading.Event()
    save = fileedit.save

    def paused(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return save(*args, **kwargs)

    monkeypatch.setattr(fileedit, "save", paused)
    task = asyncio.create_task(
        chat_runtime.decide(
            reads.ENGINE, sid, tid, view["proposals"][0]["id"], "approve", "operator"
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert chat_edits.turn_fence(tmp_path / "chat-store", sid) is None
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    current = await reads.view(sid)
    assert current["status"] == "failed"  # save happened; cancelled continuation never replayed
    assert current["proposals"][0]["status"] == "approved"
    assert (project / "README.md").read_text() == "clearer wording\n"


@pytest.mark.anyio
async def test_turn_cancelled_before_running_releases_its_fence(endpoint, project, tmp_path):
    reads.configure("write")
    sid = await chat_runtime.new_session(reads.ENGINE, str(project))
    await chat_runtime.send(reads.ENGINE, sid, str(uuid.uuid4()), "hello")
    task = chat_runtime.running_task(reads.ENGINE, sid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    current = await reads.view(sid)
    assert current["status"] == "failed"
    fence = chat_edits.turn_fence(tmp_path / "chat-store", sid)
    assert fence is not None
    fence.release()


@pytest.mark.anyio
async def test_awaiting_survives_restart_and_holds_single_flight(endpoint, project):
    sid, tid, view = await propose(endpoint, project)
    chat_runtime._LOCKS.clear()
    chat_runtime._TASKS.clear()
    assert (await reads.view(sid))["status"] == "awaiting_approval"
    with pytest.raises(chat_runtime.ChatError, match="awaiting"):
        await chat_runtime.send(reads.ENGINE, sid, str(uuid.uuid4()), "another message")
    await decide(sid, tid, view)
    assert (project / "README.md").read_text() == "clearer wording\n"


def test_decision_route_requires_session_csrf_origin_and_exact_body(client, endpoint, project):
    sid, tid, view = client.portal.call(propose, endpoint, project)
    pid = view["proposals"][0]["id"]
    url = f"/api/chat/{reads.ENGINE}:{sid}/turns/{tid}/proposals/{pid}/decide"
    anonymous = TestClient(client.app, base_url="https://testserver")
    assert anonymous.post(url, json={"decision": "approve"}).status_code == 401
    bare = TestClient(client.app, base_url="https://testserver", cookies=client.cookies)
    assert bare.post(url, json={"decision": "approve"}).status_code == 403
    assert (
        client.post(
            url, json={"decision": "approve"}, headers={"Origin": "https://elsewhere.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(url, json={"decision": "approve", "content": "replacement"}).status_code == 422
    )
    assert (project / "README.md").read_text() == "hello\n"
    result = client.post(url, json={"decision": "approve"})
    assert result.status_code == 200
    assert result.json()["proposal"]["status"] == "approved", result.json()
    assert "clearer wording" not in result.text
    client.portal.call(chat_api.settle, sid)


@pytest.mark.anyio
async def test_durable_outcome_without_transcript_completion_never_resumes(
    endpoint, project, tmp_path
):
    sid, tid, view = await propose(endpoint, project)
    rec, _checkpoint = chat_edits.decide(
        tmp_path / "chat-store",
        sid,
        tid,
        view["proposals"][0]["id"],
        reads.ENGINE,
        "approve",
        "operator",
        provider=chat_runtime._chat_provider(reads.ENGINE),
    )
    assert rec["status"] == "approved"
    chat_runtime._TASKS.clear()
    current = await reads.view(sid)
    assert current["status"] == "failed"
    assert current["proposals"][0]["status"] == "approved"
    before = len(endpoint.bodies())
    await decide(sid, tid, view)
    assert len(endpoint.bodies()) == before
    assert (project / "README.md").read_text() == "clearer wording\n"


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["file", "policy", "endpoint", "excluded", "symlink"])
async def test_changes_between_proposal_and_decision_refuse(endpoint, project, change):
    sid, tid, view = await propose(endpoint, project)
    target = project / "README.md"
    if change == "file":
        target.write_text("newer operator work\n")
    elif change == "policy":
        chat_config.set_config(reads.ENGINE, {"tools": "read"})
    elif change == "endpoint":
        chat_config.set_config(reads.ENGINE, {"model": "other-model"})
    elif change == "excluded":
        prefs.set_folder_exclusions([str(project)])
    else:
        target.rename(project / "original.md")
        target.symlink_to(project.parent / "other" / "secret.txt")
    expected = target.read_bytes()
    current = await reads.view(sid)
    assert not current["proposals"][0]["can_approve"]
    assert (await decide(sid, tid, view))["proposal"]["status"] == "refused"
    assert target.read_bytes() == expected


@pytest.mark.anyio
@pytest.mark.parametrize("read_first,partial", [(False, False), (True, True)])
async def test_no_proposal_without_complete_read(endpoint, project, read_first, partial):
    _sid, _tid, view = await propose(endpoint, project, read_first=read_first, partial=partial)
    assert view["status"] == "done"
    assert view["proposals"] == []
    assert view["tools"][-1]["outcome"] == "refused"
    assert (project / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
async def test_interrupted_decision_never_replays(endpoint, project, tmp_path, monkeypatch):
    sid, tid, view = await propose(endpoint, project)
    path = next((tmp_path / "chat-store" / "proposals" / sid).glob("*.json"))
    rec = json.loads(path.read_text())
    rec["status"] = "deciding"
    chat_edits._write(path, rec)
    monkeypatch.setattr(fileedit, "save", lambda *a, **k: pytest.fail("replayed interrupted save"))
    assert (await decide(sid, tid, view))["proposal"]["status"] == "interrupted"
    assert (await reads.view(sid))["status"] == "failed"
    assert "content" not in json.loads(path.read_text())


def test_digest_only_for_complete_strict_reads(project):
    path = project / "README.md"
    for data in [b"x" * (chat_tools.READ_MAX_BYTES + 1), b"bad\xff\n"]:
        path.write_bytes(data)
        body, _summary = reads.run(project, "read_file", path="README.md")
        assert "base_sha256" not in body


@pytest.mark.anyio
async def test_proposal_binds_the_exact_model_snapshot_not_a_later_config(
    endpoint, project, monkeypatch
):
    snapshot = chat_config.snapshot

    def changed_after_snapshot(*a, **kw):
        cfg = snapshot(*a, **kw)
        chat_config.set_config(reads.ENGINE, {"model": cfg["model"] + "-changed"})
        return cfg

    monkeypatch.setattr(chat_config, "snapshot", changed_after_snapshot)
    _sid, _tid, view = await propose(endpoint, project)
    assert view["proposals"] == []
    assert view["tools"][-1]["outcome"] == "refused"
    assert (project / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
async def test_policy_rechecked_inside_lease_before_install(endpoint, project):
    sid, tid, view = await propose(endpoint, project)

    def revoke(step):
        if step == "check":
            chat_config.set_config(reads.ENGINE, {"tools": "none"})

    fileedit._HOOK = revoke
    assert (await decide(sid, tid, view))["proposal"]["status"] == "refused"
    assert (project / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
async def test_parent_moved_out_of_conversation_during_save_cannot_receive_new_content(
    endpoint, project
):
    sid, tid, view = await propose(endpoint, project)
    relocated = project.parent / "relocated"

    def move(step):
        if step == "check":
            project.rename(relocated)

    fileedit._HOOK = move
    assert (await decide(sid, tid, view))["proposal"]["status"] == "refused"
    assert (relocated / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
@pytest.mark.parametrize("when", ["last_lease", "linked"])
async def test_parent_move_after_final_guard_withdraws_unsettled_install(
    endpoint, project, monkeypatch, when
):
    sid, tid, view = await propose(endpoint, project)
    relocated = project.parent / "relocated"
    intact = fileedit._lease_intact
    calls = 0

    def lease(fd):
        nonlocal calls
        calls += 1
        if when == "last_lease" and calls == 2:
            project.rename(relocated)
        return intact(fd)

    def move(step):
        if when == "linked" and step == "install":
            project.rename(relocated)

    monkeypatch.setattr(fileedit, "_lease_intact", lease)
    fileedit._HOOK = move
    assert (await decide(sid, tid, view))["proposal"]["status"] == "refused"
    assert (relocated / "README.md").read_text() == "hello\n"


@pytest.mark.anyio
async def test_concurrent_opposing_decisions_have_one_outcome(endpoint, project, monkeypatch):
    sid, tid, view = await propose(endpoint, project)
    save = fileedit.save
    writes = []

    def counted(*a, **kw):
        writes.append(1)
        return save(*a, **kw)

    monkeypatch.setattr(fileedit, "save", counted)
    results = await asyncio.gather(decide(sid, tid, view), decide(sid, tid, view, "reject"))
    assert results[0]["proposal"]["status"] == results[1]["proposal"]["status"]
    assert len(writes) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["before_save", "after_save"])
async def test_restart_at_ambiguous_write_boundary_never_replays(
    endpoint,
    project,
    tmp_path,
    monkeypatch,
    boundary,
):
    sid, tid, view = await propose(endpoint, project)
    save = fileedit.save

    class Crash(BaseException):
        pass

    def crashed(*a, **kw):
        if boundary == "after_save":
            save(*a, **kw)
        raise Crash

    monkeypatch.setattr(fileedit, "save", crashed)
    with pytest.raises(Crash):
        await decide(sid, tid, view)
    chat_runtime._LOCKS.clear()
    current = await reads.view(sid)
    assert current["status"] == "failed"
    assert current["proposals"][0]["status"] == "interrupted"
    await decide(sid, tid, view)  # returns the recorded uncertain outcome, never calls save
    expected = "clearer wording\n" if boundary == "after_save" else "hello\n"
    assert (project / "README.md").read_text() == expected
    sidecar = next((tmp_path / "chat-store" / "proposals" / sid).glob("*.json"))
    assert "content" not in json.loads(sidecar.read_text())


def test_complete_digest_is_of_original_bom_and_crlf_bytes(project):
    raw = b"\xef\xbb\xbfhello\r\nworld\r\n"
    (project / "README.md").write_bytes(raw)
    body, _ = reads.run(project, "read_file", path="README.md")
    assert body["base_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.anyio
async def test_save_preserves_bom_and_crlf(endpoint, project):
    (project / "README.md").write_bytes(b"\xef\xbb\xbfhello\r\n")
    sid, tid, view = await propose(endpoint, project)
    await decide(sid, tid, view)
    assert (project / "README.md").read_bytes() == b"\xef\xbb\xbfclearer wording\r\n"


@pytest.mark.anyio
async def test_diff_keeps_unterminated_original_line_separate(endpoint, project):
    (project / "README.md").write_text("hello")
    _sid, _tid, view = await propose(endpoint, project)
    assert (
        "-hello\n\\ No newline at end of file\n+clearer wording\n" in view["proposals"][0]["diff"]
    )


@pytest.mark.anyio
async def test_surrogate_tool_echo_does_not_crash_next_request(endpoint, project):
    reads.configure("read")
    response = reads.calls(("read_file", {"path": "README.md"}))
    body = response.json()
    body["choices"][0]["message"]["content"] = "bad\ud800"
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":"bad\ud800"}'
    import httpx

    endpoint.replies = [
        httpx.Response(200, content=json.dumps(body).encode()),
        reads.answer("safe answer"),
    ]
    sid, _ = await reads.turn(project)
    assert (await reads.view(sid))["status"] == "done"


@pytest.mark.anyio
async def test_write_tools_fit_small_context_with_history_at_the_admitted_boundary(
    project, endpoint
):
    from agent_sessions import chat_store

    reads.configure("write")
    chat_config.set_config(reads.ENGINE, {"context_window": 2048, "max_output_tokens": 64})
    sid = await chat_runtime.new_session(reads.ENGINE, str(project))
    # Use the runtime provider's store, as the route does; the fixture keeps it off the real HOME.
    root = chat_runtime._root(chat_runtime._chat_provider(reads.ENGINE))
    old = str(uuid.uuid4())
    chat_store.append(
        root,
        sid,
        {"type": "user", "turn_id": old, "text": "old " * 300, "ts": 1},
        {"type": "assistant", "turn_id": old, "text": "reply " * 300, "ts": 2},
        chat_runtime._status(old, "done"),
    )
    cfg = chat_config.snapshot(reads.ENGINE)
    budget = chat_runtime._budget(cfg, chat_store.read(root, sid))
    assert budget >= chat_config.BUDGET_MIN_TOKENS
    turn_id = str(uuid.uuid4())
    await chat_runtime.send(reads.ENGINE, sid, turn_id, "x" * (budget * 4))
    await chat_api.settle(sid)
    [body] = endpoint.bodies()
    assert {spec["function"]["name"] for spec in body["tools"]} == {
        "read_file",
        "list_files",
        "propose_edit",
    }
    estimate = chat_runtime._tokens(
        json.dumps({"messages": body["messages"], "tools": body["tools"]})
    )
    assert estimate + body["max_tokens"] <= cfg["context_window"]
    assert len(body["messages"]) == 2, "old whole exchanges should be trimmed"
    with pytest.raises(chat_runtime.ChatError) as error:
        await chat_runtime.send(reads.ENGINE, sid, str(uuid.uuid4()), "x" * ((budget + 1) * 4))
    assert error.value.status == 413
    assert len(endpoint.bodies()) == 1


def test_configuration_reserves_write_schemas_before_accepting_small_context():
    reads.configure("write")
    with pytest.raises(chat_config.ChatConfigError, match="system prompt and tools"):
        chat_config.set_config(reads.ENGINE, {"context_window": 2048, "max_output_tokens": 384})


def test_turn_fence_and_checkpoint_under_searchable_unreadable_ancestor(tmp_path, monkeypatch):
    ancestor = tmp_path / "search-only"
    root = ancestor / "owned" / "chat-store"
    root.mkdir(parents=True)
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    synced = []
    real_sync = os.fsync

    def sync(fd):
        synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        real_sync(fd)

    monkeypatch.setattr(chat_edits.os, "fsync", sync)
    ancestor.chmod(0o111)
    try:
        fence = chat_edits.turn_fence(root, sid)
        assert fence is not None
        fence.release()
        path = chat_edits._path(root, sid, pid)
        chat_edits._write(path, {"id": pid, "status": "awaiting_approval"})
        assert chat_edits._read(path)["status"] == "awaiting_approval"
        assert root in synced and root / "proposals" in synced and path.parent in synced
        assert ancestor not in synced
        # Another worker/session must retain exactly this pre-existing boundary, too.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys, uuid; "
                "from agent_sessions import chat_edits; "
                "fence = chat_edits.turn_fence(Path(sys.argv[1]), str(uuid.uuid4())); "
                "assert fence is not None; fence.release()",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    finally:
        ancestor.chmod(0o700)


@pytest.mark.parametrize("boundary", ["root", "ancestor"])
@pytest.mark.parametrize("operation", ["fence", "checkpoint"])
def test_first_use_refuses_unsyncable_writable_ancestry(tmp_path, boundary, operation):
    from agent_sessions.fsbrowse import FsError

    ancestor = tmp_path / "writable-search-only"
    ancestor.mkdir()
    root = ancestor / "chat-store" if boundary == "ancestor" else ancestor
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())

    def attempt():
        if operation == "fence":
            fence = chat_edits.turn_fence(root, sid)
            assert fence is not None
            fence.release()
        else:
            path = chat_edits._path(root, sid, pid)
            chat_edits._write(path, {"id": pid, "status": "awaiting_approval"})

    ancestor.chmod(0o300)
    try:
        # First use modifies the unreadable directory; merely existing on retry does not
        # establish that its child entry reached disk. Neither operation may acknowledge it.
        for _ in range(2):
            with pytest.raises(FsError, match="proposal ancestry cannot be synced"):
                attempt()
            assert not (root / "proposals" / sid / ".turn.lock").exists()
            assert not (root / "proposals" / sid / f"{pid}.json").exists()
    finally:
        ancestor.chmod(0o700)
    # Restoring directory read access lets the ordinary sync barrier recover without cleanup.
    attempt()


@pytest.mark.parametrize("operation", ["fence", "checkpoint"])
@pytest.mark.parametrize("restart", [False, True])
def test_failed_sync_then_search_only_ancestor_still_refuses(
    tmp_path, monkeypatch, operation, restart
):
    from agent_sessions.fsbrowse import FsError

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    root = ancestor / "new" / "chat-store"
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    real_sync = os.fsync

    def sync(fd):
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == ancestor:
            raise OSError("injected ancestor sync failure")
        real_sync(fd)

    def attempt():
        if operation == "fence":
            fence = chat_edits.turn_fence(root, sid)
            assert fence is not None
            fence.release()
        else:
            path = chat_edits._path(root, sid, pid)
            chat_edits._write(path, {"id": pid, "status": "awaiting_approval"})

    monkeypatch.setattr(chat_edits.os, "fsync", sync)
    with pytest.raises(OSError, match="ancestor sync failure"):
        attempt()
    monkeypatch.setattr(chat_edits.os, "fsync", real_sync)
    ancestor.chmod(0o111)
    try:
        if restart:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; "
                    "from agent_sessions import chat_edits; "
                    "from agent_sessions.fsbrowse import FsError\n"
                    "root, sid, pid, operation = sys.argv[1:]\n"
                    "try:\n"
                    " if operation == 'fence':\n"
                    "  fence = chat_edits.turn_fence(Path(root), sid); fence.release()\n"
                    " else:\n"
                    "  path = chat_edits._path(Path(root), sid, pid)\n"
                    "  chat_edits._write(path, {'id': pid, 'status': 'awaiting_approval'})\n"
                    "except FsError as exc:\n"
                    " assert exc.status == 503 and 'ancestry' in str(exc)\n"
                    "else: raise AssertionError('acknowledged an unsynced ancestor')\n",
                    str(root),
                    sid,
                    pid,
                    operation,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
        else:
            with pytest.raises(FsError, match="proposal ancestry cannot be synced"):
                attempt()
        assert not (root / "proposals" / sid / ".turn.lock").exists()
        assert not (root / "proposals" / sid / f"{pid}.json").exists()
    finally:
        ancestor.chmod(0o700)
    attempt()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_inode", "symlink"])
def test_search_only_boundary_requires_its_private_record(tmp_path, damage):
    from agent_sessions.fsbrowse import FsError

    ancestor = tmp_path / "search-only"
    root = ancestor / "owned" / "chat-store"
    root.mkdir(parents=True)
    sid = str(uuid.uuid4())
    ancestor.chmod(0o111)
    try:
        fence = chat_edits.turn_fence(root, sid)
        fence.release()
        marker = root / "proposals" / ".ancestry"
        original = marker.read_bytes()
        assert marker.stat().st_mode & 0o777 == 0o600
        if damage == "missing":
            marker.unlink()
        elif damage == "corrupt":
            marker.write_bytes(b"{")
        elif damage == "wrong_inode":
            record = json.loads(original)
            record["boundary"]["inode"] += 1
            marker.write_text(json.dumps(record))
        else:
            target = marker.with_name("retained-ancestry")
            marker.rename(target)
            marker.symlink_to(target)
        other_sid = str(uuid.uuid4())
        with pytest.raises(FsError):
            chat_edits.turn_fence(root, other_sid)
        assert not (root / "proposals" / other_sid / ".turn.lock").exists()
    finally:
        ancestor.chmod(0o700)


def test_concurrent_first_use_cannot_replace_the_creators_boundary(tmp_path, monkeypatch):
    from agent_sessions.fsbrowse import FsError

    ancestor = tmp_path / "search-only"
    root = ancestor / "owned" / "chat-store"
    root.mkdir(parents=True)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    real_link = os.link
    raced = False

    def link(source, destination, **kwargs):
        nonlocal raced
        if Path(destination).name == ".ancestry" and not raced:
            raced = True
            # A worker arriving before publication has no boundary evidence yet. It can
            # refuse temporarily, but must not install a conflicting conservative record.
            with pytest.raises(FsError):
                chat_edits.turn_fence(root, second)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(chat_edits.os, "link", link)
    ancestor.chmod(0o111)
    try:
        fence = chat_edits.turn_fence(root, first)
        assert raced and fence is not None
        fence.release()
        fence = chat_edits.turn_fence(root, second)
        assert fence is not None
        fence.release()
    finally:
        ancestor.chmod(0o700)
