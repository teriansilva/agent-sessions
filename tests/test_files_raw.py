"""Raw file bytes for the viewer's media preview and Download (#1530).

`/api/files/raw` hands file BYTES to the browser, so it is held to `/api/files/read`'s exact
containment (a parity test proves no path is reachable here that read refuses), it streams from
the verified descriptor, and its stream slots are released however a stream ends — measured
against a real uvicorn server with real sockets, because the TestClient buffers whole responses
and cannot model a client that stalls or hangs up mid-body.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import zlib

import pytest
from fastapi.testclient import TestClient

from agent_sessions import fileedit, files, fsbrowse

# Deliberately no `style-src`: nothing the viewer shows needs it, and an inline-style grant to
# agent-written content is a widening that would need its own approval (Hermes, PR #1533).
SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'"


@pytest.fixture(autouse=True)
def _reset_budgets():
    files._inflight_total = 0
    files._inflight_by_root.clear()
    files.reset_raw_streams_for_test()
    yield
    files._inflight_total = 0
    files._inflight_by_root.clear()
    files.reset_raw_streams_for_test()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "home"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(r))
    files.reset_capabilities_for_test()
    yield r
    files.reset_capabilities_for_test()


@pytest.fixture()
def app(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return main.create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _png() -> bytes:
    """A real 1x1 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xb0\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _wait_inflight(n: int, timeout: float = 3.0) -> int:
    """Stream slots are released by a short background thread; give it a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if files.raw_streams_inflight() == n:
            return n
        time.sleep(0.02)
    return files.raw_streams_inflight()


def _raw(client, path, **params):
    return client.get("/api/files/raw", params={"path": str(path), **params})


# --------------------------------------------------------------------------- the producer


# Real magic numbers: NUL-bearing binaries stay binary, while SVG (and a NUL-free WAV) are text the
# NUL sniff would never call binary — the reason classification is by extension (#1530).
SAMPLES = {
    "a.png": (_png(), "image", True),
    "a.jpg": (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9",
        "image",
        True,
    ),
    "a.gif": (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
        "image",
        True,
    ),
    "a.webp": (
        b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00",
        "image",
        True,
    ),
    "a.svg": (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
        b'<rect width="4" height="4"/></svg>\n',
        "image",
        False,
    ),
    "a.mp4": (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom", "video", True),
    "a.webm": (
        b"\x1aE\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x86\x81\x01B\x82\x84webm",
        "video",
        True,
    ),
    "a.ogg": (b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00", "audio", True),
    "a.mp3": (b"ID3\x03\x00\x00\x00\x00\x00\x0f\xff\xfb\x90\x00", "audio", True),
    "a.wav": (b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00", "audio", True),
    "nul-free.wav": (b"RIFF-made-entirely-of-printable-bytes", "audio", False),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_read_reports_preview_by_extension_whatever_the_nul_sniff_says(root, name):
    data, kind, binary = SAMPLES[name]
    (root / name).write_bytes(data)
    out = fileedit.read_file(str(root / name))
    assert out["preview"] == kind
    assert out["binary"] is binary
    assert out["mime"] == files.PREVIEW_TYPES[os.path.splitext(name)[1]][0]


def test_svg_stays_editable_text(root):
    """Preview must not cost SVG its editor: same text payload and edit verdict as a .txt twin."""
    data = SAMPLES["a.svg"][0]
    (root / "a.svg").write_bytes(data)
    (root / "twin.txt").write_bytes(data)
    svg, txt = fileedit.read_file(str(root / "a.svg")), fileedit.read_file(str(root / "twin.txt"))
    assert svg["content"] == txt["content"] == data.decode()
    assert svg["editable"] == txt["editable"]
    assert svg["version"] == txt["version"]
    assert txt["preview"] is None and "mime" not in txt


def test_unknown_types_have_no_preview(root):
    (root / "capture.mkv").write_bytes(b"\x1aE\xdf\xa3\x00\x00")
    (root / "page.html").write_text("<script>alert(1)</script>")
    assert fileedit.read_file(str(root / "capture.mkv"))["preview"] is None
    assert fileedit.read_file(str(root / "page.html"))["preview"] is None


def test_a_50_mb_file_is_classified_with_one_bounded_read(root):
    big = root / "big.mp4"
    with open(big, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42")
        f.truncate(50 * 1024 * 1024)
    t0 = time.monotonic()
    out = fileedit.read_file(str(big))
    assert time.monotonic() - t0 < 2.0
    assert out["preview"] == "video" and out["size"] == 50 * 1024 * 1024
    assert "content" not in out


# --------------------------------------------------------------------------- route basics


def test_inline_allowlisted_type_with_the_full_header_set(client, root):
    data = _png()
    (root / "shot.png").write_bytes(data)
    r = _raw(client, root / "shot.png")
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-length"] == str(len(data))
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-disposition"].startswith('inline; filename="shot.png"')
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
    assert r.headers["content-security-policy"] == SANDBOX, "the app CSP must not win here"
    assert r.headers["cache-control"] == "no-store"
    assert _wait_inflight(0) == 0


def test_svg_is_served_as_svg_under_the_sandbox(client, root):
    (root / "x.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    )
    r = _raw(client, root / "x.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-security-policy"].startswith("sandbox;")


def test_non_allowlisted_type_is_415_inline_and_octet_stream_as_download(client, root):
    (root / "page.html").write_text("<script>alert(1)</script>")
    refused = _raw(client, root / "page.html")
    assert refused.status_code == 415
    assert refused.headers["content-security-policy"] == SANDBOX
    assert refused.headers["cache-control"] == "no-store"
    dl = _raw(client, root / "page.html", download="1")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/octet-stream"
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert dl.content == b"<script>alert(1)</script>"
    assert _wait_inflight(0) == 0


def test_download_of_an_allowlisted_type_is_an_attachment_too(client, root):
    (root / "shot.png").write_bytes(_png())
    r = _raw(client, root / "shot.png", download="1")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment;")


def test_non_ascii_names_get_an_rfc5987_filename(client, root):
    (root / "Übersicht ä.png").write_bytes(_png())
    cd = _raw(client, root / "Übersicht ä.png").headers["content-disposition"]
    assert "filename*=UTF-8''%C3%9Cbersicht%20%C3%A4.png" in cd
    assert 'filename="_bersicht _.png"' in cd


def test_path_is_required(client):
    r = client.get("/api/files/raw")
    assert r.status_code == 422
    assert r.headers["cache-control"] == "no-store"


def test_logged_out_is_401_and_not_cacheable(root, monkeypatch):
    """`logged_in` refuses before the route runs, so this 401 carries the app-wide headers and the
    files middleware's no-store, not the route's sandbox set — and it carries no file bytes."""
    monkeypatch.delenv("AGENT_SESSIONS_AUTH_MODE", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", "pbkdf2_sha256$1000$abc$def")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(root / "2fa.json"))
    (root / "shot.png").write_bytes(_png())
    from agent_sessions import main

    r = TestClient(main.create_app()).get("/api/files/raw", params={"path": str(root / "shot.png")})
    assert r.status_code == 401
    assert r.headers.get("cache-control") == "no-store"
    assert files.raw_streams_inflight() == 0


def test_the_app_csp_is_unchanged(client, root):
    """`default-src 'self'` already covers media and `img-src` already allows self, so the app's
    own policy needs nothing for this feature — pinned so nobody 'helpfully' widens it."""
    from agent_sessions import security_headers

    csp = security_headers.content_security_policy([])
    assert "default-src 'self'" in csp
    assert "img-src 'self' data: blob:" in csp
    assert "media-src" not in csp
    assert "object-src 'none'" in csp


# --------------------------------------------------------------------------- parity with read


def _refusal_cases(root):
    outside = root.parent / "outside.png"
    outside.write_bytes(_png())
    (root / "real.png").write_bytes(_png())
    (root / "alias.png").symlink_to(root / "real.png")
    (root / "d").mkdir()
    os.mkfifo(root / "pipe.png")
    return {
        "outside": str(outside),
        "dotdot": str(root / ".." / "outside.png"),
        "etc": "/etc/passwd",
        "symlink": str(root / "alias.png"),
        "directory": str(root / "d"),
        "fifo": str(root / "pipe.png"),
        "missing": str(root / "gone.png"),
    }


def test_raw_refuses_exactly_what_read_refuses(client, root):
    for label, path in _refusal_cases(root).items():
        read = client.get("/api/files/read", params={"path": path})
        raw = client.get("/api/files/raw", params={"path": path, "download": "1"})
        assert read.status_code >= 400, label
        assert (
            raw.status_code == read.status_code
        ), f"{label}: raw {raw.status_code} vs read {read.status_code}"
        assert raw.headers["content-security-policy"] == SANDBOX, label
    assert _wait_inflight(0) == 0, "a refused open leaked a stream slot"


def test_what_read_serves_raw_serves(client, root):
    (root / "sub").mkdir()
    (root / "sub" / "shot.png").write_bytes(_png())
    path = root / "sub" / "shot.png"
    assert client.get("/api/files/read", params={"path": str(path)}).status_code == 200
    assert _raw(client, path).status_code == 200


# --------------------------------------------------------------------------- range


@pytest.fixture()
def digits(root):
    p = root / "digits.mp3"
    p.write_bytes(b"0123456789")
    return p


def test_single_range_is_206_with_the_exact_bytes(client, digits):
    r = client.get("/api/files/raw", params={"path": str(digits)}, headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.headers["content-length"] == "4"


def test_suffix_and_open_ended_ranges(client, digits):
    tail = client.get("/api/files/raw", params={"path": str(digits)}, headers={"Range": "bytes=-3"})
    assert (tail.status_code, tail.content) == (206, b"789")
    rest = client.get("/api/files/raw", params={"path": str(digits)}, headers={"Range": "bytes=7-"})
    assert (rest.status_code, rest.content) == (206, b"789")
    clipped = client.get(
        "/api/files/raw", params={"path": str(digits)}, headers={"Range": "bytes=8-99"}
    )
    assert (clipped.status_code, clipped.content) == (206, b"89")
    assert clipped.headers["content-range"] == "bytes 8-9/10"


def test_unsatisfiable_range_is_416_with_the_size(client, digits):
    r = client.get("/api/files/raw", params={"path": str(digits)}, headers={"Range": "bytes=10-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */10"
    assert _wait_inflight(0) == 0


@pytest.mark.parametrize("header", ["bytes=0-1,4-5", "bytes=abc", "items=0-1", "bytes=5-2"])
def test_multi_or_malformed_ranges_get_the_whole_file(client, digits, header):
    r = client.get("/api/files/raw", params={"path": str(digits)}, headers={"Range": header})
    assert r.status_code == 200
    assert r.content == b"0123456789"


def test_a_large_file_streams_in_chunks_and_whole(client, root):
    data = os.urandom(files.RAW_CHUNK * 3 + 17)
    (root / "big.webm").write_bytes(data)
    r = _raw(client, root / "big.webm")
    assert r.status_code == 200 and r.content == data
    mid = client.get(
        "/api/files/raw",
        params={"path": str(root / "big.webm")},
        headers={"Range": f"bytes={files.RAW_CHUNK - 5}-{files.RAW_CHUNK * 2 + 5}"},
    )
    assert mid.content == data[files.RAW_CHUNK - 5 : files.RAW_CHUNK * 2 + 6]


# --------------------------------------------------------------------------- the descriptor


def test_a_swap_after_the_open_is_never_served(root):
    """Bytes come from the VERIFIED descriptor: replacing the path afterwards changes nothing."""
    p = root / "clip.webm"
    p.write_bytes(b"ORIGINAL-BYTES")
    raw = files.open_raw(str(p))
    try:
        os.unlink(p)
        outside = root.parent / "outside.webm"
        outside.write_bytes(b"SHOULD-NEVER-BE-RETURNED")
        os.symlink(outside, p)
        assert raw.read(0, 64) == b"ORIGINAL-BYTES"
    finally:
        raw.close()


def test_a_repoint_between_check_and_open_is_refused(root, monkeypatch):
    outside = root.parent / "outside.webm"
    outside.write_bytes(b"SHOULD-NEVER-BE-RETURNED")
    target = root / "swap.webm"
    target.write_bytes(b"innocent")
    real = fsbrowse.contained_path

    def repointing(path, root_=None):
        resolved = real(path)
        if resolved.endswith("swap.webm"):
            os.unlink(resolved)
            os.symlink(str(outside), resolved)
        return resolved

    monkeypatch.setattr(files, "contained_path", repointing)
    with pytest.raises(fsbrowse.FsError):
        raw = files.open_raw(str(target))
        try:
            assert b"SHOULD-NEVER" not in raw.read(0, 64)
        finally:
            raw.close()
    assert files.raw_streams_inflight() == 0


def test_close_is_exactly_once_and_frees_the_slot(root):
    (root / "a.webm").write_bytes(b"x")
    raw = files.open_raw(str(root / "a.webm"))
    assert files.raw_streams_inflight() == 1
    assert raw.close() is True
    assert raw.close() is False
    assert files.raw_streams_inflight() == 0
    with pytest.raises(fsbrowse.FsError):
        raw.read(0, 1)


# --------------------------------------------------------------------------- the stream cap


def test_the_fifth_stream_is_503_with_retry_after_while_list_and_read_answer(client, root):
    (root / "a.webm").write_bytes(b"x")
    (root / "note.txt").write_text("hi")
    held = [files.open_raw(str(root / "a.webm")) for _ in range(files.RAW_MAX_STREAMS)]
    try:
        r = _raw(client, root / "a.webm")
        assert r.status_code == 503
        assert r.headers["retry-after"] == "3"
        assert r.headers["content-security-policy"] == SANDBOX
        assert client.get("/api/files/list", params={"path": str(root)}).status_code == 200
        assert (
            client.get("/api/files/read", params={"path": str(root / "note.txt")}).status_code
            == 200
        )
    finally:
        for h in held:
            h.close()
    assert _raw(client, root / "a.webm").status_code == 200


# --------------------------------------------------------------------------- a real server


@pytest.fixture()
def live(app):
    """The real app on a real socket: the only way to model a stalled or departed client."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not (server.started and server.servers) and time.monotonic() < deadline:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield port
    server.should_exit = True
    t.join(timeout=10)


def _open_stream(port: int, path, extra: str = "") -> socket.socket:
    """Send a raw GET and return the socket WITHOUT reading the body: a stalled client."""
    from urllib.parse import quote

    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(
        f"GET /api/files/raw?path={quote(str(path))} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n{extra}\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1)
    assert head.startswith(b"HTTP/1.1 20"), head[:80]
    return s


def test_stalled_and_departed_clients_give_their_slots_back(live, root):
    data = os.urandom(8 * 1024 * 1024)  # bigger than the socket buffers, so a stalled reader stalls
    (root / "big.webm").write_bytes(data)
    (root / "note.txt").write_text("hi")
    socks = [_open_stream(live, root / "big.webm") for _ in range(files.RAW_MAX_STREAMS)]
    try:
        assert _wait_inflight(files.RAW_MAX_STREAMS) == files.RAW_MAX_STREAMS
        import urllib.error
        import urllib.request
        from urllib.parse import quote

        url = f"http://127.0.0.1:{live}"
        with pytest.raises(urllib.error.HTTPError) as busy:
            urllib.request.urlopen(
                f"{url}/api/files/raw?path={quote(str(root / 'big.webm'))}", timeout=5
            )
        assert busy.value.code == 503
        # The panel keeps answering while every stream slot is held by a stalled reader.
        with urllib.request.urlopen(
            f"{url}/api/files/list?path={quote(str(root))}", timeout=5
        ) as r:
            assert r.status == 200
        with urllib.request.urlopen(
            f"{url}/api/files/read?path={quote(str(root / 'note.txt'))}", timeout=5
        ) as r:
            assert r.status == 200
    finally:
        for s in socks:  # every stalled reader hangs up mid-body
            s.close()
    assert _wait_inflight(0, timeout=10) == 0, "a departed client kept its stream slot"


def test_a_completed_range_and_a_mid_stream_error_both_free_the_slot(live, root, monkeypatch):
    data = os.urandom(files.RAW_CHUNK * 4)
    (root / "big.webm").write_bytes(data)
    import urllib.request
    from urllib.parse import quote

    url = f"http://127.0.0.1:{live}/api/files/raw?path={quote(str(root / 'big.webm'))}"
    req = urllib.request.Request(url, headers={"Range": "bytes=10-99"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 206 and r.read() == data[10:100]
    assert _wait_inflight(0) == 0

    calls = {"n": 0}
    real_read = files.RawFile.read

    def failing(self, offset, n):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk went away")
        return real_read(self, offset, n)

    monkeypatch.setattr(files.RawFile, "read", failing)
    import http.client
    import urllib.error

    # The headers are already out when the read fails, so the client sees a cut-off body.
    with pytest.raises((http.client.IncompleteRead, urllib.error.URLError, ConnectionError)):
        with urllib.request.urlopen(url, timeout=5) as r:
            r.read()
    assert _wait_inflight(0, timeout=10) == 0, "a server-side error kept the stream slot"


# --------------------------------------------------------------------------- review 6644


@pytest.fixture()
def held_open(root, monkeypatch):
    """`files.open_raw` that has OPENED (slot + fd taken) and then waits for the test's word."""
    (root / "a.webm").write_bytes(b"x")
    started, proceed = threading.Event(), threading.Event()
    real = files.open_raw

    def slow(path):
        raw = real(path)
        started.set()
        proceed.wait(5)
        return raw

    monkeypatch.setattr(files, "open_raw", slow)
    return root / "a.webm", started, proceed


def test_a_cancelled_open_closes_the_stream_it_orphans(held_open):
    """The awaiter is cancelled while the open is still running; when the worker finishes, the
    RawFile nobody will receive must be closed — fd and stream slot — not leaked (Hermes 6644)."""
    import asyncio

    from agent_sessions.routes import files as routes_files

    path, started, proceed = held_open

    async def go():
        task = asyncio.create_task(routes_files._open_raw(str(path)))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert files.raw_streams_inflight() == 1, "the worker still holds it until it returns"
        proceed.set()

    asyncio.run(go())
    assert _wait_inflight(0) == 0, "an orphaned open kept its stream slot"


def test_a_cancel_that_lands_after_the_open_finished_still_closes_it(held_open):
    """The other interleaving: the worker has already returned, but the awaiter is cancelled
    before it resumes. The done-callback runs at once on a finished future."""
    import asyncio

    from agent_sessions.routes import files as routes_files

    path, started, proceed = held_open

    async def go():
        task = asyncio.create_task(routes_files._open_raw(str(path)))
        await asyncio.to_thread(started.wait, 5)
        proceed.set()
        time.sleep(0.3)  # deliberately BLOCK the loop: the worker finishes, the task cannot resume
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert _wait_inflight(0) == 0, "a result that finished before the cancel was leaked"


def test_max_refuses_a_bigger_file_before_sending_a_byte(client, root):
    (root / "clip.webm").write_bytes(b"0123456789")
    over = _raw(client, root / "clip.webm", max="5")
    assert over.status_code == 413
    assert over.headers["content-security-policy"] == SANDBOX
    assert b"0123456789" not in over.content
    at = _raw(client, root / "clip.webm", max="10")
    assert (at.status_code, at.content) == (200, b"0123456789")
    assert _wait_inflight(0) == 0


def test_a_file_that_grows_mid_stream_never_sends_more_than_its_size_at_open(
    client, root, monkeypatch
):
    """The body length is fixed when the file is opened, which is what makes `max` a real bound."""
    p = root / "grow.webm"
    p.write_bytes(b"A" * 100)
    real_read = files.RawFile.read
    grown = {"done": False}

    def growing(self, offset, n):
        if not grown["done"]:
            grown["done"] = True
            with open(p, "ab") as f:
                f.write(b"B" * (files.RAW_CHUNK * 2))
        return real_read(self, offset, n)

    monkeypatch.setattr(files.RawFile, "read", growing)
    r = _raw(client, p, max="100")
    assert r.status_code == 200
    assert r.content == b"A" * 100
    assert r.headers["content-length"] == "100"


@pytest.mark.parametrize("bad", ["²", "³", "١٢", "-1", "1e3", "", " 5", "0x10"])
def test_a_malformed_max_is_refused_and_frees_its_slot(client, root, bad):
    """Hermes 6648: `str.isdigit()` accepts `²`, `int()` then raised outside the refusal handler
    and leaked the open stream — four requests and the viewer could never stream again."""
    (root / "clip.webm").write_bytes(b"0123456789")
    for _ in range(files.RAW_MAX_STREAMS + 1):
        r = _raw(client, root / "clip.webm", max=bad)
        assert r.status_code == 422, (bad, r.status_code)
        assert r.headers["content-security-policy"] == SANDBOX
    assert _wait_inflight(0) == 0, f"max={bad!r} leaked a stream slot"
    assert _raw(client, root / "clip.webm").status_code == 200, "the next stream was refused"


def test_anything_unexpected_before_the_stream_still_frees_the_slot(client, root, monkeypatch):
    """Ownership, not a list of known exceptions: whatever fails between the open and the
    response taking the stream, the frame that holds it closes it."""
    (root / "clip.webm").write_bytes(b"0123456789")

    def boom(header, size):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(files, "parse_range", boom)
    for _ in range(files.RAW_MAX_STREAMS + 1):
        r = _raw(client, root / "clip.webm")
        assert r.status_code == 500
        assert r.headers["content-security-policy"] == SANDBOX
    assert _wait_inflight(0) == 0
