import { createContext, useContext } from "react";
import type { Session } from "../types/api";

/** The last SUCCESSFUL, COMPLETE map sequence. A partially-collected array is never stored here —
 *  see `commit`.
 *
 *  There is deliberately NO `fetchedAt` and no freshness window. An earlier cut of #1007 served a
 *  retained result without fetching at all while it was under 30 s old, which made correctness
 *  depend on every mutating surface in the app announcing itself — and two review rounds found
 *  surfaces that did not: mission-console detach/archive, and terminal-backed session creation,
 *  which makes no session REST call at all and therefore has no transport boundary that could see
 *  it. Re-entry now ALWAYS revalidates, so the retained result is a rendering optimisation only
 *  and can never be the reason the map is wrong. What retention removes is the blocking WAIT, not
 *  the backend work: a revalidation landing inside the server's scan TTL is a measured 0.1 ms cache
 *  hit (pages 2–8: 0.3 ms total), while one landing after expiry still pays the full cold walk —
 *  but behind an already-rendered map rather than in front of it. */
export interface RetainedMap {
  sessions: Session[];
  /** The `MAX_PAGES` cap was hit before the server ran out of rows (showing a subset). */
  partial: boolean;
  /** The effective HARD scope the server applied when this result was fetched (`scopeKeyOf`).
   *
   *  Retained rows are only meaningful under the scope that produced them, and unlike staleness
   *  this cannot be fixed by revalidating a moment later: for the length of one round trip the map
   *  would paint rows from a boundary that is no longer in effect — including, when scope widens,
   *  FEWER sessions than exist. So a scope-incompatible result is not rendered at all. */
  scopeKey: string;
}

export interface OverviewSessionsStore {
  /** The retained result, or null when nothing has ever completed. */
  retained: RetainedMap | null;
  /** The hard scope in force RIGHT NOW. Compared against `retained.scopeKey` before anything is
   *  rendered. */
  scopeKey: string;
  /** Claim a generation for a new sequence. */
  begin: () => number;
  /** Commit a COMPLETE sequence, under the generation it claimed AND the scope it started under.
   *  A stale generation — or a scope that moved beneath the sequence — is discarded whole. */
  commit: (
    gen: number,
    forScope: string,
    sessions: Session[],
    partial: boolean,
  ) => void;
}

export const OverviewSessionsCtx = createContext<OverviewSessionsStore>({
  retained: null,
  scopeKey: "",
  begin: () => 0,
  commit: () => {},
});

export function useOverviewSessionsStore(): OverviewSessionsStore {
  return useContext(OverviewSessionsCtx);
}

/** The effective HARD scope of a session listing: the server-side boundary that decides list
 *  membership, and which the client cannot reproduce by filtering what it already has.
 *
 *  Read from `/api/config` ONLY, and that is the whole point. Deriving it from the optimistic
 *  client preference state instead let the key run ahead of the server: `OverviewPrefsContext`
 *  updates immediately and saves without awaiting, so a sessions GET could complete under the OLD
 *  persisted scope and still be stamped with the NEW key. Config advances only after the write
 *  lands (`commitRoots` / `commitExclusions` call `refreshConfig()` in `.then()`), so the key can
 *  never be newer than the scope the server actually applied.
 *
 *  The CURATION preferences (`projects_mode` / `projects_included` / `projects_hidden`) are
 *  deliberately absent: the map already applies those client-side at render (`sessionOnMap`,
 *  `isVisible`), so hiding takes effect instantly without touching the retained rows, and widening
 *  is corrected by the revalidation every re-entry now performs. Sorted, so a reordered list is
 *  not a scope change. */
export function scopeKeyOf(
  cfg: { project_roots?: string[]; folder_exclusions?: string[] } | null,
): string {
  const sorted = (xs: readonly string[]) => [...xs].sort();
  return JSON.stringify([
    sorted(cfg?.project_roots ?? []),
    sorted(cfg?.folder_exclusions ?? []),
  ]);
}

/** May this retained result be DISPLAYED? Scope-incompatible data is not stale data — it is wrong
 *  data, in both directions, so it is never rendered even while a refresh is already running. */
export function isScopeCompatible(
  r: RetainedMap | null,
  scopeKey: string,
): boolean {
  return !!r && r.scopeKey === scopeKey;
}
