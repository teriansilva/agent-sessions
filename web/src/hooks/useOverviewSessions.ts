import { useCallback, useEffect, useRef, useState } from "react";
import {
  isScopeCompatible,
  useOverviewSessionsStore,
} from "../app/overviewSessionsStore";
import { api } from "../lib/api";
import type { Session } from "../types/api";

// The API caps `limit` at 200; page until next_offset is null, but never beyond this many
// pages so a huge history can't spin forever. If we stop early, `partial` is set.
const PAGE = 200;
const MAX_PAGES = 20; // ≤ 4000 sessions

export interface OverviewSessions {
  sessions: Session[];
  loading: boolean;
  error: string | null;
  /** True if the page cap was hit before next_offset went null (showing a subset). */
  partial: boolean;
  /** Re-pull the full list (#361 Phase 4: a create-project mutation changed resolution).
   *  The current list stays on screen while the reload runs — `loading` only gates a load with
   *  nothing to show, so the canvas never unmounts mid-refetch. */
  refetch: () => void;
}

/** Fetch (nearly) all non-archived sessions for the overview, paging to completion under a hard
 *  cap — rendering immediately from the shell-owned retained result while it runs (#1007).
 *
 *  THE SPLIT: the DATA lives above the router (`OverviewSessionsProvider`), while LOADING and
 *  CANCELLATION stay here, route-owned. The route unmount is the canceller, so hoisting this
 *  effect would remove exactly the cleanup that stops a sequence nobody is waiting for.
 *
 *  CANCELLATION IS AN ABORT (Phase 2). Each run owns an `AbortController` whose signal rides every
 *  page request into the transport, and the effect cleanup — unmount, or a newer run replacing
 *  this one — aborts it. The page in flight is cancelled instead of downloaded and discarded, no
 *  further page is requested, and nothing the run collected is committed. What it does NOT do is
 *  save server work: the server runs its walk in a worker thread that a browser abort cannot
 *  reach, and the walk is page 1 (measured 3,551 ms, against 0.3 ms for pages 2–8 together), which
 *  has usually finished by the time anyone leaves.
 *
 *  AN ABORT IS NOT A FAILURE. A cancelled run reaches neither the error state nor the
 *  failed-refresh path, and never touches the retained result — it simply stops. Treating it as a
 *  failure is not harmless: StrictMode's mount → unmount → mount aborts the first run of the SAME
 *  run key, and marking that run settled would drop the spinner and paint "Couldn’t load
 *  sessions." over a cold map whose real load is still in flight.
 *
 *  EVERY MOUNT REVALIDATES. There is no freshness window and no "skip the fetch" path, and that is
 *  the correctness argument rather than an oversight: making the retained result authoritative for
 *  even 30 s required every mutating surface in the app to announce itself, and review found two
 *  that structurally cannot — mission-console mutations, and terminal-backed session creation,
 *  which performs no session REST call for any transport-level rule to observe. Revalidating
 *  removes that entire defect class for a request that is a measured 0.1 ms cache hit inside the
 *  server's scan TTL — and a full cold walk after it, paid behind an already-rendered map; what the
 *  operator
 *  actually complained about — a blocking 3.46 s "Loading session map…" on every visit — is fixed
 *  by rendering the retained rows, not by skipping the request.
 *
 *  So there are three outcomes, and they are not interchangeable:
 *  - retained AND scope-compatible → render it immediately, refresh BEHIND it, no spinner.
 *  - retained but scope-INCOMPATIBLE → the rows describe a boundary no longer in effect and are
 *    not rendered at all: wrong in both directions, and widening shows fewer sessions than exist.
 *    Treated as cold.
 *  - nothing retained → the original cold path, spinner and all.
 *
 *  A failed refresh keeps the prior good result and surfaces no error: only a failure with nothing
 *  showable is a failure the operator can act on. */
export function useOverviewSessions(): OverviewSessions {
  const { retained, scopeKey, begin, commit } = useOverviewSessionsStore();
  const [error, setError] = useState<string | null>(null);
  // A map-originated mutation (create project, rename, archive from the map's own ⋯) happens while
  // the route is MOUNTED, so there is no re-entry to ride — it needs its own re-run.
  const [gen, setGen] = useState(0);
  const refetch = useCallback(() => setGen((g) => g + 1), []);

  // Only rows fetched under the CURRENT hard scope may be drawn.
  const usable = isScopeCompatible(retained, scopeKey) ? retained : null;

  // `loading` is DERIVED from which run has settled rather than tracked with a synchronous
  // `setState` inside the effect (which cascades renders, and which the lint rule rejects). The
  // only state writes below happen in the async continuation, where they belong.
  // `gen` is numeric and `scopeKey` is JSON, so "#" cannot collide between them. It was a literal
  // NUL byte in an earlier revision, which made Git classify this whole file as binary.
  const runKey = `${gen}#${scopeKey}`;
  const [settledRun, setSettledRun] = useState<string | null>(null);

  // Read through a ref in the catch: `retained` is deliberately NOT an effect dependency (a commit
  // changes its identity, and depending on it would re-run the effect that produced it — a fetch
  // loop), so the render closure's copy goes stale the moment a sequence commits.
  const usableRef = useRef(usable);
  useEffect(() => {
    usableRef.current = usable;
  }, [usable]);

  useEffect(() => {
    // One controller per run, never shared: a run started after an abort gets a fresh signal, so
    // the abandoned run cannot cancel it. `signal.aborted` is also the run's only liveness flag.
    const ctl = new AbortController();
    const { signal } = ctl;
    const mine = begin();
    const forScope = scopeKey; // the scope this sequence is asking under
    const mineRun = runKey;
    (async () => {
      const acc: Session[] = [];
      let offset = 0;
      let partial = false;
      // One server walk for the whole sequence (#1007 Phase 3): page 1 asks for a pin and every
      // later page passes back the newest token, so a mutation or TTL expiry between pages cannot
      // force a second cold walk. It pins the walk only; the server re-applies scope per page.
      let snapshot = "new";
      try {
        for (let page = 0; page < MAX_PAGES; page++) {
          const res = await api.sessions(
            { limit: PAGE, offset, archived: false, snapshot },
            { signal },
          );
          // Abandoned mid-sequence. Normally the request above rejected on the abort, but a page
          // can still RESOLVE after it — the response had already arrived, or a transport could
          // not cancel in time. Drop it with everything collected so far and ask for nothing
          // more: a partial array is not a smaller map, it is a map that lost sessions.
          // (Stopping the REQUESTS is all a browser can do here; the server runs its walk in a
          // worker thread and finishes regardless.) The token is adopted only after this check,
          // so an abandoned sequence cannot carry its pin into whatever runs next.
          if (signal.aborted) return;
          if (res.snapshot) snapshot = res.snapshot;
          acc.push(...res.sessions);
          if (res.next_offset == null) break;
          offset = res.next_offset;
          if (page === MAX_PAGES - 1) partial = true; // cap hit with more to come
        }
        // In the same synchronous turn as the commit, so no abort can land between the two.
        if (signal.aborted) return;
        // Whole-sequence commit, checked against the generation it claimed AND the scope it asked
        // under: neither a slower sequence resolving after a newer one, nor one whose scope moved
        // beneath it, may overwrite the retained result.
        commit(mine, forScope, acc, partial);
        setError(null);
        setSettledRun(mineRun);
      } catch {
        // An abort is not a failure, whatever shape it surfaced in (a fetch `AbortError`, the
        // signal's own reason, a transport reset that raced it). Checked FIRST: a cancelled run
        // must not mark its run settled or raise an error — see the StrictMode note above.
        if (signal.aborted) return;
        setSettledRun(mineRun);
        // Preserve the prior good result. Only a failure with nothing showable is surfaced.
        if (!usableRef.current) setError("Couldn’t load sessions.");
      }
    })();
    return () => ctl.abort();
  }, [gen, scopeKey, runKey, begin, commit]);

  return {
    sessions: usable?.sessions ?? [],
    partial: usable?.partial ?? false,
    // Only a load with nothing to show blocks; a revalidation runs behind the map already on
    // screen. A scope change blocks too, because there is nothing correct to show underneath it.
    loading: !usable && settledRun !== runKey,
    error: usable ? null : error,
    refetch,
  };
}
