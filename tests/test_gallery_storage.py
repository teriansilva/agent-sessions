"""A renamed evidence root must not redirect an operation holding its old locks."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_sessions.gallery import storage
from agent_sessions.gallery.contract import GalleryError


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(tmp_path / "playbooks"))
    return storage.root()


@pytest.mark.parametrize("action", ["read", "write", "nested-lock"])
def test_root_move_does_not_redirect_a_held_publication_lock(state, action):
    moved = state.with_name("retained-gallery-state")

    def later_writer():
        with storage.locked("publications") as path:
            storage.write(path, {"writer": "later"})

    with pytest.raises(GalleryError, match="directory changed"):
        with storage.locked("publications") as path:
            storage.write(path, {"writer": "first"})
            state.rename(moved)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(later_writer).result()
            if action == "write":
                storage.write(path, {"writer": "wrong lock"})
            elif action == "read":
                storage.read(path)
            else:
                with storage.locked("index"):
                    pass
    assert storage.read(state / "publications.json") == {"writer": "later"}
    assert storage.read(moved / "publications.json") == {"writer": "first"}


def test_move_during_atomic_write_cannot_replace_the_new_roots_record(state, monkeypatch):
    replace = storage.os.replace
    moved = state.with_name("retained-gallery-state")
    raced = False

    def move_then_replace(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            state.rename(moved)
            state.mkdir(mode=0o700)
            (state / "publications.json").write_text('{"writer":"later"}')
            (state / "publications.json").chmod(0o600)
        return replace(*args, **kwargs)

    with pytest.raises(GalleryError, match="directory changed"):
        with storage.locked("publications") as path:
            monkeypatch.setattr(storage.os, "replace", move_then_replace)
            storage.write(path, {"writer": "first"})
    assert raced
    assert storage.read(state / "publications.json") == {"writer": "later"}


def test_descriptor_write_handles_short_writes_and_reports_flush_failure(state, monkeypatch):
    import os
    import stat

    write, fsync = os.write, os.fsync
    payload = {"content": "Complete evidence" * 20}
    with storage.locked("publications") as path:
        monkeypatch.setattr(storage.os, "write", lambda fd, data: write(fd, data[:7]))
        storage.write(path, payload)
        assert storage.read(path) == payload

        def fail_directory_flush(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("flush failed after publication")
            fsync(fd)

        monkeypatch.setattr(storage.os, "fsync", fail_directory_flush)
        with pytest.raises(OSError, match="flush failed"):
            storage.write(path, {"content": "New complete evidence"})
        assert storage.read(path) == {"content": "New complete evidence"}


def test_read_refuses_a_root_move_after_opening_the_evidence(state, monkeypatch):
    loads = storage.json.loads
    moved = state.with_name("retained-gallery-state")
    with storage.locked("publications") as path:
        storage.write(path, {"writer": "first"})

    def move_after_decode(data):
        value = loads(data)
        state.rename(moved)
        state.mkdir(mode=0o700)
        return value

    with pytest.raises(GalleryError, match="directory changed"):
        with storage.locked("publications") as path:
            monkeypatch.setattr(storage.json, "loads", move_after_decode)
            storage.read(path)
