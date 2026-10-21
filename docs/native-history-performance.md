# Native history retention (#1386)

The native JSONL remains authoritative. Validated event payloads spill to private anonymous
files on the journal filesystem; the Python heap retains cursor/operation indexes and operation
claims. A derived cache is bounded per process by 64 entries, 32 MiB of accounted Python
objects/indexes and 128 MiB of payload files. Active calls can retain evicted entries and
materialized pages temporarily, so these are retention budgets, not a total RSS cap.

Measurements on a synthetic 12,000-event, 27.25 MiB journal (one operation, ~2 KiB text events):

| Measurement | Before | After |
| --- | ---: | ---: |
| Retained Python allocations | 39.28 MiB | 0.34 MiB |
| Cold-read peak Python allocations | 94.33 MiB | 64.00 MiB |
| Unchanged poll, mean of 20 | 164.75 ms | 0.39 ms |
| Authoritative bytes read by those polls | 545 MiB | 0 |
| Cold read with tracemalloc enabled | 3.31 s | 5.41 s |
| Four warm readers, 16 retained pages: peak | 256.28 MiB | 5.62 MiB |
| Four cold readers, eight sessions: peak | 529.71 MiB | 256.04 MiB |
| Eight sessions: retained Python allocations | 314.05 MiB | 1.37 MiB |

Acceptance targets for this fixture are retained allocations <1 MiB, cold peak <100 MiB and
unchanged polls <10 ms. The traced cold-read budget is <10 s on this fixture; four concurrent
cold readers should stay below 320 MiB of traced allocations. Cold loading is slower because it writes derived payloads; the steady
state avoids repeated source reads and JSON expansion. Times depend on storage and host load.
Tracemalloc adds overhead, so these cold times are not a production latency promise.

Four concurrent warm readers retaining sixteen 100-event pages peaked at **5.62 MiB** of
additional traced allocations. Four concurrent cold readers over eight independent sessions
peaked at **256.04 MiB**, including raw source buffers, line parsing, indexes and cache entries.
Afterward **1.37 MiB** remained on the Python heap and four cache entries occupied **107.12 MiB**
of spools; older entries were evicted under the 128 MiB budget. OS page cache and other application
state are outside tracemalloc. Concurrent calls and large immutable prompts can still raise RSS.

Reproduce in separate processes from an editable development install:

```sh
git show db023604:src/agent_sessions/native_journal.py > /tmp/native-journal-baseline.py
.venv/bin/python -I scripts/benchmark-native-history.py --baseline /tmp/native-journal-baseline.py
.venv/bin/python -I scripts/benchmark-native-history.py
```

The script creates only disposable synthetic journals; it never opens real session history.
It also prints concurrent-reader and multi-entry measurements. The cache's correctness gates
are in `tests/test_native_journal.py`: unchanged/private/fsynced reads, rewritten/replaced/torn
journals, concurrent append/read, byte eviction, old cursors and exact replay, spill failure,
immutable snapshots, and recent-turn projection retaining live approvals and session facts.
Effect paths always verify full authoritative content under the existing exclusive lock.
