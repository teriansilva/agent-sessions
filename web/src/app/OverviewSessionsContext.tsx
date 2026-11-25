import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Session } from "../types/api";
import { useConfig } from "./config";
import {
  OverviewSessionsCtx,
  scopeKeyOf,
  type RetainedMap,
} from "./overviewSessionsStore";

/** Retains the map's last successful COMPLETE result in shell-owned memory (#1007 Phase 1).
 *
 *  Mounted ABOVE `RouterProvider`, deliberately and for the same reason as `WorkspaceProvider`
 *  (#936): `/overview` is lazy-loaded inside `<Routes>`, so `useOverviewSessions` fully unmounts on
 *  every navigation away. Before this, returning re-ran the whole multi-page sequence behind a
 *  blocking "Loading session map…" — on the author's host a 3.46 s cold walk, paid again per visit.
 *
 *  WHAT THIS IS, AND WHAT IT IS NOT. It is a RENDERING optimisation: the map paints immediately
 *  from the retained rows instead of showing a spinner. It is NOT a request cache — every re-entry
 *  revalidates. That split is what keeps it safe: an earlier cut skipped the fetch entirely inside
 *  a 30 s window, which made correctness depend on every mutating surface announcing itself, and
 *  review found surfaces that could not (mission-console mutations, and terminal-backed session
 *  creation, which issues no session REST call at all). A repeat fetch inside the server's scan TTL
 *  is a measured 0.1 ms cache hit; after expiry it still pays the cold walk, but behind an
 *  already-rendered map rather than in front of it. The blocking wait it removes cost seconds.
 *
 *  What moved is DATA ONLY. Loading and cancellation stay route-owned — the route unmount is the
 *  canceller — and `OverviewCanvas`'s React Flow DOM keeps mounting with the route, so the lazy
 *  boundary keeping `@xyflow/react` out of the main bundle is untouched.
 *
 *  The contract, the context object and the hooks live in `overviewSessionsStore.ts` — this file
 *  exports a component and nothing else, which is what keeps Fast Refresh working. */
export function OverviewSessionsProvider({ children }: { children: ReactNode }) {
  const [retained, setRetained] = useState<RetainedMap | null>(null);
  // Monotonic sequence token. A REF, not state: claiming one must not re-render, and `commit` has
  // to read the newest value synchronously — a state-based token would be one render stale exactly
  // when two sequences overlap, which is the case it exists to decide.
  const gen = useRef(0);

  // The hard scope in force now, read from config so it can never be newer than the scope the
  // server actually applied (see `scopeKeyOf`).
  const scopeKey = scopeKeyOf(useConfig());
  const scopeRef = useRef(scopeKey);
  useEffect(() => {
    scopeRef.current = scopeKey;
  }, [scopeKey]);

  const begin = useCallback(() => {
    gen.current += 1;
    return gen.current;
  }, []);

  const commit = useCallback(
    (mine: number, forScope: string, sessions: Session[], partial: boolean) => {
      // A LATE generation — a slower sequence that resolved after a newer one started. Discard the
      // WHOLE sequence: never merge it, and never apply the pages it did manage to collect. A
      // partial array on the map is not a smaller map, it is a map that silently lost sessions.
      if (mine !== gen.current) return;
      // The scope moved while this sequence was in flight, so its rows describe a boundary that is
      // no longer in effect. Stamping them with the CURRENT key would make wrong data look valid.
      //
      // The check belongs here rather than in a provider effect that fences on scope change: child
      // effects run before parent ones, so such an effect would bump the generation immediately
      // AFTER the hook claimed one, discarding the very sequence the scope change just started.
      if (forScope !== scopeRef.current) return;
      setRetained({ sessions, partial, scopeKey: forScope });
    },
    [],
  );

  const value = useMemo(
    () => ({ retained, scopeKey, begin, commit }),
    [retained, scopeKey, begin, commit],
  );
  return (
    <OverviewSessionsCtx.Provider value={value}>
      {children}
    </OverviewSessionsCtx.Provider>
  );
}
