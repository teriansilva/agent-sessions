import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";

/** How often to poll `/api/version` for a server-side version change (#169). 5 minutes is
 *  often enough to notice a deploy within a coffee break but not enough to be wasteful;
 *  `visibilitychange` triggers a poll the instant the tab is foregrounded again, which is
 *  the realistic "I just came back" moment. */
const POLL_MS = 5 * 60_000;

interface State {
  /** The first version we observed for this tab — what the loaded SPA bundle was built
   *  against. Stays fixed for the lifetime of the tab. `null` until the first /api/version
   *  call resolves. */
  initial: string | null;
  /** True if we've observed a version that differs from `initial` — the user is now running
   *  a stale bundle and needs to reload. Latches: once true it stays true. */
  hasNewVersion: boolean;
}

/** Polls `/api/version` and reports when the running tab is stale. We don't auto-reload —
 *  that would yank the page out from under a user in the middle of a turn — we just surface
 *  the fact via `hasNewVersion`; a small banner offers an explicit "Reload" click.
 *
 *  Sibling to #160 (lazy chunks) and #165 (deploy keeps sessions alive). The deploy still
 *  produces a stale main bundle for any tab open across it — this is how the tab finds out. */
export function useAppVersion(): State {
  const [initial, setInitial] = useState<string | null>(null);
  const [hasNewVersion, setHasNewVersion] = useState(false);
  // Pin the initial version we saw in a ref too so the polling loop has it without a
  // closure-captured stale read of the state. Set in lockstep with `initial`.
  const initialRef = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const { version } = await api.version();
        if (!alive) return;
        if (initialRef.current == null) {
          initialRef.current = version;
          setInitial(version);
          return;
        }
        if (version !== initialRef.current) setHasNewVersion(true);
      } catch {
        /* transient — ignore, try again on the next tick */
      }
    };
    // First check is immediate so the tab learns its own version on load.
    void check();
    const id = window.setInterval(check, POLL_MS);
    // Visibility-change: poll the instant the tab is foregrounded. Catches the common
    // "we deployed while you were in another tab" case much faster than the interval.
    const onVis = () => {
      if (!document.hidden) void check();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return { initial, hasNewVersion };
}
