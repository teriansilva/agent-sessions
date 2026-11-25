"""A loopback sign-in's callback is delivered only to the sign-in's own listener (#2106)."""

from __future__ import annotations

import asyncio
import http.server
import os
import socket
import subprocess
import sys
import threading

import pytest

from agent_sessions.plugins import signin, signin_callback
from agent_sessions.plugins.signin_callback import CallbackError, deliver, listening, parse


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def do_GET(self):  # noqa: N802 — http.server's spelling
        _Handler.seen.append((self.path, self.headers.get("Host", "")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"signed in; you may close this tab")

    def log_message(self, *_a):
        pass


@pytest.fixture
def server():
    """A callback listener in THIS process, like the vendor CLI's inside its unit."""
    _Handler.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _own(_unit):
    return {os.getpid()}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:35067/callback?code=abc&state=xyz",
        "  http://localhost:35067/callback?code=abc  ",
        "http://[::1]:35067/callback",
    ],
)
def test_a_loopback_callback_parses(url):
    port, target, _host = parse(url)
    assert port == 35067
    assert target.startswith("/callback")


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://127.0.0.1:35067/callback",  # the CLIs listen on plain http
        "http://example.com:35067/callback",
        "http://10.0.0.5:35067/callback",
        "http://127.0.0.2.nip.io:35067/callback",
        "http://user:pw@127.0.0.1:35067/callback",
        "http://127.0.0.1/callback",  # no port: nothing a sign-in minted
        "http://127.0.0.1:99999/callback",
        "http://127.0.0.1:35067/call back",
        "http://127.0.0.1:35067/callback\r\nX-Evil: 1",
        "javascript:alert(1)",
        "http://127.0.0.1:35067/" + "a" * 5000,
    ],
)
def test_anything_else_is_refused(url):
    with pytest.raises(CallbackError):
        parse(url)


def test_the_callback_reaches_the_sign_ins_own_listener(server):
    status = deliver("unit", f"http://127.0.0.1:{server}/callback?code=c0de&state=s1", pids=_own)
    assert status == 200
    assert _Handler.seen == [("/callback?code=c0de&state=s1", f"127.0.0.1:{server}")]


def test_localhost_is_delivered_to_the_listener_too(server):
    assert deliver("unit", f"http://localhost:{server}/cb", pids=_own) == 200
    assert _Handler.seen[0][0] == "/cb"


def test_a_port_the_sign_in_does_not_listen_on_is_refused(server):
    """A listener in ANOTHER process (BattleLab, a database...) is never reached."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,sys,time\n"
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()\n"
            "print(s.getsockname()[1], flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        foreign = int(child.stdout.readline())
        assert foreign in listening({child.pid})  # it IS listening, just not in this "unit"
        with pytest.raises(CallbackError, match=f"port {foreign}"):
            deliver("unit", f"http://127.0.0.1:{foreign}/callback", pids=_own)
    finally:
        child.kill()
        child.wait()
    assert _Handler.seen == []


def test_an_empty_unit_reaches_nothing(server):
    with pytest.raises(CallbackError):
        deliver("unit", f"http://127.0.0.1:{server}/callback", pids=lambda _u: set())
    assert _Handler.seen == []


def test_a_closed_listener_is_a_clear_refusal():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(CallbackError):
        deliver("unit", f"http://127.0.0.1:{port}/callback", pids=_own)


def test_no_running_sign_in_refuses():
    with pytest.raises(CallbackError, match="not running"):
        asyncio.run(signin.callback("00000000-0000-0000-0000-000000000000", "http://127.0.0.1:1/"))


def test_an_ended_sign_in_refuses(monkeypatch, server):
    live = signin._Live()
    live.unit = "battlelab-plugin-x.service"
    live.ended.set()
    monkeypatch.setitem(signin._LIVE, "op", live)
    with pytest.raises(CallbackError, match="not running"):
        asyncio.run(signin.callback("op", f"http://127.0.0.1:{server}/callback"))


def test_a_running_sign_in_delivers_through_its_unit(monkeypatch, server):
    live = signin._Live()
    live.unit = "battlelab-plugin-x.service"
    monkeypatch.setitem(signin._LIVE, "op", live)
    asked = []

    def pids(unit):
        asked.append(unit)
        return {os.getpid()}

    real = signin_callback.deliver
    monkeypatch.setattr(signin_callback, "deliver", lambda unit, url: real(unit, url, pids=pids))
    assert asyncio.run(signin.callback("op", f"http://127.0.0.1:{server}/cb?code=1")) == 200
    assert asked == ["battlelab-plugin-x.service"]


def test_a_redirect_is_answered_but_never_followed(server):
    """The CLI's answer is the verdict: a 302 is reported as 302, its target is never fetched."""

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{server}/followed")
            self.end_headers()

        def log_message(self, *_a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert deliver("unit", f"http://127.0.0.1:{srv.server_address[1]}/cb", pids=_own) == 302
    finally:
        srv.shutdown()
        srv.server_close()
    assert _Handler.seen == []


def test_an_ipv6_only_listener_is_reached_on_its_own_family():
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this host")

    class V6(http.server.HTTPServer):
        address_family = socket.AF_INET6

    _Handler.seen = []
    try:
        srv = V6(("::1", 0), _Handler)
    except OSError:
        pytest.skip("::1 is not configured on this host")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert listening({os.getpid()})[port] == "::1"
        assert deliver("unit", f"http://localhost:{port}/cb?code=6", pids=_own) == 200
        assert deliver("unit", f"http://[::1]:{port}/cb?code=6", pids=_own) == 200
    finally:
        srv.shutdown()
        srv.server_close()
    assert [p for p, _ in _Handler.seen] == ["/cb?code=6", "/cb?code=6"]


def test_an_account_signin_delivers_through_its_own_unit(monkeypatch, server):
    from agent_sessions import agent_account_signin

    live = signin._Live()
    live.unit = "battlelab-plugin-acct.service"
    monkeypatch.setitem(agent_account_signin._LIVE, ("claude", "work"), live)
    real = signin_callback.deliver
    units = []

    def via(unit, url):
        units.append(unit)
        return real(unit, url, pids=_own)

    monkeypatch.setattr(signin_callback, "deliver", via)
    url = f"http://127.0.0.1:{server}/cb?code=acct"
    assert asyncio.run(agent_account_signin.callback("claude", "work", url)) == 200
    assert units == ["battlelab-plugin-acct.service"]
    with pytest.raises(CallbackError, match="not running"):
        asyncio.run(agent_account_signin.callback("claude", "other", url))


def test_a_sign_in_being_cancelled_refuses(monkeypatch, server):
    """An explicit cancel in flight is a lifecycle boundary, like the end itself."""
    live = signin._Live()
    live.unit = "battlelab-plugin-x.service"
    live.closing.set()
    monkeypatch.setitem(signin._LIVE, "op", live)
    with pytest.raises(CallbackError, match="not running"):
        asyncio.run(signin.callback("op", f"http://127.0.0.1:{server}/callback"))
    assert _Handler.seen == []


_FOREIGN_HTTP = (
    "import http.server,socket,sys\n"
    "addr, port, reuse = sys.argv[1], int(sys.argv[2]), sys.argv[3] == '1'\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        print('FOREIGN GOT '+self.path, flush=True)\n"
    "        self.send_response(204); self.end_headers()\n"
    "    def log_message(self,*a): pass\n"
    "class S(http.server.HTTPServer):\n"
    "    def server_bind(self):\n"
    "        if reuse: self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)\n"
    "        super().server_bind()\n"
    "s=S((addr, port), H); print('PORT', s.server_address[1], flush=True)\n"
    "s.serve_forever()\n"
)


def _foreign(addr: str, port: int = 0, reuse: bool = False):
    child = subprocess.Popen(
        [sys.executable, "-c", _FOREIGN_HTTP, addr, str(port), "1" if reuse else "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = child.stdout.readline().split()
    assert line[:1] == ["PORT"], line
    return child, int(line[1])


def test_an_owned_listener_on_another_address_never_authorises_a_foreign_one():
    """Hermes on #2107: the sign-in holds 127.0.0.2:P while another program serves 127.0.0.1:P.
    The address is part of the endpoint: delivery goes to 127.0.0.2, never to the foreign one."""
    child, port = _foreign("127.0.0.1")
    _Handler.seen = []
    try:
        srv = http.server.HTTPServer(("127.0.0.2", port), _Handler)
    except OSError:
        child.kill()
        child.wait()
        pytest.skip("127.0.0.2 is not bindable on this host")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert listening({os.getpid()})[port] == "127.0.0.2"
        status = deliver("unit", f"http://127.0.0.1:{port}/callback?code=SYNTHETIC", pids=_own)
        assert status == 200
        assert _Handler.seen[0][0] == "/callback?code=SYNTHETIC"
    finally:
        srv.shutdown()
        srv.server_close()
        child.kill()
        out = child.communicate()[0]
    assert "FOREIGN GOT" not in out


def test_a_port_another_program_shares_is_refused():
    """SO_REUSEPORT lets a same-user process listen on the sign-in's own address:port; the kernel
    would hand the connection to either. Contested means refused."""
    child, port = _foreign("127.0.0.1", reuse=True)
    own = socket.socket()
    own.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        own.bind(("127.0.0.1", port))
        own.listen()
        assert listening({os.getpid()})[port] is None
        with pytest.raises(CallbackError, match="another program"):
            deliver("unit", f"http://127.0.0.1:{port}/callback?code=SYNTHETIC", pids=_own)
    finally:
        own.close()
        child.kill()
        out = child.communicate()[0]
    assert "FOREIGN GOT" not in out


def test_a_foreign_wildcard_listener_contests_the_loopback_port():
    child, port = _foreign("0.0.0.0", reuse=True)  # noqa: S104 — a test fixture's own bind
    own = socket.socket()
    own.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        own.bind(("127.0.0.1", port))
        own.listen()
        assert listening({os.getpid()})[port] is None
    finally:
        own.close()
        child.kill()
        child.wait()
