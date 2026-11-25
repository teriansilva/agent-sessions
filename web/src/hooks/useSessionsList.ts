import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SessionCounts } from "../app/sessionsStore";
import { useSectionState } from "../app/sectionState";
import { useConfig } from "../app/config";
import { api, ApiError } from "../lib/api";
import { CLUSTERS_CHANGED_EVENT } from "../lib/clusterEvents";
import {
  SESSION_MISSION_CHANGED_EVENT,
  type SessionMissionChange,
} from "../lib/missionEvents";
import {
  projectAfterMove,
  reviewPatch,
  SESSION_ROW_CHANGED_EVENT,
  type SessionRowChange,
} from "../lib/sessionRowEvents";
import type {
  ProjectRef,
  Session,
  SessionClusterFacet,
  SessionTagFacet,
  SessionMissionFacet,
  SessionsPage,
  SessionsQuery,
} from "../types/api";

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
  /** A mission id, `none` for sessions no mission holds, or "" for all (#948). Optional because a
   *  section state saved before it existed does not carry it. */
  mission?: string;
  /** A cluster id, `none` for sessions in no cluster and no mission, or "" for all (#1997).
   *  Optional for the same reason as `mission`. */
  cluster?: string;
  /** A custom tag, or "" for all (#2338). Optional for the same reason as `mission`. */
  tag?: string;
  archived: boolean;
  /** The Shared scope (#2198): the list shows what peers share with this instance instead of
   *  this instance's own sessions. Client-only: it is never part of the `/api/sessions` query,
   *  and `archived` keeps its value under it, so entering or leaving Shared alone asks the
   *  server nothing new. Optional because a section state saved before it does not carry it. */
  shared?: boolean;
  /** The Shared scope's own search (title or peer name, matched in the browser). A separate
   *  value from `q`: neither search ever narrows the other scope's list. */
  sharedQ?: string;
}

/** Which list the sidebar shows (#2198). */
export type SessionScope = "active" | "archived" | "shared";

export function scopeOf(filters: Filters): SessionScope {
  return filters.shared ? "shared" : filters.archived ? "archived" : "active";
}

/** The filter patch that selects a scope. Selecting Shared leaves `archived` alone (the query
 *  must not change); selecting Active or Archived leaves Shared and sets the server's flag. */
export function scopePatch(scope: SessionScope): Partial<Filters> {
  return scope === "shared"
    ? { shared: true }
    : { shared: false, archived: scope === "archived" };
}

const EMPTY: Filters = {
  q: "",
  project: "",
  engine: "",
  mission: "",
  cluster: "",
  tag: "",
  archived: false,
  shared: false,
  sharedQ: "",
};

interface Facets {
  projects: ProjectRef[];
  engines: string[];
  /** Absent when the server could not read the mission store (#948). */
  missions?: SessionMissionFacet[];
  no_mission?: number;
  /** Absent when the server could not tell, or clusters are off (#1997). */
  clusters?: SessionClusterFacet[];
  /** Absent on servers older than #2338. */
  tags?: SessionTagFacet[];
}

/** The sidebar's data layer: filtered + paginated session list with server facets.
 *  Changing a filter resets to page 0; `loadMore` appends the next page. */
export function useSessionsList() {
  const [filters, setFilters] = useSectionState<Filters>(
    "sessions.filters",
    EMPTY,
  );
  const [sessions, setSessions] = useState<Session[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [total, setTotal] = useState(0);
  // Footer counts (#1085). `null` until a server that sends them answers.
  const [counts, setCounts] = useState<SessionCounts | null>(null);
  const [facets, setFacets] = useState<Facets>({ projects: [], engines: [] });
  /** The server could not apply the mission filter (#948): say so, rather than "no sessions". */
  const [missionFilterUnavailable, setMissionFilterUnavailable] = useState(false);
  /** The same for the cluster filter (#1997). */
  const [clusterFilterUnavailable, setClusterFilterUnavailable] = useState(false);
  /** Subagents needing the operator whose main session the active filter dropped (#2325). */
  const [attentionOutside, setAttentionOutside] = useState<
    SessionsPage["attention_outside"] | null
  >(null);
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
  // A membership change moved rows into or out of the filtered set, so `nextOffset` indexes a list
  // the server no longer has. Set by the change; cleared only by a SUCCESSFUL replacement read from
  // offset 0. Until then Load more must not append by offset (#953 review 4797).
  const reconcile = useRef(false);

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
      mission: filters.mission || undefined,
      cluster: filters.cluster || undefined,
      tag: filters.tag || undefined,
      archived: filters.archived,
      limit: PAGE,
    }),
    [
      debouncedQ,
      filters.project,
      filters.engine,
      filters.mission,
      filters.cluster,
      filters.tag,
      filters.archived,
    ],
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
      opts: { silent?: boolean; limit?: number; keepOnError?: boolean } = {},
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
        if (replace && offset === 0) reconcile.current = false;
        setSessions((prev) => {
          const merged = replace ? page.sessions : [...prev, ...page.sessions];
          const seen = new Set<string>(); // dedupe by id (defensive)
          return merged.filter((s) =>
            seen.has(s.id) ? false : (seen.add(s.id), true),
          );
        });
        setNextOffset(page.next_offset);
        setTotal(page.total);
        if (typeof page.live_total === "number") {
          setCounts({ total: page.total, live: page.live_total });
        }
        setFacets(page.facets);
        setMissionFilterUnavailable(Boolean(page.mission_filter_unavailable));
        setClusterFilterUnavailable(Boolean(page.cluster_filter_unavailable));
        if (offset === 0) setAttentionOutside(page.attention_outside ?? null);
      } catch (e) {
        if (gen !== reqId.current) return;
        // Background failure → keep existing rows, no flicker. `keepOnError` is a VISIBLE read
        // (it gates Load more) whose failure must not replace the rows with an error either.
        if (opts.silent || opts.keepOnError) return;
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

  // Refetch every loaded row when "Rename sessions with the reviewed title" flips (#1510): the
  // server resolves titles, so the open list must re-read to show (or drop) the AI titles now
  // rather than on the next poll. Same prev-compare as the order above: the config's first load
  // is not a flip. A VISIBLE replacement read, like the mission change below, never a silent
  // one: it may supersede a bootstrap, filter reload or Load more still in flight, and only a
  // visible read clears `loading` when it settles (a silent one would strand it, #168). A
  // failure keeps the rows; the poll converges.
  const renames = useConfig()?.session_review?.rename_sessions;
  const seenRenames = useRef(renames);
  useEffect(() => {
    const prev = seenRenames.current;
    seenRenames.current = renames;
    if (renames !== undefined && prev !== undefined && prev !== renames)
      void fetchPage(0, true, {
        limit: Math.max(PAGE, sessionsCount.current),
        keepOnError: true,
      });
  }, [renames, fetchPage]);

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
    // The replacement read after a membership change failed, so the cursor is still the old list's.
    // Load more IS that replacement, over the loaded rows plus one more page, never an append. It
    // fails like any Load more, visibly, and the next press retries it.
    if (reconcile.current) {
      void fetchPage(0, true, { limit: sessionsCount.current + PAGE });
      return;
    }
    void fetchPage(nextOffset, false);
  }, [loading, nextOffset, fetchPage]);

  const update = useCallback(
    (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch })),
    [setFilters],
  );
  // Resets the LOCAL filters. The Shared scope's search is not one of them (#2198).
  const clear = useCallback(
    () => setFilters((f) => ({ ...EMPTY, sharedQ: f.sharedQ ?? "" })),
    [setFilters],
  );

  // The loaded rows and the list's archived scope, readable from a callback without re-creating
  // it (a row patch must know whether the row is here, and which archived-state the list shows).
  const rowsRef = useRef<Session[]>([]);
  const archivedScope = useRef(filters.archived);
  useEffect(() => {
    rowsRef.current = sessions;
    archivedScope.current = filters.archived;
  });

  /** Fold a change the server has already recorded into the loaded rows, in place. ONE function
   *  for the row menu's own handlers below and for a change announced from another surface
   *  (`sessionRowEvents`, #2173: the session pane's menu), so the two cannot drift.
   *
   *  - `archived` leaving the list's scope drops the row. The list is scoped to one
   *    archived-state, so the server's set shrank by one and every still-unloaded row shifted
   *    down one offset: `nextOffset` follows, or the next Load more would skip the first
   *    unloaded row. With everything loaded (`nextOffset == null`) there is nothing to backfill.
   *  - `sticky` re-sorts the loaded rows sticky-first, mirroring the server sort (sticky desc,
   *    then created_at desc in "Creation date" mode, else last_mtime desc, #506), so a
   *    just-favorited row floats up at once. The next poll reconciles either way.
   *  - Anything else patches the row where it stands.
   *
   *  Returns whether the list had the row. */
  const applyRowPatch = useCallback((id: string, patch: Partial<Session>): boolean => {
    const has = rowsRef.current.some((s) => s.id === id);
    if (patch.archived !== undefined && patch.archived !== archivedScope.current) {
      if (!has) return false;
      setSessions((prev) => prev.filter((s) => s.id !== id));
      setTotal((t) => Math.max(0, t - 1));
      setNextOffset((o) => (o == null ? null : Math.max(0, o - 1)));
      return true;
    }
    if (!has) return false;
    const ts = (s: Session) =>
      orderRef.current === "created_at" ? (s.created_at ?? 0) : s.last_mtime;
    setSessions((prev) => {
      const next = prev.map((s) => (s.id === id ? { ...s, ...patch } : s));
      return patch.sticky === undefined
        ? next
        : next.sort((a, b) => Number(b.sticky) - Number(a.sticky) || ts(b) - ts(a));
    });
    return true;
  }, []);

  // Rename in place: the row stays in the current view, only its title changes.
  const renameRow = useCallback(
    async (id: string, title: string) => {
      const r = await api.rename(id, title);
      applyRowPatch(id, { title: r.title });
    },
    [applyRowPatch],
  );

  // The LATEST fetch, for continuations that run after an await or from a window event: a
  // `fetchPage` captured earlier carries the query of its moment, and a read issued with it after
  // the filters changed would be the newest request and win (#2338 review).
  const fetchRef = useRef(fetchPage);
  useEffect(() => {
    fetchRef.current = fetchPage;
  }, [fetchPage]);

  // Set/clear the custom tag (#551): patch the row in place from the server's echoed value
  // (already trimmed + capped), so the summary line reflects it without a refetch.
  const setTag = useCallback(
    async (id: string, tag: string) => {
      const r = await api.setTag(id, tag);
      applyRowPatch(id, { tag: r.tag });
      // …and then ask the server (#2338): the tag filter's options and members come from it, so a
      // patch cannot add a new tag to the dropdown or move the row out of a `tag` filter. The same
      // visible replacement read as a mission change.
      // The save may resolve after the filters changed: read with the CURRENT query.
      reconcile.current = true;
      void fetchRef.current(0, true, {
        limit: Math.max(PAGE, sessionsCount.current),
        keepOnError: true,
      });
    },
    [applyRowPatch],
  );

  // Toggle archived: after the flip the row leaves the current view, so it is dropped locally
  // (avoids a full refetch + flicker). See `applyRowPatch`.
  const setArchived = useCallback(
    async (id: string, currentlyArchived: boolean) => {
      await (currentlyArchived ? api.unarchive(id) : api.archive(id));
      applyRowPatch(id, { archived: !currentlyArchived });
    },
    [applyRowPatch],
  );

  // Toggle favorite (#122): flip the row's `sticky` flag in place and re-sort (`applyRowPatch`).
  const setSticky = useCallback(
    async (id: string, value: boolean) => {
      const r = await (value ? api.favorite(id) : api.unfavorite(id));
      applyRowPatch(id, { sticky: r.sticky });
    },
    [applyRowPatch],
  );

  // Manual "Review now" (#356): run one AI review and fold the result into the row in
  // place (summary, badge, and the possibly-new display title) — no refetch flicker.
  // Returns the fresh payload so the sidebar can toast the outcome (#392); a failure
  // throws (with the server's `detail`) and leaves the last good row state untouched.
  const reviewRow = useCallback(
    async (id: string) => {
      const r = await api.reviewNow(id);
      applyRowPatch(id, reviewPatch(r));
      return r;
    },
    [applyRowPatch],
  );

  // Per-session exclude-from-review toggle (#356); the row stays, only the flag flips.
  const setReviewExcluded = useCallback(
    async (id: string, excluded: boolean) => {
      const r = await api.reviewExclude(id, excluded);
      applyRowPatch(id, { review_excluded: r.review_excluded });
    },
    [applyRowPatch],
  );

  // Per-session Pulse-orchestration opt-out (#726). Managed-by-default, so this withdraws
  // agency for ONE session. Deliberately independent of review_excluded: the session stays
  // listed, stays summarised, stays flagged needs-you — it just stops being acted on.
  const setOrchestratorExcluded = useCallback(
    async (id: string, excluded: boolean) => {
      const r = await api.setOrchestratorExcluded(id, excluded);
      applyRowPatch(id, { orchestrator_excluded: r.orchestrator_excluded });
    },
    [applyRowPatch],
  );

  // Reassign a session to a project entity — the keyboard-accessible equivalent of the map's
  // drag-to-reassign (#424 Phase 5). `ref` is the target entity, or `null` to unassign (back to
  // the folder fallback). Writes the explicit project_id via the metadata seam, then folds the
  // new resolution into the row in place. A project filter reconciles on the next poll.
  const setProject = useCallback(
    async (id: string, ref: ProjectRef | null) => {
      const pid = ref && ref.kind === "project" ? ref.id : null;
      await api.setSessionProject(id, pid);
      const cwd = rowsRef.current.find((s) => s.id === id)?.cwd ?? "";
      applyRowPatch(id, { project: projectAfterMove(ref, cwd) });
    },
    [applyRowPatch],
  );

  // A session's mission changed from anywhere in this tab — the row menu or the pane header (#948).
  // Applied to the list's OWN rows, which the shared store mirrors, so every consumer flips at once.
  useEffect(() => {
    const onChange = (e: Event) => {
      const d = (e as CustomEvent<SessionMissionChange>).detail;
      if (!d) return;
      setSessions((prev) =>
        prev.map((s) => (s.id === d.key ? { ...s, mission: d.mission } : s)),
      );
      // …and then ask the server (#953 review). A patch cannot move the row out of a `mission`
      // filter, correct the total or facets, or tell the next Load more that the filtered set just
      // shrank — so that page would start one row late and skip a session. One replacement read
      // over every loaded row supersedes any read that began before the adoption (reqId), and it
      // is visible, so Load more waits for it; if it fails the rows stay and the poll converges.
      reconcile.current = true;
      void fetchRef.current(0, true, {
        limit: Math.max(PAGE, sessionsCount.current),
        keepOnError: true,
      });
    };
    window.addEventListener(SESSION_MISSION_CHANGED_EVENT, onChange);
    return () => window.removeEventListener(SESSION_MISSION_CHANGED_EVENT, onChange);
  }, []);

  // Clusters changed from anywhere in this tab (#1997): a move, a rename, a merge, a dissolve, a
  // re-cluster. The rows' chips, the `cluster` filter's members and the tree's counts all come
  // from the server, so this is the same visible replacement read as a mission change.
  useEffect(() => {
    const onChange = () => {
      reconcile.current = true;
      void fetchRef.current(0, true, {
        limit: Math.max(PAGE, sessionsCount.current),
        keepOnError: true,
      });
    };
    window.addEventListener(CLUSTERS_CHANGED_EVENT, onChange);
    return () => window.removeEventListener(CLUSTERS_CHANGED_EVENT, onChange);
  }, []);

  // A row changed from another surface in this tab (#2173: the session pane's menu). The server
  // has already recorded it, so it is applied to the list's OWN rows exactly as the row menu's
  // handlers apply theirs. One case a patch cannot settle: an archive flip that brings a session
  // INTO this list's scope (Unarchive pressed in an archived session's pane) has no row here to
  // patch, so it is the same visible replacement read as a mission change.
  useEffect(() => {
    const onChange = (e: Event) => {
      const d = (e as CustomEvent<SessionRowChange>).detail;
      if (!d) return;
      const patched = applyRowPatch(d.key, d.patch);
      // A tag change also moves the tag filter's options and members (#2338), which only the
      // server can recompute: ask it, whether or not this list holds the row.
      const tagChanged = d.patch.tag !== undefined;
      const intoScope =
        !patched && d.patch.archived !== undefined && d.patch.archived === archivedScope.current;
      if (!tagChanged && !intoScope) return;
      reconcile.current = true;
      void fetchRef.current(0, true, {
        limit: Math.max(PAGE, sessionsCount.current),
        keepOnError: true,
      });
    };
    window.addEventListener(SESSION_ROW_CHANGED_EVENT, onChange);
    return () => window.removeEventListener(SESSION_ROW_CHANGED_EVENT, onChange);
  }, [applyRowPatch]);

  return {
    sessions,
    total,
    counts,
    missionFilterUnavailable,
    clusterFilterUnavailable,
    attentionOutside,
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
