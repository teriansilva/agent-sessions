/** The service-worker navigation denylist (#726 Phase 3).
 *
 * This test exists because of a specific, silent failure mode. Moving from `generateSW` to
 * `injectManifest` means the denylist is hand-written rather than generated. Dropping an entry
 * throws nothing, fails no build, and logs nothing — the SW simply begins answering a
 * server-rendered route with the cached React shell. The first symptom is a user staring at the
 * SPA where the login form should be, locked out of their own box, with a fix ("unregister the
 * service worker") they have no way to guess.
 *
 * So it asserts in BOTH directions. Denying too little locks people out; denying too much
 * breaks direct loads and reloads of real SPA routes, which is just as bad and much easier to
 * do by accident when tightening a regex.
 */
import { NavigationRoute } from "workbox-routing";
import { describe, expect, test } from "vitest";
import {
  isDenied,
  NAVIGATE_FALLBACK_DENYLIST,
  SERVER_RENDERED_PATHS,
  SPA_PATHS,
} from "./sw-denylist";

describe("navigation fallback denylist", () => {
  test.each(SERVER_RENDERED_PATHS)("denies the server-rendered %s", (path) => {
    expect(isDenied(path)).toBe(true);
  });

  test.each(SPA_PATHS)("keeps the fallback for the SPA route %s", (path) => {
    expect(isDenied(path)).toBe(false);
  });

  test("covers every entry the pre-injectManifest config denied", () => {
    // The literal list generateSW was configured with, transcribed from the pre-#726
    // vite.config.ts. If a migration drops one, this fails.
    const before = [
      "^\\/api",
      "^\\/ws",
      "^\\/term",
      "^\\/login",
      "^\\/logout",
      "^\\/change-password",
      "^\\/link",
      "^\\/healthz",
      // Widened from `$` to `(?:$|\\?)` deliberately: workbox matches against
      // pathname+search, so a bare `$` let /connect.html?x=1 through to the SPA shell.
      "^\\/connect\\.html(?:$|\\?)",
    ];
    const now = NAVIGATE_FALLBACK_DENYLIST.map((r) => r.source);
    for (const src of before) {
      expect(now, `denylist lost the ${src} entry in the injectManifest migration`).toContain(src);
    }
  });

  test("a new API surface is covered without touching the list", () => {
    // #726 adds /api/pulse/* routes. They must already be denied by the existing ^/api entry —
    // if this ever fails, someone has narrowed that regex.
    for (const p of [
      "/api/pulse/orchestrate",
      "/api/pulse/actions/abc/approve",
      "/api/pulse/push/subscribe",
      "/api/sessions/claude:x/orchestrator-exclude",
    ]) {
      expect(isDenied(p)).toBe(true);
    }
  });

  test("denies only by path prefix, so a SPA route merely containing the word is safe", () => {
    // `/settings/ai-review` contains "api" but is a real SPA route; an unanchored regex would
    // break it. The anchors matter.
    expect(isDenied("/settings/ai-review")).toBe(false);
    expect(isDenied("/s/claude/term-like-id")).toBe(false);
    expect(isDenied("/pulse")).toBe(false);
  });
});

// --- #730 review: test the WIRING, not just the helper -----------------------------------
//
// The critical defect this file previously missed: `isDenied()` was correct and every unit
// test passed, while the service worker denied NOTHING. The SW wrapped the helper in an
// adapter whose `test` took a `URL`, but workbox calls `regExp.test(url.pathname + url.search)`
// with a STRING — so the adapter read `.pathname` off a string, got `undefined`, and matched
// nothing. A cast silenced the type error.
//
// Unit-testing the predicate could never catch that. These tests drive the REAL
// `NavigationRoute` with the REAL denylist, which is the only thing that proves /login is
// actually excluded.
//
// Worth recording why the bug survived: workbox DOES validate the denylist
// (`WorkboxError('not-array-of-class')`) — but only when NODE_ENV !== 'production'. The
// production build strips that assertion, so the adapter threw in dev and silently denied
// nothing in the shipped worker. Passing a real RegExp[] satisfies that validation too.
describe("NavigationRoute integration (the wiring, not the predicate)", () => {
  const route = new NavigationRoute(async () => new Response("shell"), {
    denylist: NAVIGATE_FALLBACK_DENYLIST,
  });

  const matches = (path: string): boolean => {
    const url = new URL(`https://x${path}`);
    // happy-dom rejects `new Request(url, {mode: "navigate"})` outright. NavigationRoute
    // only reads `request.mode`, so a minimal stand-in drives the identical code path.
    const request = { mode: "navigate" } as Request;
    // `match` returns a truthy value when the SPA shell WOULD be served.
    return Boolean(route.match({ url, request, event: undefined as never, params: undefined }));
  };

  it.each(SERVER_RENDERED_PATHS)(
    "the real NavigationRoute refuses to shadow %s",
    (path) => {
      expect(matches(path)).toBe(false);
    },
  );

  it.each(SPA_PATHS)("the real NavigationRoute still serves the shell for %s", (path) => {
    expect(matches(path)).toBe(true);
  });

  it("denies a server route carrying a query string", () => {
    // workbox matches pathname+search, so an entry anchored with a bare `$` would leak here.
    expect(matches("/connect.html?device=1")).toBe(false);
    expect(matches("/login?next=/pulse")).toBe(false);
  });
});
