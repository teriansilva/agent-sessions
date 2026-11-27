"""#2415 Phase 1 — the ``native_worker_state`` prune category ("Ended agent worker records").

What is pinned here is that a worker dir is removed only when EVERY fact still holds at removal
time: no session record names its id (anywhere in the record, ``launch.gen`` included), its
lifecycle says ``gone`` with no config under the worker's own lock, nothing in a transcript still
reads why it ended, and systemd says its unit is over. Unknown never authorises: an unreadable
record skips the whole category, a failed unit probe keeps the dir. Removal never follows a
symlink, and a partial removal reports only the bytes it actually freed and finishes next pass.

The state dirs are real (the plugin-state dir is isolated per test by conftest); systemd is a
fake ``HOST`` that counts its unit queries.
"""

from __future__ import annotations

import fcntl
import os
import threading
import uuid

import pytest

from agent_sessions import maintenance, native_containment, native_gc, native_runtime, native_state


class _Host:
    """Unit evidence per worker id; ``show`` calls are counted (the budget)."""

    def __init__(self) -> None:
        self.units: dict[str, str] = {}  # worker id → "not-found" | "active" | "inactive" | "fail"
        self.groups: dict[str, str] = {}  # control group → cgroup state
        self.shows = 0
        self._lock = threading.Lock()  # probes run on a small pool

    def show(self, worker):
        with self._lock:
            self.shows += 1
        state = self.units.get(worker.worker_id, "not-found")
        if state == "fail":
            return None
        if state == "not-found":
            return native_containment.UnitObservation(
                worker.unit, "not-found", "inactive", "dead", None, None, 0, ""
            )
        active = state == "active"
        return native_containment.UnitObservation(
            worker.unit,
            "loaded",
            "active" if active else "inactive",
            "running" if active else "dead",
            "a" * 32,
            f"/user.slice/{worker.unit}",
            4242 if active else 0,
            "success",
        )

    def cgroup(self, control_group: str):
        return native_containment.CgroupObservation(
            control_group, self.groups.get(control_group, "absent")
        )


@pytest.fixture
def host(monkeypatch):
    h = _Host()
    monkeypatch.setattr(native_runtime, "HOST", h)
    return h


def _wid() -> str:
    return str(uuid.uuid4())


def _worker(phase: str = "gone", *, config: bool = False, **life) -> str:
    wid = _wid()
    native_state.update_lifecycle(wid, phase=phase, unit=f"u-{wid}", **life)
    if config:
        native_state.write_config(wid, {"worker_id": wid, "session_key": "claude-api:x"})
    return wid


def _record(**fields) -> str:
    sid = _wid()
    native_state.write_session(sid, {"session_key": f"claude-api:{sid}", **fields})
    return sid


def _wdir(wid: str):
    return native_state.root() / "workers" / wid


def _dry() -> dict:
    return maintenance.dry_run_caches()["native_worker_state"]


def _prune() -> dict:
    return maintenance.prune_caches(["native_worker_state"])


def _skips(out: dict) -> dict[str, int]:
    return {
        s["reason"]: s["count"] for s in out["skipped"] if s["category"] == "native_worker_state"
    }


# ---- the happy path -----------------------------------------------------------------------


def test_an_unreferenced_gone_dir_is_counted_then_removed(host):
    wid = _worker()
    size = sum(p.stat().st_size for p in _wdir(wid).iterdir())
    dry = _dry()
    assert dry == {"items": 1, "bytes": size}
    assert _wdir(wid).is_dir()  # the dry run changed nothing

    out = _prune()
    assert out["removed"] == 1
    assert out["bytes_freed"] == size
    assert out["failed"] == [] and out["skipped"] == []
    assert not _wdir(wid).exists()
    assert not (native_state.root() / "workers" / f".gc-{wid}").exists()


def test_the_category_is_listed_and_measured_with_the_others(host):
    assert "native_worker_state" in maintenance.CATEGORIES
    dry = maintenance.dry_run_caches()
    assert set(dry) == set(maintenance.CATEGORIES)
    assert dry["native_worker_state"] == {"items": 0, "bytes": 0}  # no native state at all


# ---- kept: referenced, not ended, config, live unit ---------------------------------------


@pytest.mark.parametrize(
    "where",
    ["workers", "current_worker", "launch.gen", "start.gen", "unit_name", "nested"],
)
def test_a_dir_any_session_record_names_is_kept(host, where):
    wid = _worker()
    if where == "workers":
        _record(workers=[wid], current_worker=None)
    elif where == "current_worker":
        _record(workers=[], current_worker=wid)
    elif where == "launch.gen":
        _record(launch={"state": "starting", "gen": wid})
    elif where == "start.gen":
        _record(start={"state": "started", "gen": wid})
    elif where == "unit_name":
        _record(note=f"battlelab-native-{wid.replace('-', '')}.service")
    else:
        _record(history=[{"deep": {"ids": ["x", wid.upper()]}}])
    assert _dry()["items"] == 0
    out = _prune()
    assert out["removed"] == 0
    assert _wdir(wid).is_dir()
    assert host.shows == 0  # a referenced dir never costs a unit query


@pytest.mark.parametrize("phase", ["intent", "ready", "exited", "launch_unknown", None])
def test_a_dir_not_proved_gone_is_kept(host, phase):
    wid = _wid()
    native_state.update_lifecycle(wid, **({"phase": phase} if phase else {"unit": "x"}))
    assert _dry()["items"] == 0
    assert _prune()["removed"] == 0
    assert _wdir(wid).is_dir()


def test_a_gone_dir_that_still_has_its_config_is_kept(host):
    wid = _worker(config=True)
    assert _dry()["items"] == 0
    assert _prune()["removed"] == 0
    assert (_wdir(wid) / "config.json").exists()


def test_a_dir_whose_ending_a_transcript_still_explains_is_kept(host):
    """`_ended_reason` reads a gone generation's lifecycle to say WHY it ended; keep that."""
    wid = _worker(journal_failure="disk_full")
    assert native_runtime._ended_reason(wid) is not None
    assert _dry()["items"] == 0
    assert _prune()["removed"] == 0
    assert _wdir(wid).is_dir()


@pytest.mark.parametrize(
    "life",
    [
        {"journal_failure": "disk_full"},
        {"journal_failure": "io_error"},
        {"invocation_id": "b" * 32, "exit": {"result": "oom-kill", "invocation_id": "b" * 32}},
        {
            "invocation_id": "b" * 32,
            "agent_exit": {"reason": "sigkill_during_oom", "invocation_id": "b" * 32},
        },
        {
            "invocation_id": "b" * 32,
            "exit": {"result": "oom-kill", "invocation_id": "b" * 32, "at": 10.0},
            "memory": {
                "invocation_id": "b" * 32,
                "at": 9.0,
                "working_set": 2 << 30,
                "max": 4 << 30,
                "scope": "own",
            },
        },
    ],
)
def test_the_keep_rule_covers_every_ending_the_transcript_explains(host, life):
    """`_may_explain_an_ending` is a SUPERSET of `native_runtime._ended_reason`: whenever the
    transcript has a reason to show, the dir is kept."""
    wid = _worker(**life)
    assert native_runtime._ended_reason(wid) is not None
    assert native_gc._may_explain_an_ending(native_state.read_lifecycle(wid))
    assert _prune()["removed"] == 0
    assert _wdir(wid).is_dir()


@pytest.mark.parametrize(
    ("unit", "group", "reason"),
    [
        ("active", "absent", native_gc.SKIP_UNIT),
        ("inactive", "populated", native_gc.SKIP_UNIT),
        ("inactive", "unknown", native_gc.SKIP_PROBE),
    ],
)
def test_a_dir_whose_unit_is_not_proved_over_is_kept(host, unit, group, reason):
    wid = _worker()
    host.units[wid] = unit
    host.groups[f"/user.slice/{native_containment.WorkerIdentity(wid).unit}"] = group
    assert _dry()["items"] == 0
    out = _prune()
    assert out["removed"] == 0
    assert _skips(out) == {reason: 1}
    assert _wdir(wid).is_dir()


def test_a_loaded_inactive_unit_with_an_empty_cgroup_is_over(host):
    wid = _worker()
    host.units[wid] = "inactive"
    host.groups[f"/user.slice/{native_containment.WorkerIdentity(wid).unit}"] = "empty"
    assert _prune()["removed"] == 1


def test_a_unit_probe_failure_keeps_the_dir(host):
    wid = _worker()
    host.units[wid] = "fail"
    assert _dry()["items"] == 0
    out = _prune()
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_PROBE: 1}
    assert _wdir(wid).is_dir()


def test_at_most_one_unit_query_per_file_eligible_dir(host):
    eligible = [_worker() for _ in range(3)]
    referenced = [_worker() for _ in range(4)]
    _record(workers=referenced)
    [_worker("ready") for _ in range(2)]
    [_worker(config=True) for _ in range(2)]
    _items, _bytes, queries = native_gc.measure()
    assert queries == len(eligible)
    host.shows = 0
    out = native_gc.prune()
    assert out.removed == 3
    assert host.shows == out.unit_queries == len(eligible)


# ---- unknown never authorises -------------------------------------------------------------


def test_one_unreadable_session_record_skips_the_whole_category(host):
    wid = _worker()
    broken = native_state.root() / f"{_wid()}.json"
    broken.write_text("{not json")
    broken.chmod(0o600)  # private, so it is the JSON that is unreadable
    dry = _dry()
    assert dry.get("error")
    assert dry["items"] == 0
    out = _prune()
    assert out["removed"] == 0
    assert out["failed_total"] == 1
    assert out["failed"][0]["category"] == "native_worker_state"
    assert "nothing was removed" in out["failed"][0]["reason"]
    assert _wdir(wid).is_dir()
    assert host.shows == 0


@pytest.mark.parametrize("damage", ["lifecycle", "dir"])
def test_an_unreadable_worker_dir_is_kept_and_reported(host, damage):
    wid = _worker()
    if damage == "lifecycle":
        (_wdir(wid) / "lifecycle.json").write_text("{not json")
    else:
        _wdir(wid).chmod(0o000)
    try:
        assert _dry()["items"] == 0
        out = _prune()
    finally:
        _wdir(wid).chmod(0o700)
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_UNREADABLE: 1}
    assert _wdir(wid).is_dir()
    assert host.shows == 0


def test_a_record_unreadable_at_the_second_read_removes_nothing(host, monkeypatch):
    wid = _worker()
    monkeypatch.setattr(
        native_gc,
        "_after_discovery",
        lambda: _unreadable_record(),
    )
    out = _prune()
    assert out["removed"] == 0 and out["failed_total"] == 1
    assert _wdir(wid).is_dir()


def _unreadable_record() -> None:
    broken = native_state.root() / f"{_wid()}.json"
    broken.write_text("[]")
    broken.chmod(0o600)


# ---- races between discovery and removal --------------------------------------------------


def test_a_reference_appearing_after_discovery_keeps_the_dir(host, monkeypatch):
    wid = _worker()
    monkeypatch.setattr(native_gc, "_after_discovery", lambda: _record(workers=[wid]))
    out = _prune()
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_REFERENCED: 1}
    assert _wdir(wid).is_dir()


def test_a_config_appearing_after_discovery_keeps_the_dir(host, monkeypatch):
    wid = _worker()
    monkeypatch.setattr(
        native_gc, "_after_discovery", lambda: native_state.write_config(wid, {"worker_id": wid})
    )
    out = _prune()
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_CHANGED: 1}
    assert (_wdir(wid) / "config.json").exists()


def test_a_lifecycle_leaving_gone_after_discovery_keeps_the_dir(host, monkeypatch):
    wid = _worker()
    monkeypatch.setattr(
        native_gc, "_after_discovery", lambda: native_state.update_lifecycle(wid, phase="intent")
    )
    out = _prune()
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_CHANGED: 1}


def test_a_held_lifecycle_lock_leaves_the_dir_for_the_next_pass(host, monkeypatch):
    wid = _worker()
    monkeypatch.setattr(native_gc, "_LOCK_WAIT", 0.05)
    fd = os.open(_wdir(wid) / "lifecycle.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = _prune()
    finally:
        os.close(fd)
    assert out["removed"] == 0
    assert _skips(out) == {native_gc.SKIP_BUSY: 1}
    assert _prune()["removed"] == 1  # free again → removed


def test_removal_holds_the_lifecycle_lock_every_writer_takes(host, monkeypatch):
    _worker()
    seen = {}

    def probe(worker_id):
        fd = os.open(_wdir(worker_id) / "lifecycle.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["held"] = False
        except BlockingIOError:
            seen["held"] = True
        finally:
            os.close(fd)

    monkeypatch.setattr(native_gc, "_before_remove", probe)
    assert _prune()["removed"] == 1
    assert seen == {"held": True}


# ---- descriptor-relative removal ----------------------------------------------------------


def test_a_symlinked_worker_dir_is_never_followed(host, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "lifecycle.json").write_text('{"phase": "gone"}')
    wid = _wid()
    native_state.ensure_dir(native_state.root() / "workers")
    os.symlink(outside, _wdir(wid))
    assert _dry()["items"] == 0
    assert _prune()["removed"] == 0
    assert (outside / "lifecycle.json").exists()
    assert _wdir(wid).is_symlink()


def test_a_symlink_inside_a_worker_dir_is_unlinked_not_followed(host, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    wid = _worker()
    os.symlink(victim, _wdir(wid) / "link")
    assert _prune()["removed"] == 1
    assert victim.read_text() == "keep me"


def test_non_worker_names_are_never_touched(host):
    native_state.ensure_dir(native_state.root() / "workers")
    for name in ("not-a-uuid", ".gc-not-a-uuid", "11111111-1111-1111-1111-111111111111"):
        (native_state.root() / "workers" / name).mkdir(mode=0o700)
    assert _prune()["removed"] == 0
    assert len(list((native_state.root() / "workers").iterdir())) == 3


def test_a_partial_removal_reports_only_freed_bytes_and_retries_next_pass(host, monkeypatch):
    wid = _worker()
    (_wdir(wid) / "extra.bin").write_bytes(b"x" * 1000)
    sizes = {p.name: p.stat().st_size for p in _wdir(wid).iterdir()}
    real = native_gc._unlink_at

    def failing(name, dfd):
        if name == "extra.bin":
            raise PermissionError(13, "Permission denied")
        real(name, dfd)

    monkeypatch.setattr(native_gc, "_unlink_at", failing)
    out = _prune()
    assert out["removed"] == 0
    assert out["bytes_freed"] == sum(v for k, v in sizes.items() if k != "extra.bin")
    assert out["failed_total"] == 1
    assert out["failed"][0]["item"] == wid
    assert "partly removed" in out["failed"][0]["reason"]
    leftover = native_state.root() / "workers" / f".gc-{wid}"
    assert leftover.is_dir() and not _wdir(wid).exists()
    assert _dry() == {"items": 1, "bytes": 1000}

    monkeypatch.setattr(native_gc, "_unlink_at", real)
    again = _prune()
    assert again["removed"] == 1
    assert again["bytes_freed"] == 1000
    assert again["failed"] == []
    assert not leftover.exists()


# ---- through the production routes --------------------------------------------------------


def test_the_category_through_the_routes(auth_cfg, fake_jsonl, host):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    wid = _worker()
    kept = _worker()
    _record(workers=[kept])
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}

    dry = c.get("/api/maintenance/prune").json()["categories"]["native_worker_state"]
    assert dry["items"] == 1 and dry["bytes"] > 0
    out = c.post(
        "/api/maintenance/prune", json={"categories": ["native_worker_state"]}, headers=hdr
    ).json()
    assert out["removed"] == 1 and out["failed"] == []
    assert not _wdir(wid).exists()
    assert _wdir(kept).is_dir()


# ---- batched unit queries on the real host ------------------------------------------------


class _BatchHost(native_runtime.Host):
    """The REAL host's `show`, so the pass batches through `run`; `run` answers from a script."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=30.0):
        self.calls.append(argv)
        units = argv[argv.index("--") + 1 :]
        rc, blocks = self.answer(units)
        import subprocess

        return subprocess.CompletedProcess(argv, rc, "\n\n".join(blocks) + "\n", "")

    def cgroup(self, control_group):
        return native_containment.CgroupObservation(control_group, "absent")


def _block(unit: str, load: str = "not-found", active: str = "inactive") -> str:
    return "\n".join(
        [
            "MainPID=0",
            "Result=success",
            "ControlGroup=",
            f"Id={unit}",
            f"LoadState={load}",
            f"ActiveState={active}",
            "SubState=dead",
            "InvocationID=",
        ]
    )


def test_the_real_host_is_asked_in_batches_never_more_than_once_per_dir(monkeypatch):
    ids = [_worker() for _ in range(70)]
    h = _BatchHost(lambda units: (0, [_block(u) for u in units]))
    monkeypatch.setattr(native_runtime, "HOST", h)
    out = native_gc.prune()
    assert out.removed == len(ids)
    assert out.unit_queries == len(h.calls) == 2  # 64 + 6
    for argv in h.calls:
        assert argv[:5] == ["/usr/bin/systemctl", "--user", "show", "--no-pager", argv[4]]
        assert argv[4].startswith("--property=")


@pytest.mark.parametrize(
    "answer",
    [
        lambda units: (1, [_block(u) for u in units]),  # the manager refused
        lambda units: (0, [_block(u) for u in units[:-1]]),  # one block short
        lambda units: (0, [_block(units[0])] * len(units)),  # blocks for the wrong units
    ],
)
def test_an_incomplete_batched_answer_keeps_every_dir_it_covers(monkeypatch, answer):
    ids = [_worker() for _ in range(3)]
    monkeypatch.setattr(native_runtime, "HOST", _BatchHost(answer))
    out = _prune()
    assert out["removed"] <= 1  # at most the one unit a mismatched answer did name
    kept = [w for w in ids if _wdir(w).is_dir()]
    assert len(kept) >= 2
    assert _skips(out).get(native_gc.SKIP_PROBE, 0) == len(kept)


def test_a_batched_answer_still_honours_a_live_unit(monkeypatch):
    live, ended = _worker(), _worker()
    unit = native_containment.WorkerIdentity(live).unit

    def answer(units):
        return 0, [_block(u, "loaded", "active") if u == unit else _block(u) for u in units]

    monkeypatch.setattr(native_runtime, "HOST", _BatchHost(answer))
    out = _prune()
    assert out["removed"] == 1
    assert _wdir(live).is_dir() and not _wdir(ended).exists()
    assert _skips(out) == {native_gc.SKIP_UNIT: 1}
