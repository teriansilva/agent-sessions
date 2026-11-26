"""The roster boundary skips re-reading unchanged state on read scopes, never on admission (#2413).

Every HTTP request and every structured-facade `describe()` opens a fresh read scope. Before
#2413 each one re-verified and re-parsed the whole plugin manager state (170 KB in production)
under `_ROSTER_LOCK` on the event loop, just to learn that `roster_revision` had not moved.
"""

import os
import uuid

import pytest

from agent_sessions.engines import registry
from agent_sessions.plugins import manager, storage


@pytest.fixture
def loads(monkeypatch):
    """Count full verified reads of the manager state and of the reload hint."""
    calls = {"manager": 0, "reload": 0}
    real_load, real_read = manager._load, storage.read

    def counting_load(path):
        calls["manager"] += 1
        return real_load(path)

    def counting_read(path, **kwargs):
        if path.name == "reload.json":
            calls["reload"] += 1
        return real_read(path, **kwargs)

    monkeypatch.setattr(manager, "_load", counting_load)
    monkeypatch.setattr(storage, "read", counting_read)
    return calls


def _write_manager(revision: str | None = None) -> None:
    doc = {"contract": 1, "plugins": {}, "operations": {}, "reviews": {}}
    doc["roster_revision"] = revision or str(uuid.uuid4())
    with storage.locked(manager.DOCUMENT) as path:
        storage.write(path, doc)


@pytest.fixture
def committed():
    """A revisioned manager state, settled by full passes. The first one also creates the
    revision marker, so its fingerprint moves once more and a second full pass records it."""
    _write_manager()
    for _ in range(2):
        registry.sync_committed()
        registry.sync_requested()
    yield storage.root() / manager.DOCUMENT
    from agent_sessions.plugins import FIRST_PARTY_DIR

    registry.reload(first_party_dir=FIRST_PARTY_DIR)


def _require_shortcut() -> None:
    """Cache-hit assertions need a chain the shortcut may trust. Where an ancestor of the scratch
    directory is group-writable or carries an ACL, the shortcut is deliberately off (every read
    scope re-verifies), so those assertions cannot hold there: say so instead of failing."""
    if registry._chain_fingerprint(storage.root()) is registry._UNKNOWN:
        pytest.skip("an ancestor of the scratch dir is group-writable or has an ACL: no shortcut")


def test_unchanged_state_is_read_once_across_read_scopes(committed, loads):
    _require_shortcut()
    for _ in range(25):
        with registry.snapshot_scope(fresh=True):
            pass
    assert loads == {"manager": 0, "reload": 0}


def test_admission_always_takes_the_full_verified_read(committed, loads):
    for _ in range(3):
        with registry.snapshot_scope(fresh=True, require_current=True):
            pass
    assert loads == {"manager": 3, "reload": 3}


def test_an_atomic_replacement_is_observed_and_reloads(committed, loads):
    _require_shortcut()
    before = registry.capture().generation
    _write_manager()
    with registry.snapshot_scope(fresh=True) as view:
        assert view.generation > before
    observed = loads["manager"]
    assert observed >= 1
    with registry.snapshot_scope(fresh=True):
        pass
    assert loads["manager"] == observed, "the new state is cached once it has been verified"


def test_an_in_place_rewrite_is_observed(committed, loads):
    data = committed.read_bytes()
    committed.write_bytes(data.replace(b'"contract": 1', b'"contract": 1 '))
    with registry.snapshot_scope(fresh=True):
        pass
    assert loads["manager"] >= 1


def test_a_permission_change_reverifies_and_publishes_the_closed_roster(committed, loads):
    os.chmod(committed, 0o640)
    try:
        with registry.snapshot_scope(fresh=True):
            pass
        first = loads["manager"]
        assert first >= 1
        assert registry._COMMIT_SEEN == (str(storage.root().absolute()), "unavailable")
        with registry.snapshot_scope(fresh=True):
            pass
        assert loads["manager"] > first, "an unreadable state is never cached"
    finally:
        os.chmod(committed, 0o600)
    with registry.snapshot_scope(fresh=True):
        pass
    assert registry._COMMIT_SEEN[1] not in (None, "unavailable")


def test_a_state_directory_mode_change_reverifies(committed, loads):
    root = storage.root()
    mode = os.stat(root).st_mode & 0o777
    os.chmod(root, mode | 0o020)
    try:
        with registry.snapshot_scope(fresh=True):
            pass
        assert loads["manager"] >= 1
    finally:
        os.chmod(root, mode)


def test_a_reset_commit_state_forces_the_full_pass(committed, loads):
    registry._COMMIT_SEEN = None
    with registry.snapshot_scope(fresh=True):
        pass
    assert loads["manager"] >= 1


def test_a_new_reload_request_is_observed_by_the_next_read_scope(committed, loads):
    before = registry.capture().generation
    with storage.locked("reload.json") as path:
        storage.write(path, {"id": str(uuid.uuid4())})
    with registry.snapshot_scope(fresh=True) as view:
        assert view.generation > before
    assert loads["reload"] == 1


def test_middleware_pins_no_roster_for_static_and_liveness(auth_cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions import main

    scopes = []
    real = registry.snapshot_scope

    def counting(**kwargs):
        scopes.append(kwargs)
        return real(**kwargs)

    client = TestClient(main.create_app(auth_cfg), base_url=auth_cfg.origin)
    monkeypatch.setattr(registry, "snapshot_scope", counting)
    for path in ("/healthz", "/api/auth-check", "/static/does-not-exist.css", "/assets/x.js"):
        client.get(path)
    assert scopes == []
    client.get("/login")
    assert scopes, "every other route still runs inside a roster scope"


def test_the_roster_free_list_is_exact():
    from agent_sessions import main

    assert main._ROSTER_FREE_PATHS == {"/healthz", "/api/auth-check"}
    assert main._ROSTER_FREE_PREFIXES == ("/static/", "/assets/")
    assert not main._roster_free("/healthz/x")
    assert not main._roster_free("/staticx")
    assert not main._roster_free("/api/engines")


def _unavailable() -> bool:
    return registry._COMMIT_SEEN == (str(storage.root().absolute()), "unavailable")


def test_an_ancestor_directory_made_world_writable_is_seen_without_any_file_change(
    committed, loads
):
    """Hermes on #2413: a provenance change ABOVE the state directory, with no file touched,
    must not leave read scopes on the last verified roster indefinitely."""
    before = registry.get_any("claude")
    ancestor = storage.root().parent
    mode = os.stat(ancestor).st_mode & 0o7777
    os.chmod(ancestor, mode | 0o002)
    try:
        with registry.snapshot_scope(fresh=True):
            pass
        assert loads["manager"] >= 1
        assert _unavailable()
        from agent_sessions.plugins import provenance

        with pytest.raises(provenance.ProvenanceError, match="writable by other users"):
            registry.live_provider("claude")  # admission fails closed
    finally:
        os.chmod(ancestor, mode)
    with registry.snapshot_scope(fresh=True):
        pass
    assert not _unavailable()
    assert registry.admits(registry.get("claude")) and before is not None


def test_an_ancestor_acl_disables_the_shortcut(committed, loads):
    """An ACL makes the walk's verdict depend on group membership, which no stat shows: with
    one anywhere on the chain, every read scope re-verifies."""
    import shutil
    import subprocess

    if not shutil.which("setfacl"):
        pytest.skip("setfacl is not installed")
    ancestor = storage.root().parent
    result = subprocess.run(
        ["setfacl", "-m", f"u:{os.getuid()}:rwx", str(ancestor)], capture_output=True
    )
    if result.returncode:
        pytest.skip("this filesystem does not take POSIX ACLs")
    try:
        for expected in (1, 2, 3):
            with registry.snapshot_scope(fresh=True):
                pass
            assert loads["manager"] >= expected
    finally:
        subprocess.run(["setfacl", "-b", str(ancestor)], check=True)


def test_group_membership_change_alone_is_seen_by_the_next_read_scope(
    committed, loads, monkeypatch
):
    """Hermes on #2413: trust that depends on a private group must not be cached. A
    group-writable ancestor is trusted only while its group is private; flipping only the group
    database (no file or directory metadata moves) must close the very next read scope and
    admission, and restoring it must recover."""
    from agent_sessions.plugins import provenance

    private = {"value": True}
    monkeypatch.setattr(provenance, "_group_is_private", lambda gid: private["value"])
    ancestor = storage.root().parent
    mode = os.stat(ancestor).st_mode & 0o7777
    os.chmod(ancestor, mode | 0o020)
    try:
        for expected in (1, 2):
            with registry.snapshot_scope(fresh=True):
                pass
            assert loads["manager"] >= expected, "group-dependent trust is never cached"
        assert not _unavailable()
        private["value"] = False  # a member joined the group: nothing on disk changed
        with registry.snapshot_scope(fresh=True):
            pass
        assert _unavailable()
        with pytest.raises(provenance.ProvenanceError, match="writable by other users"):
            registry.live_provider("claude")
        private["value"] = True
        with registry.snapshot_scope(fresh=True):
            pass
        assert not _unavailable()
        assert registry.admits(registry.get("claude"))
    finally:
        os.chmod(ancestor, mode)


@pytest.mark.parametrize("control", [False, True], ids=["real", "negative-control"])
def test_a_delayed_older_pass_cannot_install_a_stale_binding_or_suppress_a_reload(
    committed, monkeypatch, control
):
    """Hermes on #2413 / #2416: park a MANAGER observer right after it fingerprinted the OLD
    `manager.json` (outside the lock), let another caller settle the new state, then release the
    parked one. Its fingerprint no longer matches, so it takes the full pass over the new bytes and
    must not bind its old fingerprint to the new state. The negative control runs the same
    interleaving with an unconditional `_settled` and shows the stale binding it would install."""
    import threading

    _require_shortcut()
    if control:
        monkeypatch.setattr(registry, "_settled", lambda before, measure, state: (before, state))
    paused, release = threading.Event(), threading.Event()
    real_fingerprint, real_load = registry._fingerprint, manager._load
    parked = {}

    def pausing(path):
        result = real_fingerprint(path)
        # The manager observer fingerprints DOCUMENT, then REVISION_MARKER: park between them.
        if threading.get_ident() == parked.get("thread") and path.name == manager.DOCUMENT:
            parked["document"] = result
            parked["thread"] = None  # park once
            paused.set()
            assert release.wait(10)
        return result

    def counting_load(path):
        if threading.current_thread() is worker:
            parked["full_reads"] = parked.get("full_reads", 0) + 1
        return real_load(path)

    monkeypatch.setattr(registry, "_fingerprint", pausing)
    monkeypatch.setattr(manager, "_load", counting_load)

    def older():
        parked["thread"] = threading.get_ident()
        registry.sync_committed(full=False)  # the read-scope path, where the shortcut lives

    worker = threading.Thread(target=older)
    worker.start()
    assert paused.wait(10), "the manager observer was parked"
    old_document = parked["document"]
    _write_manager()
    with registry.snapshot_scope(fresh=True):
        pass  # the newer state is settled by another caller
    settled = registry._COMMIT_CHECKED
    assert settled is not None and settled[0][2] != old_document, "the new state is settled"
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert parked.get("full_reads", 0) >= 1, "the parked observer took the full pass"
    revision = manager.snapshot()["roster_revision"]
    assert registry._COMMIT_SEEN == (str(storage.root().absolute()), revision)
    if control:
        assert registry._COMMIT_CHECKED[0][2] == old_document, "control: stale binding installed"
        return
    assert registry._COMMIT_CHECKED in (None, settled), "an older observation was installed"
    before = registry.capture().generation
    _write_manager()
    with registry.snapshot_scope(fresh=True) as view:
        assert view.generation > before, "the next change still reloads"


def test_unreadable_state_keeps_retrying_and_recovers(committed, loads):
    data = committed.read_bytes()
    committed.write_bytes(b"{broken")
    try:
        for expected in (1, 2, 3):
            with registry.snapshot_scope(fresh=True):
                pass
            assert loads["manager"] >= expected
            assert _unavailable()
    finally:
        committed.write_bytes(data)
    with registry.snapshot_scope(fresh=True):
        pass
    assert not _unavailable()


def test_a_replacement_between_fingerprint_and_read_is_never_cached(monkeypatch):
    _write_manager()
    for _ in range(2):
        registry.sync_committed()
    real_load = manager._load
    raced = []

    def racing_load(path):
        doc = real_load(path)
        if not raced:
            raced.append(True)
            _write_manager()  # lands after this pass fingerprinted the file and read it
        return doc

    monkeypatch.setattr(manager, "_load", racing_load)
    registry.sync_committed()  # full pass; the file moved under it
    assert registry._COMMIT_CHECKED is None
    calls = []
    monkeypatch.setattr(manager, "_load", lambda path: calls.append(path) or real_load(path))
    registry.sync_committed(full=False)
    assert calls, "the moved file is read again"
    revision = manager.snapshot()["roster_revision"]
    assert registry._COMMIT_SEEN == (str(storage.root().absolute()), revision)
    from agent_sessions.plugins import FIRST_PARTY_DIR

    registry.reload(first_party_dir=FIRST_PARTY_DIR)


def test_missing_revision_marker_of_revisioned_state_is_repaired_on_the_full_path(committed, loads):
    marker = storage.root() / manager.REVISION_MARKER
    assert marker.exists()
    marker.unlink()
    with registry.snapshot_scope(fresh=True):
        pass
    assert loads["manager"] >= 1
    assert marker.exists(), "the full read restores the sentinel, exactly as before #2413"
