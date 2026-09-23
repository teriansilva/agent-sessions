import { useEffect, useState } from "react";

import { api } from "../lib/api";
import type { AgentCounts } from "../types/api";

/** How often the bottom bar re-asks — the sidebar list's own poll interval. */
export const AGENT_COUNTS_POLL_MS = 15_000;

/** Host-wide running / working agents for the bottom bar (#1085).
 *
 *  Its own poll rather than a read of the sidebar's data: the session list is not mounted on the
 *  Missions route, so a count derived from it froze there. `GET /api/agents` is one TTL-cached
 *  socket probe on the server — no transcript walk — so polling it is cheap. Paused while the tab
 *  is hidden; a failed read keeps the last good value rather than flashing a zero. */
export function useAgentCounts(): AgentCounts | null {
  const [counts, setCounts] = useState<AgentCounts | null>(null);
  useEffect(() => {
    let alive = true;
    let id: number | undefined;
    const read = () => {
      api
        .agents()
        .then((c) => {
          if (alive) setCounts(c);
        })
        .catch(() => {});
    };
    const start = () => {
      read();
      if (id == null) id = window.setInterval(read, AGENT_COUNTS_POLL_MS);
    };
    const stop = () => {
      if (id != null) window.clearInterval(id);
      id = undefined;
    };
    const onVis = () => (document.hidden ? stop() : start());
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);
  return counts;
}
