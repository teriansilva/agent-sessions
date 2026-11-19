"""A worker from an older release asks the installed release to admit its queued messages (#2165).

API-session workers outlive app updates. A worker launched from an older release rebuilt the
roster with its older, strict manifest parser, which rejected a field a newer release had
committed; every queued message and Send now was then refused `agent_removed`. Now a worker that
is not the installed release has the installed release decide, in a short-lived child.

The install is a fake ``<home>/releases/{old,new,newer}`` with ``current -> releases/new``; this
worker's package is reported as the OLD release's (`native_admit._package_file`). Each release's
``venv/bin/python`` is a small script (`WRAPPER`) that records which release ran, then either
execs this test run's interpreter with the argv it was given (the REAL helper,
``-I -m agent_sessions.native_admit``, from this checkout) or answers in a scripted way (a
timeout, malformed output, another protocol version, ...). The admission identity a REAL helper
checks comes from a real claude-api session launched through `test_native_runtime`'s host: its
ownership ledger row and its launch config.

What is simulated, and how: the worker's OWN (older) roster is the test process's in-process
roster. "The older parser rejects the newer field" is a stand-in `registry.snapshot_scope` that
yields the roster as such a parser built it (no `claude`, no `claude-api`); "the older code lacks
a newer restriction" is an in-process `admit_queued` that admits anything. The installed release's
side is never patched: it is the real helper in its own process. "Pausing the real helper" is the
wrapper holding the helper's process until the test releases it, before it execs the real check.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sys
import time
import uuid
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import test_native_journal as journal_tests
import test_native_runtime as rt
from agent_sessions import native_admit, native_ipc, native_journal, native_state, native_worker
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import manager, plugin_state_home, roster_state, storage
from test_native_protocol import OPERATION, codex_running, codex_session

host, project = rt.host, rt.project
conversation = journal_tests.conversation


@pytest.fixture
def anyio_backend():
    return "asyncio"


WRAPPER = """#!{python}
import os, sys, time
from pathlib import Path

release = Path(__file__).parents[2]
with open({log!r}, "a") as fh:
    fh.write(" ".join([release.name, str(os.getpid()), *sys.argv[1:]]) + "\\n")
mode = (release / "mode").read_text() if (release / "mode").exists() else "real"
if mode == "pause":
    end = time.monotonic() + 30
    while not (release / "go").exists() and time.monotonic() < end:
        time.sleep(0.01)
    mode = "real"
if mode == "real":
    os.execv({python!r}, [{python!r}, *sys.argv[1:]])
sys.stdin.buffer.read()
if mode == "sleep":
    time.sleep(60)
answers = {{
    "garbage": b"admitted, probably",
    "v2": b'{{"v": 2, "ok": true}}',
    "bogus": b'{{"v": 1, "refused": "agent_vanished"}}',
    "closed": b'{{"v": 1, "refused": "generation_closed"}}',
    "extra": b'{{"v": 1, "ok": true, "also": 1}}',
    "newline": b'{{"v": 1, "ok": true}}\\n',
    "loud": b'{{"v": 1, "ok": true}}' + b" " * 8192,
    "failing": b'{{"v": 1, "ok": true}}',
    "silent": b"",
}}
sys.stdout.buffer.write(answers[mode])
sys.stdout.flush()
sys.exit(1 if mode == "failing" else 0)
"""


@pytest.fixture
def install(tmp_path, monkeypatch):
    home = native_admit.install_home()  # conftest pins AGENT_SESSIONS_HOME under tmp_path
    log = tmp_path / "helpers.log"
    log.touch()
    for name in ("old", "new", "newer"):
        python = home / "releases" / name / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(WRAPPER.format(python=sys.executable, log=str(log)))
        python.chmod(0o755)
    (home / "current").symlink_to(home / "releases" / "new")
    package = home / "releases" / "old" / "venv" / "lib" / "site-packages" / "agent_sessions"
    monkeypatch.setattr(native_admit, "_package_file", lambda: package / "__init__.py")
    return SimpleNamespace(home=home, log=log)


def mode(install, release: str, value: str) -> None:
    (install.home / "releases" / release / "mode").write_text(value)


def ran(install) -> list[list[str]]:
    return [line.split() for line in install.log.read_text().splitlines()]


def gone(pid: int) -> bool:
    """Reaped: a zombie child still has its /proc entry."""
    return not Path(f"/proc/{pid}").exists()


async def started(install, count: int = 1) -> list[str]:
    for _ in range(500):
        lines = ran(install)
        if len(lines) >= count:
            return lines[count - 1]
        await asyncio.sleep(0.01)
    raise AssertionError("the helper never started")


async def real_session(project) -> dict:
    """A launched claude-api session: what a real helper admits (its ledger row, its config)."""
    created = await runtime.create_session("claude-api", str(project), operation_id=rt.ident())
    key = created["session_key"]
    config = native_state.read_config(
        native_state.read_session(key.partition(":")[2])["current_worker"]
    )
    return {
        "session_key": key,
        "adapter": config["adapter"],
        **{k: config[k] for k in ("source_engine", "binary", "entrypoint")},
    }


DUMMY = {
    "session_key": f"claude-api:{uuid.uuid4()}",
    "adapter": "claude-stream-json",
    "source_engine": "claude",
    "binary": "/nonexistent/claude",
}


def older_parser(monkeypatch) -> list:
    """The worker's own roster as the older parser built it: `claude`'s manifest "did not
    load", so `claude` and `claude-api` are absent. Returns the list of times it was read."""
    reads = []

    @contextlib.contextmanager
    def scope(**_kw):
        reads.append(True)
        yield SimpleNamespace(by_id=MappingProxyType({}), generation=1)

    monkeypatch.setattr(registry, "snapshot_scope", scope)
    return reads


def make_worker(conversation, admits_as: dict, *, running: bool):
    root, _sid, binding = conversation
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
    worker.codec = codex_running() if running else codex_session()
    worker.ready.set()
    worker.gate_open = lambda: True
    worker.report = lambda **_fields: None
    # The identity the admission checks: the real session's (or a dummy one).
    worker.session_key, worker.adapter = admits_as["session_key"], admits_as["adapter"]
    worker.config.update(
        {k: v for k, v in admits_as.items() if k not in ("session_key", "adapter")}
    )
    worker.writes = []

    async def write(frame):
        worker.writes.append(frame)

    worker.write = write
    return worker


def queue_one(conversation, worker, text="merge it") -> dict:
    root, sid, binding = conversation
    queued = journal_tests.request(
        binding, revision=native_journal.read(root, sid).revision, text=text
    )
    native_journal.claim(root, queued, queued=True)
    worker.queued.append(queued["params"])
    return queued["params"]


def handoff_of(conversation, operation_id):
    return native_journal.read(*conversation[:2]).operations[operation_id]


async def run_fifo(conversation, worker, params, *, settle=True):
    """FIFO dispatch until ``params`` left `queued` (or, ``settle=False``, a short while)."""
    worker.queue_wake.set()
    task = asyncio.create_task(worker.dispatch_queue())
    for _ in range(1500 if settle else 30):
        if handoff_of(conversation, params["operation_id"]).handoff != "queued":
            break
        await asyncio.sleep(0.01)
    worker.done.set()
    worker.queue_wake.set()
    await asyncio.wait_for(task, 10)
    return handoff_of(conversation, params["operation_id"])


def send_now_worker(conversation, admits_as, *, behind=0):
    root, sid, binding = conversation
    worker = make_worker(conversation, admits_as, running=True)
    native_journal.claim(root, journal_tests.request(binding, operation_id=OPERATION))
    queued = queue_one(conversation, worker)
    for n in range(behind):
        queue_one(conversation, worker, f"behind it {n}")
    control = journal_tests.request(
        binding,
        action="send_now",
        turn_id=OPERATION,
        queued_turn_id=queued["operation_id"],
        mode="steer",
        revision=native_journal.read(root, sid).revision,
    )
    return worker, queued, control


async def deliver(path, conversation, admits_as) -> tuple[str, list]:
    """The queued message, through FIFO dispatch or Send now: its handoff (or Send now's refusal
    code) and the frames written to the agent."""
    if path == "fifo":
        worker = make_worker(conversation, admits_as, running=False)
        queued = queue_one(conversation, worker)
        op = await run_fifo(conversation, worker, queued)
        return (op.reason_code if op.handoff == "not_sent" else op.handoff), worker.writes
    worker, queued, control = send_now_worker(conversation, admits_as)
    result = await worker.effect(control)
    if "error" in result:
        assert worker.queued == [queued], "a refused Send now keeps the message queued"
        message = result["error"]["message"]
        code = next(c for c, m in native_worker.AdmissionRefused.MESSAGES.items() if m == message)
        return code, worker.writes
    return handoff_of(conversation, queued["operation_id"]).handoff, worker.writes


# --- contract 1: when the worker delegates -------------------------------------------------------


def test_a_worker_that_is_not_installed_admits_in_process(tmp_path):
    assert native_admit.plan().mode == native_admit.IN_PROCESS  # this checkout is no release


def test_the_installed_release_itself_admits_in_process(install):
    (install.home / "current").unlink()
    (install.home / "current").symlink_to(install.home / "releases" / "old")
    assert native_admit.plan().mode == native_admit.IN_PROCESS


def test_another_installed_release_decides_from_its_resolved_directory(install):
    where = native_admit.plan()
    assert where.mode == native_admit.DELEGATE
    assert where.release == (install.home / "releases" / "new").resolve()
    assert where.argv() == [
        str(where.release / "venv" / "bin" / "python"),
        "-I",
        "-m",
        "agent_sessions.native_admit",
    ]


@pytest.mark.parametrize("pointer", ["missing", "dangling", "outside", "file"])
def test_a_broken_installed_pointer_refuses_never_the_stale_parser(install, tmp_path, pointer):
    current = install.home / "current"
    current.unlink()
    if pointer == "dangling":
        current.symlink_to(install.home / "releases" / "gone")
    elif pointer == "outside":
        (tmp_path / "elsewhere").mkdir()
        current.symlink_to(tmp_path / "elsewhere")
    elif pointer == "file":
        (install.home / "releases" / "stray").write_text("")
        current.symlink_to(install.home / "releases" / "stray")
    assert native_admit.plan().mode == native_admit.BROKEN


# --- contract 3: the exact protocol --------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    ["garbage", "v2", "bogus", "closed", "extra", "newline", "loud", "failing", "silent", "sleep"],
)
async def test_anything_but_an_exact_answer_could_not_be_checked(install, monkeypatch, answer):
    monkeypatch.setattr(native_admit, "DEADLINE", 2.0)  # only the timeout waits it out
    mode(install, "new", answer)
    began = time.monotonic()
    with pytest.raises(native_admit.Unverified):
        await native_admit.ask(native_admit.plan(), DUMMY["session_key"], "claude-stream-json", {})
    assert time.monotonic() - began < native_admit.DEADLINE + 1
    _release, pid, *_ = ran(install)[0]
    assert gone(int(pid)), "the helper was not reaped"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"v": 2, "session_key": "k", "adapter": "a", "config": {}}',
        b'{"v": true, "session_key": "k", "adapter": "a", "config": {}}',
        b'{"v": 1, "session_key": "k", "adapter": "a", "config": []}',
        b'{"v": 1, "session_key": "k", "adapter": "a", "config": {}, "more": 1}',
    ],
)
def test_the_helper_refuses_a_request_outside_the_protocol(raw):
    assert native_admit._request(raw) is None


# --- through FIFO dispatch and Send now, with the REAL helper ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["fifo", "send_now"])
async def test_a_field_only_the_installed_release_knows_is_admitted(
    install, host, project, conversation, monkeypatch, path
):
    """The live failure: the worker's own parser rejects a field the installed release
    committed, so its roster says `claude-api` was removed. The installed release admits."""
    session = await real_session(project)
    own_roster = older_parser(monkeypatch)
    handoff, writes = await deliver(path, conversation, session)
    assert handoff == "sent" and len(writes) == 1
    assert own_roster == [], "the older parser decided"
    assert [line[0] for line in ran(install)] == ["new"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["fifo", "send_now"])
@pytest.mark.parametrize("change", ["removed", "changed", "owner"])
async def test_a_real_removal_change_or_owner_change_still_refuses(
    install, host, project, conversation, path, change
):
    session = await real_session(project)
    native = session["session_key"].partition(":")[2]
    expected, session = {
        "removed": ("agent_removed", {**session, "session_key": f"gone-api:{native}"}),
        "changed": ("agent_changed", {**session, "source_engine": "codex"}),
        "owner": ("source_store_changed", {**session, "session_key": f"claude-api:{uuid.uuid4()}"}),
    }[change]
    handoff, writes = await deliver(path, conversation, session)
    assert (handoff, writes) == (expected, [])


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["fifo", "send_now"])
async def test_a_newer_restriction_still_refuses_an_old_worker(
    install, host, project, conversation, monkeypatch, path
):
    """The older code would admit (it lacks the restriction); the installed release refuses
    (here: the binary is no longer selected the way it was at launch)."""
    session = await real_session(project)
    monkeypatch.setattr(native_worker, "admit_queued", lambda *_a: None)
    moved = {**session, "entrypoint": {**session["entrypoint"], "selector": "/elsewhere/claude"}}
    handoff, writes = await deliver(path, conversation, moved)
    assert (handoff, writes) == ("agent_changed", [])


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["fifo", "send_now"])
@pytest.mark.parametrize("failure", ["sleep", "garbage", "v2", "bogus", "broken"])
async def test_a_check_that_fails_refuses_before_any_agent_write(
    install, conversation, caplog, monkeypatch, path, failure
):
    monkeypatch.setattr(native_admit, "DEADLINE", 2.0)  # only the timeout waits it out
    if failure == "broken":
        (install.home / "current").unlink()
    else:
        mode(install, "new", failure)
    handoff, writes = await deliver(path, conversation, DUMMY)
    assert (handoff, writes) == ("agent_changed", [])
    causes = [r.getMessage() for r in caplog.records if "could not be checked" in r.getMessage()]
    assert len(causes) == 1 and "cause=" in causes[0]
    assert "nonexistent" not in causes[0] and "admitted, probably" not in causes[0]


def test_the_refusal_reads_back_on_an_older_readers_closed_set(conversation, monkeypatch):
    """Contract 4: "could not be checked" is the existing `agent_changed`, which a reader from
    before #1897 (the set #1538 shipped) still accepts."""
    root, sid, binding = conversation
    queued = journal_tests.request(binding)
    native_journal.claim(root, queued, queued=True)
    native_journal.record_handoff(
        root, binding, queued["params"]["operation_id"], "not_sent", reason_code="agent_changed"
    )
    older = frozenset(
        {
            "agent_removed",
            "agent_changed",
            "source_store_changed",
            "generation_closed",
            "question_replaced",
            "worker_ended",
            "unexpected",
        }
    )
    assert native_admit.UNVERIFIED in older and native_admit.CODES <= older
    assert native_ipc.REASON_CODES >= native_admit.CODES  # no new code
    monkeypatch.setattr(native_journal, "REASON_CODES", older)
    op = native_journal.read(root, sid).operations[queued["params"]["operation_id"]]
    assert (op.handoff, op.reason_code) == ("not_sent", "agent_changed")


# --- contract 2: the helper persists nothing -----------------------------------------------------


def _tree(*roots: Path) -> dict[str, str]:
    out = {}
    for root in roots:
        for path in sorted(root.rglob("*")) if root.exists() else ():
            if path.is_file():
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.mark.anyio
async def test_the_shared_state_is_byte_identical_after_a_check(install, host, project):
    session = await real_session(project)
    # State a reconciling reload WOULD write: a revisioned manager document without its marker,
    # and a recorded copy of an agent that is no longer present.
    with storage.locked(manager.DOCUMENT) as path:
        storage.write(
            path,
            {
                "contract": 1,
                "plugins": {},
                "operations": {},
                "reviews": {},
                "roster_revision": str(uuid.uuid4()),
            },
        )
    with contextlib.suppress(FileNotFoundError):
        (path.parent / manager.REVISION_MARKER).unlink()
    recorded = roster_state.load()
    recorded.manifests["vanished"] = ("plugin.toml", b"not a manifest")
    roster_state.save(recorded)
    roots = (plugin_state_home(), native_state.root())
    before = _tree(*roots)
    code = await native_admit.ask(
        native_admit.plan(), session["session_key"], session["adapter"], session
    )
    assert code is None, "the real helper did not admit the real session"
    assert _tree(*roots) == before


def test_the_helper_process_takes_no_lock_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(storage, "_PERSIST_NOTHING", False)
    storage.persist_nothing()
    with pytest.raises(storage.StateError):
        with storage.locked("launch", wait=0):
            pass
    with pytest.raises(storage.StateError):
        storage.write(storage.root() / "x.json", {})
    with pytest.raises(ValueError):
        roster_state.save(roster_state.RosterState())


# --- contract 5: bounded, non-blocking, preemptible ----------------------------------------------


@pytest.mark.anyio
async def test_stop_and_ipc_answer_while_a_check_is_pending(install, conversation):
    mode(install, "new", "pause")
    _root, _sid, binding = conversation
    worker = make_worker(conversation, DUMMY, running=False)
    queued = queue_one(conversation, worker)
    worker.queue_wake.set()
    task = asyncio.create_task(worker.dispatch_queue())
    _release, pid, *_ = await started(install)
    probe = journal_tests.request(binding, action="probe", target_worker_id=binding.worker_id)
    answered = await asyncio.wait_for(worker.read_only(probe), 1)
    assert answered["result"] == {"containment": "live"}
    stop = journal_tests.request(binding, action="stop")
    began = time.monotonic()
    await asyncio.wait_for(worker.effect(stop), native_admit.DEADLINE)
    assert time.monotonic() - began < 1.5, "Stop waited for the admission check"
    worker.stop_child()  # as `serve_client` does once the Stop is answered
    worker.queue_wake.set()
    await asyncio.wait_for(task, 5)
    assert gone(int(pid)) and worker.writes == []
    assert handoff_of(conversation, queued["operation_id"]).handoff == "queued"


@pytest.mark.anyio
async def test_cancellation_mid_check_reaps_the_helper_and_releases_the_locks(
    install, conversation
):
    mode(install, "new", "pause")
    worker = make_worker(conversation, DUMMY, running=False)
    queued = queue_one(conversation, worker)
    worker.queue_wake.set()
    task = asyncio.create_task(worker.dispatch_queue())
    _release, pid, *_ = await started(install)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert gone(int(pid)) and worker.writes == []
    with native_state.session_lock(worker.session_id, wait=0), storage.locked("launch", wait=0):
        pass
    assert handoff_of(conversation, queued["operation_id"]).handoff == "queued"


@pytest.mark.anyio
@pytest.mark.parametrize("stale", ["head", "busy"])
async def test_fifo_admitted_after_the_request_went_stale_writes_nothing(
    install, host, project, conversation, stale
):
    """The real helper admits, but only after the queue head moved (the reader promoted
    another message) or a turn started meanwhile: the admitted request is stale, and nothing of
    it is written. A moved head is dispatched on its own, freshly admitted."""
    session = await real_session(project)
    mode(install, "new", "pause")
    worker = make_worker(conversation, session, running=False)
    head = queue_one(conversation, worker, "STALE-HEAD-TEXT")
    second = queue_one(conversation, worker, "NEW-HEAD-TEXT")
    worker.queue_wake.set()
    task = asyncio.create_task(worker.dispatch_queue())
    await started(install)
    if stale == "head":
        worker.queued.insert(0, worker.queued.pop())
    else:
        worker.codec.operation_id = "a-turn-that-started-meanwhile"
    (install.home / "releases" / "new" / "go").touch()
    for _ in range(1500):
        if stale == "busy" and len(ran(install)) == 1 and not worker.effects.locked():
            break
        if stale == "head" and handoff_of(conversation, second["operation_id"]).handoff != "queued":
            break
        await asyncio.sleep(0.01)
    worker.done.set()
    worker.queue_wake.set()
    await asyncio.wait_for(task, 10)
    assert handoff_of(conversation, head["operation_id"]).handoff == "queued"
    assert "STALE-HEAD-TEXT" not in repr(worker.writes)
    if stale == "head":
        assert "NEW-HEAD-TEXT" in repr(worker.writes) and worker.queued == [head]
    else:
        assert worker.writes == [] and worker.queued == [head, second]


@pytest.mark.anyio
@pytest.mark.parametrize("stale", ["target", "head", "stop"])
async def test_send_now_admitted_after_the_request_went_stale_writes_nothing(
    install, host, project, conversation, stale
):
    session = await real_session(project)
    mode(install, "new", "pause")
    worker, queued, control = send_now_worker(conversation, session, behind=1)
    other = worker.queued[1]
    effect = asyncio.create_task(worker.effect(control))
    _release, pid, *_ = await started(install)
    if stale == "target":
        worker.codec.operation_id = None  # the turn it was to cut ended meanwhile
    elif stale == "head":
        worker.queued.insert(0, worker.queued.pop())
    else:
        worker.stop_child()  # the worker is ending: the check is preempted
    (install.home / "releases" / "new" / "go").touch()
    result = await asyncio.wait_for(effect, 10)
    assert worker.writes == []
    assert handoff_of(conversation, queued["operation_id"]).handoff == "queued"
    assert sorted(p["operation_id"] for p in worker.queued) == sorted(
        [queued["operation_id"], other["operation_id"]]
    )
    if stale == "stop":
        assert result["error"]["code"] == "busy" and gone(int(pid))
    else:
        assert result["result"]["handoff"] == "not_sent"


@pytest.mark.anyio
async def test_a_swap_of_current_during_a_check_runs_the_release_resolved_at_detection(
    install, host, project, conversation, monkeypatch
):
    session = await real_session(project)
    detect = native_admit.plan

    def detect_then_swap():
        where = detect()
        current = install.home / "current"
        current.unlink()
        current.symlink_to(install.home / "releases" / "newer")  # an update lands now
        mode(install, "newer", "garbage")
        return where

    monkeypatch.setattr(native_admit, "plan", detect_then_swap)
    handoff, writes = await deliver("fifo", conversation, session)
    assert handoff == "sent" and len(writes) == 1
    assert [line[0] for line in ran(install)] == ["new"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["fifo", "send_now"])
async def test_lock_contention_stays_busy_and_starts_no_helper(install, conversation, path):
    with storage.locked("launch", wait=0):  # another session's launch
        if path == "fifo":
            worker = make_worker(conversation, DUMMY, running=False)
            queued = queue_one(conversation, worker)
            op = await run_fifo(conversation, worker, queued, settle=False)
            assert op.handoff == "queued" and worker.queued == [queued]
        else:
            worker, queued, control = send_now_worker(conversation, DUMMY)
            result = await worker.effect(control)
            assert result["error"]["code"] == "busy" and worker.queued == [queued]
    assert ran(install) == [] and worker.writes == []


# --- the shell-free guarantee --------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_helper_runs_a_literal_argv_never_a_shell(install, monkeypatch):
    seen = {}

    async def spawn(*argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        raise FileNotFoundError("stop here: the argv is what this test is about")

    monkeypatch.setattr(native_admit.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(native_admit.Unverified):
        await native_admit.ask(native_admit.plan(), DUMMY["session_key"], "x", {})
    release = (install.home / "releases" / "new").resolve()
    assert list(seen["argv"]) == [
        str(release / "venv" / "bin" / "python"),
        "-I",
        "-m",
        "agent_sessions.native_admit",
    ]
    assert all(isinstance(a, str) for a in seen["argv"])
    assert "shell" not in seen["kw"] and seen["kw"]["stderr"] == asyncio.subprocess.DEVNULL
    source = Path(native_admit.__file__).read_text()
    for forbidden in ("shell=True", "create_subprocess_shell", "os.system(", "bash -c"):
        assert forbidden not in source
    assert os.path.basename(seen["argv"][0]) == "python"
