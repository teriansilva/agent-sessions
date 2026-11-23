"""Shell-free + identity guarantees for the dtach session layer (issue #49)."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from agent_sessions import ptybridge, sessionlock, sessions


@pytest.fixture(autouse=True)
def _runtime():
    # conftest supplies a short private per-test runtime: long pytest names exceed AF_UNIX.
    return Path(os.environ["AGENT_SESSIONS_RUNTIME_DIR"])


def test_socket_path_is_stable_and_inside_runtime_dir(_runtime):
    p = ptybridge.socket_path("claude", "abcd-1234")
    assert p.parent == ptybridge.runtime_dir()
    assert p.name == "claude-abcd-1234.sock"
    # stable across calls
    assert ptybridge.socket_path("claude", "abcd-1234") == p


def test_socket_path_sanitises_unsafe_chars():
    # path traversal / argv-injection attempts collapse to underscores
    p = ptybridge.socket_path("claude", "../../etc/passwd")
    assert p.name == "claude-.._.._etc_passwd.sock"
    assert "/" not in p.name


@pytest.mark.parametrize("engine,sid", [("", "x"), ("claude", ""), ("", "")])
def test_socket_path_rejects_empty(engine, sid):
    with pytest.raises(ptybridge.PtyBridgeError):
        ptybridge.socket_path(engine, sid)


# ---- Mode-explicit dtach builders (#165). attach_argv is `-a` only (never falls back
# to create); launch_argv is `-c` only (fails if the sock exists, caller must hold lock).


def test_attach_argv_is_attach_only_no_command():
    argv = ptybridge.attach_argv(engine="claude", session_id="abcd1234")
    assert argv[0] == ptybridge.DTACH_BIN
    assert argv[1] == "-a"
    assert "-A" not in argv and "-c" not in argv  # NEVER attach-or-create or create
    # dtach -a's positional sock must come *immediately* after -a per man dtach(1):
    # "dtach -a <socket> <options>". Putting flags first makes dtach parse e.g. `-z`
    # as the socket path and the attach fails silently (#165 verified in the wild).
    assert argv[2].endswith("/claude-abcd1234.sock")
    assert argv[3:] == ["-z", "-E", "-r", "winch"]
    # No `<command>` ever — attach-only.
    assert len(argv) == 7


def test_launch_argv_is_create_only_with_command():
    argv = ptybridge.launch_argv(
        engine="claude",
        session_id="abcd1234",
        launch_argv=["/home/u/.local/bin/claude", "--resume", "abcd1234"],
    )
    assert argv[0] == ptybridge.DTACH_BIN
    assert argv[1] == "-c"
    assert "-A" not in argv and "-a" not in argv  # NEVER attach-or-create or attach
    assert argv[2].endswith("/claude-abcd1234.sock")
    assert argv[3:7] == ["-z", "-E", "-r", "winch"]
    assert argv[7:] == ["/home/u/.local/bin/claude", "--resume", "abcd1234"]


def test_launch_argv_rejects_empty_launch():
    with pytest.raises(ptybridge.PtyBridgeError):
        ptybridge.launch_argv(engine="claude", session_id="x", launch_argv=[])


def test_launch_argv_requires_absolute_binary():
    with pytest.raises(ptybridge.PtyBridgeError):
        ptybridge.launch_argv(engine="claude", session_id="x", launch_argv=["claude", "--resume"])


# ---- session_exists + list_sessions use a connect()-probe so orphan socks
# (file present but no listener) are NOT reported as alive (#165).


def _bind_listening_sock(path):
    """Create a UNIX socket file + actually listen — i.e. fake a live dtach master."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(1)
    return s


def _bind_orphan_sock(path):
    """Create the sock FILE without listening — i.e. fake a master that crashed
    without unlinking. The file is left behind; connect() returns ECONNREFUSED."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()  # the file persists on disk


def test_session_exists_false_when_no_sock(_runtime):
    assert ptybridge.session_exists("claude", "s1") is False


def test_session_exists_true_when_master_listening(_runtime):
    p = ptybridge.socket_path("claude", "alive")
    srv = _bind_listening_sock(p)
    try:
        assert ptybridge.session_exists("claude", "alive") is True
    finally:
        srv.close()


def test_session_exists_false_for_orphan_sock(_runtime):
    """An orphan sock file (no live master) must NOT register as alive — the
    connect-probe makes `open_action` correctly route to LAUNCH, which would then
    unlink the orphan and `dtach -c` cleanly. Without the probe (file-only check),
    this would return True and ATTACH would 4500 on the next exec."""
    p = ptybridge.socket_path("claude", "orphan")
    _bind_orphan_sock(p)
    assert p.is_socket()  # the file is there
    assert ptybridge.session_exists("claude", "orphan") is False  # but nothing's listening


def test_list_sessions_filters_orphans(_runtime):
    alive = ptybridge.socket_path("claude", "alive")
    orphan = ptybridge.socket_path("opencode", "orphan")
    bogus_file = ptybridge.runtime_dir() / "opencode-bogus.sock"
    srv = _bind_listening_sock(alive)
    try:
        _bind_orphan_sock(orphan)
        bogus_file.write_text("not a socket")
        sessions = ptybridge.list_sessions()
        assert ("claude", "alive") in sessions
        assert ("opencode", "orphan") not in sessions  # orphan filtered out
        assert ("opencode", "bogus") not in sessions  # regular file ignored
    finally:
        srv.close()


def test_the_secrets_broker_socket_is_not_a_session_and_is_not_probed(_runtime, monkeypatch):
    """It shares the runtime directory and answers a connect: listed, it was a phantom live
    session, and every scan's probe made the broker log an unhandled ConnectionResetError."""
    alive = ptybridge.socket_path("claude", "alive")
    broker = ptybridge.runtime_dir() / "secrets-6e30fe614b.sock"
    memory_broker = ptybridge.runtime_dir() / "memory-6e30fe614b00.sock"
    servers = [
        _bind_listening_sock(alive),
        _bind_listening_sock(broker),
        _bind_listening_sock(memory_broker),
    ]
    probed = []
    real_probe = ptybridge.probe_master

    def probe(path):
        probed.append(path.name)
        return real_probe(path)

    monkeypatch.setattr(ptybridge, "probe_master", probe)
    try:
        assert ptybridge.list_sessions_checked() == ([("claude", "alive")], True)
        assert probed == [alive.name]
    finally:
        for srv in servers:
            srv.close()


def test_only_the_brokers_exact_name_is_excluded_from_the_scan():
    assert ptybridge.is_broker_socket("secrets-6e30fe614b.sock")
    # A session of an engine called `secrets` is still a session.
    for name in ("secrets-abc.sock", "secrets-6e30fe614b0.sock", "claude-6e30fe614b.sock"):
        assert not ptybridge.is_broker_socket(name)


# ---- unlink_if_stale removes orphan socks under the caller's held lock (#165).


def test_unlink_if_stale_removes_orphan(_runtime):
    p = ptybridge.socket_path("claude", "ghost")
    _bind_orphan_sock(p)
    assert p.exists()
    assert ptybridge.unlink_if_stale("claude", "ghost") is True
    assert not p.exists()


def test_unlink_if_stale_leaves_alive_sock_alone(_runtime):
    p = ptybridge.socket_path("claude", "alive")
    srv = _bind_listening_sock(p)
    try:
        assert ptybridge.unlink_if_stale("claude", "alive") is False
        assert p.exists()
    finally:
        srv.close()


def test_unlink_if_stale_noop_when_absent(_runtime):
    assert ptybridge.unlink_if_stale("claude", "never-existed") is False


# ---- #355: tri-state probe — UNKNOWN (timeout) must never unlink a live master ----


def test_probe_dead_is_decisive_and_fast(tmp_path, monkeypatch):
    # No listener behind the file → ECONNREFUSED → DEAD on the first attempt; a
    # missing path is DEAD too. Both must not burn the timeout ladder.
    import socket as socket_mod
    import time as time_mod

    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path))
    sock = ptybridge.socket_path("test", "refused")
    srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)
    srv.close()  # file remains, nobody listening → refused
    t0 = time_mod.monotonic()
    assert ptybridge.probe_master(sock) == ptybridge.DEAD
    assert time_mod.monotonic() - t0 < 0.5  # decisive, no ladder
    assert ptybridge.probe_master(tmp_path / "absent.sock") == ptybridge.DEAD
    # decisive dead → unlink_if_stale removes the orphan (the pre-#355 behavior kept)
    assert ptybridge.unlink_if_stale("test", "refused") is True
    assert not sock.exists()


def test_probe_alive_with_pending_backlog(tmp_path, monkeypatch):
    # A listening socket is ALIVE even if the app never accept()s (kernel backlog).
    import socket as socket_mod

    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path))
    sock = ptybridge.socket_path("test", "alive")
    srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(8)
    try:
        assert ptybridge.probe_master(sock) == ptybridge.ALIVE
        assert ptybridge.unlink_if_stale("test", "alive") is False
        assert sock.exists()
    finally:
        srv.close()


def test_probe_unknown_on_timeout_never_unlinks(tmp_path, monkeypatch):
    # THE #355 regression: a starved master (connect backlog full → probe times out)
    # is UNKNOWN, not dead — unlink_if_stale must refuse to remove the sock, or the
    # next `dtach -c` would bind a second master (split-brain). The old single
    # 0.2 s probe read this exact state as dead.
    import socket as socket_mod

    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(ptybridge, "_PROBE_TIMEOUTS_S", (0.05, 0.05))
    sock = ptybridge.socket_path("test", "starved")
    srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(0)  # minimal backlog; never accept()
    fillers = []
    try:
        # Fill the backlog so further connects block until timeout.
        for _ in range(4):
            c = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            c.setblocking(False)
            try:
                c.connect(str(sock))
            except (BlockingIOError, OSError):
                pass
            fillers.append(c)
        verdict = ptybridge.probe_master(sock)
        if verdict != ptybridge.UNKNOWN:
            import pytest

            pytest.skip(f"kernel accepted past listen(0) backlog (verdict={verdict})")
        # UNKNOWN → the destructive step must refuse; the sock survives.
        assert ptybridge.unlink_if_stale("test", "starved") is False
        assert sock.exists()
        # ...and the boolean view stays conservative for ATTACH routing.
        assert ptybridge.session_exists("test", "starved") is False
    finally:
        for c in fillers:
            c.close()
        srv.close()


# ---- AF_UNIX `sun_path` guard (#2088) -----------------------------------------
#
# A runtime dir that pushes the socket past `sun_path` is the silent-BUSY bug: dtach
# still creates its master, but BattleLab's connect-probe fails with ENAMETOOLONG, so
# every attach is answered 4409 forever. The guard refuses the launch/attach up front
# with its own terminal error. These tests build paths of an EXACT byte length under a
# short `mktemp` base (pytest's own tmp_path is too long for AF_UNIX, see conftest).


def _runtime_with_socket_bytes(
    target: int,
    name: str = "claude-abcd1234.sock",
    char: str = "d",
) -> tuple[str, str]:
    """A runtime dir whose ``<runtime>/<name>`` is exactly ``target`` bytes."""
    base = tempfile.mkdtemp(prefix="blrt-")  # short on purpose — see the AF_UNIX note
    # full path = base + "/" + pad + "/" + name ; solve for the pad's byte count.
    pad_bytes = target - len(os.fsencode(base)) - 2 - len(os.fsencode(name))
    unit = len(char.encode())
    # The base's length depends on the host's TMPDIR, so a multi-byte `char` may not divide the
    # pad evenly: top up the remainder with single-byte ASCII so the total is still exact.
    assert pad_bytes >= 0, (target, pad_bytes, unit)
    count, rest = divmod(pad_bytes, unit)
    return base + "/" + char * count + "d" * rest, base


@pytest.mark.parametrize(
    ("platform", "usable"),
    [("linux", 107), ("darwin", 103), ("freebsd14", 103), ("openbsd7", 103)],
)
def test_the_usable_sun_path_excludes_the_trailing_nul_on_every_platform(platform, usable):
    # #2088 review: the field is 108 bytes on Linux and 104 on macOS/BSD, both INCLUDING the
    # trailing NUL, and CPython refuses len >= field size. So the usable limit is one less.
    assert ptybridge._sun_path_max(platform) == usable


def test_the_guard_refuses_the_first_byte_past_the_platform_limit(monkeypatch):
    # Exercise the guard itself at the macOS boundary, without needing a macOS kernel: 103
    # bytes pass, 104 (the full field, no room for the NUL) is refused.
    monkeypatch.setattr(ptybridge, "SUN_PATH_MAX", ptybridge._sun_path_max("darwin"))
    for size, ok in ((103, True), (104, False)):
        raw, base = _runtime_with_socket_bytes(size)
        try:
            monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
            if ok:
                ptybridge.check_socket_path("claude", "abcd1234")
            else:
                with pytest.raises(ptybridge.SocketPathTooLong):
                    ptybridge.check_socket_path("claude", "abcd1234")
        finally:
            shutil.rmtree(base, ignore_errors=True)


def test_socket_path_at_the_sun_path_limit_is_accepted(monkeypatch):
    raw, base = _runtime_with_socket_bytes(ptybridge.SUN_PATH_MAX)
    try:
        monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
        p = ptybridge.socket_path("claude", "abcd1234")
        assert len(os.fsencode(str(p))) == ptybridge.SUN_PATH_MAX
        # The validator passes, and the kernel really does accept this exact path.
        ptybridge.check_socket_path("claude", "abcd1234")
        assert ptybridge.attach_argv(engine="claude", session_id="abcd1234")[2] == str(p)
        srv = _bind_listening_sock(p)
        try:
            assert ptybridge.session_exists("claude", "abcd1234") is True
        finally:
            srv.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_socket_path_one_byte_over_the_limit_is_refused(monkeypatch):
    raw, base = _runtime_with_socket_bytes(ptybridge.SUN_PATH_MAX + 1)
    try:
        monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
        p = ptybridge.socket_path("claude", "abcd1234")
        assert len(os.fsencode(str(p))) == ptybridge.SUN_PATH_MAX + 1
        with pytest.raises(ptybridge.SocketPathTooLong) as err:
            ptybridge.check_socket_path("claude", "abcd1234")
        # The message names the actual bytes and the limit, never the path itself.
        assert str(ptybridge.SUN_PATH_MAX + 1) in str(err.value)
        assert str(ptybridge.SUN_PATH_MAX) in str(err.value)
        assert str(p) not in str(err.value)
        # Every create/attach argv construction refuses before dtach can spawn.
        with pytest.raises(ptybridge.SocketPathTooLong):
            ptybridge.attach_argv(engine="claude", session_id="abcd1234")
        with pytest.raises(ptybridge.SocketPathTooLong):
            ptybridge.launch_argv(
                engine="claude",
                session_id="abcd1234",
                launch_argv=["/bin/true"],
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_126_byte_reproduction_is_refused(monkeypatch):
    # The exact length from #1986's P1 run: dtach "created" the master, every attach
    # probe returned UNKNOWN → BUSY forever with no error.
    sid = "new-00000000-0000-0000-0000-000000000000"
    name = f"opencode2-{sid}.sock"
    raw, base = _runtime_with_socket_bytes(126, name)
    try:
        monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
        with pytest.raises(ptybridge.SocketPathTooLong) as err:
            ptybridge.check_socket_path("opencode2", sid)
        assert "126" in str(err.value)
        assert str(ptybridge.SUN_PATH_MAX) in str(err.value)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_non_ascii_runtime_dir_is_measured_in_filesystem_bytes(monkeypatch):
    # 'é' is 2 bytes in UTF-8: the CHARACTER length is under the limit, the BYTES are
    # not. The kernel measures bytes, so the guard must too.
    raw, base = _runtime_with_socket_bytes(ptybridge.SUN_PATH_MAX + 1, char="é")
    try:
        monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
        full = str(Path(raw) / "claude-abcd1234.sock")
        assert len(full) < ptybridge.SUN_PATH_MAX  # chars fit…
        assert len(os.fsencode(full)) == ptybridge.SUN_PATH_MAX + 1  # …bytes do not
        with pytest.raises(ptybridge.SocketPathTooLong):
            ptybridge.check_socket_path("claude", "abcd1234")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_open_action_refuses_an_overlong_socket_before_any_lock(monkeypatch):
    # The BUSY-loop root cause: with a live master at an overlong path, `open_action`
    # found no reachable socket and took the lock (BUSY for every later attach). The
    # guard must fire BEFORE the probes and before any lock, so nothing is wedged.
    raw, base = _runtime_with_socket_bytes(ptybridge.SUN_PATH_MAX + 1)
    try:
        monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", raw)
        key = "claude:abcd1234"
        try:
            action, lock = sessions.open_action("claude", "abcd1234")
        except ptybridge.SocketPathTooLong:
            pass
        else:
            if lock is not None:
                lock.release()
            pytest.fail(f"open_action should have refused the overlong socket: {action}")
        assert sessionlock.is_locked(key) is False
        assert not (Path(raw) / "claude-abcd1234.sock").exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)
