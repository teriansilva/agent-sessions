"""#950 Phase 1 — editing a file in place from the viewer.

The interesting tests are the adversarial ones, and each drives the REAL save against a real file,
a real kernel lease and a real second process — a stub that collapsed the timeline could not see a
bug whose cause is ordering:

* a process that already holds the file open (a writer, a reader, a non-dumpable one whose
  ``/proc`` entry is unreadable) refuses the lease, so nothing is written;
* a process that opens the file, replaces it, or claims the freed name during the save leaves the
  file as that process left it;
* the save process is SIGKILLed after every step, and after a recovery action, and recovery puts
  the original bytes back at the name or keeps them in the store — never neither.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import fileedit, files
from agent_sessions.files import FsError


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _reset():
    files._inflight_total = 0
    files._inflight_by_root.clear()
    fileedit._HOOK = None
    yield
    fileedit._HOOK = None
    files._inflight_total = 0
    files._inflight_by_root.clear()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "home"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(r))
    # Inside the panel's boundary, so a retained copy is something the viewer can open.
    monkeypatch.setenv("AGENT_SESSIONS_EDIT_RECOVERY", str(r / ".agent-sessions" / "edit-recovery"))
    files.reset_capabilities_for_test()
    # Generous CEILINGS for the tests (#1107). The production budgets (a 10 s save, a 5 s wait for
    # the store lock) are unchanged; on a starved shared runner a correct save exceeded them and
    # these tests failed with 503 "took too long" / "another save is still running". Neither
    # budget is what these tests are about, and a correct save passes the instant it finishes.
    # The two expiry paths keep their own deterministic tests (search `budget_expiry`).
    monkeypatch.setattr(fileedit, "LEASE_BUDGET_S", 120.0)
    monkeypatch.setattr(fileedit, "STORE_LOCK_WAIT_S", 120.0)
    prev = signal.getsignal(signal.SIGIO)
    assert fileedit.install_lease_signal_handler()
    yield r
    signal.signal(signal.SIGIO, prev)
    files.reset_capabilities_for_test()


def store(root) -> str:
    return fileedit.recovery_dir()


def records(root) -> list[dict]:
    out = []
    s = store(root)
    if not os.path.isdir(s):
        return out
    for entry in sorted(os.listdir(s)):
        p = os.path.join(s, entry, "record.json")
        if os.path.exists(p):
            with open(p) as f:
                out.append(json.load(f))
    return out


def leftovers(d) -> list[str]:
    return [n for n in os.listdir(d) if ".battlelab-save-" in n]


HOLD = r"""
import ctypes, sys, time
mode, path, dumpable = sys.argv[1], sys.argv[2], sys.argv[3]
if dumpable == "0":
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: /proc/<pid>/fd becomes unreadable
f = open(path, mode)
print("ready", flush=True)
time.sleep(60)
"""


@contextlib.contextmanager
def holding(path, mode="ab", dumpable=True):
    p = subprocess.Popen(
        [sys.executable, "-c", HOLD, mode, str(path), "1" if dumpable else "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert p.stdout.readline().strip() == "ready"
        yield p
    finally:
        p.kill()
        p.wait()


# --------------------------------------------------------------------------- read


def test_plain_text_is_editable_with_the_version_of_its_bytes(root):
    p = root / "a.py"
    p.write_bytes(b"x = 1\ny = 2\n")
    out = fileedit.read_file(str(p))
    assert out["editable"] is True, out["readonly_reason"]
    assert out["readonly_reason"] is None
    assert out["version"] == sha(b"x = 1\ny = 2\n")
    assert (out["eol"], out["bom"]) == ("\n", False)
    assert out["content"] == "x = 1\ny = 2\n"


def test_crlf_and_bom_are_reported_and_the_bom_is_not_rendered(root):
    p = root / "win.txt"
    p.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    out = fileedit.read_file(str(p))
    assert out["editable"] is True
    assert (out["eol"], out["bom"]) == ("\r\n", True)
    assert not out["content"].startswith("﻿")


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (b"a" * (files.FILES_MAX_READ + 1), "larger than 1 MiB"),
        (b"caf\xe9\n", "not valid UTF-8"),
        (b"one\r\ntwo\n", "mixes CRLF and LF"),
        (b"one\rtwo\r", "bare CR"),
        (b"\x00\x01binary", "binary"),
    ],
)
def test_bytes_that_cannot_round_trip_are_read_only_with_the_reason(root, data, fragment):
    p = root / "f.bin"
    p.write_bytes(data)
    out = fileedit.read_file(str(p))
    assert out["editable"] is False
    assert fragment in out["readonly_reason"]


def test_a_truncated_read_has_no_version_to_save_against(root):
    p = root / "big.txt"
    p.write_bytes(b"a" * (files.FILES_MAX_READ + 10))
    assert fileedit.read_file(str(p))["version"] is None


def test_a_hard_linked_file_is_read_only(root):
    p = root / "a.txt"
    p.write_text("x\n")
    os.link(p, root / "b.txt")
    out = fileedit.read_file(str(p))
    assert out["editable"] is False and "hard links" in out["readonly_reason"]


def test_git_metadata_is_read_only(root):
    g = root / "repo" / ".git"
    (g / "objects").mkdir(parents=True)
    (g / "refs").mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "config").write_text("[core]\n")
    out = fileedit.read_file(str(g / "config"))
    assert out["editable"] is False and "git metadata" in out["readonly_reason"]


def test_a_file_owned_by_someone_else_is_read_only(root, monkeypatch):
    p = root / "a.txt"
    p.write_text("x\n")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 4242)
    out = fileedit.read_file(str(p))
    assert out["editable"] is False and "another user" in out["readonly_reason"]


def test_without_the_lease_signal_handler_everything_is_read_only(root):
    p = root / "a.txt"
    p.write_text("x\n")
    signal.signal(signal.SIGIO, signal.SIG_DFL)
    out = fileedit.read_file(str(p))
    assert out["editable"] is False and "lease notifications" in out["readonly_reason"]
    with pytest.raises(FsError) as e:
        fileedit.save(str(p), "y\n", sha(b"x\n"))
    assert e.value.status == 501
    assert p.read_text() == "x\n"


def test_the_handler_is_a_handler_so_launched_processes_do_not_inherit_an_ignored_sigio(root):
    assert signal.getsignal(signal.SIGIO) not in (signal.SIG_IGN, signal.SIG_DFL)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import signal; print(signal.getsignal(signal.SIGIO) == signal.SIG_DFL)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "True"


# --------------------------------------------------------------------------- save


def test_save_replaces_the_bytes_and_retains_the_version_it_replaced(root):
    p = root / "a.py"
    p.write_bytes(b"old\n")
    out = fileedit.save(str(p), "new\n", sha(b"old\n"))
    assert p.read_bytes() == b"new\n"
    assert out["version"] == sha(b"new\n")
    retained = out["retained"]["path"]
    assert out["retained"]["version"] == sha(b"old\n")
    with open(retained, "rb") as f:
        assert f.read() == b"old\n"
    assert retained.startswith(store(root) + os.sep)
    assert [r["state"] for r in records(root)] == ["complete"]
    assert leftovers(root) == []
    assert os.stat(p).st_nlink == 1  # still editable


def test_a_retained_copy_opens_in_the_viewer_but_is_read_only(root):
    p = root / "a.py"
    p.write_bytes(b"old\n")
    retained = fileedit.save(str(p), "new\n", sha(b"old\n"))["retained"]["path"]
    out = fileedit.read_file(retained)
    assert out["content"] == "old\n"
    assert out["editable"] is False and "recovery store" in out["readonly_reason"]


def test_a_retained_copy_is_read_only_when_the_store_is_a_symlink(root, monkeypatch):
    """Hermes on #955: with the configured store a symlink, the gate compared a resolved file path
    against the unresolved store, so a retained original read as editable and a save replaced it."""
    physical = root / "physical-store"
    physical.mkdir()
    link = root / "store-link"
    os.symlink(physical, link)
    monkeypatch.setenv("AGENT_SESSIONS_EDIT_RECOVERY", str(link))
    p = root / "work.txt"
    p.write_bytes(b"old\n")
    retained = fileedit.save(str(p), "new\n", sha(b"old\n"))["retained"]["path"]
    for spelling in {retained, os.path.realpath(retained)}:
        out = fileedit.read_file(spelling)
        assert out["editable"] is False and "recovery store" in out["readonly_reason"], spelling
    with pytest.raises(FsError) as e:
        fileedit.save(os.path.realpath(retained), "rewritten\n", sha(b"old\n"))
    assert e.value.status == 409
    assert open(os.path.realpath(retained), "rb").read() == b"old\n"


@pytest.mark.parametrize("mode", [0o755, 0o644, 0o600])
def test_permissions_are_kept_exactly(root, mode):
    p = root / "run.sh"
    p.write_bytes(b"#!/bin/sh\n")
    os.chmod(p, mode)
    fileedit.save(str(p), "#!/bin/sh\necho hi\n", sha(b"#!/bin/sh\n"))
    assert os.stat(p).st_mode & 0o7777 == mode


def test_crlf_and_bom_round_trip_byte_for_byte(root):
    original = b"\xef\xbb\xbfone\r\ntwo\r\n"
    p = root / "win.txt"
    p.write_bytes(original)
    loaded = fileedit.read_file(str(p))
    as_editor_sends = loaded["content"].replace("\r\n", "\n")
    # Unchanged text is not a write at all.
    same = fileedit.save(str(p), as_editor_sends, loaded["version"])
    assert same["retained"] is None and p.read_bytes() == original and records(root) == []
    fileedit.save(str(p), as_editor_sends.replace("two", "TWO"), loaded["version"])
    assert p.read_bytes() == b"\xef\xbb\xbfone\r\nTWO\r\n"


def test_budget_expiry_a_save_past_its_deadline_is_refused_and_replaces_nothing(root, monkeypatch):
    """The deadline the fixture raises for every other test still refuses, deterministically —
    with the original bytes untouched (#1107: the ceiling must not turn expiry into a pass)."""
    p = root / "deadline.txt"
    p.write_bytes(b"original\n")
    loaded = fileedit.read_file(str(p))
    monkeypatch.setattr(fileedit, "LEASE_BUDGET_S", -1.0)  # already expired at the first check
    with pytest.raises(FsError) as e:
        fileedit.save(str(p), "replacement\n", loaded["version"])
    assert e.value.status == 503 and "took too long" in str(e.value)
    assert p.read_bytes() == b"original\n"


def test_budget_expiry_a_save_that_cannot_get_the_store_lock_is_refused(root, monkeypatch):
    """Another save holding the recovery store: past the wait, a 503 — never a second writer."""
    import fcntl

    p = root / "locked.txt"
    p.write_bytes(b"original\n")
    loaded = fileedit.read_file(str(p))
    store_dir = fileedit.recovery_dir()
    os.makedirs(store_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(store_dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        monkeypatch.setattr(fileedit, "STORE_LOCK_WAIT_S", 0.1)
        with pytest.raises(FsError) as e:
            fileedit.save(str(p), "replacement\n", loaded["version"])
        assert e.value.status == 503 and "another save is still running" in str(e.value)
    finally:
        os.close(fd)
    assert p.read_bytes() == b"original\n"


def test_a_stale_version_is_refused_and_names_the_version_on_disk(root):
    p = root / "a.txt"
    p.write_bytes(b"agent wrote this\n")
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(p), "mine\n", sha(b"what I loaded\n"))
    assert e.value.status == 409
    assert e.value.fields["reason"] == "changed"
    assert e.value.fields["version"] == sha(b"agent wrote this\n")
    assert p.read_bytes() == b"agent wrote this\n"
    assert records(root) == []


@pytest.mark.parametrize(
    "bad", [{"content": "a\r\nb"}, {"expect": "not-a-version"}, {"content": 7}]
)
def test_malformed_saves_are_rejected_before_anything_is_touched(root, bad):
    p = root / "a.txt"
    p.write_bytes(b"x\n")
    args = {"content": "y\n", "expect": sha(b"x\n"), **bad}
    with pytest.raises(FsError) as e:
        fileedit.save(str(p), args["content"], args["expect"])
    assert e.value.status == 422
    assert p.read_bytes() == b"x\n"


def test_a_save_into_git_metadata_is_refused(root):
    g = root / "repo" / ".git"
    (g / "objects").mkdir(parents=True)
    (g / "refs").mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    hook = g / "config"
    hook.write_bytes(b"[core]\n")
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(hook), "[core]\n\tfsmonitor = evil\n", sha(b"[core]\n"))
    # A 409 with its reason, not a 403: the client reads 403 as an auth failure and would show
    # "forbidden" instead of why this file cannot be saved.
    assert e.value.status == 409 and e.value.fields["reason"] == "not_editable"
    assert hook.read_bytes() == b"[core]\n"


def test_a_symlink_and_a_path_outside_the_root_are_refused(root, tmp_path):
    (root / "real.txt").write_text("x\n")
    (root / "alias.txt").symlink_to(root / "real.txt")
    with pytest.raises(FsError) as e:
        fileedit.save(str(root / "alias.txt"), "y\n", sha(b"x\n"))
    assert e.value.status == 422
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")
    with pytest.raises(FsError) as e:
        fileedit.save(str(outside), "y\n", sha(b"x\n"))
    assert e.value.status == 403
    assert outside.read_text() == "x\n"


# --------------------------------------------------------------------------- other processes


@pytest.mark.parametrize(
    ("mode", "dumpable"),
    [("ab", True), ("rb", True), ("ab", False)],
    ids=["writer", "reader", "non-dumpable"],
)
def test_a_process_holding_the_file_open_refuses_the_save_before_anything_is_written(
    root, mode, dumpable
):
    p = root / "log.txt"
    p.write_bytes(b"line\n")
    with holding(p, mode, dumpable) as child:
        with pytest.raises(fileedit.SaveRefused) as e:
            fileedit.save(str(p), "replaced\n", sha(b"line\n"))
        assert e.value.fields["reason"] == "open_elsewhere"
        holder = e.value.fields["holder"]
        # Naming the holder is best-effort BY CONTRACT: `lease_holder` walks /proc under
        # HOLDER_SCAN_BUDGET_S (0.3 s) and gives up rather than delay a save to decorate a
        # message. On a loaded runner — ~4,500 tests, ~1 h, thousands of pids in arbitrary
        # `listdir` order — that budget expires before the walk reaches this child and the field
        # is None. Asserting it is ALWAYS populated made this test the only failure on three
        # unrelated branches (CI python tasks 18569, 18564, 18550), hitting one or both dumpable
        # parametrisations depending on where the walk ran out. Reproduced deterministically by
        # forcing the budget negative; the refusal below is unaffected either way.
        #
        # What must hold is that the panel never names the WRONG process. Whether it manages to
        # name one at all is a decoration; the refusal itself is the kernel's answer (asserted
        # above) and never depends on the scan.
        assert holder is None or holder["pid"] == child.pid
        # A non-dumpable holder is not NAMEABLE — and is refused all the same, which is the point
        # of asking the kernel rather than scanning /proc.
    assert p.read_bytes() == b"line\n"
    assert records(root) == [] and leftovers(root) == []


def _opener(path, started: threading.Event):
    def run():
        started.set()
        with open(path, "ab") as f:  # blocks on the save's lease until it is released
            f.write(b"late\n")

    t = threading.Thread(target=run)
    return t


@pytest.mark.parametrize("step", ["intent", "stage"])
def test_an_open_arriving_during_the_save_puts_the_original_back(root, step):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    started = threading.Event()
    t = _opener(p, started)

    def hook(name):
        if name == step:
            t.start()
            started.wait()
            time.sleep(0.3)  # let the open reach the kernel and break the lease

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    t.join(10)
    assert e.value.fields["reason"] == "opened_during_save"
    # The blocked writer proceeded against the ORIGINAL, at its name. Nothing was lost or merged.
    assert p.read_bytes() == b"orig\nlate\n"
    # The store keeps its name for the original (review 4815), so the writer's bytes are there too,
    # and both the refusal and the file itself say why it is read-only now.
    assert os.stat(p).st_nlink == 2 and _store_holds(b"orig\nlate\n")
    assert "recovery store" in str(e.value)
    out = fileedit.read_file(str(p))
    assert out["editable"] is False and "recovery store" in out["readonly_reason"]
    assert leftovers(root) == []
    assert [r["state"] for r in records(root)] == ["reverted"]


def test_a_lease_broken_between_the_check_and_the_install_is_still_caught(root, monkeypatch):
    """The install re-checks the lease. Forcing a break into exactly that gap from user space is
    not possible, so the kernel's answer is sequenced: intact at the check, broken at install."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    answers = iter([True, False])
    monkeypatch.setattr(fileedit, "_lease_intact", lambda fd: next(answers))
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert e.value.fields["reason"] == "opened_during_save"
    assert p.read_bytes() == b"orig\n" and leftovers(root) == []


def test_a_file_replaced_by_rename_during_the_save_is_left_as_the_other_writer_left_it(root):
    """An atomic rename-over never opens the leased inode, so the lease cannot see it — the
    inode check after displacement does."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")

    def hook(name):
        if name == "stage":
            tmp = root / "agent.tmp"
            tmp.write_bytes(b"agent version\n")
            os.rename(tmp, p)

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused):
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert p.read_bytes() == b"agent version\n"
    assert leftovers(root) == []


def test_a_name_claimed_after_displacement_is_kept_and_both_versions_survive(root):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")

    def hook(name):
        if name == "check":
            with open(p, "xb") as f:
                f.write(b"claimant\n")

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert p.read_bytes() == b"claimant\n"
    kept = [x for x in e.value.fields["both"] if x != str(p)]
    with open(kept[0], "rb") as f:
        assert f.read() == b"orig\n"
    assert leftovers(root) == []


def test_bytes_written_into_the_displaced_inode_after_the_save_are_retained_not_lost(root):
    """The stated boundary's CONSEQUENCE: a descriptor on the old inode (here reached through the
    retained name, the only way user space can reach it after the fact) writes into the retained
    copy — which nothing in this app ever removes."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    retained = fileedit.save(str(p), "mine\n", sha(b"orig\n"))["retained"]["path"]
    with open(retained, "ab") as f:
        f.write(b"late\n")
    _resolve()
    with open(retained, "rb") as f:
        assert f.read() == b"orig\nlate\n"
    assert p.read_bytes() == b"mine\n"


# --------------------------------------------------------------------------- retention


def test_resolution_never_removes_a_retained_copy_however_old_its_record(root):
    """Hermes on #955 (review 4807, 4): automatic pruning could not be made safe. Its last step was
    a lease check followed by an ``unlink``, and no check excludes an ``open`` that arrives after
    it — measured, a writer that opened in that gap succeeded with ``st_nlink == 0`` and its bytes
    had no name left. So retained copies are kept until the operator deletes them: a record settled
    decades ago survives any number of resolution passes, with its copy and its record."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    retained = fileedit.save(str(p), "mine\n", sha(b"orig\n"))["retained"]["path"]
    record_path = os.path.join(os.path.dirname(retained), "record.json")
    with open(record_path) as f:
        rec = json.load(f)
    assert rec["state"] == "complete"
    rec["created"] = rec["settled"] = 1.0  # 1970: older than any retention window could be
    with open(record_path, "w") as f:
        json.dump(rec, f)
    for _ in range(3):
        _resolve()
    fileedit.startup_resolve()
    with open(retained, "rb") as f:
        assert f.read() == b"orig\n"
    assert os.path.exists(record_path)


# --------------------------------------------------------------------------- durability


def test_fsyncs_happen_in_the_specified_order(root, monkeypatch):
    seen: list[str] = []
    named: list[str] = []
    real = fileedit._fsync

    def record(fd, label):
        seen.append(label)
        if label == "store-parent":
            named.append(os.readlink(f"/proc/self/fd/{fd}"))
        real(fd, label)

    monkeypatch.setattr(fileedit, "_fsync", record)
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    # 0 the store did not exist. Every directory on its path that this user owns — so every one a
    #   first use, or an earlier first use that failed part-way, could have created — is made
    #   durable in its PARENT, top down, BEFORE the store is relied on as the only other name for
    #   displaced bytes. How many that is depends on where pytest's tmp dir is; the last two name
    #   `.agent-sessions` and then `edit-recovery`.
    parents = seen.count("store-parent")
    home = os.path.realpath(root)
    assert seen[:parents] == ["store-parent"] * parents
    assert named[-2:] == [home, os.path.join(home, ".agent-sessions")]
    assert seen[parents:] == [
        # 1 intent: the record, its entry, the store that now names the entry
        "record", "entry", "store",
        # 2 stage: the new bytes, then the directory that names the temporary
        "temp", "target",
        # 3 displace: the store side gains the name BEFORE the target side loses it
        "entry", "target",
        # 5 install
        "target",
        # 6 finish: the temporary's name goes, then the record says complete
        "target", "record", "entry",
    ]  # fmt: skip


CHILD = r"""
import os, signal, sys
from agent_sessions import fileedit
assert fileedit.install_lease_signal_handler()
# The same test CEILINGS the `root` fixture sets (#1107) — this child is another process, so the
# fixture's monkeypatch never reached it and a starved runner expired the real 10 s budget here.
fileedit.LEASE_BUDGET_S = 120.0
fileedit.STORE_LOCK_WAIT_S = 120.0
step, path, content, expect, action = sys.argv[1:6]

def hook(name):
    if name == step:
        os.kill(os.getpid(), signal.SIGKILL)

fileedit._HOOK = hook
if action == "save":
    fileedit.save(path, content, expect)
else:
    with fileedit._store_locked() as fd:
        fileedit.resolve_pending(fd)
print("finished", flush=True)
"""


def _child(root, step, path, action="save", content="mine\n", expect=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_SESSIONS_")}
    env["AGENT_SESSIONS_FS_ROOT"] = str(root)
    env["AGENT_SESSIONS_EDIT_RECOVERY"] = fileedit.recovery_dir()
    return subprocess.run(
        [sys.executable, "-c", CHILD, step, str(path), content, expect or "", action],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _resolve():
    with fileedit._store_locked() as fd:
        fileedit.resolve_pending(fd)


@pytest.mark.parametrize(
    ("step", "survivor", "names"),
    [
        ("intent", b"orig\n", 1),
        ("stage", b"orig\n", 1),
        # Displaced, so recovery links it back and the store keeps its own name (review 4815).
        ("displace", b"orig\n", 2),
        ("check", b"orig\n", 2),
        ("install", b"mine\n", 1),
        ("finish", b"mine\n", 1),
    ],
)
def test_a_save_killed_after_any_step_recovers_without_losing_the_original(
    root, step, survivor, names
):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    r = _child(root, step, p, expect=sha(b"orig\n"))
    assert r.returncode == -signal.SIGKILL, r.stderr
    _resolve()
    _resolve()  # idempotent: a second pass finds nothing left to do and changes nothing
    assert p.read_bytes() == survivor
    assert os.stat(p).st_nlink == names
    assert leftovers(root) == []
    states = [rec["state"] for rec in records(root)]
    assert "intent" not in states
    if survivor == b"mine\n":
        (rec,) = records(root)
        with open(os.path.join(store(root), rec["id"], rec["retained"]), "rb") as f:
            assert f.read() == b"orig\n"


def test_recovery_killed_mid_put_back_converges_on_the_next_run(root):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    assert _child(root, "displace", p, expect=sha(b"orig\n")).returncode == -signal.SIGKILL
    assert not p.exists()  # the bytes are in the store, not at the name
    r = _child(root, "put_back:linked", p, action="resolve")
    assert r.returncode == -signal.SIGKILL, r.stderr
    assert os.stat(p).st_nlink == 2  # linked back
    _resolve()
    assert p.read_bytes() == b"orig\n"
    assert os.stat(p).st_nlink == 2  # and the store's name is kept, never dropped
    assert [rec["state"] for rec in records(root)] == ["reverted"]


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def _hdr(c, cfg):
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_read_route_carries_the_edit_fields(client, root):
    p = root / "a.txt"
    p.write_bytes(b"x\n")
    r = client.get("/api/files/read", params={"path": str(p)})
    assert r.status_code == 200
    assert r.json()["editable"] is True and r.json()["version"] == sha(b"x\n")


def test_write_route_saves_and_is_never_cached(client, root, auth_cfg):
    p = root / "a.txt"
    p.write_bytes(b"x\n")
    r = client.post(
        "/api/files/write",
        json={"path": str(p), "content": "y\n", "expect": sha(b"x\n")},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert p.read_bytes() == b"y\n"


def test_write_route_requires_csrf(client, root):
    p = root / "a.txt"
    p.write_bytes(b"x\n")
    r = client.post(
        "/api/files/write", json={"path": str(p), "content": "y\n", "expect": sha(b"x\n")}
    )
    assert r.status_code == 403
    assert p.read_bytes() == b"x\n"


def test_write_route_returns_the_refusal_fields(client, root, auth_cfg):
    p = root / "a.txt"
    p.write_bytes(b"agent\n")
    r = client.post(
        "/api/files/write",
        json={"path": str(p), "content": "y\n", "expect": sha(b"loaded\n")},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "changed" and body["version"] == sha(b"agent\n") and body["detail"]


def test_write_route_refuses_an_oversized_body(client, root, auth_cfg):
    p = root / "a.txt"
    p.write_bytes(b"x\n")
    r = client.post(
        "/api/files/write",
        content=b'{"path": "' + b"a" * (fileedit.MAX_BODY_BYTES + 10) + b'"}',
        headers={**_hdr(client, auth_cfg), "Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_concurrent_saves_through_the_server_let_exactly_one_win(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    app = main.create_app()
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    clients = [TestClient(app) for _ in range(3)]
    headers = [_hdr(c, auth_cfg) for c in clients]
    gate = threading.Barrier(len(clients))
    results: list[int] = []

    def go(i):
        gate.wait()
        r = clients[i].post(
            "/api/files/write",
            json={"path": str(p), "content": f"writer {i}\n", "expect": sha(b"orig\n")},
            headers=headers[i],
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(clients))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == [200, 409, 409]
    assert p.read_bytes() in {f"writer {i}\n".encode() for i in range(3)}
    assert leftovers(root) == []


def test_a_lease_break_during_an_in_flight_request_is_a_409(client, root, auth_cfg):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    started = threading.Event()
    t = _opener(p, started)

    def hook(name):
        if name == "stage":
            t.start()
            started.wait()
            time.sleep(0.3)

    headers = _hdr(client, auth_cfg)
    fileedit._HOOK = hook
    r = client.post(
        "/api/files/write",
        json={"path": str(p), "content": "mine\n", "expect": sha(b"orig\n")},
        headers=headers,
    )
    t.join(10)
    assert r.status_code == 409 and r.json()["reason"] == "opened_during_save"
    assert p.read_bytes() == b"orig\nlate\n"


# --------------------------------------------------------------------------- review round 1 (#955)


def test_fsyncs_skip_the_store_parents_once_the_store_is_known_durable(root, monkeypatch):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    fileedit.save(str(p), "mine\n", sha(b"orig\n"))  # first use: the store, synced, then its marker
    seen: list[str] = []
    real = fileedit._fsync
    monkeypatch.setattr(fileedit, "_fsync", lambda fd, label: (seen.append(label), real(fd, label)))
    fileedit.save(str(p), "again\n", sha(b"mine\n"))
    assert "store-parent" not in seen


@pytest.mark.parametrize("fails_at", ["ancestor", "store"])
def test_a_store_parent_fsync_that_failed_is_redone_before_the_store_is_used(
    root, monkeypatch, fails_at
):
    """Hermes on #955 (review 4807, 6): ``mkdir`` succeeded, the ``fsync`` that makes its entry
    durable failed, and the retry saw an existing directory and synced nothing — a later
    displacement then relied on a directory entry that was never made durable. Failing at the
    ancestor (`.agent-sessions` created, its name unsynced) and at the store itself."""
    home = os.path.realpath(root)
    failing = home if fails_at == "ancestor" else os.path.join(home, ".agent-sessions")
    synced: list[str] = []
    armed = [True]
    real = fileedit._fsync

    def fsync(fd, label):
        if label == "store-parent":
            path = os.readlink(f"/proc/self/fd/{fd}")
            if armed and path == failing:
                armed.clear()
                raise OSError(errno.EIO, "injected")
            synced.append(path)
        real(fd, label)

    monkeypatch.setattr(fileedit, "_fsync", fsync)
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    with pytest.raises(FsError) as e:
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert e.value.status == 503 and not armed
    assert p.read_bytes() == b"orig\n" and os.path.isdir(os.path.join(home, ".agent-sessions"))
    synced.clear()
    fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert p.read_bytes() == b"mine\n"
    # The retry synced BOTH names the failed attempt may have created, not only what was missing.
    assert {home, os.path.join(home, ".agent-sessions")} <= set(synced), synced
    synced.clear()
    fileedit.save(str(p), "again\n", sha(b"mine\n"))
    assert synced == []  # and only until that has succeeded once


def test_a_store_in_a_home_whose_parent_cannot_be_read_still_saves(root, monkeypatch):
    """The operator's home is never synced into its parent: no first use creates it, and an
    ordinary user cannot open that parent where it is 0711 — which failed every save with a 503."""
    home = os.path.realpath(root)
    monkeypatch.setenv("HOME", str(root))
    real_open = os.open

    def refuse_homes_parent(path, flags, *args, **kwargs):
        if path == os.path.dirname(home) and flags & os.O_DIRECTORY:
            raise PermissionError(errno.EACCES, "injected", path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_homes_parent)
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    assert p.read_bytes() == b"mine\n"


# 1 — the recovery store is untrusted input


_FORGED_ID = "1700000000-deadbeef0000"


def _forge(root, **over) -> str:
    """Plant a journal the way a client that could write into the store would."""
    d = root / "proj"
    d.mkdir(exist_ok=True)
    rec = {
        "id": _FORGED_ID,
        "target": str(d / "x.txt"),
        "parent": str(d),
        "name": "x.txt",
        "tmp": f".x.txt.battlelab-save-{_FORGED_ID}",
        "retained": "previous-x.txt",
        "expect": "0" * 64,
        "new_version": "1" * 64,
        "created": 1.0,
        "state": "intent",
        **over,
    }
    entry = os.path.join(store(root), _FORGED_ID)
    os.makedirs(entry, exist_ok=True)
    with open(os.path.join(entry, "record.json"), "w") as f:
        json.dump(rec, f)
    return entry


@pytest.mark.parametrize("store_exists", [False, True], ids=["store-absent", "store-present"])
def test_the_upload_route_cannot_write_into_the_recovery_store(
    client, root, auth_cfg, store_exists
):
    if store_exists:
        os.makedirs(store(root))
    rel = os.path.relpath(os.path.join(store(root), _FORGED_ID, "record.json"), root)
    r = client.post(
        "/api/files/upload",
        data={"dir": str(root), "relpath": rel},
        files={"file": ("record.json", b"{}", "application/octet-stream")},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 403, r.text
    assert not os.path.exists(os.path.join(store(root), _FORGED_ID))


@pytest.mark.parametrize(
    "how",
    ["absolute-tmp", "traversing-tmp", "traversing-retained-settled"],
)
def test_a_forged_journal_never_mutates_anything_outside_the_root(root, tmp_path, how):
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious\n")
    if how == "absolute-tmp":
        entry = _forge(root, tmp=str(victim))
    elif how == "traversing-tmp":
        entry = _forge(root, tmp="../../victim.txt")
    else:
        entry = _forge(
            root,
            state="complete",
            retained="../../../../victim.txt",
            expect=sha(b"precious\n"),
            created=1.0,
            settled=1.0,
        )
    # An unrelated save runs recovery first — and must still succeed.
    other = root / "other.txt"
    other.write_bytes(b"o\n")
    fileedit.save(str(other), "O\n", sha(b"o\n"))
    assert other.read_bytes() == b"O\n"
    for _ in range(2):
        _resolve()
    fileedit.startup_resolve()
    assert victim.read_bytes() == b"precious\n"
    # Never acted on: the forged entry is set aside, not resolved.
    assert not os.path.exists(os.path.join(entry, "record.json")) or all(
        r.get("id") != _FORGED_ID or r.get("state") in ("intent", "complete") for r in records(root)
    )


# 2 — permissions beyond the mode bits


needs_acl = pytest.mark.skipif(shutil.which("setfacl") is None, reason="setfacl is not installed")


def _acl(path) -> str:
    return subprocess.run(
        ["getfacl", "-p", "--omit-header", str(path)], capture_output=True, text=True, check=True
    ).stdout


def _second_group() -> int:
    other = next((g for g in os.getgroups() if g != os.getegid()), None)
    if other is None:
        pytest.skip("the test user belongs to no second group to move a file into")
    return other


@needs_acl
def test_an_explicit_restrictive_acl_survives_a_save(root):
    p = root / "secret.txt"
    p.write_bytes(b"x\n")
    os.chmod(p, 0o640)
    subprocess.run(["setfacl", "-m", "u:nobody:---", str(p)], check=True)
    before = _acl(p)
    assert "user:nobody:---" in before
    fileedit.save(str(p), "y\n", sha(b"x\n"))
    assert p.read_bytes() == b"y\n"
    assert _acl(p) == before
    assert os.stat(p).st_mode & 0o7777 == 0o640


@needs_acl
def test_a_parent_default_acl_does_not_grant_what_the_file_did_not_have(root):
    d = root / "shared"
    d.mkdir()
    p = d / "a.txt"
    p.write_bytes(b"x\n")
    os.chmod(p, 0o600)
    subprocess.run(["setfacl", "-d", "-m", "u:nobody:r", str(d)], check=True)
    assert "nobody" not in _acl(p)
    fileedit.save(str(p), "y\n", sha(b"x\n"))
    assert p.read_bytes() == b"y\n"
    assert "nobody" not in _acl(p)
    assert os.stat(p).st_mode & 0o7777 == 0o600


def test_the_files_group_is_kept(root):
    other = _second_group()
    p = root / "g.txt"
    p.write_bytes(b"x\n")
    os.chown(p, -1, other)
    fileedit.save(str(p), "y\n", sha(b"x\n"))
    assert os.stat(p).st_gid == other


def test_a_group_that_cannot_be_kept_refuses_before_anything_is_displaced(root, monkeypatch):
    other = _second_group()
    p = root / "g.txt"
    p.write_bytes(b"x\n")
    os.chown(p, -1, other)

    def denied(fd, uid, gid):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", denied)
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(p), "y\n", sha(b"x\n"))
    assert e.value.fields["reason"] == "not_editable"
    assert p.read_bytes() == b"x\n" and os.stat(p).st_gid == other
    assert leftovers(root) == []
    for rec in records(root):
        assert rec["state"] != "intent"
        assert not os.path.exists(os.path.join(store(root), rec["id"], rec["retained"]))


# 3 — a `.git` FILE is git metadata too


def _run_git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_a_linked_worktrees_git_pointer_is_read_only_and_unsaveable(root):
    main = root / "main"
    main.mkdir()
    _run_git(main, "init", "-q", "-b", "main")
    _run_git(
        main,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    _run_git(main, "worktree", "add", "-q", str(root / "wt"))
    pointer = root / "wt" / ".git"
    assert pointer.is_file()
    data = pointer.read_bytes()
    out = fileedit.read_file(str(pointer))
    assert out["editable"] is False and "git" in out["readonly_reason"]
    with pytest.raises(fileedit.SaveRefused) as e:
        fileedit.save(str(pointer), "gitdir: /elsewhere\n", sha(data))
    assert e.value.fields["reason"] == "not_editable"
    assert pointer.read_bytes() == data


@pytest.mark.parametrize("rel", ["proj/sub/.git", "proj/.git/notes.txt"])
def test_a_submodule_style_git_file_and_anything_under_a_git_component_are_read_only(root, rel):
    p = root / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(b"gitdir: ../.git/modules/sub\n")
    out = fileedit.read_file(str(p))
    assert out["editable"] is False and "git" in out["readonly_reason"]
    with pytest.raises(fileedit.SaveRefused):
        fileedit.save(str(p), "gitdir: /elsewhere\n", sha(b"gitdir: ../.git/modules/sub\n"))
    assert p.read_bytes() == b"gitdir: ../.git/modules/sub\n"


# 4 — whatever was displaced goes back as what it was


def test_a_directory_swapped_in_during_the_save_is_put_back_where_it_was(root):
    p = root / "a.txt"
    p.write_bytes(b"orig\n")

    def hook(name):
        if name == "stage":
            os.unlink(p)
            os.mkdir(p)
            (p / "inner.txt").write_bytes(b"agent dir\n")

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused):
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    fileedit._HOOK = None
    assert p.is_dir() and (p / "inner.txt").read_bytes() == b"agent dir\n"
    assert leftovers(root) == []
    other = root / "b.txt"
    other.write_bytes(b"b\n")
    fileedit.save(str(other), "B\n", sha(b"b\n"))  # an unrelated save is not blocked
    assert other.read_bytes() == b"B\n"


def test_a_symlink_swapped_in_during_the_save_is_put_back_as_the_symlink(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not yours\n")
    p = root / "a.txt"
    p.write_bytes(b"orig\n")

    def hook(name):
        if name == "stage":
            os.unlink(p)
            os.symlink(outside, p)

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused):
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    fileedit._HOOK = None
    assert os.path.islink(p) and os.readlink(p) == str(outside)
    assert os.stat(outside).st_nlink == 1 and outside.read_bytes() == b"not yours\n"
    assert leftovers(root) == []


def test_a_record_that_cannot_be_resolved_does_not_block_other_saves(root, monkeypatch):
    def unavailable(*a, **k):
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")

    p = root / "a.txt"
    p.write_bytes(b"orig\n")

    def hook(name):
        if name == "stage":
            os.unlink(p)
            os.mkdir(p)

    # Scoped, never `monkeypatch.undo()`: undo also reverts every conftest store pin, so the
    # rest of the test would run against the operator's real stores.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fileedit, "_rename_noreplace", unavailable, raising=False)
        fileedit._HOOK = hook
        with pytest.raises(FsError):
            fileedit.save(str(p), "mine\n", sha(b"orig\n"))
        fileedit._HOOK = None
        other = root / "b.txt"
        other.write_bytes(b"b\n")
        fileedit.save(str(other), "B\n", sha(b"b\n"))
        assert other.read_bytes() == b"B\n"
    _resolve()  # with the no-clobber rename back, the directory returns to its name
    assert p.is_dir()


# 5 — nothing in the store is removed automatically


def test_an_orphaned_original_is_kept(root):
    d = root / "proj"
    d.mkdir()
    p = d / "a.txt"
    p.write_bytes(b"orig\n")
    assert _child(root, "displace", p, expect=sha(b"orig\n")).returncode == -signal.SIGKILL
    os.rename(d, root / "moved")
    _resolve()
    (rec,) = records(root)
    assert rec["state"] == "orphaned"
    retained = os.path.join(store(root), rec["id"], rec["retained"])
    for _ in range(3):
        _resolve()
    with open(retained, "rb") as f:
        assert f.read() == b"orig\n"


# --------------------------------------------------------------------------- review 4807 (#955)

# 1 — a put-back never removes a name on the strength of an earlier link


def _rename_over(path, data: bytes) -> None:
    """What an agent's atomic write does: a new inode renamed over the name."""
    tmp = path.parent / ".agent-write.tmp"
    tmp.write_bytes(data)
    os.rename(tmp, path)


def _store_holds(data: bytes) -> bool:
    for dirpath, _dirs, names in os.walk(fileedit.recovery_dir()):
        for n in names:
            full = os.path.join(dirpath, n)
            if os.path.isfile(full) and not os.path.islink(full):
                with open(full, "rb") as f:
                    if f.read() == data:
                        return True
    return False


def test_a_put_back_during_recovery_keeps_the_original_when_its_name_is_replaced(root):
    """Hermes on #955 (review 4807, 1): recovery linked the original back, another process renamed
    over the restored name, and dropping the store's name then removed the original's LAST name —
    target replaced, store empty, record `reverted`."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    assert _child(root, "displace", p, expect=sha(b"orig\n")).returncode == -signal.SIGKILL

    writer: list[int] = []

    def hook(name):
        if name == "put_back:linked":
            # Review 4815: a process opens the restored file for writing, then another replaces its
            # name. What the first writes AFTER recovery must still have a name.
            writer.append(os.open(p, os.O_WRONLY | os.O_APPEND))
            _rename_over(p, b"agent\n")

    fileedit._HOOK = hook
    _resolve()
    fileedit._HOOK = None
    try:
        os.write(writer[0], b"late\n")
        assert os.fstat(writer[0]).st_nlink == 1  # the store's name; 0 is a nameless inode
    finally:
        os.close(writer[0])
    assert p.read_bytes() == b"agent\n"  # the other writer's version is left where it put it
    assert _store_holds(b"orig\nlate\n")  # the original, with what was written into it later


def test_a_refused_save_keeps_the_original_when_its_restored_name_is_replaced(root, monkeypatch):
    """The same race on the save's own put-back (a failed check): the store keeps its name for the
    original, so replacing the restored name leaves the original in the store."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    monkeypatch.setattr(fileedit, "_lease_intact", lambda fd: False)

    def hook(name):
        if name == "put_back:linked":
            _rename_over(p, b"agent\n")

    fileedit._HOOK = hook
    with pytest.raises(fileedit.SaveRefused):
        fileedit.save(str(p), "mine\n", sha(b"orig\n"))
    fileedit._HOOK = None
    assert p.read_bytes() == b"agent\n"
    assert _store_holds(b"orig\n")
    assert leftovers(root) == []


def test_recovery_finding_the_original_under_both_names_keeps_it_when_the_name_is_replaced(
    root, monkeypatch
):
    """The interrupted-put-back shortcut: the target and the store both name the original (a
    put-back that linked and stopped). Recovery checked they were the same file and then unlinked
    the store's name — a replacement between that check and the unlink lost the original. The
    replacement is driven into exactly that gap, right after the check returns."""
    p = root / "a.txt"
    p.write_bytes(b"orig\n")
    assert _child(root, "displace", p, expect=sha(b"orig\n")).returncode == -signal.SIGKILL
    (rec,) = records(root)
    os.link(os.path.join(store(root), rec["id"], rec["retained"]), p)
    real = fileedit._same_file
    replaced: list[bool] = []

    writer: list[int] = []

    def same_then_replaced(a_fd, a, b_fd, b):
        out = real(a_fd, a, b_fd, b)
        if a == rec["retained"] and not replaced:
            writer.append(os.open(p, os.O_WRONLY | os.O_APPEND))  # opened before the replacement
            _rename_over(p, b"agent\n")
            replaced.append(True)
        return out

    monkeypatch.setattr(fileedit, "_same_file", same_then_replaced)
    _resolve()
    assert replaced, "recovery no longer compares the two names — drive the race elsewhere"
    try:
        os.write(writer[0], b"late\n")
        assert os.fstat(writer[0]).st_nlink == 1  # the store's name; 0 is a nameless inode
    finally:
        os.close(writer[0])
    assert p.read_bytes() == b"agent\n"
    assert _store_holds(b"orig\nlate\n")


# 5 — a malformed record is set aside and never fails another save


@pytest.mark.parametrize(
    "how",
    ["state-list-and-created-dict", "state-dict", "record-is-a-directory", "record-is-a-fifo",
     "nested-too-deep"],
)  # fmt: skip
def test_a_malformed_record_is_set_aside_and_never_fails_an_unrelated_save(root, how):
    """Hermes on #955 (review 4807, 5): ``state: []`` raised ``TypeError`` in the frozenset
    membership test, which ran before the per-record exception boundary, so every later save failed.
    Reading and validating are inside that boundary now, and each case here escaped it before: an
    unhashable state, a record ``read`` refuses, one whose ``open`` would block for ever, and JSON
    nested past the parser's recursion limit."""
    if how == "state-list-and-created-dict":
        entry = _forge(root, state=[], created={"at": 1})
    elif how == "state-dict":
        entry = _forge(root, state={"intent": True})
    else:
        entry = _forge(root)
        record_path = os.path.join(entry, "record.json")
        os.unlink(record_path)
        if how == "record-is-a-directory":
            os.mkdir(record_path)
        elif how == "record-is-a-fifo":
            os.mkfifo(record_path)
        else:
            with open(record_path, "w") as f:
                f.write("[" * 100_000)
    other = root / "other.txt"
    other.write_bytes(b"o\n")
    fileedit.save(str(other), "O\n", sha(b"o\n"))
    assert other.read_bytes() == b"O\n"
    fileedit.startup_resolve()
    assert not os.path.exists(entry)
    assert os.path.isdir(os.path.join(store(root), f".invalid-{_FORGED_ID}"))
