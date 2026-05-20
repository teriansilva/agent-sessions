from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_sessions import metadata


def test_empty_load_for_missing_file(tmp_home):
    assert metadata.load() == {}


def test_patch_creates_file_and_persists(tmp_home):
    m = metadata.patch("abc", title="API refactor", sticky=True, sort_key=10)
    assert m.title == "API refactor"
    assert m.sticky is True
    again = metadata.load()
    assert "abc" in again
    assert again["abc"].title == "API refactor"
    assert again["abc"].sticky is True
    assert again["abc"].sort_key == 10


def test_patch_merges_fields(tmp_home):
    metadata.patch("abc", title="first")
    metadata.patch("abc", sticky=True)
    state = metadata.load()
    assert state["abc"].title == "first"
    assert state["abc"].sticky is True


def test_patch_rejects_unknown_fields(tmp_home):
    with pytest.raises(ValueError):
        metadata.patch("abc", bogus="x")


def test_concurrent_writers_do_not_corrupt(tmp_home):
    """Two threads racing on the same uuid converge to a valid JSON file with both keys.

    This is the smoke test for fcntl.flock — without it, the read-modify-write
    races and one writer's data is lost OR the file ends up half-written.
    """
    errors: list[BaseException] = []

    def writer(uid: str, n: int):
        try:
            for _ in range(n):
                metadata.patch(uid, sort_key=42)
        except BaseException as e:  # pragma: no cover  (only if test fails)
            errors.append(e)

    t1 = threading.Thread(target=writer, args=("a", 20))
    t2 = threading.Thread(target=writer, args=("b", 20))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    state = metadata.load()
    assert "a" in state and "b" in state
    # Both must have the final sort_key value (last write wins for each key).
    assert state["a"].sort_key == 42
    assert state["b"].sort_key == 42
    # Underlying file must still be valid JSON.
    raw = Path(tmp_home / ".config" / "agent-sessions" / "metadata.json").read_text()
    json.loads(raw)


def test_load_tolerates_corrupt_file(tmp_home):
    p = Path(tmp_home / ".config" / "agent-sessions" / "metadata.json")
    p.parent.mkdir(parents=True)
    p.write_text("not json {")
    assert metadata.load() == {}
