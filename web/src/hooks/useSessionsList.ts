import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { Session, SessionsQuery } from "../types/api";

const PAGE = 20;

export interface Filters {
  q: string;
  project: string;
  engine: string;
  archived: boolean;
}

const EMPTY: Filters = { q: "", project: "", engine: "", archived: false };

interface Facets {
  projects: string[];
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

  const fetchPage = useCallback(
    async (offset: number, replace: boolean) => {
      const gen = ++reqId.current;
      setLoading(true);
      setError(null);
      try {
        const page = await api.sessions({ ...query, offset });
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
        setError(
          e instanceof ApiError && e.status === 401 ? "Please sign in." : "Failed to load sessions.",
        );
      } finally {
        if (gen === reqId.current) setLoading(false);
      }
    },
    [query],
  );

  useEffect(() => {
    // Reload from page 0 whenever the filters change.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchPage(0, true);
  }, [fetchPage]);

  const loadMore = useCallback(() => {
    if (loading || nextOffset == null) return;
    void fetchPage(nextOffset, false);
  }, [loading, nextOffset, fetchPage]);

  const update = useCallback((patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch })), []);
  const clear = useCallback(() => setFilters(EMPTY), []);

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
  };
}
