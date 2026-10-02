import { useEffect } from "react";
import type { ReactNode } from "react";
import { api } from "../lib/api";
import { markRosterFailed, setRoster } from "./engineRoster";

/** How long to wait before retrying a failed roster load. The roster is small and changes only at
 *  an app restart, so a slow steady retry is enough — and the last good roster is kept meanwhile. */
export const ROSTER_RETRY_MS = 15_000;

/** Loads `/api/engines` into the roster store (#853 P4). Mounted above the router, beside the
 *  other app-wide providers, so a navigation never refetches it. Renders its children at once:
 *  appearance falls back to the raw id until the roster lands, and every eligibility check waits
 *  for `rosterReady()` rather than guessing. */
export function EngineRosterProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      // Through a promise so even a synchronous throw is a FAILED LOAD (stale roster kept, retry),
      // never an exception that takes the app shell down with it.
      Promise.resolve()
        .then(() => api.engines())
        .then((d) => {
          // A response with no `engines` array is not a roster (a stub answering every route with
          // `{}`, a proxy error page): a FAILED load, never "no agents".
          if (!Array.isArray(d?.engines)) throw new Error("not a roster");
          if (alive) setRoster(d.engines, Array.isArray(d.problems) ? d.problems : []);
        })
        .catch(() => {
          if (!alive) return;
          markRosterFailed();
          timer = setTimeout(load, ROSTER_RETRY_MS);
        });
    };
    load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, []);
  return <>{children}</>;
}
