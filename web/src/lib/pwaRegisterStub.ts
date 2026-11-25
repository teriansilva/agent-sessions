/** No-op stand-in for `virtual:pwa-register` (#661): the virtual module only exists in builds
 *  with the PWA plugin active. Two build contexts alias it here:
 *  - vitest (vitest.config.ts) — import-analysis can't resolve the virtual module even behind a
 *    dynamic import, and jsdom has nothing to register anyway;
 *  - the standalone connect page (vite.config.connect.ts) — it deliberately excludes VitePWA
 *    (no service worker under the landing origin), but its app-mode dynamic-imports the real
 *    SPA whose module graph reaches swUpdate.ts. Registering nothing preserves the "no SW
 *    artifacts in dist-connect" invariant its deploy workflow asserts. */
export function registerSW(
  options?: unknown,
): (reloadPage?: boolean) => Promise<void> {
  void options; // accepted for signature-compatibility; nothing to register here
  return () => Promise.resolve();
}
