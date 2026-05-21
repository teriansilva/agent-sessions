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


def test_dtach_argv_create_or_attach_shape():
    argv = ptybridge.dtach_argv(
        engine="claude",
        session_id="abcd1234",
        launch_argv=["/home/u/.local/bin/claude", "--resume", "abcd1234"],
    )
    assert argv[0] == ptybridge.DTACH_BIN
    assert argv[1] == "-A"
    assert argv[2].endswith("/claude-abcd1234.sock")
    # detach niceties present, in order, before the command
    assert argv[3:7] == ["-z", "-E", "-r", "winch"]
    # the engine command is appended verbatim, last
    assert argv[7:] == ["/home/u/.local/bin/claude", "--resume", "abcd1234"]


def test_dtach_argv_rejects_empty_launch():
    with pytest.raises(ptybridge.PtyBridgeError):
        ptybridge.dtach_argv(engine="claude", session_id="x", launch_argv=[])


def test_dtach_argv_requires_absolute_binary():
    # a bare name could be mis-read as a dtach flag and isn't on the login PATH
    with pytest.raises(ptybridge.PtyBridgeError):
        ptybridge.dtach_argv(engine="claude", session_id="x", launch_argv=["claude", "--resume"])


def test_session_exists_and_list(_runtime):
    assert ptybridge.session_exists("claude", "s1") is False
    assert ptybridge.list_sessions() == []
    # bind a real unix socket where the session's socket would live
    p = ptybridge.socket_path("claude", "s1")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(p))
        assert ptybridge.session_exists("claude", "s1") is True
        assert ("claude", "s1") in ptybridge.list_sessions()
        # a plain file is not a socket -> ignored
        (ptybridge.runtime_dir() / "opencode-bogus.sock").write_text("x")
        assert ("opencode", "bogus") not in ptybridge.list_sessions()
    finally:
        srv.close()
