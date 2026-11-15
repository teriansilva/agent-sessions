# Shared Memory qualification — 2026-11-14

These are synthetic development measurements for the shared Memory service (#2036),
on Linux 6.8 x86-64 / Xeon E5-4660 v4 (virtualized). Each probe was restricted to **two logical
CPUs** on a shared 64-vCPU host, using Python 3.13.13, SQLite FTS5, LanceDB compat 0.41.0 and
Arrow 26.0.1. Results are individual runs, not confidence intervals or production guarantees.
All fixture stores were private and isolated from operator data. The base revision was
`62490f57cae6da07b888cc0f2f365d95b958b086`. Tested application revisions are:

- Initial service, native, semantic/capture, compatibility and installer qualification:
  `8115b40a03131e6ecae0236dbf988bceb9d4848e`.
- Complete scaled hybrid retrieval, cached-session freshness and batched maintenance:
  `ab4fe07adb75c7754e485a8d4d3d4a55171d1fe3`.
- Sparse physical erasure, semantic disablement and final full-suite qualification:
  `8ae1eaacbc3e0b004f38464b303ee2d884258177`.

The reports bind the measured backend/index implementation. Later UI freshness and browser
readiness changes are qualified separately in the PR; they leave those backend sources unchanged.
Reports capture individual runs; do not treat an earlier benchmark as a rerun on the final head.

Integration with HQ settings/model consolidation uses base
`426a0284e84899a1bc69c54ffcdc0f6ed93b209a`. Memory settings now mount in the shared HQ composition;
independent capture clears inherited model choices at entry. That integration has separate
component, transport and full-suite validation; the retrieval/index implementation is unchanged.

## Complete service and larger-note scaling

Both runs have 100 projects × 1,000 notes. They seed synthetic accepted-source tables directly;
they do not benchmark intake. Larger notes represent operator-enlarged bodies (ordinary agent
intake remains capped at 4 KiB). Queries include scope authorization, the current project catalog,
current-source hydration, secret fencing and credential-shape checking. The registered-secret
inventory is empty. There is no embedding or answer-model latency in this table.

| Measurement | [120-character bodies](service-100k-short.json) | [4,600-character bodies](service-100k-large.json) |
|---|---:|---:|
| Notes / vector chunks | 100,000 / 100,000 | 100,000 / 300,000 |
| First query | 206 ms | 225 ms |
| Global warm p95 | **222 ms** | **230 ms** |
| Scoped warm p95 | 32 ms | 55 ms |
| Four simultaneous readers, p95 | 874 ms | 913 ms |
| Initial indexing | 68 s | 400 s |
| Update processing / delete and physical cleanup | 4.60 s / 0.96 s | 3.23 s / 1.41 s |
| Peak process RSS | 161 MiB | 174 MiB |
| Whole fixture disk | 116 MiB | 1,761 MiB |

The 161 queries per run include a deliberately broad query matching every note. Single-query
p95 meets the ≤250 ms target at both sizes. The four-reader stress case does **not** meet that
latency; do not apply the single-query claim to concurrent broad searches. Update/delete timings
invoke the worker immediately: normal visibility additionally waits for the 15-second polling
interval, so these runs fit the ≤30-second steady-state target. Initial backfills have an explicit
indexing state and take longer. Disk includes source databases, generated Markdown and the derived
index, not just FTS. The large result's original `limits` string incorrectly says “short notes”;
its `note_body_chars` and `chunks` fields are correct, and the script's label is fixed.

The [initial all-Lance service candidate](rejected-lance-service.json) missed the target (global
p95 515 ms, four readers 1,790 ms). The release uses one shared retrieval engine with SQLite
FTS5/BM25 **per note** plus optional Lance vector chunks, fused by note identity. Grouped hydration
and cheaper ordering of two pure credential predicates also contributed to the final result;
this is not a claim that changing a library alone accounts for the difference.

## Vector retrieval, semantic quality and abstention

The [complete 100,000-note / 384-dimensional hybrid service run](hybrid-service-100k.json)
includes FTS5 and real ANN ranking, source authorization, current-source hydration, note-level
RRF and the actual HTTP adapter. Global/scoped p95 was **230/51 ms excluding query embedding**,
or **256/77 ms including the synthetic local query-vector request**. Four concurrent broad
readers took 1,048 ms p95 excluding that request. Its first hybrid query took 708 ms (784 ms
including the request); this is a startup sample, not a cold-query percentile. Source indexing
and the bulk fixture-vector/native-index build are recorded separately. Peak RSS including
construction was 1,594 MiB, whole fixture disk 925 MiB. Actual ANN plans were verified.

This run loads seeded random vectors in bulk and serves a fixed query vector over local HTTP.
It measures the complete scaled retrieval path, not semantic relevance, real encoder throughput
or the normal 16-input embedding backfill rate. The authored real-encoder evaluation below is
separate. The [earlier serial, uncached hybrid service](rejected-serial-hybrid-service.json)
missed the target at 489 ms global p95. Native-session reuse and independent lexical/ANN work
removed that overhead. The scale probe also caught repeated ANN construction from a case-sensitive
index-type check, and needless vector replacement on unrelated secret-inventory events.
Regression tests now pin native-session freshness, cache invalidation, unchanged-vector retention,
batched durable deletion and configured reads that create no vector directory.

The [100,000-vector native run](native-100k.json) uses random 384-dimensional vectors and exact
cosine ground truth: Recall@10 was 1.0 globally and within a scope, over 25 queries each. Query
plans contained `ANNSubIndex`; global/scoped p95 was 96/21 ms and ANN construction took 49 s.
Logical deletion plus old-version cleanup passed. Its 1,722 MiB peak RSS includes the full random matrix,
exact-truth calculation and native index construction; it is not the product's query RAM budget.
Whole fixture disk was 476 MiB. This measures ANN correctness, not semantic relevance.

The separate [sparse physical-erasure probe](sparse-vector-erasure.json) deletes one vector from
the 100,000-note hybrid fixture, below the native default 10% materialization threshold. Forced
compaction and zero-age cleanup completed in **6.8 seconds**: 99,999 vectors remained, no masked
rows or old versions remained, a freshly abandoned transaction file was removed, and a unique
float32 byte marker from the deleted random vector occurred twice before cleanup and zero times
afterward across app-owned vector files. This verifies that fixture's physical index cleanup;
it does not claim SSD forensic or external-backup erasure. Regression tests cover sparse deletion
and semantic disablement while retaining source notes and keyword recall.

The [semantic run](semantic-and-abstention.json) uses the published
[50-note / 100-query fixture](../../tests/fixtures/memory-semantic.json), evenly split between
exact titles and paraphrases, with one known relevant note per query. MiniLM embeddings were
served locally through the **actual HTTP adapter**, and results came from the shared service.
Keyword Recall@10 was 0.55 (12 ms p95); hybrid Recall@10 was **0.93** (**97 ms p95 including query
embedding and HTTP**). Seven paraphrase misses are included in the raw report. Vector backfill
took 3.4 s; peak RSS including the qualification encoder was 425 MiB. This small authored fixture
is not a representative production query distribution.

Encoder: Apache-2.0 `Qdrant/all-MiniLM-L6-v2-onnx`, 384 dimensions, revision
`d13954661f83248295ba75c1ed411eef3b7b936e`, FastEmbed 0.9.1. FastEmbed and its model cache are
qualification tools only; the product neither installs nor downloads an encoder.

Six [unanswerable questions](../../tests/fixtures/memory-abstention.json) were queried against
the same hybrid index. All six returned eight actual neighbours in `ready` state, then the
registered `memory_answer` prompt was evaluated with `deepseek-v4.1-flash` through the saved,
acknowledged chat transport. Manual review of the recorded answers found **6/6 declined to invent
the requested fact**. One answer inaccurately described the ready hybrid search as keyword-only;
this remains a model-output limitation. No encoder-independent distance threshold or universal
abstention/injection guarantee is claimed.

## Capture and compatibility

[Capture results](capture.json): **24/24** selection outcomes on the published
[fixture](../../tests/fixtures/memory-capture.json), including quotations, questions, transient
progress, injection attempts and direct assertions. The credential-shaped case was withheld
before the model. Every returned quote passed exact-span validation. The configured default
chat model returned HTTP 502 during qualification; the available `deepseek-v4.1-flash` was chosen
for these synthetic calls without changing any saved operator setting. Optional capture remains
off by default and reports/retries unavailable transport rather than inventing a successful save.

The [upgrade/rollback copy probe](upgrade-rollback.json) admitted an old-release inbox note,
upgraded its source, then used the unchanged base release's reader, editor and deletion against
that synthetic store. Paths and slug were retained; new transactional change triggers observed
the old writer. The old reader still works; old MCP processes need a restart to use the broker.
Older releases cannot operate HQ or the new embedding-key inventory and are not a safe place to
continue new admissions involving that key. No production rollback was performed.

`tests/test_memory_recovery.py` also proves a new interpreter resumes a partial backfill, drains
an edit behind its saved cursor, and restores citations, receipts and forget tombstones from a
consistent SQLite source backup before rebuilding the index. The restart regression was red
before the final worker fix. The full Python suite runs on the minimum supported Python 3.11;
Python 3.13 separately reproduced an existing native-ownership symlink-loop failure on unchanged
main. Real containment tests require the existing systemd user bus to be inherited explicitly
in this API session (`XDG_RUNTIME_DIR=/run/user/1000` and its D-Bus socket); the unverified-scope
test passes with that environment. Optional host-load budget tests remain skipped in CI mode.
Neither condition is hidden by altering production ownership or resource rules.

## Reproduction and installation cost

The [fresh-process read samples](process-cold-reads.json) were 939/953 ms for the short/large
keyword fixtures. These include first-use application costs after import; the OS filesystem
cache was not flushed on the shared host. The table's “first query” values instead describe
the first query in the process that just built the index. Neither is a cold-query p95.

Commands and backup constraints are in [the architecture guide](../memory.md). Use new private
`--root` paths for every run; the script refuses existing directories. Add `--ai-config <prefs>`
and `--chat-model <available-model>` to `--mode semantic` only when authorizing the six synthetic
abstention calls. This reads the saved acknowledged chat settings but changes none. Raw reports
contain synthetic text only; no credential or operator content is copied.

The Linux x86-64 native wheel pair is approximately 126 MiB compressed (73 MiB LanceDB compat +
53 MiB Arrow), before transitive dependencies/unpacking. Keyword search never imports either
native package. Other platforms are metadata-qualified, not claimed runtime-tested. CPU flags
are checked before native import, and installing conflicting Lance distributions fails closed.

A pristine Ubuntu 22.04 installer smoke using vendored Python 3.12.13 and Node 22.14.0 passed
the real build, installed-server health check and uninstall while preserving seeded agent data.
It took 23 minutes 31 seconds on this network, with cached toolchains and cold native-wheel
downloads. Four installer unit cases exceeded their 600-second timeout on the same cold
downloads. Local retries use the smoke's ordinary verified pip cache; no timeout or production
installer behavior was weakened. Download time is a material installation cost, not a query cost.

The shared runner's Node-only browser jobs can have `uv` on PATH without a prepared Python
environment. Browser transport fixtures therefore check installed project-venv metadata before
running; the dedicated app-mode workflow provisions Python with `uv sync --extra dev` and runs
the real transport tests. Native-wheel downloads belong to setup, outside relay readiness timers.
