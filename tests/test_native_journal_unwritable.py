"""When an API session's record itself cannot be written (#1733), it fails visibly and honestly.

Real workers and codecs (`test_native_runtime`'s harness, vendor CLIs substituted):

- before any claim, a rollover that cannot be written is a KNOWN refusal: 409
  `journal_unwritable`, nothing claimed, no unit launched — the pane keeps the draft;
- mid-turn, a worker whose append fails really stops; its turn reads uncertain with the specific
  reason (never `not_sent`) and is never resent; a new send waits until that generation is
  proved gone, and Stop is the way through;
- Hand off creates a new durable session record, so under a full disk it fails visibly too.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os

import pytest

import test_native_clients as client_tests
import test_native_journal as journal_tests
import test_native_runtime as rt
from agent_sessions import (
    chat_store,
    native_containment,
    native_journal,
    native_runtime,
    native_segments,
    native_state,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from test_native_journal import request

host, project = rt.host, rt.project
app_client = client_tests.app_client
conversation = journal_tests.conversation
ENGINES, ident, settle, frames = rt.ENGINES, rt.ident, rt.settle, rt.frames


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _root(key):
    engine, _, native = key.partition(":")
    return native_runtime._journal_root(registry.get(engine)), native


def _disk_full(*_args, **_kwargs):
    raise native_segments.Unwritable("disk_full", "a new segment could not be written")


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_rollover_that_cannot_be_written_refuses_before_any_claim_or_launch(
    host, project, monkeypatch, source
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text="hello")
    await settle(key, first)
    await runtime.stop(key)
    root, sid = _root(key)
    before = {
        p.name: p.read_bytes()
        for p in root.iterdir()
        if p.name.startswith(sid) and not p.name.endswith(".lock")
    }
    launches = len(host.launches)
    # The record is due to roll over (this web process sees a tiny segment) and cannot.
    monkeypatch.setattr(native_journal, "SOFT_BYTES", 1)
    with monkeypatch.context() as fault:
        fault.setattr(native_segments, "rollover", _disk_full)
        refused_id = ident()
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.submit_turn(key, operation_id=refused_id, text="continue")
    assert refused.value.status == 409
    assert refused.value.reason == "journal_unwritable"
    assert "disk full" in refused.value.detail and "nothing was sent" in refused.value.detail
    assert len(host.launches) == launches  # no unit was launched for it
    after = {
        p.name: p.read_bytes()
        for p in root.iterdir()
        if p.name.startswith(sid) and not p.name.endswith(".lock")
    }
    assert after == before  # nothing claimed, nothing rolled over, nothing staged
    snap = await runtime.snapshot(key)
    assert refused_id not in {t["turn_id"] for t in snap["turns"]}  # nothing was claimed
    # Space is back: a NEW send (new operation id) goes through, rolling the record over.
    again = ident()
    await runtime.submit_turn(key, operation_id=again, text="continue")
    _, turn = await settle(key, again)
    assert turn["state"] == "completed"
    assert native_segments.indexes(root, sid)  # it rolled over (this process: tiny segments)


def test_the_submit_route_returns_the_closed_reason(app_client, monkeypatch):
    async def refuse(key, **params):
        raise runtime.StructuredError(
            409,
            "BattleLab couldn't write this session's record (disk full); nothing was sent",
            "journal_unwritable",
        )

    monkeypatch.setattr(runtime, "submit_turn", refuse)
    url = f"/api/structured/sessions/codex-api:{ident()}/turns"
    answer = app_client.post(url, json={"operation_id": ident(), "text": "continue"})
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "BattleLab couldn't write this session's record (disk full); nothing was sent",
        "reason": "journal_unwritable",
    }

    async def other(key, **params):
        raise runtime.StructuredError(409, "the conversation changed")

    monkeypatch.setattr(runtime, "submit_turn", other)
    answer = app_client.post(url, json={"operation_id": ident(), "text": "continue"})
    assert answer.status_code == 409 and answer.json() == {"detail": "the conversation changed"}


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude", "opencode", "copilot"])
async def test_a_mid_turn_write_failure_stops_the_worker_and_reads_uncertain(
    host, project, monkeypatch, source
):
    engine = ENGINES[source]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    root, sid = _root(key)
    held = ident()
    await runtime.submit_turn(key, operation_id=held, text="WAIT_RELEASE")
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if any(t["turn_id"] == held and t["state"] == "running" for t in snap["turns"]):
            break
        await asyncio.sleep(0.05)
    worker = snap["native"]["worker"]
    active = native_segments.segment_path(root, sid, max([0, *native_segments.indexes(root, sid)]))
    active.chmod(0o400)  # the record can no longer be written (a full disk, for the worker)
    (project / "release-queued-turn").touch()
    snap, turn = await settle(key, held, state=("uncertain",))
    assert turn["reason"] == "BattleLab couldn't write this session's record (write failed)"
    assert "not_sent" not in turn and "not sent" not in turn["reason"]
    unit = native_containment.WorkerIdentity(worker).unit
    for _ in range(200):
        if not host._alive(unit):
            break
        await asyncio.sleep(0.05)
    assert not host._alive(unit)  # the worker really stopped: its unit is gone
    life = native_state.read_lifecycle(worker)
    assert life["journal_failure"] == "write_failed" and life["phase"] == "exited"
    active.chmod(0o600)
    sent = [f for f in frames(project) if "WAIT_RELEASE" in json.dumps(f["frame"])]
    assert sent
    # A new send goes to a successor; the uncertain turn is never sent again.
    after = ident()
    await runtime.submit_turn(key, operation_id=after, text="hello")
    snap, done = await settle(key, after)
    assert done["state"] == "completed"
    assert next(t for t in snap["turns"] if t["turn_id"] == held)["state"] == "uncertain"
    resent = [f for f in frames(project) if "WAIT_RELEASE" in json.dumps(f["frame"])]
    assert len(resent) == len(sent)


@pytest.mark.anyio
async def test_a_new_send_waits_until_the_failed_generation_is_proved_gone(host, project):
    engine = ENGINES["codex"]
    key = (await runtime.create_session(engine, str(project), operation_id=ident()))["session_key"]
    root, sid = _root(key)
    held = ident()
    await runtime.submit_turn(key, operation_id=held, text="WAIT_RELEASE")
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if any(t["turn_id"] == held and t["state"] == "running" for t in snap["turns"]):
            break
        await asyncio.sleep(0.05)
    worker = snap["native"]["worker"]
    unit = native_containment.WorkerIdentity(worker).unit
    active = native_segments.segment_path(root, sid, max([0, *native_segments.indexes(root, sid)]))
    active.chmod(0o400)
    (project / "release-queued-turn").touch()
    await settle(key, held, state=("uncertain",))
    active.chmod(0o600)
    # Its control group still holds a stuck child: the generation is NOT proved gone.
    stuck = {"held": True}
    show, cgroup, stop = host.show, host.cgroup, host.stop

    def shown(identity):
        if stuck["held"] and identity.unit == unit:
            return native_containment.UnitObservation(
                unit,
                "loaded",
                "active",
                "running",
                host.invocations[unit],
                f"/user.slice/{unit}",
                host.procs[unit].pid,
                "success",
            )
        return show(identity)

    def observe(control_group):
        if stuck["held"] and os.path.basename(control_group) == unit:
            return native_containment.CgroupObservation(control_group, "populated")
        return cgroup(control_group)

    def stopping(argv):
        if argv[-1] == unit:
            stuck["held"] = False  # Stop really ends it
        return stop(argv)

    host.show, host.cgroup, host.stop = shown, observe, stopping
    launches = len(host.launches)
    # Refused (the existing admission: never a successor beside an unproved generation), and
    # nothing is launched or claimed for it.
    refused_id = ident()
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(key, operation_id=refused_id, text="hello")
    assert refused.value.status in {409, 503} and len(host.launches) == launches
    snap = await runtime.snapshot(key)
    assert refused_id not in {t["turn_id"] for t in snap["turns"]}
    assert next(t for t in snap["turns"] if t["turn_id"] == held)["state"] == "uncertain"
    await runtime.stop(key)  # the operator's Stop proves it gone
    after = ident()
    await runtime.submit_turn(key, operation_id=after, text="hello")
    _, turn = await settle(key, after)
    assert turn["state"] == "completed"


@pytest.mark.anyio
async def test_hand_off_under_a_full_disk_fails_visibly_and_creates_nothing(
    host, project, monkeypatch
):
    """Hand off writes a NEW durable session record (`create_session`): it may fail too."""
    original = os.write

    def full(fd, data):
        if os.path.basename(os.readlink(f"/proc/self/fd/{fd}")).startswith("."):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return original(fd, data)

    engine = ENGINES["codex"]
    root = native_runtime._journal_root(registry.get(engine))

    def journals():
        return sorted(p.name for p in root.glob("*.jsonl")) if root.exists() else []

    before, launches = journals(), len(host.launches)
    with monkeypatch.context() as fault:
        fault.setattr(chat_store.os, "write", full)
        with pytest.raises((runtime.StructuredError, OSError)):
            await runtime.create_session(engine, str(project), operation_id=ident())
    assert journals() == before and len(host.launches) == launches


# --- the reason, the queued messages, persistence only after fsync -----------------------------


def test_the_specific_reason_needs_the_lifecycle_and_stays_uncertain(monkeypatch):
    worker = ident()
    monkeypatch.setattr(native_state, "read_lifecycle", lambda w: {"journal_failure": "disk_full"})
    assert native_runtime._ended_reason(worker) == (
        "BattleLab couldn't write this session's record (disk full)"
    )
    assert native_runtime._ended_code(worker) == "journal_unwritable"
    # On a truly full disk the lifecycle write itself can fail: the generic text, still uncertain.
    monkeypatch.setattr(native_state, "read_lifecycle", lambda w: {"phase": "exited"})
    assert native_runtime._ended_reason(worker) is None
    assert native_runtime._ended_code(worker) == "worker_ended"


def test_a_queued_message_of_a_stopped_worker_settles_journal_unwritable(conversation):
    root, sid, binding = conversation
    running = request(binding)
    native_journal.claim(root, running)
    native_journal.record_handoff(root, binding, running["params"]["operation_id"], "sent")
    queued = request(binding, revision=3, text="then this")
    native_journal.claim(root, queued, queued=True)
    qid = queued["params"]["operation_id"]

    def view():
        return native_runtime.project(
            native_journal.read(root, sid),
            {"request": {"model": None}, "adapter": binding.adapter},
            None,
            ended_workers={binding.worker_id},
            ended_code=lambda w: "journal_unwritable",
        )

    derived = next(t for t in view()["turns"] if t["turn_id"] == qid)
    assert derived["not_sent"]["code"] == "journal_unwritable"
    assert native_journal.settle_worker_ended(
        root, binding.session_key, qid, binding.worker_id, "journal_unwritable"
    )
    native_journal._CACHE.clear()
    durable = native_journal.read(root, sid).operations[qid]
    assert (durable.handoff, durable.reason_code) == ("not_sent", "journal_unwritable")
    with pytest.raises(native_journal.JournalError):
        native_journal.settle_worker_ended(root, binding.session_key, qid, binding.worker_id, "x")


def test_a_settlement_is_only_persistent_after_its_fsync(conversation, monkeypatch):
    """A terminal settlement whose fsync failed is never reported written."""
    root, sid, binding = conversation
    queued = request(binding, revision=1)
    native_journal.claim(root, queued, queued=True)
    qid = queued["params"]["operation_id"]
    fsync = os.fsync
    with monkeypatch.context() as fault:
        fault.setattr(
            os,
            "fsync",
            lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "fsync failed")),
        )
        with pytest.raises(native_journal.JournalError):
            native_journal.settle_worker_ended(
                root, binding.session_key, qid, binding.worker_id, "journal_unwritable"
            )
    # Never acknowledged; a reader shows it only once ITS fsync of those bytes succeeds.
    synced = []
    with monkeypatch.context() as watch:
        watch.setattr(os, "fsync", lambda fd: (synced.append(fd), fsync(fd))[1])
        native_journal._CACHE.clear()
        assert native_journal.read(root, sid).operations[qid].handoff == "not_sent"
    assert synced


# --- Hermes on #1771: older workers, effects already sent ---------------------------------------


def test_no_web_side_rollover_unless_every_generation_reads_segments_or_is_proved_gone(
    conversation, monkeypatch
):
    """A worker of the previous release reads and appends segment 0 only, possibly through a
    descriptor it opened before any freeze. The web process rolls over only on AUTHORITATIVE
    evidence: every recorded generation reads segments or is proved gone — never because a cheap
    liveness observation missed it (Hermes on #1771)."""
    root, sid, binding = conversation
    monkeypatch.setattr(native_runtime, "_journal_root", lambda prov: root)
    monkeypatch.setattr(native_runtime, "_live_worker", lambda session_id: None)  # missed it
    monkeypatch.setattr(native_journal, "SOFT_BYTES", 1)
    record = {"current_worker": "old", "workers": ["old"]}
    lifecycles = {"old": {"phase": "ready"}}
    gone = {"old": False}
    monkeypatch.setattr(native_state, "read_session", lambda session_id: record)
    monkeypatch.setattr(native_state, "read_lifecycle", lambda w: lifecycles.get(w, {}))
    monkeypatch.setattr(native_runtime, "_proved_gone", lambda w: gone[w])
    # The older worker's descriptor, opened before anything else happens.
    path = native_segments.segment_path(root, sid, 0)
    old = os.open(path, os.O_RDWR | os.O_APPEND)
    try:
        native_runtime._prepare_journal(None, sid)
        assert native_segments.indexes(root, sid) == []  # not proved gone: no rollover
        queued = request(binding, revision=1)
        native_journal.claim(root, queued, queued=True)  # (a NEW worker would roll over itself)
        native_journal._CACHE.clear()
        before = native_segments.indexes(root, sid)
        native_journal.settle_worker_ended(
            root,
            binding.session_key,
            queued["params"]["operation_id"],
            binding.worker_id,
            may_roll=native_runtime._segments_safe(sid),
        )
        assert native_segments.indexes(root, sid) == before
    finally:
        os.close(old)
    for change in ({"journal_segments": True}, None):
        if change is None:
            lifecycles["old"] = {"phase": "exited"}
            gone["old"] = True  # proved gone (unit and cgroup evidence)
        else:
            lifecycles["old"] = {"phase": "ready", **change}
        # The active segment holds a record of its own (an empty successor never rolls again).
        native_journal.append_events(
            root, binding, [{"kind": "memory_pressure", "data": {"level": "normal"}}]
        )
        count = len(native_segments.indexes(root, sid))
        native_runtime._prepare_journal(None, sid)
        assert len(native_segments.indexes(root, sid)) == count + 1
    record["workers"] = ["unreadable"]
    monkeypatch.setattr(
        native_state,
        "read_lifecycle",
        lambda w: (_ for _ in ()).throw(native_state.StateError("unreadable")),
    )
    assert native_runtime._segments_safe(sid) is False


def test_a_journal_failure_after_an_effect_is_never_a_known_refusal():
    """Send now writes the queued message to the CLI before journaling its control: a journal
    failure there stays unavailable (uncertain), never `journal_unwritable` / nothing was sent."""
    import inspect
    from types import SimpleNamespace

    from agent_sessions import native_ipc, native_worker

    binding = native_ipc.Binding(
        session_key=f"codex-api:{ident()}",
        worker_id=ident(),
        connection_id=ident(),
        adapter="codex-app-server",
    )
    reports = []
    stub = SimpleNamespace(binding=binding, report=lambda **fields: reports.append(fields))
    stub.error = lambda *a: native_worker.Worker.error(stub, *a)
    stub.journal_failed = lambda exc: native_worker.Worker.journal_failed(stub, exc)
    req = {"request_id": ident(), "action": "send_now"}
    exc = native_journal.JournalError(
        "unwritable", "a new segment could not be written", "disk_full"
    )
    after = native_worker.Worker.journal_error(stub, req, exc, before_effect=False)
    assert after["error"]["code"] == "unavailable" and "reason" not in after["error"]
    assert "nothing was sent" not in after["error"]["message"]
    before = native_worker.Worker.journal_error(stub, req, exc)
    assert before["error"]["reason"] == "journal_unwritable"
    assert reports == [{"journal_failure": "disk_full"}] * 2
    for handler in (native_worker.Worker.send_now, native_worker.Worker.withdraw):
        assert "before_effect=False" in inspect.getsource(handler)


# --- Hermes on #1771, round 3: an inventory that is not authoritative ---------------------------


def _legacy_append(fd, binding):
    """What a worker of the previous release appends through its own descriptor of segment 0."""
    record = {
        "type": "native_event",
        "worker_id": binding.worker_id,
        "connection_id": binding.connection_id,
        "ts": 1.0,
        "event": {"kind": "memory_pressure", "data": {"level": "normal"}},
    }
    os.write(fd, chat_store._records_bytes([record]))


def test_a_missing_session_record_never_authorizes_a_rollover(conversation, monkeypatch, tmp_path):
    """A lost or unreadable record is not proof that no older worker runs (Hermes on #1771): one
    with a pre-opened descriptor would append behind the successor's pin."""
    root, sid, binding = conversation
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(native_runtime, "_journal_root", lambda prov: root)
    monkeypatch.setattr(native_journal, "SOFT_BYTES", 1)
    monkeypatch.setattr(native_state, "read_session", lambda session_id: None)
    path = native_segments.segment_path(root, sid, 0)
    legacy = os.open(path, os.O_RDWR | os.O_APPEND)  # opened before anything else happens
    try:
        assert native_runtime._segments_safe(sid) is False
        native_runtime._prepare_journal(None, sid)
        assert native_segments.indexes(root, sid) == []  # nothing rolled over, nothing frozen
        _legacy_append(legacy, binding)
    finally:
        os.close(legacy)
    native_journal._CACHE.clear()
    assert native_journal.read(root, sid).revision == 2  # the chain is intact and readable
    for broken in ({"workers": "old"}, ["not", "a", "record"]):
        monkeypatch.setattr(native_state, "read_session", lambda session_id, b=broken: b)
        assert native_runtime._segments_safe(sid) is False


def test_the_proof_and_the_rollover_hold_the_lifecycle_lock_launches_register_under(
    conversation, monkeypatch, tmp_path
):
    """A generation registers under the session lifecycle lock; admission proves the inventory
    and rolls over under the same lock, so none can start between the proof and the rollover."""
    import threading

    root, sid, binding = conversation
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(native_runtime, "_journal_root", lambda prov: root)
    monkeypatch.setattr(native_journal, "SOFT_BYTES", 1)
    monkeypatch.setattr(native_state, "read_session", lambda session_id: {"workers": []})
    attempts = []
    prepare = native_journal.prepare

    def register_meanwhile(*args):
        def launch():  # what `_launch_locked`'s caller does to register a generation
            try:
                with native_state.session_lock(sid, wait=0):
                    attempts.append("registered")
            except native_state.LockBusy:
                attempts.append("blocked")

        thread = threading.Thread(target=launch)
        thread.start()
        thread.join(10)
        return prepare(*args)

    monkeypatch.setattr(native_journal, "prepare", register_meanwhile)
    native_runtime._prepare_journal(None, sid)
    assert attempts == ["blocked"]
    assert native_segments.indexes(root, sid) == [1]
    with native_state.session_lock(sid, wait=0):  # released afterwards
        pass


def test_on_a_full_disk_the_pre_claim_refusal_is_the_known_409(conversation, monkeypatch, tmp_path):
    """Review of #1771: with EVERY write under the store failing ENOSPC (not just the successor
    temp) the fold's spool used to fail first, as a generic unavailable journal (404, no panel
    B). It now reaches the known refusal. Fault injection only."""
    from test_native_journal_chain import _caches_cold, _full_disk

    root, sid, binding = conversation
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(native_runtime, "_journal_root", lambda prov: root)
    monkeypatch.setattr(native_state, "read_session", lambda session_id: {"workers": []})
    native_journal.claim(root, request(binding))  # the session's lock file exists
    monkeypatch.setattr(native_journal, "SOFT_BYTES", 1)  # due to roll over before the next claim
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    _caches_cold()
    with monkeypatch.context() as disk:
        _full_disk(disk, root)
        with pytest.raises(native_runtime.NativeError) as refused:
            native_runtime._prepare_journal(None, sid)
    assert refused.value.status == 409 and refused.value.reason == "journal_unwritable"
    assert "disk full" in refused.value.detail
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
