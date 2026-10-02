"""Per-file row memo for the session walk (#1048).

The walk's unit of work is "one session file → one row", and that mapping is **pure with respect
to the file's bytes**: nothing a route later overlays (metadata, renames, favourites, live
``working`` state) is decided here — the registry's own note is explicit that "only the DISK WALK
is cached". So a file whose bytes have not changed cannot produce a different row, and re-reading
it is pure waste.

Measured on the author's install before this existed: one ``scan_all()`` cost **3,695 ms** and
re-parsed ~94,000 JSON records, while a ``stat()``-only pass over the same 1,372 files cost
**51 ms** and only **2 of those 1,372 files changed in any 15 s window**. The sidebar polls every
15 s against a 10 s snapshot TTL, so that walk ran in full, for ever.

## Identity, not just the path

An entry is keyed on ``(namespace, path, st_dev, st_ino, st_size, st_mtime_ns)``.

* ``st_mtime_ns`` and not ``st_mtime`` — a 1 s stamp cannot separate two writes inside the same
  second, and an agent appending to a transcript does exactly that.

  **``_ns`` is a unit, not a resolution**, and assuming otherwise is a bug this module was written
  with and had to fix. Measured on the target host's ext4: consecutive writes report timestamps
  **1 ms** apart, so two modifications inside one millisecond are indistinguishable by mtime. If
  they also leave ``st_size`` unchanged, the identity is unchanged and a stale row would be served.
  That is exactly git's "racily clean" index problem, and this takes git's answer — see
  :data:`RACY_WINDOW_NS`.
* ``st_dev``/``st_ino`` because a path is a reusable name: a file deleted and recreated at the same
  path must miss even if the size happens to match.
* ``namespace`` so two readers that build *different* rows from the same file (a live vs archived
  tree, say) can never be served each other's answer.

**The identity is re-checked after the build, and a row whose file moved underneath it is returned
but never stored.** Otherwise a file written between the pre-build ``stat`` and the builder's own
read would be cached under the identity it no longer has, and a later walk that saw exactly that
old identity again would be served a row built from different bytes.

## What it deliberately does NOT do

* **It is not cleared by ``invalidate_scan_cache()``.** Every mutation that invalidation exists for
  either moves the file (archive/unarchive → a new path → a new key) or creates one (a new session
  → a key that was never present), so clearing buys no correctness and costs a full cold walk after
  every archive. The identity key is the invalidation.
* **It never serves the CHECKED walk.** ``scan()`` and ``checked_scan`` have deliberately opposite
  failure policies — one fail-soft per file, one error-preserving — and ``checked_scan.py`` records
  what happened when they were unified: "exactly what hid every healthy ``shell`` session behind
  one malformed record". A fail-soft ``None`` cached here must never reach a measurement that
  authorises deletion, so the checked paths simply do not call this.
* **It does not bound by bytes.** Rows are small — measured across 1,527 real sessions, every
  ``first_user_message`` together came to 119 KB, the largest single one 5.5 KB — so a bounded
  entry count is a bounded memory footprint. The cap is enforced, not assumed.

A **leaf module on purpose**, like ``checked_scan``: it imports nothing from the app, so
``scanner.py`` and the engine providers can both use it without an import cycle.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Hard ceiling on memoised rows. The author's install walks 1,372 files; this leaves room for an
#: install several times larger before eviction starts costing re-reads, and is a real bound rather
#: than a reassurance — `_entries` is trimmed to it on every insert.
MAX_ENTRIES = 4096

#: How recently a file may have been modified and still be memoised (#1048).
#:
#: A filesystem timestamp has a granularity — 1 ms on the target host's ext4, 1 s on ext3 and on
#: some network filesystems. Inside one tick, a modification that leaves ``st_size`` unchanged is
#: invisible to :func:`identity`, so a row cached for a file that was written *this tick* could be
#: contradicted by a write that is never detected. **A row is therefore only stored once the file
#: has been quiet for longer than any plausible tick.** This is git's rule for a racily-clean index
#: entry, and it is chosen over trusting resolution because the cost is nil: the only files it
#: refuses to memoise are the ones being actively written, which are exactly the files whose rows
#: must be rebuilt anyway. Measured on the author's install, 2 of 1,372 files are in that state at
#: any moment.
#:
#: One second covers every filesystem plausibly hosting an agent's transcripts. It is not a
#: correctness knob to tune down: below the true granularity, the hole reopens silently.
RACY_WINDOW_NS = 1_000_000_000

#: Sentinel for "this key is present and its value is ``None``". ``None`` is a legitimate memoised
#: answer — "this file is not a listable session" is exactly the verdict that costs a full read on
#: a codex rollout with no user turn — so presence is tested on the key, never on the value.
_MISSING = object()

_lock = threading.Lock()
_entries: OrderedDict[tuple, Any] = OrderedDict()
_hits = 0
_misses = 0


def identity(st: os.stat_result) -> tuple[int, int, int, int]:
    """The version of a file, as the memo keys it. See the module note on each field."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def memoized(namespace: str, path: Path, build: Callable[[], Any]) -> Any:
    """``build()``'s result for ``path``, reused while the file's identity is unchanged.

    ``build`` keeps its own failure policy: when the pre-read ``stat`` fails there is no identity to
    key on, so it is called and its answer returned unmemoised — which is what lets a fail-soft
    builder stay fail-soft and a raising one keep raising.
    """
    global _hits, _misses
    try:
        st = path.stat()
    except OSError:
        # No identity ⇒ nothing to key on. The builder decides what an unreadable file means.
        return build()

    before = identity(st)
    key = (namespace, str(path), before)
    with _lock:
        hit = _entries.get(key, _MISSING)
        if hit is not _MISSING:
            _entries.move_to_end(key)
            _hits += 1
            return hit
        _misses += 1

    row = build()

    # Only store a row we can still name. Between the stat above and the builder's own read the
    # file may have been rewritten; caching then would file a row built from the NEW bytes under
    # the OLD identity, and a later walk that saw that identity would be served the wrong row.
    try:
        st_after = path.stat()
    except OSError:
        return row
    if identity(st_after) != before:
        return row
    # …and only once the file is old enough that a further write could not hide inside the
    # filesystem's timestamp granularity. See RACY_WINDOW_NS.
    if time.time_ns() - st_after.st_mtime_ns < RACY_WINDOW_NS:
        return row

    with _lock:
        _entries[key] = row
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)
    return row


def clear() -> None:
    """Drop every entry and reset the counters. For tests, and for an explicit operator purge."""
    global _hits, _misses
    with _lock:
        _entries.clear()
        _hits = 0
        _misses = 0


def stats() -> dict[str, int]:
    """``{entries, hits, misses}`` — what the perf probe reports. Never raises."""
    with _lock:
        return {"entries": len(_entries), "hits": _hits, "misses": _misses}
