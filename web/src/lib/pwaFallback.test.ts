import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// The SW navigation fallback serves index.html for client routes, but it must NOT
// shadow the server-rendered (Jinja) routes — login/logout/change-password/api/ws —
// or those pages dead-end on the SPA shell. /change-password is the one that bit us:
// after login the server 303s there, and without the denylist the SW served the React
// shell (no change-password route) → login appeared to fail.
describe("PWA navigateFallbackDenylist", () => {
  // vitest runs with cwd = web/, where vite.config.ts lives.
  const cfg = readFileSync("vite.config.ts", "utf8");

  it.each(["/api", "/ws", "/login", "/logout", "/change-password", "/healthz"])(
    "denylists the server-owned route %s",
    (route) => {
      expect(cfg).toContain(`/^\\${route}/`);
    },
  );
});
