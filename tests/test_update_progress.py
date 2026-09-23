"""Self-update progress (#1085): the installer's record, read as input.

The installer writes one JSON record per milestone to a path `apply()` names; the app reads it,
across its own restart, and turns it into the Updates page's progress bar. Everything the page
shows is validated here: state and step come from closed sets, numbers are plain integers, and
the label is the server's own — never text from the file.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_sessions import update
from agent_sessions.main import create_app

NOW = 1_800_000_000


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    return tmp_path


def _write(home, **rec):
    base = {"state": "running", "step": "web", "pid": os.getpid(), "started_at": NOW - 100}
    base.update(rec)
    base.setdefault("at", NOW - 5)
    (home / "update-progress.json").write_text(json.dumps(base))


def test_no_record_is_idle(home):
    p = update.progress(now=NOW)
    assert p["state"] == "idle" and p["steps"] == 7


def test_a_running_step_is_labelled_by_the_server(home):
    _write(home, step="web")
    p = update.progress(now=NOW)
    assert p["state"] == "running"
    assert (p["step_index"], p["steps"]) == (4, 7)
    assert p["label"] == "Building the web UI"
    assert p["elapsed_s"] == 100


def test_a_running_record_whose_installer_is_gone_is_failed(home, monkeypatch):
    monkeypatch.setattr(update, "_pid_alive", lambda pid: False)
    _write(home, pid=4242)
    assert update.progress(now=NOW)["state"] == "failed"


def test_a_seed_the_installer_never_overwrote_fails_after_the_grace(home):
    _write(home, step="", pid=0, at=NOW - 10)
    assert update.progress(now=NOW)["state"] == "running"
    _write(home, step="", pid=0, at=NOW - 600)
    assert update.progress(now=NOW)["state"] == "failed"


def test_a_running_record_with_no_write_for_too_long_is_stale(home):
    _write(home, at=NOW - update.PROGRESS_STALE_S - 1)
    assert update.progress(now=NOW)["state"] == "stale"


def test_done_records_the_duration_once_for_last_update_took(home):
    _write(home, state="done", step="health", started_at=NOW - 400, at=NOW - 40)
    p = update.progress(now=NOW)
    assert p["state"] == "done" and p["elapsed_s"] == 360
    assert p["last_duration_s"] == 360
    # A later run shows the previous duration while it is running.
    _write(home, state="running", step="fetch", started_at=NOW - 20, at=NOW - 1)
    assert update.progress(now=NOW)["last_duration_s"] == 360


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["running"]),
        json.dumps({"state": "hacked", "step": "web", "pid": 1, "started_at": 1, "at": 1}),
        json.dumps({"state": "running", "step": "<script>", "pid": 1, "started_at": 1, "at": 1}),
        json.dumps({"state": "running", "step": "web", "pid": True, "started_at": 1, "at": 1}),
        json.dumps({"state": "running", "step": "web", "pid": 1, "started_at": -5, "at": 1}),
        "{" + " " * 5000 + "}",
    ],
)
def test_anything_malformed_reads_as_idle_and_no_file_text_is_echoed(home, raw):
    (home / "update-progress.json").write_text(raw)
    p = update.progress(now=NOW)
    assert p["state"] == "idle"
    assert "<script>" not in json.dumps(p)


def test_apply_names_the_progress_file_and_seeds_it(home, monkeypatch):
    inst = home / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": "a" * 40})
    captured = {}
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: captured.update(kw) or SimpleNamespace()
    )
    assert update.apply() is True
    env = captured["env"]
    assert env["AGENT_SESSIONS_UPDATE_PROGRESS"] == str(home / "update-progress.json")
    assert env["AGENT_SESSIONS_UPDATE_STARTED"].isdigit()
    seeded = json.loads((home / "update-progress.json").read_text())
    assert seeded["state"] == "running" and seeded["pid"] == 0
    assert update.progress()["state"] == "running"


def test_progress_route_requires_login_and_serves_the_record(auth_cfg, home):
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert c.get("/api/update/progress", follow_redirects=False).status_code in (401, 303)
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    _write(home, step="python", at=int(__import__("time").time()))
    d = c.get("/api/update/progress").json()
    assert d["state"] == "running" and d["label"] == "Installing the Python package"


def _until_zombie(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] == "Z":
                return
        except OSError:
            return
        time.sleep(0.02)


@pytest.fixture
def installer(monkeypatch):
    """Register a real child as `apply()` does, and always leave it waited on."""
    spawned = []

    def spawn(argv):
        child = subprocess.Popen(argv, start_new_session=True)  # noqa: S603
        spawned.append(child)
        monkeypatch.setattr(update, "_INSTALLER", child)
        return child

    yield spawn
    for c in spawned:
        if c.poll() is None:
            c.kill()
        c.wait()


def test_an_installer_that_exited_is_not_running_even_as_an_unreaped_child(home, installer):
    """Hermes on #1089: a child that exited through `set -e` stays a ZOMBIE until reaped, and
    signal 0 succeeds for a zombie. A real child, never waited on, as `apply()` leaves it."""
    child = installer(["sh", "-c", "exit 1"])
    _until_zombie(child.pid)
    _write(home, pid=child.pid, at=NOW - 5)
    assert update.progress(now=NOW)["state"] == "failed"
    # Reaped through its own handle, so the Popen knows how it ended.
    assert child.returncode == 1


def test_a_live_child_installer_reads_as_running(home, installer):
    child = installer(["sleep", "5"])
    _write(home, pid=child.pid, at=NOW - 5)
    assert update.progress(now=NOW)["state"] == "running"


def test_a_pid_that_is_not_our_installer_is_looked_at_never_reaped(home):
    """A stale record's PID can belong to ANOTHER child of the app by now. Reading progress must
    not steal its exit status — the check sees the zombie and leaves the reaping to its owner."""
    other = subprocess.Popen(["sh", "-c", "exit 7"])  # noqa: S603, S607
    try:
        _until_zombie(other.pid)
        _write(home, pid=other.pid, at=NOW - 5)
        assert update.progress(now=NOW)["state"] == "failed"
    finally:
        assert other.wait(timeout=5) == 7
