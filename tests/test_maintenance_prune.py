"""#993 increment 1 — Settings → Maintenance cache prune, and the runner that owns every job.

What is pinned here is the SAFETY of each category rather than its bookkeeping:

* a stale socket is removed only through the single-writer-lock guard, so a socket whose lock is
  held (a new master generation) is never unlinked, and a name that does not round-trip through
  ``ptybridge.socket_path`` is never touched;
* a category whose contents could not be READ is reported unknown (``error``) rather than zero, so
  a confirmation can never omit contents the prune would still delete, and the prune names the
  discovery failure instead of reporting a clean empty sweep;
* the runner's single-flight slot belongs to the JOB, so a request that goes away (a client
  disconnect cancels the request coroutine) cannot free it while the job is still mutating.

Real unix sockets and real ``flock``s are used — both are cheap and the guard is the kernel's —
under short private dirs (a unix socket path must fit in 108 bytes, which a pytest tmp_path does
not reliably do). No process is ever signalled.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import maintenance, ptybridge, sessionlock
from agent_sessions.main import create_app

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def runtime(monkeypatch):
    """A short private runtime dir for dtach sockets (AF_UNIX paths are length-limited)."""
    d = Path(tempfile.mkdtemp(prefix="asp-", dir="/tmp"))
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(d))
    yield ptybridge.runtime_dir()
    shutil.rmtree(d, ignore_errors=True)


def _dead_socket(path: Path) -> Path:
    """A socket FILE with nobody listening — exactly what a hard-killed master leaves behind."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    return path


# ---- stale_sockets ------------------------------------------------------------------------


def test_a_dead_socket_is_counted_by_the_dry_run_and_removed_by_prune(runtime):
    sock = _dead_socket(ptybridge.socket_path("claude", UUID_A))
    dry = maintenance.dry_run_caches()
    assert dry["stale_sockets"]["items"] == 1
    out = maintenance.prune_caches(["stale_sockets"])
    assert out["removed"] == 1
    assert out["failed"] == []
    assert not sock.exists()


def test_a_socket_whose_lock_is_held_is_never_unlinked(runtime):
    """The 2026-06-12 wedge: a held lock means a NEW master generation owns the path."""
    sock = _dead_socket(ptybridge.socket_path("claude", UUID_B))
    held = sessionlock.acquire(f"claude:{UUID_B}")
    assert held is not None
    try:
        out = maintenance.prune_caches(["stale_sockets"])
    finally:
        held.release()
    assert out["removed"] == 0
    assert sock.exists()
    assert [s["category"] for s in out["skipped"]] == ["stale_sockets"]
    assert "lock" in out["skipped"][0]["reason"]


def test_a_live_master_socket_is_not_a_candidate(runtime):
    path = ptybridge.socket_path("claude", UUID_C)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    try:
        assert maintenance.dry_run_caches()["stale_sockets"]["items"] == 0
        assert maintenance.prune_caches(["stale_sockets"])["removed"] == 0
        assert path.exists()
    finally:
        srv.close()


def test_names_that_do_not_round_trip_are_left_alone(runtime):
    """A sanitised or foreign name cannot be mapped back to ONE session key, so its lock cannot
    be the guard — leave it rather than guess."""
    odd = runtime / "claude-not a uuid.sock"
    odd.write_text("")
    foreign = _dead_socket(runtime / f"nosuchengine-{UUID_A}.sock")
    other = runtime / "README"
    other.write_text("")
    assert maintenance.dry_run_caches()["stale_sockets"]["items"] == 0
    maintenance.prune_caches(["stale_sockets"])
    assert odd.exists() and foreign.exists() and other.exists()


# ---- hooks_void_dirs: DEFERRED (Hermes on PR #1000) ----------------------------------------


def test_the_hooks_void_category_is_not_offered():
    """Pruning a peer instance's empty hooks dir frees its NAME in a shared temp dir, where a
    local user can plant a replacement the peer's `hooks_void()` (an `isdir` check) would hand to
    git as `core.hooksPath`. The category stays out until dirs carry cross-process lifetime
    ownership; the backlog is left alone rather than pruned unsafely."""
    assert "hooks_void_dirs" not in maintenance.CATEGORIES
    with pytest.raises(ValueError):
        maintenance.prune_caches(["hooks_void_dirs"])
    assert "hooks_void_dirs" not in maintenance.dry_run_caches()


# ---- archived_scrollback -------------------------------------------------------------------


def test_a_scrollback_delete_failure_is_reported_not_counted_as_freed(monkeypatch):
    """`clear_scrollback` is best-effort and used to count a mirror's bytes BEFORE unlinking it,
    so a refused delete came back as `removed=0, bytes_freed=1024, failed=[]` — freed bytes for a
    file still on disk. The prune contract needs failures to be distinguishable."""
    from agent_sessions import scrollback

    key = "claude:44444444-4444-4444-4444-444444444444"
    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(key)
    mirror.write_bytes(b"x" * 1024)
    monkeypatch.setattr(scrollback, "archived_keys_checked", lambda: ([(key, key)], []))

    real_unlink = Path.unlink

    def refuse(self, *a, **k):
        if self.suffix == ".scrollback":
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refuse)
    out = maintenance.prune_caches(["archived_scrollback"])

    assert out["removed"] == 0
    assert out["bytes_freed"] == 0
    assert [f["category"] for f in out["failed"]] == ["archived_scrollback"]
    assert "PermissionError" in out["failed"][0]["reason"]
    assert mirror.exists()  # nothing was actually freed


def test_a_scrollback_delete_that_lands_counts_its_bytes(monkeypatch):
    from agent_sessions import scrollback

    key = "claude:55555555-5555-5555-5555-555555555555"
    scrollback._ensure_scrollback_dir()
    scrollback._scrollback_path(key).write_bytes(b"y" * 2048)
    monkeypatch.setattr(scrollback, "archived_keys_checked", lambda: ([(key, key)], []))

    out = maintenance.prune_caches(["archived_scrollback"])
    assert out["removed"] == 1
    assert out["bytes_freed"] == 2048
    assert out["failed"] == []
    assert not scrollback._scrollback_path(key).exists()


# ---- unreadable contents are UNKNOWN, never zero (Hermes on PR #1000) ----------------------


def test_an_unreadable_runtime_dir_is_unknown_not_an_empty_category(runtime):
    """A read failure is not an absence. Swallowing the enumeration error reported
    ``{items: 0, bytes: 0}`` — a *successful* measurement of nothing — so the confirmation showed
    a total that omitted whatever the prune would still find and delete."""
    _dead_socket(ptybridge.socket_path("claude", UUID_A))
    runtime.chmod(0o000)
    try:
        dry = maintenance.dry_run_caches()
        out = maintenance.prune_caches(["stale_sockets"])
    finally:
        runtime.chmod(0o700)

    assert dry["stale_sockets"].get("error")  # never a clean {"items": 0, "bytes": 0}
    assert out["removed"] == 0
    assert [f["category"] for f in out["failed"]] == ["stale_sockets"]
    assert out["failed_total"] == 1


def test_an_unreadable_mirror_makes_the_whole_measure_unknown(monkeypatch):
    """Two real mirrors, one whose ``stat()`` is refused during the preview only. The preview used
    to drop the unreadable one and report ``items=1``; the prune then removed BOTH. A partial
    measurement is not a count, so the category is unknown rather than partially right."""
    from agent_sessions import scrollback

    readable = "claude:66666666-6666-6666-6666-666666666666"
    blocked_key = "claude:77777777-7777-7777-7777-777777777777"
    scrollback._ensure_scrollback_dir()
    scrollback._scrollback_path(readable).write_bytes(b"r" * 1024)
    blocked = scrollback._scrollback_path(blocked_key)
    blocked.write_bytes(b"b" * 1024)
    monkeypatch.setattr(
        scrollback,
        "archived_keys_checked",
        lambda: ([(readable, readable), (blocked_key, blocked_key)], []),
    )

    real_stat = Path.stat
    refusing = {"on": True}

    def refuse(self, *a, **k):
        if refusing["on"] and self == blocked:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", refuse)
    dry = maintenance.dry_run_caches()
    refusing["on"] = False  # the I/O recovers before the prune, as in the reproduction
    out = maintenance.prune_caches(["archived_scrollback"])

    assert dry["archived_scrollback"].get("error")
    assert dry["archived_scrollback"]["items"] == 0  # never the partial count of 1
    assert out["removed"] == 2  # exactly what a partial confirmation would have understated


def test_failed_provider_discovery_is_surfaced_not_read_as_an_empty_sweep(
    monkeypatch, tmp_path, fake_jsonl
):
    """A corrupt engine store is not an empty one. Provider scans are fail-soft by design (a
    broken ``opencode.db`` must never blank the Claude rows), so a maintenance measurement built
    on one reported "nothing archived" for a store it could not open, and the prune then claimed
    a clean sweep."""
    from agent_sessions import scrollback
    from agent_sessions.engines import opencode as oc

    db = tmp_path / "opencode.db"
    db.write_bytes(b"NOT A SQLITE DATABASE " * 64)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    monkeypatch.setattr(oc.OpenCodeProvider, "is_present", lambda self: True)

    _keys, problems = scrollback.archived_keys_checked()
    assert [p for p in problems if p.startswith("opencode:")]

    dry = maintenance.dry_run_caches()
    assert dry["archived_scrollback"].get("error")

    out = maintenance.prune_caches(["archived_scrollback"])
    assert any(
        f["category"] == "archived_scrollback" and "opencode" in f["reason"] for f in out["failed"]
    )


def test_archived_discovery_takes_rows_and_evidence_from_one_call(monkeypatch):
    """Discovery resolves through ``engines.scan_all_checked`` — ONE call answering both "what is
    there" and "was that complete".

    An earlier round of this PR asked a separate health probe beside a fail-soft listing, to keep
    the ``engines.scan_all`` monkeypatch seam intact. That split is the defect: a store that fails
    the first read and recovers before the second reports empty-with-no-problems, so the rows and
    their completeness describe different moments (Hermes on PR #1000, review 4894). The seam now
    lives on the checked call itself, which is what maintenance patches.
    """
    from agent_sessions import engines, scrollback
    from agent_sessions.scanner import Session

    uuid = "88888888-8888-8888-8888-888888888888"
    fake = Session(
        engine="claude",
        uuid=uuid,
        cwd="/tmp/not-a-real-project",
        last_mtime=0.0,
        first_user_message="",
        archived=True,
    )
    calls = {"n": 0}

    def only_the_fake() -> tuple[list[Session], list[str]]:
        calls["n"] += 1
        return [fake], []

    monkeypatch.setattr(engines, "scan_all_checked", only_the_fake)
    keys, problems = scrollback.archived_keys_checked()

    assert calls["n"] == 1  # one pass decided both answers — no second, separate probe
    # BOTH identities travel to the fence; unaliased, the logical and physical keys coincide.
    assert keys == [(f"claude:{uuid}", f"claude:{uuid}")]
    assert problems == []


def test_an_unreadable_sidecar_never_makes_an_active_session_deletable(monkeypatch, tmp_path):
    """The P1 of review 4894 — a DATA-LOSS path, not a reporting one.

    ``OpenCodeProvider.unarchive`` records an explicit ``archived: false`` override and
    deliberately leaves the native DB flag alone (opencode.db is read-only to us), so for an
    unarchived opencode session the SIDECAR is the only thing that says it is active. ``load()``
    maps a failed read to ``{}``, which silently reverts that session to the engine's "archived"
    and makes its scrollback prune-eligible. A destructive boundary has to fail CLOSED when it
    cannot see.
    """
    from agent_sessions import engines, metadata, scrollback
    from agent_sessions.scanner import Session

    native = "ses_activewhilethedbsaysarchived"
    key = f"opencode:{native}"
    sidecar = tmp_path / "metadata.json"
    sidecar.write_text(json.dumps({key: {"archived": False}}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()

    # What the engine still reports — exactly what `unarchive` leaves behind.
    row = Session(
        engine="opencode",
        uuid=native,
        cwd="/tmp/not-a-real-project",
        last_mtime=0.0,
        first_user_message="",
        archived=True,
    )
    monkeypatch.setattr(engines, "scan_all_checked", lambda: ([row], []))

    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(key)
    mirror.write_bytes(b"live session output")

    # Control: while the sidecar is readable, the override keeps the session out entirely.
    assert scrollback.archived_keys_checked() == ([], [])

    real_open = Path.open

    def refuse(self, *a, **k):
        if self == sidecar:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", refuse)

    keys, problems = scrollback.archived_keys_checked()
    assert keys == []  # fail closed: nothing is eligible when archive state is unknowable
    assert problems and "sidecar" in problems[0]

    out = maintenance.prune_caches(["archived_scrollback"])
    assert out["removed"] == 0
    assert mirror.exists()  # the active session's scrollback survives
    assert [f["category"] for f in out["failed"]] == ["archived_scrollback"]


def test_a_store_that_fails_once_cannot_report_empty_with_no_problems(
    monkeypatch, tmp_path, fake_jsonl
):
    """Rows and the evidence that they are complete must come from ONE pass.

    With a fail-soft listing plus a SEPARATE health probe, a store that failed the listing read and
    recovered before the probe answered "empty, and nothing went wrong" — so the preview counted
    nothing while the prune went on to delete what a later, working read found.
    """
    import sqlite3

    from agent_sessions.engines import opencode as oc

    db = tmp_path / "opencode.db"
    db.write_bytes(b"")  # the file exists; the READ is what fails, once
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    monkeypatch.setattr(oc.OpenCodeProvider, "is_present", lambda self: True)

    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return []

    monkeypatch.setattr(oc.OpenCodeProvider, "_query_rows", flaky)

    dry = maintenance.dry_run_caches()
    assert dry["archived_scrollback"].get("error")  # the pass that failed is the pass reported


def test_a_corrupt_store_is_not_read_as_absent_when_the_cli_is_missing(
    monkeypatch, tmp_path, fake_jsonl
):
    """Presence is itself a READ, so it cannot be the filter that decides what to measure.

    ``OpenCodeProvider.is_present`` needs a launchable CLI **or** a readable DB, so a corrupt store
    on a host without the CLI answers "absent" — and a presence filter turns *unreadable* into
    *nothing to see*. This deliberately does NOT force ``is_present``; that is the branch review
    4894 pointed out the earlier corrupt-DB test was stepping over.
    """
    from agent_sessions import discover, scrollback
    from agent_sessions.engines import opencode as oc

    db = tmp_path / "opencode.db"
    db.write_bytes(b"NOT A SQLITE DATABASE " * 64)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    monkeypatch.setattr(discover, "resolve", lambda engine_id: None)  # no CLI on this host

    assert oc.OpenCodeProvider().is_present() is False  # the trap this test exists for

    _keys, problems = scrollback.archived_keys_checked()
    assert [p for p in problems if p.startswith("opencode:")]


def test_one_corrupt_shell_record_never_hides_healthy_sessions_from_the_listing(
    monkeypatch, tmp_path, fake_jsonl
):
    """The DISPLAY path stays fail-soft per record, whatever maintenance needs.

    Routing ordinary ``scan_all()`` through the checked scan — to make the rows and their
    completeness share one code path — leaked maintenance strictness into the sidebar: one
    malformed shell record hid every healthy shell session (Hermes on PR #1000, review 4898).
    """
    from agent_sessions import engines
    from agent_sessions.engines import shell

    store = tmp_path / "shell-sessions"
    store.mkdir()
    good = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    (store / f"{good}.json").write_text(json.dumps({"id": good, "cwd": "/tmp/good"}))
    (store / "cccccccc-cccc-cccc-cccc-cccccccccccc.json").write_text("{")  # malformed
    monkeypatch.setenv("AGENT_SESSIONS_SHELL_DIR", str(store))

    assert [s.uuid for s in shell.ShellProvider().scan()] == [good]
    assert [s.uuid for s in engines.scan_all() if s.engine == "shell"] == [good]


def test_a_shell_record_that_cannot_be_read_is_not_counted_as_absent(
    monkeypatch, tmp_path, fake_jsonl
):
    """The non-opencode half of finding 3: a per-record read failure must not read as absence.

    ``shell`` keeps one JSON record per session and drops any record whose read fails, so "this
    session has no record" and "its record would not open" arrive identically — fine for a listing,
    wrong for a measurement of what is about to be deleted.
    """
    from agent_sessions import scrollback
    from agent_sessions.engines import shell

    native = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    store = tmp_path / "shell-sessions"
    store.mkdir()
    record = store / f"{native}.json"
    record.write_text(json.dumps({"id": native, "cwd": "/tmp/not-a-real-project"}))
    monkeypatch.setenv("AGENT_SESSIONS_SHELL_DIR", str(store))

    control_rows, control_problems = shell.ShellProvider().scan_checked()
    assert len(control_rows) == 1 and control_problems == []  # control: readable, so it is listed

    # Make the record genuinely unreadable rather than patching one method name: the checked scan
    # probes the file's bytes before its builder runs, so an injected failure on a single call the
    # probe does not make would slip past it AND be swallowed by the builder — which is the very
    # swallow this test exists to pin.
    record.chmod(0o000)

    assert shell.ShellProvider().scan() == []  # the fail-soft listing still drops it silently

    # …while the checked scan refuses to call that "absent". It stays PARTIAL — the unreadable
    # record costs only itself — which is the other half of review 4898's finding 4.
    rows, provider_problems = shell.ShellProvider().scan_checked()
    assert rows == []
    assert [p for p in provider_problems if p.startswith("shell:")]

    _keys, problems = scrollback.archived_keys_checked()
    assert [p for p in problems if p.startswith("shell:")]


# ---- eligibility: damage is not absence, and a snapshot is not permission ------------------


@pytest.mark.parametrize(
    "damaged",
    [
        pytest.param(None, id="row-is-not-an-object"),
        pytest.param({"archived": "false"}, id="archived-is-not-a-boolean"),
    ],
)
def test_a_damaged_override_never_authorises_a_deletion(monkeypatch, tmp_path, damaged):
    """Review 4898's P1 #1 — a DAMAGED override read as absent.

    The display decoder is fail-soft about exactly the shapes that matter: a row that is not an
    object is dropped, and a non-boolean ``archived`` becomes ``None``. Both then read downstream
    as "nobody recorded an override" and defer to the engine's own flag — which, for an unarchived
    opencode session, still says archived. Damage is not absence, and it is certainly not consent
    to delete.
    """
    from agent_sessions import engines, metadata, scrollback
    from agent_sessions.scanner import Session

    native = "ses_damagedoverride"
    key = f"opencode:{native}"
    sidecar = tmp_path / "metadata.json"
    sidecar.write_text(json.dumps({key: damaged}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()

    # What the engine still reports for an unarchived opencode session.
    row = Session(
        engine="opencode",
        uuid=native,
        cwd="/tmp/not-a-real-project",
        last_mtime=0.0,
        first_user_message="",
        archived=True,
    )
    monkeypatch.setattr(engines, "scan_all_checked", lambda: ([row], []))

    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(key)
    mirror.write_bytes(b"still an active session")

    keys, problems = scrollback.archived_keys_checked()
    assert keys == []  # nothing is eligible when the override cannot be read
    assert any("archive state" in p for p in problems)

    out = maintenance.prune_caches(["archived_scrollback"])
    assert out["removed"] == 0
    assert mirror.exists()  # the active session keeps its scrollback
    assert [f["category"] for f in out["failed"]] == ["archived_scrollback"]


def test_a_null_archived_flag_is_no_override_rather_than_damage(monkeypatch, tmp_path):
    """The sibling of the damaged-override test above, and its exact opposite in meaning.

    ``patch()`` persists the whole ``SessionMeta``, whose ``archived`` is ``None`` for any session
    that was merely renamed, coloured or reviewed — so an explicit ``null`` is how the app itself
    records "nobody has said anything", indistinguishable in intent from an absent key. Reading it
    as damage is not a safe over-approximation: it sweeps ordinary rows into fail-closed (146 of
    919 on the author's own sidecar), floods every preview with problems, and refuses to prune
    sessions that genuinely are archived. A non-boolean that is NOT null stays unreadable.
    """
    from agent_sessions import engines, metadata, scrollback
    from agent_sessions.scanner import Session

    uuid = "99999999-9999-9999-9999-999999999999"
    key = f"claude:{uuid}"
    sidecar = tmp_path / "metadata.json"
    # The ordinary shape: a reviewed session with no archive override recorded.
    sidecar.write_text(json.dumps({key: {"archived": None, "ai_title": "a reviewed session"}}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()

    assert metadata.archive_state_of_row(json.loads(sidecar.read_text()), key) == "unset"

    # `unset` defers to the engine, so a natively-archived session is still eligible…
    row = Session(
        engine="claude",
        uuid=uuid,
        cwd="/tmp/not-a-real-project",
        last_mtime=0.0,
        first_user_message="",
        archived=True,
    )
    monkeypatch.setattr(engines, "scan_all_checked", lambda: ([row], []))
    keys, problems = scrollback.archived_keys_checked()
    assert keys == [(key, key)]
    assert problems == []  # …and NOT reported as an archive state nobody could establish


def test_an_unarchive_between_discovery_and_deletion_spares_the_mirror(monkeypatch, tmp_path):
    """Review 4898's P1 #2 — the TOCTOU barrier.

    Discovery is a snapshot, and the prune runs in a worker thread while ordinary unarchive
    requests keep being served, so a session can stop being archived between the scan that found
    it and the unlink that would delete it. Here the unarchive COMMITS in that gap, through the
    real ``metadata.patch`` under the writers' lock; the per-key re-check takes that same lock, so
    the transition is either already visible or it waits for the unlink that is no longer coming.
    """
    from agent_sessions import metadata, scrollback

    native = "ses_unarchivedmidprune"
    key = f"opencode:{native}"
    sidecar = tmp_path / "metadata.json"
    sidecar.write_text(json.dumps({key: {"archived": True}}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()

    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(key)
    mirror.write_bytes(b"unarchived while the prune was running")

    def discover_then_unarchive():
        # The key WAS archived when discovery ran… (logical and physical coincide for this
        # session; the aliased case has its own regression.)
        keys = [(key, key)]
        # …and the operator unarchives it before the deletion step reaches it.
        metadata.patch(key, archived=False)
        return keys, []

    monkeypatch.setattr(scrollback, "archived_keys_checked", discover_then_unarchive)

    out = maintenance.prune_caches(["archived_scrollback"])

    assert mirror.exists()  # the session is active again; its scrollback is not ours to delete
    assert out["removed"] == 0
    assert out["bytes_freed"] == 0
    assert [s["reason"] for s in out["skipped"]] == ["the session was unarchived while pruning"]


def test_an_aliased_session_unarchived_mid_prune_keeps_its_cache(monkeypatch, tmp_path):
    """Review 4915/4919's P1 #1 — the fence was blind on exactly the engine that needs it.

    An opencode session launched under a ``new-<uuid>`` placeholder keeps every runtime resource —
    socket, lock, scrollback mirror — keyed by that PHYSICAL id, while ``unarchive`` records its
    override under the LOGICAL (real ``ses_…``) id. Discovery returned only the physical key, so
    the per-key re-check asked about an id the override is never written under, read ``unset``,
    deferred to opencode's own still-set ``time_archived``, and deleted an ACTIVE session's cache.
    """
    from agent_sessions import engines, metadata, scrollback

    real = "opencode:ses_aliasedandunarchived"
    placeholder = "opencode:new-11111111-1111-1111-1111-111111111111"
    sidecar = tmp_path / "metadata.json"
    sidecar.write_text(json.dumps({"__aliases__": {placeholder: real}, real: {"archived": True}}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()

    # The alias layer's direction, pinned here because the whole finding turns on it: the stored
    # map is placeholder→real, so the resources of the real id live under the PLACEHOLDER.
    _index, aliases, _overrides = metadata.load_checked()
    assert engines.physical_key(real, aliases) == placeholder

    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(placeholder)
    mirror.write_bytes(b"an aliased session's scrollback")

    def discover_then_unarchive():
        keys = [(real, placeholder)]  # archived when discovery ran…
        metadata.patch(real, archived=False)  # …then unarchived, under the LOGICAL id
        return keys, []

    monkeypatch.setattr(scrollback, "archived_keys_checked", discover_then_unarchive)

    out = maintenance.prune_caches(["archived_scrollback"])

    assert mirror.exists()  # the session is active again; its scrollback is not ours to delete
    assert out["removed"] == 0
    assert [s["reason"] for s in out["skipped"]] == ["the session was unarchived while pruning"]

    # The pre-fix fence, asserted at the seam rather than by patching a method that no longer
    # exists: the unarchive the prune just observed is INVISIBLE when only the physical key is
    # asked about, and `unset` is precisely what defers to opencode's own still-archived flag.
    with metadata.archive_state_held(placeholder) as blind:
        assert blind == "unset"
    with metadata.archive_state_held(real, placeholder) as seeing:
        assert seeing == "active"


def test_the_fence_locks_even_before_the_sidecar_exists(monkeypatch, tmp_path):
    """Review 4915/4919's P1 #2 — first creation was unfenced.

    ``archive_state_held`` short-circuited to ``unset`` when the sidecar did not exist, WITHOUT
    taking the lock. A natively-archived session legitimately reaches that branch on an install
    where BattleLab has never written a sidecar, so an unarchive could be created and committed
    inside the very block whose purpose is to exclude it. The lock is now always taken;
    ``_exclusive`` creating the file is the accepted cost, and an empty sidecar reads as ``unset``
    exactly as absence did.
    """
    from agent_sessions import metadata

    sidecar = tmp_path / "metadata.json"
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()
    assert not sidecar.exists()  # the branch under test: nothing has ever been written

    key = "opencode:ses_firstevercreation"
    committed = threading.Event()

    def unarchive_now():
        metadata.patch(key, archived=False)
        committed.set()

    writer = threading.Thread(target=unarchive_now)
    with metadata.archive_state_held(key) as state:
        assert state == "unset"  # nothing recorded yet — the honest answer, now given under lock
        writer.start()
        # Pre-fix this write landed INSIDE the block, because no lock was held on this path at all.
        assert not committed.wait(0.5)
    writer.join(5)

    assert committed.is_set()  # …and it lands as soon as the fence lets go
    _i, _a, overrides = metadata.load_checked()
    assert overrides[key] == "active"


# ---- discovery: an unreadable store is not an empty one, per provider ----------------------


@pytest.mark.parametrize(
    ("engine_id", "dir_attr"),
    [
        ("shell", "_shell_dir"),
        ("codex", "_codex_sessions_dir"),
        ("gemini", "_gemini_tmp_dir"),
        ("antigravity", "_antigravity_dir"),
        ("kimi", "_kimi_dir"),
    ],
)
def test_an_unreadable_store_root_is_never_read_as_empty(
    monkeypatch, tmp_path, engine_id, dir_attr
):
    """Review 4898's finding 3, across every provider maintenance relies on.

    ``Path.glob``/``rglob``/``iterdir`` suppress a permission error on the directory itself and
    yield nothing, and each provider then wraps that in ``except OSError: return out`` — so an
    unreadable store and an empty one give the same answer. That is right for the sidebar and
    wrong for a measurement that authorises deletion.

    Both halves are asserted together, because fixing one by breaking the other is precisely what
    finding 4 caught: the display path must stay silently fail-soft, and the checked path must
    refuse to call the same store empty.
    """
    from agent_sessions import engines
    from agent_sessions.engines import base

    root = tmp_path / engine_id
    root.mkdir()
    monkeypatch.setattr(base, dir_attr, lambda *a, **k: root)
    prov = engines.get(engine_id)

    root.chmod(0o000)
    try:
        assert prov.scan() == []  # the display path stays fail-soft, and silent
        with pytest.raises(OSError):
            prov.scan_checked()  # …the checked path refuses to call that "no sessions"
        _rows, problems = engines.scan_all_checked()
        assert [p for p in problems if p.startswith(f"{engine_id}:")]
    finally:
        root.chmod(0o700)


def test_an_unreadable_claude_transcript_is_named_not_silently_dropped(tmp_path):
    """The Claude half of finding 3, through the production scanner.

    ``session_from_jsonl`` turns an unreadable transcript into ``None`` — indistinguishable from
    "not a session" — so an archived session whose transcript cannot be read vanishes from the
    measurement while its scrollback mirror stays eligible. The checked walk keeps every transcript
    that read cleanly and names the one that did not.
    """
    from agent_sessions import scanner

    project = tmp_path / ".claude" / "projects" / "-home-user-x"
    project.mkdir(parents=True)
    good = project / "77777777-7777-7777-7777-777777777777.jsonl"
    good.write_text('{"type":"user","message":{"content":"readable"}}\n')
    bad = project / "88888888-8888-8888-8888-888888888888.jsonl"
    bad.write_text('{"type":"user","message":{"content":"unreadable"}}\n')

    rows, problems = scanner.scan_checked(home=tmp_path)
    assert sorted(s.uuid[:8] for s in rows) == ["77777777", "88888888"] and problems == []

    bad.chmod(0o000)
    try:
        # Mixed valid/corrupt: the healthy transcript survives, the unreadable one is NAMED.
        rows, problems = scanner.scan_checked(home=tmp_path)
        assert [s.uuid[:8] for s in rows] == ["77777777"]
        assert [p for p in problems if p.startswith("claude:")]
        # …while the display walk still lists BOTH, and that asymmetry is the point.
        # `session_from_jsonl` stats the file first — which SUCCEEDS, since stat needs permission
        # on the directory rather than on the file — and only the later content read fails, so the
        # row is still built with a decoded-path cwd instead of being dropped. The sidebar losing
        # nothing over one unreadable transcript is the fail-soft invariant working as intended,
        # and it is exactly why the checked walk cannot be derived from this one: maintenance must
        # OMIT what it could not read and say so, which is the opposite behaviour.
        assert sorted(s.uuid[:8] for s in scanner.scan(home=tmp_path)) == ["77777777", "88888888"]
    finally:
        bad.chmod(0o600)


def test_the_checked_walk_admits_exactly_what_the_display_walk_admits(tmp_path):
    """Review 4915/4919's P1 #3 — a membership divergence that made a LIVE session deletable.

    The checked walk recursed, so a transcript at any depth counted; ``_walk`` does not, taking the
    ``*.jsonl`` directly inside each immediate project child. A backup copy of a live session's
    transcript parked under ``projects-archive/<project>/backup/`` was therefore archived in the
    checked path and live in the display path — and since a uuid present in both trees resolves as
    ARCHIVED, the measurement made a running session's scrollback eligible for deletion.
    """
    from agent_sessions import scanner

    live_uuid = "aaaaaaa1-0000-0000-0000-000000000001"
    archived_uuid = "aaaaaaa2-0000-0000-0000-000000000002"
    record = '{"type":"user","message":{"content":"x"}}\n'

    live = tmp_path / ".claude" / "projects" / "-home-user-x"
    live.mkdir(parents=True)
    (live / f"{live_uuid}.jsonl").write_text(record)

    arch = tmp_path / ".claude" / "projects-archive" / "-home-user-x"
    arch.mkdir(parents=True)
    (arch / f"{archived_uuid}.jsonl").write_text(record)
    # The trap: a BACKUP of the LIVE session, nested one level deeper under the archive tree.
    (arch / "backup").mkdir()
    (arch / "backup" / f"{live_uuid}.jsonl").write_text(record)

    display = {(s.uuid, s.archived) for s in scanner.scan(home=tmp_path)}
    checked, problems = scanner.scan_checked(home=tmp_path)

    assert problems == []
    assert {(s.uuid, s.archived) for s in checked} == display
    # Concretely: the live session stays LIVE, and its nested backup admits no second row.
    assert display == {(live_uuid, False), (archived_uuid, True)}


def _stage_kimi_unreadable_state(tmp_path, monkeypatch):
    """A kimi store whose buckets LIST cleanly and whose session record will not read."""
    from agent_sessions.engines import base

    root = tmp_path / "kimi"
    session = (
        root / "sessions" / "wd_project_abc123" / ("session_dddddddd-dddd-dddd-dddd-dddddddddddd")
    )
    session.mkdir(parents=True)
    state = session / "state.json"
    state.write_text(json.dumps({"workDir": "/tmp/not-a-real-project"}))
    state.chmod(0o000)
    monkeypatch.setattr(base, "_kimi_dir", lambda *a, **k: root)
    return None


def _stage_gemini_unreadable_project_map(tmp_path, monkeypatch):
    """A gemini store whose chat files read fine and whose cwd map does not. The map is read ONCE
    and every row's cwd comes from it, so swallowing its failure drops every chat."""
    from agent_sessions.engines import base

    root = tmp_path / "gemini"
    chats = root / "projecthash" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-2026-01-01-abc.jsonl").write_text(
        json.dumps(
            {
                "sessionId": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "projectHash": "projecthash",
            }
        )
        + "\n"
    )
    pmap = root / "project-map.json"
    pmap.write_text(json.dumps({"projecthash": "/tmp/not-a-real-project"}))
    pmap.chmod(0o000)
    monkeypatch.setattr(base, "_gemini_tmp_dir", lambda *a, **k: root)
    return None


def _stage_antigravity_unreadable_conversation(tmp_path, monkeypatch):
    """An agy store listing one conversation whose db is not a database — the cwd scrape behind
    the row is the read that fails."""
    from agent_sessions.engines import base

    root = tmp_path / "antigravity"
    conv = root / "conversations"
    conv.mkdir(parents=True)
    (conv / "ffffffff-ffff-ffff-ffff-ffffffffffff.db").write_bytes(b"NOT A SQLITE DATABASE " * 64)
    monkeypatch.setattr(base, "_antigravity_dir", lambda *a, **k: root)
    return None


def _stage_antigravity_locked_conversation(tmp_path, monkeypatch):
    """A VALID agy conversation db whose row read is blocked by another connection holding
    ``BEGIN EXCLUSIVE`` — review 4915's own antigravity scenario.

    This is the case a readability probe structurally cannot see: the file opens fine and only the
    actual SQLite lookup fails, which `_db_cwd` swallowed into an empty cwd and thus a dropped row.
    The optional cwd cache is deliberately absent so the db blob is the only source for the cwd.

    The blob must be framed the way agy writes it — a protobuf tag, a length varint, then the
    ``file://`` URI — because `_file_uri_path` reads that varint to delimit the path. A blob that
    merely CONTAINS the URI parses to ``""`` and the row is dropped for a reason that has nothing
    to do with locking, which silently makes this fixture prove nothing.

    Costs ~10s: sqlite's default busy timeout is 5s per connection, and the display read and the
    checked read each take one.
    """
    import sqlite3

    from agent_sessions.engines import base

    root = tmp_path / "antigravity-locked"
    conv = root / "conversations"
    conv.mkdir(parents=True)
    db = conv / "fffffff0-ffff-ffff-ffff-fffffffffff0.db"
    uri = b"file:///tmp/not-a-real-project"
    blob = b"\x12" + bytes([len(uri)]) + uri
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (blob,))
    con.commit()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(base, "_antigravity_dir", lambda *a, **k: root)

    def restore():
        holder.execute("ROLLBACK")
        holder.close()
        con.close()

    return restore


def _stage_opencode_inaccessible_ancestor(tmp_path, monkeypatch):
    """An existing ``opencode.db`` under a directory that cannot be traversed — the case
    ``os.path.exists`` answers *False* for, which is why presence is now an ``os.stat``."""
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    db = hidden / "opencode.db"
    db.write_bytes(b"")
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    hidden.chmod(0o000)
    return lambda: hidden.chmod(0o700)  # a 0o000 DIRECTORY would defeat tmp_path cleanup


@pytest.mark.parametrize(
    ("engine_id", "stage"),
    [
        pytest.param("kimi", _stage_kimi_unreadable_state, id="kimi-state-json"),
        pytest.param("gemini", _stage_gemini_unreadable_project_map, id="gemini-project-map"),
        pytest.param(
            "antigravity", _stage_antigravity_unreadable_conversation, id="antigravity-db"
        ),
        pytest.param(
            "antigravity", _stage_antigravity_locked_conversation, id="antigravity-locked"
        ),
        pytest.param("opencode", _stage_opencode_inaccessible_ancestor, id="opencode-ancestor"),
    ],
)
def test_a_providers_authoritative_read_failure_is_named_not_swallowed(
    monkeypatch, tmp_path, engine_id, stage
):
    """Review 4915/4919's P2 #4 — the ENUMERATION was checked; the read behind each row was not.

    Each of these providers resolves a row through a second, authoritative read: kimi's
    ``state.json``, gemini's ``project-map.json``, antigravity's conversation db, opencode's
    ``opencode.db``. All four swallowed a failure of THAT read into "no usable row" / "no
    sessions" — indistinguishable from an empty store, and the one answer that must never
    authorise a deletion. In every case here the store root lists cleanly and only the read behind
    it fails, so a checked *enumeration* on its own would still have reported a clean empty.
    """
    from agent_sessions import engines

    restore = stage(tmp_path, monkeypatch)
    try:
        # The DISPLAY path stays fail-soft and silent — the repo invariant, not a bug…
        assert engines.get(engine_id).scan() == []
        # …while the measurement refuses to call the same store empty, at the seam maintenance uses.
        _rows, problems = engines.scan_all_checked()
        assert [p for p in problems if p.startswith(f"{engine_id}:")]
    finally:
        if restore is not None:
            restore()


# ---- the runner ---------------------------------------------------------------------------


def test_the_runner_refuses_a_second_job_while_one_runs():
    async def main():
        runner = maintenance.Runner()
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return "done"

        first = asyncio.create_task(runner.run("prune", slow))
        await asyncio.sleep(0)
        with pytest.raises(maintenance.MaintenanceBusy) as e:
            await runner.run("missions", slow)
        assert e.value.info["job"] == "prune"
        gate.set()
        assert await first == "done"
        assert runner.busy_info() is None

    asyncio.run(main())


def test_a_cancelled_request_does_not_release_single_flight_before_the_job_returns():
    """A client disconnect cancels the REQUEST coroutine. The job keeps the slot until it has
    actually finished — otherwise a second job could start while the first is still mutating."""

    async def main():
        runner = maintenance.Runner()
        gate = asyncio.Event()
        finished = asyncio.Event()

        async def slow():
            await gate.wait()
            finished.set()
            return "done"

        request = asyncio.create_task(runner.run("prune", slow))
        await asyncio.sleep(0)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        # The request is gone; the job is not.
        assert runner.busy_info() is not None
        assert runner.busy_info()["job"] == "prune"
        with pytest.raises(maintenance.MaintenanceBusy):
            await runner.run("prune", slow)
        gate.set()
        await asyncio.wait_for(finished.wait(), 2)
        for _ in range(100):
            if runner.busy_info() is None:
                break
            await asyncio.sleep(0.01)
        assert runner.busy_info() is None

        async def quick():
            return "again"

        assert await runner.run("prune", quick) == "again"

    asyncio.run(main())


def test_a_failing_job_releases_the_slot():
    async def main():
        runner = maintenance.Runner()

        async def boom():
            raise RuntimeError("disk on fire")

        with pytest.raises(RuntimeError):
            await runner.run("prune", boom)
        assert runner.busy_info() is None

    asyncio.run(main())


# ---- routes -------------------------------------------------------------------------------


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def test_archived_scrollback_through_the_routes(auth_cfg, fake_jsonl):
    from agent_sessions import webterm

    active_key = f"claude:{UUID_A}"  # live in the fixture
    archived_key = "claude:44444444-4444-4444-4444-444444444444"  # archived in the fixture
    webterm._buffer_append(active_key, b"active session output")
    webterm._buffer_append(archived_key, b"archived session output")

    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    dry = c.get("/api/maintenance/prune")
    assert dry.status_code == 200
    body = dry.json()
    assert body["categories"]["archived_scrollback"]["items"] == 1
    assert body["categories"]["archived_scrollback"]["bytes"] > 0
    assert body["runner"] is None
    assert "compact" not in body and "schedule" not in body  # increments 2 and 3

    r = c.post("/api/maintenance/prune", json={"categories": ["archived_scrollback"]}, headers=hdr)
    assert r.status_code == 200
    out = r.json()
    assert out["removed"] == 1 and out["bytes_freed"] > 0
    assert out["skipped"] == [] and out["failed"] == []
    assert not webterm._scrollback_path(archived_key).exists()
    assert webterm._scrollback_path(active_key).exists()


def test_an_unreadable_category_reaches_the_routes_as_an_error(auth_cfg, fake_jsonl, runtime):
    """End to end, through the production routes: the dry run marks the category ``error`` (which
    is what the card's guard blocks on) and the POST reports the failure rather than 200-with-
    nothing-removed."""
    _dead_socket(ptybridge.socket_path("claude", UUID_B))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    runtime.chmod(0o000)
    try:
        body = c.get("/api/maintenance/prune").json()
        out = c.post(
            "/api/maintenance/prune", json={"categories": ["stale_sockets"]}, headers=hdr
        ).json()
    finally:
        runtime.chmod(0o700)

    assert body["categories"]["stale_sockets"].get("error")
    assert body["categories"]["stale_sockets"]["items"] == 0
    assert out["removed"] == 0
    assert out["failed_total"] == 1
    assert out["failed"][0]["category"] == "stale_sockets"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"categories": []},
        {"categories": "stale_sockets"},
        {"categories": ["stale_sockets", "stale_sockets"]},
        {"categories": ["everything"]},
        {"categories": [True]},
        {"categories": ["stale_sockets"], "force": True},
        ["stale_sockets"],
    ],
)
def test_prune_body_is_strict(auth_cfg, fake_jsonl, payload):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(
        "/api/maintenance/prune",
        json=payload,
        headers={"X-CSRF-Token": csrf, "Origin": auth_cfg.origin},
    )
    assert r.status_code == 422


def test_prune_requires_login_and_csrf(auth_cfg, fake_jsonl):
    c = _client(auth_cfg)
    assert c.get("/api/maintenance/prune").status_code == 401
    _login(c, auth_cfg)
    r = c.post(
        "/api/maintenance/prune",
        json={"categories": ["stale_sockets"]},
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403


def test_a_second_prune_while_one_runs_is_a_409(auth_cfg, fake_jsonl, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real = maintenance.prune_caches

    def blocking(categories):
        started.set()
        assert release.wait(10)
        return real(categories)

    monkeypatch.setattr(maintenance, "prune_caches", blocking)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    first: dict = {}

    def go():
        first["r"] = c.post(
            "/api/maintenance/prune", json={"categories": ["stale_sockets"]}, headers=hdr
        )

    t = threading.Thread(target=go)
    t.start()
    try:
        assert started.wait(10)
        second = c.post(
            "/api/maintenance/prune", json={"categories": ["stale_sockets"]}, headers=hdr
        )
        assert second.status_code == 409
        assert second.json()["busy"]["job"] == "prune"
        assert "retry when maintenance finishes" in second.json()["detail"]
        assert c.get("/api/maintenance/prune").json()["runner"]["job"] == "prune"
    finally:
        release.set()
        t.join(20)
    assert first["r"].status_code == 200


# ---- discovery: a read that fails AFTER the probe is named, never counted as absent ----------


_UUID = "12345678-1234-4123-8123-1234567890ab"


def _seed_claude(base_mod, monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = tmp_path / ".claude" / "projects" / "-home-u-proj"
    root.mkdir(parents=True)
    transcript = root / f"{_UUID}.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "cwd": "/home/u/proj", "message": {"content": "t"}}) + "\n"
    )
    return transcript


def _seed_shell(base_mod, monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "shell"
    root.mkdir()
    monkeypatch.setattr(base_mod, "_shell_dir", lambda *a, **k: root)
    rec = root / f"{_UUID}.json"
    rec.write_text(json.dumps({"id": _UUID, "cwd": "/home/u/proj"}))
    return rec


def _seed_codex(base_mod, monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "codex"
    day = root / "2026" / "09" / "18"
    day.mkdir(parents=True)
    monkeypatch.setattr(base_mod, "_codex_sessions_dir", lambda *a, **k: root)
    rollout = day / f"rollout-2026-09-18T00-00-00-{_UUID}.jsonl"
    rollout.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"type": "session_meta", "cwd": "/home/u/p"}}
        )
        + "\n"
    )
    return rollout


def _seed_gemini(base_mod, monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "gemini"
    chats = root / "projecthash" / "chats"
    chats.mkdir(parents=True)
    monkeypatch.setattr(base_mod, "_gemini_tmp_dir", lambda *a, **k: root)
    (root / "project-map.json").write_text(json.dumps({"projecthash": "/home/u/proj"}))
    chat = chats / f"session-{_UUID}.jsonl"
    chat.write_text(json.dumps({"sessionId": _UUID, "projectHash": "projecthash"}) + "\n")
    return chat


def _seed_kimi(base_mod, monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "kimi"
    sess = root / "sessions" / "bucket" / f"session_{_UUID}"
    sess.mkdir(parents=True)
    monkeypatch.setattr(base_mod, "_kimi_dir", lambda *a, **k: root)
    state = sess / "state.json"
    state.write_text(json.dumps({"workDir": "/home/u/proj", "title": "t"}))
    return state


def _seed_kimi_v2(base_mod, monkeypatch, tmp_path: Path) -> Path:
    state = _seed_kimi(base_mod, monkeypatch, tmp_path)
    state.write_text(json.dumps({"cwd": "/home/u/proj", "title": "t", "updatedAt": 1789776000000}))
    return state


def _seed_antigravity(base_mod, monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "antigravity"
    (root / "conversations").mkdir(parents=True)
    (root / "cache").mkdir()
    monkeypatch.setattr(base_mod, "_antigravity_dir", lambda *a, **k: root)
    (root / "cache" / "last_conversations.json").write_text(json.dumps({"/home/u/proj": _UUID}))
    db = root / "conversations" / f"{_UUID}.db"
    db.write_bytes(b"sqlite-ish")
    return db


@pytest.mark.parametrize(
    ("engine_id", "seed", "method"),
    [
        ("claude", _seed_claude, "open"),
        ("claude", _seed_claude, "stat"),
        ("shell", _seed_shell, "read_text"),
        ("shell", _seed_shell, "stat"),
        ("codex", _seed_codex, "open"),
        ("codex", _seed_codex, "stat"),
        ("gemini", _seed_gemini, "open"),
        ("gemini", _seed_gemini, "stat"),
        ("kimi", _seed_kimi, "stat"),
        ("kimi", _seed_kimi_v2, "stat"),
        ("antigravity", _seed_antigravity, "stat"),
    ],
)
def test_a_read_that_fails_after_the_probe_is_named_never_counted_as_absent(
    monkeypatch, tmp_path, runtime, engine_id, seed, method
):
    """A successful preflight cannot vouch for a later failed read/stat (review 4951).

    Exercise the real provider, archive discovery, preview and prune. Another nonempty category
    must not make an incomplete zero measurement confirmable; recovery requires a fresh preview.
    """
    from agent_sessions import checked_scan, engines, metadata, scrollback
    from agent_sessions.engines import base

    target = seed(base, monkeypatch, tmp_path)
    prov = engines.get(engine_id)

    native_id = _UUID if engine_id != "kimi" else f"session_{_UUID}"
    rows, problems = prov.scan_checked()
    assert [s.uuid for s in rows] == [native_id]
    assert problems == []

    key = f"{engine_id}:{native_id}"
    sidecar = tmp_path / "metadata.json"
    sidecar.write_text(json.dumps({key: {"archived": True}}))
    monkeypatch.setattr(metadata, "_default_path", lambda: sidecar)
    metadata.invalidate_raw_cache()
    monkeypatch.setattr(engines, "scan_all_checked", prov.scan_checked)
    scrollback._ensure_scrollback_dir()
    mirror = scrollback._scrollback_path(key)
    content = b"archived output that must be counted before confirmation"
    mirror.write_bytes(content)
    _dead_socket(ptybridge.socket_path("claude", UUID_A))

    assert maintenance.dry_run_caches()["archived_scrollback"] == {
        "items": 1,
        "bytes": len(content),
    }
    real = getattr(Path, method)
    readable = checked_scan._readable
    probing = False
    probes = []

    def probe(path):
        nonlocal probing
        probing = True
        try:
            readable(path)
        finally:
            probing = False
        probes.append(path)

    def failing(self, *a, **k):
        if self == target and not probing:
            raise OSError(errno.EIO, "injected: the authoritative read failed")
        return real(self, *a, **k)

    monkeypatch.setattr(checked_scan, "_readable", probe)
    monkeypatch.setattr(Path, method, failing)

    rows, problems = prov.scan_checked()
    assert probes  # the preflight really succeeded
    assert rows == []
    assert [p for p in problems if p.startswith(f"{engine_id}:")], (
        "an authoritative read failure was swallowed into a silent omission: "
        "the measurement reports a valid zero for a store it could not read"
    )
    # Claude's display fallback still builds a row from the path if only content is unreadable.
    display_ids = [native_id] if engine_id == "claude" and method == "open" else []
    assert [s.uuid for s in prov.scan()] == display_ids

    dry = maintenance.dry_run_caches()
    assert dry["stale_sockets"]["items"] == 1
    assert dry["archived_scrollback"]["error"] == "DiscoveryIncomplete"
    out = maintenance.prune_caches(["archived_scrollback"])
    assert out["removed"] == 0
    assert out["failed"]
    assert mirror.read_bytes() == content

    # Recovery exposes the omitted bytes in a fresh preview; pruning then removes exactly those.
    monkeypatch.setattr(Path, method, real)
    assert maintenance.dry_run_caches()["archived_scrollback"] == {
        "items": 1,
        "bytes": len(content),
    }
    out = maintenance.prune_caches(["archived_scrollback"])
    assert out["failed"] == []
    assert out["removed"] == 1
    assert out["bytes_freed"] == len(content)
    assert not mirror.exists()


@pytest.mark.parametrize("seed", [_seed_kimi, _seed_kimi_v2])
def test_kimi_checked_metadata_parses_the_single_read(monkeypatch, tmp_path, seed):
    """A second open after a readable probe used to swallow EIO and silently omit the row."""
    from agent_sessions.engines import base, kimi

    target = seed(base, monkeypatch, tmp_path)
    real = Path.open
    opens = []

    def open_once(self, *a, **k):
        if self == target:
            opens.append(self)
            if len(opens) > 1:
                raise OSError(errno.EIO, "the file became unreadable after its first read")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "open", open_once)
    result = kimi._meta_checked(target.parent)
    assert result is not None
    assert result[:2] == ("/home/u/proj", "t")
    assert opens == [target]
