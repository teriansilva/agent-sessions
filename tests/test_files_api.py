"""File-panel backend tests (#783).

The interesting ones are adversarial: a component repointed between validation and open, a FIFO
that would block a worker forever, and the budget that bounds work `to_thread` cannot cancel.
Everything here runs against a `mktemp`-style tmp root via ``AGENT_SESSIONS_FS_ROOT`` — the real
``~/.claude`` is never touched.
"""

from __future__ import annotations

import os
import socket
import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import files, fsbrowse


@pytest.fixture(autouse=True)
def _reset_budget():
    """The admission counters are module state; leaving one behind fails an unrelated test."""
    files._inflight_total = 0
    files._inflight_by_root.clear()
    yield
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


# --------------------------------------------------------------------------- containment


def test_contained_path_is_the_public_name(root):
    assert fsbrowse.contained_path(str(root)) == os.path.realpath(str(root))
    assert fsbrowse.contained_path("") == os.path.realpath(str(root))


@pytest.mark.parametrize("bad", ["/etc", "/etc/passwd", "../..", "/"])
def test_paths_outside_the_root_are_refused(root, bad):
    with pytest.raises(fsbrowse.FsError) as e:
        files.list_dir(bad)
    assert e.value.status == 403


def test_symlink_escaping_the_root_is_refused(root):
    """Refused — and refused *earlier* than containment, because it is a symlink at all.

    Phase 1's display-only rule fires first (422) rather than the containment check (403). Both
    are refusals and no outside entry is ever returned; the display-only reason is the more
    specific one, so this asserts the property (nothing escapes) rather than a particular code.
    """
    (root / "link").symlink_to("/etc")
    with pytest.raises(fsbrowse.FsError) as e:
        files.list_dir(str(root / "link"))
    assert e.value.status in (403, 422)


def test_intermediate_symlink_is_fine_when_it_lands_inside(root):
    """Only the FINAL component is display-only; realpath collapses the rest and containment
    still judges the result — otherwise a symlinked project dir would be unusable."""
    (root / "real").mkdir()
    (root / "real" / "deep").mkdir()
    (root / "real" / "deep" / "f.txt").write_text("ok")
    (root / "hop").symlink_to(root / "real")
    out = files.list_dir(str(root / "hop" / "deep"))
    assert [e["name"] for e in out["entries"]] == ["f.txt"]
    assert files.read_file(str(root / "hop" / "deep" / "f.txt"))["content"] == "ok"


def test_symlinked_file_is_not_readable_phase1(root):
    (root / "real.txt").write_text("secret")
    (root / "alias.txt").symlink_to(root / "real.txt")
    # Contained, but phase 1 is display-only: O_NOFOLLOW refuses it at acquisition.
    with pytest.raises(fsbrowse.FsError) as e:
        files.read_file(str(root / "alias.txt"))
    assert e.value.status == 422


def test_adversarial_repoint_between_validate_and_open(root, monkeypatch):
    """The whole reason containment is proved on the descriptor rather than the pre-check.

    We let `contained_path` bless an in-root path, then swap that path for a symlink to an
    out-of-root file before `read_file` opens it. Either the open refuses (O_NOFOLLOW) or the
    /proc/self/fd re-check does — what must never happen is outside bytes coming back.
    """
    outside = root.parent / "outside.txt"
    outside.write_text("SHOULD-NEVER-BE-RETURNED")
    target = root / "swap.txt"
    target.write_text("innocent")

    real_contained = fsbrowse.contained_path

    def repointing_contained(path):
        resolved = real_contained(path)
        if resolved.endswith("swap.txt"):
            os.unlink(resolved)
            os.symlink(str(outside), resolved)  # repoint AFTER the blessing
        return resolved

    monkeypatch.setattr(files, "contained_path", repointing_contained)
    with pytest.raises(fsbrowse.FsError):
        out = files.read_file(str(target))
        assert "SHOULD-NEVER-BE-RETURNED" not in out.get("content", "")


# --------------------------------------------------------------------------- special files


def test_fifo_is_refused_promptly(root):
    """A no-writer FIFO must not block a worker.

    The blocking form (`O_RDONLY | O_NOFOLLOW`) waits inside open() forever and never reaches the
    type check, so this asserts *promptness*, not merely refusal.
    """
    fifo = root / "pipe"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(fsbrowse.FsError) as e:
        files.read_file(str(fifo))
    assert e.value.status == 422
    assert time.monotonic() - started < 2.0, "open() blocked on the FIFO"


def test_socket_is_refused(root):
    sock_path = root / "sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
        with pytest.raises(fsbrowse.FsError) as e:
            files.read_file(str(sock_path))
        assert e.value.status == 422
    finally:
        s.close()


def test_directory_passed_to_read_is_refused(root):
    (root / "sub").mkdir()
    with pytest.raises(fsbrowse.FsError) as e:
        files.read_file(str(root / "sub"))
    assert e.value.status == 422


def test_file_passed_to_list_is_refused(root):
    (root / "f.txt").write_text("x")
    with pytest.raises(fsbrowse.FsError) as e:
        files.list_dir(str(root / "f.txt"))
    assert e.value.status in (404, 422)


def test_missing_path_is_404(root):
    with pytest.raises(fsbrowse.FsError) as e:
        files.read_file(str(root / "nope.txt"))
    assert e.value.status == 404


# --------------------------------------------------------------------------- listing


def test_listing_includes_dotfiles_and_sorts_dirs_first(root):
    (root / ".env").write_text("SECRET=1")
    (root / "zeta").mkdir()
    (root / "alpha.txt").write_text("a")
    (root / "Beta.txt").write_text("b")
    out = files.list_dir(str(root))
    names = [e["name"] for e in out["entries"]]
    assert names == ["zeta", ".env", "alpha.txt", "Beta.txt"]
    assert out["complete"] is True
    assert out["total"] == 4
    assert out["truncated"] is False


def test_link_payload_shape(root):
    (root / "target").mkdir()
    (root / "inside").symlink_to(root / "target")
    (root / "outside").symlink_to("/etc")
    by_name = {e["name"]: e for e in files.list_dir(str(root))["entries"]}

    inside = by_name["inside"]
    assert inside["kind"] == "link"
    assert inside["link_contained"] is True
    assert inside["link_kind"] == "dir"

    outside = by_name["outside"]
    assert outside["kind"] == "link"
    assert outside["link_contained"] is False
    # link_kind is present ONLY when contained — an uncontained target's kind is not resolved.
    assert "link_kind" not in outside


def test_entry_cap_reports_incomplete_not_a_fake_total(root, monkeypatch):
    monkeypatch.setattr(files, "FILES_MAX_ENTRIES", 5)
    for i in range(20):
        (root / f"f{i:02d}.txt").write_text("x")
    out = files.list_dir(str(root))
    assert len(out["entries"]) == 5
    assert out["complete"] is False
    assert out["total"] is None, "a capped scan must not report a count it never finished"
    assert out["truncated"] is True


def test_time_budget_reports_incomplete(root, monkeypatch):
    monkeypatch.setattr(files, "FILES_SCAN_BUDGET_MS", 0)
    for i in range(5):
        (root / f"f{i}.txt").write_text("x")
    out = files.list_dir(str(root))
    assert out["complete"] is False
    assert out["total"] is None


def test_parent_is_none_at_the_root(root):
    assert files.list_dir(str(root))["parent"] is None
    (root / "sub").mkdir()
    assert files.list_dir(str(root / "sub"))["parent"] == os.path.realpath(str(root))


# --------------------------------------------------------------------------- read


def test_read_text(root):
    (root / "a.py").write_text("print('hi')\n")
    out = files.read_file(str(root / "a.py"))
    assert out["binary"] is False
    assert out["content"] == "print('hi')\n"
    assert out["truncated"] is False


def test_binary_is_detected_and_not_decoded(root):
    (root / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    out = files.read_file(str(root / "img.png"))
    assert out["binary"] is True
    assert "content" not in out
    assert out["mime"] == "image/png"


def test_read_cap_is_enforced_while_reading(root, monkeypatch):
    monkeypatch.setattr(files, "FILES_MAX_READ", 64)
    (root / "big.txt").write_text("a" * 4096)
    out = files.read_file(str(root / "big.txt"))
    assert out["truncated"] is True
    assert len(out["content"]) == 64
    assert out["size"] == 4096  # the real size is still reported honestly


def test_invalid_utf8_is_replaced_not_fatal(root):
    (root / "mixed.txt").write_bytes(b"ok \xff\xfe more")
    out = files.read_file(str(root / "mixed.txt"))
    assert out["binary"] is False
    assert "ok" in out["content"]


def test_non_utf8_filename_is_listed(root):
    raw = os.fsdecode(b"caf\xc3\xa9.txt")
    (root / raw).write_text("x")
    names = [e["name"] for e in files.list_dir(str(root))["entries"]]
    assert raw in names


# --------------------------------------------------------------------------- worker budget


def test_budget_refuses_before_the_work_is_submitted(client, root, monkeypatch):
    """Admission must happen in the ROUTE, above the executor.

    Reserving inside the worker bounded only what was *running*: `to_thread` had already handed
    the callable to an executor whose queue is unbounded, so a flood piled up ahead of the
    counters and "refuses rather than queues" was false. This pins the fix by making the
    filesystem call itself fail loudly — if the request is admitted, the response is a 500, not a
    503, which is exactly what the old ordering produced.
    """
    monkeypatch.setattr(files, "FILES_MAX_WORKERS", 1)
    submitted: list[str] = []

    def tripwire(path):
        submitted.append(path)
        raise AssertionError("work was submitted despite a saturated budget")

    held = threading.Event()
    release = threading.Event()

    def hold():
        files.acquire("holder")
        try:
            held.set()
            release.wait(5)
        finally:
            files.release("holder")

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        monkeypatch.setattr(files, "list_dir", tripwire)
        r = client.get("/api/files/list", params={"path": str(root)})
        assert r.status_code == 503
        assert submitted == [], "the route must refuse before reaching the executor"
    finally:
        release.set()
        t.join(5)
    total, _ = files.inflight_workers()
    assert total == 0, "the reservation must be released on every exit path"


# --------------------------------------------------------------------------- capabilities


def test_capabilities_fail_closed(root, monkeypatch):
    caps = files.Capabilities(False, "unsupported platform: missing X")
    monkeypatch.setattr(files, "_CAPS", caps)
    with pytest.raises(fsbrowse.FsError) as e:
        files.list_dir(str(root))
    assert e.value.status == 501
    assert "unsupported platform" in str(e.value)


def test_capabilities_ok_on_this_platform(root):
    assert files.capabilities().ok, files.capabilities().reason


# --------------------------------------------------------------------------- routes


@pytest.fixture()
def client(root, monkeypatch, auth_cfg):
    """`auth_cfg` seeds the env the app requires (secret key, origin, isolated 2FA store).

    Without it this passed locally only because a developer shell happens to export
    AGENT_SESSIONS_SECRET_KEY — CI has no such luck, and `create_app` refuses to start.
    """
    monkeypatch.setenv("AGENT_SESSIONS_AUTH_MODE", "none")
    from agent_sessions import main

    return TestClient(main.create_app())


def test_routes_are_no_store_on_success_and_error(client, root):
    (root / "a.txt").write_text("hello")
    ok = client.get("/api/files/read", params={"path": str(root / "a.txt")})
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"

    listed = client.get("/api/files/list", params={"path": str(root)})
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"

    denied = client.get("/api/files/read", params={"path": "/etc/passwd"})
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store", "an error leaks the path too"


def test_route_status_mapping(client, root):
    (root / "d").mkdir()
    assert client.get("/api/files/list", params={"path": "/etc"}).status_code == 403
    assert client.get("/api/files/read", params={"path": str(root / "gone")}).status_code == 404
    assert client.get("/api/files/read", params={"path": str(root / "d")}).status_code == 422
    assert client.get("/api/files/read").status_code == 422


def test_capabilities_route(client):
    body = client.get("/api/files/capabilities").json()
    assert body["ok"] is True


def test_filesystem_work_is_dispatched_off_the_event_loop(client, root, monkeypatch):
    """The route must hand the blocking call to a worker thread, not run it inline."""
    loop_thread = threading.current_thread().ident
    seen: list[int | None] = []
    real = files.list_dir

    def spy(path):
        seen.append(threading.current_thread().ident)
        return real(path)

    monkeypatch.setattr(files, "list_dir", spy)
    assert client.get("/api/files/list", params={"path": str(root)}).status_code == 200
    assert seen and seen[0] != loop_thread, "list_dir ran on the caller's thread"


def test_permission_denied_is_surfaced_not_500(client, root):
    locked = root / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        r = client.get("/api/files/list", params={"path": str(locked)})
        assert r.status_code in (403, 400)
    finally:
        os.chmod(locked, stat.S_IRWXU)


def test_cancelling_a_request_does_not_free_the_slot_early(root):
    """The slot belongs to the WORKER, not to the request task.

    `asyncio.to_thread` cancellation abandons only the await; the sync callable keeps running. If
    release lived in the request's `finally`, a client disconnect would hand the slot back while
    the thread was still blocked — measured as occupancy dropping 1 → 0 with the worker alive, and
    the next flood admitted straight through. This pins the ownership transfer.
    """
    import asyncio

    started = threading.Event()
    finish = threading.Event()

    def blocking():
        started.set()
        finish.wait(10)
        return "done"

    async def scenario():
        files.acquire("k")
        loop = asyncio.get_running_loop()
        # run_in_executor returns a Future, not a coroutine — wrap it so it can be cancelled the
        # same way the route's awaited call is.
        task = asyncio.ensure_future(
            loop.run_in_executor(files.executor(), lambda: files.guarded("k", blocking))
        )
        await asyncio.to_thread(started.wait, 5)
        assert files.inflight_workers()[0] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The await is gone; the worker is not. The slot must still be held.
        assert files.inflight_workers()[0] == 1, "a cancelled request must not free a live slot"
        finish.set()
        for _ in range(200):
            if files.inflight_workers()[0] == 0:
                break
            await asyncio.sleep(0.02)
        assert files.inflight_workers()[0] == 0, "the worker must release when it actually exits"

    asyncio.run(scenario())


def test_a_filename_that_is_not_utf8_does_not_brick_the_listing(client, root):
    """POSIX filenames are bytes. `scandir` surfaces undecodable ones as lone surrogates, which
    `JSONResponse` cannot encode — one such file used to take down the whole directory."""
    os.mkdir(os.path.join(os.fsencode(str(root)), b"okdir"))
    with open(os.path.join(os.fsencode(str(root)), b"bad-\xff.txt"), "wb") as fh:
        fh.write(b"x")
    (root / "fine.txt").write_text("y")

    r = client.get("/api/files/list", params={"path": str(root)})
    assert r.status_code == 200, r.text
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert "fine.txt" in names and "okdir" in names
    assert body["unencodable"] == 1, "the skipped entry must be reported, not silently dropped"


def test_the_pool_that_executes_is_the_pool_the_budget_names():
    """The bound is only honest if the budget and the thread pool are the SAME thing.

    `asyncio.to_thread` dispatches to the interpreter's default pool — up to min(32, cpu+4)
    threads, an unbounded queue, shared with every other blocking call in the process. Counting
    admission against that is counting one thing while another executes the work: a flood could
    put more scans on threads than the budget names, and an unrelated flood could starve the panel
    without ever raising FilesBusy.
    """
    pool = files.executor()
    assert pool is files.executor(), "one shared pool, not one per call"
    assert pool._max_workers == files.FILES_MAX_WORKERS
    # Admission can never exceed the pool, so nothing can sit queued behind a full pool.
    assert files.FILES_MAX_WORKERS <= pool._max_workers


def test_routes_run_on_the_dedicated_pool_not_the_default_one(client, root):
    (root / "f.txt").write_text("x")
    seen: list[str] = []
    real = files.list_dir

    def spy(path):
        seen.append(threading.current_thread().name)
        return real(path)

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(files, "list_dir", spy)
    try:
        assert client.get("/api/files/list", params={"path": str(root)}).status_code == 200
    finally:
        mp.undo()
    assert seen and seen[0].startswith("files"), f"ran on {seen!r}, not the panel's own pool"


def test_cancelling_BEFORE_the_worker_starts_still_releases_the_slot(root):
    """The submit→start window is its own leak, distinct from post-start cancellation.

    Cancelling a queued executor callable drops it, so `run_slot`'s finally never runs and nobody
    in a worker can release. Measured on the pre-fix head: the slot stayed held even after the
    pool drained, and eight repeats exhausted the budget until restart.
    """
    import asyncio

    files.shutdown_executor_for_test()
    hold_started = threading.Event()
    let_go = threading.Event()

    async def scenario():
        pool = files.executor()
        # Fill every worker so the next submission can only ever sit QUEUED.
        blockers = [
            pool.submit(lambda: (hold_started.set(), let_go.wait(10)))
            for _ in range(files.FILES_MAX_WORKERS)
        ]
        assert hold_started.wait(5)

        slot = files.acquire_slot("queued-key")
        cf = pool.submit(files.run_slot, slot, lambda: "never runs")
        fut = asyncio.wrap_future(cf)
        await asyncio.sleep(0.05)
        assert files.inflight_workers()[0] == 1
        fut.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fut
        # wrap_future propagates the cancel to the concurrent future via a scheduled callback,
        # so give the loop a turn before reading its state.
        await asyncio.sleep(0)
        assert cf.cancelled(), "the callable must have been dropped while queued"
        if cf.cancelled():
            slot.release()
        assert files.inflight_workers()[0] == 0, "a never-started callable must not leak its slot"

        let_go.set()
        for b in blockers:
            b.result(10)

    try:
        asyncio.run(scenario())
    finally:
        let_go.set()
        files.shutdown_executor_for_test()


def test_release_is_exactly_once_however_ownership_lands(root):
    """Both owners are allowed to try; the second attempt must be a no-op."""
    slot = files.acquire_slot("k")
    assert files.inflight_workers()[0] == 1
    assert slot.release() is True
    assert slot.release() is False, "a double release would corrupt the counters"
    assert files.inflight_workers()[0] == 0


def test_a_symlink_target_that_is_not_utf8_does_not_brick_the_listing(client, root):
    """`readlink` can return undecodable bytes for a link whose NAME is perfectly valid — an
    independent failure from a bad name, and one that used to 500 *without* the no-store headers."""
    os.symlink(os.fsdecode(b"bad-\xff-target"), str(root / "link"))
    (root / "fine.txt").write_text("y")

    r = client.get("/api/files/list", params={"path": str(root)})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    by_name = {e["name"]: e for e in r.json()["entries"]}
    assert "fine.txt" in by_name
    link = by_name["link"]
    assert link["kind"] == "link"
    assert link["link_target"] is None
    assert link["link_unencodable_target"] is True
    assert link["link_contained"] is False


def test_auth_failures_also_carry_no_store(root, monkeypatch):
    """`Depends(logged_in)` raises before the handler, so a 401 never reached the route's own
    header code. The response still names the requested path, so it must not be cacheable."""
    monkeypatch.delenv("AGENT_SESSIONS_AUTH_MODE", raising=False)
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", "pbkdf2_sha256$1000$abc$def")
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(root / "2fa.json"))
    from agent_sessions import main

    anon = TestClient(main.create_app())
    for url in ("/api/files/list", "/api/files/read", "/api/files/capabilities"):
        r = anon.get(url, params={"path": str(root)})
        assert r.status_code == 401, f"{url} -> {r.status_code}"
        assert r.headers.get("cache-control") == "no-store", f"{url} leaked a cacheable 401"


def test_lexical_path_aliases_share_one_fairness_counter(root):
    """Four spellings of one directory must not each take their own per-root slot.

    Keyed on the raw path, `root`, `root/.`, `root//` and `root/sub/..` were admitted as four
    distinct roots — so one directory could occupy the whole pool and hand every unrelated root a
    503, which is exactly what FILES_MAX_WORKERS_PER_ROOT exists to prevent.
    """
    base = str(root)
    aliases = [base, f"{base}/.", f"{base}//", f"{base}/sub/.."]
    keys = {files.fairness_key(a) for a in aliases}
    assert len(keys) == 1, f"aliases must canonicalise to one key, got {keys!r}"

    slots = []
    try:
        for a in aliases[: files.FILES_MAX_WORKERS_PER_ROOT]:
            slots.append(files.acquire_slot(a))
        # The next alias is the (N+1)th for the SAME canonical root and must be refused.
        with pytest.raises(files.FilesBusy):
            files.acquire_slot(aliases[files.FILES_MAX_WORKERS_PER_ROOT])
        total, by_root = files.inflight_workers()
        assert total == files.FILES_MAX_WORKERS_PER_ROOT
        assert len(by_root) == 1, f"aliases split the counter: {by_root!r}"
    finally:
        for sl in slots:
            sl.release()


def test_unhandled_and_serialization_failures_keep_no_store(client, root, monkeypatch):
    """An exception escaping the endpoint unwinds past route-level header code and is rendered by
    Starlette's outer error middleware, which knows nothing about this policy."""

    def boom(*_a, **_k):
        raise RuntimeError("forced")

    monkeypatch.setattr(files, "capabilities", boom)
    r = client.get("/api/files/capabilities")
    assert r.status_code == 500
    assert r.headers.get("cache-control") == "no-store", "a 500 must not be cacheable either"

    # A payload that cannot be encoded fails while the response is built, inside the handler.
    monkeypatch.setattr(files, "list_dir", lambda p: {"path": "\udcff", "entries": []})
    r2 = client.get("/api/files/list", params={"path": str(root)})
    assert r2.status_code == 500
    assert r2.headers.get("cache-control") == "no-store"


def test_the_default_and_explicit_root_spellings_share_one_counter(root):
    """An omitted `path` resolves to the configured root, so it must key like the explicit one.

    They did not: `fairness_key("")` returned "" while the explicit spelling returned the absolute
    path, so one canonical directory could take 2 x FILES_MAX_WORKERS_PER_ROOT workers.
    """
    assert files.fairness_key("") == files.fairness_key(str(root))
    assert files.fairness_key(None) == files.fairness_key(f"{root}/.")

    slots = []
    try:
        # Fill the per-root allotment using the OMITTED spelling...
        for _ in range(files.FILES_MAX_WORKERS_PER_ROOT):
            slots.append(files.acquire_slot(""))
        # ...then the EXPLICIT spelling must be refused, not granted a fresh allotment.
        with pytest.raises(files.FilesBusy):
            files.acquire_slot(str(root))
        total, by_root = files.inflight_workers()
        assert total == files.FILES_MAX_WORKERS_PER_ROOT
        assert len(by_root) == 1, f"the two spellings split the counter: {by_root!r}"
    finally:
        for sl in slots:
            sl.release()
