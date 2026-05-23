import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { Session, SessionsQuery } from "../types/api";

interface State {
  sessions: Session[];
  total: number;
  loading: boolean;
  error: string | null;
}

/** Read-only session list for the Phase-0 proof view. Pagination/filters land
 *  with the full sidebar (Phase 3); this just proves the typed API + render path. */
export function useSessions(query: SessionsQuery = {}) {
  const [state, setState] = useState<State>({
    sessions: [],
    total: 0,
    loading: true,
    error: null,
  });

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const page = await api.sessions({ limit: 50, ...query });
      setState({ sessions: page.sessions, total: page.total, loading: false, error: null });
    } catch (e) {
      const msg = e instanceof ApiError && e.status === 401 ? "Please sign in." : "Failed to load sessions.";
      setState((s) => ({ ...s, loading: false, error: msg }));
    }
    // query is spread into the call; callers pass a stable object
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.archived, query.q, query.project, query.engine]);

  useEffect(() => {
    // Intentional fetch-on-mount; load() sets loading=true then resolves. (Phase 3
    // replaces this with TanStack Query / the store.)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  return { ...state, reload: load };
}
