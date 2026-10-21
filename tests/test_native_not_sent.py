"""A queued message is never lost without a reason (#1538): the admission identity, the closed
reason code from the worker to the journal, its read-back, and the snapshot's ``not_sent``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_manifest_api as manifests
import test_native_journal as journal_tests
import test_plugins as plugins
from agent_sessions import (
    chat_store,
    native_ipc,
    native_journal,
    native_ownership,
    native_protocol,
    native_runtime,
    native_state,
    native_worker,
    structured_runtime,
)
from agent_sessions.plugins import PluginProvider, api_source, provenance
from test_native_journal import request

conversation = journal_tests.conversation


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- the selection a launch pins, and what a fresh resolution must keep ---------------------------


def _first_party(tmp_path: Path, *, env=None, search=("~/bin",)) -> PluginProvider:
    d = plugins.doc()
    d["binary"]["search_paths"] = list(search)
    return PluginProvider(
        plugins.parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env=env or {},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )


def _launcher(tmp_path: Path, target: Path) -> Path:
    (tmp_path / "bin").mkdir(exist_ok=True)
    link = tmp_path / "bin" / "gemini"
    if link.is_symlink():
        link.unlink()
    os.symlink(target, link)
    return link


def _pinned(prov: PluginProvider) -> dict:
    ep = prov.entrypoint_selection()
    return api_source.selection(ep, prov.trust)


def _same(pinned: dict, prov: PluginProvider) -> bool:
    ep = prov.entrypoint_selection()
    return api_source.same_selection(pinned, ep, prov.trust)


def test_the_selector_is_the_unresolved_launcher_and_a_vendor_update_keeps_it(tmp_path):
    v1 = plugins.exe(tmp_path / "versions" / "1" / "gemini")
    v2 = plugins.exe(tmp_path / "versions" / "2" / "gemini")
    link = _launcher(tmp_path, v1)
    ep = _first_party(tmp_path).entrypoint_selection()
    assert (ep.via, ep.selector, ep.state, ep.path) == (
        "search_paths",
        str(link),
        provenance.ADOPTED,
        str(v1),
    )
    pinned = _pinned(_first_party(tmp_path))
    _launcher(tmp_path, v2)
    fresh = _first_party(tmp_path).entrypoint_selection()
    assert fresh.path == str(v2) and fresh.selector == str(link)
    assert _same(pinned, _first_party(tmp_path))


def test_a_retarget_outside_the_versions_directory_keeps_the_selection(tmp_path):
    """The policy decision: the first-party launcher is trusted for any provenance-valid
    target. There is no vendor-root check."""
    pinned_target = plugins.exe(tmp_path / "versions" / "1" / "gemini")
    _launcher(tmp_path, pinned_target)
    pinned = _pinned(_first_party(tmp_path))
    _launcher(tmp_path, plugins.exe(tmp_path / "opt" / "mine" / "gemini"))
    assert _same(pinned, _first_party(tmp_path))


@pytest.mark.parametrize("change", ["set", "changed", "cleared"])
def test_an_env_override_set_changed_or_cleared_is_another_selection(tmp_path, change):
    var = plugins.parse(plugins.doc()).binary.env_var
    a = plugins.exe(tmp_path / "a" / "gemini")
    b = plugins.exe(tmp_path / "b" / "gemini")
    _launcher(tmp_path, a)
    before = {} if change == "set" else {var: str(a)}
    after = {"set": {var: str(a)}, "changed": {var: str(b)}, "cleared": {}}[change]
    pinned = _pinned(_first_party(tmp_path, env=before))
    assert not _same(pinned, _first_party(tmp_path, env=after))


def test_a_different_search_path_hit_is_another_selection(tmp_path):
    later = plugins.exe(tmp_path / "later" / "gemini")
    pinned = _pinned(_first_party(tmp_path, search=("~/first", "~/later")))
    assert pinned["selector"] == str(later)
    plugins.exe(tmp_path / "first" / "gemini")  # an earlier search path now has one
    assert not _same(pinned, _first_party(tmp_path, search=("~/first", "~/later")))


def test_an_install_record_appearing_changes_via_and_state(tmp_path):
    target = plugins.exe(tmp_path / "versions" / "1" / "gemini")
    _launcher(tmp_path, target)
    pinned = _pinned(_first_party(tmp_path))
    prov = _first_party(tmp_path)
    installed = plugins.exe(tmp_path / "p" / "bin" / "gemini")
    plugins.write_record(
        prov, install_entrypoint="bin/gemini", install_sha256=plugins.sha(installed)
    )
    ep = _first_party(tmp_path).entrypoint_selection()
    assert (ep.via, ep.state) == ("install", provenance.MANAGED)
    assert not _same(pinned, _first_party(tmp_path))


def test_a_trust_change_is_another_selection(tmp_path):
    target = plugins.exe(tmp_path / "versions" / "1" / "gemini")
    _launcher(tmp_path, target)
    ep = _first_party(tmp_path).entrypoint_selection()
    pinned = api_source.selection(ep, provenance.FIRST_PARTY)
    assert not api_source.same_selection(pinned, ep, provenance.LOCAL)
    assert not api_source.same_selection(None, ep, provenance.FIRST_PARTY)


def test_a_third_party_adopted_retarget_fails_its_confirmation(tmp_path):
    d = plugins.doc()
    d["binary"]["search_paths"] = ["~/bin"]
    a = plugins.exe(tmp_path / "versions" / "a" / "myagent", b"#!/bin/true\n#a\n")
    b = plugins.exe(tmp_path / "versions" / "b" / "myagent", b"#!/bin/true\n#b\n")
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "myagent"
    os.symlink(a, link)
    prov = plugins.local(d, tmp_path)
    plugins.write_record(prov, confirmed_path=str(a), confirmed_sha256=plugins.sha(a))
    assert _pinned(prov)["state"] == provenance.ADOPTED
    link.unlink()
    os.symlink(b, link)
    with pytest.raises(plugins.EngineError, match="confirmation"):
        plugins.local(d, tmp_path).entrypoint_selection()


def test_a_managed_binary_with_a_changed_digest_fails_provenance(tmp_path):
    d = plugins.doc()
    d["install"] = plugins._install(entrypoint="bin/myagent")
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    root = tmp_path / "plugins" / "myagent"
    ep = plugins.exe(root / "bin" / "myagent", b"#!/bin/true\n#v1\n")

    def prov():
        return PluginProvider(
            plugins.parse(d),
            trust=provenance.LOCAL,
            root=root,
            env={},
            home=tmp_path,
            state_dir=tmp_path / "state",
        )

    plugins.write_record(prov(), install_entrypoint="bin/myagent", install_sha256=plugins.sha(ep))
    assert _pinned(prov())["state"] == provenance.MANAGED
    ep.write_bytes(b"#!/bin/true\n#v2\n")
    with pytest.raises(plugins.EngineError, match="no longer matches"):
        prov().entrypoint_selection()


# --- admit_queued: every gate a launch passed holds again -----------------------------------------


@pytest.fixture
def admission(tmp_path, monkeypatch):
    """A claude-api client over the in-tree claude manifest, whose `~/.local/bin/claude`
    launcher points into `versions/`, with the ownership ledger stubbed to agree."""
    api = manifests._provider(
        manifests._api(name="claude-api", source="claude", kind="claude-stream-json"), tmp_path
    )
    source = manifests._provider(manifests._doc("claude"), tmp_path)
    target = plugins.exe(tmp_path / ".local" / "share" / "claude" / "versions" / "1" / "claude")
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    os.symlink(target, tmp_path / ".local" / "bin" / "claude")
    ep = source.entrypoint_selection()
    config = {
        "source_engine": "claude",
        "binary": ep.path,
        "entrypoint": api_source.selection(ep, source.trust),
    }
    owner = SimpleNamespace(source="the-store")
    monkeypatch.setattr(native_ownership, "lookup", lambda key: owner)
    monkeypatch.setattr(native_ownership, "source_identity", lambda prov: "the-store")
    roster = SimpleNamespace(by_id={"claude-api": api, "claude": source}, generation=1)
    return SimpleNamespace(roster=roster, config=config, owner=owner, tmp=tmp_path)


KEY = f"claude-api:{uuid.uuid4()}"


def _admit(a, *, adapter="claude-stream-json", roster=None, config=None):
    native_worker.admit_queued(roster or a.roster, KEY, adapter, config or a.config)


def _refusal(a, **kw) -> str:
    with pytest.raises(native_worker.AdmissionRefused) as refused:
        _admit(a, **kw)
    return refused.value.code


def test_the_launched_agent_is_admitted_after_its_launcher_moved(admission):
    _admit(admission)
    newer = plugins.exe(admission.tmp / ".local" / "share" / "claude" / "versions" / "2" / "claude")
    link = admission.tmp / ".local" / "bin" / "claude"
    link.unlink()
    os.symlink(newer, link)
    _admit(admission)


def test_a_removed_api_client_or_source_is_agent_removed(admission):
    by_id = admission.roster.by_id
    gone = SimpleNamespace(by_id={"claude": by_id["claude"]}, generation=1)
    assert _refusal(admission, roster=gone) == "agent_removed"
    no_source = SimpleNamespace(by_id={"claude-api": by_id["claude-api"]}, generation=1)
    assert _refusal(admission, roster=no_source) == "agent_removed"


def test_a_switched_source_engine_or_protocol_is_agent_changed(admission):
    assert _refusal(admission, config={**admission.config, "source_engine": "codex"}) == (
        "agent_changed"
    )
    assert _refusal(admission, adapter="codex-app-server") == "agent_changed"


def test_a_launch_that_pinned_only_its_path_keeps_the_exact_path_rule(admission):
    old = {k: v for k, v in admission.config.items() if k != "entrypoint"}
    _admit(admission, config=old)
    assert _refusal(admission, config={**old, "binary": "/elsewhere/claude"}) == "agent_changed"


def test_a_removed_launcher_is_agent_changed(admission):
    (admission.tmp / ".local" / "bin" / "claude").unlink()
    assert _refusal(admission) == "agent_changed"


def test_a_changed_store_owner_is_source_store_changed(admission, monkeypatch):
    monkeypatch.setattr(native_ownership, "lookup", lambda key: None)
    assert _refusal(admission) == "source_store_changed"
    monkeypatch.setattr(native_ownership, "lookup", lambda key: SimpleNamespace(source="other"))
    assert _refusal(admission) == "source_store_changed"


def test_admission_refusals_have_fixed_messages_and_a_closed_code():
    for code in native_worker.AdmissionRefused.MESSAGES:
        assert code in native_journal.REASON_CODES
        assert (
            str(native_worker.AdmissionRefused(code))
            == native_worker.AdmissionRefused.MESSAGES[code]
        )
    with pytest.raises(ValueError):
        native_worker.AdmissionRefused("anything else")


# --- dispatch_queue: the refusal is recorded with its code and logged by type name only -----------


def _dispatcher(tmp_path, admission_error, monkeypatch, recorded):
    worker = native_worker.Worker.__new__(native_worker.Worker)
    worker.session_key = KEY
    worker.journal_root = tmp_path
    worker.binding = None
    worker.done = asyncio.Event()
    worker.queue_wake = asyncio.Event()
    worker.effects = asyncio.Lock()
    worker.queued = [{"operation_id": str(uuid.uuid4()), "text": "x"}]
    worker.sent_now = set()
    worker.terminated = worker.closing = False
    worker.reason = ""
    worker.flush = lambda: None
    worker.dispatch_busy = lambda: False
    worker._sent_now_cuts = lambda: False
    worker.stop_child = lambda: None

    async def current(_params):
        return True

    worker._answer_current = current

    @contextlib.contextmanager
    def refuse():
        raise admission_error
        yield  # pragma: no cover

    worker.queued_admission = refuse

    def record(root, binding, operation_id, handoff, **kw):
        recorded.append((operation_id, handoff, kw))
        worker.done.set()
        return {}

    monkeypatch.setattr(native_journal, "record_handoff", record)
    return worker


async def _drain(worker):
    worker.queue_wake.set()
    task = asyncio.create_task(worker.dispatch_queue())
    for _ in range(200):
        if worker.done.is_set():
            break
        await asyncio.sleep(0.01)
    worker.done.set()
    worker.queue_wake.set()
    await asyncio.wait_for(task, 5)


@pytest.mark.anyio
async def test_an_unexpected_exception_is_recorded_unexpected_and_logged_by_type_name_only(
    tmp_path, monkeypatch, caplog
):
    secret = "/home/someone/.config/token=sk-live-123 at /srv/claude/versions/9"
    recorded = []
    worker = _dispatcher(tmp_path, KeyError(secret), monkeypatch, recorded)
    operation = worker.queued[0]["operation_id"]
    with caplog.at_level(logging.WARNING, logger="agent_sessions.native_worker"):
        await _drain(worker)
    assert recorded == [(operation, "not_sent", {"reason_code": "unexpected"})]
    lines = [r.getMessage() for r in caplog.records if r.name == "agent_sessions.native_worker"]
    assert lines == [
        f"queued message not sent: session={KEY} operation={operation} "
        "reason=unexpected error=KeyError"
    ]
    assert "token" not in caplog.text and "/home/someone" not in caplog.text
    assert worker.queued == []


@pytest.mark.anyio
@pytest.mark.parametrize("code", sorted(native_worker.AdmissionRefused.MESSAGES))
async def test_an_admission_refusal_is_recorded_with_its_code(tmp_path, monkeypatch, caplog, code):
    recorded = []
    worker = _dispatcher(tmp_path, native_worker.AdmissionRefused(code), monkeypatch, recorded)
    with caplog.at_level(logging.WARNING, logger="agent_sessions.native_worker"):
        await _drain(worker)
    assert [r[1:] for r in recorded] == [("not_sent", {"reason_code": code})]
    assert f"reason={code} error=AdmissionRefused" in caplog.text


@pytest.mark.anyio
async def test_lock_contention_is_never_a_refusal(tmp_path, monkeypatch):
    recorded = []
    worker = _dispatcher(tmp_path, native_state.LockBusy("busy"), monkeypatch, recorded)
    task = asyncio.create_task(worker.dispatch_queue())
    worker.queue_wake.set()
    await asyncio.sleep(0.3)
    worker.done.set()
    worker.queue_wake.set()
    await asyncio.wait_for(task, 5)
    assert recorded == [] and len(worker.queued) == 1


# --- the journal: the code, its read-back, and records from before it ----------------------------


def _queued(root, sid, binding):
    req = request(binding, revision=native_journal.read(root, sid).revision)
    native_journal.claim(root, req, queued=True)
    return req["params"]["operation_id"]


def test_the_reason_code_is_recorded_and_read_back(conversation):
    root, sid, binding = conversation
    op = _queued(root, sid, binding)
    receipt = native_journal.record_handoff(
        root, binding, op, "not_sent", reason_code="agent_changed"
    )
    assert receipt["reason_code"] == "agent_changed"
    native_journal._CACHE.clear()
    read = native_journal.read(root, sid).operations[op]
    assert (read.handoff, read.reason_code, read.queued) == ("not_sent", "agent_changed", True)
    assert read.receipt()["reason_code"] == "agent_changed"
    # A repeat only observes the first: the reason never changes.
    again = native_journal.record_handoff(root, binding, op, "not_sent", reason_code="worker_ended")
    assert again["reason_code"] == "agent_changed"


def test_a_record_from_before_reason_codes_reads_as_unexpected(conversation):
    root, sid, binding = conversation
    op = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, op, "not_sent")
    line = json.loads(chat_store._path(root, sid).read_text().splitlines()[-1])
    assert "reason_code" not in line  # exactly the shape an older worker wrote
    native_journal._CACHE.clear()
    read = native_journal.read(root, sid).operations[op]
    assert read.reason_code is None and read.receipt()["reason_code"] == "unexpected"


@pytest.mark.parametrize(
    "handoff,code",
    [("sent", "agent_changed"), ("not_sent", "because"), ("uncertain", "unexpected")],
)
def test_a_reason_is_only_a_closed_code_on_a_not_sent(conversation, handoff, code):
    root, sid, binding = conversation
    op = _queued(root, sid, binding)
    with pytest.raises(native_journal.JournalError):
        native_journal.record_handoff(root, binding, op, handoff, reason_code=code)


def test_a_hand_written_reason_outside_the_set_makes_the_journal_unreadable(conversation):
    root, sid, binding = conversation
    op = _queued(root, sid, binding)
    with chat_store._path(root, sid).open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "native_handoff",
                    "operation_id": op,
                    "handoff": "not_sent",
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "ts": 1.0,
                    "reason_code": "/home/x/secret",
                }
            )
            + "\n"
        )
    native_journal._CACHE.clear()
    with pytest.raises(native_journal.JournalError):
        native_journal.read(root, sid)


def test_worker_ended_is_written_once_and_only_for_a_still_queued_message(conversation):
    root, sid, binding = conversation
    waiting, left = _queued(root, sid, binding), _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, left, "uncertain")
    key, worker = binding.session_key, binding.worker_id
    assert native_journal.settle_worker_ended(root, key, waiting, worker) is True
    assert native_journal.settle_worker_ended(root, key, waiting, worker) is False
    assert native_journal.settle_worker_ended(root, key, left, worker) is False
    assert native_journal.settle_worker_ended(root, key, waiting, str(uuid.uuid4())) is False
    journal = native_journal.read(root, sid)
    assert journal.operations[waiting].reason_code == "worker_ended"
    assert journal.operations[left].handoff == "uncertain"


def test_ipc_receipts_carry_a_reason_only_on_not_sent():
    base = {"operation_id": str(uuid.uuid4()), "recorded_revision": 3}
    out = native_ipc._result(
        "submit", {**base, "handoff": "not_sent", "reason_code": "worker_ended"}
    )
    assert out["reason_code"] == "worker_ended"
    assert "reason_code" not in native_ipc._result("submit", {**base, "handoff": "not_sent"})
    for bad in (
        {**base, "handoff": "sent", "reason_code": "agent_changed"},
        {**base, "handoff": "not_sent", "reason_code": "free text"},
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc._result("submit", bad)


# --- the snapshot's `not_sent`: queued operator submits only ------------------------------------


def _view(root, sid, binding, live=None, **kw):
    raw = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id if live is None else live,
        **kw,
    )
    return raw, {
        t["turn_id"]: t for t in structured_runtime._snapshot(binding.session_key, raw)["turns"]
    }


def test_not_sent_is_on_every_operator_submit_proved_unsent_and_nothing_else(conversation):
    root, sid, binding = conversation
    direct = request(binding)
    native_journal.claim(root, direct)
    direct_id = direct["params"]["operation_id"]
    native_journal.record_handoff(root, binding, direct_id, "not_sent")
    refused = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, refused, "not_sent", reason_code="agent_removed")
    legacy = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, legacy, "not_sent")
    uncertain = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, uncertain, "uncertain")
    waiting = _queued(root, sid, binding)
    _, turns = _view(root, sid, binding)
    assert turns[refused]["not_sent"]["code"] == "agent_removed"
    assert turns[refused]["state"] == "failed"
    assert turns[refused]["reason"] == (
        "not sent: this session's agent was removed while the message waited"
        " — nothing was delivered"
    )
    assert turns[legacy]["not_sent"]["code"] == "unexpected"
    # A direct submit refused before stdin is the same proved non-send (#1538 review: a Send
    # again is usually direct); one written before reason codes reads `unexpected`.
    assert turns[direct_id]["not_sent"]["code"] == "unexpected"
    assert turns[direct_id]["not_sent"]["retry"] == native_journal.retry_id(direct_id)
    for other in (uncertain, waiting):
        assert "not_sent" not in turns[other], other
    assert turns[direct_id]["state"] == "failed"
    assert turns[waiting]["state"] == "queued"
    # Its worker gone, a message still queued reads `worker_ended` (the derived fallback).
    # Its worker PROVED ended (#1538 review): a message still queued reads `worker_ended`.
    _, gone = _view(root, sid, binding, live=str(uuid.uuid4()), ended_workers={binding.worker_id})
    assert gone[waiting]["not_sent"]["code"] == "worker_ended"
    assert "not_sent" not in gone[uncertain]
    # Merely not observed live proves nothing: still pending, no `not_sent`, no Send again.
    _, unseen = _view(root, sid, binding, live=str(uuid.uuid4()))
    assert unseen[waiting]["state"] == "queued" and "not_sent" not in unseen[waiting]


def test_a_refused_send_now_control_puts_no_not_sent_on_its_message(conversation):
    root, sid, binding = conversation
    running = request(binding)
    native_journal.claim(root, running)
    target = running["params"]["operation_id"]
    native_journal.record_handoff(root, binding, target, "sent")
    message = _queued(root, sid, binding)
    control = request(
        binding,
        action="send_now",
        turn_id=target,
        queued_turn_id=message,
        mode="interrupt",
        revision=native_journal.read(root, sid).revision,
    )
    native_journal.claim(root, control)
    native_journal.record_handoff(root, binding, control["params"]["operation_id"], "not_sent")
    _, turns = _view(root, sid, binding)
    assert turns[message]["state"] == "queued" and "not_sent" not in turns[message]
    assert all("not_sent" not in t for t in turns.values())


def test_not_sent_survives_history_pagination(conversation):
    root, sid, binding = conversation
    refused = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, refused, "not_sent", reason_code="agent_changed")
    later = []
    for _ in range(3):
        req = request(binding, revision=native_journal.read(root, sid).revision)
        native_journal.claim(root, req)
        native_journal.record_handoff(root, binding, req["params"]["operation_id"], "sent")
        later.append(req["params"]["operation_id"])
    window, _ = _view(root, sid, binding, max_turns=2)
    assert refused not in {t["turn_id"] for t in window["turns"]}
    page = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=2,
        before=window["history_before"],
    )
    shaped = [structured_runtime._turn(t) for t in page["turns"]]
    assert next(t for t in shaped if t["turn_id"] == refused)["not_sent"]["code"] == "agent_changed"


# --- #1538 review: reason types, the derived retry identity, authoritative exit evidence ---------


@pytest.mark.parametrize("bad", [[], {}, 7, None])
def test_a_reason_of_the_wrong_type_is_a_journal_error_never_a_type_error(conversation, bad):
    root, sid, binding = conversation
    op = _queued(root, sid, binding)
    if bad is not None:
        with pytest.raises(native_journal.JournalError):
            native_journal.record_handoff(root, binding, op, "not_sent", reason_code=bad)
    with chat_store._path(root, sid).open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "native_handoff",
                    "operation_id": op,
                    "handoff": "not_sent",
                    "worker_id": binding.worker_id,
                    "connection_id": binding.connection_id,
                    "ts": 1.0,
                    "reason_code": bad,
                }
            )
            + "\n"
        )
    native_journal._CACHE.clear()
    with pytest.raises(native_journal.JournalError):
        native_journal.read(root, sid)


def test_the_retry_id_is_derived_from_the_original_and_nothing_else():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert native_journal.retry_id(a) == native_journal.retry_id(a)
    assert native_journal.retry_id(a) != native_journal.retry_id(b)
    assert native_journal.retry_id(a) not in {a, b}
    assert str(uuid.UUID(native_journal.retry_id(a))) == native_journal.retry_id(a)
    with pytest.raises(native_journal.JournalError):
        native_journal.retry_id("not-a-uuid")


def test_the_snapshot_says_sent_again_from_the_journal_whatever_the_window(conversation):
    root, sid, binding = conversation
    refused = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, refused, "not_sent", reason_code="agent_changed")
    retry = native_journal.retry_id(refused)
    _, turns = _view(root, sid, binding)
    assert turns[refused]["not_sent"] == {
        "code": "agent_changed",
        "retry": retry,
        "retry_recorded": False,
        "retried": False,
    }
    req = request(binding, operation_id=retry, revision=native_journal.read(root, sid).revision)
    native_journal.claim(root, req)
    for _ in range(3):  # push the retry itself out of a small window
        more = request(binding, revision=native_journal.read(root, sid).revision)
        native_journal.claim(root, more)
    page = native_runtime.project(
        native_journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
    )
    original = next(t for t in page["turns"] if t["turn_id"] == refused)
    assert original["not_sent"]["retried"] is True


@pytest.mark.parametrize(
    "evidence,proved",
    [
        ({"record": {"current_worker": "other"}}, True),
        ({"record": {"closed": True}}, True),
        ({"phase": "exited"}, True),
        ({"phase": "refused"}, True),
        ({"gone": True}, True),
        ({}, False),  # current, open, phase ready, not proved gone: inconclusive
    ],
)
def test_only_authoritative_evidence_proves_a_worker_ended(monkeypatch, evidence, proved):
    worker = str(uuid.uuid4())
    record = {"current_worker": worker, "closed": False, **evidence.get("record", {})}
    monkeypatch.setattr(native_state, "read_session", lambda _sid: record)
    phase = evidence.get("phase", "ready")
    monkeypatch.setattr(native_state, "read_lifecycle", lambda _w: {"phase": phase})
    monkeypatch.setattr(native_runtime, "_proved_gone", lambda _w: evidence.get("gone", False))
    assert native_runtime._exit_proved(str(uuid.uuid4()), worker) is proved


def test_a_retry_refused_before_stdin_chains_instead_of_ending_recovery(conversation):
    """#1538 review: a Send again claimed DIRECT (an idle worker) can still settle `not_sent`
    before stdin. The original is history (its retry exists) but not `retried`; the failed retry
    carries its own `not_sent` with ITS derived id, so the chain is recoverable at its newest
    link and no failed claim's id is ever submitted again."""
    root, sid, binding = conversation
    original = _queued(root, sid, binding)
    native_journal.record_handoff(root, binding, original, "not_sent", reason_code="worker_ended")
    first = native_journal.retry_id(original)
    attempt = request(binding, operation_id=first, revision=native_journal.read(root, sid).revision)
    native_journal.claim(root, attempt)  # direct: nothing queued it
    native_journal.record_handoff(root, binding, first, "not_sent", reason_code="unexpected")
    _, turns = _view(root, sid, binding)
    assert turns[original]["not_sent"] == {
        "code": "worker_ended",
        "retry": first,
        "retry_recorded": True,
        "retried": False,
    }
    second = native_journal.retry_id(first)
    assert second not in {original, first}
    assert turns[first]["not_sent"] == {
        "code": "unexpected",
        "retry": second,
        "retry_recorded": False,
        "retried": False,
    }
    # The next link goes through: the failed retry is now `retried`, the original unchanged.
    attempt = request(
        binding, operation_id=second, revision=native_journal.read(root, sid).revision
    )
    native_journal.claim(root, attempt)
    native_journal.record_handoff(root, binding, second, "sent")
    _, turns = _view(root, sid, binding)
    assert turns[first]["not_sent"]["retried"] is True
    assert turns[original]["not_sent"]["retried"] is False
    assert "not_sent" not in turns[second]


@pytest.mark.anyio
@pytest.mark.parametrize("queued", [False, True])
async def test_a_submit_refused_at_its_own_handoff_records_a_reason_direct_or_queued(
    tmp_path, monkeypatch, caplog, queued
):
    """A direct submit (a Send again to an idle worker) refused before stdin is a proved
    non-send like a queued one: it records `unexpected` and logs the type name only."""
    recorded = []
    worker = _dispatcher(tmp_path, RuntimeError("unused"), monkeypatch, recorded)

    def refuse(action, params, *, now=False):
        raise native_protocol.ProtocolError("/home/x/uploads/secret.png changed")

    worker.frame = refuse
    operation = str(uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="agent_sessions.native_worker"):
        await worker.handoff("submit", {"operation_id": operation, "text": "x"}, queued=queued)
    assert recorded == [(operation, "not_sent", {"reason_code": "unexpected"})]
    assert "error=ProtocolError" in caplog.text and "secret" not in caplog.text
