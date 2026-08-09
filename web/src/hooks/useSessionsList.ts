import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConfig } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { ProjectRef, Session, SessionsQuery } from "../types/api";

const PAGE = 20;
/** Background refresh cadence — keeps the sidebar live without hammering the server (#159). */
const POLL_MS = 15_000;
/** Search-input debounce (#561): a burst of keystrokes collapses to ONE `/api/sessions` request
 *  instead of one per key (each fires a full uncached disk scan server-side). 250 ms is below the
 *  perceptual "instant" bar; the input value itself updates immediately (the box never lags) —
 *  only the derived query/fetch is debounced. Dropdown/tab changes stay immediate (discrete
 *  single events — debouncing them would feel laggy). */
const SEARCH_DEBOUNCE_MS = 250;

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
  // The active sort order (#506), tracked in a ref so the optimistic favorite re-sort can mirror
  // the server's secondary key (created_at vs last_mtime) without re-creating its callback. The
  // server stays the source of truth; the next poll/refetch reconciles regardless.
  const order = useConfig()?.session_list_order;
  const orderRef = useRef(order);
  useEffect(() => {
    orderRef.current = order;
  }, [order]);
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

  // Debounce ONLY the search text (#561): `filters.q` updates on every keystroke (so the input
  // stays responsive), but `debouncedQ` — the value folded into the fetch — trails it by
  // SEARCH_DEBOUNCE_MS, so a typed burst yields one request, not one per key. Project/engine/
  // archived are NOT debounced (see the query memo below): they change on discrete clicks and
  // should re-filter instantly. The existing reqId supersession + visibleInFlight poll-suppression
  // guards (Hermes #168) are untouched — they still cover the debounced query vs a dropdown change,
  // loadMore, and the silent poll.
  const [debouncedQ, setDebouncedQ] = useState(filters.q);
  useEffect(() => {
    if (debouncedQ === filters.q) return; // no pending change (e.g. bootstrap) → no timer churn
    const h = setTimeout(() => setDebouncedQ(filters.q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(h);
  }, [filters.q, debouncedQ]);

  const query: SessionsQuery = useMemo(
    () => ({
      q: debouncedQ,
      project: filters.project || undefined,
      engine: filters.engine || undefined,
      archived: filters.archived,
      limit: PAGE,
    }),
    [debouncedQ, filters.project, filters.engine, filters.archived],
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
        const page = await api.sessions({
          ...query,
          offset,
          limit: opts.limit ?? PAGE,
        });
        if (gen !== reqId.current) return; // superseded by a newer request → drop
        setSessions((prev) => {
          const merged = replace ? page.sessions : [...prev, ...page.sessions];
          const seen = new Set<string>(); // dedupe by id (defensive)
          return merged.filter((s) =>
            seen.has(s.id) ? false : (seen.add(s.id), true),
          );
        });
        setNextOffset(page.next_offset);
        setTotal(page.total);
        setFacets(page.facets);
      } catch (e) {
        if (gen !== reqId.current) return;
        if (opts.silent) return; // background failure → keep existing rows, no flicker
        setError(
          e instanceof ApiError && e.status === 401
            ? "Please sign in."
            : "Failed to load sessions.",
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

  // Refetch when the sort-order pref flips (#548: the sidebar toggle — and the Settings radio —
  // write the pref and refresh the shared config; the server sorts, so re-sorting in place is a
  // page-0 replace through the regular fetch path with its reqId/visibleInFlight guards). The
  // prev-compare skips the bootstrap and the config's initial load — those are already fetched
  // with the then-current order.
  const seenOrder = useRef(order);
  useEffect(() => {
    const prev = seenOrder.current;
    seenOrder.current = order;
    if (order !== undefined && prev !== undefined && prev !== order)
      void fetchPage(0, true);
  }, [order, fetchPage]);

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

  const update = useCallback(
    (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch })),
    [],
  );
  const clear = useCallback(() => setFilters(EMPTY), []);

  // Rename in place: the row stays in the current view, only its title changes.
  const renameRow = useCallback(async (id: string, title: string) => {
    const r = await api.rename(id, title);
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, title: r.title } : s)),
    );
  }, []);

  // Set/clear the custom tag (#551): patch the row in place from the server's echoed value
  // (already trimmed + capped), so the summary line reflects it without a refetch.
  const setTag = useCallback(async (id: string, tag: string) => {
    const r = await api.setTag(id, tag);
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, tag: r.tag } : s)),
    );
  }, []);

  // Toggle archived: the list is scoped to one archived-state, so after the flip the
  // row leaves the current view → drop it locally (avoids a full refetch + flicker).
  // Removing it shrinks the server's archived-scoped set by one, so every still-unloaded
  // row shifts down one offset — decrement nextOffset to match, or the next "Load more"
  // would skip the first unloaded row (it sat at the old offset). When all rows are
  // already loaded (nextOffset == null) there is nothing to backfill.
  const setArchived = useCallback(
    async (id: string, currentlyArchived: boolean) => {
      await (currentlyArchived ? api.unarchive(id) : api.archive(id));
      setSessions((prev) => prev.filter((s) => s.id !== id));
      setTotal((t) => Math.max(0, t - 1));
      setNextOffset((o) => (o == null ? null : Math.max(0, o - 1)));
    },
    [],
  );

  // Toggle favorite (#122): flip the row's `sticky` flag in place, then re-sort the loaded
  // rows sticky-first to MIRROR the server sort (sticky desc, then the active timestamp tier —
  // created_at desc in "Creation date" mode, else last_mtime desc, #506) so a just-favorited row
  // floats to the top immediately and the rest don't briefly disagree with server order. The next
  // poll/refetch reconciles either way.
  const setSticky = useCallback(async (id: string, value: boolean) => {
    const r = await (value ? api.favorite(id) : api.unfavorite(id));
    const ts = (s: Session) =>
      orderRef.current === "created_at" ? (s.created_at ?? 0) : s.last_mtime;
    setSessions((prev) =>
      prev
        .map((s) => (s.id === id ? { ...s, sticky: r.sticky } : s))
        .sort((a, b) => Number(b.sticky) - Number(a.sticky) || ts(b) - ts(a)),
    );
  }, []);

  // Manual "Review now" (#356): run one AI review and fold the result into the row in
  // place (summary, badge, and the possibly-new display title) — no refetch flicker.
  // Returns the fresh payload so the sidebar can toast the outcome (#392); a failure
  // throws (with the server's `detail`) and leaves the last good row state untouched.
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
              // #481: keep the recap fresh too, so opening the session-brief modal right after a
              // sidebar Review-now shows the new recap rather than a stale/missing one.
              ai_recap: r.ai_recap,
            }
          : s,
      ),
    );
    return r;
  }, []);

  // Per-session exclude-from-review toggle (#356); the row stays, only the flag flips.
  const setReviewExcluded = useCallback(
    async (id: string, excluded: boolean) => {
      const r = await api.reviewExclude(id, excluded);
      setSessions((prev) =>
        prev.map((s) =>
          s.id === id ? { ...s, review_excluded: r.review_excluded } : s,
        ),
      );
    },
    [],
  );

  // Per-session Pulse-orchestration opt-out (#726). Managed-by-default, so this withdraws
  // agency for ONE session. Deliberately independent of review_excluded: the session stays
  // listed, stays summarised, stays flagged needs-you — it just stops being acted on.
  const setOrchestratorExcluded = useCallback(
    async (id: string, excluded: boolean) => {
      const r = await api.setOrchestratorExcluded(id, excluded);
      setSessions((prev) =>
        prev.map((s) =>
          s.id === id
            ? { ...s, orchestrator_excluded: r.orchestrator_excluded }
            : s,
        ),
      );
    },
    [],
  );

  // Reassign a session to a project entity — the keyboard-accessible equivalent of the map's
  // drag-to-reassign (#424 Phase 5). `ref` is the target entity, or `null` to unassign (back to
  // the folder fallback). Writes the explicit project_id via the metadata seam, then folds the
  // new resolution into the row in place. A project filter reconciles on the next poll.
  const setProject = useCallback(async (id: string, ref: ProjectRef | null) => {
    const pid = ref && ref.kind === "project" ? ref.id : null;
    await api.setSessionProject(id, pid);
    setSessions((prev) =>
      prev.map((s) =>
        s.id === id
          ? {
              ...s,
              project:
                ref && ref.kind === "project"
                  ? ref
                  : { kind: "folder", id: s.cwd, name: s.cwd },
            }
          : s,
      ),
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
    setTag,
    setArchived,
    setSticky,
    reviewRow,
    setReviewExcluded,
    setOrchestratorExcluded,
    setProject,
  };
}
