import { type ComponentType, lazy } from "react";

/** Wraps `React.lazy` to recover from stale-chunk failures after a deploy (#160).
 *
 * After we ship a new release, hashed asset filenames change. A tab open across the deploy
 * still holds the OLD index.html / module graph; when it lazy-imports a chunk it fetches a
 * hash that 404s, surfacing as a blank Suspense + a console error like
 * `Failed to fetch dynamically imported module: …/Overview-<hash>.js`.
 *
 * The fix: on the first chunk-load failure for a given `key`, set a one-shot sessionStorage
 * guard and `location.reload()` — the reload picks up the new index.html (and the new chunk
 * hash) and the import succeeds. On the *second* failure with the guard already set we DO NOT
 * reload again (no loop): the loader rethrows so an upstream error boundary can render a
 * recoverable fallback. On a successful import we clear the guard so a *later* deploy gets a
 * fresh reload chance. Non-chunk errors (real component bugs) are never reloaded — they bubble
 * straight to the error boundary like any normal render error. */

const GUARD_PREFIX = "tr-lazy-reload:";

function isChunkLoadError(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  const m = e.message || "";
  // Vite throws this exact phrase across browsers; Safari uses "Importing a module script
  // failed". `e.name === 'ChunkLoadError'` covers a possible webpack-style fallback.
  return (
    e.name === "ChunkLoadError" ||
    /Failed to fetch dynamically imported module/i.test(m) ||
    /Importing a module script failed/i.test(m)
  );
}

export function lazyWithReload<T extends { default: ComponentType<unknown> }>(
  factory: () => Promise<T>,
  key: string,
) {
  const guardKey = GUARD_PREFIX + key;
  return lazy(() =>
    factory().then(
      (mod) => {
        try {
          sessionStorage.removeItem(guardKey);
        } catch {
          /* private mode / storage disabled — ignore */
        }
        return mod;
      },
      (err) => {
        if (!isChunkLoadError(err)) throw err; // real bug → bubble unchanged
        let alreadyReloaded: boolean;
        try {
          alreadyReloaded = sessionStorage.getItem(guardKey) === "1";
        } catch {
          throw err; // storage disabled → cannot guard against a loop; surface fallback
        }
        if (alreadyReloaded) throw err; // already tried; let the boundary handle it
        sessionStorage.setItem(guardKey, "1");
        // Trigger a full reload and never resolve, so Suspense stays in its fallback until
        // the page is replaced. (Resolving with a stub would briefly mount it.)
        window.location.reload();
        return new Promise<T>(() => {});
      },
    ),
  );
}
