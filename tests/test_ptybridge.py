"""Shell-free + identity guarantees for the dtach session layer (issue #49)."""

from __future__ import annotations

import socket

import pytest

from agent_sessions import ptybridge


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_path / "pty"))
    return tmp_path / "pty"


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
