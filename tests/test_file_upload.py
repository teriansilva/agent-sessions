"""Upload backend tests (#807).

The valuable ones here are adversarial, and three of them exist because the obvious
implementation is wrong in a way that reads as correct:

* ``UploadFile = File(...)`` does not bound anything — Starlette has already spooled the whole
  file part by the time the route runs, so the bound has to be applied at INGRESS. The chunked
  oversize test asserts bounded temp disk and no destination artifact, not merely a 413.
* A per-batch limit "counted afterwards" is not a limit: concurrent requests both read the
  penultimate value and both proceed. The overshoot and under-declaration tests sample the
  accounting *during* the streams.
* Refusing a component named ``.git`` does not close the git-metadata surface: a bare repo, a
  ``.git``-file indirection, and a linked worktree's common-dir all hold ``hooks/`` and ``config``
  under a name that comparison never sees.

Everything runs under ``AGENT_SESSIONS_FS_ROOT``; the real ``~`` is never touched.
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, filewrite
from agent_sessions.files import FsError


@pytest.fixture(autouse=True)
def _reset():
    files._inflight_total = 0
    files._inflight_by_root.clear()
    filewrite.reset_batches_for_test()
    yield
    filewrite.reset_batches_for_test()
    files._inflight_total = 0
    files._inflight_by_root.clear()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "home"
    r.mkdir()
    monkeypatch.setenv("AGENT_SESSIONS_FS_ROOT", str(r))
    files.reset_capabilities_for_test()
    yield r
    files.reset_capabilities_for_test()


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def _hdr(c, cfg):
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def _post(c, cfg, target, relpath, data=b"hello\n", **extra):
    fields = {"dir": str(target), "relpath": relpath, **extra}
    return c.post(
        "/api/files/upload",
        data=fields,
        files={"file": (relpath.split("/")[-1], data, "application/octet-stream")},
        headers=_hdr(c, cfg),
    )


# --------------------------------------------------------------------------- the happy path


def test_a_file_lands_where_the_panel_was_looking(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "notes.md", b"# hi\n")
    assert r.status_code == 200, r.text
    assert (root / "notes.md").read_bytes() == b"# hi\n"
    assert r.json()["bytes"] == 5


def test_a_folder_uploads_whole_with_its_structure(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "src/lib/util.ts", b"export {}\n")
    assert r.status_code == 200, r.text
    assert (root / "src" / "lib" / "util.ts").read_bytes() == b"export {}\n"


def test_an_uploaded_file_is_never_executable(client, root, auth_cfg):
    _post(client, auth_cfg, root, "run.sh", b"#!/bin/sh\necho hi\n")
    mode = (root / "run.sh").stat().st_mode
    assert (
        not mode & stat.S_IXUSR
    ), "an upload landed executable in a tree an agent runs commands in"
    assert not mode & (stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------- containment


@pytest.mark.parametrize(
    "bad",
    ["../escape.txt", "a/../../escape.txt", "/abs.txt", "a//b.txt", "./x.txt", "a/./b.txt"],
)
def test_zip_slip_shapes_are_refused_as_components(root, bad):
    """Validated as components, never as a joined string a prefix check has to normalise."""
    with pytest.raises(FsError) as e:
        filewrite.validate_relpath(bad)
    assert e.value.status == 422


def test_a_nul_or_control_byte_in_a_name_is_refused(root):
    with pytest.raises(FsError):
        filewrite.validate_relpath("a\x00b.txt")
    with pytest.raises(FsError):
        filewrite.validate_relpath("a\nb.txt")


def test_depth_and_component_length_are_bounded(root):
    with pytest.raises(FsError):
        filewrite.validate_relpath("/".join(["d"] * (filewrite.MAX_DEPTH + 1)) + "/f.txt")
    with pytest.raises(FsError):
        filewrite.validate_relpath("x" * 300)


def test_a_symlinked_intermediate_directory_is_refused_not_followed(
    client, root, auth_cfg, tmp_path
):
    """O_NOFOLLOW on EVERY component open — the symlink fails the open rather than redirecting
    the write. Never checked-then-opened, so there is no window to race."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    r = _post(client, auth_cfg, root, "link/pwned.txt")
    assert r.status_code == 422, r.text
    assert not (outside / "pwned.txt").exists(), "the write followed a symlink out of the root"


def test_a_target_outside_the_root_is_refused(client, root, auth_cfg, tmp_path):
    r = _post(client, auth_cfg, tmp_path / "elsewhere", "x.txt")
    assert r.status_code in (403, 404), r.text


# --------------------------------------------------------------------------- collisions


def test_a_collision_fails_by_default_and_never_overwrites(client, root, auth_cfg):
    (root / "a.txt").write_bytes(b"original\n")
    r = _post(client, auth_cfg, root, "a.txt", b"replacement\n")
    assert r.status_code == 409, r.text
    assert (root / "a.txt").read_bytes() == b"original\n"


def test_keep_both_takes_the_next_free_name(client, root, auth_cfg):
    (root / "a.txt").write_bytes(b"original\n")
    r = _post(client, auth_cfg, root, "a.txt", b"second\n", on_collision="keep_both")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "a (2).txt"
    assert (root / "a.txt").read_bytes() == b"original\n"
    assert (root / "a (2).txt").read_bytes() == b"second\n"


def test_replace_publishes_by_rename_and_leaves_no_temp(client, root, auth_cfg):
    (root / "a.txt").write_bytes(b"original\n")
    r = _post(client, auth_cfg, root, "a.txt", b"replacement\n", on_collision="replace")
    assert r.status_code == 200, r.text
    assert (root / "a.txt").read_bytes() == b"replacement\n"
    leftovers = [p.name for p in root.iterdir() if p.name.startswith(".a.txt.upload-")]
    assert leftovers == [], f"a temp file survived: {leftovers}"


def test_an_unknown_collision_mode_is_refused(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "a.txt", on_collision="clobber")
    assert r.status_code == 422


# --------------------------------------------------------------------------- git metadata


def _repo(at):
    at.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(at)], check=True)
    return at


def test_a_plain_dot_git_is_refused(client, root, auth_cfg):
    _repo(root / "proj")
    r = _post(client, auth_cfg, root / "proj", ".git/hooks/pre-commit", b"#!/bin/sh\nid\n")
    assert r.status_code == 403, r.text
    assert not (root / "proj" / ".git" / "hooks" / "pre-commit").exists()


def test_a_bare_repository_is_refused_even_though_nothing_is_called_dot_git(client, root, auth_cfg):
    """`repo.git/` holds `hooks/` and `config` under a name a `.git` comparison never sees."""
    bare = root / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = _post(client, auth_cfg, bare, "hooks/pre-commit", b"#!/bin/sh\nid\n")
    assert r.status_code == 403, r.text
    assert not (bare / "hooks" / "pre-commit").exists()


def test_a_gitdir_reached_through_a_dot_git_FILE_is_refused(client, root, auth_cfg):
    """#782 explicitly supports contained gitdir indirection, so the metadata root is DISCOVERED."""
    work = root / "work"
    work.mkdir()
    real = root / "elsewhere-gitdir"
    subprocess.run(["git", "init", "-q", "--bare", str(real)], check=True)
    (work / ".git").write_text(f"gitdir: {real}\n")
    r = _post(client, auth_cfg, real, "hooks/pre-commit", b"#!/bin/sh\nid\n")
    assert r.status_code == 403, r.text
    assert not (real / "hooks" / "pre-commit").exists()


def test_a_linked_worktrees_common_dir_is_refused(client, root, auth_cfg):
    main = _repo(root / "main")
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "t"], check=True)
    (main / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(main), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "i"], check=True)
    wt = root / "wt"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "side"],
        check=True,
        capture_output=True,
    )
    # The linked worktree's own gitdir lives under the MAIN repo's .git/worktrees/…
    r = _post(client, auth_cfg, main / ".git", "hooks/pre-commit", b"#!/bin/sh\nid\n")
    assert r.status_code == 403, r.text


def test_an_ordinary_file_in_a_repository_still_uploads(client, root, auth_cfg):
    """The refusal must be about METADATA, not about repositories — a project tree is the point."""
    proj = _repo(root / "proj")
    r = _post(client, auth_cfg, proj, "src/new.py", b"print(1)\n")
    assert r.status_code == 200, r.text
    assert (proj / "src" / "new.py").exists()


# --------------------------------------------------------------------------- the ingress bound


def _multipart(boundary: str, target, relpath: str, payload: bytes) -> bytes:
    b = boundary.encode()
    parts = []
    for name, val in (("dir", str(target)), ("relpath", relpath)):
        parts.append(
            b"--" + b + b"\r\n"
            b'Content-Disposition: form-data; name="'
            + name.encode()
            + b'"\r\n\r\n'
            + val.encode()
            + b"\r\n"
        )
    parts.append(
        b"--" + b + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n"
    )
    parts.append(b"--" + b + b"--\r\n")
    return b"".join(parts)


def test_a_chunked_oversized_body_is_cut_off_with_no_honest_content_length(
    client, root, auth_cfg, tmp_path, monkeypatch
):
    """The test Hermes asked for: CHUNKED, oversized, and no truthful length.

    Asserts three things, because a 413 alone would not distinguish this from the broken shape:
    a NAMED 413, **bounded temp disk**, and **no artifact at the destination**.
    """
    tmpdir = tmp_path / "spool"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    boundary = "----bnd"
    over = filewrite.MAX_FILE_BYTES + 4 * 1024 * 1024
    body = _multipart(boundary, root, "big.bin", b"\0" * over)

    def chunks():
        # A generator body ⇒ httpx sends `Transfer-Encoding: chunked`, so there is no
        # Content-Length for the cheap precheck to catch. The real bound has to do the work.
        for i in range(0, len(body), 64 * 1024):
            yield body[i : i + 64 * 1024]

    r = client.post(
        "/api/files/upload",
        content=chunks(),
        headers={
            **_hdr(client, auth_cfg),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status_code == 413, r.text
    assert "limit" in r.json()["detail"].lower() or "larger" in r.json()["detail"].lower()
    assert not (root / "big.bin").exists(), "a refused upload left an artifact at the destination"
    spooled = sum(f.stat().st_size for f in tmpdir.rglob("*") if f.is_file())
    assert spooled <= 1024 * 1024, f"the parser spooled {spooled} bytes to temp disk"


def test_an_honest_oversized_content_length_is_refused_early(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "big.bin", b"\0" * (filewrite.MAX_FILE_BYTES + 1))
    assert r.status_code == 413, r.text
    assert not (root / "big.bin").exists()


def test_the_file_part_may_not_arrive_before_the_fields(client, root, auth_cfg):
    boundary = "----bnd"
    body = (
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
        b"payload\r\n"
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Disposition: form-data; name="dir"\r\n\r\n' + str(root).encode() + b"\r\n"
        b"--" + boundary.encode() + b"--\r\n"
    )
    r = client.post(
        "/api/files/upload",
        content=body,
        headers={
            **_hdr(client, auth_cfg),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status_code == 422, r.text
    assert not (root / "x.txt").exists()


# --------------------------------------------------------------------------- batches


def test_a_batch_is_refused_before_a_byte_moves_when_the_manifest_is_over_budget(
    client, root, auth_cfg
):
    files_ = [{"relpath": f"f{i}.bin", "size": 30 * 1024 * 1024} for i in range(20)]
    r = client.post(
        "/api/files/upload/batch", json={"files": files_}, headers=_hdr(client, auth_cfg)
    )
    assert r.status_code == 413, r.text


def test_a_manifest_naming_too_many_files_is_refused(client, root, auth_cfg):
    files_ = [{"relpath": f"f{i}", "size": 1} for i in range(filewrite.MAX_BATCH_FILES + 1)]
    r = client.post(
        "/api/files/upload/batch", json={"files": files_}, headers=_hdr(client, auth_cfg)
    )
    assert r.status_code == 413


def test_an_unknown_or_expired_batch_is_a_409_not_a_silent_unbounded_upload(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "a.txt", batch_id="nope")
    assert r.status_code == 409, r.text
    assert not (root / "a.txt").exists()


def test_concurrent_requests_cannot_both_pass_the_last_file_slot():
    """N threads, one slot. Exactly one may win — a limit checked as "count what has landed"
    would let two read the penultimate value and both proceed."""
    b = filewrite.Batch("t", {f"f{i}.bin": 10 for i in range(64)})
    b_files = filewrite.MAX_BATCH_FILES
    b.files_used = b_files - 1
    wins, start = [], threading.Barrier(16)
    lock = threading.Lock()

    def go(i):
        start.wait()
        try:
            b.take_file_slot(f"f{i}.bin")
        except filewrite.BatchError:
            return
        with lock:
            wins.append(i)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1, f"{len(wins)} requests passed a one-slot batch"
    assert b.files_used == b_files


def test_under_declaration_gains_nothing_and_the_cap_holds_at_every_instant():
    """Each 'request' declares 1 MiB and tries to stream far more.

    The assertion is that the accounting is never over the cap **while the streams are running**,
    not merely after a reconciliation — reserving a declared size and reconciling afterwards is
    exactly the model that does not bound anything.
    """
    n = 8
    declared = 1024 * 1024
    manifest = {f"f{i}.bin": declared for i in range(n)}
    b = filewrite.Batch("t", manifest)
    breaches: list[int] = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            with b.lock:
                if b.bytes_used > filewrite.MAX_BATCH_BYTES:
                    breaches.append(b.bytes_used)
            # A busy sample is the point: it must never observe an over-cap instant.

    def stream(i):
        rel = f"f{i}.bin"
        b.take_file_slot(rel)
        sent = 0
        # Try to stream 25x the declared size, in realistic chunks.
        for _ in range(25 * 16):
            try:
                b.take_bytes(rel, 64 * 1024)
            except FsError:
                break
            sent += 64 * 1024
        # A file may stream no more than its own manifest entry.
        assert sent <= declared, f"{rel} streamed {sent} against a {declared} declaration"

    s = threading.Thread(target=sampler, daemon=True)
    s.start()
    ts = [threading.Thread(target=stream, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    s.join(timeout=2)
    assert breaches == [], f"the batch cap was exceeded mid-stream: {breaches}"
    assert b.bytes_used <= filewrite.MAX_BATCH_BYTES


def test_a_failed_upload_rolls_its_reservation_back_so_one_bad_file_cannot_poison_a_batch(
    client, root, auth_cfg
):
    r = client.post(
        "/api/files/upload/batch",
        json={"files": [{"relpath": "a.txt", "size": 8}, {"relpath": "b.txt", "size": 8}]},
        headers=_hdr(client, auth_cfg),
    )
    bid = r.json()["batch_id"]
    (root / "a.txt").write_bytes(b"original\n")
    bad = _post(client, auth_cfg, root, "a.txt", b"nope\n", batch_id=bid)
    assert bad.status_code == 409
    good = _post(client, auth_cfg, root, "b.txt", b"fine\n", batch_id=bid)
    assert good.status_code == 200, good.text
    assert good.json()["batch"]["files_used"] == 1, "the failed file kept its slot"


def test_a_file_not_in_the_manifest_is_refused(client, root, auth_cfg):
    r = client.post(
        "/api/files/upload/batch",
        json={"files": [{"relpath": "a.txt", "size": 8}]},
        headers=_hdr(client, auth_cfg),
    )
    bid = r.json()["batch_id"]
    out = _post(client, auth_cfg, root, "smuggled.txt", b"x\n", batch_id=bid)
    assert out.status_code == 422, out.text
    assert not (root / "smuggled.txt").exists()


# --------------------------------------------------------------------------- route guards


def test_upload_needs_csrf(client, root, auth_cfg):
    r = client.post(
        "/api/files/upload",
        data={"dir": str(root), "relpath": "a.txt"},
        files={"file": ("a.txt", b"x", "application/octet-stream")},
    )
    assert r.status_code == 403
    assert not (root / "a.txt").exists()
    r = client.post("/api/files/upload/batch", json={"files": [{"relpath": "a", "size": 1}]})
    assert r.status_code == 403


def test_no_upload_is_reachable_by_GET(client, root):
    for route in ("/api/files/upload", "/api/files/upload/batch"):
        r = client.get(route, params={"dir": str(root), "relpath": "a.txt"})
        assert r.status_code in (404, 405), f"{route} answered a GET with {r.status_code}"
    assert list(root.iterdir()) == [], "a GET wrote to the destination"


def test_upload_responses_are_never_cached(client, root, auth_cfg):
    r = _post(client, auth_cfg, root, "a.txt")
    assert r.headers["cache-control"] == "no-store"


def test_only_one_file_per_request(client, root, auth_cfg):
    """MEASURED before the guard: a second file part streamed into the FIRST part's descriptor,
    so two parts produced one `first.txt` containing `AAAABBBB` — a crafted multipart could append
    content to a file the operator named and never saw, and the batch took two slots for one
    destination."""
    b = "----bnd"
    body = (
        f'--{b}\r\nContent-Disposition: form-data; name="dir"\r\n\r\n{root}\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="relpath"\r\n\r\nfirst.txt\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="first.txt"\r\n\r\nAAAA\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="file2"; '
        f'filename="second.txt"\r\n\r\nBBBB\r\n'
        f"--{b}--\r\n"
    ).encode()
    r = client.post(
        "/api/files/upload",
        content=body,
        headers={**_hdr(client, auth_cfg), "Content-Type": f"multipart/form-data; boundary={b}"},
    )
    assert r.status_code == 422, r.text
    assert not (root / "first.txt").exists(), "a refused multi-part upload left an artifact"


def test_the_umask_is_read_without_changing_it(monkeypatch):
    """The usual `os.umask(x)`/`os.umask(back)` swap is a process-global mutation with a window
    another pool thread can create a file in. Reading it must not touch it."""
    filewrite._umask_cache = None
    calls = []
    real = os.umask
    monkeypatch.setattr(os, "umask", lambda m: (calls.append(m), real(m))[1])
    filewrite._umask()
    assert calls == [], "reading the umask mutated it"
    filewrite._umask_cache = None


# ------------------------------------------- review round 2 (#827)


def test_a_short_write_never_publishes_a_truncated_file(client, root, auth_cfg, monkeypatch):
    """`os.write()` may accept FEWER bytes than it is given. Ignoring the count published three
    bytes of an eight-byte upload and reported 200 — a truncated file reported as success."""
    real = os.write
    state = {"first": True}

    def short(fd, data):
        if state["first"] and len(data) > 3:
            state["first"] = False
            return real(fd, data[:3])
        return real(fd, data)

    monkeypatch.setattr(filewrite.os, "write", short)
    r = _post(client, auth_cfg, root, "whole.bin", b"abcdefgh")
    assert r.status_code == 200, r.text
    assert (root / "whole.bin").read_bytes() == b"abcdefgh", "a short write truncated the upload"


def test_replace_refuses_when_the_target_changed_while_streaming(
    client, root, auth_cfg, monkeypatch
):
    """The operator chose `replace` against a specific file. Streaming takes time, and this panel
    is docked into a session an agent is editing — an unconditional rename destroys work that
    appeared AFTER the decision."""
    target = root / "a.txt"
    target.write_bytes(b"original\n")
    real = filewrite.Destination.write

    def racy(self, data):
        # The agent rewrites the file mid-upload, exactly once.
        if getattr(self, "_raced", False) is False:
            self._raced = True
            target.write_bytes(b"NEWER WORK FROM THE AGENT\n")
        return real(self, data)

    monkeypatch.setattr(filewrite.Destination, "write", racy)
    r = _post(client, auth_cfg, root, "a.txt", b"upload\n", on_collision="replace")
    assert r.status_code == 409, r.text
    assert "changed while it was being uploaded" in r.json()["detail"]
    assert target.read_bytes() == b"NEWER WORK FROM THE AGENT\n", "the newer edit was destroyed"


def test_a_gitdir_cannot_be_assembled_one_upload_at_a_time(client, root, auth_cfg):
    """A shape built one innocent request at a time, and the piece that can never arrive.

    A pre-write check only sees what already exists, so it is blind to a shape assembled across
    requests — which is why this used to need a lock, and why that lock lost three review rounds.
    It is now settled without serialising anything: every other part may land (an ordinary folder
    upload must keep working), but a HEAD file cannot be created, and without one neither git nor
    `_looks_like_gitdir` treats the directory as a repository.
    """
    d = root / "innocent"
    d.mkdir()
    for rel, body in (
        ("objects/placeholder", b"x"),
        ("refs/heads/main", b"0" * 40),
        ("config", b"[core]\n\thooksPath = .\n"),
        ("hooks/pre-commit", b"#!/bin/sh\nid\n"),
    ):
        r = _post(client, auth_cfg, d, rel, body)
        assert r.status_code == 200, f"{rel}: {r.text}"
    # Everything is in place except the one file that would arm it.
    r = _post(client, auth_cfg, d, "HEAD", b"ref: refs/heads/main\n")
    assert r.status_code == 403, r.text
    assert not (d / "HEAD").exists(), "the arming upload was published"
    assert not filewrite._looks_like_gitdir(str(d)), "the shape completed anyway"


def test_a_settled_batch_stops_counting_against_the_session(client, root, auth_cfg):
    """Batches were removed only by IDLE EXPIRY, so N completed one-file drops locked the next one
    out for the whole TTL."""
    for i in range(filewrite.MAX_LIVE_BATCHES + 2):
        r = client.post(
            "/api/files/upload/batch",
            json={"files": [{"relpath": f"f{i}.txt", "size": 5}]},
            headers=_hdr(client, auth_cfg),
        )
        assert r.status_code == 200, f"drop {i} refused: {r.text}"
        bid = r.json()["batch_id"]
        up = _post(client, auth_cfg, root, f"f{i}.txt", b"hello", batch_id=bid)
        assert up.status_code == 200, up.text


def test_a_batch_belongs_to_the_session_that_minted_it():
    """An id alone must not let another session spend someone else's allowance."""
    made = filewrite.create_batch([{"relpath": "a.txt", "size": 4}], owner="session-a")
    with pytest.raises(filewrite.BatchError) as e:
        filewrite.get_batch(made["batch_id"], owner="session-b")
    assert e.value.status == 403
    assert filewrite.get_batch(made["batch_id"], owner="session-a") is not None


def test_one_session_cannot_exhaust_every_other_sessions_allowance():
    for i in range(filewrite.MAX_LIVE_BATCHES):
        filewrite.create_batch([{"relpath": f"f{i}", "size": 1}], owner="greedy")
    with pytest.raises(filewrite.BatchError):
        filewrite.create_batch([{"relpath": "x", "size": 1}], owner="greedy")
    # A different session is unaffected — the cap is per owner, not process-global.
    assert filewrite.create_batch([{"relpath": "y", "size": 1}], owner="polite")["batch_id"]


# ------------------------------------------- review round 3 (#827)


def test_a_refused_gitdir_completion_leaves_NOTHING_behind(client, root, auth_cfg):
    """A refusal must leave nothing behind — not even the folders it walked through.

    The old refusal fired at publish time, after the request had already created its `refs/`
    chain, and `abandon()` removed only the leaf: the shape was complete the moment the 403 was
    written. The refusal now happens during relpath validation, BEFORE any descent, so a deep
    path is rejected without the intermediate directories ever existing. That is the same
    property, obtained by never starting rather than by cleaning up correctly.
    """
    d = root / "innocent"
    d.mkdir()
    for rel, body in (("objects/placeholder", b"x"), ("refs/heads/main", b"0" * 40)):
        assert _post(client, auth_cfg, d, rel, body).status_code == 200
    r = _post(client, auth_cfg, d, "deeply/nested/HEAD", b"ref: refs/heads/main\n")
    assert r.status_code == 403, r.text
    assert not (d / "deeply").exists(), "a refused upload created the folders on its way down"
    assert not filewrite._looks_like_gitdir(str(d)), "a gitdir shape survived the refusal"


def test_replace_catches_a_SAME_SIZE_in_place_rewrite(client, root, auth_cfg, monkeypatch):
    """`(inode, mtime_ns, size)` compares equal when an agent rewrites a file in place without
    changing its length and the timestamp is coarse or restored — so the stale choice overwrote
    newer work anyway. The content digest is what actually answers the question."""
    target = root / "a.txt"
    target.write_bytes(b"AAAAAAAA")
    st = target.stat()
    real = filewrite.Destination.write

    def racy(self, data):
        if getattr(self, "_raced", False) is False:
            self._raced = True
            # Same length, and the timestamp put back exactly as it was.
            target.write_bytes(b"BBBBBBBB")
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        return real(self, data)

    monkeypatch.setattr(filewrite.Destination, "write", racy)
    r = _post(client, auth_cfg, root, "a.txt", b"UPLOADED", on_collision="replace")
    assert r.status_code == 409, r.text
    assert target.read_bytes() == b"BBBBBBBB", "a same-size rewrite was destroyed"


def test_an_upload_can_NEVER_create_a_HEAD_file(client, root, auth_cfg):
    """The structural rule that replaced the fence.

    A directory is a repository to git — and to `_looks_like_gitdir` — only if it holds a HEAD
    *file*. Refusing to create one makes the gitdir shape uncompletable by upload without
    consulting any directory state, so there is no window for a second request to exploit. The
    lock this replaced was defeated three review rounds running.
    """
    for rel in ("HEAD", "nested/HEAD", "a/b/c/HEAD"):
        r = _post(client, auth_cfg, root, rel, b"ref: refs/heads/main\n")
        assert r.status_code == 403, f"{rel} was accepted: {r.text}"
        assert not (root / rel).exists(), f"{rel} landed despite the refusal"


def test_names_that_merely_RESEMBLE_head_are_left_alone(client, root, auth_cfg):
    """The rule is exact-case and last-component-only, so it must not become a general ban on
    the word. A refusal that swallows ordinary files would be a worse bug than the one it fixes.
    """
    for rel in ("HEAD.md", "head", "HEADER", "my-HEAD.txt", "HEAD/notes.txt"):
        r = _post(client, auth_cfg, root, rel, b"ordinary\n")
        assert r.status_code == 200, f"{rel} was wrongly refused: {r.text}"
        assert (root / rel).read_bytes() == b"ordinary\n"


def test_a_folder_with_a_HEAD_file_accepts_no_further_gitdir_part(client, root, auth_cfg):
    """The rule that closes the concurrent-completion window, pinned directly.

    Background, measured rather than assumed. Refusing the HEAD *leaf* stops one being uploaded,
    but not a shape completing where HEAD already exists by other means: two requests can both
    pass the existing shape-walk (neither sees `refs/` yet), both create the shared `refs/`, and
    then neither rollback can remove it — the owner cannot while the sibling's subtree is inside,
    and the sibling does not own it. `refs/` survives both refusals and any pre-planted
    `config`/`hooks/` become live in something git treats as a repository.

    **What I could not do is reproduce that interleaving deterministically**, and it is worth
    saying so rather than shipping a test that passes either way. Run sequentially it does not
    occur at all — the first request's `refs/` makes the candidate a complete shape, so the
    existing walk refuses the second before it creates anything. Forcing the true window needs
    both requests inside `_descend` *and* the owner of `refs/` rolling back first, and ownership
    is decided by a nondeterministic `mkdir` race that a test cannot pin from outside.

    So this pins the rule instead, which is what actually closes the window: with a HEAD file
    present, no further gitdir part can be created at all, so the shape never reaches three parts
    even transiently and there is nothing to roll back. Checked before any component is created,
    on state neither request mutates — both racers read the same fact and both refuse.
    """
    d = root / "candidate"
    (d / "objects").mkdir(parents=True)
    (d / "HEAD").write_text("ref: refs/heads/main\n")

    for rel in ("refs/heads/main", "refs/tags/x", "objects/pack/x.pack"):
        r = _post(client, auth_cfg, d, rel, b"0" * 40)
        assert r.status_code == 403, f"{rel} was accepted: {r.text}"
    # Nothing was created on the way to the refusal — that is the half a late check cannot give.
    assert not (d / "refs").exists(), "a refused upload created the folder it was refused for"
    assert not filewrite._looks_like_gitdir(str(d))


def test_an_ordinary_folder_still_accepts_objects_and_refs(client, root, auth_cfg):
    """The rule is conditioned on a HEAD file being present, not on the names alone.

    `objects/` and `refs/` are ordinary directory names in real projects, and a folder upload has
    to keep working. Banning them outright would have bought the same guarantee at a cost this
    feature cannot pay.
    """
    plain = root / "myproject"
    plain.mkdir()
    for rel in ("objects/mesh.bin", "refs/table.csv"):
        r = _post(client, auth_cfg, plain, rel, b"data\n")
        assert r.status_code == 200, f"{rel} was wrongly refused: {r.text}"
    assert (plain / "objects" / "mesh.bin").exists()
    assert (plain / "refs" / "table.csv").exists()


# --------------------------------------------------------------- Skip is bound to ONE entry


def _batch(client, auth_cfg, names):
    r = client.post(
        "/api/files/upload/batch",
        json={"files": [{"relpath": n, "size": 8} for n in names]},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def _skip(client, auth_cfg, bid, relpath):
    return client.post(
        "/api/files/upload/skip",
        json={"batch_id": bid, "relpath": relpath},
        headers=_hdr(client, auth_cfg),
    )


def test_a_duplicate_skip_cannot_retire_a_sibling_that_is_still_in_flight(client, root, auth_cfg):
    """The Skip endpoint took a relpath and ignored it, settling a bare counter.

    Two Skips for the SAME collision therefore settled a two-entry batch, `_reap` retired it,
    and the sibling that was genuinely still to come failed as `unknown or expired` — the
    operator's remaining file simply never landed. Settlement is per entry, so answering one
    question twice is a no-op, not an answer to the other one.
    """
    (root / "a.txt").write_bytes(b"EXISTING")
    bid = _batch(client, auth_cfg, ["a.txt", "b.txt"])

    clash = _post(client, auth_cfg, root, "a.txt", b"NEW", batch_id=bid)
    assert clash.status_code == 409, clash.text

    assert _skip(client, auth_cfg, bid, "a.txt").status_code == 200
    # The same answer again — a client retry after a dropped response.
    assert _skip(client, auth_cfg, bid, "a.txt").status_code == 200

    landed = _post(client, auth_cfg, root, "b.txt", b"FINE", batch_id=bid)
    assert landed.status_code == 200, f"the sibling was retired by a duplicate Skip: {landed.text}"
    assert (root / "b.txt").read_bytes() == b"FINE"
    assert (root / "a.txt").read_bytes() == b"EXISTING", "Skip must not write"


def test_skip_refuses_an_entry_that_is_not_in_the_manifest(client, root, auth_cfg):
    """A relpath the batch never declared is not a Skip — it is an attempt to settle something
    else."""
    bid = _batch(client, auth_cfg, ["a.txt"])
    r = _skip(client, auth_cfg, bid, "not-mine.txt")
    assert r.status_code == 422, r.text


def test_skip_refuses_an_entry_with_no_collision_awaiting_an_answer(client, root, auth_cfg):
    """Skip answers a *question*. With no outstanding collision there is nothing to answer, and
    allowing it let one request settle entries it had never been asked about."""
    bid = _batch(client, auth_cfg, ["a.txt", "b.txt"])
    r = _skip(client, auth_cfg, bid, "b.txt")
    assert r.status_code == 409, r.text
    # And the batch is still usable — a refused Skip settles nothing.
    assert _post(client, auth_cfg, root, "b.txt", b"FINE", batch_id=bid).status_code == 200


def test_a_late_skip_after_replace_is_refused_not_reported_as_skipped(client, root, auth_cfg):
    """Skip settled ANY already-terminal entry and answered 200.

    So a Skip that lost the race — the operator had already chosen Replace, and the file landed —
    came back `skipped: true`. That tells them nothing happened to a file that was in fact
    overwritten, which is the one thing the collision prompt exists to prevent. The entry's
    recorded outcome now decides: only a prior *skip* replays.
    """
    (root / "a.txt").write_bytes(b"EXISTING")
    # TWO entries deliberately: with one, the batch settles on Replace and is reaped, so a late
    # Skip gets 409 "unknown or expired" — the right status for the WRONG reason, which would
    # let this pass against the unfixed code. The sibling keeps the batch live so the late Skip
    # actually reaches `skip_file` and the real defect is exercised.
    bid = _batch(client, auth_cfg, ["a.txt", "b.txt"])

    assert _post(client, auth_cfg, root, "a.txt", b"NEW", batch_id=bid).status_code == 409
    replaced = _post(client, auth_cfg, root, "a.txt", b"NEW", batch_id=bid, on_collision="replace")
    assert replaced.status_code == 200, replaced.text
    assert (root / "a.txt").read_bytes() == b"NEW"

    late = _skip(client, auth_cfg, bid, "a.txt")
    assert late.status_code == 409, f"a late Skip was accepted after Replace: {late.text}"
    assert "stale" in late.json()["detail"], late.text


def test_a_retried_skip_on_a_ONE_FILE_batch_replays_instead_of_expiring(client, root, auth_cfg):
    """The idempotent-Skip branch was unreachable exactly where it mattered most.

    A one-file batch settles the moment its only file is skipped, and `_reap` deleted settled
    batches immediately — so the client's retry (a dropped response, a flaky relay) looked up a
    batch that no longer existed and got `unknown or expired`. Settled batches now linger for a
    short grace period, counting against no cap, purely so the retry can be answered.
    """
    (root / "a.txt").write_bytes(b"EXISTING")
    bid = _batch(client, auth_cfg, ["a.txt"])
    assert _post(client, auth_cfg, root, "a.txt", b"NEW", batch_id=bid).status_code == 409

    first = _skip(client, auth_cfg, bid, "a.txt")
    assert first.status_code == 200, first.text
    retry = _skip(client, auth_cfg, bid, "a.txt")
    assert (
        retry.status_code == 200
    ), f"the retry could not reach the idempotent branch: {retry.text}"
    assert (root / "a.txt").read_bytes() == b"EXISTING", "a replayed Skip must still not write"


def test_a_settled_batch_lingering_for_replay_still_frees_its_allowance(client, root, auth_cfg):
    """The grace period must not resurrect the lockout it was carved out of.

    Settled batches stay addressable, so they must be excluded from the live caps — otherwise
    eight completed one-file drops would lock the ninth out again, which is the bug `_reap` was
    added to fix in the first place.
    """
    for i in range(filewrite.MAX_LIVE_BATCHES + 2):
        made = filewrite.create_batch([{"relpath": f"f{i}.txt", "size": 4}], owner="one-session")
        filewrite.get_batch(made["batch_id"], owner="one-session").finish_file(
            f"f{i}.txt", "landed"
        )
    # Every one of those settled; a fresh batch must still be admitted.
    assert filewrite.create_batch([{"relpath": "z.txt", "size": 4}], owner="one-session")


def test_a_settled_batch_lingering_for_replay_accepts_NO_further_writes(client, root, auth_cfg):
    """The replay grace must not leave a finished batch write-capable.

    Settled batches stay addressable so a retried Skip can be answered — but every entry has
    already reached a terminal outcome and its allowance has been accounted for and closed. If
    an upload could still be pushed through one, the grace window would hand back capacity the
    caps had already reclaimed, and land bytes for a drop the operator was told had finished.
    """
    (root / "a.txt").write_bytes(b"EXISTING")
    bid = _batch(client, auth_cfg, ["a.txt"])
    assert _post(client, auth_cfg, root, "a.txt", b"NEW", batch_id=bid).status_code == 409
    assert _skip(client, auth_cfg, bid, "a.txt").status_code == 200

    # The retried Skip still replays …
    assert _skip(client, auth_cfg, bid, "a.txt").status_code == 200
    # … but a WRITE against the same lingering batch does not.
    late = _post(client, auth_cfg, root, "a.txt", b"SNEAKY", batch_id=bid, on_collision="replace")
    assert late.status_code == 409, f"a settled batch still accepted a write: {late.text}"
    assert (root / "a.txt").read_bytes() == b"EXISTING", "the sneaked write landed"


def test_lingering_settled_batches_are_themselves_bounded(client, root, auth_cfg):
    """Exempting settled batches from the live caps must not become an unbounded registry.

    They are excluded from both ceilings so they cannot lock a session out — which means nothing
    else bounds them, and a create-and-settle loop would grow `_batches` without limit inside one
    120s grace window. The exemption needs its own bound or it is a hole in the ceiling it was
    carved out of.
    """
    filewrite.reset_batches_for_test()
    for i in range(filewrite.MAX_SETTLED_LINGER + 40):
        made = filewrite.create_batch([{"relpath": "f.txt", "size": 4}], owner=f"sess-{i}")
        filewrite.get_batch(made["batch_id"], owner=f"sess-{i}").finish_file("f.txt", "landed")
    filewrite.create_batch([{"relpath": "z.txt", "size": 4}], owner="last")
    lingering = sum(1 for b in filewrite._batches.values() if b.settled)
    assert lingering <= filewrite.MAX_SETTLED_LINGER, f"{lingering} settled records lingering"


def test_skip_requires_a_relpath_at_all(client, root, auth_cfg):
    bid = _batch(client, auth_cfg, ["a.txt"])
    r = client.post(
        "/api/files/upload/skip",
        json={"batch_id": bid},
        headers=_hdr(client, auth_cfg),
    )
    assert r.status_code == 422, r.text


def test_a_refused_upload_never_removes_a_folder_it_did_not_create(client, root, auth_cfg):
    """The rollback respects ownership again. A pre-existing `refs/heads` must survive a refusal
    that merely traversed it — removing it was destroying the operator's own directory."""
    d = root / "proj"
    (d / "refs" / "heads").mkdir(parents=True)
    (d / "HEAD").write_text("ref: refs/heads/main\n")
    (d / "objects").mkdir()
    r = _post(client, auth_cfg, d, "refs/heads/main", b"0" * 40)
    assert r.status_code == 403, r.text
    assert (d / "refs" / "heads").exists(), "a refusal deleted a pre-existing directory"


def test_an_ordinary_failed_upload_does_not_delete_a_folder_it_found(client, root, auth_cfg):
    """Aggressive cleanup is for the hostile-shape path ONLY. A plain failure must not remove a
    directory that was already there — that would be destroying the operator's own empty folder."""
    d = root / "proj"
    (d / "existing").mkdir(parents=True)
    dest = filewrite.open_destination(str(d), "existing/file.txt", "fail")
    dest.abandon()  # an ordinary failure, not a shape refusal
    assert (d / "existing").exists(), "an ordinary rollback removed a pre-existing folder"


def test_a_collision_keeps_its_batch_alive_for_the_operators_choice(client, root, auth_cfg):
    """A 409 used to settle the manifest entry, so a one-file batch retired, `_reap` deleted it,
    and the Keep both / Replace retry came back "unknown or expired" — the collision flow did not
    work at all end to end."""
    (root / "a.txt").write_bytes(b"original\n")
    r = client.post(
        "/api/files/upload/batch",
        json={"files": [{"relpath": "a.txt", "size": 6}]},
        headers=_hdr(client, auth_cfg),
    )
    bid = r.json()["batch_id"]
    first = _post(client, auth_cfg, root, "a.txt", b"second", batch_id=bid)
    assert first.status_code == 409, first.text
    # The operator picks "keep both" and the client retries the SAME batch.
    retry = _post(
        client, auth_cfg, root, "a.txt", b"second", batch_id=bid, on_collision="keep_both"
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["name"] == "a (2).txt"
    assert (root / "a.txt").read_bytes() == b"original\n"


def test_owner_rotation_cannot_grow_the_batch_registry_without_bound():
    """The per-owner cap is defeated by rotating identities — a probe admitted 800 live batches
    across 100 owners. A process-wide ceiling is what fails closed."""
    made = 0
    with pytest.raises(filewrite.BatchError) as e:
        for i in range(filewrite.MAX_LIVE_BATCHES_TOTAL + 50):
            filewrite.create_batch([{"relpath": f"f{i}", "size": 1}], owner=f"owner-{i}")
            made += 1
    assert "too many uploads" in str(e.value)
    assert made <= filewrite.MAX_LIVE_BATCHES_TOTAL


def test_replace_refuses_a_file_too_large_to_identify(client, root, auth_cfg, monkeypatch):
    """Identity has to cover the WHOLE file: hashing a prefix and trusting size+mtime for the tail
    let an in-place rewrite past the prefix through untouched. Beyond the ceiling the panel
    refuses rather than approximating."""
    monkeypatch.setattr(filewrite, "_REPLACE_MAX", 8)
    (root / "big.bin").write_bytes(b"0123456789")  # 10 bytes > the patched ceiling
    r = _post(client, auth_cfg, root, "big.bin", b"new", on_collision="replace")
    assert r.status_code == 413, r.text
    assert (root / "big.bin").read_bytes() == b"0123456789", "the oversized target was replaced"
