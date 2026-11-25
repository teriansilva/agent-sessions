/** One automation's run history, paged (#1201) — the list the run-history page draws.
 *
 *  Pinned by `useRunPages.test.tsx` (#1252 reviews):
 *
 *  - **The cursor is the server's position, not the display's.** It advances by the RAW length of
 *    the pages the server returned; the display is deduplicated by run id.
 *  - **Arrivals are found, not inferred.** `total` nets inserts against retention's prunes, so a
 *    change in it says nothing. Every load reads from the head until a run the list already
 *    holds appears (bounded); the rows above it are new, and the cursor moves down by that many.
 *  - **A short or empty page with runs still unseen walks again from the top**, bounded, so rows
 *    that a prune or an insert moved past the cursor are still reached.
 *  - **All or nothing.** The rows, the cursor and the total of a load are committed together only
 *    when every read of it succeeded; a failure leaves the list and the cursor as they were.
 *  - **One page at a time, and a Retry wins.** A second `loadMore` in flight is dropped by a ref;
 *    `loadFirst` starts a new generation, and a read of an older one applies nothing.
 */
import { useCallback, useRef, useState } from "react";

import { ApiError, api } from "../../lib/api";
import { errorWords } from "../../lib/automations";
import type { AutomationRun } from "../../types/automations";

export const RUNS_PAGE = 50;

/** Newest first, as the server orders them (`created_at DESC, id`); a run id appears once. */
export function mergeRuns(prev: AutomationRun[], more: AutomationRun[]): AutomationRun[] {
  const byId = new Map(prev.map((r) => [r.id, r]));
  for (const r of more) byId.set(r.id, r);
  return [...byId.values()].sort(
    (a, b) => b.created_at - a.created_at || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}

export function useRunPages(id: string, page = RUNS_PAGE) {
  const [runs, setRuns] = useState<AutomationRun[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  /** The list as last committed — the source of truth a load reads its "held" ids from. */
  const rows = useRef<AutomationRun[]>([]);
  /** The server position the next older page starts at. */
  const cursor = useRef(0);
  const paging = useRef(false);
  const generation = useRef(0);

  const commit = useCallback((next: AutomationRun[], nextTotal: number) => {
    rows.current = next;
    setRuns(next);
    setTotal(nextTotal);
    setError(null);
    setLoadedAt(Date.now());
  }, []);

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 404) setMissing(true);
    setError(errorWords(e, "Couldn’t load run history"));
  }, []);

  /** The newest page, from the top: the cursor restarts after it. */
  const loadFirst = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const r = await api.automationRuns(id, page, 0);
      if (gen !== generation.current) return; // a newer reload owns the list
      cursor.current = r.runs.length;
      setMissing(false);
      commit(r.runs, r.total);
    } catch (e) {
      if (gen === generation.current) fail(e);
    }
  }, [id, page, fail, commit]);

  const loadMore = useCallback(async () => {
    if (paging.current) return; // a page is already on the wire
    paging.current = true;
    setLoadingMore(true);
    const gen = generation.current;
    const maxPages = 50;
    try {
      const held = new Set(rows.current.map((r) => r.id));
      let merged = rows.current;
      let serverTotal = 0;
      // 1. Arrivals: read from the head until a held run appears. The rows above it are new.
      let arrived = 0;
      for (let off = 0, n = 0; n < maxPages; off += page, n++) {
        const r = await api.automationRuns(id, page, off);
        if (gen !== generation.current) return;
        serverTotal = r.total;
        merged = mergeRuns(merged, r.runs);
        const hit = r.runs.findIndex((x) => held.has(x.id));
        if (hit >= 0) {
          arrived += hit;
          break;
        }
        arrived += r.runs.length;
        if (r.runs.length < page) break;
      }
      // 2. The next older page, from the cursor moved down by what arrived.
      let next = cursor.current + arrived;
      const older = await api.automationRuns(id, page, next);
      if (gen !== generation.current) return;
      serverTotal = older.total;
      merged = mergeRuns(merged, older.runs);
      next += older.runs.length;
      // 3. A short or empty page while runs are still unseen: something moved past the cursor.
      //    Walk again from the top, bounded, until everything the server holds is shown.
      if (older.runs.length < page && merged.length < serverTotal) {
        next = 0;
        for (let n = 0; n < maxPages && merged.length < serverTotal; n++) {
          const r = await api.automationRuns(id, page, next);
          if (gen !== generation.current) return;
          serverTotal = r.total;
          merged = mergeRuns(merged, r.runs);
          next += r.runs.length;
          if (r.runs.length < page) break;
        }
      }
      // Every read succeeded: commit the rows, the cursor and the total together.
      cursor.current = next;
      commit(merged, serverTotal);
    } catch (e) {
      // Nothing committed: the list and the cursor stay as they were, so a Retry loses nothing.
      if (gen === generation.current) fail(e);
    } finally {
      paging.current = false;
      setLoadingMore(false);
    }
  }, [id, page, fail, commit]);

  /** Re-read the newest page and merge it (a run finished while open). The cursor is kept. */
  const refreshHead = useCallback(async () => {
    const gen = generation.current;
    try {
      const r = await api.automationRuns(id, page, 0);
      if (gen !== generation.current) return;
      commit(mergeRuns(rows.current, r.runs), r.total);
    } catch {
      /* the list already shown stays */
    }
  }, [id, page, commit]);

  /** A run this page just started (Run now): shown at once; the next load reconciles positions. */
  const prepend = useCallback((run: AutomationRun) => {
    const next = mergeRuns(rows.current, [run]);
    rows.current = next;
    setRuns(next);
    setTotal((t) => t + 1);
  }, []);

  return {
    runs,
    total,
    error,
    loadedAt,
    missing,
    loadingMore,
    loadFirst,
    loadMore,
    refreshHead,
    prepend,
  };
}
