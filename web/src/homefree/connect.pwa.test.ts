// @vitest-environment node
//
// Regression (#558 review): the PWA navigation fallback serves index.html for
// every navigation except the denylist, so the standalone /connect.html page
// must be denylisted or a service-worker-controlled browser gets the default SPA shell
// instead of the connect page.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const viteConfig = readFileSync(
  fileURLToPath(new URL("../../vite.config.ts", import.meta.url)),
  "utf8",
);
const connectViteConfig = readFileSync(
  fileURLToPath(new URL("../../vite.config.connect.ts", import.meta.url)),
  "utf8",
);

describe("PWA navigation fallback", () => {
  it("denylists /connect.html so the SW never shadows it with index.html", () => {
    // The denylist entry for the standalone connect page must be present.
    expect(viteConfig).toMatch(/\/\^\\\/connect\\\.html\$\//);
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
