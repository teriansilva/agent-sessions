import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError, gotoChangePassword, setCsrfToken } from "../lib/api";
import type { AppConfig } from "../types/api";
import { ConfigCtx, ConfigRefreshCtx } from "./config";

/** Fetches /api/config on mount (retrying until the first read lands) and primes the CSRF token used by mutations.
 *  Children render immediately; consumers treat `null` as "not yet loaded".
 *  First-run forced password change (`must_change_password`) → route to the
 *  server-rendered /change-password before the (non-functional, 403-gated) app loads.
 *  Also provides a refetch via `ConfigRefreshCtx` (Hermes #367): settings panels call it
 *  when a save flips server-derived gating (e.g. ai_review.configured), so consumers like
 *  the sidebar's Review now/exclude controls update without a full reload. */
/** Backoff for a failed FIRST read: 1 s, doubling, capped at 30 s. */
const RETRY_MIN_MS = 1_000;
const RETRY_MAX_MS = 30_000;

export function ConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  /** WHICH READ THIS IS (#900 review 5, finding 8).
   *
   *  Every response was applied, in whatever order it arrived. Two saves and two refreshes is
   *  not an exotic sequence — each save triggers one — and an OLDER response landing last rolled
   *  the whole config back to the state before the newer save. In the playbook editor that is
   *  visible and expensive: the panel re-seeds from config when it is not dirty, and the newer
   *  save had just cleared `dirty`, so the editor showed the block the operator had already
   *  replaced and the next save persisted it.
   *
   *  A monotonic ticket, compared at resolution — the same idiom the mission console's row reads
   *  use. Refs rather than state: the value has to be readable from inside a promise that was
   *  created before the newer one existed. */
  const issued = useRef(0);
  /** Whether ANY read has been applied. Until one has, a failed read is retried (below). */
  const loaded = useRef(false);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryDelay = useRef(RETRY_MIN_MS);
  /** Whether the provider's effect is live. A read still in flight when it unmounts can reject
   *  AFTERWARDS — Home Free's teardown unmounts and then disposes the tunnel under it — and must
   *  not schedule a retry from a provider nobody is rendering. */
  const live = useRef(false);
  /** The retry timer calls through this, since `load` cannot name itself. */
  const loadRef = useRef<() => void>(() => {});
  const load = useCallback(() => {
    if (retryTimer.current !== null) {
      clearTimeout(retryTimer.current);
      retryTimer.current = null;
    }
    const tk = ++issued.current;
    api
      .config()
      .then((c) => {
        // ONLY THE LATEST *ISSUED* READ IS APPLIED (#900 review 7, finding 8).
        //
        // Comparing against the latest APPLIED ticket answered a different question — "is this
        // the newest response so far" — and that let a failure roll the config back: request B
        // is issued after a save, B fails, and A (issued BEFORE the save, so carrying pre-save
        // values) resolves afterwards and is applied. The panel then shows the block the
        // operator had already replaced, and re-seeds from it because the save had cleared
        // `dirty`.
        //
        // A response that a newer request has superseded describes a moment we have already
        // asked to move past, whether or not that newer request succeeded. Keeping what we had
        // is the honest outcome; the next refresh — every save issues one — resolves it.
        if (tk !== issued.current) return;
        if (c.must_change_password) {
          gotoChangePassword();
          return;
        }
        loaded.current = true;
        retryDelay.current = RETRY_MIN_MS;
        setCsrfToken(c.csrf);
        setConfig(c);
      })
      .catch((e: unknown) => {
        // What this is for is a network error or a 5xx, and the common one is the restart after
        // Update now: the reload that follows it can reach the server while it is still coming
        // up. A failed FIRST read used to be final for the page's lifetime — no config means no
        // operator tile (the only way into Settings), no CSRF token (every POST 403s) and "Ask
        // needs an AI endpoint" on a configured install. So until one read has landed, keep
        // asking, backing off. After one has, a failed refresh keeps what we had (see above) and
        // the next refresh resolves it.
        //
        // An auth refusal is not retried: `authGate` has already sent a 401 to /login and a
        // password-change 403 to /change-password, and asking again cannot change the answer.
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) return;
        if (!live.current || loaded.current || tk !== issued.current) return;
        const delay = retryDelay.current;
        retryDelay.current = Math.min(delay * 2, RETRY_MAX_MS);
        retryTimer.current = setTimeout(() => loadRef.current(), delay);
      });
  }, []);
  useEffect(() => {
    live.current = true;
    loadRef.current = load;
    load();
    // Coming back online, or back to the foreground (a phone PWA resumed from the background),
    // is the moment a failed first read is most likely to succeed — do not wait out the backoff.
    const retryNow = () => {
      if (!loaded.current && document.visibilityState === "visible") load();
    };
    window.addEventListener("online", retryNow);
    document.addEventListener("visibilitychange", retryNow);
    return () => {
      window.removeEventListener("online", retryNow);
      document.removeEventListener("visibilitychange", retryNow);
      // Fence the lifetime: no longer live, and every read issued so far is superseded, so
      // neither a late result nor a late failure acts. StrictMode's effect replay goes through
      // here too — its first read is superseded and the replay issues a fresh one.
      live.current = false;
      issued.current += 1;
      if (retryTimer.current !== null) {
        clearTimeout(retryTimer.current);
        retryTimer.current = null;
      }
    };
  }, [load]);
  return (
    <ConfigRefreshCtx.Provider value={load}>
      <ConfigCtx.Provider value={config}>{children}</ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>
  );
}
