import { useEffect, useState } from "react";
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
}

/** Fetch (nearly) all non-archived sessions for the overview, paging to completion under a
 *  hard cap. Read-only; one shot on mount. */
export function useOverviewSessions(): OverviewSessions {
  const [state, setState] = useState<OverviewSessions>({
    sessions: [],
    loading: true,
    error: null,
    partial: false,
  });

  useEffect(() => {
    let alive = true;
    (async () => {
      const acc: Session[] = [];
      let offset = 0;
      let partial = false;
      try {
        for (let page = 0; page < MAX_PAGES; page++) {
          const res = await api.sessions({ limit: PAGE, offset, archived: false });
          acc.push(...res.sessions);
          if (res.next_offset == null) {
            offset = -1;
            break;
          }
          offset = res.next_offset;
          if (page === MAX_PAGES - 1) partial = true; // cap hit with more to come
        }
        if (alive) setState({ sessions: acc, loading: false, error: null, partial });
      } catch {
        if (alive)
          setState({ sessions: acc, loading: false, error: "Couldn’t load sessions.", partial });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return state;
}
