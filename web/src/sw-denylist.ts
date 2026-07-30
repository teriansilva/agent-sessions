/** Routes the SPA's navigation fallback must NEVER shadow (#64, #558, #726 Phase 3).
 *
 *  This list used to be inline in `vite.config.ts` and *generated* into the service worker by
 *  workbox's `generateSW`. Moving to `injectManifest` means we hand-write the SW — and the
 *  single biggest risk in that migration is silently dropping an entry. A missing one doesn't
 *  fail a build or throw at runtime: the SW simply starts answering a server-rendered route
 *  with the cached React shell, and the user hits a page that renders the SPA where a login
 *  form or a JSON body should be. That failure is invisible until someone is locked out.
 *
 *  So the list lives HERE, imported by both the service worker and its test, and the test
 *  asserts every server-rendered surface is covered. One source of truth, one place to add to
 *  when a new server-rendered route lands.
 *
 *  What belongs on this list: anything the FastAPI app serves itself — Jinja pages, JSON APIs,
 *  websockets, and the standalone Home Free connect shell. What does NOT: SPA routes like
 *  `/pulse` or `/s/:engine/:id`, which need the fallback to survive a direct load or a reload.
 */
export const NAVIGATE_FALLBACK_DENYLIST: RegExp[] = [
  /^\/api/,
  /^\/ws/,
  /^\/term/,
  /^\/login/,
  /^\/logout/,
  // The forced first-login change has no SPA route; without this the SW serves the React
  // shell there and login dead-ends (#463).
  /^\/change-password/,
  // Device-link QR sign-in (#650): server-rendered approval page + JSON routes.
  /^\/link/,
  /^\/healthz/,
  // The standalone Home Free connect page is its own precached shell — the SPA index.html
  // fallback must not shadow it for browsers already controlled by the app's SW (#27/#558).
  // Anchored on end-or-query, NOT bare `$`: workbox matches against `pathname + search`, so
  // a plain `$` would let `/connect.html?foo=1` fall through to the SPA shell.
  /^\/connect\.html(?:$|\?)/,
];

/** Every server-rendered path that MUST be excluded, as concrete examples. The test walks
 *  these and fails if any is not matched — regexes are easy to typo in ways that still look
 *  right (`/^\/logout/` vs `/^\/log-out/`), and a concrete path catches that. */
export const SERVER_RENDERED_PATHS: string[] = [
  "/api/sessions",
  "/api/pulse/orchestrator",
  "/api/pulse/evidence/claude:abc",
  "/ws/term/abc",
  "/term/abc",
  "/login",
  "/logout",
  "/change-password",
  "/link/approve",
  "/healthz",
  "/connect.html",
];

/** SPA routes that must KEEP the navigation fallback — denying one of these breaks a direct
 *  load or a reload of a real page, which is the opposite failure and just as bad. */
export const SPA_PATHS: string[] = [
  "/",
  "/pulse",
  "/overview",
  "/settings",
  "/settings/ai-review",
  "/s/claude/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
];

export function isDenied(pathname: string): boolean {
  return NAVIGATE_FALLBACK_DENYLIST.some((re) => re.test(pathname));
}
