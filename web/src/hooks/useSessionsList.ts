import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { ProjectRef, Session, SessionsQuery } from "../types/api";

const PAGE = 20;
/** Background refresh cadence — keeps the sidebar live without hammering the server (#159). */
const POLL_MS = 15_000;

export interface Filters {
  q: string;
  project: string;
  engine: string;
  archived: boolean;
}

const EMPTY: Filters = { q: "", project: "", engine: "", archived: false };

interface Facets {
  projects: ProjectRef[];
  engines: string[];
}

/** The sidebar's data layer: filtered + paginated session list with server facets.
 *  Changing a filter resets to page 0; `loadMore` appends the next page. */
export function useSessionsList() {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState<Facets>({ projects: [], engines: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Monotonic request id: a slower earlier fetch (e.g. an older search query) must
  // never overwrite the state of a newer one that already resolved.
  const reqId = useRef(0);
  // `initialLoaded` flips after the very first fetch settles. Polling is gated on it so a
  // silent 15s poll firing while the bootstrap is in flight can't supersede it (#168).
  const [initialLoaded, setInitialLoaded] = useState(false);
  // Visible (non-silent) request count: bootstrap + filter changes + loadMore. While > 0
  // the silent poll is suppressed entirely — otherwise a silent fetch bumping `reqId`
  // would orphan the visible request's `finally`, never clearing `loading=true`
  // ("Load more" disabled / spinner forever). Hermes #168 review caught this for both
  // the bootstrap path AND a `loadMore` mid-poll. A counter (not a boolean) handles the
  // case where two visible requests overlap (e.g. fast filter typing).
  const visibleInFlight = useRef(0);

  const query: SessionsQuery = useMemo(
    () => ({
      q: filters.q,
      project: filters.project || undefined,
      engine: filters.engine || undefined,
      archived: filters.archived,
      limit: PAGE,
    }),
    [filters],
  );

  // Live row count — read inside refresh() without re-creating it on every list mutation,
  // so the polling effect's setInterval doesn't get torn down and restarted each tick (#159).
  const sessionsCount = useRef(0);
  useEffect(() => {
    sessionsCount.current = sessions.length;
  }, [sessions]);

  const fetchPage = useCallback(
    async (
      offset: number,
      replace: boolean,
      opts: { silent?: boolean; limit?: number } = {},
    ) => {
      const gen = ++reqId.current;
      // Silent (background) refreshes don't show a loading state or surface errors — the
      // user sees current rows until the next successful fetch (#159).
      if (!opts.silent) {
        setLoading(true);
        setError(null);
        visibleInFlight.current += 1; // suppresses the silent poll while we're pending
      }
      try {
        const page = await api.sessions({ ...query, offset, limit: opts.limit ?? PAGE });
        if (gen !== reqId.current) return; // superseded by a newer request → drop
        setSessions((prev) => {
          const merged = replace ? page.sessions : [...prev, ...page.sessions];
          const seen = new Set<string>(); // dedupe by id (defensive)
          return merged.filter((s) => (seen.has(s.id) ? false : (seen.add(s.id), true)));
        });
        setNextOffset(page.next_offset);
        setTotal(page.total);
        setFacets(page.facets);
      } catch (e) {
        if (gen !== reqId.current) return;
        if (opts.silent) return; // background failure → keep existing rows, no flicker
        setError(
          e instanceof ApiError && e.status === 401 ? "Please sign in." : "Failed to load sessions.",
        );
      } finally {
        if (gen === reqId.current && !opts.silent) setLoading(false);
        // The first visible (non-silent) fetch to fully settle — successful, errored, or
        // superseded after this point — unblocks polling. Silent fetches don't qualify;
        // they're never the bootstrap.
        if (!opts.silent) {
          setInitialLoaded(true);
          // Always decrement, even on supersession — pairs with the increment above.
          visibleInFlight.current = Math.max(0, visibleInFlight.current - 1);
        }
      }
    },
    [query],
  );

  useEffect(() => {
    // Reload from page 0 whenever the filters change.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchPage(0, true);
  }, [fetchPage]);

  /** Silent refresh from offset 0 covering every loaded row, so a session that just got new
   *  activity moves up + relative-time labels stay fresh without losing pages the user has
   *  already loaded (#159). Skipped while any visible request (bootstrap / filter / loadMore)
   *  is in flight, so the silent fetch can't supersede the visible one and leave `loading`
   *  stuck `true` (Hermes #168 review). The next 15s tick picks up cleanly once visible is
   *  done. */
  const refresh = useCallback(() => {
    if (visibleInFlight.current > 0) return; // a visible request is pending — let it finish
    const limit = Math.max(PAGE, sessionsCount.current);
    void fetchPage(0, true, { silent: true, limit });
  }, [fetchPage]);

  // Live polling: refresh on a cadence, pause while the tab is hidden, resume + refresh
  // immediately when it becomes visible again. Gated on `initialLoaded` so a slow first
  // fetch can't be lapped by a silent poll (the race Hermes caught: both fetches in flight,
  // `reqId` bumped past the visible one, `loading` stuck true).
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (!initialLoaded) return; // wait for the bootstrap fetch to settle
    let id: number | undefined;
    const start = () => {
      if (id != null) return;
      id = window.setInterval(refresh, POLL_MS);
    };
    const stop = () => {
      if (id != null) window.clearInterval(id);
      id = undefined;
    };
    const onVis = () => {
      if (document.hidden) {
        stop();
      } else {
        refresh(); // catch up on what we missed while hidden
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [refresh, initialLoaded]);

  const loadMore = useCallback(() => {
    if (loading || nextOffset == null) return;
    void fetchPage(nextOffset, false);
  }, [loading, nextOffset, fetchPage]);

  const update = useCallback((patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch })), []);
  const clear = useCallback(() => setFilters(EMPTY), []);

  // Rename in place: the row stays in the current view, only its title changes.
  const renameRow = useCallback(async (id: string, title: string) => {
    const r = await api.rename(id, title);
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title: r.title } : s)));
  }, []);

  // Toggle archived: the list is scoped to one archived-state, so after the flip the
  // row leaves the current view → drop it locally (avoids a full refetch + flicker).
  // Removing it shrinks the server's archived-scoped set by one, so every still-unloaded
  // row shifts down one offset — decrement nextOffset to match, or the next "Load more"
  // would skip the first unloaded row (it sat at the old offset). When all rows are
  // already loaded (nextOffset == null) there is nothing to backfill.
  const setArchived = useCallback(async (id: string, currentlyArchived: boolean) => {
    await (currentlyArchived ? api.unarchive(id) : api.archive(id));
    setSessions((prev) => prev.filter((s) => s.id !== id));
    setTotal((t) => Math.max(0, t - 1));
    setNextOffset((o) => (o == null ? null : Math.max(0, o - 1)));
  }, []);

  // Manual "Review now" (#356): run one AI review and fold the result into the row in
  // place (summary, badge, and the possibly-new display title) — no refetch flicker.
  const reviewRow = useCallback(async (id: string) => {
    const r = await api.reviewNow(id);
    setSessions((prev) =>
      prev.map((s) =>
        s.id === id
          ? {
              ...s,
              title: r.title || s.title,
              ai_summary: r.ai_summary,
              ai_title: r.ai_title,
              intervention_required: r.intervention_required,
              intervention_reason: r.intervention_reason,
              reviewed_at: r.reviewed_at,
              review_excluded: r.review_excluded,
            }
          : s,
      ),
    );
  }, []);

  // Per-session exclude-from-review toggle (#356); the row stays, only the flag flips.
  const setReviewExcluded = useCallback(async (id: string, excluded: boolean) => {
    const r = await api.reviewExclude(id, excluded);
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, review_excluded: r.review_excluded } : s)),
    );
  }, []);

  return {
    sessions,
    total,
    facets,
    filters,
    loading,
    error,
    hasMore: nextOffset != null,
    loadMore,
    update,
    clear,
    renameRow,
    setArchived,
    reviewRow,
    setReviewExcluded,
  };
}
