// @vitest-environment node
//
// Regression (#558 review): the PWA navigation fallback serves index.html for
// every navigation except the denylist, so the standalone /connect.html page
// must be denylisted or a service-worker-controlled browser gets the default SPA shell
// instead of the connect page.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { isDenied } from "../sw-denylist";

const viteConfig = readFileSync(
  fileURLToPath(new URL("../../vite.config.ts", import.meta.url)),
  "utf8",
);
const connectViteConfig = readFileSync(
  fileURLToPath(new URL("../../vite.config.connect.ts", import.meta.url)),
  "utf8",
);
const swSource = readFileSync(fileURLToPath(new URL("../sw.ts", import.meta.url)), "utf8");

describe("PWA navigation fallback", () => {
  it("denylists /connect.html so the SW never shadows it with index.html", () => {
    // The denylist moved out of vite.config.ts in #726 Phase 3: the SW is now hand-written
    // (injectManifest, so it can carry a `push` listener), and the list is shared between the
    // worker and its test in src/sw-denylist.ts. The INTENT of this #558 regression is
    // unchanged — /connect.html must never be shadowed by the SPA shell — so it is asserted
    // against the new source of truth rather than the old location.
    expect(isDenied("/connect.html")).toBe(true);
    // And the SW must actually consume that list, or the assertion above guards nothing.
    expect(swSource).toMatch(/from "\.\/sw-denylist"/);
    expect(swSource).toMatch(/isDenied\(/);
  });

  it("still builds connect.html as its own entry", () => {
    expect(viteConfig).toMatch(/connect:\s*"connect\.html"/);
  });

  it("keeps the public /connect/ build static, app-capable, and service-worker-free", () => {
    expect(connectViteConfig).toMatch(/base:\s*"\/connect\/"/);
    expect(connectViteConfig).toMatch(/publicDir:\s*"public"/);
    expect(connectViteConfig).toMatch(/input:\s*\{\s*connect:\s*"connect\.html"\s*\}/);
    expect(connectViteConfig).not.toMatch(/vite-plugin-pwa|VitePWA\s*\(|workbox:|registerType:/);
  });
});
