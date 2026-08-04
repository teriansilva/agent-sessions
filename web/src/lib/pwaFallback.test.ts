import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { isDenied, NAVIGATE_FALLBACK_DENYLIST } from "../sw-denylist";

// The SW navigation fallback serves index.html for client routes, but it must NOT
// shadow the server-rendered (Jinja) routes — login/logout/change-password/api/ws —
// or those pages dead-end on the SPA shell. /change-password is the one that bit us:
// after login the server 303s there, and without the denylist the SW served the React
// shell (no change-password route) → login appeared to fail.
//
// #726 Phase 3 moved the list out of vite.config.ts: the SW is now hand-written
// (injectManifest, so it can carry a `push` listener for Web Push), and the denylist lives in
// src/sw-denylist.ts shared by the worker and its tests. This guard keeps its intent and gets
// stronger — it asserts BEHAVIOUR rather than the presence of a string in a config file, so it
// would also catch a regex that is present but subtly wrong.
describe("PWA navigateFallbackDenylist", () => {
  it.each([
    "/api",
    "/ws",
    "/login",
    "/logout",
    "/change-password",
    "/link",
    "/healthz",
  ])("denylists the server-owned route %s", (route) => {
    expect(isDenied(route)).toBe(true);
    // …and the deeper paths under it, which is what actually gets navigated to.
    expect(isDenied(`${route}/something/deeper`)).toBe(true);
  });

  it("is the list the service worker actually consumes", () => {
    // A behavioural assertion is worthless if the SW ignores this module.
    // vitest runs with cwd = web/.
    const sw = readFileSync("src/sw.ts", "utf8");
    expect(sw).toContain('from "./sw-denylist"');
    expect(sw).toMatch(/isDenied\(/);
    expect(NAVIGATE_FALLBACK_DENYLIST.length).toBeGreaterThan(0);
  });

  it("is wired into the build as an injectManifest service worker", () => {
    // If the config ever reverts to generateSW, the hand-written push handler silently stops
    // shipping and notifications die with no error anywhere.
    const cfg = readFileSync("vite.config.ts", "utf8");
    expect(cfg).toMatch(/strategies:\s*"injectManifest"/);
    expect(cfg).toMatch(/filename:\s*"sw\.ts"/);
  });
});
