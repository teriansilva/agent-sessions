"""The per-file walk memo (#1048), and the invariant that keeps a full walk off the event loop.

Every test here is about a MISS that must happen. A memo that never serves a stale row but also
never hits is merely slow; a memo that hits when the bytes changed is wrong. So the hit case gets
one test and the miss cases get the rest.
"""

from __future__ import annotations

import ast
import os
import pathlib
import time

import pytest

from agent_sessions import scancache


@pytest.fixture(autouse=True)
def _clean_memo():
    scancache.clear()
    yield
    scancache.clear()


def _touch(p: pathlib.Path, text: str) -> pathlib.Path:
    """Write `text` and backdate the file past the racy window.

    Every test that expects a HIT must do this, and that is not test scaffolding noise — it is the
    guarantee being asserted. A file written *just now* is deliberately not memoised (see
    `RACY_WINDOW_NS`), so without the backdating these tests would pass for the wrong reason and a
    broken identity key would still look green.
    """
    p.write_text(text)
    return _age(p)


def _age(p: pathlib.Path, seconds: float = 5.0) -> pathlib.Path:
    ns = time.time_ns() - int(seconds * 1_000_000_000)
    os.utime(p, ns=(ns, ns))
    return p


class _Counter:
    """A builder that records how many times it actually ran."""

    def __init__(self, value="row"):
        self.calls = 0
        self.value = value

    def __call__(self):
        self.calls += 1
        return self.value


# --- the one hit case ---------------------------------------------------------------------


def test_an_unchanged_file_is_built_once(tmp_path):
    f = _touch(tmp_path / "a.jsonl", "hello")
    build = _Counter()
    first = scancache.memoized("ns", f, build)
    second = scancache.memoized("ns", f, build)
    assert (first, second) == ("row", "row")
    assert build.calls == 1


def test_a_none_verdict_is_memoised_too(tmp_path):
    """ "Not a listable session" is the EXPENSIVE answer on a codex rollout with no user turn —
    it is the verdict that used to cost a cover-to-cover read. Caching only truthy rows would
    leave exactly the pathological case uncached."""
    f = _touch(tmp_path / "a.jsonl", "hello")
    build = _Counter(value=None)
    assert scancache.memoized("ns", f, build) is None
    assert scancache.memoized("ns", f, build) is None
    assert build.calls == 1


# --- the miss cases -----------------------------------------------------------------------


def test_changed_content_misses(tmp_path):
    """Both versions aged, so the rebuild is the identity key at work, not the racy rule."""
    f = _touch(tmp_path / "a.jsonl", "hello")
    build = _Counter()
    scancache.memoized("ns", f, build)
    _touch(f, "hello world")
    scancache.memoized("ns", f, build)
    assert build.calls == 2


def test_same_size_rewrite_misses_on_mtime(tmp_path):
    """A rewrite that keeps the byte count is the case a size-only key would serve stale.

    Both versions are aged past the racy window, so the miss can ONLY be the timestamp — otherwise
    this would pass on the racy rule alone and say nothing about the key.
    """
    f = _touch(tmp_path / "a.jsonl", "aaaaa")
    build = _Counter()
    scancache.memoized("ns", f, build)
    before = f.stat()

    f.write_text("bbbbb")  # identical length
    _age(f, seconds=3.0)  # aged, but to a DIFFERENT instant than `before`
    after = f.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns != before.st_mtime_ns

    scancache.memoized("ns", f, build)
    assert build.calls == 2


def test_a_file_written_just_now_is_not_memoised(tmp_path):
    """The racy-clean rule (#1048). A filesystem timestamp has a granularity — 1 ms on the target
    host's ext4 — so a modification inside the current tick that also preserves `st_size` would be
    invisible. A file that has not been quiet for `RACY_WINDOW_NS` is therefore never stored."""
    f = tmp_path / "a.jsonl"
    f.write_text("aaaaa")  # NOT aged
    build = _Counter()
    scancache.memoized("ns", f, build)
    scancache.memoized("ns", f, build)
    assert build.calls == 2
    assert scancache.stats()["entries"] == 0

    # The very same bytes, once the file has gone quiet, do memoise.
    _age(f)
    scancache.memoized("ns", f, build)
    scancache.memoized("ns", f, build)
    assert build.calls == 3


def test_same_size_and_same_mtime_still_misses_when_the_inode_changed(tmp_path):
    """A path is a reusable name. Delete + recreate with the same bytes and a restored mtime is
    indistinguishable from "unchanged" unless the identity carries the inode."""
    f = _touch(tmp_path / "a.jsonl", "aaaaa")
    st = f.stat()
    build = _Counter()
    scancache.memoized("ns", f, build)

    # Build the replacement under another name and RENAME it into place: unlink-then-recreate
    # hands the same inode straight back on ext4 (measured — the loop this replaces skipped every
    # run and asserted nothing), while a rename is guaranteed to install a different one.
    other = tmp_path / "b.jsonl"
    other.write_text("aaaaa")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert other.stat().st_ino != st.st_ino
    other.rename(f)

    now = f.stat()
    assert now.st_ino != st.st_ino
    assert now.st_size == st.st_size
    assert now.st_mtime_ns == st.st_mtime_ns
    scancache.memoized("ns", f, build)
    assert build.calls == 2


def test_namespaces_do_not_share_entries(tmp_path):
    """`_walk` reads the same builder for the live and archived trees, and `archived` lands IN the
    row. Two readers must never be served each other's answer."""
    f = _touch(tmp_path / "a.jsonl", "hello")
    live, arch = _Counter("live"), _Counter("archived")
    assert scancache.memoized("claude:live", f, live) == "live"
    assert scancache.memoized("claude:archived", f, arch) == "archived"
    assert (live.calls, arch.calls) == (1, 1)


def test_a_file_that_changes_during_the_build_is_returned_but_not_stored(tmp_path):
    """The window between the pre-read `stat` and the builder's own read. Caching there would file
    a row built from the NEW bytes under the OLD identity — and a later walk that saw that identity
    again would be served a row that never described it."""
    f = _touch(tmp_path / "a.jsonl", "hello")
    before = f.stat()

    calls = []

    def racing_build():
        calls.append(1)
        # The file moves on while we are "reading" it — and lands aged, so the ONLY thing that can
        # keep this row out of the store is the before/after identity comparison.
        _touch(f, "hello, much later and longer")
        return f"row{len(calls)}"

    assert scancache.memoized("ns", f, racing_build) == "row1"
    assert scancache.stats()["entries"] == 0

    # Put the file back to EXACTLY its original identity. If the racing row had been stored, this
    # would serve it; it must rebuild instead.
    f.write_text("hello")
    os.utime(f, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert f.stat().st_size == before.st_size
    assert f.stat().st_mtime_ns == before.st_mtime_ns
    assert scancache.memoized("ns", f, racing_build) == "row2"


def test_an_unstattable_path_builds_and_does_not_cache(tmp_path):
    missing = tmp_path / "gone.jsonl"
    build = _Counter()
    assert scancache.memoized("ns", missing, build) == "row"
    assert scancache.memoized("ns", missing, build) == "row"
    assert build.calls == 2
    assert scancache.stats()["entries"] == 0


# --- the bound ----------------------------------------------------------------------------


def test_the_entry_cap_is_enforced_and_evicts_the_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(scancache, "MAX_ENTRIES", 3)
    files = [_touch(tmp_path / f"{i}.jsonl", str(i)) for i in range(4)]
    for f in files[:3]:
        scancache.memoized("ns", f, _Counter())
    assert scancache.stats()["entries"] == 3

    # Touch the oldest so it is no longer the LRU victim, then overflow.
    scancache.memoized("ns", files[0], _Counter())
    scancache.memoized("ns", files[3], _Counter())
    assert scancache.stats()["entries"] == 3

    # files[1] was the least recently used, so it is the one that must have gone.
    probe = _Counter()
    scancache.memoized("ns", files[1], probe)
    assert probe.calls == 1
    kept = _Counter()
    scancache.memoized("ns", files[0], kept)
    assert kept.calls == 0


def test_stats_counts_hits_and_misses(tmp_path):
    f = _touch(tmp_path / "a.jsonl", "hello")
    scancache.memoized("ns", f, _Counter())
    scancache.memoized("ns", f, _Counter())
    s = scancache.stats()
    assert (s["hits"], s["misses"], s["entries"]) == (1, 1, 1)


# --- the route invariant --------------------------------------------------------------------

_WALKS = {"scan_all", "scan_all_cached", "scan_all_pinned"}
_ROUTES = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_sessions" / "routes"


def _direct_walk_calls(fn: ast.AST) -> list[str]:
    """`engines.<walk>()` calls in ``fn``'s OWN body — not inside a nested function or lambda.

    The nesting is the point, not an implementation detail: the only sanctioned way to walk the
    store from a route is to hand a blocking builder to ``asyncio.to_thread``, and that builder is
    a nested ``def`` or ``lambda``. So "called without crossing a function boundary" is exactly
    "called on the event loop".
    """
    found: list[str] = []
    boundary = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, boundary):
                continue  # a nested function IS the asyncio.to_thread boundary
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _WALKS
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "engines"
            ):
                found.append(child.func.attr)
            visit(child)

    for stmt in fn.body:
        if isinstance(stmt, boundary):
            continue
        visit(stmt)
    return found


def _get_route_handlers(tree: ast.AST):
    """Every coroutine decorated with ``@app.get`` — the READ routes."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr == "get":
                yield node


def test_no_read_route_walks_the_store_on_the_event_loop():
    """#1048's regression guard.

    ``/api/projects`` and ``/api/folders`` each ran ``engines.scan_all()`` — uncached, and inline
    in the coroutine — so opening Settings -> Projects paid a full cold walk AND stalled every
    terminal WebSocket for its duration. ``/api/sessions`` has done this correctly since #678; the
    rule is only worth anything if it is checked.

    Scoped to ``@app.get`` deliberately. The two bulk-archive MUTATION routes still walk inline and
    are left alone by this PR: their loops call into the mission reservation and runtime-cleanup
    paths, so moving them off the loop is a concurrency change, not a caching one. Tracked on
    #1048 rather than widened into it.
    """
    offenders: list[str] = []
    for path in sorted(_ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for handler in _get_route_handlers(tree):
            for attr in _direct_walk_calls(handler):
                offenders.append(f"{path.name}:{handler.lineno} {handler.name} -> engines.{attr}()")
    assert offenders == [], (
        "a GET route walks the whole session store in its own coroutine body; move it into a "
        "blocking builder handed to asyncio.to_thread:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_a_regression():
    """The guard above passes trivially if its matcher is broken, so prove it fires."""
    tree = ast.parse("async def r():\n" "    for s in engines.scan_all():\n" "        pass\n")
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    assert _direct_walk_calls(handler) == ["scan_all"]

    threaded = ast.parse(
        "async def r():\n"
        "    def _build():\n"
        "        return list(engines.scan_all_cached())\n"
        "    return await asyncio.to_thread(_build)\n"
    )
    handler = next(n for n in ast.walk(threaded) if isinstance(n, ast.AsyncFunctionDef))
    assert _direct_walk_calls(handler) == []
