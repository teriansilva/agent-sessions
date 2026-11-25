"""Reproducible allocation/latency evidence for #1386; never reads real session history."""

import argparse
import gc
import importlib.util
import json
import os
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path

from agent_sessions import native_journal

parser = argparse.ArgumentParser(
    description="Measure native journal retention and polling on disposable synthetic history"
)
parser.add_argument(
    "--baseline", type=Path, help="An earlier native_journal.py to compare in a separate process"
)
args = parser.parse_args()
version = "baseline" if args.baseline else "working-tree"
if args.baseline:
    spec = importlib.util.spec_from_file_location("agent_sessions.baseline_journal", args.baseline)
    native_journal = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = native_journal
    spec.loader.exec_module(native_journal)

with tempfile.TemporaryDirectory(prefix="battlelab-history-bench-") as temp:
    root = Path(temp)
    sid, worker, conn, turn = (str(uuid.uuid4()) for _ in range(4))
    path = root / f"{sid}.jsonl"
    header = dict(
        type="session",
        id=sid,
        cwd=temp,
        created_at=1.0,
        request=dict(runtime="api", session_key=f"test-api:{sid}"),
        model=None,
    )
    claim = dict(
        type="native_operation",
        operation_id=turn,
        request=dict(action="submit", params=dict(text="benchmark", context={})),
        worker_id=worker,
        connection_id=conn,
        ts=2.0,
    )
    observation = dict(
        type="native_event",
        worker_id=worker,
        connection_id=conn,
        ts=3.0,
        event=dict(
            kind="text",
            data=dict(
                operation_id=turn,
                native_turn_id="native-turn",
                item_id="item",
                text="bench " * 340,
                partial=False,
                truncated=False,
            ),
        ),
    )
    with path.open("w") as file:
        for record in (header, claim):
            file.write(json.dumps(record) + "\n")
        line = json.dumps(observation) + "\n"
        for _ in range(12000):
            file.write(line)
    path.chmod(0o600)
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    view = native_journal.read(root, sid)
    cold = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    del view
    gc.collect()
    retained = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    original = os.pread
    reads = [0]
    inode = path.stat().st_ino

    def counted(fd, count, offset):
        result = original(fd, count, offset)
        if os.fstat(fd).st_ino == inode:
            reads[0] += len(result)
        return result

    os.pread = counted
    started = time.perf_counter()
    for _ in range(20):
        page = native_journal.read(root, sid).page(12002, 1)
        assert page["events"] == []
    warm = (time.perf_counter() - started) / 20
    print(
        json.dumps(
            dict(
                version=version,
                events=12000,
                journal_MiB=path.stat().st_size / 1024**2,
                retained_Python_MiB=retained / 1024**2,
                peak_Python_MiB=peak / 1024**2,
                cold_seconds_with_tracemalloc=cold,
                warm_poll_ms=warm * 1000,
                warm_journal_bytes_read=reads[0],
            )
        )
    )

    # Concurrent warmed readers retain materialized old pages plus independent indexes.
    from concurrent.futures import ThreadPoolExecutor

    gc.collect()
    tracemalloc.start()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda _: native_journal.read(root, sid).page(0, 100), range(16)))
    pages_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    del pages
    # Eight independent sessions exceed the spool-retention budget.
    raw = path.read_bytes()
    sessions = []
    for _ in range(8):
        another = str(uuid.uuid4())
        target = root / f"{another}.jsonl"
        target.write_bytes(raw.replace(sid.encode(), another.encode()))
        target.chmod(0o600)
        sessions.append(another)
    del raw
    native_journal._CACHE.clear()
    gc.collect()
    tracemalloc.start()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda item: len(native_journal.read(root, item).events), sessions):
            assert result == 12000
    gc.collect()
    retained_many, peak_many = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    entries = list(native_journal._CACHE.values())
    print(
        json.dumps(
            dict(
                version=version,
                four_concurrent_readers_16_pages_peak_MiB=pages_peak / 1024**2,
                eight_sessions_retained_MiB=retained_many / 1024**2,
                four_cold_readers_peak_MiB=peak_many / 1024**2,
                cache_entries=len(entries),
                cached_spool_MiB=(
                    sum(e.journal.events.disk_bytes for e in entries) / 1024**2
                    if not args.baseline
                    else None
                ),
            )
        )
    )
