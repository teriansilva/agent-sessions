"""#991: single-key session resolution for the terminal route, and the one walk coordinator.

Two contracts live here.

**Lookup parity.** ``EngineProvider.lookup(native)`` reads ONE session, fresh, instead of walking
the whole store. It must return exactly the row ``scan()`` produces for that id — every scan filter
included — or ``None`` for an item ``scan()`` drops. A lookup that disagreed with the scan would
turn a hidden session into an attachable one, so each provider is pinned against its own scan.

**Generations.** ``scan_all_cached()`` (the sidebar's TTL snapshot) and ``scan_all_since(arrival)``
(the fresh fallback for providers without a lookup) share ONE coordinator per home: walks never
overlap, a caller that arrived before a walk started shares it, a caller that arrived after joins
the single next walk, and a failed walk releases every waiter. The timing tests record arrivals and
walk starts explicitly rather than relying on threads happening to start together.

The autouse ``_isolate_scan_cache`` fixture sets the TTL to 0; tests that need the snapshot set it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from agent_sessions import engines
from agent_sessions.scanner import Session

# conftest's opencode fixture ids (the DB is built by the ``opencode_db`` fixture).
OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"
OC_FORK = "ses_ffffffffffffffffffffffff"
OC_ACT = "ses_cccccccccccccccccccccccc"

_MISSING_UUID = "99999999-9999-4999-8999-999999999999"
_GEM = "0a0a0a0a-0b0b-4c0c-8d0d-0e0e0e0e0e0e"


def _parity(prov, *, expected: set[str], filtered: set[str]) -> None:
    """``lookup(id)`` equals the first ``scan()`` row for that id, for every scanned id; and is
    ``None`` for every id the scan drops."""
    first: dict[str, Session] = {}
    for row in prov.scan():
        first.setdefault(row.uuid, row)
    assert expected <= set(first), f"fixture drift: scan lacks {expected - set(first)}"
    for sid, row in first.items():
        assert prov.lookup(sid) == row, f"{prov.engine_id}: lookup({sid}) disagrees with scan"
    for sid in filtered:
        assert sid not in first, f"fixture drift: scan unexpectedly lists {sid}"
        assert prov.lookup(sid) is None, f"{prov.engine_id}: lookup({sid}) resolved a dropped item"


# --- claude --------------------------------------------------------------------------------------

_HEADLESS = "66666666-6666-4666-8666-666666666666"
_BOTH = "77777777-7777-4777-8777-777777777777"
_ARCH_HEADLESS = "88888888-8888-4888-8888-888888888888"


def _claude_store(tmp_home: Path) -> None:
    projects = tmp_home / ".claude" / "projects"
    archive = tmp_home / ".claude" / "projects-archive"
    # A `claude -p` one-shot is not a session (scanner skips `entrypoint: "sdk-cli"`).
    (projects / "-home-user-claude-repo-a" / f"{_HEADLESS}.jsonl").write_text(
        '{"type":"user","entrypoint":"sdk-cli","message":{"content":"probe"}}\n'
    )
    # Present in BOTH trees: the archived copy wins (#194), so the row is the archived one.
    (projects / "-tmp-other" / f"{_BOTH}.jsonl").write_text(
        '{"type":"user","message":{"content":"live copy"}}\n'
    )
    (archive / "-home-user-claude-old" / f"{_BOTH}.jsonl").write_text(
        '{"type":"user","message":{"content":"archived copy"}}\n'
    )
    # An archived copy that is itself a one-shot does not hide the live session.
    (projects / "-tmp-other" / f"{_ARCH_HEADLESS}.jsonl").write_text(
        '{"type":"user","cwd":"/tmp/other","message":{"content":"live, real"}}\n'
    )
    (archive / "-home-user-claude-old" / f"{_ARCH_HEADLESS}.jsonl").write_text(
        '{"type":"user","entrypoint":"sdk-cli","message":{"content":"probe"}}\n'
    )


def test_claude_lookup_matches_scan(fake_jsonl, tmp_home):
    _claude_store(tmp_home)
    prov = engines.ClaudeProvider()
    _parity(
        prov,
        expected={
            "11111111-1111-1111-1111-111111111111",
            "44444444-4444-4444-4444-444444444444",
            "55555555-5555-5555-5555-555555555555",
            _BOTH,
            _ARCH_HEADLESS,
        },
        filtered={_HEADLESS, _MISSING_UUID},
    )
    assert prov.lookup(_BOTH).archived is True
    assert prov.lookup(_ARCH_HEADLESS).archived is False
    assert prov.lookup("55555555-5555-5555-5555-555555555555").cwd == "/home/user/claude/demoapp.io"
    assert prov.lookup("not-a-uuid") is None
    assert prov.lookup("../../etc/passwd") is None


def test_claude_lookup_sees_a_session_that_just_landed_without_a_walk(
    fake_jsonl, tmp_home, monkeypatch
):
    """A fresh session's JSONL written after the sidebar snapshot was taken resolves through the
    single-key read, and no full walk runs to find it."""
    real = engines.scan_all
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(engines, "scan_all", counting)
    engines.set_scan_cache_ttl(30.0)
    engines.scan_all_cached()
    assert calls["n"] == 1

    fresh = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    (tmp_home / ".claude" / "projects" / "-tmp-other" / f"{fresh}.jsonl").write_text(
        '{"type":"user","cwd":"/tmp/other","message":{"content":"just started"}}\n'
    )
    row = engines.resolve_session("claude", fresh)
    assert row is not None and row.cwd == "/tmp/other"
    assert calls["n"] == 1, "resolving a fresh claude session walked the store"


# --- codex ---------------------------------------------------------------------------------------

_CDX = "019e2ba1-1590-7003-8e4a-51ab62cec96e"
_CDX_ODD = "019e2ba1-1590-7003-8e4a-51ab62cec96f"
_CDX_SUB = "019e2ba1-1590-7003-8e4a-51ab62cec970"
_CDX_NOCWD = "019e2ba1-1590-7003-8e4a-51ab62cec971"


def _rollout(root: Path, uuid: str, cwd: str | None, *, day="2026/05/15", meta_extra=None):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    payload: dict = {"id": uuid}
    if cwd:
        payload["cwd"] = cwd
    payload.update(meta_extra or {})
    lines = [
        {"timestamp": "2026-05-15T15:33:57Z", "type": "session_meta", "payload": payload},
        {
            "timestamp": "t",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "hi"},
        },
    ]
    f = d / f"rollout-2026-05-15T15-33-57-{uuid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.fixture
def codex_store(tmp_home, monkeypatch) -> Path:
    root = tmp_home / ".codex" / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    _rollout(root, _CDX, "/home/user/proj")
    # Not in the dated YYYY/MM/DD layout: scan's rglob still finds it, so lookup must too.
    _rollout(root, _CDX_ODD, "/home/user/odd", day="imported")
    # A spawned subagent thread is not a session (#821).
    _rollout(root, _CDX_SUB, "/home/user/proj", meta_extra={"thread_source": "subagent"})
    # No cwd → no row.
    _rollout(root, _CDX_NOCWD, None)
    return root


def test_codex_lookup_matches_scan(codex_store):
    _parity(
        engines.CodexProvider(),
        expected={_CDX, _CDX_ODD},
        filtered={_CDX_SUB, _CDX_NOCWD, _MISSING_UUID},
    )


_CDX_SPLIT = "019e2ba1-1590-7003-8e4a-51ab62cec972"


def test_codex_lookup_falls_through_an_unusable_dated_copy(codex_store):
    """A dated rollout that is not a listable session (no cwd) must not hide a valid copy of the
    same id stored elsewhere: ``scan()``'s rglob lists the valid copy, so ``lookup()`` must too."""
    _rollout(codex_store, _CDX_SPLIT, None)
    _rollout(codex_store, _CDX_SPLIT, "/home/user/imported", day="imported")
    _parity(
        engines.CodexProvider(),
        expected={_CDX, _CDX_ODD, _CDX_SPLIT},
        filtered={_CDX_SUB, _CDX_NOCWD, _MISSING_UUID},
    )
    assert engines.CodexProvider().lookup(_CDX_SPLIT).cwd == "/home/user/imported"


# --- opencode ------------------------------------------------------------------------------------


def test_opencode_lookup_matches_scan(opencode_db):
    prov = engines.OpenCodeProvider()
    _parity(
        prov,
        expected={OC_TOP, OC_ARCHIVED},
        filtered={OC_FORK, OC_ACT, "ses_zzzzzzzzzzzzzzzzzzzzzzzz"},
    )
    assert prov.lookup("not-a-ses-id") is None


def test_opencode_lookup_is_fail_soft(tmp_home, monkeypatch):
    db = tmp_home / "broken.db"
    db.write_bytes(b"this is not a sqlite database at all" * 8)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    assert engines.OpenCodeProvider().lookup(OC_TOP) is None
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / "absent.db"))
    assert engines.OpenCodeProvider().lookup(OC_TOP) is None


# --- shell ---------------------------------------------------------------------------------------

_SH_A = "5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a"
_SH_B = "5b5b5b5b-5b5b-4b5b-8b5b-5b5b5b5b5b5b"
_SH_NOCWD = "5c5c5c5c-5c5c-4c5c-8c5c-5c5c5c5c5c5c"
_SH_BAD = "5d5d5d5d-5d5d-4d5d-8d5d-5d5d5d5d5d5d"


def test_shell_lookup_matches_scan(tmp_home):
    d = tmp_home / ".claude" / "shell-sessions"
    d.mkdir(parents=True)
    for sid, cwd in ((_SH_A, "/home/user/a"), (_SH_B, "/home/user/b"), (_SH_NOCWD, "")):
        (d / f"{sid}.json").write_text(json.dumps({"id": sid, "cwd": cwd, "created_at": 1.7e9}))
    (d / f"{_SH_BAD}.json").write_text("{not json")
    _parity(
        engines.ShellProvider(),
        expected={_SH_A, _SH_B},
        filtered={_SH_NOCWD, _SH_BAD, _MISSING_UUID},
    )


# --- antigravity ---------------------------------------------------------------------------------

_AG_BLOB = "7f0ee6e0-1467-4f7d-843b-4e70e15e73f5"
_AG_CACHE = "11112222-3333-4444-5555-666677778888"
_AG_NOCWD = "22223333-4444-4555-8666-777788889999"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _agy_db(path: Path, cwd: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE `trajectory_metadata_blob` "
        "(`id` text DEFAULT 'main', `data` blob, PRIMARY KEY(`id`))"
    )
    if cwd is not None:
        uri = f"file://{cwd}".encode()
        blob = b"\n\x26\n" + _varint(len(uri)) + uri + b"z\xe8\x07"
        con.execute("INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)", (blob,))
    con.commit()
    con.close()


def test_antigravity_lookup_matches_scan(tmp_home, monkeypatch):
    root = tmp_home / ".gemini" / "antigravity-cli"
    monkeypatch.setenv("AGENT_SESSIONS_ANTIGRAVITY_DIR", str(root))
    _agy_db(root / "conversations" / f"{_AG_BLOB}.db", "/home/user/agy-blob")
    _agy_db(root / "conversations" / f"{_AG_CACHE}.db", None)
    _agy_db(root / "conversations" / f"{_AG_NOCWD}.db", None)
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "last_conversations.json").write_text(
        json.dumps({"/home/user/agy-cache": _AG_CACHE})
    )
    _parity(
        engines.AntigravityProvider(),
        expected={_AG_BLOB, _AG_CACHE},
        filtered={_AG_NOCWD, _MISSING_UUID},
    )


# --- kimi ----------------------------------------------------------------------------------------

_KM_WALK = "session_25f66293-9603-46af-bbf3-bd79ef84ca54"
_KM_INDEX_ONLY = "session_aaaabbbb-cccc-dddd-eeee-ffff00001111"
_KM_STALE = "session_bbbbcccc-dddd-eeee-ffff-000011112222"
_KM_NOSTATE = "session_ccccdddd-eeee-ffff-0000-111122223333"


def _kimi_session(sdir: Path, cwd: str | None) -> Path:
    sdir.mkdir(parents=True, exist_ok=True)
    if cwd is not None:
        (sdir / "state.json").write_text(
            json.dumps(
                {
                    "createdAt": "2026-07-19T14:19:03.061Z",
                    "updatedAt": "2026-07-19T15:20:04.500Z",
                    "title": "Kimi work",
                    "workDir": cwd,
                }
            )
        )
    return sdir


def test_kimi_lookup_matches_scan(tmp_home, monkeypatch):
    root = tmp_home / ".kimi-code"
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(root))
    walk_dir = _kimi_session(root / "sessions" / "wd_proj_dead" / _KM_WALK, "/home/user/kimi")
    # Indexed but living outside the sessions/ tree: only the index can place it.
    elsewhere = _kimi_session(tmp_home / "kimi-elsewhere" / _KM_INDEX_ONLY, "/home/user/kimi2")
    _kimi_session(root / "sessions" / "wd_proj_dead" / _KM_NOSTATE, None)
    rows = [
        {"sessionId": _KM_WALK, "sessionDir": str(walk_dir), "workDir": "/home/user/kimi"},
        {"sessionId": _KM_INDEX_ONLY, "sessionDir": str(elsewhere), "workDir": "/home/user/kimi2"},
        {"sessionId": _KM_STALE, "sessionDir": str(root / "gone" / _KM_STALE), "workDir": "/x"},
    ]
    (root / "session_index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    _parity(
        engines.KimiProvider(),
        expected={_KM_WALK, _KM_INDEX_ONLY},
        filtered={_KM_STALE, _KM_NOSTATE},
    )


# --- resolver: no walk for single-key providers --------------------------------------------------


def test_eight_concurrent_resolves_per_lookup_engine_make_no_full_walk(
    fake_jsonl, opencode_db, codex_store, tmp_home, monkeypatch
):
    """The map opens eight windows at once. For every provider with a single-key read, eight
    concurrent resolutions must perform ZERO full walks (today's route performs eight)."""
    sh = tmp_home / ".claude" / "shell-sessions"
    sh.mkdir(parents=True)
    (sh / f"{_SH_A}.json").write_text(json.dumps({"id": _SH_A, "cwd": "/home/user/a"}))
    agy = tmp_home / ".gemini" / "antigravity-cli"
    monkeypatch.setenv("AGENT_SESSIONS_ANTIGRAVITY_DIR", str(agy))
    _agy_db(agy / "conversations" / f"{_AG_BLOB}.db", "/home/user/agy-blob")
    kimi = tmp_home / ".kimi-code"
    monkeypatch.setenv("AGENT_SESSIONS_KIMI_DIR", str(kimi))
    _kimi_session(kimi / "sessions" / "wd_proj_dead" / _KM_WALK, "/home/user/kimi")

    targets = {
        "claude": "11111111-1111-1111-1111-111111111111",
        "codex": _CDX,
        "opencode": OC_TOP,
        "shell": _SH_A,
        "antigravity": _AG_BLOB,
        "kimi": _KM_WALK,
    }
    expected = {
        eng: next(r for r in engines.get(eng).scan() if r.uuid == sid)
        for eng, sid in targets.items()
    }

    walks = {"n": 0}

    def forbidden_walk():
        walks["n"] += 1
        return []

    monkeypatch.setattr(engines, "scan_all", forbidden_walk)
    engines.set_scan_cache_ttl(30.0)

    for eng, sid in targets.items():
        barrier = threading.Barrier(8)
        results: list[Session | None] = [None] * 8

        def worker(i, eng=eng, sid=sid, barrier=barrier, results=results):
            barrier.wait()
            results[i] = engines.resolve_session(eng, sid)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [expected[eng]] * 8, f"{eng} did not resolve consistently"
    assert walks["n"] == 0, f"{walks['n']} full walk(s) for single-key providers"


# --- the walk coordinator: deterministic generation tests ----------------------------------------


class _GatedWalk:
    """A stand-in for ``scan_all``: counts walks, tracks overlap, records each walk's start, and
    blocks until the test opens the gate. Each walk returns one gemini row whose ``created_at``
    carries the walk number, so a caller can tell which generation answered it."""

    def __init__(self, *, fail_first: bool = False, uuid: str = _GEM):
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.starts: list[float] = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.fail_first = fail_first
        self.uuid = uuid

    def __call__(self) -> list[Session]:
        with self._lock:
            self.calls += 1
            n = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.starts.append(time.monotonic())
        self.entered.set()
        try:
            assert self.gate.wait(10), "the test never opened the gate"
            if self.fail_first and n == 1:
                raise RuntimeError("walk failed")
            return [
                Session(
                    engine="gemini",
                    uuid=self.uuid,
                    cwd="/home/user/gem",
                    last_mtime=0.0,
                    first_user_message="",
                    archived=False,
                    created_at=float(n),
                )
            ]
        finally:
            with self._lock:
                self.active -= 1


def _run_threads(n, target):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    return threads


def test_gemini_existing_key_arrivals_before_the_walk_share_exactly_one(monkeypatch):
    """(a) Eight arrivals, all recorded BEFORE the walk starts, are answered by one walk."""
    walk = _GatedWalk()
    monkeypatch.setattr(engines, "scan_all", walk)
    arrivals = [time.monotonic() for _ in range(8)]
    results: list[Session | None] = [None] * 8

    def worker(i):
        results[i] = engines.resolve_session("gemini", _GEM, arrival=arrivals[i])

    threads = _run_threads(8, worker)
    assert walk.entered.wait(5)
    walk.gate.set()
    for t in threads:
        t.join()
    assert walk.calls == 1
    assert walk.starts[0] >= max(arrivals)
    assert all(r is not None and r.uuid == _GEM and r.created_at == 1.0 for r in results)


def test_gemini_missing_key_arrivals_before_the_walk_share_exactly_one(monkeypatch):
    """(b) Same set-up for an id the walk does not contain: one walk, and every caller gets
    ``None`` (which the route turns into today's rejection)."""
    walk = _GatedWalk(uuid="1b1b1b1b-1b1b-4b1b-8b1b-1b1b1b1b1b1b")
    monkeypatch.setattr(engines, "scan_all", walk)
    arrivals = [time.monotonic() for _ in range(8)]
    results: list[object] = ["unset"] * 8

    def worker(i):
        results[i] = engines.resolve_session("gemini", _GEM, arrival=arrivals[i])

    threads = _run_threads(8, worker)
    assert walk.entered.wait(5)
    walk.gate.set()
    for t in threads:
        t.join()
    assert walk.calls == 1
    assert results == [None] * 8


def test_arrivals_after_a_walk_started_share_one_next_generation(monkeypatch):
    """(c) A caller that arrives while a walk it cannot trust is in flight waits for it, then
    joins the single NEXT walk — shared by every such caller."""
    walk = _GatedWalk()
    monkeypatch.setattr(engines, "scan_all", walk)
    first: list[list[Session]] = []
    early = threading.Thread(target=lambda: first.append(engines.scan_all_since(time.monotonic())))
    early.start()
    assert walk.entered.wait(5)

    late_arrival = time.monotonic()
    assert late_arrival > walk.starts[0]
    late: list[list[Session] | None] = [None] * 5

    def worker(i):
        late[i] = engines.scan_all_since(late_arrival)

    threads = _run_threads(5, worker)
    walk.gate.set()
    early.join()
    for t in threads:
        t.join()

    assert walk.calls == 2, f"expected one shared next walk, saw {walk.calls} walks"
    assert walk.starts[1] >= late_arrival
    assert first[0][0].created_at == 1.0
    assert all(r is not None and r[0].created_at == 2.0 for r in late)
    assert walk.max_active == 1


def test_sidebar_and_fallback_walks_never_overlap(monkeypatch):
    """(d) The sidebar's cached reads, invalidations and fresh fallback reads, interleaved from
    many threads, never run two walks at once."""
    walk = _GatedWalk()
    walk.gate.set()

    def slow_walk():
        time.sleep(0.005)
        return walk()

    monkeypatch.setattr(engines, "scan_all", slow_walk)
    engines.set_scan_cache_ttl(30.0)
    errors: list[BaseException] = []

    def worker(i):
        try:
            for j in range(15):
                kind = (i + j) % 3
                if kind == 0:
                    engines.scan_all_cached()
                elif kind == 1:
                    engines.scan_all_since(time.monotonic())
                else:
                    engines.invalidate_scan_cache()
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    for t in _run_threads(9, worker):
        t.join()
    assert not errors, errors
    assert walk.calls >= 2, "the test never exercised a walk"
    assert walk.max_active == 1, f"{walk.max_active} walks ran concurrently"


def test_a_failed_walk_releases_every_waiter_and_the_next_call_walks_again(monkeypatch):
    """(e) A walk that raises hands the error to every caller waiting on it (nobody is stranded),
    and the next call starts a new generation instead of inheriting the failure."""
    walk = _GatedWalk(fail_first=True)
    monkeypatch.setattr(engines, "scan_all", walk)
    arrivals = [time.monotonic() for _ in range(4)]
    outcomes: list[object] = [None] * 4

    def worker(i):
        try:
            outcomes[i] = engines.scan_all_since(arrivals[i])
        except RuntimeError as exc:
            outcomes[i] = exc

    threads = _run_threads(4, worker)
    assert walk.entered.wait(5)
    walk.gate.set()
    for t in threads:
        t.join()
    assert walk.calls == 1
    assert all(isinstance(o, RuntimeError) for o in outcomes), outcomes

    rows = engines.scan_all_since(time.monotonic())
    assert walk.calls == 2 and rows[0].created_at == 2.0
    # Through the resolver a failed walk is "not found", never an exception into the route.
    walk2 = _GatedWalk(fail_first=True)
    walk2.gate.set()
    monkeypatch.setattr(engines, "scan_all", walk2)
    assert engines.resolve_session("gemini", _GEM) is None


def test_invalidation_forces_a_fresh_generation_for_the_fallback(monkeypatch):
    """A completed walk that started after arrival is normally reusable — but not once the tree
    has been invalidated since, because the invalidation means the store changed."""
    walk = _GatedWalk()
    walk.gate.set()
    monkeypatch.setattr(engines, "scan_all", walk)
    arrival = time.monotonic()
    engines.scan_all_since(arrival)
    engines.scan_all_since(arrival)
    assert walk.calls == 1
    engines.invalidate_scan_cache()
    engines.scan_all_since(arrival)
    assert walk.calls == 2
