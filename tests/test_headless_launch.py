"""`dtach -n` against a REAL dtach — the test #732 did not have (#739).

#732 was closed rather than merged because its headless launch could not have worked: `-c` needs
a terminal, the headless path handed it `DEVNULL`, and every launch died with *"Attaching to a
session requires a terminal"* before falling into an aliveness timeout that looked like a slow
start. Its unit tests mocked `create_subprocess_exec` **and** `session_exists`, so they verified
the bookkeeping and nothing about the spawn — which is why the issue's first risk row says a
fully-mocked spawn is untested.

So these launch an actual `dtach` binary against an actual command, and assert on the process
tree rather than on a mock's call list. They skip (rather than fail) where no dtach is installed,
because that is an environment fact, not a regression — CI has one.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import time

import pytest

from agent_sessions import ptybridge, reaper

pytestmark = pytest.mark.skipif(
    shutil.which(ptybridge.DTACH_BIN) is None and not os.path.exists(ptybridge.DTACH_BIN),
    reason="no dtach binary on this host",
)

ENGINE = "claude"
SID = "aaaaaaaa-1111-2222-3333-444444444444"


@pytest.fixture
def sockdir(monkeypatch):
    """A SHORT socket directory — deliberately not pytest's `tmp_path`.

    `sockaddr_un.sun_path` is 108 bytes. pytest's tmp_path is
    `/tmp/pytest-of-<user>/pytest-<n>/<test-name-truncated-to-30>0/`, and this suite's session ids
    are full UUIDs — together they overflow it. The symptom is worth recording because it is not
    the one you would guess: the socket FILE appears, so an existence check passes, and only the
    connect fails — as a `probe_master` of `unknown` rather than `dead`, which reads exactly like a
    slow host. That cost a debugging round here.

    `runtime_dir()` also memoises the directory it last created (`_DIR_READY_FOR`), so the env var
    alone is not enough.
    """
    d = tempfile.mkdtemp(prefix="as-pty-")
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", d)
    monkeypatch.setattr(ptybridge, "_DIR_READY_FOR", None, raising=False)
    yield pathlib.Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _reap(pid: int | None) -> None:
    if not pid:
        return
    with __import__("contextlib").suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


def test_the_detached_builder_differs_from_the_attached_one_by_the_MODE_FLAG_ONLY():
    """Everything that carries the shell-free guarantee is shared; only `-c`/`-n` differs.

    A second builder would be a second place to get the literal-argv shape wrong, so the contract
    is that these two lists are identical apart from one element.
    """
    kw = {"engine": ENGINE, "session_id": SID, "launch_argv": ["/bin/cat"]}
    attached = ptybridge.launch_argv(**kw)
    detached = ptybridge.launch_argv(**kw, detached=True)
    assert attached[1] == "-c" and detached[1] == "-n"
    assert attached[:1] + attached[2:] == detached[:1] + detached[2:]
    # Still shell-free: a binary at argv[0], never a command string.
    assert detached[0] == ptybridge.DTACH_BIN
    assert detached[-1] == "/bin/cat"


def test_the_detached_builder_keeps_the_absolute_path_rule():
    for bad in (["cat"], ["./cat"], []):
        with pytest.raises(ptybridge.PtyBridgeError):
            ptybridge.launch_argv(engine=ENGINE, session_id=SID, launch_argv=bad, detached=True)


def test_a_REAL_dtach_n_comes_up_with_no_terminal_at_all(sockdir):
    """The core claim, proved against the binary.

    stdin/stdout/stderr are all `DEVNULL` — precisely the configuration that made `-c` fail. If
    `-n` needed a terminal this would exit non-zero and no socket would appear.
    """
    argv = ptybridge.launch_argv(
        engine=ENGINE, session_id=SID, launch_argv=["/bin/sleep", "30"], detached=True
    )
    proc = subprocess.Popen(  # noqa: S603 — literal argv, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        sock = ptybridge.socket_path(ENGINE, SID)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        assert sock.exists(), (
            "no socket: dtach -n did not create the master "
            f"(stderr={proc.stderr.read()[:200] if proc.stderr else b''!r})"
        )
        # …and it is genuinely SERVING, not just a leftover file. Polled, because dtach creates
        # the socket and then listens — the gap is small but real, and asserting on the first
        # probe made this flake on a loaded host for a reason that had nothing to do with `-n`.
        state = ptybridge.UNKNOWN
        for _ in range(60):
            state = ptybridge.probe_master(sock)
            if state == ptybridge.ALIVE:
                break
            time.sleep(0.05)
        assert state == ptybridge.ALIVE, f"the -n master never accepted a connection ({state})"
    finally:
        _reap(proc.pid)


def test_the_REAPER_can_see_an_n_master(sockdir):
    """The failure this would otherwise have: a headless master invisible to archive-time cleanup
    and to the split-brain guard, so every dispatched session leaks an agent with nothing on
    screen to say so."""
    argv = ptybridge.launch_argv(
        engine=ENGINE, session_id=SID, launch_argv=["/bin/sleep", "30"], detached=True
    )
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        sock = ptybridge.socket_path(ENGINE, SID)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        assert sock.exists()
        found = None
        for _ in range(40):
            found = reaper._find_master_pid(ENGINE, SID)
            if found:
                break
            time.sleep(0.05)
        assert found is not None, "the reaper could not find the -n master"
    finally:
        _reap(proc.pid)


def test_an_ATTACH_client_is_still_not_mistaken_for_a_master(sockdir):
    """Widening the matcher must not widen it too far: `dtach -a` is the registry's READER, and
    signalling it would tear down a viewer rather than the agent."""
    argv = ptybridge.launch_argv(
        engine=ENGINE, session_id=SID, launch_argv=["/bin/sleep", "30"], detached=True
    )
    master = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    attach = None
    try:
        sock = ptybridge.socket_path(ENGINE, SID)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        attach = subprocess.Popen(  # noqa: S603
            ptybridge.attach_argv(engine=ENGINE, session_id=SID),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.4)
        found = reaper._find_master_pid(ENGINE, SID)
        assert found is not None
        assert found != attach.pid, "the reaper picked the ATTACH client as the master"
    finally:
        _reap(master.pid)
        _reap(attach.pid if attach else None)


# ---- the production boundary (#898 review, finding 1) ----------------------------------------


@pytest.mark.anyio
async def test_THE_BRIEF_ACTUALLY_ARRIVES_with_no_browser_anywhere(sockdir, tmp_path, monkeypatch):
    """End to end, with nothing mocked between the launch and the bytes.

    This is the test the first version of this PR did not have, and its absence hid a real hole:
    the happy-path unit test stubbed `headless_seed.deliver`, so it could not notice that nothing
    was DRAINING the `dtach -n` master. With no reader there is no scrollback, no first-paint
    observation and no registered writer — so in production the delivery waited out its readiness
    timeout every time, and every test still passed.

    So: a real `dtach -n`, a real `SessionStream` reader, the real readiness gate, and the real
    injector. The proof is the file the TARGET writes from its own stdin — not anything this
    process observed about what it wrote.

    **The target paints after a beat, and that is faithful rather than convenient.** `dtach`
    replays only the CURRENT screen to a newly attached client, and `SessionStream` deliberately
    suppresses that replay burst — so a program that has already finished painting before the
    reader attaches is invisible to it. A real agent takes seconds to start, which is why the
    production ordering (launch, attach the reader, then wait) works at all; a target that painted
    instantly would be testing a race no real engine runs.
    """
    from agent_sessions import handoff, headless_seed, ptybridge, session_input, session_stream

    out_file = tmp_path / "got.txt"
    prog = (
        "import sys,time;"
        "time.sleep(2.0);"
        # DECSET 2004 (bracketed paste) + enough output to clear the first-paint floor.
        "sys.stdout.write('\\x1b[?2004h' + 'x' * 4096 + '\\n');"
        "sys.stdout.flush();"
        "d = sys.stdin.readline();"
        f"open({str(out_file)!r}, 'w').write(d)"
    )
    engine, sid = "claude", "bbbbbbbb-1111-2222-3333-555555555555"
    key = f"{engine}:{sid}"

    argv = ptybridge.launch_argv(
        engine=engine,
        session_id=sid,
        launch_argv=["/usr/bin/python3", "-c", prog],
        detached=True,
    )
    proc = subprocess.Popen(  # noqa: S603 — literal argv, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    registry = session_stream.SessionRegistry()
    try:
        sock = ptybridge.socket_path(engine, sid)
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        assert sock.exists(), "the -n master never came up"

        # THE READER, before anything waits on output. Without this nothing drains the master and
        # the gate below can never open — exactly the production bug this test exists for.
        await registry.ensure_headless(engine, sid)

        # The brief, through the one seed store, redeemed by the one injector.
        handle = handoff.create_handle(
            source_key="",
            target_engine=engine,
            mode="dispatch",
            seed="HELLO-FROM-THE-BRIEF",
            cwd=str(tmp_path),
        )
        handoff.bind_target(handle, key)

        delivered, why = await headless_seed.deliver(key, key, timeout=40.0)
        assert delivered, f"the brief was not delivered: {why}"

        # …and the AGENT received it, on its own stdin.
        for _ in range(100):
            if out_file.exists() and out_file.read_text().strip():
                break
            await asyncio.sleep(0.1)
        got = out_file.read_text() if out_file.exists() else ""
        assert "HELLO-FROM-THE-BRIEF" in got, f"target received {got!r}"
        # Delivered AS A BRACKETED PASTE, which is what stops a multi-line brief being read as a
        # sequence of separate commands by the agent's line editor.
        assert "200~" in got and "201~" in got
    finally:
        session_input.reset()
        _reap(proc.pid)
