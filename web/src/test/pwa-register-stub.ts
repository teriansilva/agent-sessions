/** Vitest stand-in for `virtual:pwa-register` (#661): the virtual module only exists inside the
 *  Vite build with the PWA plugin active — vitest's import-analysis can't resolve it even behind
 *  a dynamic import, so vitest.config aliases it here. Registration is a no-op in jsdom. */
export function registerSW(options?: unknown): (reloadPage?: boolean) => Promise<void> {
  void options; // accepted for signature-compatibility; nothing to register in jsdom
  return () => Promise.resolve();
}
