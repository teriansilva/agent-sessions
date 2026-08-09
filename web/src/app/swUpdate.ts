/** Service-worker update plumbing (#661). One module owns SW registration + update checks so the
 *  rest of the app only sees two facts: "has the SW swapped in a fresh shell?" and "apply the
 *  update now". Registration is explicit (vite.config sets `injectRegister: null`) via the
 *  `virtual:pwa-register` module, imported DYNAMICALLY inside `initSWUpdates()` — tests never call
 *  it, so vitest doesn't have to resolve the virtual module.
 *
 *  `registerType` stays `autoUpdate`: a new SW skipWaits + claims on its own, which refreshes the
 *  PRECACHE — but deliberately nothing here reloads the page on `controllerchange`. A forced
 *  reload mid-typing in a live terminal is unacceptable; the swap only flips state that the
 *  footer chip renders, and the reload happens on the user's tap (`applySWUpdate`). */

/** How often to ask the browser to re-check sw.js while the tab stays open. Cold navigations
 *  check on their own; this covers the long-lived installed-PWA tab that never navigates. */
const SW_RECHECK_MS = 60 * 60_000;

/** Fallback delay for the tap-to-reload path: if no `controllerchange` arrives (SW already
 *  swapped earlier, SW unsupported, update check found nothing), reload anyway so the tap never
 *  dead-ends. Generous enough for a small sw.js fetch + precache install on a slow link. */
const APPLY_FALLBACK_MS = 5_000;

let swapped = false;
let hadController =
  typeof navigator !== "undefined" && "serviceWorker" in navigator
    ? !!navigator.serviceWorker.controller
    : false;
let registration: ServiceWorkerRegistration | null = null;
const listeners = new Set<() => void>();

/** True once a NEW service worker has taken control of this tab (a controller SWAP — the initial
 *  claim on first install doesn't count). The fresh shell is already precached; a reload serves it. */
export const swHasSwapped = (): boolean => swapped;

/** Subscribe to the swap flipping true. Returns the unsubscribe. */
export function onSWSwap(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** Register the SW and arm the liveness checks. Called once from main.tsx; safe no-op where
 *  serviceWorker is unsupported or the virtual module is absent (vitest/jsdom, SSR). */
export async function initSWUpdates(): Promise<void> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator))
    return;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    // Only a swap counts: `hadController` distinguishes "new SW replaced the old one" from the
    // very first claim after install (where there was no controller to replace).
    if (hadController) {
      swapped = true;
      listeners.forEach((cb) => cb());
    }
    hadController = true;
  });
  try {
    const { registerSW } = await import("virtual:pwa-register");
    registerSW({
      onRegisteredSW(_url, r) {
        if (!r) return;
        registration = r;
        // A long-lived tab never re-checks sw.js on its own — poll hourly and the moment the
        // app is foregrounded (the realistic "I just came back after a release" beat).
        window.setInterval(
          () => void r.update().catch(() => {}),
          SW_RECHECK_MS,
        );
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) void r.update().catch(() => {});
        });
      },
    });
  } catch {
    // Dev server / unsupported build — version-mismatch detection still works without a SW.
  }
}

let applying = false;

/** The footer chip's tap action: refresh the SW (so the reload lands on the NEW precached shell,
 *  not the stale one — the flaw in the old banner's bare `location.reload()`), then reload once.
 *  Single-shot: repeated taps while a reload is in flight are ignored. */
export function applySWUpdate(): void {
  if (applying) return;
  applying = true;
  let done = false;
  const go = () => {
    if (done) return;
    done = true;
    window.location.reload();
  };
  const sw =
    typeof navigator !== "undefined" && "serviceWorker" in navigator
      ? navigator.serviceWorker
      : undefined;
  if (!sw) {
    go();
    return;
  }
  // If an update check finds a new SW it installs + activates (autoUpdate skipWaits), the
  // controller swaps, and we reload onto the fresh shell. If nothing new turns up (the SW
  // already swapped in the background, or the server bumped without a web change pre-#661),
  // the fallback timer reloads anyway.
  sw.addEventListener("controllerchange", go, { once: true });
  const check = registration
    ? registration.update().catch(() => {})
    : Promise.resolve();
  void check.finally(() => {
    window.setTimeout(go, APPLY_FALLBACK_MS);
  });
}
