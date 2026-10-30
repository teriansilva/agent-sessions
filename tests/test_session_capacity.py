"""The limit on running sessions (#1819): the recommendation, the setting, and the one gate."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_sessions import resource_limits, session_capacity, sysinfo

GIB = 1024**3


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(session_capacity.ptybridge, "master_census", lambda: ([], 0))
    monkeypatch.setattr(session_capacity, "_api_running", set)


def _child_env() -> dict[str, str]:
    keep = {"AGENT_SESSIONS_LOCK_DIR", "AGENT_SESSIONS_PREFS"}
    return {k: v for k, v in os.environ.items() if not k.startswith("AGENT_SESSIONS_") or k in keep}


def limit(monkeypatch, value):
    monkeypatch.setattr(resource_limits, "max_sessions", lambda: value)


# ---- the recommendation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory_gib", "cpus", "by_memory", "by_cpu", "recommended", "limited_by"),
    [
        (4, 2, 2, 12, 2, "memory"),
        (16, 4, 14, 24, 14, "memory"),
        (32, 8, 28, 48, 28, "memory"),
        (255, 64, 229, 384, 229, "memory"),
        (255, 8, 229, 48, 48, "cpu"),
        (2, 8, 0, 48, 1, "memory"),  # never below one
        (4096, 256, 3686, 1536, 256, "cpu"),  # never above the range
    ],
)
def test_the_recommendation_table(memory_gib, cpus, by_memory, by_cpu, recommended, limited_by):
    got = sysinfo.session_recommendation(memory_gib * GIB, cpus)
    assert got["by_memory"] == by_memory
    assert got["by_cpu"] == by_cpu
    assert got["recommended"] == recommended
    assert got["limited_by"] == limited_by


def test_an_unreadable_host_still_gets_a_finite_recommendation():
    assert sysinfo.session_recommendation(None, 4)["recommended"] == 24
    assert sysinfo.session_recommendation(None, 4)["limited_by"] == "cpu"
    assert sysinfo.session_recommendation(8 * GIB, None)["recommended"] == 6
    nothing = sysinfo.session_recommendation(None, None)
    assert (nothing["recommended"], nothing["limited_by"]) == (8, "unknown")


def _cgroups(monkeypatch, tmp_path, path, files: dict[str, dict[str, str]]):
    root = tmp_path / "cgroup"
    for group, contents in files.items():
        (root / group).mkdir(parents=True, exist_ok=True)
        for name, text in contents.items():
            (root / group / name).write_text(text)
    proc = tmp_path / "proc-cgroup"
    proc.write_text(f"0::{path}\n")
    monkeypatch.setattr(sysinfo, "CGROUP_ROOT", root)
    monkeypatch.setattr(sysinfo, "PROC_CGROUP", proc)
    monkeypatch.setattr(sysinfo, "_host_memory", lambda: 64 * GIB)
    monkeypatch.setattr(sysinfo, "_host_cpus", lambda: 16)


def test_a_container_limit_beats_the_hosts_numbers(monkeypatch, tmp_path):
    _cgroups(
        monkeypatch,
        tmp_path,
        "/",
        {".": {"memory.max": str(8 * GIB), "cpu.max": "250000 100000"}},
    )
    got = sysinfo.session_capacity()
    assert got["memory_bytes"] == 8 * GIB
    assert got["cpus"] == 3  # 2.5 rounded up
    assert got["recommended"] == 6


def test_a_parent_limit_applies_but_the_apps_own_unit_does_not(monkeypatch, tmp_path):
    _cgroups(
        monkeypatch,
        tmp_path,
        "/user.slice/app.slice/agent-sessions.service",
        {
            # The unit's own cap: sessions run beside the unit, so it doesn't bound them.
            "user.slice/app.slice/agent-sessions.service": {"memory.max": str(1 * GIB)},
            "user.slice": {"memory.max": str(20 * GIB), "cpu.max": "max 100000"},
        },
    )
    got = sysinfo.session_capacity()
    assert got["memory_bytes"] == 20 * GIB
    assert got["cpus"] == 16  # "max" sets nothing


def test_a_containers_own_scope_applies_when_its_namespace_shows_the_host_tree(
    monkeypatch, tmp_path
):
    # `--cgroupns=host`: the limit sits on the leaf, and everything in the container is under it.
    _cgroups(
        monkeypatch,
        tmp_path,
        "/system.slice/docker-abc.scope",
        {"system.slice/docker-abc.scope": {"memory.max": str(8 * GIB)}},
    )
    assert sysinfo.session_capacity()["memory_bytes"] == 8 * GIB


def test_a_cgroup_v1_container_limit_applies_and_v1_unlimited_doesnt(monkeypatch, tmp_path):
    _cgroups(monkeypatch, tmp_path, "/", {"memory": {"memory.limit_in_bytes": str(8 * GIB)}})
    assert sysinfo.session_capacity()["memory_bytes"] == 8 * GIB
    (tmp_path / "cgroup/memory/memory.limit_in_bytes").write_text("9223372036854771712")
    assert sysinfo.session_capacity()["memory_bytes"] == 64 * GIB


def test_garbage_in_a_cgroup_file_is_ignored(monkeypatch, tmp_path):
    _cgroups(monkeypatch, tmp_path, "/", {".": {"memory.max": "max", "cpu.max": "oops 0"}})
    got = sysinfo.session_capacity()
    assert (got["memory_bytes"], got["cpus"]) == (64 * GIB, 16)


# ---- the setting ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, 257, True, "8", 8.0, None])
def test_a_bad_limit_is_refused(value):
    with pytest.raises(ValueError):
        resource_limits.save({"max_sessions": value})


def test_unsaved_follows_the_recommendation_and_saved_stays(monkeypatch):
    capacity = sysinfo.session_recommendation(16 * GIB, 4)
    monkeypatch.setattr(sysinfo, "session_capacity", lambda: capacity)
    assert resource_limits.max_sessions() == 14
    settings = resource_limits.settings()
    assert settings["values"]["max_sessions"] == 14
    assert settings["recommended"]["max_sessions"] == 14
    assert settings["sources"]["max_sessions"] == "recommended"
    assert settings["bounds"]["max_sessions"] == {"min": 1, "max": 256}
    assert settings["capacity"]["limited_by"] == "memory"

    resource_limits.save({"max_sessions": 40})
    assert resource_limits.max_sessions() == 40
    assert resource_limits.settings()["sources"]["max_sessions"] == "settings"

    bigger = sysinfo.session_recommendation(64 * GIB, 16)
    monkeypatch.setattr(sysinfo, "session_capacity", lambda: bigger)
    assert resource_limits.max_sessions() == 40
    assert resource_limits.settings()["recommended"]["max_sessions"] == 57


def test_saving_the_recommendation_goes_back_to_following_the_host(monkeypatch):
    # "Restore recommended": a number frozen today would silently outlive a change of hardware.
    capacity = sysinfo.session_recommendation(16 * GIB, 4)
    monkeypatch.setattr(sysinfo, "session_capacity", lambda: capacity)
    resource_limits.save({"max_sessions": 40, "library_threads": 4})
    resource_limits.save({"max_sessions": 14})
    assert resource_limits.settings()["sources"]["max_sessions"] == "recommended"
    assert resource_limits.saved() == {"library_threads": 4}
    bigger = sysinfo.session_recommendation(64 * GIB, 16)
    monkeypatch.setattr(sysinfo, "session_capacity", lambda: bigger)
    assert resource_limits.max_sessions() == 57


def test_a_corrupt_saved_limit_falls_back_to_the_recommendation(monkeypatch):
    capacity = sysinfo.session_recommendation(16 * GIB, 4)
    monkeypatch.setattr(sysinfo, "session_capacity", lambda: capacity)
    resource_limits.prefs.mutate_block("resource_limits", lambda _old: {"max_sessions": 9999})
    assert resource_limits.max_sessions() == 14
    assert resource_limits.settings()["notice"]


# ---- the gate ---------------------------------------------------------------------------------


def test_admits_below_and_refuses_at_the_limit(monkeypatch):
    limit(monkeypatch, 2)
    monkeypatch.setattr(session_capacity, "_api_running", lambda: {"codex:a"})
    first = session_capacity.admit("claude:b")
    with pytest.raises(session_capacity.SessionLimitReached) as refused:
        session_capacity.admit("claude:c")
    assert (refused.value.running, refused.value.limit) == (2, 2)
    assert "Archive a session" in str(refused.value)
    first.release()
    session_capacity.admit("claude:c")


def test_masters_and_api_workers_both_count_and_a_probe_timeout_counts_as_running(monkeypatch):
    limit(monkeypatch, 3)
    monkeypatch.setattr(session_capacity.ptybridge, "master_census", lambda: ([("claude", "a")], 1))
    monkeypatch.setattr(session_capacity, "_api_running", lambda: {"codex:b"})
    assert session_capacity.count() == 3
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("claude:new")


def test_a_key_that_already_counts_takes_no_second_slot(monkeypatch):
    limit(monkeypatch, 1)
    monkeypatch.setattr(session_capacity, "_api_running", lambda: {"codex:a"})
    # A generation replacing itself, or a reservation still held while its master comes up.
    held = [session_capacity.admit("codex:a"), session_capacity.admit("codex:a")]
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("codex:b")
    assert len(held) == 2


def test_a_hyphenated_engine_is_one_session_though_its_socket_name_reads_back_differently(
    monkeypatch,
):
    # `my-agent:abc` has the socket `my-agent-abc`, which can only read back as `my:agent-abc`.
    limit(monkeypatch, 1)
    monkeypatch.setattr(
        session_capacity.ptybridge, "master_census", lambda: ([("my", "agent-abc")], 0)
    )
    slot = session_capacity.admit("my-agent:abc")
    with pytest.raises(session_capacity.SessionLimitReached) as refused:
        session_capacity.admit("codex:other")
    assert refused.value.running == 1
    slot.release()


def test_check_says_what_a_launch_would_hear_and_holds_nothing(monkeypatch):
    limit(monkeypatch, 1)
    session_capacity.check()
    assert list(session_capacity._slots_dir().iterdir()) == []
    monkeypatch.setattr(session_capacity, "_api_running", lambda: {"codex:a"})
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.check()


def test_an_anonymous_launch_takes_a_slot(monkeypatch):
    limit(monkeypatch, 1)
    held = session_capacity.admit(None)
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit(None)
    held.release()


def test_lowering_the_limit_stops_nothing_and_only_refuses(monkeypatch):
    limit(monkeypatch, 1)
    keys = {"a:1", "a:2", "a:3"}
    monkeypatch.setattr(session_capacity, "_api_running", lambda: keys)
    with pytest.raises(session_capacity.SessionLimitReached) as refused:
        session_capacity.admit("a:4")
    assert refused.value.running == 3
    assert keys == {"a:1", "a:2", "a:3"}


def test_the_race_for_the_last_slot_admits_exactly_one(monkeypatch):
    limit(monkeypatch, 1)
    barrier = threading.Barrier(16)
    admitted: list[session_capacity.Reservation] = []
    refused: list[str] = []

    def race(n: int) -> None:
        barrier.wait()
        key = f"claude:{n}"
        try:
            admitted.append(session_capacity.admit(key))
        except session_capacity.SessionLimitReached:
            refused.append(key)

    threads = [threading.Thread(target=race, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 1
    assert len(refused) == 15


def test_the_race_holds_across_processes(tmp_path):
    # Peer app instances share the lock directory; a thread race can't prove the flock.
    script = """
import os, sys, time
from agent_sessions import resource_limits, session_capacity as c
c.ptybridge.master_census = lambda: ([], 0)
c._api_running = set
resource_limits.max_sessions = lambda: 1
while not os.path.exists(sys.argv[1]):
    time.sleep(0.001)
try:
    slot = c.admit(f"claude:{os.getpid()}")
    print("admitted", flush=True)
except c.SessionLimitReached:
    print("refused", flush=True)
time.sleep(float(sys.argv[2]))  # the holder stays alive, as a launching app does
"""
    go = tmp_path / "go"
    env = _child_env()
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(go), "1.5"],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(6)
    ]
    time.sleep(1.0)  # let the imports finish, so they all contend
    go.touch()
    answers = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert answers == ["admitted"] + ["refused"] * 5


def test_a_dead_holders_reservation_frees_its_slot(monkeypatch):
    limit(monkeypatch, 1)
    # A real process takes the slot, then dies without releasing: the kernel drops its flock.
    script = """
import os, sys
from agent_sessions import resource_limits, session_capacity as c
c.ptybridge.master_census = lambda: ([], 0)
c._api_running = set
resource_limits.max_sessions = lambda: 1
slot = c.admit("claude:crashed")
print("held", flush=True)
sys.stdin.read()
os._exit(9)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=_child_env(),
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("claude:y")  # alive: it holds the slot
    holder.stdin.close()
    assert holder.wait(timeout=30) == 9
    slot = session_capacity.admit("claude:y")
    assert len(list(session_capacity._slots_dir().iterdir())) == 1  # the leftover was removed
    slot.release()


def test_a_slow_launch_keeps_its_slot_however_long_it_takes(monkeypatch):
    limit(monkeypatch, 1)
    slot = session_capacity.admit("claude:slow")
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 7 * 24 * 3600)
    with pytest.raises(session_capacity.SessionLimitReached):
        session_capacity.admit("claude:other")
    slot.release()
    session_capacity.admit("claude:other")


def test_a_reservation_nobody_kept_is_released(monkeypatch):
    # What a launch cancelled inside `to_thread(admit)` leaves: nobody ever received the object.
    limit(monkeypatch, 1)
    session_capacity.admit("claude:dropped")
    gc.collect()
    session_capacity.admit("claude:y")


def test_a_corrupt_slot_file_is_held_only_while_locked(monkeypatch):
    limit(monkeypatch, 1)
    (session_capacity._slots_dir() / "junk").write_text("{nope")
    session_capacity.admit("claude:y")


def test_an_uncountable_host_admits_with_a_warning(monkeypatch, caplog):
    limit(monkeypatch, 1)

    def broken():
        raise OSError("the runtime directory could not be listed")

    monkeypatch.setattr(session_capacity.ptybridge, "master_census", broken)
    slot = session_capacity.admit("claude:x")
    assert list(session_capacity._slots_dir().iterdir()) == []
    slot.release()  # a no-op, not a crash
    assert "admitting this launch" in caplog.text


def test_release_is_idempotent(monkeypatch):
    limit(monkeypatch, 1)
    slot = session_capacity.admit("claude:a")
    slot.release()
    slot.release()
    assert list(session_capacity._slots_dir().iterdir()) == []


# ---- the list behind the dialog ---------------------------------------------------------------


def test_the_capacity_route_lists_what_runs_oldest_first_and_counts_what_it_cant_list(
    fake_jsonl, auth_cfg, monkeypatch
):
    from test_webterm import _GOOD, _client, _login_headers

    limit_of = resource_limits.save
    limit_of({"max_sessions": 3})
    monkeypatch.setattr(
        session_capacity,
        "running",
        lambda: ({_GOOD, "codex:outside-the-lists-scope"}, 1),
    )
    c = _client(auth_cfg)
    assert c.get("/api/sessions/capacity").status_code == 401
    headers = _login_headers(c, auth_cfg)
    got = c.get("/api/sessions/capacity", headers=headers).json()
    assert got["limit"] == 3
    assert got["source"] == "settings"
    assert got["readable"] is True
    assert got["running"] == 3
    assert [row["id"] for row in got["sessions"]] == [_GOOD]
    assert got["others"] == 2  # the session out of scope, and the socket that didn't answer


def test_the_capacity_route_says_when_it_cant_read(fake_jsonl, auth_cfg, monkeypatch):
    from test_webterm import _client, _login_headers

    def broken():
        raise OSError("no")

    monkeypatch.setattr(session_capacity, "running", broken)
    c = _client(auth_cfg)
    got = c.get("/api/sessions/capacity", headers=_login_headers(c, auth_cfg)).json()
    assert got["readable"] is False
    assert got["running"] is None  # never a healthy zero
