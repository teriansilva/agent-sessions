#!/usr/bin/env python3
"""Reproducible synthetic Memory qualification (#2036). Never opens an operator notebook.

Run with the project's Python environment and a NEW --root directory. `service` measures the
complete keyword query path including scope checks and authoritative hydration. `native`
qualifies filtered/global ANN recall against exact synthetic-vector neighbours. Neither is a
semantic-model quality evaluation; see docs/memory.md for that separate corpus and report.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import resource
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path


def percentile(values, p=0.95):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def disk_bytes(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def configure(root):
    root.mkdir(mode=0o700)  # deliberately refuses an existing directory, including real stores
    for key, value in {
        "AGENT_SESSIONS_HOME": "home",
        "AGENT_SESSIONS_MEMORY_DIR": "memory",
        "AGENT_SESSIONS_PREFS": "prefs.json",
        "AGENT_SESSIONS_PROJECTS": "projects.json",
        "AGENT_SESSIONS_METADATA": "metadata.json",
        "AGENT_SESSIONS_LOCK_DIR": "locks",
        "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY": "secret-key",
    }.items():
        os.environ[key] = str(root / value)
    os.environ["LANCE_CPU_THREADS"] = "2"
    os.environ["RAYON_NUM_THREADS"] = "2"
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, set(sorted(os.sched_getaffinity(0))[:2]))
    (root / "SYNTHETIC.json").write_text(json.dumps({"issue": 2036, "operator_data": False}))


def service_probe(root, count, note_chars=120):
    from agent_sessions import memory, memory_index, memory_service, projects, template_secrets

    # This isolated fixture contains no credentials. Avoid unrelated inventories and record the
    # limitation: the benchmark includes fencing/hydration, not a large secret-inventory scan.
    template_secrets.redaction_values = lambda: []
    scopes = []
    seed_started = time.monotonic()
    for offset in range(0, count, memory.SCOPE_MAX_NOTES):
        work = root / f"project-{offset}"
        work.mkdir()
        pid = projects.create(f"Fixture {offset}", folders=[str(work)]).id
        scopes.append(pid)
        with memory._scope_lock(pid):
            memory._create_scope(pid)
        conn = memory._connect(pid)
        try:
            with memory._txn(conn):
                memory._set_meta(conn, "enabled", "1")
                conn.executemany(
                    "INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            f"note-{i:06d}",
                            f"Deployment protocol {i}",
                            "Synthetic fixture",
                            "finding",
                            (
                                "Deployment permission protocol requires revision checks. "
                                f"Marker ref{i:06d}. "
                                + (
                                    "Synthetic qualification paragraph with bounded source text. "
                                    * max(0, note_chars // 58)
                                )
                            )[:note_chars],
                            str(i),
                            "fixture",
                            1,
                            1,
                        )
                        for i in range(offset, min(count, offset + memory.SCOPE_MAX_NOTES))
                    ],
                )
        finally:
            conn.close()
    seeded = time.monotonic() - seed_started
    started = time.monotonic()
    passes = 0
    while True:
        more = memory_index.sync()
        passes += 1
        if not more:
            break
        if passes > 1000:
            raise RuntimeError("index did not catch up")
    indexed = time.monotonic() - started
    operator = memory_service.operator()
    worker = memory_service.worker([scopes[-1]])

    def query(scoped=False):
        before = time.perf_counter()
        result = memory_service.search(worker if scoped else operator, "deployment protocol")
        elapsed = (time.perf_counter() - before) * 1000
        assert result["hits"] and result["complete"]
        if scoped:
            assert all(hit["scope"] == scopes[-1] for hit in result["hits"])
        return elapsed

    cold = query()
    global_times = [query() for _ in range(50)]
    scoped_times = [query(True) for _ in range(50)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        concurrent_times = list(pool.map(query, [i % 2 == 0 for i in range(60)]))
    with memory_index._connect() as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    before = time.monotonic()
    memory.update_note(
        scopes[-1], f"note-{count - 1:06d}", {"body": "Updated fixture marker walnut."}
    )
    while memory_index.sync():
        pass
    assert memory_service.search(worker, "walnut")["hits"]
    update_s = time.monotonic() - before
    before = time.monotonic()
    memory.delete_note(scopes[-1], f"note-{count - 1:06d}")
    assert not memory_service.search(worker, "walnut")["hits"]
    while memory_index.sync():
        pass
    delete_s = time.monotonic() - before
    return {
        "source_notebooks": len(scopes),
        "source_notes": count,
        "chunks": chunks,
        "note_body_chars": note_chars,
        "seed_s": seeded,
        "initial_index_s": indexed,
        "initial_index_passes": passes,
        "first_query_ms": cold,
        "global_warm_p95_ms": percentile(global_times),
        "scoped_warm_p95_ms": percentile(scoped_times),
        "four_readers_p95_ms": percentile(concurrent_times),
        "queries": 161,
        "update_to_index_processing_s": update_s,
        "delete_to_physical_cleanup_s": delete_s,
        "default_poll_interval_s": memory_index.INTERVAL_S,
        "status": memory_index.status(),
        "limits": "Synthetic notes at the reported body/chunk sizes; empty secret inventory; "
        "keyword retrieval including "
        "principal/scope checks, current project catalog and authoritative hydration. "
        "No model or embedding latency, no semantic-quality claim.",
    }


def hybrid_probe(root, count, note_chars):
    """Scaled complete query path with precomputed fixture vectors, never model-quality evidence."""
    import contextlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent_sessions import (
        memory,
        memory_embeddings,
        memory_index,
        memory_service,
        memory_settings,
    )

    support = memory_index.vector_support()
    if not support["available"]:
        raise RuntimeError("native backend unavailable: " + support["reason"])
    import numpy as np
    import pyarrow as pa

    print(
        "[hybrid] building and measuring the keyword service fixture", file=sys.stderr, flush=True
    )
    keyword = service_probe(root, count, note_chars)
    print(
        "[hybrid] keyword fixture complete; configuring synthetic query vectors",
        file=sys.stderr,
        flush=True,
    )
    scopes = memory_index.available_scopes()
    # The keyword lifecycle probe deletes one note. Restore the exact live count with a new
    # assertion, without replaying its forgotten origin or exceeding a notebook's quota.
    for scope in scopes:
        with memory_service.readonly(scope) as source:
            if source.execute("SELECT COUNT(*) FROM notes").fetchone()[0] < memory.SCOPE_MAX_NOTES:
                restored = memory_service.submit(
                    memory_service.operator(),
                    scope,
                    title="Replacement protocol",
                    body="Deployment protocol replacement.",
                    origin="replacement-after-forget",
                )
                assert restored["state"] == "saved", restored
                break
    while memory_index.sync():
        pass
    rng, dimensions = np.random.default_rng(2036), 384
    query_vector = rng.normal(size=dimensions).astype("float32")
    query_vector /= np.linalg.norm(query_vector)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(
                {
                    "data": [
                        {"index": i, "embedding": query_vector.tolist()}
                        for i, _ in enumerate(request["input"])
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        memory_settings.save(
            {
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "synthetic-cosine-fixture-v1",
            }
        )
        print("[hybrid] loading precomputed source-bound vectors", file=sys.stderr, flush=True)
        generation = memory_settings.stored()["revision"]
        memory_settings.observe_dimensions(generation, dimensions)
        dense = memory_index._table(
            memory_index._database(create=True),
            "dense_" + generation,
            dimensions=dimensions,
            create=True,
        )
        before = time.monotonic()
        with contextlib.closing(memory_index._connect(write=True)) as conn:
            cursor = conn.execute(
                "SELECT id,scope,slug,content_digest,revision,generation,chunk,offset "
                "FROM documents ORDER BY id"
            )
            while rows := cursor.fetchmany(2048):
                vectors = rng.normal(size=(len(rows), dimensions)).astype("float32")
                vectors /= np.linalg.norm(vectors, axis=1)[:, None]
                dense.add(
                    pa.Table.from_pylist(
                        [
                            dict(row) | {"vector": vector.tolist()}
                            for row, vector in zip(rows, vectors, strict=True)
                        ],
                        schema=dense.schema,
                    )
                )
            conn.execute("UPDATE documents SET dense_generation=?", (generation,))
            memory_index._maintain_dense(conn, dense, generation)
            memory_index._set(conn, "dense_revision", generation)
        print(
            "[hybrid] native index built; checking worker convergence", file=sys.stderr, flush=True
        )
        while memory_index.sync():
            pass
        build_s = time.monotonic() - before
        assert memory_index.status()["state"] == "ready"
        print(
            "[hybrid] measuring the complete ready hybrid query path", file=sys.stderr, flush=True
        )
        assert "ANNSubIndex" in dense.search(query_vector).distance_type("cosine").explain_plan()
        local = threading.local()
        actual_embed = memory_embeddings.embed

        def timed_embed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return actual_embed(*args, **kwargs)
            finally:
                local.embedding_ms = (time.perf_counter() - started) * 1000

        def query(principal):
            local.embedding_ms = 0
            started = time.perf_counter()
            result = memory_service.search(principal, "deployment protocol", limit=8)
            elapsed = (time.perf_counter() - started) * 1000
            assert result["state"] == "ready" and result["hits"]
            if not principal.operator:
                assert {h["scope"] for h in result["hits"]} <= set(principal.scopes)
            return elapsed - local.embedding_ms, elapsed

        memory_embeddings.embed = timed_embed
        try:
            first = query(memory_service.operator())
            global_times = [query(memory_service.operator()) for _ in range(50)]
            scoped_times = [query(memory_service.worker([scopes[-1]])) for _ in range(50)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                concurrent_times = list(
                    pool.map(lambda _: query(memory_service.operator()), range(40))
                )
        finally:
            memory_embeddings.embed = actual_embed
        return {
            "keyword_baseline": keyword,
            "source_notes": count,
            "chunks": dense.count_rows(),
            "dimensions": dimensions,
            "fixture_vector_load_and_ann_build_s": build_s,
            "first_query_without_embedding_ms": first[0],
            "first_query_total_ms": first[1],
            **{
                f"{name}_p95_without_embedding_ms": percentile([t[0] for t in times])
                for name, times in (
                    ("global", global_times),
                    ("scoped", scoped_times),
                    ("four_readers", concurrent_times),
                )
            },
            **{
                f"{name}_p95_total_ms": percentile([t[1] for t in times])
                for name, times in (
                    ("global", global_times),
                    ("scoped", scoped_times),
                    ("four_readers", concurrent_times),
                )
            },
            "ann_plan_verified": True,
            "status": memory_index.status(),
            "limits": "Complete hybrid query path: real FTS/ANN, RRF, authorization and "
            "current-source hydration. Precomputed seeded random fixture vectors are loaded "
            "in bulk; local HTTP returns a fixed synthetic query vector through the actual "
            "adapter. Reports separately subtract that adapter time. This measures scaled "
            "hybrid retrieval, not semantic quality, real encoder/backfill throughput "
            "or representative query diversity. Empty registered-secret inventory.",
        }
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def native_probe(root, count):
    from agent_sessions import memory_index

    support = memory_index.vector_support()
    if not support["available"]:
        raise RuntimeError("native backend unavailable: " + support["reason"])
    import lancedb
    import numpy as np
    import pyarrow as pa
    from lancedb.index import FTS, BTree, IvfHnswFlat

    rng = np.random.default_rng(2036)
    dimensions = 384
    vectors = rng.normal(size=(count, dimensions)).astype("float32")
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    ids = np.arange(count, dtype="int64")
    rows = pa.table(
        {
            "id": ids,
            "scope": pa.array([f"p{i % 100:03d}" for i in range(count)]),
            "text": pa.array([f"Permission protocol revision ref{i:06d}" for i in range(count)]),
            "vector": pa.FixedSizeListArray.from_arrays(pa.array(vectors.ravel()), dimensions),
        }
    )
    db = lancedb.connect(root / "lance")
    before = time.monotonic()
    table = db.create_table("probe", rows)
    table.create_index("text", config=FTS(memory_limit=64 * 1024 * 1024, num_workers=1))
    table.create_index("scope", config=BTree())
    lexical_build = time.monotonic() - before
    before = time.monotonic()
    table.create_index(
        "vector",
        config=IvfHnswFlat(
            distance_type="cosine",
            num_partitions=min(64, max(1, count // 256)),
            m=16,
            ef_construction=100,
        ),
    )
    ann_build = time.monotonic() - before
    timings, recalls = {"global": [], "scoped": []}, {"global": [], "scoped": []}
    for kind in timings:
        for i in range(25):
            idx = (i * 101) % count
            allowed = np.arange(idx % 100, count, 100) if kind == "scoped" else ids
            before = time.perf_counter()
            query = table.search(vectors[idx]).distance_type("cosine").nprobes(64).ef(256).limit(10)
            if kind == "scoped":
                query = query.where(f"scope='p{idx % 100:03d}'", prefilter=True)
            if i == 0:
                assert "ANNSubIndex" in query.explain_plan()
            hits = query.to_list()
            timings[kind].append((time.perf_counter() - before) * 1000)
            truth = set(allowed[np.argsort(vectors[allowed] @ vectors[idx])[-10:]].tolist())
            recalls[kind].append(len(truth & {h["id"] for h in hits}) / len(truth))
    table.delete("id=42")
    # This is a single-process fixture, with no outstanding query reader during cleanup.
    table.optimize(cleanup_older_than=timedelta(0))
    assert not table.search().where("id=42").to_list()
    return {
        "rows": count,
        "dimensions": dimensions,
        "lexical_build_s": lexical_build,
        "ann_build_s": ann_build,
        **{f"{k}_ann_p95_ms": percentile(v) for k, v in timings.items()},
        **{f"{k}_recall_at_10": statistics.mean(v) for k, v in recalls.items()},
        "delete_and_version_cleanup": "passed",
        "ann_plan_verified": True,
        "limits": "Synthetic random vectors; exact cosine ground truth; no source hydration, "
        "authorization or embedding/model latency. Not a semantic-quality measure.",
    }


def semantic_probe(root, cache, ai_config=None, chat_model=None):
    """An explicitly installed qualification model, exposed through the real HTTP transport."""
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from importlib.metadata import version

    from fastembed import TextEmbedding

    from agent_sessions import (
        memory,
        memory_index,
        memory_service,
        memory_settings,
        template_secrets,
    )

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"  # noqa: S105 — disables credentials
    fixture = Path(__file__).resolve().parent.parent / "tests/fixtures/memory-semantic.json"
    cases = json.loads(fixture.read_text())
    encoder = TextEmbedding(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        cache_dir=str(cache),
        threads=2,
        local_files_only=True,
    )
    model_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with model_lock:
                vectors = list(encoder.embed(request["input"]))
            reply = json.dumps(
                {
                    "data": [
                        {"index": i, "embedding": vector.tolist()}
                        for i, vector in enumerate(vectors)
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_):
            pass

    template_secrets.redaction_values = lambda: []
    targets = []
    for index, (title, body, _) in enumerate(cases):
        saved = memory_service.submit(
            memory_service.operator(),
            memory.HQ_SCOPE,
            title=title,
            body=body,
            origin=f"fixture-{index}",
            source="Synthetic qualification",
            quote=body,
        )
        assert saved["state"] == "saved", saved
        targets.append(saved["slug"])
    while memory_index.sync():
        pass

    def measure():
        hits, times, failures = [], [], []
        for index, (title, _, paraphrase) in enumerate(cases):
            for kind, query in (("exact", title), ("paraphrase", paraphrase)):
                before = time.perf_counter()
                result = memory_service.search(memory_service.operator(), query, limit=10)
                times.append((time.perf_counter() - before) * 1000)
                found = targets[index] in {h["slug"] for h in result["hits"]}
                hits.append(int(found))
                if not found:
                    failures.append({"case": index, "kind": kind, "query": query})
        return {
            "recall_at_10": statistics.mean(hits),
            "p95_ms": percentile(times),
            "queries": len(hits),
            "failures": failures,
        }

    keyword = measure()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        memory_settings.save(
            {
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            }
        )
        before = time.monotonic()
        passes = 0
        while memory_index.sync():
            passes += 1
            if passes > 100:
                raise RuntimeError("semantic index did not catch up")
        build_s = time.monotonic() - before
        assert memory_index.status()["state"] == "ready", memory_index.status()
        hybrid = measure()
        abstention = abstention_probe(ai_config, chat_model) if ai_config else None
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    return {
        "corpus_notes": len(cases),
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "model": "Qdrant/all-MiniLM-L6-v2-onnx",
        "dimensions": 384,
        "model_revision": "d13954661f83248295ba75c1ed411eef3b7b936e",
        "fastembed": version("fastembed"),
        "native": version("lancedb-compat"),
        "keyword": keyword,
        "hybrid_including_http_and_embedding": hybrid,
        "semantic_build_s": build_s,
        "abstention": abstention,
        "limits": "Self-authored 50-note/100-query answerable fixture, not a production "
        "distribution. One known relevant note per query. Real local MiniLM embeddings "
        "through the HTTP adapter and shared service. Model is qualification-only; "
        "product neither bundles nor downloads it. No universal relevance threshold.",
    }


def evaluation_chat(config_path, model):
    from agent_sessions import prefs, review, template_secrets

    cfg = prefs.get_ai_review(config_path)
    if model:
        cfg = {**cfg, "model": model}
    # Credentials remain in process memory; preserve the normal origin/egress gate.
    prefs.get_ai_review_migrated = lambda: cfg
    template_secrets.redaction_values = lambda: [cfg["api_key"]] if cfg.get("api_key") else []
    review._require_config()
    return cfg


def abstention_probe(config_path, model):
    """Record real answers to unanswerable questions against actual hybrid neighbours."""
    import asyncio
    import hashlib

    from agent_sessions import memory_service, prompts, review

    cfg = evaluation_chat(config_path, model)
    fixture = Path(__file__).resolve().parent.parent / "tests/fixtures/memory-abstention.json"

    async def evaluate():
        results = []
        for query in json.loads(fixture.read_text()):
            result = await asyncio.to_thread(
                memory_service.search, memory_service.operator(), query, limit=8
            )
            assert result["state"] == "ready", result["state"]
            payload = await review.complete_json(
                [
                    {"role": "system", "content": prompts.effective("memory_answer")},
                    {"role": "user", "content": json.dumps({"question": query, "memory": result})},
                ]
            )
            results.append(
                {
                    "question": query,
                    "expected_support": False,
                    "search_state": result["state"],
                    "neighbours": [h["slug"] for h in result["hits"]],
                    "answer": payload.get("answer"),
                }
            )
        return results

    return {
        "model": cfg["model"],
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "cases": asyncio.run(evaluate()),
        "limits": "Unanswerable synthetic questions against the published semantic corpus. "
        "Actual neighbours and model answers are recorded for manual assessment; no universal "
        "abstention or injection-safety guarantee.",
    }


def capture_probe(config_path, model=None):
    """Opt-in synthetic calls through the saved origin's existing acknowledged chat transport."""
    import asyncio
    import hashlib

    from agent_sessions import memory, memory_capture, prompts, review, template_secrets

    fixture = Path(__file__).resolve().parent.parent / "tests/fixtures/memory-capture.json"
    cases = json.loads(fixture.read_text())
    cfg = evaluation_chat(config_path, model)

    async def evaluate():
        results = []
        for name, expected, text in cases:
            before = time.perf_counter()
            skipped = bool(
                memory._withheld(text, template_secrets.redaction_values())
                or "[withheld" in text.lower()
            )
            items = (
                []
                if skipped
                else memory_capture._validate(
                    await review.complete_json(
                        [
                            {"role": "system", "content": prompts.effective("memory_capture")},
                            {"role": "user", "content": text},
                        ]
                    ),
                    text,
                )
            )
            results.append(
                {
                    "case": name,
                    "expected_capture": expected,
                    "captured": bool(items),
                    "candidate_count": len(items),
                    "quotes": [item["quote"] for item in items],
                    "withheld_before_model": skipped,
                    "latency_ms": (time.perf_counter() - before) * 1000,
                }
            )
        return results

    results = asyncio.run(evaluate())
    return {
        "model": cfg["model"],
        "cases": results,
        "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "correct": sum(r["captured"] == r["expected_capture"] for r in results),
        "count": len(results),
        "limits": "One synthetic run on the operator-configured acknowledged endpoint. "
        "Checks selection and verbatim provenance, not proof against every injection. "
        "No operator content or credential in the artifact.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="new private fixture directory")
    parser.add_argument(
        "--mode", choices=["service", "hybrid", "native", "semantic", "capture"], required=True
    )
    parser.add_argument(
        "--ai-config",
        type=Path,
        help="saved acknowledged AI settings (capture; optional semantic abstention calls)",
    )
    parser.add_argument(
        "--model-cache", type=Path, help="existing fastembed model cache (semantic only)"
    )
    parser.add_argument(
        "--chat-model", help="optional capture evaluation model on that saved origin"
    )
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument(
        "--note-chars", type=int, default=120, help="service fixture body size (up to 32000)"
    )
    args = parser.parse_args()
    if not 1000 <= args.rows <= 1000000:
        parser.error("rows must be between 1,000 and 1,000,000")
    if not 120 <= args.note_chars <= 32000:
        parser.error("note-chars must be between 120 and 32000")
    root = args.root.resolve()
    configure(root)
    if args.mode == "capture":
        if not args.ai_config or not args.ai_config.is_file():
            parser.error("capture requires --ai-config; makes bounded paid synthetic model calls")
        result = capture_probe(args.ai_config, args.chat_model)
    elif args.mode == "semantic":
        if not args.model_cache or not args.model_cache.is_dir():
            parser.error("semantic requires an existing --model-cache")
        result = semantic_probe(root, args.model_cache.resolve(), args.ai_config, args.chat_model)
    else:
        result = (
            (service_probe if args.mode == "service" else hybrid_probe)(
                root, args.rows, args.note_chars
            )
            if args.mode in {"service", "hybrid"}
            else native_probe(root, args.rows)
        )
    result.update(
        mode=args.mode,
        affinity_cpus=2,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        bytes_on_disk=disk_bytes(root),
        python=os.sys.version.split()[0],
    )
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
