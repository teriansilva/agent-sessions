import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { applySWUpdate, onSWSwap, swHasSwapped } from "./swUpdate";

/** How often to poll `/api/version` for a server-side version change (#169). 5 minutes is
 *  often enough to notice a deploy within a coffee break but not enough to be wasteful;
 *  `visibilitychange` triggers a poll the instant the tab is foregrounded again, which is
 *  the realistic "I just came back" moment. */
const POLL_MS = 5 * 60_000;

interface State {
  /** The version THIS bundle was built as — the compile-time stamp (`__APP_VERSION__`),
   *  `"dev"` for unstamped dev/CI builds. Unlike the first `/api/version` response (the
   *  pre-#661 baseline), this actually proves what the tab loaded. */
  current: string;
  /** The server's installed version — latest `/api/version` value, `null` until the first
   *  poll resolves. */
  server: string | null;
  /** What the footer shows: the build stamp when real, else the server's version (a dev
   *  build honestly reports what the server runs), else null (nothing to show yet). */
  displayVersion: string | null;
  /** True when a newer shell is ready: the server version differs from this bundle's stamp
   *  (stamped builds only), or the SW already swapped a fresh shell in (#661). Drives the
   *  footer's TAP TO RELOAD chip — never an auto-reload. */
  updateReady: boolean;
  /** Apply the update: refresh the SW so the reload lands on the NEW precached shell (a bare
   *  `location.reload()` under a stale SW re-serves the stale shell), then reload once. */
  applyUpdate: () => void;
}

/** One hook owns the whole update surface (#661, replacing the #169 banner): the build stamp,
 *  the `/api/version` comparison, the SW-swap signal, and the apply action. Consumed by the
 *  status-footer version tag + update chip; deliberately no auto-reload anywhere. */
export function useAppVersion(current: string = __APP_VERSION__): State {
  const [server, setServer] = useState<string | null>(null);
  const [swSwapped, setSwSwapped] = useState(swHasSwapped);

  useEffect(() => onSWSwap(() => setSwSwapped(true)), []);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const { version } = await api.version();
        if (alive) setServer(version);
      } catch {
        /* transient — ignore, try again on the next tick */
      }
    };
    // First check is immediate so the footer learns the server version on load.
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

  // `"dev"` disables the mismatch path: an unstamped build can't claim to know it's stale.
  const mismatch = current !== "dev" && server !== null && server !== current;
  const applyUpdate = useCallback(() => applySWUpdate(), []);
  return {
    current,
    server,
    displayVersion: current !== "dev" ? current : server,
    updateReady: mismatch || swSwapped,
    applyUpdate,
  };
}
