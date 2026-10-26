"""Memory pressure of a native worker's own unit (#1643): sampled by the worker from its own
cgroup, levelled with hysteresis, shown only while fresh and pinned, cleared on any doubt."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

import test_native_journal as journal_tests
import test_session_running_counts as running_tests
from agent_sessions import (
    native_containment,
    native_ipc,
    native_memory,
    native_runtime,
    native_state,
)
from agent_sessions import native_journal as journal
from agent_sessions.native_memory import Group, Sample, Tracker
from agent_sessions.native_worker import Worker

GIB = 1024**3
conversation = journal_tests.conversation
INVOCATION = "d4d3d996ed7949e38104d854a4a3e2cb"
UNIT = "battlelab-native-8f14e45fceea467a95757a6f2e8f0b2a.service"
GROUP = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}"


# --- whose group ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "group"),
    [
        (f"0::{GROUP}\n", GROUP),
        (f"12:pids:/x\n0::{GROUP}\n", GROUP),  # hybrid hosts list v1 lines too
        ("0::/user.slice/app.slice/battlelab-native-0000.service\n", None),  # another unit
        (f"0::{GROUP}/../{UNIT}\n", None),
        (f"1:name=systemd:{GROUP}\n", None),  # v1 only: no unified path
        ("", None),
    ],
)
def test_only_the_workers_own_unit_is_its_group(text, group):
    assert native_memory.own_group(text, UNIT) == group


# --- one reading ---------------------------------------------------------------------------------


def _group(root, *, current=6 * GIB, maximum=str(8 * GIB), inactive=GIB, high=0, drop=None):
    directory = root / GROUP.lstrip("/")
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "memory.current": f"{current}\n",
        "memory.max": f"{maximum}\n",
        "memory.stat": f"anon 123\ninactive_file {inactive}\nactive_file 9\n",
        "memory.events": f"low 0\nhigh {high}\nmax 0\noom 0\noom_kill 0\n",
    }
    for name, text in files.items():
        if name != drop:
            (directory / name).write_text(text)
    return root


def test_the_working_set_excludes_reclaimable_page_cache(tmp_path):
    reading = native_memory.sample(GROUP, _group(tmp_path))
    assert (reading.working_set, reading.maximum, reading.groups[0].throttled, reading.scope) == (
        5 * GIB,
        8 * GIB,
        0,
        "session",
    )


def _parent(root, name, *, maximum, current=None, inactive=0, high=0, local_high=0):
    """An ancestor: `high` is its hierarchical counter (siblings' events included), `local_high`
    its own limit's (`memory.events.local`)."""
    directory = root / GROUP.lstrip("/").rsplit("/", 1)[0]
    for _ in range(["app.slice", "user@1000.service"].index(name)):
        directory = directory.parent
    (directory / "memory.max").write_text(f"{maximum}\n")
    if current is not None:
        (directory / "memory.current").write_text(f"{current}\n")
        (directory / "memory.stat").write_text(f"inactive_file {inactive}\n")
        (directory / "memory.events").write_text(f"high {high}\n")
        (directory / "memory.events.local").write_text(f"high {local_high}\n")
    return directory


def shape(reading):
    return reading.working_set, reading.maximum, reading.scope


def test_a_tighter_parent_limit_is_the_one_measured_and_says_it_is_shared(tmp_path):
    # Own cap 8 GiB, 5 GiB used; the parent slice is capped at 2 GiB with 1.9 GiB used.
    root = _group(tmp_path)
    parent = _parent(root, "app.slice", maximum=2 * GIB, current=int(1.9 * GIB))
    reading = native_memory.sample(GROUP, root)
    assert shape(reading) == (int(1.9 * GIB), 2 * GIB, "shared")
    assert reading.source == str(parent)
    assert Tracker().update(reading) == "critical"


def test_an_unlimited_parent_is_skipped_and_a_looser_one_changes_nothing(tmp_path):
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum="max")
    _parent(root, "user@1000.service", maximum=64 * GIB, current=10 * GIB)
    assert shape(native_memory.sample(GROUP, root)) == (5 * GIB, 8 * GIB, "session")


def test_a_parent_limit_whose_usage_cannot_be_read_is_never_healthy(tmp_path):
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=2 * GIB)  # a limit, no usage files
    assert native_memory.sample(GROUP, root) is None


def test_a_siblings_throttling_at_a_looser_parent_is_not_this_sessions(tmp_path):
    # Hermes on #1651: worker 5/8 GiB, parent 1/64 GiB. A sibling's own limit throttles: the
    # parent's HIERARCHICAL counter rises, its own limit's (`.local`) does not. Not this session.
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=64 * GIB, current=GIB, high=3, local_high=0)
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    _parent(root, "app.slice", maximum=64 * GIB, current=GIB, high=9, local_high=0)
    assert tracker.update(native_memory.sample(GROUP, root)) == "normal"


def test_a_parent_throttling_at_its_own_limit_holds_this_session_back_too(tmp_path):
    # Its own limit's events rise: the kernel reclaims across the whole group, this session
    # included, whatever the ratios say. Critical, about the shared group, with its figures.
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=64 * GIB, current=GIB, local_high=3)
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    _parent(root, "app.slice", maximum=64 * GIB, current=GIB, local_high=4)
    assert tracker.update(native_memory.sample(GROUP, root)) == "critical"
    memory = native_memory.state(tracker.level, tracker.shown, INVOCATION, tracker.throttled)
    assert (memory["scope"], memory["throttled"]) == ("session", "shared")


def test_throttling_at_the_limiting_parent_is_read_from_its_own_events_only(tmp_path):
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=2 * GIB, current=int(1.4 * GIB), high=3, local_high=0)
    tracker = Tracker()
    assert (
        tracker.update(native_memory.sample(GROUP, root)) == "normal"
    )  # 70% of 2 GiB: the tighter group
    # Hierarchical only (a descendant's own limit): not the shared limit throttling.
    _parent(root, "app.slice", maximum=2 * GIB, current=int(1.4 * GIB), high=9, local_high=0)
    assert tracker.update(native_memory.sample(GROUP, root)) == "normal"
    # Its own limit's events: the group this session shares is being held back.
    _parent(root, "app.slice", maximum=2 * GIB, current=int(1.4 * GIB), high=9, local_high=1)
    reading = native_memory.sample(GROUP, root)
    assert tracker.update(reading) == "critical" and reading.scope == "shared"


def test_the_sessions_own_throttling_counts_under_a_busier_parent(tmp_path):
    # Hermes on #1651: worker 5/8 GiB (62%), parent 8.2/10 GiB (82%, the tightest ratio). The
    # worker's OWN limit throttles between samples: that is this session being held back, so it
    # reads critical, and says so as evidence beside the most urgent group's figures.
    root = _group(tmp_path, high=4)
    _parent(root, "app.slice", maximum=10 * GIB, current=int(8.2 * GIB), local_high=0)
    tracker = Tracker()
    assert tracker.update(native_memory.sample(GROUP, root)) == "high"  # the parent's 82%
    _group(tmp_path, high=5)
    assert tracker.update(native_memory.sample(GROUP, root)) == "critical"
    memory = native_memory.state(tracker.level, tracker.shown, INVOCATION, tracker.throttled)
    assert (memory["max"], memory["scope"], memory["throttled"]) == (10 * GIB, "shared", "session")


def test_a_shared_group_near_its_cap_is_never_hidden_by_the_sessions_throttling(tmp_path):
    # Hermes r6 on #1651: worker 5/8 GiB throttling at its own limit, parent at 9.5/10 GiB. The
    # warning states the parent's 95% (the most urgent) AND that the session is throttled.
    root = _group(tmp_path, high=1)
    _parent(root, "app.slice", maximum=10 * GIB, current=int(9.5 * GIB))
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    _group(tmp_path, high=2)
    assert tracker.update(native_memory.sample(GROUP, root)) == "critical"
    memory = native_memory.state(tracker.level, tracker.shown, INVOCATION, tracker.throttled)
    assert memory["working_set"] == int(9.5 * GIB) and memory["max"] == 10 * GIB
    assert (memory["scope"], memory["throttled"]) == ("shared", "session")


def test_a_group_near_its_cap_without_throttle_evidence_claims_no_throttling(tmp_path):
    # Hermes r6: a parent at 95% of its hard cap, its own `high` counter unchanged. Critical by
    # closeness to the cap — and `throttled` stays None, so no copy says the kernel holds it.
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=10 * GIB, current=int(9.5 * GIB), local_high=7)
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    assert tracker.update(native_memory.sample(GROUP, root)) == "critical"
    memory = native_memory.state(tracker.level, tracker.shown, INVOCATION, tracker.throttled)
    assert (memory["scope"], memory["throttled"]) == ("shared", None)


def test_without_throttling_the_tightest_groups_figures_are_shown(tmp_path):
    root = _group(tmp_path)
    _parent(root, "app.slice", maximum=10 * GIB, current=int(8.2 * GIB))
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    memory = native_memory.state(tracker.level, tracker.shown, INVOCATION)
    assert (memory["max"], memory["scope"]) == (10 * GIB, "shared")


def test_a_change_of_limiting_group_is_a_fresh_baseline_not_throttling(tmp_path):
    root = _group(tmp_path, high=50)  # the worker's own counter
    tracker = Tracker()
    assert tracker.update(native_memory.sample(GROUP, root)) == "normal"
    _parent(root, "app.slice", maximum=12 * GIB, current=int(8.5 * GIB), local_high=60)
    assert tracker.update(native_memory.sample(GROUP, root)) == "normal"  # 71% of a new group


def test_the_own_units_throttling_still_counts(tmp_path):
    root = _group(tmp_path, high=1)
    tracker = Tracker()
    tracker.update(native_memory.sample(GROUP, root))
    _group(tmp_path, high=2)
    assert tracker.update(native_memory.sample(GROUP, root)) == "critical"


# --- levels --------------------------------------------------------------------------------------


def reading(ratio, throttled=0):
    return Sample((Group("/own", int(ratio * 1000), 1000, throttled, own=True),))


@pytest.mark.parametrize(
    ("ratio", "level"), [(0.10, "normal"), (0.749, "normal"), (0.75, "high"), (0.90, "critical")]
)
def test_levels_rise_at_their_thresholds(ratio, level):
    assert Tracker().update(reading(ratio)) == level


def test_throttling_at_the_soft_limit_is_critical_whatever_the_ratio():
    tracker = Tracker()
    assert tracker.update(reading(0.5, throttled=3)) == "normal"  # a first count proves nothing
    assert tracker.update(reading(0.5, throttled=4)) == "critical"


def test_a_level_falls_only_after_three_clearly_lower_samples():
    tracker = Tracker()
    assert tracker.update(reading(0.80)) == "high"
    # Just under the threshold, inside the margin: it stays.
    assert [tracker.update(reading(0.72)) for _ in range(5)] == ["high"] * 5
    # Clearly lower: two quiet samples are not enough, the third is.
    assert [tracker.update(reading(0.60)) for _ in range(3)] == ["high", "high", "normal"]


def test_a_rise_resets_the_quiet_count():
    tracker = Tracker()
    tracker.update(reading(0.95))
    tracker.update(reading(0.50))
    tracker.update(reading(0.50))
    assert tracker.update(reading(0.95)) == "critical"
    assert [tracker.update(reading(0.50)) for _ in range(3)][-1] == "normal"


def test_a_failed_read_after_a_warning_is_unavailable_at_once():
    tracker = Tracker()
    assert tracker.update(reading(0.95)) == "critical"
    assert tracker.update(None) == "unavailable"
    assert tracker.update(reading(0.50)) == "normal"  # a fresh start, not a fall


# --- who may show it -----------------------------------------------------------------------------


def life(level="high", *, at=0.0, invocation=INVOCATION, phase="ready", **extra):
    """``at``: seconds from now, resolved by `public` callers at run time (see `shown`)."""
    return {
        "phase": phase,
        "invocation_id": INVOCATION,
        "memory": {
            "level": level,
            "working_set": 6 * GIB,
            "max": 8 * GIB,
            "at": at,
            "invocation_id": invocation,
        },
        **extra,
    }


def shown(doc):
    """`public` with the sample's `at` read as an offset from now: collection-time stamps go stale
    on a slow runner."""
    memory = dict(doc.get("memory") or {})
    if "at" in memory:
        memory["at"] = time.time() + memory["at"]
    return native_memory.public({**doc, **({"memory": memory} if "memory" in doc else {})})


def test_a_fresh_pinned_warning_is_shown_with_byte_counts_only():
    assert shown(life("critical")) == {
        "level": "critical",
        "working_set": 6 * GIB,
        "max": 8 * GIB,
        "scope": "session",
        "throttled": None,
    }
    doc = life("high")
    doc["memory"]["scope"] = "shared"
    assert shown(doc)["scope"] == "shared"


@pytest.mark.parametrize(
    "doc",
    [
        life("normal"),
        life("unavailable"),
        life(at=-native_memory.FRESH - 1),  # its worker stopped sampling
        life(at=60),  # from the future: not trusted
        life(invocation="f" * 32),  # another invocation's state
        life(phase="exited"),
        life(phase="gone"),
        life(exit={"result": "oom-kill", "invocation_id": INVOCATION}),
        {"phase": "ready", "invocation_id": INVOCATION},
        {**life(), "invocation_id": None},
    ],
)
def test_anything_stale_foreign_or_ended_shows_nothing(doc):
    assert shown(doc) is None


# --- the worker's sampler ------------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch, tmp_path):
    worker_id = "8f14e45f-ceea-467a-9575-7a6f2e8f0b2a"
    assert native_containment.WorkerIdentity(worker_id).unit == UNIT
    native_state.update_lifecycle(
        worker_id, phase="ready", invocation_id=INVOCATION, control_group=GROUP
    )
    monkeypatch.setattr(native_memory, "CGROUP_ROOT", _group(tmp_path / "cg"))
    monkeypatch.setattr(native_memory.Path, "read_text", _proc(f"0::{GROUP}\n"))
    return SimpleNamespace(worker_id=worker_id, root=tmp_path / "cg")


def _proc(text):
    real = native_memory.Path.read_text

    def read_text(self, *args, **kwargs):
        return text if str(self) == "/proc/self/cgroup" else real(self, *args, **kwargs)

    return read_text


def test_the_worker_reads_its_own_pinned_group(worker):
    memory = Worker.memory_sample(worker, Tracker())
    assert memory["level"] == "normal" and memory["invocation_id"] == INVOCATION
    assert (memory["working_set"], memory["max"]) == (5 * GIB, 8 * GIB)


def test_a_group_that_is_not_the_pinned_one_is_unavailable(worker):
    native_state.update_lifecycle(worker.worker_id, control_group=GROUP + "-other")
    assert Worker.memory_sample(worker, Tracker())["level"] == "unavailable"


def test_before_the_runtime_pins_the_group_nothing_is_read(worker):
    native_state.update_lifecycle(worker.worker_id, control_group=None)
    assert Worker.memory_sample(worker, Tracker())["level"] == "unavailable"


def test_an_unreadable_group_after_a_warning_clears_row_and_pane(worker, monkeypatch):
    _group(worker.root, current=int(7.6 * GIB), inactive=0)
    tracker = Tracker()
    first = Worker.memory_sample(worker, tracker)
    native_state.update_lifecycle(worker.worker_id, memory=first)
    assert first["level"] == "critical"
    assert native_memory.public(native_state.read_lifecycle(worker.worker_id)) is not None
    (worker.root / GROUP.lstrip("/") / "memory.current").unlink()
    native_state.update_lifecycle(worker.worker_id, memory=Worker.memory_sample(worker, tracker))
    assert native_memory.public(native_state.read_lifecycle(worker.worker_id)) is None


def test_the_watch_writes_while_warning_and_on_every_change(worker, monkeypatch):
    levels = iter(["normal", "normal", "high", "high", "normal", "normal"])
    writes, events = [], []

    class Fake:
        worker_id = worker.worker_id
        done = asyncio.Event()

        def memory_sample(self, tracker):
            try:
                level = next(levels)
            except StopIteration:
                self.done.set()
                level = "normal"
            return {"level": level}

        def report(self, **fields):
            writes.append(fields["memory"]["level"])

        def record(self, batch):
            events.extend((e.kind, e.data["level"]) for e in batch)

    monkeypatch.setattr(native_memory, "INTERVAL", 0.001)
    asyncio.run(Worker.memory_watch(Fake()))
    # The first reading, both warning samples (a fresh `at`), the fall; not the repeats.
    assert writes == ["normal", "high", "high", "normal"]
    # The event stream carries each CHANGE of level, once (#1643's `memory_pressure` contract).
    assert events == [
        ("memory_pressure", "normal"),
        ("memory_pressure", "high"),
        ("memory_pressure", "normal"),
    ]


@pytest.mark.parametrize(
    "data",
    [
        {
            "level": "critical",
            "working_set": 7,
            "max": 8,
            "scope": "shared",
            "throttled": "session",
        },
        {
            "level": "unavailable",
            "working_set": None,
            "max": None,
            "scope": None,
            "throttled": None,
        },
        {"level": "high"},
    ],
)
def test_a_memory_pressure_event_passes_the_ipc_boundary(data):
    assert native_ipc.normalize_event({"kind": "memory_pressure", "data": data})["data"] == data


@pytest.mark.parametrize(
    "data",
    [
        {"level": "panic"},
        {"level": "high", "working_set": "7 GB"},
        {"level": "high", "working_set": -1},
        {"level": "high", "scope": "host"},
        {"level": "high", "throttled": True},
        {"level": "high", "note": "free text"},
        {},
    ],
)
def test_anything_else_in_a_memory_pressure_event_is_refused(data):
    with pytest.raises(native_ipc.IPCError):
        native_ipc.normalize_event({"kind": "memory_pressure", "data": data})


def test_the_journal_keeps_memory_pressure_events_and_the_projection_ignores_them(conversation):
    root, sid, binding = conversation
    event = {"kind": "memory_pressure", "data": {"level": "critical", "working_set": 7, "max": 8}}
    journal.append_events(root, binding, [event])
    read = journal.read(root, sid)
    kinds = [item["event"]["kind"] for item in read.events.for_operations(set())]
    assert kinds == ["memory_pressure"]
    view = native_runtime.project(
        read, {"request": {"model": None}, "adapter": binding.adapter}, binding.worker_id
    )
    assert view["turns"] == []


# --- the session list ----------------------------------------------------------------------------


def _record(key, worker_id, *, closed=False):
    sid = key.split(":", 1)[1]
    native_state.write_session(
        sid, {"session_key": key, "current_worker": worker_id, "closed": closed}
    )


def test_the_list_reads_each_open_sessions_current_generation_once(monkeypatch):
    native_runtime._pressure_cache = None
    warn, quiet, closed = (str(uuid.uuid4()) for _ in range(3))
    keys = {name: f"codex-api:{uuid.uuid4()}" for name in ("warn", "quiet", "closed")}
    for name, worker_id in (("warn", warn), ("quiet", quiet), ("closed", closed)):
        _record(keys[name], worker_id, closed=name == "closed")
    for worker_id, level in ((warn, "critical"), (quiet, "normal"), (closed, "critical")):
        doc = life(level)
        doc["memory"]["at"] = time.time()
        native_state.update_lifecycle(worker_id, **doc)
    shown = native_runtime.memory_pressures()
    assert shown == {
        keys["warn"]: {
            "level": "critical",
            "working_set": 6 * GIB,
            "max": 8 * GIB,
            "scope": "session",
            "throttled": None,
        }
    }
    # Cached for a moment: a list poll never re-reads every record per row.
    native_state.update_lifecycle(warn, phase="exited")
    assert native_runtime.memory_pressures() == shown
    native_runtime._pressure_cache = None
    assert native_runtime.memory_pressures() == {}


def test_rows_carry_the_warning_and_every_other_row_carries_null(auth_cfg, fake_jsonl, monkeypatch):
    running_tests._running(monkeypatch)
    warning = {"level": "high", "working_set": 6 * GIB, "max": 8 * GIB}
    monkeypatch.setattr(
        native_runtime, "memory_pressures", lambda: {f"claude:{running_tests.A}": warning}
    )
    c = running_tests._client(auth_cfg)
    rows = {r["id"]: r for r in c.get("/api/sessions?limit=50").json()["sessions"]}
    assert rows[f"claude:{running_tests.A}"]["memory_pressure"] == warning
    assert rows[f"claude:{running_tests.B}"]["memory_pressure"] is None


@pytest.mark.parametrize(
    ("given", "published"),
    [("session", "session"), ("shared", "shared"), ("yes", None), (None, None)],
)
def test_throttle_evidence_is_published_only_in_its_closed_form(given, published):
    doc = life("critical")
    doc["memory"]["throttled"] = given
    assert shown(doc)["throttled"] == published


def _deep(tmp_path, levels: int, *, parent_max=2 * GIB, parent_used=int(1.9 * GIB)):
    """A worker unit `levels` groups below a capped ancestor (the first one under the root)."""
    root = tmp_path / "cg"
    top = root / "top.slice"
    path = top
    for i in range(levels - 1):
        path = path / f"l{i}.slice"
    unit = path / "worker.service"
    unit.mkdir(parents=True)
    for name, text in {
        "memory.current": f"{GIB}\n",
        "memory.max": f"{8 * GIB}\n",
        "memory.stat": "inactive_file 0\n",
        "memory.events": "high 0\n",
    }.items():
        (unit / name).write_text(text)
    for name, text in {
        "memory.max": f"{parent_max}\n",
        "memory.current": f"{parent_used}\n",
        "memory.stat": "inactive_file 0\n",
        "memory.events.local": "high 0\n",
    }.items():
        (top / name).write_text(text)
    return root, "/" + str(unit.relative_to(root))


def test_a_capped_ancestor_seventeen_levels_up_is_still_measured(tmp_path):
    # Hermes on #1651: the walk used to stop after 16 levels and read healthy.
    root, group = _deep(tmp_path, 17)
    reading = native_memory.sample(group, root)
    assert (reading.maximum, reading.scope) == (2 * GIB, "shared")
    assert Tracker().update(reading) == "critical"


def test_a_hierarchy_deeper_than_the_bound_is_unavailable_never_healthy(tmp_path):
    root, group = _deep(tmp_path, native_memory.MAX_DEPTH + 2)
    assert native_memory.sample(group, root) is None
